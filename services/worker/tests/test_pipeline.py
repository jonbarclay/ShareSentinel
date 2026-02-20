"""Integration tests for the worker pipeline orchestrator.

All external dependencies (Graph API, AI provider, database, Redis,
notifications) are mocked.  These tests verify that process_job routes
through the correct pipeline steps for different scenarios.
"""

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.ai.base_provider import AnalysisResponse
from app.config import Config
from app.notifications.base_notifier import NotificationDispatcher
from app.pipeline.orchestrator import process_job


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**overrides) -> Config:
    """Return a Config with test-friendly defaults."""
    defaults = dict(
        redis_url="redis://localhost:6379/0",
        database_url="postgresql://test:test@localhost/test",
        azure_tenant_id="tenant",
        azure_client_id="client",
        azure_client_secret="secret",
        ai_provider="anthropic",
        anthropic_api_key="sk-test",
        sensitivity_threshold=4,
        hash_reuse_days=30,
        tmpfs_path="/tmp/sharesentinel",
        notify_on_folder_share=True,
        notify_on_failure=True,
        notification_channels=["email"],
    )
    defaults.update(overrides)
    return Config(**defaults)


def _make_file_job(**overrides) -> dict:
    """Return a minimal file-share job dict."""
    base = {
        "event_id": "evt-test-001",
        "file_name": "report.pdf",
        "item_type": "File",
        "user_id": "user@org.com",
        "sharing_type": "Anonymous",
        "sharing_scope": "Anyone",
        "sharing_permission": "View",
        "event_time": "2024-01-15T10:30:00Z",
        "object_id": "https://org-my.sharepoint.com/personal/user/Documents/report.pdf",
        "site_url": "https://org-my.sharepoint.com/personal/user/",
        "source_relative_url": "personal/user/Documents",
    }
    base.update(overrides)
    return base


def _make_folder_job(**overrides) -> dict:
    base = _make_file_job(
        event_id="evt-test-folder-001",
        file_name="Budget Reports",
        item_type="Folder",
    )
    base.update(overrides)
    return base


def _make_ai_response(rating: int = 5, success: bool = True, **kw) -> AnalysisResponse:
    defaults = dict(
        sensitivity_rating=rating,
        categories_detected=["pii"],
        summary="Contains PII.",
        confidence="high",
        recommendation="Remove sharing link.",
        raw_response='{"sensitivity_rating": %d}' % rating,
        provider="anthropic",
        model="claude-sonnet-4-5-20250929",
        input_tokens=500,
        output_tokens=100,
        estimated_cost_usd=0.003,
        processing_time_seconds=1.2,
        success=success,
    )
    defaults.update(kw)
    return AnalysisResponse(**defaults)


def _mock_db_pool():
    """Return a mock asyncpg pool."""
    return MagicMock()


def _mock_redis():
    return AsyncMock()


def _mock_ai_provider(response: AnalysisResponse | None = None):
    provider = AsyncMock()
    provider.analyze = AsyncMock(return_value=response or _make_ai_response())
    return provider


def _mock_notifier_dispatcher():
    dispatcher = AsyncMock(spec=NotificationDispatcher)
    dispatcher.dispatch = AsyncMock(return_value={"email": True})
    return dispatcher


# ---------------------------------------------------------------------------
# Shared mock patches
# ---------------------------------------------------------------------------

def _common_patches():
    """Return a dict of patch targets -> mock values used by most tests."""
    return {
        "app.pipeline.orchestrator.EventRepository": MagicMock,
        "app.pipeline.orchestrator.VerdictRepository": MagicMock,
        "app.pipeline.orchestrator.FileHashRepository": MagicMock,
        "app.pipeline.orchestrator.AuditLogRepository": MagicMock,
    }


def _make_repo_mocks():
    """Create repository mocks with async methods."""
    event_repo = AsyncMock()
    event_repo.create_event = AsyncMock()
    event_repo.update_event_status = AsyncMock()
    event_repo.get_event = AsyncMock(return_value={"status": "processing"})

    verdict_repo = AsyncMock()
    verdict_repo.create_verdict = AsyncMock()

    file_hash_repo = AsyncMock()

    audit_repo = AsyncMock()
    audit_repo.log = AsyncMock()

    return event_repo, verdict_repo, file_hash_repo, audit_repo


# ===========================================================================
# Tests
# ===========================================================================

class TestFileProcessingPath(unittest.TestCase):
    """Test the happy-path file processing: download -> extract -> AI -> verdict."""

    def test_full_file_pipeline(self):
        """File share: download, extract text, AI analysis rating 5, notify."""
        job = _make_file_job()
        config = _make_config()
        ai_provider = _mock_ai_provider(_make_ai_response(rating=5))
        dispatcher = _mock_notifier_dispatcher()
        event_repo, verdict_repo, file_hash_repo, audit_repo = _make_repo_mocks()

        fake_file = Path("/tmp/sharesentinel/evt-test-001/report.pdf")

        with (
            patch("app.pipeline.orchestrator.EventRepository", return_value=event_repo),
            patch("app.pipeline.orchestrator.VerdictRepository", return_value=verdict_repo),
            patch("app.pipeline.orchestrator.FileHashRepository", return_value=file_hash_repo),
            patch("app.pipeline.orchestrator.AuditLogRepository", return_value=audit_repo),
            patch("app.pipeline.orchestrator._build_graph_auth") as mock_auth,
            patch("app.pipeline.orchestrator.GraphClient") as mock_gc,
            patch("app.pipeline.orchestrator.MetadataPrescreen") as mock_meta_cls,
            patch("app.pipeline.orchestrator.FileClassifier") as mock_classifier_cls,
            patch("app.pipeline.orchestrator.FileDownloader") as mock_dl_cls,
            patch("app.pipeline.orchestrator.FileHasher") as mock_hasher_cls,
            patch("app.pipeline.orchestrator._extract_and_build_request") as mock_extract,
            patch("app.pipeline.orchestrator.retry_with_backoff", new_callable=lambda: _passthrough_retry),
            patch("app.pipeline.orchestrator.Cleanup") as mock_cleanup,
        ):
            # Metadata returns file info
            meta_instance = AsyncMock()
            meta_instance.fetch_metadata = AsyncMock(return_value={
                "name": "report.pdf",
                "size": 1024,
                "drive_id": "drive-1",
                "item_id": "item-1",
                "parent_path": "/Documents",
            })
            mock_meta_cls.return_value = meta_instance

            # Classifier returns FULL_ANALYSIS
            from app.pipeline.classifier import Action, Category, ClassificationResult
            classification = ClassificationResult(
                category=Category.PROCESSABLE,
                action=Action.FULL_ANALYSIS,
                reason="PDF is processable",
            )
            classifier_instance = MagicMock()
            classifier_instance.classify.return_value = classification
            mock_classifier_cls.return_value = classifier_instance

            # Downloader returns a fake path
            dl_instance = AsyncMock()
            dl_instance.download = AsyncMock(return_value=fake_file)
            mock_dl_cls.return_value = dl_instance

            # Hasher: new content (no reuse)
            hasher_instance = MagicMock()
            hasher_instance.compute_hash.return_value = "abc123hash"
            hasher_instance.check_reuse = AsyncMock(return_value=None)
            mock_hasher_cls.return_value = hasher_instance

            # Extraction builds a text-mode request
            from app.ai.base_provider import AnalysisRequest
            mock_extract.return_value = AnalysisRequest(
                mode="text",
                text_content="Sensitive document content",
                file_name="report.pdf",
            )

            # Make fake_file.stat() work for audit log
            with patch.object(Path, "stat", return_value=MagicMock(st_size=1024)):
                asyncio.run(process_job(
                    job, config, _mock_db_pool(), _mock_redis(),
                    ai_provider, dispatcher,
                ))

            # Verify AI was called
            ai_provider.analyze.assert_called_once()

            # Verify verdict was recorded
            verdict_repo.create_verdict.assert_called_once()

            # Verify notification was dispatched (rating=5 >= threshold=4)
            dispatcher.dispatch.assert_called_once()

            # Verify event marked completed
            event_repo.update_event_status.assert_called()


class TestFolderSharePath(unittest.TestCase):
    """Folder shares should be immediately flagged without AI analysis."""

    def test_folder_share_flags_and_notifies(self):
        job = _make_folder_job()
        config = _make_config()
        ai_provider = _mock_ai_provider()
        dispatcher = _mock_notifier_dispatcher()
        event_repo, verdict_repo, file_hash_repo, audit_repo = _make_repo_mocks()

        with (
            patch("app.pipeline.orchestrator.EventRepository", return_value=event_repo),
            patch("app.pipeline.orchestrator.VerdictRepository", return_value=verdict_repo),
            patch("app.pipeline.orchestrator.FileHashRepository", return_value=file_hash_repo),
            patch("app.pipeline.orchestrator.AuditLogRepository", return_value=audit_repo),
            patch("app.pipeline.orchestrator.Cleanup") as mock_cleanup,
        ):
            asyncio.run(process_job(
                job, config, _mock_db_pool(), _mock_redis(),
                ai_provider, dispatcher,
            ))

            # AI should NOT be called for folder shares
            ai_provider.analyze.assert_not_called()

            # Verdict should be recorded as folder_share
            verdict_repo.create_verdict.assert_called_once()
            call_kwargs = verdict_repo.create_verdict.call_args
            assert call_kwargs.kwargs.get("analysis_mode") == "folder_flag" or \
                (call_kwargs.args and "folder_flag" in str(call_kwargs))

            # Notification should be dispatched
            dispatcher.dispatch.assert_called_once()

            # Event should be completed
            event_repo.update_event_status.assert_called()


class TestExcludedFileTypePath(unittest.TestCase):
    """Excluded file types (video, audio, etc.) should get filename-only analysis."""

    def test_excluded_file_gets_filename_only(self):
        job = _make_file_job(file_name="meeting-recording.mp4")
        config = _make_config()
        ai_response = _make_ai_response(rating=2)
        ai_provider = _mock_ai_provider(ai_response)
        dispatcher = _mock_notifier_dispatcher()
        event_repo, verdict_repo, file_hash_repo, audit_repo = _make_repo_mocks()

        with (
            patch("app.pipeline.orchestrator.EventRepository", return_value=event_repo),
            patch("app.pipeline.orchestrator.VerdictRepository", return_value=verdict_repo),
            patch("app.pipeline.orchestrator.FileHashRepository", return_value=file_hash_repo),
            patch("app.pipeline.orchestrator.AuditLogRepository", return_value=audit_repo),
            patch("app.pipeline.orchestrator._build_graph_auth"),
            patch("app.pipeline.orchestrator.GraphClient"),
            patch("app.pipeline.orchestrator.MetadataPrescreen") as mock_meta_cls,
            patch("app.pipeline.orchestrator.FileClassifier") as mock_classifier_cls,
            patch("app.pipeline.orchestrator.retry_with_backoff", new_callable=lambda: _passthrough_retry),
            patch("app.pipeline.orchestrator.Cleanup"),
        ):
            meta_instance = AsyncMock()
            meta_instance.fetch_metadata = AsyncMock(return_value={
                "name": "meeting-recording.mp4",
                "size": 100_000_000,
                "drive_id": "drive-1",
                "item_id": "item-1",
            })
            mock_meta_cls.return_value = meta_instance

            from app.pipeline.classifier import Action, Category, ClassificationResult
            classification = ClassificationResult(
                category=Category.EXCLUDED,
                action=Action.FILENAME_ONLY,
                reason="Video files are excluded",
            )
            classifier_instance = MagicMock()
            classifier_instance.classify.return_value = classification
            mock_classifier_cls.return_value = classifier_instance

            asyncio.run(process_job(
                job, config, _mock_db_pool(), _mock_redis(),
                ai_provider, dispatcher,
            ))

            # AI should be called with filename_only mode
            ai_provider.analyze.assert_called_once()
            request_arg = ai_provider.analyze.call_args[0][0]
            assert request_arg.mode == "filename_only"

            # Rating=2 < threshold=4, so no notification
            dispatcher.dispatch.assert_not_called()


class TestHashReusePath(unittest.TestCase):
    """When a file hash matches a previous analysis, skip AI and reuse the verdict."""

    def test_hash_reuse_skips_ai(self):
        job = _make_file_job(event_id="evt-hash-reuse-001")
        config = _make_config()
        ai_provider = _mock_ai_provider()
        dispatcher = _mock_notifier_dispatcher()
        event_repo, verdict_repo, file_hash_repo, audit_repo = _make_repo_mocks()

        fake_file = Path("/tmp/sharesentinel/evt-hash-reuse-001/report.pdf")

        with (
            patch("app.pipeline.orchestrator.EventRepository", return_value=event_repo),
            patch("app.pipeline.orchestrator.VerdictRepository", return_value=verdict_repo),
            patch("app.pipeline.orchestrator.FileHashRepository", return_value=file_hash_repo),
            patch("app.pipeline.orchestrator.AuditLogRepository", return_value=audit_repo),
            patch("app.pipeline.orchestrator._build_graph_auth"),
            patch("app.pipeline.orchestrator.GraphClient"),
            patch("app.pipeline.orchestrator.MetadataPrescreen") as mock_meta_cls,
            patch("app.pipeline.orchestrator.FileClassifier") as mock_classifier_cls,
            patch("app.pipeline.orchestrator.FileDownloader") as mock_dl_cls,
            patch("app.pipeline.orchestrator.FileHasher") as mock_hasher_cls,
            patch("app.pipeline.orchestrator.retry_with_backoff", new_callable=lambda: _passthrough_retry),
            patch("app.pipeline.orchestrator.Cleanup"),
        ):
            meta_instance = AsyncMock()
            meta_instance.fetch_metadata = AsyncMock(return_value={
                "name": "report.pdf", "size": 1024,
                "drive_id": "d1", "item_id": "i1",
            })
            mock_meta_cls.return_value = meta_instance

            from app.pipeline.classifier import Action, Category, ClassificationResult
            classifier_instance = MagicMock()
            classifier_instance.classify.return_value = ClassificationResult(
                category=Category.PROCESSABLE,
                action=Action.FULL_ANALYSIS,
                reason="PDF",
            )
            mock_classifier_cls.return_value = classifier_instance

            dl_instance = AsyncMock()
            dl_instance.download = AsyncMock(return_value=fake_file)
            mock_dl_cls.return_value = dl_instance

            # Hasher finds a previous match
            hasher_instance = MagicMock()
            hasher_instance.compute_hash.return_value = "existing_hash_abc"
            hasher_instance.check_reuse = AsyncMock(return_value={
                "first_event_id": "evt-original-001",
                "sensitivity_rating": 3,
            })
            mock_hasher_cls.return_value = hasher_instance

            with patch.object(Path, "stat", return_value=MagicMock(st_size=1024)):
                asyncio.run(process_job(
                    job, config, _mock_db_pool(), _mock_redis(),
                    ai_provider, dispatcher,
                ))

            # AI should NOT be called -- hash reuse should short-circuit
            ai_provider.analyze.assert_not_called()

            # Verdict should still be recorded (with reused rating)
            verdict_repo.create_verdict.assert_called_once()

            # Rating=3 < threshold=4 so no notification
            dispatcher.dispatch.assert_not_called()


class TestFileNotFoundHandling(unittest.TestCase):
    """When Graph API returns 404, the pipeline should mark the event completed."""

    def test_file_not_found_completes_without_analysis(self):
        job = _make_file_job(event_id="evt-404-001")
        config = _make_config()
        ai_provider = _mock_ai_provider()
        dispatcher = _mock_notifier_dispatcher()
        event_repo, verdict_repo, file_hash_repo, audit_repo = _make_repo_mocks()

        with (
            patch("app.pipeline.orchestrator.EventRepository", return_value=event_repo),
            patch("app.pipeline.orchestrator.VerdictRepository", return_value=verdict_repo),
            patch("app.pipeline.orchestrator.FileHashRepository", return_value=file_hash_repo),
            patch("app.pipeline.orchestrator.AuditLogRepository", return_value=audit_repo),
            patch("app.pipeline.orchestrator._build_graph_auth"),
            patch("app.pipeline.orchestrator.GraphClient"),
            patch("app.pipeline.orchestrator.MetadataPrescreen") as mock_meta_cls,
            patch("app.pipeline.orchestrator.FileClassifier") as mock_classifier_cls,
            patch("app.pipeline.orchestrator.FileDownloader") as mock_dl_cls,
            patch("app.pipeline.orchestrator.retry_with_backoff", new_callable=lambda: _passthrough_retry),
            patch("app.pipeline.orchestrator.Cleanup"),
        ):
            meta_instance = AsyncMock()
            meta_instance.fetch_metadata = AsyncMock(return_value={
                "name": "deleted.pdf", "size": 1024,
                "drive_id": "d1", "item_id": "i1",
            })
            mock_meta_cls.return_value = meta_instance

            from app.pipeline.classifier import Action, Category, ClassificationResult
            classifier_instance = MagicMock()
            classifier_instance.classify.return_value = ClassificationResult(
                category=Category.PROCESSABLE,
                action=Action.FULL_ANALYSIS,
                reason="PDF",
            )
            mock_classifier_cls.return_value = classifier_instance

            from app.pipeline.downloader import DownloadError
            dl_instance = AsyncMock()
            dl_instance.download = AsyncMock(
                side_effect=DownloadError("file_not_found", "File not found")
            )
            mock_dl_cls.return_value = dl_instance

            asyncio.run(process_job(
                job, config, _mock_db_pool(), _mock_redis(),
                ai_provider, dispatcher,
            ))

            # AI should not be called
            ai_provider.analyze.assert_not_called()

            # Event should be marked completed with file_not_found
            update_calls = event_repo.update_event_status.call_args_list
            found_not_found = any(
                "file_not_found" in str(c) for c in update_calls
            )
            assert found_not_found, f"Expected file_not_found in update calls: {update_calls}"


class TestAIAnalysisFailureHandling(unittest.TestCase):
    """When the AI provider returns a failure response, the pipeline should handle it."""

    def test_ai_failure_still_records_verdict(self):
        job = _make_file_job(event_id="evt-ai-fail-001")
        config = _make_config()
        failed_response = _make_ai_response(
            rating=0, success=False, error="API timeout",
        )
        ai_provider = _mock_ai_provider(failed_response)
        dispatcher = _mock_notifier_dispatcher()
        event_repo, verdict_repo, file_hash_repo, audit_repo = _make_repo_mocks()

        fake_file = Path("/tmp/sharesentinel/evt-ai-fail-001/data.xlsx")

        with (
            patch("app.pipeline.orchestrator.EventRepository", return_value=event_repo),
            patch("app.pipeline.orchestrator.VerdictRepository", return_value=verdict_repo),
            patch("app.pipeline.orchestrator.FileHashRepository", return_value=file_hash_repo),
            patch("app.pipeline.orchestrator.AuditLogRepository", return_value=audit_repo),
            patch("app.pipeline.orchestrator._build_graph_auth"),
            patch("app.pipeline.orchestrator.GraphClient"),
            patch("app.pipeline.orchestrator.MetadataPrescreen") as mock_meta_cls,
            patch("app.pipeline.orchestrator.FileClassifier") as mock_classifier_cls,
            patch("app.pipeline.orchestrator.FileDownloader") as mock_dl_cls,
            patch("app.pipeline.orchestrator.FileHasher") as mock_hasher_cls,
            patch("app.pipeline.orchestrator._extract_and_build_request") as mock_extract,
            patch("app.pipeline.orchestrator.retry_with_backoff", new_callable=lambda: _passthrough_retry),
            patch("app.pipeline.orchestrator.Cleanup"),
        ):
            meta_instance = AsyncMock()
            meta_instance.fetch_metadata = AsyncMock(return_value={
                "name": "data.xlsx", "size": 2048,
                "drive_id": "d1", "item_id": "i1",
            })
            mock_meta_cls.return_value = meta_instance

            from app.pipeline.classifier import Action, Category, ClassificationResult
            classifier_instance = MagicMock()
            classifier_instance.classify.return_value = ClassificationResult(
                category=Category.PROCESSABLE,
                action=Action.FULL_ANALYSIS,
                reason="Excel",
            )
            mock_classifier_cls.return_value = classifier_instance

            dl_instance = AsyncMock()
            dl_instance.download = AsyncMock(return_value=fake_file)
            mock_dl_cls.return_value = dl_instance

            hasher_instance = MagicMock()
            hasher_instance.compute_hash.return_value = "hash_xyz"
            hasher_instance.check_reuse = AsyncMock(return_value=None)
            mock_hasher_cls.return_value = hasher_instance

            from app.ai.base_provider import AnalysisRequest
            mock_extract.return_value = AnalysisRequest(
                mode="text", text_content="Spreadsheet data",
                file_name="data.xlsx",
            )

            with patch.object(Path, "stat", return_value=MagicMock(st_size=2048)):
                asyncio.run(process_job(
                    job, config, _mock_db_pool(), _mock_redis(),
                    ai_provider, dispatcher,
                ))

            # AI was called (returned failure response)
            ai_provider.analyze.assert_called_once()

            # Verdict should still be recorded (rating=0 from failure)
            verdict_repo.create_verdict.assert_called_once()

            # Rating=0 < threshold=4, no notification
            dispatcher.dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# Retry passthrough helper
# ---------------------------------------------------------------------------

def _passthrough_retry():
    """Return an async function that calls the target directly (no retries)."""

    async def _call(fn, *args, **kwargs):
        return await fn(*args, **kwargs)

    return _call


if __name__ == "__main__":
    unittest.main()
