"""Tests for EmailNotifier - subject lines, template rendering, SMTP sending."""

import asyncio
import smtplib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.notifications.base_notifier import AlertPayload
from app.notifications.email_notifier import EmailNotifier


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _find_template_dir() -> Path:
    """Walk up from the test file to find config/notification_templates."""
    current = Path(__file__).resolve().parent
    for _ in range(10):
        candidate = current / "config" / "notification_templates"
        if candidate.is_dir():
            return candidate
        current = current.parent
    return Path("/app/config/notification_templates")

_TEMPLATE_DIR = _find_template_dir()


def _make_notifier(**overrides) -> EmailNotifier:
    """Create an EmailNotifier with test defaults."""
    defaults = dict(
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="user@example.com",
        smtp_password="secret",
        from_address="sharesentinel@example.com",
        to_addresses=["analyst1@example.com", "analyst2@example.com"],
        use_tls=True,
        template_dir=_TEMPLATE_DIR,
    )
    defaults.update(overrides)
    return EmailNotifier(**defaults)


def _sample_payload(**overrides) -> AlertPayload:
    """Return a realistic AlertPayload for testing."""
    from app.ai.base_provider import CategoryDetection
    defaults = dict(
        event_id="evt-abc-123",
        alert_type="high_sensitivity_file",
        file_name="Q4-Financials.xlsx",
        file_path="/sites/Finance/Shared Documents/Q4-Financials.xlsx",
        file_size_human="2.3 MB",
        item_type="File",
        sharing_user="jane.doe@contoso.com",
        sharing_type="Anonymous",
        sharing_permission="Edit",
        event_time="2026-02-20T14:32:00Z",
        sharing_link_url="https://contoso.sharepoint.com/s/abc123",
        categories=[
            CategoryDetection(id="pii_financial", confidence="high", evidence="Account numbers in column B"),
            CategoryDetection(id="pii_government_id", confidence="high", evidence="SSNs in column D"),
        ],
        escalation_tier="tier_1",
        context="institutional",
        summary="Contains quarterly revenue figures and employee SSNs.",
        recommendation="Restrict sharing immediately.",
        analysis_mode="text",
        was_sampled=True,
        sampling_description="Only the first 100KB of extracted text was analyzed.",
    )
    defaults.update(overrides)
    return AlertPayload(**defaults)


# ---------------------------------------------------------------------------
# Subject Line Tests
# ---------------------------------------------------------------------------

class TestSubjectLine:
    """Verify subject lines for each alert type."""

    def test_tier_1_subject(self):
        payload = _sample_payload(
            alert_type="high_sensitivity_file",
            file_name="secrets.docx",
            escalation_tier="tier_1",
        )
        subject = EmailNotifier._build_subject(payload)
        assert "[URGENT]" in subject
        assert "secrets.docx" in subject

    def test_tier_2_subject(self):
        from app.ai.base_provider import CategoryDetection
        payload = _sample_payload(
            alert_type="high_sensitivity_file",
            file_name="report.pdf",
            escalation_tier="tier_2",
            categories=[CategoryDetection(id="hr_personnel", confidence="high", evidence="salary data")],
        )
        subject = EmailNotifier._build_subject(payload)
        assert "[Alert]" in subject
        assert "report.pdf" in subject
        assert "HR/Personnel" in subject

    def test_folder_share_subject(self):
        payload = _sample_payload(
            alert_type="folder_share",
            file_name="HR Documents",
            sharing_type="Organization-wide",
        )
        subject = EmailNotifier._build_subject(payload)
        assert subject == (
            "[ShareSentinel] Folder Shared with Organization-wide "
            "Access - HR Documents"
        )

    def test_processing_failure_subject(self):
        payload = _sample_payload(
            alert_type="processing_failure",
            file_name="corrupted.pdf",
        )
        subject = EmailNotifier._build_subject(payload)
        assert subject == "[ShareSentinel] Processing Failed - corrupted.pdf"

    def test_unknown_type_fallback(self):
        payload = _sample_payload(alert_type="unknown_type", file_name="data.csv")
        subject = EmailNotifier._build_subject(payload)
        assert subject == "[ShareSentinel] Alert - data.csv"


# ---------------------------------------------------------------------------
# Template Rendering Tests
# ---------------------------------------------------------------------------

class TestTemplateRendering:
    """Verify Jinja2 template renders with various payloads."""

    def test_renders_high_sensitivity(self):
        notifier = _make_notifier()
        payload = _sample_payload()
        html = notifier._render_html(payload)

        assert "Q4-Financials.xlsx" in html
        assert "jane.doe@contoso.com" in html
        assert "tier_1" in html
        assert "pii_financial" in html
        assert "pii_government_id" in html
        assert "institutional" in html  # context
        assert "text" in html  # analysis_mode
        assert "100KB" in html  # sampling note

    def test_renders_folder_share_without_ai_section(self):
        notifier = _make_notifier()
        payload = _sample_payload(
            alert_type="folder_share",
            item_type="Folder",
            file_name="Shared Folder",
            categories=None,
            escalation_tier=None,
            context=None,
            summary=None,
            recommendation=None,
            analysis_mode=None,
            was_sampled=False,
            sampling_description=None,
        )
        html = notifier._render_html(payload)

        assert "Shared Folder" in html
        assert "Folder Shared with Broad Access" in html
        # AI analysis section should NOT be present
        assert "AI Analysis Results" not in html

    def test_renders_processing_failure_with_reason(self):
        notifier = _make_notifier()
        payload = _sample_payload(
            alert_type="processing_failure",
            file_name="broken.docx",
            failure_reason="Graph API returned 403 Forbidden",
            categories=None,
            escalation_tier=None,
            context=None,
            summary=None,
            recommendation=None,
            analysis_mode=None,
        )
        html = notifier._render_html(payload)

        assert "broken.docx" in html
        assert "Processing Failure" in html
        assert "Graph API returned 403 Forbidden" in html
        assert "AI Analysis Results" not in html

    def test_plain_text_fallback(self):
        payload = _sample_payload()
        text = EmailNotifier._build_plain_text(payload)

        assert "Q4-Financials.xlsx" in text
        assert "tier_1" in text
        assert "Financial" in text  # category label
        assert "jane.doe@contoso.com" in text
        assert "evt-abc-123" in text

    def test_plain_text_folder_no_ai(self):
        payload = _sample_payload(
            alert_type="folder_share",
            categories=None,
            escalation_tier=None,
            summary=None,
        )
        text = EmailNotifier._build_plain_text(payload)

        assert "AI ANALYSIS RESULTS" not in text
        assert "FOLDER SHARED WITH BROAD ACCESS" in text


# ---------------------------------------------------------------------------
# SMTP Send Tests
# ---------------------------------------------------------------------------

class TestSendAlert:
    """Verify send_alert builds the MIME message and calls SMTP."""

    @pytest.mark.asyncio
    async def test_send_alert_success(self):
        notifier = _make_notifier()
        payload = _sample_payload()

        mock_smtp_instance = MagicMock()
        with patch("app.notifications.email_notifier.smtplib.SMTP") as mock_smtp_cls:
            mock_smtp_cls.return_value = mock_smtp_instance

            result = await notifier.send_alert(payload)

        assert result is True
        mock_smtp_cls.assert_called_once_with("smtp.example.com", 587)
        mock_smtp_instance.ehlo.assert_called()
        mock_smtp_instance.starttls.assert_called_once()
        mock_smtp_instance.login.assert_called_once_with("user@example.com", "secret")
        mock_smtp_instance.sendmail.assert_called_once()

        # Verify sendmail arguments
        call_args = mock_smtp_instance.sendmail.call_args
        assert call_args[0][0] == "sharesentinel@example.com"
        assert call_args[0][1] == ["analyst1@example.com", "analyst2@example.com"]
        # Third arg is the message string
        msg_str = call_args[0][2]
        assert "Q4-Financials.xlsx" in msg_str

        mock_smtp_instance.quit.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_alert_no_tls(self):
        notifier = _make_notifier(use_tls=False)
        payload = _sample_payload()

        mock_smtp_instance = MagicMock()
        with patch("app.notifications.email_notifier.smtplib.SMTP") as mock_smtp_cls:
            mock_smtp_cls.return_value = mock_smtp_instance

            result = await notifier.send_alert(payload)

        assert result is True
        mock_smtp_instance.starttls.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_alert_smtp_failure_returns_false(self):
        notifier = _make_notifier()
        payload = _sample_payload()

        with patch("app.notifications.email_notifier.smtplib.SMTP") as mock_smtp_cls:
            mock_smtp_cls.side_effect = smtplib.SMTPConnectError(421, "Service unavailable")

            result = await notifier.send_alert(payload)

        assert result is False

    @pytest.mark.asyncio
    async def test_send_alert_no_credentials(self):
        """When smtp_user/smtp_password are empty, login should be skipped."""
        notifier = _make_notifier(smtp_user="", smtp_password="")
        payload = _sample_payload()

        mock_smtp_instance = MagicMock()
        with patch("app.notifications.email_notifier.smtplib.SMTP") as mock_smtp_cls:
            mock_smtp_cls.return_value = mock_smtp_instance

            result = await notifier.send_alert(payload)

        assert result is True
        mock_smtp_instance.login.assert_not_called()
