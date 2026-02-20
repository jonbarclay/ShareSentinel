"""Payload validation for Splunk webhook events."""

import logging
from urllib.parse import urlparse

from app.models import SharingEventResult

logger = logging.getLogger("webhook-listener")

KNOWN_SHARING_OPERATIONS = {
    "AnonymousLinkCreated",
    "AnonymousLinkUsed",
    "CompanySharingLinkCreated",
    "SharingLinkCreated",
    "AddedToSharingLink",
}

KNOWN_ITEM_TYPES = {"File", "Folder"}


class ValidationError(Exception):
    """Raised when payload validation fails."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def validate_payload(result: SharingEventResult) -> list[str]:
    """Validate a SharingEventResult and return a list of warnings.

    Raises ValidationError for hard failures (missing required fields,
    invalid ObjectId URL format). Returns a list of warning strings for
    soft issues (unrecognized Operation or ItemType).
    """
    warnings: list[str] = []

    # Required fields — Pydantic enforces presence, but check for empty strings
    if not result.Operation or not result.Operation.strip():
        raise ValidationError("Missing or empty required field: Operation")
    if not result.UserId or not result.UserId.strip():
        raise ValidationError("Missing or empty required field: UserId")
    if not result.ObjectId or not result.ObjectId.strip():
        raise ValidationError("Missing or empty required field: ObjectId")
    if not result.ItemType or not result.ItemType.strip():
        raise ValidationError("Missing or empty required field: ItemType")

    # ObjectId URL validation
    try:
        parsed = urlparse(result.ObjectId)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValidationError(
                f"ObjectId is not a valid URL: {result.ObjectId[:80]}"
            )
    except Exception as exc:
        if isinstance(exc, ValidationError):
            raise
        raise ValidationError(
            f"ObjectId is not a valid URL: {result.ObjectId[:80]}"
        ) from exc

    # Soft checks — warn but don't reject
    if result.Operation not in KNOWN_SHARING_OPERATIONS:
        msg = f"Unrecognized sharing operation: {result.Operation}"
        logger.warning(msg, extra={"operation": result.Operation})
        warnings.append(msg)

    if result.ItemType not in KNOWN_ITEM_TYPES:
        msg = f"Unrecognized item type: {result.ItemType}"
        logger.warning(msg, extra={"item_type": result.ItemType})
        warnings.append(msg)

    return warnings
