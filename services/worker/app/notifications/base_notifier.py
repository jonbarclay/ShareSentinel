"""Abstract base class for notification channels and shared data structures."""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class AlertPayload:
    """All the information an analyst needs to act on an alert."""

    event_id: str
    alert_type: str  # "high_sensitivity_file", "folder_share", "processing_failure"

    # File/item details
    file_name: str
    file_path: str
    file_size_human: str  # e.g., "2.3 MB"
    item_type: str  # "File" or "Folder"

    # Sharing details
    sharing_user: str
    sharing_type: str  # "Anonymous" or "Organization-wide"
    sharing_permission: str  # "View" or "Edit"
    event_time: str
    sharing_link_url: Optional[str] = None
    sharing_links: Optional[List[Dict[str, str]]] = None

    # AI analysis results (None for folder shares and processing failures)
    sensitivity_rating: Optional[int] = None
    categories_detected: Optional[List[str]] = None
    summary: Optional[str] = None
    confidence: Optional[str] = None
    recommendation: Optional[str] = None
    analysis_mode: Optional[str] = None  # "text", "multimodal", "filename_only"

    # Remediation context (populated only for remediation_report)
    permission_details: Optional[List[Dict[str, str]]] = None

    # Additional context
    filename_flagged: bool = False
    filename_flag_keywords: Optional[List[str]] = None
    was_sampled: bool = False
    sampling_description: Optional[str] = None
    failure_reason: Optional[str] = None


class BaseNotifier(ABC):
    """Abstract base class for notification channels."""

    @abstractmethod
    async def send_alert(self, payload: AlertPayload) -> bool:
        """Send an alert to analysts. Returns True if successful."""
        pass

    @abstractmethod
    def get_channel_name(self) -> str:
        """Return the notification channel name (e.g., 'email', 'jira')."""
        pass


class NotificationDispatcher:
    """Sends alerts through all configured notification channels."""

    def __init__(self, notifiers: List[BaseNotifier]):
        self.notifiers = notifiers

    async def dispatch(self, payload: AlertPayload) -> Dict[str, bool]:
        """Send alert through all configured channels. Returns {channel: success}."""
        results = {}
        for notifier in self.notifiers:
            channel = notifier.get_channel_name()
            try:
                success = await notifier.send_alert(payload)
                results[channel] = success
            except Exception as e:
                logger.error(f"Notification channel {channel} failed: {e}")
                results[channel] = False
        return results
