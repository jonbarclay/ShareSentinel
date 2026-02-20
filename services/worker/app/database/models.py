"""Data classes matching the PostgreSQL schema tables."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional


@dataclass
class EventRecord:
    """Mirrors the ``events`` table."""

    id: int = 0
    event_id: str = ""

    # Splunk webhook payload
    operation: str = ""
    workload: Optional[str] = None
    user_id: str = ""
    object_id: str = ""
    site_url: Optional[str] = None
    file_name: Optional[str] = None
    relative_path: Optional[str] = None
    item_type: str = ""
    sharing_type: Optional[str] = None
    sharing_scope: Optional[str] = None
    sharing_permission: Optional[str] = None
    event_time: Optional[datetime] = None

    # Graph API metadata
    confirmed_file_name: Optional[str] = None
    file_size_bytes: Optional[int] = None
    mime_type: Optional[str] = None
    web_url: Optional[str] = None
    sharing_link_url: Optional[str] = None
    drive_id: Optional[str] = None
    item_id_graph: Optional[str] = None

    # Processing state
    status: str = "queued"
    processing_started_at: Optional[datetime] = None
    processing_completed_at: Optional[datetime] = None

    # Processing details
    file_category: Optional[str] = None
    extraction_method: Optional[str] = None
    was_sampled: bool = False
    sampling_description: Optional[str] = None
    file_hash: Optional[str] = None
    hash_match_reuse: bool = False
    hash_match_event_id: Optional[str] = None
    filename_flagged: bool = False
    filename_flag_keywords: Optional[str] = None

    # Failure info
    failure_reason: Optional[str] = None
    retry_count: int = 0

    # Cleanup tracking
    temp_file_deleted: bool = False

    # Metadata
    received_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    raw_payload: Optional[Dict[str, Any]] = None


@dataclass
class VerdictRecord:
    """Mirrors the ``verdicts`` table."""

    id: int = 0
    event_id: str = ""

    # AI verdict (category-based)
    sensitivity_rating: Optional[int] = None  # legacy, nullable
    categories_detected: List[str] = field(default_factory=list)  # legacy
    category_assessments: List[Dict[str, Any]] = field(default_factory=list)
    overall_context: Optional[str] = None
    escalation_tier: Optional[str] = None  # "tier_1", "tier_2", "none"
    summary: Optional[str] = None
    confidence: Optional[str] = None  # legacy, kept for backward compat
    recommendation: Optional[str] = None

    # Analysis metadata
    analysis_mode: str = ""
    ai_provider: str = ""
    ai_model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: Decimal = Decimal("0")
    processing_time_seconds: Optional[Decimal] = None

    # Notification tracking
    notification_required: bool = False
    notification_sent: bool = False
    notification_sent_at: Optional[datetime] = None
    notification_channel: Optional[str] = None
    notification_reference: Optional[str] = None

    # Analyst disposition
    analyst_reviewed: bool = False
    analyst_reviewed_at: Optional[datetime] = None
    analyst_reviewed_by: Optional[str] = None
    analyst_disposition: Optional[str] = None
    analyst_notes: Optional[str] = None

    created_at: Optional[datetime] = None


@dataclass
class FileHashRecord:
    """Mirrors the ``file_hashes`` table."""

    id: int = 0
    file_hash: str = ""
    first_event_id: str = ""
    sensitivity_rating: Optional[int] = None  # legacy
    category_ids: List[str] = field(default_factory=list)
    last_seen_at: Optional[datetime] = None
    times_seen: int = 1
    created_at: Optional[datetime] = None
