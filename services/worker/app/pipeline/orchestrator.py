"""Master pipeline orchestrator: connects all modules into the 12-step process_job flow."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from ..ai.base_provider import (
    AnalysisRequest,
    AnalysisResponse,
    BaseAIProvider,
    CategoryDetection,
    TIER_1,
    TIER_2,
    apply_escalation_overrides,
)
from ..ai.exceptions import TransientAIError
from ..ai.prompt_manager import format_file_size
from ..config import Config
from ..database.repositories import (
    AuditLogRepository,
    EventRepository,
    FileHashRepository,
    VerdictRepository,
)
from ..extraction import get_extractor
from ..extraction.base import ExtractionResult
from ..extraction.image_preprocessor import (
    preprocess_image,
    render_pdf_pages_as_images,
)
from ..extraction.ocr_extractor import OcrExtractor
from ..extraction.transcript_extractor import TranscriptExtractor
from ..extraction.whisper_client import WhisperClient
from ..graph_api.client import GraphClient
from ..graph_api.stream_captions import get_stream_captions
from ..graph_api.transcript import (
    get_meeting_organizer_id,
    get_meeting_transcript,
    is_teams_recording,
    parse_recording_timestamp,
)
from ..notifications.base_notifier import AlertPayload, NotificationDispatcher
from .classifier import Action, Category, ClassificationResult, FileClassifier
from .cleanup import Cleanup
from .downloader import DownloadError, FileDownloader
from ..graph_api.client import AccessDeniedError, FileNotFoundError as GraphFileNotFoundError, GraphAPIError
from .hasher import FileHasher
from .metadata import MetadataPrescreen
from .retry import retry_with_backoff
from .second_look import needs_second_look, run_second_look

logger = logging.getLogger(__name__)


async def process_job(
    job_data: dict,
    config: Config,
    db_pool: Any,
    redis: Any,
    ai_provider: BaseAIProvider,
    notifier_dispatcher: NotificationDispatcher,
    second_look_provider: Optional[BaseAIProvider] = None,
    av_semaphore: Optional[asyncio.Semaphore] = None,
) -> None:
    """Execute the full 12-step pipeline for a single sharing event.

    Parameters
    ----------
    job_data:
        Raw job dict from the Redis queue (parsed into an attribute-accessible
        object internally).
    config:
        Worker configuration.
    db_pool:
        asyncpg connection pool.
    redis:
        aioredis / redis.asyncio connection.
    ai_provider:
        Configured AI provider instance.
    notifier_dispatcher:
        Dispatcher that fans out alerts to all configured channels.
    """
    job = _DictJob(job_data)
    event_id: str = job.event_id

    # Repositories
    event_repo = EventRepository(db_pool)
    verdict_repo = VerdictRepository(db_pool)
    file_hash_repo = FileHashRepository(db_pool)
    audit_repo = AuditLogRepository(db_pool)

    # Pipeline-wide state
    downloaded_file: Optional[Path] = None
    file_hash: Optional[str] = None
    metadata: Dict[str, Any] = {}
    classification: Optional[ClassificationResult] = None
    analysis_response: Optional[AnalysisResponse] = None
    analysis_mode: str = "filename_only"

    pipeline_start = time.monotonic()

    try:
        # ----------------------------------------------------------------
        # Step 1: Record Event (skip if duplicate)
        # ----------------------------------------------------------------
        row_id = await event_repo.create_event(job)
        if row_id is None:
            logger.info("[%s] Step 1: Duplicate event, skipping", event_id)
            await audit_repo.log(event_id, "duplicate_skipped", {"reason": "event_id already exists"})
            return
        await audit_repo.log(event_id, "event_recorded", {"status": "processing"})
        logger.info("[%s] Step 1: Event recorded", event_id)

        # ----------------------------------------------------------------
        # Step 2: Classify Item (File vs Folder)
        # ----------------------------------------------------------------
        item_type = getattr(job, "item_type", "File") or "File"
        if item_type.lower() == "folder":
            logger.info("[%s] Step 2: Folder share detected", event_id)
            await _handle_folder_share(
                job, event_id, config, db_pool,
                event_repo, verdict_repo, file_hash_repo, audit_repo,
                ai_provider, notifier_dispatcher, second_look_provider,
                av_semaphore=av_semaphore,
            )
            return

        if item_type.lower() not in ("file",):
            logger.warning("[%s] Step 2: Unrecognised item_type=%s, treating as File", event_id, item_type)

        await audit_repo.log(event_id, "classify_item", {"item_type": item_type, "result": "file"})

        # ----------------------------------------------------------------
        # Steps 3-11: Delegate to _process_single_file
        # ----------------------------------------------------------------
        await _process_single_file(
            event_id, job, config,
            event_repo, verdict_repo, file_hash_repo, audit_repo,
            ai_provider, notifier_dispatcher, second_look_provider,
            av_semaphore=av_semaphore,
        )

    except TransientAIError as exc:
        logger.warning("[%s] Pipeline hit transient AI error: %s", event_id, exc)
        try:
            await _requeue_event(
                event_id, str(exc), config, event_repo, audit_repo, redis, job_data,
            )
        except Exception:
            logger.exception("[%s] Failed to requeue after transient error", event_id)

    except Exception:
        logger.exception("[%s] Pipeline failed with unhandled exception", event_id)
        try:
            current = await _current_status(event_repo, event_id)
            if current not in ("completed", "remediated"):
                await event_repo.update_event_status(
                    event_id, "failed", failure_reason="unhandled_exception",
                )
                await audit_repo.log(
                    event_id, "pipeline_failed", status="error",
                    error="Unhandled exception in pipeline",
                )
            else:
                logger.warning("[%s] Suppressed status downgrade from '%s' to 'failed'", event_id, current)
        except Exception:
            logger.exception("[%s] Failed to update event status after error", event_id)

    finally:
        # ----------------------------------------------------------------
        # Step 12: Cleanup
        # ----------------------------------------------------------------
        elapsed = time.monotonic() - pipeline_start
        Cleanup.cleanup_event_files(event_id, config.tmpfs_path)
        try:
            await event_repo.update_event_status(
                event_id, temp_file_deleted=True,
                status=(await _current_status(event_repo, event_id)),
            )
        except Exception:
            logger.exception("[%s] Failed to mark temp_file_deleted", event_id)

        logger.info("[%s] Pipeline finished in %.1fs", event_id, elapsed)


# ======================================================================
# Internal helpers
# ======================================================================


class _DictJob:
    """Thin wrapper that exposes dict keys as attributes for repository compat."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def __getattr__(self, name: str) -> Any:
        try:
            return self._data[name]
        except KeyError:
            return None


QUEUE_KEY = "sharesentinel:jobs"


async def _requeue_event(
    event_id: str,
    failure_reason: str,
    config: Config,
    event_repo: EventRepository,
    audit_repo: AuditLogRepository,
    redis: Any,
    job_data: dict,
) -> None:
    """Requeue a failed event for delayed retry, or mark it permanently failed."""
    # Check current retry count
    event = await event_repo.get_event(event_id)
    current_retries = (event or {}).get("retry_count", 0) or 0

    if current_retries >= config.max_event_retries:
        logger.warning(
            "[%s] Max retries (%d) exceeded, marking as failed",
            event_id, config.max_event_retries,
        )
        await event_repo.update_event_status(
            event_id, "failed", failure_reason="max_retries_exceeded",
        )
        await audit_repo.log(event_id, "requeue_exhausted", {
            "retry_count": current_retries,
            "last_error": failure_reason,
        }, status="error", error="Max retries exceeded")
        return

    new_count = await event_repo.requeue_event(event_id, failure_reason)
    delay = config.requeue_base_delay_seconds * (2 ** (new_count - 1))

    await audit_repo.log(event_id, "requeue_scheduled", {
        "retry_count": new_count,
        "delay_seconds": delay,
        "reason": failure_reason,
    })
    logger.info(
        "[%s] Requeued (retry %d/%d), re-enqueue in %ds",
        event_id, new_count, config.max_event_retries, delay,
    )

    # Schedule a delayed push back onto the Redis queue
    async def _delayed_push() -> None:
        await asyncio.sleep(delay)
        await redis.rpush(QUEUE_KEY, json.dumps(job_data))
        logger.info("[%s] Delayed re-enqueue fired after %ds", event_id, delay)

    asyncio.create_task(_delayed_push())


def _build_graph_auth(config: Config) -> Any:
    """Construct a GraphAuth from the worker config."""
    from ..graph_api.auth import GraphAuth

    return GraphAuth(
        tenant_id=config.azure_tenant_id,
        client_id=config.azure_client_id,
        client_secret=config.azure_client_secret,
        certificate_path=config.azure_certificate_path or None,
        certificate_password=config.azure_certificate_password or None,
    )


async def _current_status(event_repo: EventRepository, event_id: str) -> str:
    """Return the current DB status for the event, defaulting to 'completed'."""
    row = await event_repo.get_event(event_id)
    if row:
        return row.get("status", "completed")
    return "completed"


# ------------------------------------------------------------------
# Reusable single-file processor (used by process_job and folder enumeration)
# ------------------------------------------------------------------


async def _process_single_file(
    event_id: str,
    job: _DictJob,
    config: Config,
    event_repo: EventRepository,
    verdict_repo: VerdictRepository,
    file_hash_repo: FileHashRepository,
    audit_repo: AuditLogRepository,
    ai_provider: BaseAIProvider,
    notifier_dispatcher: NotificationDispatcher,
    second_look_provider: Optional[BaseAIProvider] = None,
    skip_notification: bool = False,
    prefetched_metadata: Optional[Dict[str, Any]] = None,
    av_semaphore: Optional[asyncio.Semaphore] = None,
) -> Optional[AnalysisResponse]:
    """Run the file-processing pipeline (steps 3-11) for a single file.

    When called for a folder child:
    - *prefetched_metadata* provides drive_id, item_id, name, size, mime_type
      so we skip the Graph metadata fetch.
    - *skip_notification* suppresses per-file notifications (the folder handler
      sends a single summary notification instead).

    Returns the AnalysisResponse, or None on unhandled failure.
    """
    downloaded_file: Optional[Path] = None
    file_hash: Optional[str] = None
    metadata: Dict[str, Any] = {}
    classification: Optional[ClassificationResult] = None
    analysis_response: Optional[AnalysisResponse] = None
    analysis_mode: str = "filename_only"

    try:
        # ----------------------------------------------------------------
        # Step 3: Metadata Pre-screen
        # ----------------------------------------------------------------
        if prefetched_metadata is not None:
            metadata = prefetched_metadata
            await audit_repo.log(event_id, "metadata_prefetched", {
                "name": metadata.get("name"),
                "size": metadata.get("size"),
            })
            logger.info("[%s] Step 3: Using prefetched metadata (size=%s)", event_id, metadata.get("size"))
        else:
            graph_client = GraphClient(auth=_build_graph_auth(config))
            prescreener = MetadataPrescreen()
            try:
                metadata = await retry_with_backoff(
                    prescreener.fetch_metadata,
                    job, graph_client, event_repo, audit_repo,
                    config.user_profile_cache_days, config.upn_domain,
                )
            except GraphFileNotFoundError:
                logger.warning("[%s] Step 3: Item no longer exists (404)", event_id)
                await audit_repo.log(event_id, "metadata_failed", {"reason": "file_not_found"}, status="error")
                await event_repo.update_event_status(event_id, "completed", failure_reason="file_not_found")
                return None
            except (AccessDeniedError, GraphAPIError, httpx.HTTPStatusError) as exc:
                status_code = getattr(exc, "status_code", 0)
                if isinstance(exc, httpx.HTTPStatusError):
                    status_code = exc.response.status_code
                logger.warning("[%s] Step 3: Metadata fetch failed (HTTP %s: %s)", event_id, status_code, exc)
                await audit_repo.log(
                    event_id, "metadata_failed",
                    {"reason": "graph_api_error", "status_code": status_code},
                    status="error",
                )

                # Check if the DB has stored drive_id / item_id from a previous
                # successful metadata fetch.  If so, build minimal metadata and
                # let the classifier decide the route (e.g. format conversion
                # for .loop files) instead of immediately falling back to
                # filename-only.
                stored_event = await event_repo.get_event(event_id)
                stored_drive = (stored_event or {}).get("drive_id", "") or ""
                stored_item = (stored_event or {}).get("item_id_graph", "") or ""
                file_name_hint = getattr(job, "file_name", "") or ""

                if stored_drive and stored_item:
                    logger.info(
                        "[%s] Step 3: Using stored drive_id/item_id for %s",
                        event_id, file_name_hint,
                    )
                    await audit_repo.log(event_id, "metadata_from_db", {
                        "drive_id": stored_drive,
                        "item_id": stored_item,
                    })
                    metadata = {
                        "name": file_name_hint,
                        "size": (stored_event or {}).get("file_size_bytes", 0) or 0,
                        "drive_id": stored_drive,
                        "item_id": stored_item,
                        "parent_path": getattr(job, "relative_path", "") or "",
                    }
                    # Fall through to classification (Steps 4-5)
                else:
                    logger.warning("[%s] Step 3: No stored IDs, falling back to filename-only", event_id)
                    metadata = {
                        "name": file_name_hint,
                        "size": 0,
                        "parent_path": getattr(job, "relative_path", "") or "",
                    }
                    analysis_mode = "filename_only"
                    request = _build_filename_only_request(job, metadata, classification=None)
                    analysis_response = await _run_ai_analysis(ai_provider, request, event_id, audit_repo)
                    await _record_and_notify(
                        event_id, analysis_response, analysis_mode, config,
                        event_repo, verdict_repo, file_hash_repo, audit_repo,
                        notifier_dispatcher, job, metadata, file_hash,
                        skip_notification=skip_notification,
                    )
                    return analysis_response
            logger.info("[%s] Step 3: Metadata fetched (size=%s)", event_id, metadata.get("size"))

        # ----------------------------------------------------------------
        # Steps 4 + 5: Apply Exclusion Rules & Check File Size
        # ----------------------------------------------------------------
        classifier = FileClassifier()
        file_name = metadata.get("name", getattr(job, "file_name", ""))
        file_size = metadata.get("size", 0)
        classification = classifier.classify_with_metadata(
            file_name, "File", file_size, config, metadata=metadata,
        )

        await audit_repo.log(event_id, "classification", {
            "category": classification.category.value,
            "action": classification.action.value,
            "reason": classification.reason,
        })
        logger.info(
            "[%s] Steps 4-5: Classified as %s -> %s",
            event_id, classification.category.value, classification.action.value,
        )

        # Short-circuit: delegated content (OneNote + Whiteboard — Loop uses format conversion)
        if classification.action == Action.PENDING_MANUAL:
            ext = FileClassifier._get_extension(file_name)
            pkg = (metadata.get("package") or {}).get("type", "").lower()
            if pkg in ("onenote", "whiteboard"):
                ct = pkg
            elif ext == ".whiteboard":
                ct = "whiteboard"
            else:
                ct = "onenote"

            await event_repo.set_content_type(event_id, ct)
            await event_repo.update_event_status(event_id, "pending_manual_inspection")
            await audit_repo.log(event_id, "pending_manual_inspection", {
                "content_type": ct,
                "reason": classification.reason,
            })
            logger.info("[%s] Parked as pending_manual_inspection (content_type=%s)", event_id, ct)
            return None

        # Short-circuit: convertible content (Loop/Whiteboard — Graph API format conversion)
        if classification.action == Action.FORMAT_CONVERSION:
            conversion_format = classification.extraction_method  # "html" or "pdf"
            # Determine content_type for the event (only Loop reaches here now)
            ext = FileClassifier._get_extension(file_name)
            pkg = (metadata.get("package") or {}).get("type", "").lower()
            if pkg == "loop":
                ct = "loop"
            elif ext in (".loop", ".fluid"):
                ct = "loop"
            else:
                ct = "convertible"

            await event_repo.set_content_type(event_id, ct)
            await audit_repo.log(event_id, "format_conversion_start", {
                "content_type": ct,
                "target_format": conversion_format,
            })
            logger.info(
                "[%s] Format conversion: content_type=%s format=%s",
                event_id, ct, conversion_format,
            )

            # Download converted file via Graph API
            drive_id = metadata.get("drive_id", "")
            item_id = metadata.get("item_id", "")

            if not drive_id or not item_id:
                logger.warning("[%s] Missing drive_id/item_id for format conversion, falling back to filename_only", event_id)
                await audit_repo.log(event_id, "format_conversion_failed", {
                    "reason": "missing_identifiers",
                }, status="error")
                analysis_mode = "filename_only"
                request = _build_filename_only_request(job, metadata, classification)
                analysis_response = await _run_ai_analysis(ai_provider, request, event_id, audit_repo)
                await _record_and_notify(
                    event_id, analysis_response, analysis_mode, config,
                    event_repo, verdict_repo, file_hash_repo, audit_repo,
                    notifier_dispatcher, job, metadata, file_hash,
                    skip_notification=skip_notification,
                )
                return analysis_response

            graph_client = GraphClient(auth=_build_graph_auth(config))
            downloader = FileDownloader()

            try:
                downloaded_file = await retry_with_backoff(
                    downloader.download_converted,
                    drive_id, item_id, event_id, file_name,
                    conversion_format, graph_client, config,
                )
            except DownloadError as exc:
                if exc.reason in ("file_not_found", "access_denied"):
                    logger.warning("[%s] Format conversion download failed: %s", event_id, exc.reason)
                    await audit_repo.log(event_id, "format_conversion_failed", {"reason": exc.reason}, status="error")
                    await event_repo.update_event_status(event_id, "completed", failure_reason=exc.reason)
                    return None
                # Other download errors: fall back to filename_only
                logger.warning("[%s] Format conversion download failed, falling back to filename_only: %s", event_id, exc)
                await audit_repo.log(event_id, "format_conversion_failed", {"reason": str(exc)}, status="error")
                analysis_mode = "filename_only"
                request = _build_filename_only_request(job, metadata, classification)
                analysis_response = await _run_ai_analysis(ai_provider, request, event_id, audit_repo)
                await _record_and_notify(
                    event_id, analysis_response, analysis_mode, config,
                    event_repo, verdict_repo, file_hash_repo, audit_repo,
                    notifier_dispatcher, job, metadata, file_hash,
                    skip_notification=skip_notification,
                )
                return analysis_response
            except GraphAPIError as exc:
                # 406/501: format conversion not supported for this item
                logger.warning("[%s] Graph API format conversion not supported (HTTP %s): %s", event_id, exc.status_code, exc)
                await audit_repo.log(event_id, "format_conversion_unsupported", {
                    "status_code": exc.status_code,
                    "error": str(exc),
                }, status="error")
                analysis_mode = "filename_only"
                request = _build_filename_only_request(job, metadata, classification)
                analysis_response = await _run_ai_analysis(ai_provider, request, event_id, audit_repo)
                await _record_and_notify(
                    event_id, analysis_response, analysis_mode, config,
                    event_repo, verdict_repo, file_hash_repo, audit_repo,
                    notifier_dispatcher, job, metadata, file_hash,
                    skip_notification=skip_notification,
                )
                return analysis_response

            await audit_repo.log(event_id, "format_conversion_downloaded", {
                "path": str(downloaded_file),
                "size": downloaded_file.stat().st_size,
                "format": conversion_format,
            })
            logger.info("[%s] Format conversion downloaded: %s", event_id, downloaded_file)

            # Override file_name and classification to route into existing extraction pipeline
            # HTML → TextExtractor, PDF → PDFExtractor (both already mapped in extraction/__init__.py)
            file_name = downloaded_file.name
            classification = ClassificationResult(
                category=Category.PROCESSABLE,
                action=Action.FULL_ANALYSIS,
                extraction_method="text_extractor" if conversion_format == "html" else "pdf_extractor",
                reason=f"Format-converted from {ct} to {conversion_format}.",
            )

            # Continue into the standard pipeline: hash, extract, AI, record, notify
            # (Steps 7-11 below will handle this since downloaded_file is now set)

        # Short-circuit: audio/video transcription pipeline
        if classification.action == Action.TRANSCRIPT_ANALYSIS:
            transcript_result = await _handle_transcript_analysis(
                event_id, job, config, metadata, classification, file_name, file_size,
                event_repo, verdict_repo, file_hash_repo, audit_repo,
                ai_provider, notifier_dispatcher, second_look_provider,
                skip_notification=skip_notification,
                av_semaphore=av_semaphore,
            )
            return transcript_result

        # Short-circuit paths that skip download
        if classification.action == Action.FILENAME_ONLY:
            analysis_mode = "filename_only"
            request = _build_filename_only_request(job, metadata, classification)
            analysis_response = await _run_ai_analysis(ai_provider, request, event_id, audit_repo)
            await _record_and_notify(
                event_id, analysis_response, analysis_mode, config,
                event_repo, verdict_repo, file_hash_repo, audit_repo,
                notifier_dispatcher, job, metadata, file_hash,
                skip_notification=skip_notification,
            )
            return analysis_response

        # ----------------------------------------------------------------
        # Step 6: Download File (skip if already downloaded via format conversion)
        # ----------------------------------------------------------------
        if downloaded_file is None:
            drive_id = metadata.get("drive_id", "")
            item_id = metadata.get("item_id", "")

            if not prefetched_metadata:
                graph_client = GraphClient(auth=_build_graph_auth(config))
            else:
                graph_client = GraphClient(auth=_build_graph_auth(config))

            downloader = FileDownloader()
            try:
                downloaded_file = await retry_with_backoff(
                    downloader.download,
                    drive_id, item_id, event_id, file_name, graph_client, config,
                )
            except DownloadError as exc:
                logger.warning("[%s] Step 6: Download failed reason=%s", event_id, exc.reason)
                await audit_repo.log(event_id, "download_failed", {"reason": exc.reason}, status="error", error=str(exc))

                if exc.reason == "file_not_found":
                    await event_repo.update_event_status(event_id, "completed", failure_reason="file_not_found")
                    return None

                if exc.reason == "access_denied":
                    await event_repo.update_event_status(event_id, "completed", failure_reason="access_denied")
                    return None

                # Other download failures: fall back to filename-only analysis
                analysis_mode = "filename_only"
                request = _build_filename_only_request(job, metadata, classification)
                analysis_response = await _run_ai_analysis(ai_provider, request, event_id, audit_repo)
                await _record_and_notify(
                    event_id, analysis_response, analysis_mode, config,
                    event_repo, verdict_repo, file_hash_repo, audit_repo,
                    notifier_dispatcher, job, metadata, file_hash,
                    skip_notification=skip_notification,
                )
                return analysis_response

            await audit_repo.log(event_id, "file_downloaded", {
                "path": str(downloaded_file),
                "size": downloaded_file.stat().st_size,
            })
            logger.info("[%s] Step 6: Downloaded to %s", event_id, downloaded_file)

        # ----------------------------------------------------------------
        # Step 7: Hash + Dedup
        # ----------------------------------------------------------------
        hasher = FileHasher()
        file_hash = hasher.compute_hash(downloaded_file)

        reuse_match = await hasher.check_reuse(file_hash, file_hash_repo, config.hash_reuse_days)
        if reuse_match:
            logger.info("[%s] Step 7: Hash reuse match, previous_event=%s", event_id, reuse_match.get("first_event_id"))
            from ..ai.base_provider import CategoryDetection
            stored_cat_ids = reuse_match.get("category_ids") or []
            if isinstance(stored_cat_ids, str):
                import json as _json
                try:
                    stored_cat_ids = _json.loads(stored_cat_ids)
                except (ValueError, TypeError):
                    stored_cat_ids = []
            reused_categories = [
                CategoryDetection(id=cid, confidence="high", evidence="reused from previous analysis")
                for cid in stored_cat_ids
            ] if stored_cat_ids else []

            analysis_response = AnalysisResponse(
                categories=reused_categories,
                context="mixed",
                summary=f"Hash reuse: identical file previously analysed (event {reuse_match.get('first_event_id', 'unknown')}). Categories reused.",
                recommendation="See original verdict for full details.",
                raw_response="",
                provider="hash_reuse",
                model="n/a",
                input_tokens=0,
                output_tokens=0,
                estimated_cost_usd=0.0,
                processing_time_seconds=0.0,
            )
            analysis_mode = "hash_reuse"
            await audit_repo.log(event_id, "hash_reuse", {
                "file_hash": file_hash,
                "original_event_id": reuse_match.get("first_event_id"),
                "reused_categories": [c.id for c in reused_categories],
            })

            await _record_and_notify(
                event_id, analysis_response, analysis_mode, config,
                event_repo, verdict_repo, file_hash_repo, audit_repo,
                notifier_dispatcher, job, metadata, file_hash,
                skip_notification=skip_notification,
            )
            return analysis_response

        await audit_repo.log(event_id, "hash_computed", {"file_hash": file_hash, "reuse": False})
        logger.info("[%s] Step 7: New content hash=%s...%s", event_id, file_hash[:8], file_hash[-4:])

        # ----------------------------------------------------------------
        # Step 8: Extract Content
        # ----------------------------------------------------------------
        request = await _extract_and_build_request(
            downloaded_file, file_name, file_size, classification,
            job, metadata, event_id, audit_repo,
        )
        analysis_mode = request.mode

        # ----------------------------------------------------------------
        # Step 9: AI Analysis
        # ----------------------------------------------------------------
        analysis_response = await _run_ai_analysis(ai_provider, request, event_id, audit_repo)

        # ----------------------------------------------------------------
        # Step 9b: Second-Look Review (optional)
        # ----------------------------------------------------------------
        if (second_look_provider
                and config.second_look_enabled
                and needs_second_look(analysis_response, analysis_mode)):
            analysis_response = await run_second_look(
                second_look_provider, request, analysis_response,
                event_id, audit_repo,
            )

        # ----------------------------------------------------------------
        # Steps 10-11: Record Verdict & Notify
        # ----------------------------------------------------------------
        await _record_and_notify(
            event_id, analysis_response, analysis_mode, config,
            event_repo, verdict_repo, file_hash_repo, audit_repo,
            notifier_dispatcher, job, metadata, file_hash,
            skip_notification=skip_notification,
        )
        return analysis_response

    except TransientAIError:
        # Let transient AI errors propagate so the caller can requeue
        raise
    except Exception:
        logger.exception("[%s] _process_single_file failed", event_id)
        try:
            await event_repo.update_event_status(event_id, "failed", failure_reason="unhandled_exception")
            await audit_repo.log(event_id, "child_processing_failed", status="error", error="Unhandled exception")
        except Exception:
            logger.exception("[%s] Failed to update child status after error", event_id)
        return None
    finally:
        if downloaded_file:
            if ":child:" in event_id:
                Cleanup.cleanup_child_file(event_id, downloaded_file, config.tmpfs_path)
            else:
                Cleanup.cleanup_event_files(event_id, config.tmpfs_path)


# ------------------------------------------------------------------
# Step 2 (folder) helper
# ------------------------------------------------------------------


async def _handle_folder_share(
    job: _DictJob,
    event_id: str,
    config: Config,
    db_pool: Any,
    event_repo: EventRepository,
    verdict_repo: VerdictRepository,
    file_hash_repo: FileHashRepository,
    audit_repo: AuditLogRepository,
    ai_provider: BaseAIProvider,
    notifier_dispatcher: NotificationDispatcher,
    second_look_provider: Optional[BaseAIProvider] = None,
    av_semaphore: Optional[asyncio.Semaphore] = None,
) -> None:
    """Handle a folder share: enumerate children, process each file, send summary."""
    sharing_type = getattr(job, "sharing_type", "") or getattr(job, "sharing_scope", "") or ""
    sharing_perm = getattr(job, "sharing_permission", "") or ""

    # ---- Step 1: Get folder metadata to obtain drive_id + item_id ----
    graph_client = GraphClient(auth=_build_graph_auth(config))
    try:
        folder_meta = await retry_with_backoff(
            graph_client.get_item_metadata,
            getattr(job, "object_id", ""),
        )
    except (GraphFileNotFoundError, AccessDeniedError, GraphAPIError, httpx.HTTPStatusError) as exc:
        logger.warning("[%s] Folder metadata fetch failed: %s", event_id, exc)
        await audit_repo.log(event_id, "folder_metadata_failed", {"error": str(exc)}, status="error")
        # Fall back to the old behaviour: flag for manual review
        await _handle_folder_share_fallback(
            job, event_id, config, event_repo, verdict_repo,
            audit_repo, notifier_dispatcher, {},
        )
        return

    drive_id = folder_meta.get("parentReference", {}).get("driveId", "")
    folder_item_id = folder_meta.get("id", "")
    folder_name = folder_meta.get("name", getattr(job, "file_name", ""))
    folder_web_url = folder_meta.get("webUrl", "")

    # Fetch sharing permissions for the folder (same as metadata pre-screen does for files)
    sharing_link_url = None
    sharing_links = None
    if drive_id and folder_item_id:
        try:
            from ..graph_api.sharing import get_sharing_permissions, extract_sharing_link, extract_all_sharing_links
            permissions = await get_sharing_permissions(
                auth=graph_client._auth,
                drive_id=drive_id,
                item_id=folder_item_id,
            )
            sharing_link_url = extract_sharing_link(permissions)
            sharing_links = extract_all_sharing_links(permissions)
        except Exception:
            logger.warning("[%s] Failed to fetch folder sharing permissions", event_id, exc_info=True)

        # Enroll sharing links into lifecycle tracking
        if sharing_links:
            try:
                from ..lifecycle.enrollment import enroll_sharing_links
                await enroll_sharing_links(
                    db_pool=db_pool,
                    permissions=permissions,
                    event_id=event_id,
                    user_id=getattr(job, "user_id", ""),
                    drive_id=drive_id,
                    item_id=folder_item_id,
                    file_name=folder_name,
                    event_time=getattr(job, "event_time", None),
                )
            except Exception:
                logger.warning("[%s] Folder lifecycle enrollment failed", event_id, exc_info=True)

    # Persist Graph metadata on the event row
    await event_repo.update_event_metadata(event_id, {
        "confirmed_file_name": folder_name,
        "web_url": folder_web_url,
        "drive_id": drive_id,
        "item_id_graph": folder_item_id,
        "sharing_link_url": sharing_link_url,
        "sharing_links": sharing_links,
    })

    # ---- Step 2: Enumerate all files ----
    try:
        children = await graph_client.list_folder_children(drive_id, folder_item_id)
    except Exception as exc:
        logger.warning("[%s] Folder enumeration failed: %s", event_id, exc)
        await audit_repo.log(event_id, "folder_enumeration_failed", {"error": str(exc)}, status="error")
        await _handle_folder_share_fallback(
            job, event_id, config, event_repo, verdict_repo,
            audit_repo, notifier_dispatcher, {},
        )
        return

    total_children = len(children)
    await event_repo.update_folder_progress(event_id, total=total_children)
    await audit_repo.log(event_id, "folder_enumerated", {
        "total_files": total_children,
        "folder_name": folder_name,
    })
    logger.info("[%s] Folder enumerated: %d files", event_id, total_children)

    # ---- Step 3: If empty folder, send simple notification ----
    if total_children == 0:
        summary = (
            f"Folder '{folder_name}' shared with {sharing_type} {sharing_perm} access. "
            "Folder is empty — no files to analyse."
        )
        response = AnalysisResponse(
            categories=[], context="institutional", summary=summary,
            recommendation="Monitor folder for future file additions.",
            raw_response="", provider="system", model="n/a",
            input_tokens=0, output_tokens=0,
            estimated_cost_usd=0.0, processing_time_seconds=0.0,
        )
        await verdict_repo.create_verdict(event_id, response, "folder_flag", config.notify_on_folder_share)
        if config.notify_on_folder_share:
            payload = AlertPayload(
                event_id=event_id, alert_type="folder_share",
                file_name=folder_name, file_path=folder_web_url,
                file_size_human="N/A", item_type="Folder",
                sharing_user=getattr(job, "user_id", "") or "",
                sharing_type=sharing_type, sharing_permission=sharing_perm,
                event_time=getattr(job, "event_time", "") or "",
                summary=summary, recommendation=response.recommendation,
            )
            await notifier_dispatcher.dispatch(payload)
        await event_repo.update_event_status(event_id, "completed")
        await audit_repo.log(event_id, "pipeline_complete", {"outcome": "empty_folder"})
        return

    # ---- Step 4: Process each child file sequentially ----
    child_results: list[Dict[str, Any]] = []
    flagged_count = 0
    failed_count = 0
    clean_count = 0

    for idx, child_item in enumerate(children):
        child_event_id = f"{event_id}:child:{idx}"
        child_name = child_item.get("name", "unknown")
        logger.info("[%s] Processing child %d/%d: %s", event_id, idx + 1, total_children, child_name)

        try:
            # 4a: Create child event in DB
            row_id = await event_repo.create_child_event(event_id, idx, child_item, job)
            if row_id is None:
                # Duplicate — already processed (idempotent rerun)
                await event_repo.update_folder_progress(event_id, increment_processed=True)
                child_results.append({
                    "event_id": child_event_id,
                    "file_name": child_name,
                    "file_path": (child_item.get("parentReference") or {}).get("path", ""),
                    "status": "skipped_duplicate",
                    "escalation_tier": None,
                    "categories": [],
                    "summary": "Already processed (duplicate)",
                    "failure_reason": None,
                })
                clean_count += 1
                continue

            # 4b: Build prefetched metadata from driveItem
            parent_ref = child_item.get("parentReference", {})
            prefetched = {
                "name": child_name,
                "size": child_item.get("size", 0),
                "drive_id": parent_ref.get("driveId", ""),
                "item_id": child_item.get("id", ""),
                "mime_type": (child_item.get("file") or {}).get("mimeType", ""),
                "parent_path": parent_ref.get("path", ""),
                "web_url": child_item.get("webUrl", ""),
            }

            # 4c: Build synthetic job for the child
            child_job_data = {
                "event_id": child_event_id,
                "operation": getattr(job, "operation", ""),
                "workload": getattr(job, "workload", None),
                "user_id": getattr(job, "user_id", ""),
                "object_id": child_item.get("webUrl", "") or getattr(job, "object_id", ""),
                "site_url": getattr(job, "site_url", None),
                "file_name": child_name,
                "relative_path": parent_ref.get("path", ""),
                "item_type": "File",
                "sharing_type": getattr(job, "sharing_type", None),
                "sharing_scope": getattr(job, "sharing_scope", None),
                "sharing_permission": getattr(job, "sharing_permission", None),
                "event_time": getattr(job, "event_time", None),
            }
            child_job = _DictJob(child_job_data)

            # 4d: Process the file
            resp = await _process_single_file(
                child_event_id, child_job, config,
                event_repo, verdict_repo, file_hash_repo, audit_repo,
                ai_provider, notifier_dispatcher, second_look_provider,
                skip_notification=True,
                prefetched_metadata=prefetched,
                av_semaphore=av_semaphore,
            )

            # 4e: Update counters
            await event_repo.update_folder_progress(event_id, increment_processed=True)

            # 4f: Classify result
            is_flagged = False
            tier = None
            cats: list[str] = []
            child_summary = ""
            if resp is not None:
                tier = resp.escalation_tier
                cats = [c.id for c in resp.categories]
                child_summary = resp.summary or ""
                if tier in ("tier_1", "tier_2"):
                    is_flagged = True
                    flagged_count += 1
                    await event_repo.update_folder_progress(event_id, increment_flagged=True)
                else:
                    clean_count += 1
            else:
                failed_count += 1

            child_results.append({
                "event_id": child_event_id,
                "file_name": child_name,
                "file_path": parent_ref.get("path", ""),
                "escalation_tier": tier,
                "categories": cats,
                "summary": child_summary,
                "status": "completed" if resp is not None else "failed",
                "failure_reason": None if resp is not None else "processing_error",
            })

        except Exception:
            logger.exception("[%s] Error processing child %s", event_id, child_name)
            failed_count += 1
            await event_repo.update_folder_progress(event_id, increment_processed=True)
            child_results.append({
                "event_id": child_event_id,
                "file_name": child_name,
                "file_path": (child_item.get("parentReference") or {}).get("path", ""),
                "escalation_tier": None,
                "categories": [],
                "summary": "",
                "status": "failed",
                "failure_reason": "unhandled_exception",
            })
            try:
                await event_repo.update_event_status(child_event_id, "failed", failure_reason="unhandled_exception")
            except Exception:
                pass

    # ---- Step 5: Build and send summary notification ----
    summary_text = (
        f"Folder '{folder_name}' shared with {sharing_type} {sharing_perm} access. "
        f"Enumerated {total_children} files: "
        f"{flagged_count} flagged, {clean_count} clean, {failed_count} failed."
    )

    # Aggregate categories from all flagged children so the parent verdict
    # has the correct escalation_tier (tier_1 / tier_2 / none).
    aggregated_categories: list[CategoryDetection] = []
    for cr in child_results:
        if cr.get("escalation_tier") in ("tier_1", "tier_2"):
            for cat_id in cr.get("categories", []):
                aggregated_categories.append(
                    CategoryDetection(
                        id=cat_id,
                        confidence="high",
                        evidence=f"From child file: {cr.get('file_name', 'unknown')}",
                    )
                )

    # Create a synthetic parent verdict
    response = AnalysisResponse(
        categories=aggregated_categories, context="institutional", summary=summary_text,
        recommendation="Review flagged files in the folder." if flagged_count else "No sensitive files detected.",
        raw_response="", provider="system", model="folder_enumeration",
        input_tokens=0, output_tokens=0,
        estimated_cost_usd=0.0, processing_time_seconds=0.0,
    )
    notification_required = flagged_count > 0 or config.notify_on_folder_share
    await verdict_repo.create_verdict(event_id, response, "folder_enumeration", notification_required)
    await audit_repo.log(event_id, "verdict_recorded", {"type": "folder_enumerated", "flagged": flagged_count})

    if notification_required:
        payload = AlertPayload(
            event_id=event_id,
            alert_type="folder_share_enumerated",
            file_name=folder_name,
            file_path=folder_web_url,
            file_size_human="N/A",
            item_type="Folder",
            sharing_user=getattr(job, "user_id", "") or "",
            sharing_type=sharing_type,
            sharing_permission=sharing_perm,
            event_time=getattr(job, "event_time", "") or "",
            summary=summary_text,
            recommendation=response.recommendation,
            child_summaries=child_results,
            folder_total_files=total_children,
            folder_flagged_files=flagged_count,
            folder_clean_files=clean_count,
            folder_failed_files=failed_count,
        )
        results = await notifier_dispatcher.dispatch(payload)
        await audit_repo.log(event_id, "notification_sent", {"channels": results})
        logger.info("[%s] Folder summary notification sent: %s", event_id, results)

    await event_repo.update_event_status(event_id, "completed")
    await audit_repo.log(event_id, "pipeline_complete", {
        "outcome": "folder_enumerated",
        "total": total_children,
        "flagged": flagged_count,
        "clean": clean_count,
        "failed": failed_count,
    })
    logger.info(
        "[%s] Folder processing complete: %d total, %d flagged, %d clean, %d failed",
        event_id, total_children, flagged_count, clean_count, failed_count,
    )


async def _handle_folder_share_fallback(
    job: _DictJob,
    event_id: str,
    config: Config,
    event_repo: EventRepository,
    verdict_repo: VerdictRepository,
    audit_repo: AuditLogRepository,
    notifier_dispatcher: NotificationDispatcher,
    metadata: Dict[str, Any],
) -> None:
    """Original folder-share handler used as fallback when enumeration fails."""
    sharing_type = getattr(job, "sharing_type", "") or getattr(job, "sharing_scope", "") or ""
    sharing_perm = getattr(job, "sharing_permission", "") or ""
    summary = (
        f"Folder shared with {sharing_type} {sharing_perm} access. "
        "Automatic flag for analyst review (enumeration unavailable)."
    )

    response = AnalysisResponse(
        categories=[], context="institutional", summary=summary,
        recommendation="Review folder contents and sharing permissions.",
        raw_response="", provider="system", model="n/a",
        input_tokens=0, output_tokens=0,
        estimated_cost_usd=0.0, processing_time_seconds=0.0,
    )
    await verdict_repo.create_verdict(event_id, response, "folder_flag", config.notify_on_folder_share)
    await audit_repo.log(event_id, "verdict_recorded", {"type": "folder_share_flagged"})

    if config.notify_on_folder_share:
        payload = AlertPayload(
            event_id=event_id, alert_type="folder_share",
            file_name=getattr(job, "file_name", "") or "",
            file_path=metadata.get("parent_path", ""),
            file_size_human="N/A", item_type="Folder",
            sharing_user=getattr(job, "user_id", "") or "",
            sharing_type=sharing_type, sharing_permission=sharing_perm,
            event_time=getattr(job, "event_time", "") or "",
            sharing_link_url=metadata.get("sharing_link_url"),
            sharing_links=metadata.get("sharing_links"),
            summary=summary, recommendation=response.recommendation,
        )
        await notifier_dispatcher.dispatch(payload)
        await audit_repo.log(event_id, "notification_sent", {"type": "folder_share_fallback"})

    await event_repo.update_event_status(event_id, "completed")
    await audit_repo.log(event_id, "pipeline_complete", {"outcome": "folder_flagged_fallback"})
    logger.info("[%s] Folder share flagged (fallback) and notified", event_id)


# ------------------------------------------------------------------
# Audio/video transcription handler
# ------------------------------------------------------------------


async def _handle_transcript_analysis(
    event_id: str,
    job: _DictJob,
    config: Config,
    metadata: Dict[str, Any],
    classification: ClassificationResult,
    file_name: str,
    file_size: int,
    event_repo: EventRepository,
    verdict_repo: VerdictRepository,
    file_hash_repo: FileHashRepository,
    audit_repo: AuditLogRepository,
    ai_provider: BaseAIProvider,
    notifier_dispatcher: NotificationDispatcher,
    second_look_provider: Optional[BaseAIProvider] = None,
    skip_notification: bool = False,
    av_semaphore: Optional[asyncio.Semaphore] = None,
) -> Optional[AnalysisResponse]:
    """Handle audio/video files via transcript retrieval or Whisper transcription.

    Processing order:
    1. If Teams recording → try Graph API transcript (no download needed)
    2. Try Stream auto-generated captions via SharePoint v2.1 API (no download needed)
    3. If no transcript yet & Whisper enabled → download + transcribe via Whisper
    4. If transcript obtained (any source) → download + extract keyframes
    5. Final fallback → filename-only analysis
    """
    if not config.transcription_enabled:
        logger.info("[%s] Transcription disabled, falling back to filename-only", event_id)
        return await _transcript_filename_fallback(
            event_id, job, config, metadata, classification,
            event_repo, verdict_repo, file_hash_repo, audit_repo,
            ai_provider, notifier_dispatcher, skip_notification,
        )

    transcript_text: Optional[str] = None
    transcript_source: Optional[str] = None
    media_duration: Optional[int] = None

    # --- Path A: Graph API transcript for Teams recordings ---
    if is_teams_recording(file_name):
        logger.info("[%s] Teams recording detected, attempting Graph API transcript", event_id)
        await audit_repo.log(event_id, "transcript_attempt", {"method": "graph_api", "filename": file_name})

        drive_id = metadata.get("drive_id", "")
        item_id = metadata.get("item_id", "")
        graph_auth = _build_graph_auth(config)

        # Get the actual meeting organizer from the beta driveItem source facet
        organizer_id = None
        if drive_id and item_id:
            organizer_id = await get_meeting_organizer_id(
                auth=graph_auth, drive_id=drive_id, item_id=item_id,
            )

        if organizer_id:
            recording_time = parse_recording_timestamp(file_name)
            try:
                transcript_text = await get_meeting_transcript(
                    auth=graph_auth,
                    organizer_id=organizer_id,
                    recording_time=recording_time,
                    timeout=config.graph_transcript_timeout_seconds,
                )
            except Exception:
                logger.exception("[%s] Graph API transcript retrieval failed", event_id)

            if transcript_text:
                transcript_source = "graph_api"
                await audit_repo.log(event_id, "transcript_retrieved", {
                    "source": "graph_api",
                    "length": len(transcript_text),
                })
                logger.info("[%s] Graph API transcript retrieved (%d chars)", event_id, len(transcript_text))
            else:
                logger.info("[%s] No Graph API transcript available, will try Whisper", event_id)
                await audit_repo.log(event_id, "transcript_unavailable", {"method": "graph_api"})
        else:
            logger.info("[%s] Could not resolve meeting organizer ID", event_id)

    # --- Path A.5: Stream auto-generated captions (SharePoint v2.1 API) ---
    if transcript_text is None:
        drive_id = metadata.get("drive_id", "")
        item_id = metadata.get("item_id", "")
        site_url = getattr(job, "site_url", "") or ""
        if drive_id and item_id and site_url:
            logger.info("[%s] Attempting Stream caption retrieval", event_id)
            await audit_repo.log(event_id, "transcript_attempt", {"method": "stream_captions"})
            try:
                graph_auth = _build_graph_auth(config)
                stream_text = await get_stream_captions(
                    auth=graph_auth,
                    drive_id=drive_id,
                    item_id=item_id,
                    site_url=site_url,
                    timeout=config.graph_transcript_timeout_seconds,
                )
                if stream_text:
                    transcript_text = stream_text
                    transcript_source = "stream_captions"
                    await audit_repo.log(event_id, "transcript_retrieved", {
                        "source": "stream_captions",
                        "length": len(transcript_text),
                    })
                    logger.info(
                        "[%s] Stream captions retrieved (%d chars)", event_id, len(transcript_text),
                    )
                else:
                    logger.info("[%s] No Stream captions available, will try Whisper", event_id)
                    await audit_repo.log(event_id, "transcript_unavailable", {"method": "stream_captions"})
            except Exception:
                logger.exception("[%s] Stream caption retrieval failed", event_id)
                await audit_repo.log(event_id, "transcript_unavailable", {"method": "stream_captions"})

    # --- Path B: Whisper transcription (fallback) ---
    whisper_frames: list = []
    if transcript_text is None and config.whisper_enabled:
        logger.info("[%s] Attempting Whisper transcription", event_id)
        await audit_repo.log(event_id, "transcript_attempt", {"method": "whisper"})

        # Need to download the file first
        drive_id = metadata.get("drive_id", "")
        item_id = metadata.get("item_id", "")
        downloaded_file: Optional[Path] = None

        if drive_id and item_id:
            # Acquire A/V semaphore to limit concurrent heavy transcription jobs
            async def _download_and_transcribe() -> None:
                nonlocal transcript_text, transcript_source, media_duration, downloaded_file, whisper_frames
                graph_client = GraphClient(auth=_build_graph_auth(config))
                downloader = FileDownloader()
                try:
                    downloaded_file = await retry_with_backoff(
                        downloader.download,
                        drive_id, item_id, event_id, file_name, graph_client, config,
                    )
                except DownloadError as exc:
                    logger.warning("[%s] A/V download failed: %s", event_id, exc.reason)
                    await audit_repo.log(event_id, "av_download_failed", {"reason": exc.reason})
                    return

                if downloaded_file:
                    try:
                        whisper = WhisperClient(service_url=config.whisper_service_url)
                        result = await whisper.transcribe(downloaded_file)
                        if result:
                            transcript_text = result.get("text", "")
                            media_duration = int(result.get("duration", 0)) or None
                            transcript_source = "whisper"
                            whisper_frames = result.get("frames", [])
                            await audit_repo.log(event_id, "transcript_retrieved", {
                                "source": "whisper",
                                "length": len(transcript_text),
                                "duration": media_duration,
                                "frame_count": len(whisper_frames),
                            })
                            logger.info(
                                "[%s] Whisper transcript retrieved (%d chars, %ss, %d frames)",
                                event_id, len(transcript_text), media_duration, len(whisper_frames),
                            )
                    except Exception:
                        logger.exception("[%s] Whisper transcription failed", event_id)
                        await audit_repo.log(event_id, "transcript_unavailable", {"method": "whisper"})
                    finally:
                        # Clean up downloaded file
                        Cleanup.cleanup_event_files(event_id, config.tmpfs_path)

            if av_semaphore:
                async with av_semaphore:
                    await _download_and_transcribe()
            else:
                await _download_and_transcribe()
        else:
            logger.warning("[%s] No drive_id/item_id for Whisper download", event_id)

    # --- Keyframe extraction (for any transcript source that didn't already provide frames) ---
    extracted_frames: list = []
    if (
        transcript_text
        and transcript_source != "whisper"  # Whisper already extracts frames
        and config.video_frame_extraction_enabled
    ):
        drive_id = metadata.get("drive_id", "")
        item_id = metadata.get("item_id", "")
        if drive_id and item_id:
            logger.info("[%s] Downloading video for keyframe extraction", event_id)

            async def _download_and_extract_frames() -> None:
                nonlocal extracted_frames, media_duration
                graph_client = GraphClient(auth=_build_graph_auth(config))
                downloader = FileDownloader()
                try:
                    downloaded = await retry_with_backoff(
                        downloader.download,
                        drive_id, item_id, event_id, file_name, graph_client, config,
                    )
                except DownloadError as exc:
                    logger.warning("[%s] Frame extraction download failed: %s", event_id, exc.reason)
                    await audit_repo.log(event_id, "frame_download_failed", {"reason": exc.reason})
                    return

                if downloaded:
                    try:
                        whisper = WhisperClient(service_url=config.whisper_service_url)
                        result = await whisper.extract_frames(downloaded)
                        if result:
                            extracted_frames = result.get("frames", [])
                            media_duration = int(result.get("duration", 0)) or media_duration
                            await audit_repo.log(event_id, "frames_extracted", {
                                "frame_count": len(extracted_frames),
                                "duration": media_duration,
                            })
                            logger.info(
                                "[%s] Keyframes extracted (%d frames)",
                                event_id, len(extracted_frames),
                            )
                    except Exception:
                        logger.exception("[%s] Frame extraction failed", event_id)
                    finally:
                        Cleanup.cleanup_event_files(event_id, config.tmpfs_path)

            if av_semaphore:
                async with av_semaphore:
                    await _download_and_extract_frames()
            else:
                await _download_and_extract_frames()

    # --- Build analysis request from transcript or fall back ---
    if transcript_text and transcript_source:
        extraction = TranscriptExtractor.from_text(
            transcript_text,
            source=transcript_source,
            duration_seconds=media_duration,
        )
        if extraction.success and extraction.text_content:
            # Update event with transcript metadata
            try:
                await event_repo.update_transcript_metadata(
                    event_id, transcript_source=transcript_source,
                    media_duration_seconds=media_duration,
                )
            except Exception:
                logger.debug("[%s] Could not update transcript metadata", event_id, exc_info=True)

            # Determine if we can use transcript_multimodal mode
            # Frames may come from Path A.5 (extracted_frames) or Path B (whisper_frames)
            all_frames = extracted_frames or whisper_frames
            use_frames = bool(all_frames) and config.video_frame_extraction_enabled

            file_meta = dict(extraction.metadata)
            if use_frames:
                frame_timestamps = [f["timestamp_seconds"] for f in all_frames]
                file_meta["frame_count"] = len(all_frames)
                file_meta["frame_timestamps"] = frame_timestamps

            request = AnalysisRequest(
                mode="transcript_multimodal" if use_frames else "transcript",
                file_name=file_name,
                file_path=metadata.get("parent_path", ""),
                file_size=file_size,
                sharing_user=getattr(job, "user_id", "") or "",
                sharing_type=getattr(job, "sharing_type", "") or getattr(job, "sharing_scope", "") or "",
                sharing_permission=getattr(job, "sharing_permission", "") or "",
                event_time=getattr(job, "event_time", "") or "",
                filename_flagged=metadata.get("filename_flagged", False),
                filename_flag_keywords=metadata.get("filename_matched_keywords", []),
                text_content=extraction.text_content,
                was_sampled=extraction.was_sampled,
                sampling_description=extraction.sampling_description,
                file_metadata=file_meta,
                images=[f["image_bytes"] for f in all_frames] if use_frames else None,
                image_mime_types=[f["mime_type"] for f in all_frames] if use_frames else None,
            )
            analysis_mode = "transcript_multimodal" if use_frames else "transcript"

            # AI analysis
            analysis_response = await _run_ai_analysis(ai_provider, request, event_id, audit_repo)

            # Second-look review
            if (second_look_provider
                    and config.second_look_enabled
                    and needs_second_look(analysis_response, analysis_mode)):
                analysis_response = await run_second_look(
                    second_look_provider, request, analysis_response,
                    event_id, audit_repo,
                )

            # Record and notify
            await _record_and_notify(
                event_id, analysis_response, analysis_mode, config,
                event_repo, verdict_repo, file_hash_repo, audit_repo,
                notifier_dispatcher, job, metadata, None,
                skip_notification=skip_notification,
            )
            return analysis_response

    # --- Fallback: filename-only analysis ---
    logger.info("[%s] No transcript available, falling back to filename-only", event_id)
    return await _transcript_filename_fallback(
        event_id, job, config, metadata, classification,
        event_repo, verdict_repo, file_hash_repo, audit_repo,
        ai_provider, notifier_dispatcher, skip_notification,
    )


async def _transcript_filename_fallback(
    event_id: str,
    job: _DictJob,
    config: Config,
    metadata: Dict[str, Any],
    classification: Optional[ClassificationResult],
    event_repo: EventRepository,
    verdict_repo: VerdictRepository,
    file_hash_repo: FileHashRepository,
    audit_repo: AuditLogRepository,
    ai_provider: BaseAIProvider,
    notifier_dispatcher: NotificationDispatcher,
    skip_notification: bool = False,
) -> Optional[AnalysisResponse]:
    """Filename-only analysis fallback for A/V files."""
    await audit_repo.log(event_id, "transcript_fallback", {"method": "filename_only"})
    request = _build_filename_only_request(job, metadata, classification)
    analysis_response = await _run_ai_analysis(ai_provider, request, event_id, audit_repo)
    await _record_and_notify(
        event_id, analysis_response, "filename_only", config,
        event_repo, verdict_repo, file_hash_repo, audit_repo,
        notifier_dispatcher, job, metadata, None,
        skip_notification=skip_notification,
    )
    return analysis_response


# ------------------------------------------------------------------
# Filename-only request builder
# ------------------------------------------------------------------


def _build_filename_only_request(
    job: _DictJob,
    metadata: Dict[str, Any],
    classification: Optional[ClassificationResult],
) -> AnalysisRequest:
    """Build an AnalysisRequest for filename/path-only analysis."""
    reason = classification.reason if classification else "Unknown"
    file_name = metadata.get("name", getattr(job, "file_name", "") or "")
    return AnalysisRequest(
        mode="filename_only",
        file_name=file_name,
        file_path=metadata.get("parent_path", ""),
        file_size=metadata.get("size", 0),
        sharing_user=getattr(job, "user_id", "") or "",
        sharing_type=getattr(job, "sharing_type", "") or getattr(job, "sharing_scope", "") or "",
        sharing_permission=getattr(job, "sharing_permission", "") or "",
        event_time=getattr(job, "event_time", "") or "",
        filename_flagged=metadata.get("filename_flagged", False),
        filename_flag_keywords=metadata.get("filename_matched_keywords", []),
        file_metadata={"classification_reason": reason},
    )


# ------------------------------------------------------------------
# Step 8: Extraction logic
# ------------------------------------------------------------------


async def _extract_and_build_request(
    file_path: Path,
    file_name: str,
    file_size: int,
    classification: ClassificationResult,
    job: _DictJob,
    metadata: Dict[str, Any],
    event_id: str,
    audit_repo: AuditLogRepository,
) -> AnalysisRequest:
    """Run the appropriate extraction strategy and return an AnalysisRequest.

    Decision tree:
    - IMAGE -> preprocess_image -> multimodal
    - PROCESSABLE -> text extractor -> fallback OCR (PDF) -> fallback multimodal (PDF) -> filename_only
    - ARCHIVE -> archive extractor -> text-based analysis
    - Anything else -> filename_only
    """
    common_kwargs = dict(
        file_name=file_name,
        file_path=metadata.get("parent_path", ""),
        file_size=file_size,
        sharing_user=getattr(job, "user_id", "") or "",
        sharing_type=getattr(job, "sharing_type", "") or getattr(job, "sharing_scope", "") or "",
        sharing_permission=getattr(job, "sharing_permission", "") or "",
        event_time=getattr(job, "event_time", "") or "",
        filename_flagged=metadata.get("filename_flagged", False),
        filename_flag_keywords=metadata.get("filename_matched_keywords", []),
    )

    # ---- IMAGE ----
    if classification.action == Action.MULTIMODAL:
        preprocessed = preprocess_image(file_path)
        await audit_repo.log(event_id, "extraction", {
            "method": "image_preprocess",
            "original_size": preprocessed.original_size_bytes,
            "processed_size": preprocessed.processed_size_bytes,
        })
        return AnalysisRequest(
            mode="multimodal",
            images=[preprocessed.image_bytes],
            image_mime_types=[preprocessed.mime_type],
            file_metadata={"source": preprocessed.source},
            **common_kwargs,
        )

    # ---- ARCHIVE ----
    if classification.action == Action.ARCHIVE_MANIFEST:
        ext = Path(file_name).suffix.lower()
        extractor = get_extractor(ext)
        if extractor:
            result = extractor.extract(file_path, file_size)
            if result.success and result.text_content:
                await audit_repo.log(event_id, "extraction", {
                    "method": "archive_manifest",
                    "content_length": result.content_length,
                })
                return AnalysisRequest(
                    mode="text",
                    text_content=result.text_content,
                    was_sampled=result.was_sampled,
                    sampling_description=result.sampling_description,
                    file_metadata=result.metadata,
                    **common_kwargs,
                )
        # Archive extraction failed: fall back to filename_only
        await audit_repo.log(event_id, "extraction", {"method": "archive_failed_fallback_filename"})
        return AnalysisRequest(mode="filename_only", **common_kwargs)

    # ---- TEXT-EXTRACTABLE (FULL_ANALYSIS) ----
    if classification.action == Action.FULL_ANALYSIS:
        ext = Path(file_name).suffix.lower()
        extractor = get_extractor(ext)

        extraction_result: Optional[ExtractionResult] = None
        if extractor:
            extraction_result = extractor.extract(file_path, file_size)

        # Success path: extracted enough text
        if extraction_result and extraction_result.success and extraction_result.text_content:
            await audit_repo.log(event_id, "extraction", {
                "method": extraction_result.extraction_method,
                "content_length": extraction_result.content_length,
                "was_sampled": extraction_result.was_sampled,
            })
            return AnalysisRequest(
                mode="text",
                text_content=extraction_result.text_content,
                was_sampled=extraction_result.was_sampled,
                sampling_description=extraction_result.sampling_description,
                file_metadata=extraction_result.metadata,
                **common_kwargs,
            )

        # Text extraction failed: try OCR if PDF
        if ext == ".pdf":
            logger.info("[%s] Text extraction failed for PDF, trying OCR", event_id)
            ocr = OcrExtractor()
            ocr_result = ocr.extract(file_path, file_size)

            if ocr_result.success and ocr_result.text_content:
                await audit_repo.log(event_id, "extraction", {
                    "method": "ocr",
                    "content_length": ocr_result.content_length,
                })
                return AnalysisRequest(
                    mode="text",
                    text_content=ocr_result.text_content,
                    was_sampled=ocr_result.was_sampled,
                    sampling_description=ocr_result.sampling_description,
                    file_metadata=ocr_result.metadata,
                    **common_kwargs,
                )

            # OCR also failed: render PDF pages as images for multimodal
            logger.info("[%s] OCR failed for PDF, rendering pages as images", event_id)
            try:
                images = render_pdf_pages_as_images(file_path)
                if images:
                    await audit_repo.log(event_id, "extraction", {
                        "method": "pdf_multimodal_fallback",
                        "page_count": len(images),
                    })
                    return AnalysisRequest(
                        mode="multimodal",
                        images=[img.image_bytes for img in images],
                        image_mime_types=[img.mime_type for img in images],
                        file_metadata={"pages_rendered": len(images)},
                        **common_kwargs,
                    )
            except Exception:
                logger.exception("[%s] PDF page rendering failed", event_id)

        # Non-PDF or all fallbacks exhausted: filename-only
        await audit_repo.log(event_id, "extraction", {"method": "all_failed_fallback_filename"})
        return AnalysisRequest(mode="filename_only", **common_kwargs)

    # Fallback for any unexpected action
    await audit_repo.log(event_id, "extraction", {"method": "fallback_filename_only"})
    return AnalysisRequest(mode="filename_only", **common_kwargs)


# ------------------------------------------------------------------
# Step 9: AI analysis with retry
# ------------------------------------------------------------------


async def _run_ai_analysis(
    ai_provider: BaseAIProvider,
    request: AnalysisRequest,
    event_id: str,
    audit_repo: AuditLogRepository,
) -> AnalysisResponse:
    """Call the AI provider with retries and audit logging.

    TransientAIError is included in the retryable set so that rate-limit,
    timeout, and server-error responses are retried automatically.  If
    retries are exhausted the exception propagates to the caller.
    """
    await audit_repo.log(event_id, "ai_analysis_start", {"mode": request.mode})

    response = await retry_with_backoff(
        ai_provider.analyze, request, call_timeout=120,
        retryable_exceptions=(TransientAIError, ConnectionError, TimeoutError),
    )

    # Permanent failure (bad request, auth error, parse failure) — the
    # provider returned success=False instead of raising.
    if not response.success:
        logger.warning(
            "[%s] AI analysis returned success=False: %s", event_id, response.error,
        )
        await audit_repo.log(event_id, "ai_analysis_permanent_failure", {
            "provider": response.provider,
            "model": response.model,
            "error": response.error,
        }, status="error", error=response.error)

    cat_ids = [c.id for c in response.categories]
    await audit_repo.log(event_id, "ai_analysis_complete", {
        "provider": response.provider,
        "model": response.model,
        "escalation_tier": response.escalation_tier,
        "categories": cat_ids,
        "context": response.context,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "cost_usd": response.estimated_cost_usd,
        "duration_s": response.processing_time_seconds,
    })
    logger.info(
        "[%s] Step 9: AI analysis complete tier=%s categories=%s cost=$%.4f",
        event_id, response.escalation_tier, cat_ids,
        response.estimated_cost_usd,
    )
    return response


# ------------------------------------------------------------------
# Steps 10-11: Record verdict + Notify
# ------------------------------------------------------------------


async def _record_and_notify(
    event_id: str,
    response: AnalysisResponse,
    analysis_mode: str,
    config: Config,
    event_repo: EventRepository,
    verdict_repo: VerdictRepository,
    file_hash_repo: FileHashRepository,
    audit_repo: AuditLogRepository,
    notifier_dispatcher: NotificationDispatcher,
    job: _DictJob,
    metadata: Dict[str, Any],
    file_hash: Optional[str],
    skip_notification: bool = False,
) -> None:
    """Record the AI verdict, store file hash, notify if risky, and complete."""

    # Deterministic escalation from category taxonomy
    original_tier = response.escalation_tier
    cat_ids = [c.id for c in response.categories]

    # Apply post-processing overrides (coursework context, FERPA name
    # linkage, student-path heuristic)
    file_name = metadata.get("name", getattr(job, "file_name", "") or "")
    override = apply_escalation_overrides(
        base_tier=original_tier,
        category_ids=response.category_ids,
        context=response.context,
        pii_types_found=response.pii_types_found,
        file_name=file_name,
        file_path=metadata.get("parent_path", ""),
        site_url=getattr(job, "site_url", "") or "",
        object_id=getattr(job, "object_id", "") or "",
    )
    escalation_tier = override.adjusted_tier
    notification_required = escalation_tier in ("tier_1", "tier_2")

    if override.applied:
        logger.info(
            "[%s] Escalation overridden: %s (base=%s -> %s)",
            event_id, override.reason, original_tier, escalation_tier,
        )
        await audit_repo.log(event_id, "escalation_override", {
            "reason": override.reason,
            "original_tier": original_tier,
            "adjusted_tier": escalation_tier,
            "original_categories": cat_ids,
            "replacement_category": override.replacement_category,
            "context": response.context,
            "pii_types_found": response.pii_types_found,
        })

        # Rewrite response categories so the stored verdict reflects the
        # final determination, not the raw AI guess.  The original AI
        # assessment is preserved in the audit log above.
        escalating_ids = TIER_1 | TIER_2
        replacement = override.replacement_category or "none"
        new_categories = []
        for cat in response.categories:
            if cat.id in escalating_ids:
                new_categories.append(CategoryDetection(
                    id=replacement,
                    confidence=cat.confidence,
                    evidence=f"[Override: {override.reason}] {cat.evidence}",
                ))
            else:
                new_categories.append(cat)
        response.categories = new_categories
        cat_ids = [c.id for c in response.categories]

    if notification_required:
        logger.info(
            "[%s] Escalation triggered: tier=%s categories=%s",
            event_id, escalation_tier, cat_ids,
        )

    # Step 10: Record verdict
    await verdict_repo.create_verdict(
        event_id=event_id,
        response=response,
        analysis_mode=analysis_mode,
        notification_required=notification_required,
    )
    await audit_repo.log(event_id, "verdict_recorded", {
        "escalation_tier": escalation_tier,
        "categories": cat_ids,
        "notification_required": notification_required,
    })

    # Store file hash if we have one and this is not a reuse
    if file_hash and analysis_mode != "hash_reuse":
        await FileHasher.store_hash(
            file_hash, event_id, cat_ids, file_hash_repo,
        )

    # Step 11: Notify if risky
    if notification_required and not skip_notification:
        payload = AlertPayload(
            event_id=event_id,
            alert_type="high_sensitivity_file",
            file_name=file_name,
            file_path=metadata.get("parent_path", ""),
            file_size_human=format_file_size(metadata.get("size", 0)),
            item_type="File",
            sharing_user=getattr(job, "user_id", "") or "",
            sharing_type=getattr(job, "sharing_type", "") or getattr(job, "sharing_scope", "") or "",
            sharing_permission=getattr(job, "sharing_permission", "") or "",
            event_time=getattr(job, "event_time", "") or "",
            sharing_link_url=metadata.get("sharing_link_url"),
            sharing_links=metadata.get("sharing_links"),
            categories=response.categories,
            escalation_tier=escalation_tier,
            context=response.context,
            summary=response.summary,
            recommendation=response.recommendation,
            analysis_mode=analysis_mode,
            affected_count=response.affected_count,
            pii_types_found=response.pii_types_found,
            filename_flagged=metadata.get("filename_flagged", False),
            filename_flag_keywords=metadata.get("filename_matched_keywords"),
        )
        results = await notifier_dispatcher.dispatch(payload)
        await audit_repo.log(event_id, "notification_sent", {"channels": results})
        logger.info("[%s] Step 11: Notification dispatched channels=%s", event_id, results)

    # Complete
    await event_repo.update_event_status(event_id, "completed")
    await audit_repo.log(event_id, "pipeline_complete", {
        "escalation_tier": escalation_tier,
        "mode": analysis_mode,
    })
    logger.info("[%s] Steps 10-11 complete, event marked completed", event_id)


# ------------------------------------------------------------------
# Failure notification helper
# ------------------------------------------------------------------


async def _notify_failure(
    event_id: str,
    reason: str,
    job: _DictJob,
    metadata: Dict[str, Any],
    config: Config,
    notifier_dispatcher: NotificationDispatcher,
    audit_repo: AuditLogRepository,
) -> None:
    """Send a processing-failure alert if configured."""
    if not config.notify_on_failure:
        return

    payload = AlertPayload(
        event_id=event_id,
        alert_type="processing_failure",
        file_name=metadata.get("name", getattr(job, "file_name", "") or ""),
        file_path=metadata.get("parent_path", ""),
        file_size_human=format_file_size(metadata.get("size", 0)),
        item_type="File",
        sharing_user=getattr(job, "user_id", "") or "",
        sharing_type=getattr(job, "sharing_type", "") or getattr(job, "sharing_scope", "") or "",
        sharing_permission=getattr(job, "sharing_permission", "") or "",
        event_time=getattr(job, "event_time", "") or "",
        sharing_link_url=metadata.get("sharing_link_url"),
        sharing_links=metadata.get("sharing_links"),
        failure_reason=reason,
    )
    results = await notifier_dispatcher.dispatch(payload)
    await audit_repo.log(event_id, "failure_notification_sent", {"reason": reason, "channels": results})
