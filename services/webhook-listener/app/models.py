"""Pydantic models for Splunk webhook payloads and queue jobs."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class SharingEventResult(BaseModel):
    """The 'result' object from the Splunk webhook payload."""

    Operation: str
    Workload: Optional[str] = None
    UserId: str
    ObjectId: str
    SiteUrl: Optional[str] = None
    SourceFileName: Optional[str] = None
    SourceRelativeUrl: Optional[str] = None
    ItemType: str  # "File" or "Folder"
    EventSource: Optional[str] = None
    CreationTime: Optional[str] = None
    SharingType: Optional[str] = None
    SharingScope: Optional[str] = None
    SharingPermission: Optional[str] = None

    model_config = {"extra": "allow"}


class SplunkWebhookPayload(BaseModel):
    """Top-level Splunk webhook payload."""

    result: SharingEventResult

    model_config = {"extra": "allow"}


class QueueJob(BaseModel):
    """The normalized job object pushed to the Redis queue."""

    event_id: str
    operation: str
    workload: str
    user_id: str
    object_id: str
    site_url: str
    file_name: str
    relative_path: str
    item_type: str  # "File" or "Folder"
    sharing_type: str
    sharing_scope: str
    sharing_permission: str
    event_time: str
    received_at: str
    raw_payload: dict
