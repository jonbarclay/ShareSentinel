# 02 - Event Ingestion: Audit Log Poller

## Purpose

ShareSentinel pulls sharing events directly from the Microsoft Graph Audit Log Query API on a configurable schedule (default: every 15 minutes). This provides a direct integration with Microsoft 365 audit logs without any intermediary dependencies.

The poller runs as a concurrent loop inside the `lifecycle-cron` container alongside the sharing link lifecycle processor.

## Microsoft Graph Audit Log Query API

**API**: Microsoft Graph beta endpoint (async query model)

**Permission required**: `AuditLogsQuery.Read.All` (application permission with admin consent)

### How the API Works

1. **POST** to `/beta/security/auditLog/queries` with operation filters, record type filters, and a date range.
2. The response returns a query ID with `status: "notStarted"`.
3. **Poll** `GET /beta/security/auditLog/queries/{id}` every 10 seconds until `status: "succeeded"` (typically takes 3–6 minutes).
4. **GET** `/beta/security/auditLog/queries/{id}/records` — paginated results (150 per page, follow `@odata.nextLink`).
5. Each record contains: `operation`, `userPrincipalName`, `objectId`, `service`, `createdDateTime`, and `auditData` (a JSON blob with the full unified audit log event).

### Operations Captured

| Operation | Description |
|-|-|
| `AnonymousLinkCreated` | Anyone-with-the-link sharing |
| `CompanyLinkCreated` | Organization-wide sharing link |

Record type filter: `["sharePointSharingOperation"]`

These two operations capture all anonymous and org-wide sharing link creation events in OneDrive and SharePoint.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  lifecycle-cron container                                 │
│                                                          │
│  ┌────────────────────┐   ┌─────────────────────────┐   │
│  │ Lifecycle Processor│   │ Audit Log Poller         │   │
│  │ (daily)            │   │ (every 15 min)           │   │
│  │                    │   │                          │   │
│  │ - Check milestones │   │ 1. Read last_poll_time   │   │
│  │ - Send countdown   │   │ 2. POST audit query      │   │
│  │   notifications    │   │ 3. Poll for completion   │   │
│  │ - Remove expired   │   │ 4. Fetch all records     │   │
│  │   sharing links    │   │ 5. Dedup (Redis SET NX)  │   │
│  └────────────────────┘   │ 6. RPUSH to Redis queue  │   │
│                           │ 7. Save last_poll_time   │   │
│                           └─────────────────────────┘   │
│                                       │                  │
│                                       ▼                  │
│                                   ┌───────┐             │
│                                   │ Redis │             │
│                                   │ Queue │             │
│                                   └───┬───┘             │
└───────────────────────────────────────┼──────────────────┘
                                        │
                                        ▼
                               ┌─────────────────┐
                               │ Worker (consumes │
                               │ from queue)      │
                               └─────────────────┘
```

Up to four loops run concurrently via `asyncio.gather()` in the lifecycle-cron `main.py`: the lifecycle processor, audit log poller, site policy scanner (if `SITE_POLICY_ENABLED=true`), and folder rescan (if `FOLDER_RESCAN_ENABLED=true`).

## Audit Record → Queue Job Mapping

Each audit record is transformed into a queue job dict that matches the format the worker expects:

| Audit record field | Queue job field | Source |
|-|-|-|
| `operation` | `operation` | top-level |
| `userPrincipalName` / `userId` | `user_id` | top-level |
| `objectId` | `object_id` | top-level |
| `service` | `workload` | top-level |
| `createdDateTime` | `event_time` | top-level |
| `auditData.SiteUrl` | `site_url` | auditData JSON |
| `auditData.SourceFileName` | `file_name` | auditData JSON |
| `auditData.SourceRelativeUrl` | `relative_path` | auditData JSON |
| `auditData.ItemType` | `item_type` | auditData JSON |
| `auditData.EventData` | `sharing_type` / `sharing_scope` / `sharing_permission` | auditData JSON (parsed) |
| SHA-256 hash | `event_id` | computed: `SHA256(objectId + operation + createdDateTime + userId)` |
| full audit record | `raw_payload` | entire record dict |

## Deduplication

Uses SHA-256-based deduplication:

- **Dedup key**: `SHA256(ObjectId + Operation + CreationTime + UserId)`
- **Redis implementation**: `SET sharesentinel:dedup:<hash> 1 EX 86400 NX` — atomically sets only if not present, with 24-hour TTL.
- **Overlap window**: Each poll cycle queries from `last_poll_time - 5 minutes` to avoid missing events at boundaries. Dedup handles any resulting duplicates.

## Polling State

A single-row `audit_poll_state` table in PostgreSQL tracks the poller's progress:

```sql
CREATE TABLE IF NOT EXISTS audit_poll_state (
    id INTEGER PRIMARY KEY DEFAULT 1,
    last_poll_time TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    last_poll_status TEXT DEFAULT 'success',
    events_found INTEGER DEFAULT 0,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    CONSTRAINT single_row CHECK (id = 1)
);
```

On first run, defaults to `NOW() - 1 hour`, so the first poll picks up the most recent hour of events.

## Configuration

| Variable | Description | Default |
|-|-|-|
| `AUDIT_POLL_ENABLED` | Enable/disable the audit log poller | `true` |
| `AUDIT_POLL_INTERVAL_MINUTES` | Minutes between poll cycles | `15` |
| `AUDIT_POLL_OPERATIONS` | Comma-separated operation names to query | `AnonymousLinkCreated,CompanyLinkCreated` |
| `REDIS_URL` | Redis connection URL (required for poller) | `redis://redis:6379/0` |

## Error Handling

- **Query timeout**: If the audit query doesn't complete within 10 minutes, a `TimeoutError` is logged and the cycle retries next interval (last_poll_time is NOT updated, so no events are missed).
- **Query failure**: If the query status is `"failed"`, a `RuntimeError` is logged and the cycle retries.
- **Transient HTTP errors**: 502, 503, 504, and 429 responses during status polling are retried automatically with 10-second intervals.
- **Token refresh**: The Graph API access token is refreshed between long waits and between pagination pages.
- **Empty results**: Normal — the poller updates last_poll_time and moves on.

## Key Implementation Files

| File | Description |
|-|-|
| `services/lifecycle-cron/app/audit_poller.py` | `AuditLogPoller` class with all polling logic |
| `services/lifecycle-cron/app/config.py` | `LifecycleConfig` with audit poll settings |
| `services/lifecycle-cron/app/main.py` | Entry point running both lifecycle and audit poll loops |
| `services/worker/app/database/migrations/012_audit_poll_state.sql` | Migration for polling state table |

