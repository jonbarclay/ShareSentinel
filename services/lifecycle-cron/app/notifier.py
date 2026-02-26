"""Email notifications for sharing link lifecycle countdown and removal."""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import List, Optional

from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger(__name__)


def _find_template_dir() -> Path:
    """Locate the notification_templates directory."""
    docker_path = Path("/app/config/notification_templates")
    if docker_path.is_dir():
        return docker_path
    current = Path(__file__).resolve().parent
    for _ in range(10):
        candidate = current / "config" / "notification_templates"
        if candidate.is_dir():
            return candidate
        current = current.parent
    return Path("config/notification_templates")


class LifecycleNotifier:
    """Sends lifecycle countdown and removal notification emails."""

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        smtp_user: str,
        smtp_password: str,
        from_address: str,
        security_email: str,
        use_tls: bool = True,
        template_dir: Optional[Path] = None,
    ):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.from_address = from_address
        self.security_email = security_email
        self.use_tls = use_tls

        self._template_dir = template_dir or _find_template_dir()
        env = Environment(
            loader=FileSystemLoader(str(self._template_dir)),
            autoescape=True,
        )
        self._template = env.get_template("lifecycle_countdown.html")

    async def send_countdown_email(
        self,
        to_address: str,
        user_display_name: str,
        file_name: str,
        file_path: str,
        sharing_scope: str,
        sharing_type: str,
        link_created_date: str,
        days_remaining: int,
        removal_date: str,
        is_removal_notice: bool = False,
    ) -> bool:
        """Send a countdown notification or removal notice email.

        Returns True on success, False on failure.
        """
        try:
            # Build subject
            if is_removal_notice:
                subject = f"[ShareSentinel] Sharing link removed - {file_name}"
            else:
                subject = (
                    f"[ShareSentinel] Sharing link expires in {days_remaining} "
                    f"days - {file_name}"
                )

            # Render HTML
            html_body = self._template.render(
                user_display_name=user_display_name,
                file_name=file_name,
                file_path=file_path,
                sharing_scope=sharing_scope,
                sharing_type=sharing_type,
                link_created_date=link_created_date,
                days_remaining=days_remaining,
                removal_date=removal_date,
                is_removal_notice=is_removal_notice,
            )

            # Build recipients: user TO, security BCC
            to_addresses: List[str] = [to_address]
            all_recipients = list(to_addresses)
            if self.security_email:
                all_recipients.append(self.security_email)

            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = self.from_address
            msg["To"] = ", ".join(to_addresses)
            if self.security_email:
                msg["Bcc"] = self.security_email

            # Plain text fallback
            if is_removal_notice:
                plain = (
                    f"ShareSentinel: The sharing link for '{file_name}' has been "
                    f"removed after reaching the 180-day limit.\n\n"
                    f"File: {file_name}\nPath: {file_path}\n"
                    f"Sharing: {sharing_scope} {sharing_type}\n"
                    f"Link created: {link_created_date}\n"
                )
            else:
                plain = (
                    f"ShareSentinel: The sharing link for '{file_name}' will expire "
                    f"in {days_remaining} days on {removal_date}.\n\n"
                    f"File: {file_name}\nPath: {file_path}\n"
                    f"Sharing: {sharing_scope} {sharing_type}\n"
                    f"Link created: {link_created_date}\n"
                )

            msg.attach(MIMEText(plain, "plain"))
            msg.attach(MIMEText(html_body, "html"))

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, self._send_smtp, msg, all_recipients,
            )
            logger.info(
                "Lifecycle email sent: file=%s to=%s removal=%s",
                file_name, to_address, is_removal_notice,
            )
            return True

        except Exception:
            logger.exception(
                "Failed to send lifecycle email for %s to %s", file_name, to_address,
            )
            return False

    def _send_smtp(self, msg: MIMEMultipart, recipients: List[str]) -> None:
        """Send the message via SMTP (blocking)."""
        server: Optional[smtplib.SMTP] = None
        try:
            server = smtplib.SMTP(self.smtp_host, self.smtp_port)
            server.ehlo()
            if self.use_tls:
                server.starttls()
                server.ehlo()
            if self.smtp_user and self.smtp_password:
                server.login(self.smtp_user, self.smtp_password)
            server.sendmail(self.from_address, recipients, msg.as_string())
        finally:
            if server is not None:
                try:
                    server.quit()
                except smtplib.SMTPException:
                    pass
