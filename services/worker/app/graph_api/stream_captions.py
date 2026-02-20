"""SharePoint Stream caption retrieval via the SharePoint REST v2.1 API.

Retrieves auto-generated (or manually uploaded) VTT captions for video
files stored in SharePoint / OneDrive for Business.  The endpoint used
is the *undocumented* but widely-observed ``/_api/v2.1`` media transcripts
API that the Stream player itself calls:

    GET https://{host}{site_path}/_api/v2.1/drives/{driveId}/items/{itemId}/media/transcripts

This requires a **SharePoint-scoped** access token (audience
``https://{host}``), not a Graph-scoped token.

Required Azure AD application permissions:
- ``Sites.Read.All`` (or ``Sites.FullControl.All`` which we already have)

The token is cached by MSAL the same way as the Graph token.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlparse

import httpx

from .auth import GraphAuth
from .transcript import parse_vtt_to_text

logger = logging.getLogger(__name__)


def _extract_site_parts(site_url: str) -> tuple[str, str]:
    """Extract (host, site_path) from a SharePoint site URL.

    Examples
    --------
    >>> _extract_site_parts("https://contoso.sharepoint.com/sites/TeamSite/")
    ('contoso.sharepoint.com', '/sites/TeamSite')
    >>> _extract_site_parts("https://contoso-my.sharepoint.com/personal/jsmith_contoso_com/")
    ('contoso-my.sharepoint.com', '/personal/jsmith_contoso_com')
    """
    parsed = urlparse(site_url)
    host = parsed.hostname or ""
    # Strip trailing slash from the path
    path = parsed.path.rstrip("/")
    return host, path


def _get_sharepoint_token(auth: GraphAuth, host: str) -> str:
    """Acquire a SharePoint-scoped access token.

    Reuses the same MSAL ``ConfidentialClientApplication`` (and its
    credential resolution logic) as the Graph token, but requests a
    different audience.
    """
    app = auth._get_app()
    sp_scope = [f"https://{host}/.default"]

    result = app.acquire_token_silent(sp_scope, account=None)
    if result and "access_token" in result:
        return result["access_token"]

    result = app.acquire_token_for_client(scopes=sp_scope)
    if result and "access_token" in result:
        logger.debug("Acquired SharePoint token for %s", host)
        return result["access_token"]

    error = result.get("error_description", result.get("error", "unknown"))
    raise RuntimeError(f"SharePoint token acquisition failed for {host}: {error}")


async def get_stream_captions(
    auth: GraphAuth,
    drive_id: str,
    item_id: str,
    site_url: str,
    timeout: float = 30,
) -> Optional[str]:
    """Retrieve Stream auto-generated captions for a video file.

    Parameters
    ----------
    auth:
        ``GraphAuth`` instance (certificate or client-secret credential).
    drive_id:
        The SharePoint drive ID containing the video.
    item_id:
        The driveItem ID of the video file.
    site_url:
        The SharePoint site URL (e.g. ``https://tenant.sharepoint.com/sites/SiteName/``).
    timeout:
        HTTP request timeout in seconds.

    Returns
    -------
    str or None
        Plain text transcript parsed from VTT, or None if no captions exist.
    """
    if not drive_id or not item_id or not site_url:
        return None

    host, site_path = _extract_site_parts(site_url)
    if not host or not site_path:
        logger.debug("Could not parse site_url: %s", site_url)
        return None

    try:
        token = _get_sharepoint_token(auth, host)
    except RuntimeError:
        logger.warning("Failed to acquire SharePoint token for Stream captions", exc_info=True)
        return None

    headers = {"Authorization": f"Bearer {token}"}
    http_timeout = httpx.Timeout(connect=10.0, read=timeout, write=10.0, pool=10.0)

    try:
        async with httpx.AsyncClient(timeout=http_timeout) as client:
            # Step 1: List available transcripts
            list_url = (
                f"https://{host}{site_path}/_api/v2.1"
                f"/drives/{drive_id}/items/{item_id}/media/transcripts"
            )
            resp = await client.get(list_url, headers=headers)

            if resp.status_code == 404:
                logger.debug("No Stream captions endpoint for drive=%s item=%s", drive_id, item_id)
                return None
            if resp.status_code != 200:
                logger.debug(
                    "Stream captions list failed HTTP %d: %s",
                    resp.status_code, resp.text[:200],
                )
                return None

            transcripts = resp.json().get("value", [])
            if not transcripts:
                logger.debug("No Stream captions available for item=%s", item_id)
                return None

            logger.info(
                "Found %d Stream caption(s) for item=%s", len(transcripts), item_id,
            )

            # Step 2: Pick the best transcript (prefer default, then first)
            transcript = next(
                (t for t in transcripts if t.get("isDefault")),
                transcripts[0],
            )

            # Step 3: Download the VTT content
            # Prefer the pre-authenticated temporaryDownloadUrl
            download_url = transcript.get("temporaryDownloadUrl")
            if download_url:
                vtt_resp = await client.get(download_url)
            else:
                # Fallback: use the streamContent endpoint
                tid = transcript["id"]
                stream_url = f"{list_url}/{tid}/streamContent"
                vtt_resp = await client.get(stream_url, headers=headers)

            if vtt_resp.status_code != 200:
                logger.warning(
                    "Stream caption download failed HTTP %d for item=%s",
                    vtt_resp.status_code, item_id,
                )
                return None

            vtt_content = vtt_resp.text
            if not vtt_content or len(vtt_content) < 10:
                return None

            plain_text = parse_vtt_to_text(vtt_content)
            if plain_text:
                logger.info(
                    "Stream captions retrieved (%d chars VTT -> %d chars text) for item=%s",
                    len(vtt_content), len(plain_text), item_id,
                )
            return plain_text or None

    except httpx.TimeoutException:
        logger.warning("Timeout fetching Stream captions for item=%s", item_id)
        return None
    except Exception:
        logger.exception("Unexpected error fetching Stream captions for item=%s", item_id)
        return None
