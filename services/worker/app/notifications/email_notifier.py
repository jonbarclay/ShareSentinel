"""Email notification channel using SMTP with Jinja2-templated HTML."""

import asyncio
import logging
import smtplib
from dataclasses import asdict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import List, Optional

from jinja2 import Environment, FileSystemLoader, Template

from .base_notifier import AlertPayload, BaseNotifier

logger = logging.getLogger(__name__)

# Default template location relative to project root
_DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parents[4] / "config" / "notification_templates"
_DEFAULT_TEMPLATE_NAME = "analyst_alert.html"


class EmailNotifier(BaseNotifier):
    """Sends formatted HTML email alerts to analysts via SMTP."""

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        smtp_user: str,
        smtp_password: str,
        from_address: str,
        to_addresses: List[str],
        use_tls: bool = True,
        template_dir: Optional[Path] = None,
        template_name: Optional[str] = None,
    ):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.from_address = from_address
        self.to_addresses = to_addresses
        self.use_tls = use_tls

        self._template_dir = template_dir or _DEFAULT_TEMPLATE_DIR
        self._template_name = template_name or _DEFAULT_TEMPLATE_NAME
        self._template = self._load_template()

    # ------------------------------------------------------------------
    # Template loading
    # ------------------------------------------------------------------

    def _load_template(self) -> Template:
        """Load the Jinja2 HTML email template from disk."""
        env = Environment(
            loader=FileSystemLoader(str(self._template_dir)),
            autoescape=True,
        )
        try:
            template = env.get_template(self._template_name)
            logger.info(
                "Loaded email template from %s/%s",
                self._template_dir,
                self._template_name,
            )
            return template
        except Exception:
            logger.exception("Failed to load email template")
            raise

    # ------------------------------------------------------------------
    # Subject line construction
    # ------------------------------------------------------------------

    @staticmethod
    def _build_subject(payload: AlertPayload) -> str:
        """Build the email subject line based on alert type."""
        if payload.alert_type == "high_sensitivity_file":
            return (
                f"[ShareSentinel] High Sensitivity File Detected "
                f"(Rating: {payload.sensitivity_rating}/5) - {payload.file_name}"
            )
        elif payload.alert_type == "folder_share":
            return (
                f"[ShareSentinel] Folder Shared with {payload.sharing_type} "
                f"Access - {payload.file_name}"
            )
        elif payload.alert_type == "processing_failure":
            return f"[ShareSentinel] Processing Failed - {payload.file_name}"
        else:
            return f"[ShareSentinel] Alert - {payload.file_name}"

    # ------------------------------------------------------------------
    # Plain-text fallback
    # ------------------------------------------------------------------

    @staticmethod
    def _build_plain_text(payload: AlertPayload) -> str:
        """Build a plain-text fallback version of the alert email."""
        lines: List[str] = []
        lines.append("=" * 60)
        lines.append("ShareSentinel Alert")
        lines.append("=" * 60)
        lines.append("")

        # Alert type
        type_labels = {
            "high_sensitivity_file": "HIGH SENSITIVITY FILE DETECTED",
            "folder_share": "FOLDER SHARED WITH BROAD ACCESS",
            "processing_failure": "PROCESSING FAILURE",
        }
        lines.append(type_labels.get(payload.alert_type, "ALERT"))
        lines.append("")

        # File details
        lines.append("FILE DETAILS")
        lines.append(f"  Name: {payload.file_name}")
        lines.append(f"  Path: {payload.file_path}")
        lines.append(f"  Size: {payload.file_size_human}")
        lines.append(f"  Type: {payload.item_type}")
        lines.append("")

        # Sharing details
        lines.append("SHARING DETAILS")
        lines.append(f"  Shared by: {payload.sharing_user}")
        lines.append(f"  Sharing type: {payload.sharing_type}")
        lines.append(f"  Permission: {payload.sharing_permission}")
        lines.append(f"  Time: {payload.event_time}")
        if payload.sharing_link_url:
            lines.append(f"  Link: {payload.sharing_link_url}")
        lines.append("")

        # AI analysis (only for file alerts)
        if payload.alert_type == "high_sensitivity_file" and payload.sensitivity_rating is not None:
            lines.append("AI ANALYSIS RESULTS")
            lines.append(f"  Sensitivity rating: {payload.sensitivity_rating}/5")
            if payload.categories_detected:
                lines.append(f"  Categories: {', '.join(payload.categories_detected)}")
            if payload.summary:
                lines.append(f"  Summary: {payload.summary}")
            if payload.confidence:
                lines.append(f"  Confidence: {payload.confidence}")
            if payload.recommendation:
                lines.append(f"  Recommendation: {payload.recommendation}")
            if payload.analysis_mode:
                lines.append(f"  Analysis mode: {payload.analysis_mode}")
            if payload.was_sampled and payload.sampling_description:
                lines.append(f"  Note: {payload.sampling_description}")
            lines.append("")

        # Failure reason
        if payload.alert_type == "processing_failure" and payload.failure_reason:
            lines.append("FAILURE REASON")
            lines.append(f"  {payload.failure_reason}")
            lines.append("")

        # Action required
        lines.append("ACTION REQUIRED")
        if payload.alert_type == "high_sensitivity_file":
            lines.append(
                "  Review the file and sharing settings. Consider contacting the "
                "user to restrict sharing or remove the link."
            )
        elif payload.alert_type == "folder_share":
            lines.append(
                "  A folder with broad sharing may expose future files. Review "
                "the folder contents and sharing settings immediately."
            )
        elif payload.alert_type == "processing_failure":
            lines.append(
                "  This sharing event could not be evaluated automatically. "
                "Please review the file manually."
            )
        lines.append("")

        # Footer
        lines.append("-" * 60)
        lines.append(f"Event ID: {payload.event_id}")
        lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # HTML rendering
    # ------------------------------------------------------------------

    def _render_html(self, payload: AlertPayload) -> str:
        """Render the Jinja2 HTML template with payload data."""
        return self._template.render(**asdict(payload))

    # ------------------------------------------------------------------
    # SMTP sending
    # ------------------------------------------------------------------

    def _send_smtp(self, msg: MIMEMultipart) -> None:
        """Send the message via SMTP (blocking). Intended to run in a thread."""
        server: Optional[smtplib.SMTP] = None
        try:
            server = smtplib.SMTP(self.smtp_host, self.smtp_port)
            server.ehlo()
            if self.use_tls:
                server.starttls()
                server.ehlo()
            if self.smtp_user and self.smtp_password:
                server.login(self.smtp_user, self.smtp_password)
            server.sendmail(self.from_address, self.to_addresses, msg.as_string())
            logger.debug("SMTP sendmail completed successfully")
        finally:
            if server is not None:
                try:
                    server.quit()
                except smtplib.SMTPException:
                    pass

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def send_alert(self, payload: AlertPayload) -> bool:
        """Build and send the alert email. Returns True on success."""
        try:
            subject = self._build_subject(payload)
            html_body = self._render_html(payload)
            text_body = self._build_plain_text(payload)

            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = self.from_address
            msg["To"] = ", ".join(self.to_addresses)

            msg.attach(MIMEText(text_body, "plain"))
            msg.attach(MIMEText(html_body, "html"))

            # Run blocking SMTP in a thread so we don't block the event loop
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._send_smtp, msg)

            logger.info(
                "Email alert sent for event %s to %s",
                payload.event_id,
                ", ".join(self.to_addresses),
            )
            return True

        except Exception:
            logger.exception(
                "Failed to send email alert for event %s", payload.event_id
            )
            return False

    def get_channel_name(self) -> str:
        return "email"
