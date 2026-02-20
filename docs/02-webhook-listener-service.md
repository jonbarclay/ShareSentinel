# 02 - Webhook Listener Service

## Purpose

The webhook listener is a lightweight FastAPI service that receives HTTP POST requests from Splunk, validates the payload, checks for duplicate events, and pushes valid jobs onto the Redis queue for processing by the worker.

## API Endpoint

### POST /webhook/splunk

Receives Splunk webhook alerts containing OneDrive/SharePoint sharing event data.

**Request Headers:**
- `Content-Type: application/json`
- `Authorization: Bearer <shared_secret>` (optional but recommended; a shared secret configured in both Splunk and the listener to prevent unauthorized submissions)

**Response:**
- `200 OK` with `{"status": "queued", "event_id": "<id>"}` on success
- `200 OK` with `{"status": "duplicate", "event_id": "<id>"}` if the event was already processed (deduplication hit)
- `400 Bad Request` with `{"error": "<description>"}` if the payload is malformed
- `401 Unauthorized` if the authorization header is missing or invalid
- `500 Internal Server Error` if Redis is unavailable

**Important**: Always return 200 for valid payloads (even duplicates). Splunk will retry on non-2xx responses, which could cause duplicate processing. Only return 4xx/5xx for genuinely invalid requests or system failures.

### GET /health

Health check endpoint.

**Response:**
- `200 OK` with `{"status": "healthy", "redis_connected": true}` if the service is operational
- `503 Service Unavailable` with `{"status": "unhealthy", "redis_connected": false}` if Redis is unreachable

## Splunk Webhook Payload

Splunk webhook alert payloads vary based on how the alert is configured. The alert will be configured to forward the relevant fields from the Azure OneDrive/SharePoint sharing audit logs. The expected payload structure is as follows (this may need to be adjusted based on the actual Splunk alert configuration):

```json
{
  "result": {
    "Operation": "AnonymousLinkCreated",
    "Workload": "OneDrive",
    "UserId": "user@organization.com",
    "ObjectId": "https://organization-my.sharepoint.com/personal/user_organization_com/Documents/sensitive-file.pdf",
    "SiteUrl": "https://organization-my.sharepoint.com/personal/user_organization_com/",
    "SourceFileName": "sensitive-file.pdf",
    "SourceRelativeUrl": "personal/user_organization_com/Documents",
    "ItemType": "File",
    "EventSource": "SharePoint",
    "CreationTime": "2024-01-15T10:30:00Z",
    "SharingType": "Anonymous",
    "SharingScope": "Anyone",
    "SharingPermission": "View"
  }
}
```

**Key fields for processing:**

| Field | Description | Usage |
|-------|-------------|-------|
| `Operation` | The sharing operation type (AnonymousLinkCreated, CompanySharingLinkCreated, etc.) | Determines the type of sharing event |
| `Workload` | "OneDrive" or "SharePoint" | Context for Graph API calls |
| `UserId` | The UPN of the user who created the sharing link | Included in analyst notifications |
| `ObjectId` | The full URL to the shared item | Used to construct Graph API requests |
| `SiteUrl` | The SharePoint site or OneDrive URL | Used to identify the drive for Graph API calls |
| `SourceFileName` | The filename of the shared item | Used for filename pre-screening and display |
| `SourceRelativeUrl` | The relative path within the site | Used for path-based analysis |
| `ItemType` | "File" or "Folder" | Determines processing path (file analysis vs. folder flagging) |
| `CreationTime` | When the sharing link was created | Audit trail |
| `SharingType` | "Anonymous" or "Company" | Context for risk assessment |
| `SharingScope` | The scope of sharing | Context for risk assessment |
| `SharingPermission` | "View" or "Edit" | Edit permissions are higher risk |

**Note on payload flexibility**: Splunk webhook payloads can be structured differently depending on the alert configuration. The listener should be tolerant of additional fields and should only require the fields it actually needs. Use Pydantic models with `extra = "allow"` to handle unexpected fields gracefully. The exact field mapping may need adjustment during initial deployment based on how the Splunk alerts are configured. Build the Pydantic model to accept the known fields but also allow the raw payload to be stored for debugging.

## Pydantic Models

```python
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime

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

    class Config:
        extra = "allow"  # Accept additional fields from Splunk

class SplunkWebhookPayload(BaseModel):
    """Top-level Splunk webhook payload."""
    result: SharingEventResult

    class Config:
        extra = "allow"

class QueueJob(BaseModel):
    """The normalized job object pushed to the Redis queue."""
    event_id: str           # Unique hash for deduplication
    operation: str          # The sharing operation type
    workload: str           # "OneDrive" or "SharePoint"
    user_id: str            # UPN of the sharing user
    object_id: str          # Full URL to the shared item
    site_url: str           # SharePoint site or OneDrive URL
    file_name: str          # Filename of the shared item
    relative_path: str      # Relative path within the site
    item_type: str          # "File" or "Folder"
    sharing_type: str       # "Anonymous" or "Company"
    sharing_scope: str      # Scope of sharing
    sharing_permission: str # "View" or "Edit"
    event_time: str         # When the sharing event occurred
    received_at: str        # When the webhook was received (ISO format)
    raw_payload: dict       # The complete original payload for debugging
```

## Validation Logic

The validation module should check:

1. **Required fields present**: `Operation`, `UserId`, `ObjectId`, and `ItemType` are required. If any are missing, return 400.
2. **Operation is a sharing operation**: Validate that the `Operation` field matches known sharing operations. Expected values include:
   - `AnonymousLinkCreated`
   - `AnonymousLinkUsed` (may want to skip these; they indicate link usage, not creation)
   - `CompanySharingLinkCreated`
   - `SharingLinkCreated`
   - `AddedToSharingLink`
   - Other sharing-related operations
   
   For the MVP, accept all Operation values and let the worker decide how to handle them. Log a warning for unrecognized operations.
3. **ItemType is recognized**: Should be "File" or "Folder". Log a warning if it's something else but still process it.
4. **ObjectId is a valid URL**: Basic URL format validation.

## Deduplication Logic

Deduplication prevents the same sharing event from being processed multiple times. This can happen if Splunk retries a webhook, if duplicate log entries exist, or if multiple sharing actions fire for the same item in quick succession.

**Deduplication key**: Generate a SHA-256 hash of the concatenation of `ObjectId + Operation + CreationTime + UserId`. This uniquely identifies a specific sharing event.

**Redis implementation**:
- Use a Redis SET with a TTL.
- Before enqueuing a job, check if the dedup key exists: `SISMEMBER sharesentinel:dedup <hash>`.
- If it exists, the event is a duplicate; return 200 with status "duplicate".
- If it does not exist, add the key with a TTL: `SADD sharesentinel:dedup <hash>` followed by `EXPIRE sharesentinel:dedup:member:<hash> 86400` (24-hour TTL).

**Alternative simpler approach**: Use `SET sharesentinel:dedup:<hash> 1 EX 86400 NX`. This atomically sets the key only if it doesn't exist, with a 24-hour TTL. If the SET returns `True`, the event is new; proceed to enqueue. If it returns `None`, the event is a duplicate.

The 24-hour TTL means the system will re-process an event if the same sharing action somehow fires again after 24 hours, which is acceptable. Adjust the TTL as needed.

## Queue Integration

**Redis queue**: Use a Redis list named `sharesentinel:jobs`.

**Enqueue operation**: Serialize the `QueueJob` model to JSON and push it to the list: `RPUSH sharesentinel:jobs <json_payload>`.

The worker consumes from the left side: `BLPOP sharesentinel:jobs 0` (blocking pop with infinite timeout).

This gives FIFO ordering (first in, first out).

## Configuration

The webhook listener reads configuration from environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `WEBHOOK_PORT` | Port to listen on | `8000` |
| `WEBHOOK_AUTH_SECRET` | Shared secret for webhook authentication | (none; auth disabled if not set) |
| `REDIS_URL` | Redis connection URL | `redis://redis:6379/0` |
| `DEDUP_TTL_SECONDS` | How long to remember seen events | `86400` (24 hours) |
| `LOG_LEVEL` | Logging level | `INFO` |

## Logging

The webhook listener logs the following events:

- **INFO**: Webhook received (event_id, operation, user_id, file_name, item_type)
- **INFO**: Job enqueued (event_id)
- **INFO**: Duplicate detected (event_id)
- **WARNING**: Validation failed (reason, partial payload info)
- **WARNING**: Unrecognized operation type (operation value)
- **ERROR**: Redis connection failure
- **ERROR**: Unexpected exception during processing

Log format should be structured JSON for easy parsing:

```json
{
  "timestamp": "2024-01-15T10:30:00Z",
  "level": "INFO",
  "service": "webhook-listener",
  "event": "job_enqueued",
  "event_id": "abc123",
  "operation": "AnonymousLinkCreated",
  "user_id": "user@org.com",
  "file_name": "document.pdf",
  "item_type": "File"
}
```

Never log the full ObjectId URL or file content. The ObjectId could contain sensitive path information. Log the filename and event metadata only.

## Implementation Notes

- Use `uvicorn` as the ASGI server with a single worker process (sufficient for < 100 requests/day).
- The FastAPI app should have a startup event that tests the Redis connection and logs the result.
- The FastAPI app should have a shutdown event that cleanly closes the Redis connection.
- Use `redis.asyncio` (async Redis client) since FastAPI is async-native. This prevents blocking the event loop during Redis operations.
- The `/webhook/splunk` endpoint should be an async function.
- Add request logging middleware that logs the request method, path, and response status code for every request.
- Consider adding a simple rate limiter (e.g., max 10 requests per second) as a safety valve against misconfigured Splunk alerts flooding the system. This can be a simple in-memory counter; no need for distributed rate limiting at this volume.
