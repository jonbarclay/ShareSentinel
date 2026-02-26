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

# Default template location — /app/config in Docker, or walk up to find config/ locally
def _find_template_dir() -> Path:
    """Locate the notification_templates directory."""
    # Docker: config is mounted at /app/config
    docker_path = Path("/app/config/notification_templates")
    if docker_path.is_dir():
        return docker_path
    # Local dev: walk up from this file to find config/
    current = Path(__file__).resolve().parent
    for _ in range(10):
        candidate = current / "config" / "notification_templates"
        if candidate.is_dir():
            return candidate
        current = current.parent
    return Path("config/notification_templates")

_DEFAULT_TEMPLATE_DIR = _find_template_dir()
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
        from ..ai.base_provider import CATEGORY_LABELS
        if payload.alert_type == "high_sensitivity_file":
            tier_label = "URGENT" if payload.escalation_tier == "tier_1" else "Alert"
            top_cat = ""
            if payload.category_ids:
                top_id = payload.category_ids[0]
                top_cat = CATEGORY_LABELS.get(top_id, top_id)
            return (
                f"[ShareSentinel] [{tier_label}] {top_cat} - {payload.file_name}"
            )
        elif payload.alert_type == "folder_share":
            return (
                f"[ShareSentinel] Folder Shared with {payload.sharing_type} "
                f"Access - {payload.file_name}"
            )
        elif payload.alert_type == "folder_share_enumerated":
            flagged = payload.folder_flagged_files
            total = payload.folder_total_files
            label = f"{flagged} flagged" if flagged else "No sensitive files"
            return (
                f"[ShareSentinel] Folder Scan: {label} "
                f"({total} files) - {payload.file_name}"
            )
        elif payload.alert_type == "remediation_report":
            tier_label = payload.escalation_tier or "unknown"
            return (
                f"[ShareSentinel] Sharing Link Removed - {payload.file_name} "
                f"({tier_label})"
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
            "remediation_report": "SHARING LINK REMEDIATION REPORT",
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
        if payload.alert_type in ("high_sensitivity_file", "remediation_report") and payload.categories:
            from ..ai.base_provider import CATEGORY_LABELS
            lines.append("AI ANALYSIS RESULTS")
            lines.append(f"  Escalation tier: {payload.escalation_tier or 'none'}")
            lines.append(f"  Context: {payload.context or 'unknown'}")
            lines.append("  Detected categories:")
            for cat in payload.categories:
                label = CATEGORY_LABELS.get(cat.id, cat.id)
                lines.append(f"    - {label} (confidence: {cat.confidence})")
                if cat.evidence:
                    lines.append(f"      Evidence: {cat.evidence}")
            if payload.summary:
                lines.append(f"  Summary: {payload.summary}")
            if payload.recommendation:
                lines.append(f"  Recommendation: {payload.recommendation}")
            if payload.analysis_mode:
                lines.append(f"  Analysis mode: {payload.analysis_mode}")
            if payload.was_sampled and payload.sampling_description:
                lines.append(f"  Note: {payload.sampling_description}")
            lines.append("")

        # Remediation report
        if payload.alert_type == "remediation_report":
            lines.append("ACTION TAKEN")
            lines.append("  Sharing permissions have been removed for this item.")
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
        elif payload.alert_type in ("folder_share", "folder_share_enumerated"):
            lines.append(
                "  A folder with broad sharing may expose future files. Review "
                "the folder contents and sharing settings immediately."
            )

        # Folder enumeration child results
        if payload.alert_type == "folder_share_enumerated" and payload.child_summaries:
            lines.append("")
            lines.append("FOLDER SCAN RESULTS")
            lines.append(f"  Total files: {payload.folder_total_files}")
            lines.append(f"  Flagged: {payload.folder_flagged_files}")
            lines.append(f"  Clean: {payload.folder_clean_files}")
            lines.append(f"  Failed: {payload.folder_failed_files}")
            flagged = [c for c in payload.child_summaries if c.get("escalation_tier") in ("tier_1", "tier_2")]
            if flagged:
                lines.append("")
                lines.append("  FLAGGED FILES:")
                for child in flagged:
                    lines.append(f"    - {child.get('file_name', '?')} [{child.get('escalation_tier', '?')}]")
                    cats = child.get("categories", [])
                    if cats:
                        lines.append(f"      Categories: {', '.join(cats)}")
                    if child.get("summary"):
                        lines.append(f"      {child['summary']}")
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
