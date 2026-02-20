"""Jira notification channel - creates tickets via the Jira REST API v3."""

import logging
from typing import Any, Dict, List

import httpx

from .base_notifier import AlertPayload, BaseNotifier

logger = logging.getLogger(__name__)


class JiraNotifier(BaseNotifier):
    """Creates Jira tickets for analyst alerts using the REST API v3."""

    def __init__(
        self,
        jira_url: str,
        jira_email: str,
        jira_api_token: str,
        project_key: str,
        issue_type: str = "Task",
    ):
        self.jira_url = jira_url.rstrip("/")
        self.jira_email = jira_email
        self.jira_api_token = jira_api_token
        self.project_key = project_key
        self.issue_type = issue_type

    # ------------------------------------------------------------------
    # Priority mapping
    # ------------------------------------------------------------------

    @staticmethod
    def _map_priority(payload: AlertPayload) -> str:
        """Map alert details to a Jira priority name."""
        if payload.alert_type == "high_sensitivity_file":
            if payload.escalation_tier == "tier_1":
                return "Highest"
            return "High"
        elif payload.alert_type == "folder_share":
            return "Medium"
        elif payload.alert_type == "processing_failure":
            return "Low"
        return "Medium"

    # ------------------------------------------------------------------
    # Labels
    # ------------------------------------------------------------------

    @staticmethod
    def _build_labels(payload: AlertPayload) -> List[str]:
        """Build the list of Jira labels for the issue."""
        labels = ["sharesentinel", "dlp"]
        if payload.escalation_tier:
            labels.append(payload.escalation_tier)
        for cat_id in payload.category_ids:
            labels.append(cat_id)
        return labels

    # ------------------------------------------------------------------
    # Summary (subject line)
    # ------------------------------------------------------------------

    @staticmethod
    def _build_summary(payload: AlertPayload) -> str:
        """Build the Jira issue summary (title)."""
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
        elif payload.alert_type == "processing_failure":
            return f"[ShareSentinel] Processing Failed - {payload.file_name}"
        return f"[ShareSentinel] Alert - {payload.file_name}"

    # ------------------------------------------------------------------
    # ADF description
    # ------------------------------------------------------------------

    @staticmethod
    def _build_description_adf(payload: AlertPayload) -> Dict[str, Any]:
        """Build an Atlassian Document Format (ADF) description for the issue."""

        def _heading(text: str, level: int = 3) -> Dict[str, Any]:
            return {
                "type": "heading",
                "attrs": {"level": level},
                "content": [{"type": "text", "text": text}],
            }

        def _paragraph(*parts: Dict[str, Any]) -> Dict[str, Any]:
            return {"type": "paragraph", "content": list(parts)}

        def _text(value: str, bold: bool = False) -> Dict[str, Any]:
            node: Dict[str, Any] = {"type": "text", "text": value}
            if bold:
                node["marks"] = [{"type": "strong"}]
            return node

        def _rule() -> Dict[str, Any]:
            return {"type": "rule"}

        content: List[Dict[str, Any]] = []

        # -- File details --
        content.append(_heading("File Details"))
        content.append(
            _paragraph(
                _text("Name: ", bold=True), _text(payload.file_name),
            )
        )
        content.append(
            _paragraph(
                _text("Path: ", bold=True), _text(payload.file_path),
            )
        )
        content.append(
            _paragraph(
                _text("Size: ", bold=True), _text(payload.file_size_human),
            )
        )
        content.append(
            _paragraph(
                _text("Type: ", bold=True), _text(payload.item_type),
            )
        )

        content.append(_rule())

        # -- Sharing details --
        content.append(_heading("Sharing Details"))
        content.append(
            _paragraph(
                _text("Shared by: ", bold=True), _text(payload.sharing_user),
            )
        )
        content.append(
            _paragraph(
                _text("Sharing type: ", bold=True), _text(payload.sharing_type),
            )
        )
        content.append(
            _paragraph(
                _text("Permission: ", bold=True), _text(payload.sharing_permission),
            )
        )
        content.append(
            _paragraph(
                _text("Time: ", bold=True), _text(payload.event_time),
            )
        )
        if payload.sharing_link_url:
            content.append(
                _paragraph(
                    _text("Link: ", bold=True), _text(payload.sharing_link_url),
                )
            )

        content.append(_rule())

        # -- AI analysis results (file alerts only) --
        if payload.alert_type == "high_sensitivity_file" and payload.categories:
            from ..ai.base_provider import CATEGORY_LABELS
            content.append(_heading("AI Analysis Results"))
            content.append(
                _paragraph(
                    _text("Escalation tier: ", bold=True),
                    _text(payload.escalation_tier or "none"),
                )
            )
            content.append(
                _paragraph(
                    _text("Context: ", bold=True),
                    _text(payload.context or "unknown"),
                )
            )
            for cat in payload.categories:
                label = CATEGORY_LABELS.get(cat.id, cat.id)
                cat_line = f"{label} (confidence: {cat.confidence})"
                if cat.evidence:
                    cat_line += f" — {cat.evidence}"
                content.append(_paragraph(_text("• ", bold=True), _text(cat_line)))
            if payload.summary:
                content.append(
                    _paragraph(
                        _text("Summary: ", bold=True), _text(payload.summary),
                    )
                )
            if payload.recommendation:
                content.append(
                    _paragraph(
                        _text("Recommendation: ", bold=True),
                        _text(payload.recommendation),
                    )
                )
            if payload.analysis_mode:
                content.append(
                    _paragraph(
                        _text("Analysis mode: ", bold=True),
                        _text(payload.analysis_mode),
                    )
                )
            if payload.was_sampled and payload.sampling_description:
                content.append(
                    _paragraph(
                        _text("Note: ", bold=True),
                        _text(payload.sampling_description),
                    )
                )
            content.append(_rule())

        # -- Failure reason --
        if payload.alert_type == "processing_failure" and payload.failure_reason:
            content.append(_heading("Failure Reason"))
            content.append(_paragraph(_text(payload.failure_reason)))
            content.append(_rule())

        # -- Footer --
        content.append(
            _paragraph(
                _text(f"Event ID: {payload.event_id}", bold=False),
            )
        )

        return {
            "version": 1,
            "type": "doc",
            "content": content,
        }

    # ------------------------------------------------------------------
    # Issue builder
    # ------------------------------------------------------------------

    def _build_issue(self, payload: AlertPayload) -> Dict[str, Any]:
        """Build the JSON body for the Jira create-issue API call."""
        return {
            "fields": {
                "project": {"key": self.project_key},
                "summary": self._build_summary(payload),
                "issuetype": {"name": self.issue_type},
                "priority": {"name": self._map_priority(payload)},
                "labels": self._build_labels(payload),
                "description": self._build_description_adf(payload),
            }
        }

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def send_alert(self, payload: AlertPayload) -> bool:
        """Create a Jira ticket for the alert. Returns True on success."""
        try:
            issue_data = self._build_issue(payload)
            url = f"{self.jira_url}/rest/api/3/issue"

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    url,
                    json=issue_data,
                    auth=(self.jira_email, self.jira_api_token),
                    headers={"Content-Type": "application/json"},
                    timeout=30.0,
                )
                response.raise_for_status()

                ticket_key = response.json().get("key", "UNKNOWN")
                logger.info(
                    "Created Jira ticket %s for event %s",
                    ticket_key,
                    payload.event_id,
                )
                return True

        except httpx.HTTPStatusError as exc:
            from ..utils.log_sanitizer import sanitize_response_body
            logger.error(
                "Jira API returned %s for event %s: %s",
                exc.response.status_code,
                payload.event_id,
                sanitize_response_body(exc.response.text),
            )
            return False
        except Exception:
            logger.exception(
                "Failed to create Jira ticket for event %s", payload.event_id
            )
            return False

    def get_channel_name(self) -> str:
        return "jira"
