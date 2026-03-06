"""Allowlist enforcement — ensures only allow-listed SharePoint sites permit anonymous sharing.

Uses the Office365-REST-Python-Client library for SharePoint admin CSOM operations,
since the Graph API does not support per-site sharingCapability management.
All synchronous SPO library calls are wrapped in asyncio.to_thread() for async compat.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional, Set

import asyncpg

from .config import LifecycleConfig

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


def _get_all_site_properties_sync(ctx) -> list:
    """Fetch all site collections from the SharePoint tenant admin API.

    Returns a list of SiteProperties objects.
    """
    from office365.sharepoint.tenant.administration.tenant import Tenant

    tenant = Tenant(ctx)
    sites = tenant.get_site_properties_from_sharepoint_by_filters("", "0", True)
    ctx.execute_query()

    return list(sites)


def _get_sharing_capability_sync(site_props) -> int:
    """Extract the SharingCapability value from a SiteProperties object."""
    return site_props.sharing_capability


def _set_sharing_capability_sync(ctx, site_url: str, capability_value: int) -> None:
    """Update a site's sharing capability via the SharePoint tenant admin API."""
    from office365.sharepoint.tenant.administration.tenant import Tenant

    tenant = Tenant(ctx)
    site_props = tenant.get_site_properties_by_url(site_url, True)
    ctx.execute_query()

    site_props.sharing_capability = capability_value
    site_props.update()
    ctx.execute_query()


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


async def run_enforcement(
    db_pool: asyncpg.Pool,
    config: LifecycleConfig,
    sync_id: int,
    full_enforcement: bool = True,
) -> dict:
    """Execute one enforcement pass.

    full_enforcement=True  (weekly cron): disable on non-allow-listed + enable on allow-listed
    full_enforcement=False (manual Sync Now): only enable on allow-listed sites
    """
    stats = {
        "total_sites_checked": 0,
        "sites_disabled": 0,
        "sites_enabled": 0,
        "sites_already_correct": 0,
        "sites_failed": 0,
    }

    enabled_capability = SHARING_CAPABILITY_MAP.get(
        config.allowlist_enabled_capability,
        SHARING_CAPABILITY_MAP["ExternalUserAndGuestSharing"],
    )
    disabled_capability = SHARING_CAPABILITY_MAP.get(
        config.allowlist_disabled_capability,
        SHARING_CAPABILITY_MAP["ExternalUserSharingOnly"],
    )

    try:
        # Claim sync row
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE site_allowlist_syncs
                SET status = 'in_progress', started_at = $1
                WHERE id = $2
                """,
                datetime.now(timezone.utc),
                sync_id,
            )

        # Load allow list
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT site_url FROM site_allowlist")
        allowed_urls: Set[str] = {
            r["site_url"].rstrip("/").lower() for r in rows
        }

        logger.info(
            "Enforcement starting (sync_id=%d, full=%s, allowed_sites=%d)",
            sync_id, full_enforcement, len(allowed_urls),
        )

        # Create SPO admin context and fetch all sites (synchronous, wrapped)
        ctx = await asyncio.to_thread(_create_spo_admin_context, config)
        all_sites = await asyncio.to_thread(_get_all_site_properties_sync, ctx)

        stats["total_sites_checked"] = len(all_sites)

        for site_props in all_sites:
            site_url = str(site_props.url).rstrip("/").lower() if site_props.url else ""
            site_display_name = str(site_props.title) if hasattr(site_props, "title") else ""
            current_capability = _get_sharing_capability_sync(site_props)
            current_capability_name = SHARING_CAPABILITY_REVERSE.get(
                current_capability, str(current_capability)
            )

            is_allowed = site_url in allowed_urls
            action = "no_change"
            desired_capability_name = current_capability_name
            error_msg: Optional[str] = None

            try:
                if is_allowed and current_capability != enabled_capability:
                    # Enable anonymous sharing on allow-listed site
                    desired_capability_name = SHARING_CAPABILITY_REVERSE.get(
                        enabled_capability, str(enabled_capability)
                    )
                    await asyncio.to_thread(
                        _set_sharing_capability_sync, ctx, site_url, enabled_capability
                    )
                    action = "enabled"
                    stats["sites_enabled"] += 1
                    logger.info("Enabled anonymous sharing: %s", site_url)

                elif (
                    not is_allowed
                    and full_enforcement
                    and _capability_allows_anonymous(current_capability)
                ):
                    # Disable anonymous sharing on non-allow-listed site
                    desired_capability_name = SHARING_CAPABILITY_REVERSE.get(
                        disabled_capability, str(disabled_capability)
                    )
                    await asyncio.to_thread(
                        _set_sharing_capability_sync, ctx, site_url, disabled_capability
                    )
                    action = "disabled"
                    stats["sites_disabled"] += 1
                    logger.info("Disabled anonymous sharing: %s", site_url)

                else:
                    stats["sites_already_correct"] += 1

            except Exception as e:
                action = "failed"
                error_msg = _sanitize_error(e)
                stats["sites_failed"] += 1
                logger.error("Failed to update %s: %s", site_url, e)

            # Record per-site detail
            async with db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO site_allowlist_sync_details
                        (sync_id, site_id, site_url, site_display_name,
                         previous_capability, desired_capability, action_taken, error_message)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    sync_id,
                    site_url,
                    site_url,
                    site_display_name,
                    current_capability_name,
                    desired_capability_name,
                    action,
                    error_msg,
                )

            # Rate limit between PATCH calls
            if action in ("enabled", "disabled"):
                await asyncio.sleep(0.2)

        # Mark sync as completed
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE site_allowlist_syncs
                SET status = 'completed',
                    completed_at = $1,
                    total_sites_checked = $2,
                    sites_disabled = $3,
                    sites_enabled = $4,
                    sites_already_correct = $5,
                    sites_failed = $6
                WHERE id = $7
                """,
                datetime.now(timezone.utc),
                stats["total_sites_checked"],
                stats["sites_disabled"],
                stats["sites_enabled"],
                stats["sites_already_correct"],
                stats["sites_failed"],
                sync_id,
            )

    except Exception as e:
        logger.exception("Enforcement run failed (sync_id=%d)", sync_id)
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE site_allowlist_syncs
                SET status = 'failed',
                    completed_at = $1,
                    error_message = $2,
                    total_sites_checked = $3,
                    sites_disabled = $4,
                    sites_enabled = $5,
                    sites_already_correct = $6,
                    sites_failed = $7
                WHERE id = $8
                """,
                datetime.now(timezone.utc),
                _sanitize_error(e),
                stats["total_sites_checked"],
                stats["sites_disabled"],
                stats["sites_enabled"],
                stats["sites_already_correct"],
                stats["sites_failed"],
                sync_id,
            )

    logger.info("Enforcement complete (sync_id=%d): %s", sync_id, stats)
    return stats
