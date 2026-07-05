"""Policy enforcement for provisioning audit events.

When the audit poller ingests a site/group/user creation event (gated by
``AUDIT_POLL_PROVISIONING_ENABLED``), the record is routed here instead of the
worker's file-analysis queue. New sites and OneDrives are born with
``DisableCompanyWideSharingLinks = NotSpecified`` (CWS links allowed) and the
tenant-default ``SharingCapability``, and new M365 groups can be created
Public — so without this path a newly provisioned object sits out of policy
until the next scheduled sweep (daily for site policy, weekly for CWS).

Handled operations:

* ``SiteCollectionCreated`` (recordType sharePoint) — covers both new
  SharePoint site collections AND OneDrive provisioning (the personal site is
  created on the user's first OneDrive access). Pushes ``disable_sharing``
  and/or ``cws_disallow_site`` single-site actions onto the existing
  lifecycle-cron action queues unless the site is allow-listed / the OneDrive
  owner is in the CWS exception group.
* ``Add group.`` (recordType azureActiveDirectory) — looks the group up via
  Graph; if it is a Unified group created Public and not on the visibility
  allow list, pushes a ``set_private`` action (whose consumer also cascades
  the sharing + CWS policies onto the group's site).
* ``Add user.`` (recordType azureActiveDirectory) — logged only. A user's
  OneDrive does not exist at account creation; it is provisioned on first
  access, which emits the ``SiteCollectionCreated`` event we act on above.

Everything here is best-effort: failures are logged and swallowed so the
audit poll cycle is never broken by a single bad record.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlparse, urlunparse

from .allowlist_enforcer import _is_unsupported_site_type
from .config import LifecycleConfig
from .graph_api import GraphAuth, get_group, get_group_site_url

logger = logging.getLogger(__name__)

# Single-site remediation action queues drained by the lifecycle-cron loops
# (same queues the dashboard and the worker's event drift detector push to).
SITE_POLICY_ACTION_QUEUE = "sharesentinel:site_policy_action"
CWS_ACTION_QUEUE = "sharesentinel:company_wide_sharing_action"

# Exception group membership mirror maintained by cws_exception_group_sync_loop.
EXCEPTION_UPN_CACHE_KEY = "sharesentinel:cws_exception_upns"

# Per-target dedup. The site prefixes are shared with the worker's
# cws_drift_detector so a sharing event and a provisioning event for the same
# site within the TTL produce a single remediation.
CWS_IMMEDIATE_SEEN_PREFIX = "sharesentinel:cws_immediate_seen:"
ANON_IMMEDIATE_SEEN_PREFIX = "sharesentinel:anon_immediate_seen:"
VISIBILITY_IMMEDIATE_SEEN_PREFIX = "sharesentinel:visibility_immediate_seen:"
IMMEDIATE_SEEN_TTL_S = 3600

# Audit operations (lowercased for matching). Entra ID directory operations
# historically carry a trailing period ("Add group."); match both forms.
SITE_CREATED_OPERATIONS = frozenset({"sitecollectioncreated"})
GROUP_ADDED_OPERATIONS = frozenset({"add group.", "add group"})
USER_ADDED_OPERATIONS = frozenset({"add user.", "add user"})
PROVISIONING_OPERATIONS = (
    SITE_CREATED_OPERATIONS | GROUP_ADDED_OPERATIONS | USER_ADDED_OPERATIONS
)

# Operation / record-type filters the poller adds to the audit query when
# provisioning ingestion is enabled.
PROVISIONING_QUERY_OPERATIONS = ["SiteCollectionCreated", "Add group.", "Add user."]
PROVISIONING_QUERY_RECORD_TYPES = ["sharePoint", "azureActiveDirectory"]

_MANAGED_PATHS = ("sites", "teams", "personal")
_GUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)


def _site_collection_root(site_url: str) -> str:
    """Return the site-collection root URL for any SharePoint/OneDrive URL
    (mirrors the worker's cws_drift_detector helper)."""
    try:
        parsed = urlparse(site_url.rstrip("/"))
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0].lower() in _MANAGED_PATHS:
            new_path = f"/{parts[0]}/{parts[1]}"
            return urlunparse((parsed.scheme, parsed.netloc, new_path, "", "", ""))
        return site_url.rstrip("/")
    except Exception:
        return site_url.rstrip("/")


def _upn_to_personal_segment(upn: str) -> str:
    """Encode a UPN the way SharePoint encodes OneDrive personal-site paths
    (jane.doe@uvu.edu -> jane_doe_uvu_edu). Encoding in this direction is
    unambiguous, unlike trying to reverse a path segment into a UPN."""
    return re.sub(r"[^a-z0-9\-]", "_", upn.strip().lower())


def _extract_group_id(record: dict, audit_data: dict) -> str:
    """Pull the created group's object id out of an Entra ID audit record.

    The id can live in the record's top-level objectId or in the auditData
    Target list (entries may be bare GUIDs or prefixed forms); return the
    first GUID found.
    """
    candidates: list[str] = [str(record.get("objectId") or "")]
    for target in audit_data.get("Target") or []:
        if isinstance(target, dict):
            candidates.append(str(target.get("ID") or ""))
    for c in candidates:
        m = _GUID_RE.search(c)
        if m:
            return m.group(0).lower()
    return ""


class ProvisioningEnforcer:
    """Routes provisioning audit records to single-site/group policy actions."""

    def __init__(
        self,
        auth: GraphAuth,
        redis_client: Any,
        db_pool: Any,
        config: LifecycleConfig,
    ) -> None:
        self._auth = auth
        self._redis = redis_client
        self._db = db_pool
        self._config = config

    async def handle_record(self, record: dict, job: dict) -> str:
        """Process one provisioning audit record. Never raises."""
        op = (record.get("operation") or "").strip().lower()
        try:
            if op in SITE_CREATED_OPERATIONS:
                return await self._handle_site_created(record, job)
            if op in GROUP_ADDED_OPERATIONS:
                return await self._handle_group_added(record)
            if op in USER_ADDED_OPERATIONS:
                # Not actionable yet: the user's OneDrive doesn't exist until
                # first access, which fires SiteCollectionCreated — the event
                # we enforce on. Logged for traceability only.
                logger.info(
                    "Provisioning: user created (%s) — OneDrive policy will be "
                    "applied when its SiteCollectionCreated event arrives",
                    job.get("user_id") or record.get("userPrincipalName", ""),
                )
                return "user_logged"
            return "ignored"
        except Exception:
            logger.exception("Provisioning handler failed for op=%s (non-fatal)", op)
            return "error"

    # -- New site collection (SharePoint site or OneDrive) -------------------

    async def _handle_site_created(self, record: dict, job: dict) -> str:
        site_url = job.get("site_url") or str(record.get("objectId") or "")
        if not site_url.startswith("https://"):
            logger.debug("Provisioning: SiteCollectionCreated without usable URL")
            return "no_url"

        root = _site_collection_root(site_url)
        if _is_unsupported_site_type(root):
            return "unsupported_site_type"
        normalized = root.rstrip("/").lower()
        actor_upn = (job.get("user_id") or "").strip().lower()

        logger.info("Provisioning: new site collection detected: %s", normalized)
        results = []

        # 1. Anonymous sharing capability (site_policy_loop drains the queue).
        if self._config.site_policy_enabled:
            if not await self._is_on_allowlist("site_allowlist", normalized):
                if await self._claim(ANON_IMMEDIATE_SEEN_PREFIX, normalized):
                    await self._redis.rpush(
                        SITE_POLICY_ACTION_QUEUE,
                        json.dumps({
                            "action": "disable_sharing",
                            "site_url": root,
                            "site_display_name": job.get("file_name", "") or "",
                            "triggered_by": "provisioning_event",
                        }),
                    )
                    logger.info(
                        "Provisioning: queued disable_sharing for new site %s",
                        normalized,
                    )
                    results.append("sharing_queued")
        else:
            logger.debug(
                "Provisioning: SITE_POLICY_ENABLED=false — skipping sharing "
                "check for %s", normalized,
            )

        # 2. Company-wide sharing links. New sites default to NotSpecified,
        # which allows CWS links, so every non-exempt new site needs the
        # disallow action (idempotent on the consumer side).
        if self._config.cws_allowlist_action_enabled and self._config.sharepoint_admin_url:
            if not await self._is_on_allowlist(
                "site_company_wide_sharing_allowlist", normalized
            ) and not await self._is_cws_exempt_onedrive(normalized, actor_upn):
                if await self._claim(CWS_IMMEDIATE_SEEN_PREFIX, normalized):
                    await self._redis.rpush(
                        CWS_ACTION_QUEUE,
                        json.dumps({
                            "action": "cws_disallow_site",
                            "site_url": root,
                            "site_display_name": job.get("file_name", "") or "",
                            "triggered_by": "provisioning_event",
                        }),
                    )
                    logger.info(
                        "Provisioning: queued cws_disallow_site for new site %s",
                        normalized,
                    )
                    results.append("cws_queued")

        return ",".join(results) if results else "no_action"

    # -- New Entra ID group ---------------------------------------------------

    async def _handle_group_added(self, record: dict) -> str:
        audit_data = record.get("auditData") or {}
        if isinstance(audit_data, str):
            try:
                audit_data = json.loads(audit_data)
            except json.JSONDecodeError:
                audit_data = {}

        group_id = _extract_group_id(record, audit_data)
        if not group_id:
            logger.debug("Provisioning: Add group event without resolvable group id")
            return "no_group_id"

        group = await get_group(self._auth, group_id)
        if group is None:
            # Audit events usually lag creation by several minutes, so a 404
            # here normally means the group was already deleted; the daily
            # visibility scan remains the safety net either way.
            logger.warning(
                "Provisioning: group %s not found in Graph (deleted or "
                "replication lag) — daily scan will cover it", group_id,
            )
            return "group_not_found"

        if "Unified" not in (group.get("groupTypes") or []):
            return "non_unified_group"
        if (group.get("visibility") or "").lower() != "public":
            return "already_private"

        if not self._config.site_policy_enabled:
            logger.warning(
                "Provisioning: new PUBLIC group %s (%s) detected but "
                "SITE_POLICY_ENABLED=false — no enforcement queued",
                group_id, group.get("displayName", ""),
            )
            return "site_policy_disabled"

        async with self._db.acquire() as conn:
            allowed = await conn.fetchval(
                "SELECT 1 FROM site_visibility_allowlist "
                "WHERE LOWER(group_id) = LOWER($1) LIMIT 1",
                group_id,
            )
        if allowed:
            return "allowlisted"

        if not await self._claim(VISIBILITY_IMMEDIATE_SEEN_PREFIX, group_id):
            return "deduped"

        site_url = await get_group_site_url(self._auth, group_id)
        await self._redis.rpush(
            SITE_POLICY_ACTION_QUEUE,
            json.dumps({
                "action": "set_private",
                "group_id": group_id,
                "group_display_name": group.get("displayName", ""),
                "site_url": site_url,
                "triggered_by": "provisioning_event",
            }),
        )
        logger.info(
            "Provisioning: queued set_private for new public group %s (%s)",
            group_id, group.get("displayName", ""),
        )
        return "visibility_queued"

    # -- Shared helpers --------------------------------------------------------

    async def _is_on_allowlist(self, table: str, normalized_url: str) -> bool:
        # Table name comes from the two literal call sites above, never from
        # input.
        async with self._db.acquire() as conn:
            row = await conn.fetchval(
                f"SELECT 1 FROM {table} WHERE LOWER(RTRIM(site_url, '/')) = $1 LIMIT 1",
                normalized_url,
            )
        return bool(row)

    async def _is_cws_exempt_onedrive(
        self, normalized_url: str, actor_upn: str
    ) -> bool:
        """True if this is a OneDrive whose owner is in the CWS exception group.

        The owner is matched by encoding each exception member's UPN into its
        personal-site path segment (unambiguous direction); the actor UPN gets
        a cheap SISMEMBER fast-path since OneDrive provisioning is usually
        triggered by the owner themselves.
        """
        if "/personal/" not in normalized_url:
            return False
        try:
            if actor_upn and await self._redis.sismember(
                EXCEPTION_UPN_CACHE_KEY, actor_upn
            ):
                return True
            segment = normalized_url.rsplit("/personal/", 1)[1].split("/")[0]
            members = await self._redis.smembers(EXCEPTION_UPN_CACHE_KEY)
            for member in members:
                upn = member.decode() if isinstance(member, bytes) else str(member)
                if _upn_to_personal_segment(upn) == segment:
                    return True
        except Exception:
            logger.debug(
                "Provisioning: exception-group check failed (treating as "
                "non-exempt)", exc_info=True,
            )
        return False

    async def _claim(self, prefix: str, key: str) -> bool:
        """Best-effort single-flight per target. Fails open so a Redis blip
        never drops a remediation (consumers are idempotent)."""
        try:
            won = await self._redis.set(
                f"{prefix}{key}", "1", nx=True, ex=IMMEDIATE_SEEN_TTL_S,
            )
            return bool(won)
        except Exception:
            return True
