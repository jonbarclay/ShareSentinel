"""Graph API transcript retrieval for Teams meeting recordings.

Retrieves Microsoft-generated VTT transcripts via the ``getAllTranscripts``
OData function on the beta endpoint.  The pipeline is:

1. Fetch the driveItem's ``source`` facet (beta) to get ``meetingOrganizerId``.
2. Call ``getAllTranscripts(meetingOrganizerUserId=...)`` to list transcripts.
3. Match a transcript to the recording by comparing timestamps.
4. Fetch the VTT content and parse it to plain text.

Required Graph API permissions (Application):
- ``OnlineMeetingTranscript.Read.All``
- ``OnlineMeetings.Read.All``

Also requires a Teams Application Access Policy granting the app access.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any, Dict, Optional

import httpx

from .auth import GraphAuth
from .client import GRAPH_BASE

GRAPH_BETA = "https://graph.microsoft.com/beta"

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Teams recording filename patterns
# ---------------------------------------------------------------------------

# "<Meeting Subject>-YYYYMMDD_HHMMSS-Meeting Recording.mp4"
_PATTERN_STANDARD = re.compile(
    r"-(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})-Meeting Recording\.\w+$",
    re.IGNORECASE,
)
# "GMTYYYYMMDD-HHMMSS_Recording.mp4"
_PATTERN_GMT = re.compile(
    r"^GMT(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})_Recording\.\w+$",
    re.IGNORECASE,
)


def parse_recording_timestamp(filename: str) -> Optional[datetime]:
    """Extract the meeting datetime from a Teams recording filename.

    Returns a timezone-aware UTC datetime, or None if the filename does not
    match known Teams recording patterns.
    """
    for pattern in (_PATTERN_STANDARD, _PATTERN_GMT):
        m = pattern.search(filename)
        if m:
            year, month, day, hour, minute, second = (int(g) for g in m.groups())
            try:
                return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
            except ValueError:
                logger.warning("Invalid date in Teams filename: %s", filename)
                return None
    return None


def is_teams_recording(filename: str) -> bool:
    """Return True if *filename* matches a Teams recording naming pattern."""
    return parse_recording_timestamp(filename) is not None


# ---------------------------------------------------------------------------
# Graph API transcript retrieval
# ---------------------------------------------------------------------------


async def get_meeting_organizer_id(
    auth: GraphAuth,
    drive_id: str,
    item_id: str,
    timeout: float = 15,
) -> Optional[str]:
    """Fetch the meeting organizer's Azure AD object ID from the beta driveItem source facet.

    Teams recordings stored in SharePoint/OneDrive include a ``source``
    facet (beta only) with ``meetingOrganizerId``.  This is required for
    querying transcripts because the driveItem's ``createdBy`` is
    "SharePoint App", not the actual organizer.
    """
    url = f"{GRAPH_BETA}/drives/{drive_id}/items/{item_id}?$select=id,source"
    headers = {"Authorization": f"Bearer {auth.get_access_token()}"}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.debug("Beta driveItem source fetch failed HTTP %d", resp.status_code)
                return None
            source = resp.json().get("source", {})
            organizer = source.get("meetingOrganizerId")
            if organizer:
                logger.debug("Meeting organizer ID: %s", organizer)
            return organizer
    except Exception:
        logger.debug("Failed to fetch driveItem source facet", exc_info=True)
        return None


async def get_meeting_transcript(
    auth: GraphAuth,
    organizer_id: str,
    recording_time: Optional[datetime] = None,
    timeout: int = 30,
) -> Optional[str]:
    """Retrieve a meeting transcript from the Graph API.

    Uses ``getAllTranscripts(meetingOrganizerUserId=...)`` on the beta
    endpoint to list available transcripts, optionally matches by
    recording timestamp, then fetches VTT content.

    Parameters
    ----------
    auth:
        A ``GraphAuth`` instance for obtaining access tokens.
    organizer_id:
        Azure AD object ID of the meeting organizer (from the driveItem
        beta ``source.meetingOrganizerId``).
    recording_time:
        Optional meeting datetime (from filename parsing) used to select
        the closest transcript when the organizer has multiple.
    timeout:
        HTTP request timeout in seconds.

    Returns
    -------
    str or None
        Plain text transcript, or None if unavailable.
    """
    headers = {"Authorization": f"Bearer {auth.get_access_token()}"}
    http_timeout = httpx.Timeout(connect=10.0, read=float(timeout), write=10.0, pool=10.0)

    try:
        async with httpx.AsyncClient(timeout=http_timeout) as client:
            # Step 1: List all transcripts for this organizer
            list_url = (
                f"{GRAPH_BETA}/users/{organizer_id}/onlineMeetings"
                f"/getAllTranscripts(meetingOrganizerUserId='{organizer_id}')"
            )
            resp = await client.get(list_url, headers=headers)
            if resp.status_code == 404:
                logger.info("No transcripts found for organizer=%s (404)", organizer_id)
                return None
            if resp.status_code != 200:
                logger.warning(
                    "getAllTranscripts failed HTTP %d for organizer=%s: %s",
                    resp.status_code, organizer_id, resp.text[:200],
                )
                return None

            transcripts = resp.json().get("value", [])
            if not transcripts:
                logger.info("No transcripts available for organizer=%s", organizer_id)
                return None

            logger.info(
                "Found %d transcript(s) for organizer=%s", len(transcripts), organizer_id,
            )

            # Step 2: Select the best transcript
            transcript = _select_transcript(transcripts, recording_time)
            if not transcript:
                logger.info("No matching transcript found for recording time")
                return None

            meeting_id = transcript.get("meetingId", "")
            transcript_id = transcript.get("id", "")
            if not meeting_id or not transcript_id:
                logger.warning("Transcript missing meetingId or id")
                return None

            # Step 3: Fetch VTT content
            content_url = (
                f"{GRAPH_BASE}/users/{organizer_id}/onlineMeetings/{meeting_id}"
                f"/transcripts/{transcript_id}/content?$format=text/vtt"
            )
            c_resp = await client.get(content_url, headers=headers)
            if c_resp.status_code != 200:
                # Fall back to beta endpoint
                content_url_beta = (
                    f"{GRAPH_BETA}/users/{organizer_id}/onlineMeetings/{meeting_id}"
                    f"/transcripts/{transcript_id}/content?$format=text/vtt"
                )
                c_resp = await client.get(content_url_beta, headers=headers)

            if c_resp.status_code != 200:
                logger.warning(
                    "Transcript content fetch failed HTTP %d for meeting=%s transcript=%s: %s",
                    c_resp.status_code, meeting_id[:40], transcript_id[:20], c_resp.text[:200],
                )
                return None

            vtt_content = c_resp.text
            plain_text = parse_vtt_to_text(vtt_content)
            if plain_text:
                logger.info(
                    "Retrieved transcript (%d chars VTT -> %d chars text) for organizer=%s",
                    len(vtt_content), len(plain_text), organizer_id,
                )
            return plain_text or None

    except httpx.TimeoutException:
        logger.warning("Timeout fetching transcript for organizer=%s", organizer_id)
        return None
    except Exception:
        logger.exception("Unexpected error fetching transcript for organizer=%s", organizer_id)
        return None


def _select_transcript(
    transcripts: list[Dict[str, Any]],
    recording_time: Optional[datetime],
) -> Optional[Dict[str, Any]]:
    """Select the best transcript from the list.

    If ``recording_time`` is provided, picks the transcript whose
    ``createdDateTime`` is closest to it.  Otherwise returns the most
    recent transcript.
    """
    if not transcripts:
        return None

    if len(transcripts) == 1:
        return transcripts[0]

    if recording_time:
        # Find the transcript closest to the recording timestamp
        best = None
        best_delta = timedelta.max
        for t in transcripts:
            created_str = t.get("createdDateTime", "")
            if not created_str:
                continue
            try:
                # Parse ISO timestamp (may have varying precision)
                created_str = created_str.rstrip("Z").split(".")[0]
                created = datetime.fromisoformat(created_str).replace(tzinfo=timezone.utc)
                delta = abs(created - recording_time)
                if delta < best_delta:
                    best_delta = delta
                    best = t
            except (ValueError, TypeError):
                continue
        if best and best_delta < timedelta(hours=4):
            return best

    # Fall back to most recent transcript
    transcripts_sorted = sorted(
        transcripts,
        key=lambda t: t.get("createdDateTime", ""),
        reverse=True,
    )
    return transcripts_sorted[0]


# ---------------------------------------------------------------------------
# VTT parsing
# ---------------------------------------------------------------------------


def parse_vtt_to_text(vtt_content: str) -> str:
    """Parse WebVTT content to plain text.

    Strips:
    - The ``WEBVTT`` header and any metadata lines
    - Cue identifiers (numeric *or* UUID-based, e.g. Stream captions)
    - Timestamp lines (``HH:MM:SS.mmm --> HH:MM:SS.mmm``)
    - HTML tags (``<v Speaker Name>``, ``</v>``, etc.)
    - Duplicate consecutive lines (same speaker repeating)

    Returns concatenated speaker text with line breaks.
    """
    if not vtt_content:
        return ""

    # Strip BOM that SharePoint Stream prepends
    vtt_content = vtt_content.lstrip("\ufeff")

    lines = vtt_content.splitlines()
    text_lines: list[str] = []
    prev_line = ""

    # Regex for timestamp lines
    timestamp_re = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3}\s*-->")
    # Regex for HTML/VTT tags like <v Speaker>, </v>, <c>, etc.
    tag_re = re.compile(r"</?[^>]+>")

    # Build a set of line indices that are cue identifiers.
    # A cue ID is any non-empty line immediately before a timestamp line.
    # This handles both numeric IDs ("1", "2") and UUID-based IDs from Stream.
    stripped_lines = [l.strip() for l in lines]
    cue_id_indices: set[int] = set()
    for i, sl in enumerate(stripped_lines):
        if timestamp_re.match(sl):
            # The previous non-empty line is the cue identifier
            for j in range(i - 1, -1, -1):
                if stripped_lines[j]:
                    cue_id_indices.add(j)
                    break

    in_header = True
    for idx, line in enumerate(lines):
        stripped = stripped_lines[idx]

        # Skip header section (WEBVTT, NOTE, etc.)
        if in_header:
            if not stripped or stripped.startswith("WEBVTT") or stripped.startswith("NOTE"):
                continue
            # First non-empty, non-header line ends header section
            in_header = False

        # Skip empty lines, cue identifiers, and timestamps
        if not stripped:
            continue
        if idx in cue_id_indices:
            continue
        if timestamp_re.match(stripped):
            continue

        # Strip HTML tags and unescape entities
        clean = tag_re.sub("", stripped)
        clean = unescape(clean).strip()

        if not clean:
            continue

        # Deduplicate consecutive identical lines
        if clean != prev_line:
            text_lines.append(clean)
            prev_line = clean

    return "\n".join(text_lines)
