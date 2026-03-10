"""Power Automate webhook notifier for Teams alerts."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class TeamsNotifier:
    """Sends plain-text alerts to a Power Automate workflow trigger."""

    def __init__(self, webhook_url: str) -> None:
        self._webhook_url = webhook_url

    @property
    def is_configured(self) -> bool:
        return bool(self._webhook_url)

    async def send_alert(self, message: str) -> bool:
        """POST a plain text message to the Power Automate webhook.

        Returns True if the request succeeded, False otherwise.
        """
        if not self._webhook_url:
            logger.warning("Teams webhook URL not configured, skipping alert")
            return False

        payload = {"text": message}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                resp = await client.post(self._webhook_url, json=payload)
                if resp.is_success:
                    logger.info("Teams alert sent successfully")
                    return True
                logger.error(
                    "Teams webhook returned %d: %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return False
        except Exception:
            logger.exception("Failed to send Teams alert")
            return False
