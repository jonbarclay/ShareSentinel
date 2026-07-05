"""Allowlist enforcement helpers — per-site sharing capability management.

Site inventory comes from PnP PowerShell (scripts/Get-SitePolicyInventory.ps1)
because Get-PnPTenantSite pages correctly at tenant scale; the raw CSOM
getSitePropertiesFromSharePointByFilters REST call returns only the first
~1,400 rows with no continuation cursor (verified 2026-07-05: 1,423 of ~235K
sites), which silently exempted the rest of the tenant from the sharing policy.

Per-site reads/writes still use the Office365-REST-Python-Client CSOM API,
since the Graph API does not support per-site sharingCapability management.
All synchronous SPO library calls are wrapped in asyncio.to_thread() for async compat.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .config import LifecycleConfig
from .pwsh import run_pwsh_script

logger = logging.getLogger(__name__)


def _sanitize_error(e: Exception, max_length: int = 500) -> str:
    """Truncate and sanitize exception message for storage."""
    msg = str(e)
    if "Bearer " in msg:
        msg = re.sub(r'Bearer [A-Za-z0-9\-._~+/]+=*', 'Bearer [REDACTED]', msg)
    return msg[:max_length]


def _create_spo_admin_context(config: LifecycleConfig):
    """Create SharePoint Online admin client context.

    Returns a ClientContext connected to the tenant admin site.
    Uses certificate auth (PFX) matching the rest of the project.
    Falls back to client_secret if no certificate is configured.
    """
    import hashlib

    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        pkcs12,
    )
    from office365.sharepoint.client_context import ClientContext

    admin_url = config.sharepoint_admin_url
    if not admin_url:
        raise RuntimeError("SHAREPOINT_ADMIN_URL is not configured")

    if config.azure_certificate_path:
        # Certificate-based auth (PFX → PEM key + thumbprint)
        with open(config.azure_certificate_path, "rb") as f:
            pfx_data = f.read()
        private_key, certificate, _ = pkcs12.load_key_and_certificates(
            pfx_data,
            config.azure_certificate_password.encode()
            if config.azure_certificate_password
            else None,
        )
        pem_key = private_key.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        ).decode()
        thumbprint = hashlib.sha1(
            certificate.public_bytes(Encoding.DER)
        ).hexdigest().upper()

        logger.info(
            "SPO admin: using certificate auth (thumbprint=%s)", thumbprint
        )
        ctx = ClientContext(admin_url).with_client_certificate(
            tenant=config.azure_tenant_id,
            client_id=config.azure_client_id,
            thumbprint=thumbprint,
            private_key=pem_key,
        )
    else:
        # Client secret fallback
        from office365.runtime.auth.client_credential import ClientCredential

        logger.info("SPO admin: using client_secret auth")
        credentials = ClientCredential(
            config.azure_client_id, config.azure_client_secret
        )
        ctx = ClientContext(admin_url).with_credentials(credentials)

    return ctx


@dataclass
class SiteRecord:
    """One site from the tenant inventory (PnP enumeration)."""

    url: str
    title: str
    sharing_capability: int  # SHARING_CAPABILITY_MAP value, or -1 if unknown


async def get_site_inventory(
    config: LifecycleConfig,
    *,
    include_onedrive: bool = False,
    onedrive_only: bool = False,
    log_prefix: str = "site-inventory",
) -> list[SiteRecord]:
    """Enumerate tenant site collections via scripts/Get-SitePolicyInventory.ps1.

    Raises RuntimeError if the script fails or produces no output — callers must
    treat that as a failed scan, never as an empty (compliant) tenant.
    """
    out_path = Path(f"/tmp/site_inventory_{onedrive_only}_{include_onedrive}_{int(time.time())}.jsonl")
    try:
        rc, _stdout, stderr = await run_pwsh_script(
            "Get-SitePolicyInventory.ps1",
            [
                "-OutFile", str(out_path),
                "-IncludeOneDrive", "1" if include_onedrive else "0",
                "-OneDriveOnly", "1" if onedrive_only else "0",
            ],
            log_prefix,
        )
        if rc != 0:
            raise RuntimeError(
                f"Site inventory script failed rc={rc}: {stderr[-300:] if stderr else 'no stderr'}"
            )
        if not out_path.exists():
            raise RuntimeError("Site inventory script produced no output file")

        records: list[SiteRecord] = []
        unknown_capability = 0
        with out_path.open(encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Skipping malformed inventory line: %r", line[:120])
                    continue
                cap_name = str(row.get("sharing_capability") or "")
                cap = SHARING_CAPABILITY_MAP.get(cap_name, -1)
                if cap == -1:
                    unknown_capability += 1
                records.append(
                    SiteRecord(
                        url=str(row.get("url") or ""),
                        title=str(row.get("title") or ""),
                        sharing_capability=cap,
                    )
                )
        if not records:
            raise RuntimeError("Site inventory is empty — refusing to treat as compliant tenant")
        if unknown_capability:
            logger.info(
                "Site inventory: %d/%d sites need per-site capability resolution",
                unknown_capability, len(records),
            )
        return records
    finally:
        try:
            out_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove inventory temp file %s", out_path)


def _is_throttle_error(e: Exception) -> bool:
    msg = str(e)
    return "429" in msg or "throttl" in msg.lower() or "IdleSession" in msg


def _encode_csom_url(site_url: str) -> str:
    """Escape characters that break the tenant-admin OData filter.

    ``get_site_properties_by_url`` interpolates the URL into a ``$filter=``
    query string without encoding; a literal ``&`` in the site URL (e.g.
    /sites/DEPT-BusinessIntelligence&ResearchServices) terminates the query
    parameter and the server rejects the truncated expression. Percent-encode
    the characters the library leaves raw; the server decodes them back before
    comparing against the aggregated site list.
    """
    return site_url.replace("%", "%25").replace("&", "%26").replace("#", "%23").replace("+", "%2B")


def _clear_pending(ctx) -> None:
    """Drop any queued-but-unexecuted CSOM queries from the context.

    A failed execute_query can leave its query (with unresolved identity
    placeholders) on the context's pending queue; the NEXT call on the same
    context then fails with a bogus request like ``items(<key>)`` → 401.
    Observed live 2026-07-05: one bad site poisoned the following site's
    remediation. Always clear after a failure.
    """
    try:
        ctx.clear()
    except Exception:
        logger.debug("ctx.clear() failed", exc_info=True)


def _with_csom_retry(fn, ctx, *args, max_attempts: int = 3):
    """Run a synchronous CSOM call with exponential backoff on throttling.

    The PowerShell scripts back off on 429/throttle/IdleSession; the Python
    CSOM path previously had no retry, so a throttled call silently skipped
    the site. Mirrors that behavior here (15s, 30s waits). Clears the
    context's pending-query queue after any failure so one bad site can't
    poison the next call on the shared context.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            return fn(ctx, *args)
        except Exception as e:
            _clear_pending(ctx)
            if attempt < max_attempts and _is_throttle_error(e):
                wait = min(180, 15 * (2 ** (attempt - 1)))
                logger.warning(
                    "CSOM throttled (attempt %d/%d), sleeping %ds: %s",
                    attempt, max_attempts, wait, str(e)[:120],
                )
                time.sleep(wait)
                continue
            raise


def _get_sharing_capability_by_url_sync(ctx, site_url: str) -> int:
    """Resolve a single site's SharingCapability with a per-site admin call.

    Bulk tenant enumeration can return uninitialized values for some site
    properties (same class of problem as DisableCompanyWideSharingLinks,
    which is *always* Unknown in bulk results); a per-site
    ``get_site_properties_by_url`` read returns the authoritative value.
    Retries on throttling.
    """
    from office365.sharepoint.tenant.administration.tenant import Tenant

    def _read(ctx_, url_):
        tenant = Tenant(ctx_)
        site_props = tenant.get_site_properties_by_url(_encode_csom_url(url_), True)
        ctx_.execute_query()
        return site_props.sharing_capability

    return _with_csom_retry(_read, ctx, site_url)


def _is_unsupported_site_type(site_url: str) -> bool:
    """Return True for site classes that can't be managed via the tenant
    admin sharing APIs (mirrors the worker's cws_drift_detector logic).

    * /contentstorage/* — SharePoint Embedded containers (Loop, Whiteboard,
      Designer, Teams private channels).
    * *-public.sharepoint.com — legacy SharePoint Online Public Website.
    * /sites/contenttypehub — Content Type Hub system site.
    * /portals/* — PointPublishing site collections ("is a PointPublishing
      site collection. The only valid parameters...").
    * MySite host root — "The property DisableCompanyWideSharingLinks cannot
      be set on the MySite host." (Personal sites UNDER it are fine.)
    """
    if not site_url:
        return True
    lower = site_url.lower().rstrip("/")
    if "/contentstorage/" in lower:
        return True
    if "/portals/" in lower:
        return True
    if lower.endswith("/sites/contenttypehub"):
        return True
    try:
        from urllib.parse import urlparse
        parsed = urlparse(lower)
        host = parsed.netloc
        path = parsed.path.rstrip("/")
    except Exception:
        return False
    if host.endswith("-public.sharepoint.com"):
        return True
    return host.endswith("-my.sharepoint.com") and not path


def _set_sharing_capability_sync(ctx, site_url: str, capability_value: int) -> None:
    """Update a site's sharing capability via the SharePoint tenant admin API.

    Retries on throttling so a 429 doesn't silently skip enforcement.
    """
    from office365.runtime.paths.v3.entity import EntityPath
    from office365.sharepoint.tenant.administration.tenant import Tenant

    def _write(ctx_, url_, value_):
        tenant = Tenant(ctx_)
        site_props = tenant.get_site_properties_by_url(_encode_csom_url(url_), True)
        ctx_.execute_query()
        # Pin the entity path to the SiteId so update() addresses the admin
        # list item directly. Without this, update() re-resolves the site BY
        # URL through another $filter round trip, which the server rejects for
        # URLs containing '&' (e.g. /sites/DEPT-BusinessIntelligence&Research
        # Services) — the read works, the write blew up.
        site_id = site_props.properties.get("SiteId")
        if site_id:
            site_props._resource_path = EntityPath(
                site_id, site_props.parent_collection.resource_path
            )
        site_props.sharing_capability = value_
        site_props.update()
        ctx_.execute_query()

    _with_csom_retry(_write, ctx, site_url, capability_value)


# SharingCapability enum values used by SharePoint CSOM
SHARING_CAPABILITY_MAP = {
    "Disabled": 0,
    "ExternalUserSharingOnly": 1,
    "ExternalUserAndGuestSharing": 2,
    "ExistingExternalUserSharingOnly": 3,
}

SHARING_CAPABILITY_REVERSE = {v: k for k, v in SHARING_CAPABILITY_MAP.items()}


def _capability_allows_anonymous(capability_value: int) -> bool:
    """Return True if the sharing capability allows anonymous link creation."""
    return capability_value == SHARING_CAPABILITY_MAP["ExternalUserAndGuestSharing"]


# CompanyWideSharingLinksPolicy enum values
# (used by SiteProperties.DisableCompanyWideSharingLinks — the "Anyone in your
# organization" sharing option toggle).
COMPANY_WIDE_SHARING_MAP = {
    "NotDisabled": 0,
    "Disabled": 1,
    "NotSpecified": 2,
}
COMPANY_WIDE_SHARING_REVERSE = {v: k for k, v in COMPANY_WIDE_SHARING_MAP.items()}
COMPANY_WIDE_SHARING_PROPERTY = "DisableCompanyWideSharingLinks"


def _get_company_wide_sharing_sync(site_props) -> int:
    """Extract DisableCompanyWideSharingLinks value from a SiteProperties object.

    The Office365-REST-Python-Client library doesn't declare this property on
    SiteProperties, so we read it from the dynamic properties dict.  Sites that
    were never configured return NotSpecified (2), which behaves like NotDisabled.
    """
    return site_props.properties.get(COMPANY_WIDE_SHARING_PROPERTY, COMPANY_WIDE_SHARING_MAP["NotSpecified"])


def _set_company_wide_sharing_sync(ctx, site_url: str, value: int) -> None:
    """Update DisableCompanyWideSharingLinks on a single site via the tenant admin API."""
    from office365.sharepoint.tenant.administration.tenant import Tenant

    tenant = Tenant(ctx)
    site_props = tenant.get_site_properties_by_url(_encode_csom_url(site_url), True)
    ctx.execute_query()

    site_props.set_property(COMPANY_WIDE_SHARING_PROPERTY, value)
    site_props.update()
    ctx.execute_query()


def _company_wide_sharing_allowed(value: int) -> bool:
    """Return True if the current value allows company-wide (anyone-in-org) links."""
    # Only Disabled (1) actually blocks the option. NotSpecified behaves as allowed.
    return value != COMPANY_WIDE_SHARING_MAP["Disabled"]


# NOTE: the legacy run_enforcement() pass (site_allowlist_syncs writer) was
# removed 2026-07-05. It had no callers, enumerated only the first CSOM page,
# skipped OneDrive, lacked the unsupported-site filter, and treated
# uninitialized SharingCapability values as compliant. The dashboard still
# reads site_allowlist_syncs history for display; run_site_policy_scan is the
# enforcement path.
