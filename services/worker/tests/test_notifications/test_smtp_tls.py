"""Tests for SMTP TLS certificate verification."""

import ssl
import smtplib
from unittest.mock import MagicMock, patch, call
from pathlib import Path

from app.notifications.email_notifier import EmailNotifier


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
    defaults = dict(
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="user@example.com",
        smtp_password="secret",
        from_address="sharesentinel@example.com",
        to_addresses=["analyst@example.com"],
        use_tls=True,
        template_dir=_TEMPLATE_DIR,
    )
    defaults.update(overrides)
    return EmailNotifier(**defaults)


class TestSmtpTls:
    """Verify starttls is called with an ssl.SSLContext."""

    def test_starttls_receives_ssl_context(self):
        """The _send_smtp method must pass context=<SSLContext> to starttls."""
        notifier = _make_notifier()
        mock_smtp = MagicMock()

        with patch("app.notifications.email_notifier.smtplib.SMTP", return_value=mock_smtp):
            msg = MagicMock()
            msg.as_string.return_value = "test message"
            notifier._send_smtp(msg)

        # starttls should have been called with a context kwarg
        mock_smtp.starttls.assert_called_once()
        call_kwargs = mock_smtp.starttls.call_args
        ctx = call_kwargs.kwargs.get("context") or (
            call_kwargs.args[0] if call_kwargs.args else None
        )
        assert isinstance(ctx, ssl.SSLContext), (
            f"starttls must receive an ssl.SSLContext, got {type(ctx)}"
        )

    def test_starttls_not_called_when_tls_disabled(self):
        notifier = _make_notifier(use_tls=False)
        mock_smtp = MagicMock()

        with patch("app.notifications.email_notifier.smtplib.SMTP", return_value=mock_smtp):
            msg = MagicMock()
            msg.as_string.return_value = "test message"
            notifier._send_smtp(msg)

        mock_smtp.starttls.assert_not_called()
