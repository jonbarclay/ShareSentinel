"""Master pipeline orchestrator: connects all modules into the 12-step process_job flow."""

from __future__ import annotations

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
from ..ai.prompt_manager import PromptManager, format_file_size
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
    MultimodalContent,
    preprocess_image,
    render_pdf_pages_as_images,
)
from ..extraction.ocr_extractor import OcrExtractor
from ..graph_api.client import GraphClient
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
        )

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
                logger.warning("[%s] Step 3: Metadata fetch failed (HTTP %s: %s), falling back to filename-only", event_id, status_code, exc)
                await audit_repo.log(
                    event_id, "metadata_failed",
                    {"reason": "graph_api_error", "status_code": status_code},
                    status="error",
                )
                metadata = {
                    "name": getattr(job, "file_name", "") or "",
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

        # Short-circuit: delegated content (Loop/OneNote/Whiteboard)
        if classification.action == Action.PENDING_MANUAL:
            content_type_map = {
                ".loop": "loop", ".fluid": "loop",
                ".one": "onenote", ".whiteboard": "whiteboard",
            }
            ext = FileClassifier._get_extension(file_name)
            ct = content_type_map.get(ext, "delegated")
            # Check metadata package facet as fallback
            pkg = (metadata.get("package") or {}).get("type", "").lower()
            if pkg == "onenote":
                ct = "onenote"
            elif pkg == "whiteboard":
                ct = "whiteboard"
            elif pkg == "loop":
                ct = "loop"

            await event_repo.set_content_type(event_id, ct)
            await event_repo.update_event_status(event_id, "pending_manual_inspection")
            await audit_repo.log(event_id, "pending_manual_inspection", {
                "content_type": ct,
                "reason": classification.reason,
            })
            logger.info("[%s] Parked as pending_manual_inspection (content_type=%s)", event_id, ct)
            return None

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
        # Step 6: Download File
        # ----------------------------------------------------------------
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
    """Call the AI provider with retries and audit logging."""
    await audit_repo.log(event_id, "ai_analysis_start", {"mode": request.mode})

    response = await retry_with_backoff(
        ai_provider.analyze, request, call_timeout=120,
    )

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
