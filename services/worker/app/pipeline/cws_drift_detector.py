"""Event-driven CWS drift detection.

Runs as a fast side-effect when the worker processes an org-wide link event
(``CompanyLinkCreated`` from the live poller, or ``CompanySharingLinkCreated``
from the sharing-scan backfill). If the sharing happened on a site that is NOT on the CWS
allowlist AND the actor's UPN is NOT in the CWS exception group, record a
``trigger_source='audit_event'`` row in ``cws_verification_events`` so the
dashboard and operators see the drift in near-real-time.

This is the "third strap" in the belt-and-suspenders CWS verification design —
it catches drift on any site that ever fires an org-wide link event, even if
the scheduled sweep hasn't yet visited that site.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)

EXCEPTION_UPN_CACHE_KEY = "sharesentinel:cws_exception_upns"

# Audit operations that represent an org-wide ("people in your organization")
# sharing link. The live Graph Audit Log Query API emits ``CompanyLinkCreated``;
# the sharing-scan backfill (scripts/backfill_from_sharing_scan.py) labels the
# same link class ``CompanySharingLinkCreated``. Both must trigger CWS drift
# detection, so match either name (case-insensitive).
COMPANY_LINK_OPERATIONS = frozenset({
    "companylinkcreated",
    "companysharinglinkcreated",
})

# Single-site remediation action queues drained by the lifecycle-cron loops.
# These mirror the messages the dashboard pushes on allowlist add/remove.
CWS_ACTION_QUEUE = "sharesentinel:company_wide_sharing_action"
SITE_POLICY_ACTION_QUEUE = "sharesentinel:site_policy_action"

# Per-site dedup so a burst of share-link events for the same site doesn't
# flood the action queues. Short TTL: the consumer remediates in seconds and a
# repeat within the window would just record already_compliant.
CWS_IMMEDIATE_SEEN_PREFIX = "sharesentinel:cws_immediate_seen:"
ANON_IMMEDIATE_SEEN_PREFIX = "sharesentinel:anon_immediate_seen:"
IMMEDIATE_SEEN_TTL_S = 3600


async def _claim_immediate(redis: Any, prefix: str, norm_url: str) -> bool:
    """Best-effort single-flight per site within IMMEDIATE_SEEN_TTL_S.

    Returns True if this caller won the claim (and should push the action),
    False if another recent event already claimed it. Fails open (True) if
    Redis is unavailable so we never silently drop a remediation.
    """
    try:
        won = await redis.set(
            f"{prefix}{norm_url}", "1", nx=True, ex=IMMEDIATE_SEEN_TTL_S,
        )
        return bool(won)
    except Exception:
        logger.debug("immediate dedup claim failed (fail-open)", exc_info=True)
        return True

# SharePoint managed paths: a valid site-collection URL has exactly one path
# segment after the managed path. Audit events sometimes populate SiteUrl with
# a subsite/folder/library path (e.g. /sites/Foo/Marketing); Set-PnPTenantSite
# only accepts the site-collection root, so we trim everything past the second
# segment before recording drift.
_MANAGED_PATHS = ("sites", "teams", "personal")


def _is_unsupported_site_type(site_url: str) -> bool:
    """Return True for site classes that can't be managed via
    Set-PnPTenantSite -DisableCompanyWideSharingLinks.

    * /contentstorage/* — SharePoint Embedded containers (Loop, Whiteboard,
        Designer, Teams private channels). PnP returns
        "The requested operation is not supported for site".
    * *-public.sharepoint.com — legacy SharePoint Online Public Website,
        deprecated by Microsoft in 2018. PnP returns
        "Attempted to perform an unauthorized operation".
    * /sites/contenttypehub — Content Type Hub system site. PnP returns
        "The requested operation is not supported for site".
    * /portals/* — PointPublishing site collections ("is a PointPublishing
        site collection. The only valid parameters...").
    * MySite host root — "The property DisableCompanyWideSharingLinks cannot
        be set on the MySite host." (Personal sites UNDER it are fine.)
    """
    if not site_url:
        return False
    lower = site_url.lower().rstrip("/")
    if "/contentstorage/" in lower:
        return True
    if "/portals/" in lower:
        return True
    if lower.endswith("/sites/contenttypehub"):
        return True
    try:
        parsed = urlparse(lower)
        host = parsed.netloc
        path = parsed.path.rstrip("/")
    except Exception:
        return False
    if host.endswith("-public.sharepoint.com"):
        return True
    if host.endswith("-my.sharepoint.com") and not path:
        return True
    return False


def _site_collection_root(site_url: str) -> str:
    """Return the site-collection root URL for any SharePoint/OneDrive URL.

    Examples:
      /sites/Foo/Marketing/Shared%20Documents  ->  /sites/Foo
      /personal/jane_uvu_edu/Documents/x.docx  ->  /personal/jane_uvu_edu
      /sites/Foo                               ->  /sites/Foo  (unchanged)
      https://host/                            ->  https://host  (root web)
    """
    try:
        parsed = urlparse(site_url.rstrip("/"))
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0].lower() in _MANAGED_PATHS:
            new_path = f"/{parts[0]}/{parts[1]}"
            return urlunparse((parsed.scheme, parsed.netloc, new_path, "", "", ""))
        return site_url.rstrip("/")
    except Exception:
        return site_url.rstrip("/")


async def check_company_link_drift(
    db_pool: Any,
    redis: Any,
    operation: str,
    site_url: str,
    user_upn: str,
    site_display_name: str = "",
    enabled: bool = False,
) -> None:
    """Record a CWS drift event if this CompanyLinkCreated is out of policy.

    Always records a detect-only drift row (the daily drift-remediation drain
    is the safety net). When *enabled*, also files an immediate single-site
    remediation by pushing a ``cws_disallow_site`` action onto the
    lifecycle-cron CWS action queue so the OneDrive/site is fixed within
    seconds rather than waiting for the next drain.

    Silent on any error — this is a best-effort side-effect and must not
    affect the main pipeline.

    Fires for any org-wide link operation (``CompanyLinkCreated`` from the live
    poller or ``CompanySharingLinkCreated`` from the sharing-scan backfill); see
    ``COMPANY_LINK_OPERATIONS``.
    """
    if operation.strip().lower() not in COMPANY_LINK_OPERATIONS:
        return
    if not site_url:
        return

    try:
        site_url = _site_collection_root(site_url)
        if _is_unsupported_site_type(site_url):
            return
        normalized = site_url.lower()
        upn = (user_upn or "").strip().lower()

        async with db_pool.acquire() as conn:
            allowed = await conn.fetchval(
                """
                SELECT 1 FROM site_company_wide_sharing_allowlist
                WHERE LOWER(RTRIM(site_url, '/')) = $1
                LIMIT 1
                """,
                normalized,
            )
        if allowed:
            return

        exempt_user = False
        if upn:
            try:
                exempt_user = bool(await redis.sismember(EXCEPTION_UPN_CACHE_KEY, upn))
            except Exception:
                logger.debug("cws drift: redis sismember failed", exc_info=True)
        if exempt_user:
            return

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO cws_verification_events
                    (scan_id, site_url, user_upn,
                     expected_value, actual_value, drift_type,
                     exempt_reason, trigger_source, error_message)
                VALUES (NULL, $1, $2, 'Disabled', 'NotDisabled', 'drift',
                        '', 'audit_event', NULL)
                """,
                site_url, upn,
            )
        logger.info("CWS drift recorded from audit event site=%s user=%s", normalized, upn)

        # Immediate remediation: push a single-site cws_disallow_site action.
        if enabled and await _claim_immediate(redis, CWS_IMMEDIATE_SEEN_PREFIX, normalized):
            try:
                await redis.rpush(
                    CWS_ACTION_QUEUE,
                    json.dumps({
                        "action": "cws_disallow_site",
                        "site_url": site_url,
                        "site_display_name": site_display_name or "",
                        "triggered_by": "audit_event",
                    }),
                )
                logger.info(
                    "CWS immediate remediation queued (cws_disallow_site) for %s",
                    normalized,
                )
            except Exception:
                logger.debug(
                    "cws drift: failed to queue immediate remediation",
                    exc_info=True,
                )
    except Exception:
        logger.debug("cws drift detector failed (non-fatal)", exc_info=True)


async def check_anonymous_link_drift(
    db_pool: Any,
    redis: Any,
    operation: str,
    site_url: str,
    site_display_name: str = "",
    enabled: bool = False,
) -> None:
    """File an immediate anonymous-sharing remediation if out of policy.

    Fires on ``AnonymousLinkCreated``. If the site/OneDrive is not on the
    sharing allow list (``site_allowlist``), push a ``disable_sharing`` action
    onto the lifecycle-cron site-policy action queue so its SharingCapability is
    downgraded (CSOM) within seconds. Covers both SharePoint sites and OneDrive
    personal sites (the scheduled site-policy scan also covers OneDrive when
    SITE_POLICY_INCLUDE_ONEDRIVE is true, its default).

    Detection-only when *enabled* is False (no-op). Silent on any error.
    """
    # Case-insensitive: the company-link path matches case-insensitively and
    # backfill sources have used differently-cased operation names.
    if operation.strip().lower() != "anonymouslinkcreated":
        return
    if not enabled or not site_url:
        return

    try:
        site_url = _site_collection_root(site_url)
        if _is_unsupported_site_type(site_url):
            return
        normalized = site_url.lower()

        async with db_pool.acquire() as conn:
            allowed = await conn.fetchval(
                """
                SELECT 1 FROM site_allowlist
                WHERE LOWER(RTRIM(site_url, '/')) = $1
                LIMIT 1
                """,
                normalized,
            )
        if allowed:
            return

        if not await _claim_immediate(redis, ANON_IMMEDIATE_SEEN_PREFIX, normalized):
            return

        await redis.rpush(
            SITE_POLICY_ACTION_QUEUE,
            json.dumps({
                "action": "disable_sharing",
                "site_url": site_url,
                "site_display_name": site_display_name or "",
                "triggered_by": "audit_event",
            }),
        )
        logger.info(
            "Anonymous-sharing immediate remediation queued (disable_sharing) for %s",
            normalized,
        )
    except Exception:
        logger.debug("anonymous drift detector failed (non-fatal)", exc_info=True)
