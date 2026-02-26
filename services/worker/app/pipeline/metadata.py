"""Metadata pre-screen: fetch Graph API metadata and check filename sensitivity."""

from __future__ import annotations

import logging
import re
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

import yaml

from ..config import Config
from ..database.repositories import AuditLogRepository, EventRepository, UserProfileRepository
from ..graph_api.client import GraphClient
from ..graph_api.sharing import extract_all_sharing_links, extract_sharing_link, get_sharing_permissions
from ..lifecycle.enrollment import enroll_sharing_links

logger = logging.getLogger(__name__)

# Default path for the file_types config
_FILE_TYPES_CONFIG = "config/file_types.yml"


def _load_sensitivity_keywords(config_path: str = _FILE_TYPES_CONFIG) -> List[str]:
    """Load sensitivity keyword regex patterns from the file_types YAML config."""
    try:
        with open(config_path) as f:
            data = yaml.safe_load(f)
        return data.get("sensitivity_keywords", [])
    except Exception:
        logger.exception("Failed to load sensitivity keywords from %s", config_path)
        return []


class MetadataPrescreen:
    """Fetch item metadata from Graph API and perform filename sensitivity checks.

    This is pipeline Step 3: lightweight Graph API call to collect file
    metadata before deciding whether to download.
    """

    def __init__(self, config_path: str = _FILE_TYPES_CONFIG) -> None:
        self._keywords = _load_sensitivity_keywords(config_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_metadata(
        self,
        job: Any,
        graph_client: GraphClient,
        event_repo: EventRepository,
        audit_repo: AuditLogRepository,
        user_profile_cache_days: int = 7,
        upn_domain: str = "uvu.edu",
    ) -> Dict[str, Any]:
        """Call Graph API for item metadata, update the event record, and return the metadata dict.

        Parameters
        ----------
        job:
            Queue job object with attributes ``event_id``, ``object_id``,
            ``user_id``, ``site_url``, ``workload``, ``relative_path``,
            ``file_name``.
        graph_client:
            Authenticated ``GraphClient`` instance.
        event_repo:
            ``EventRepository`` for persisting metadata on the event row.
        audit_repo:
            ``AuditLogRepository`` for writing audit entries.

        Returns
        -------
        dict
            Enriched metadata dict with keys: ``name``, ``size``,
            ``mime_type``, ``web_url``, ``drive_id``, ``item_id``,
            ``parent_path``, ``created_by``, ``modified_by``,
            ``sharing_link_url``, ``filename_flagged``,
            ``filename_matched_keywords``.
        """
        event_id: str = getattr(job, "event_id", "")
        logger.info("Fetching metadata for event_id=%s", event_id)

        # 1. Get item metadata from Graph API
        raw = await graph_client.get_item_metadata(
            object_id=getattr(job, "object_id", ""),
            site_url=getattr(job, "site_url", None),
            workload=getattr(job, "workload", None),
            user_id=getattr(job, "user_id", None),
            relative_path=getattr(job, "relative_path", None),
            file_name=getattr(job, "file_name", None),
        )

        # Extract fields from the Graph response
        name = raw.get("name", getattr(job, "file_name", ""))
        size = raw.get("size", 0)
        mime_type = (raw.get("file") or {}).get("mimeType", "")
        web_url = raw.get("webUrl", "")
        parent_path = (raw.get("parentReference") or {}).get("path", "")
        drive_id = (raw.get("parentReference") or {}).get("driveId", "")
        item_id = raw.get("id", "")
        created_by = (
            (raw.get("createdBy") or {}).get("user") or {}
        ).get("displayName", "")
        modified_by = (
            (raw.get("lastModifiedBy") or {}).get("user") or {}
        ).get("displayName", "")

        # 2. Get sharing link(s)
        sharing_link_url: Optional[str] = None
        sharing_links: Optional[List[Dict[str, Any]]] = None
        if drive_id and item_id:
            try:
                permissions = await get_sharing_permissions(
                    auth=graph_client._auth,
                    drive_id=drive_id,
                    item_id=item_id,
                )
                sharing_link_url = extract_sharing_link(permissions)
                sharing_links = extract_all_sharing_links(permissions)
            except Exception:
                logger.warning(
                    "Failed to retrieve sharing permissions for event_id=%s",
                    event_id,
                    exc_info=True,
                )
            else:
                # Enroll sharing links into lifecycle tracking (non-blocking)
                try:
                    event_time = getattr(job, "event_time", None)
                    await enroll_sharing_links(
                        db_pool=event_repo._pool,
                        permissions=permissions,
                        event_id=event_id,
                        user_id=getattr(job, "user_id", ""),
                        drive_id=drive_id,
                        item_id=item_id,
                        file_name=name,
                        event_time=event_time,
                    )
                except Exception:
                    logger.warning(
                        "Lifecycle enrollment failed for event_id=%s",
                        event_id,
                        exc_info=True,
                    )

        # 3. Check filename against sensitivity keywords
        filename_flagged, matched_keywords = self.check_filename_keywords(
            name, self._keywords
        )
        if filename_flagged:
            logger.info(
                "Filename sensitivity match event_id=%s keywords=%s",
                event_id,
                matched_keywords,
            )

        # 4. Persist metadata on the event record
        db_metadata = {
            "confirmed_file_name": name,
            "file_size_bytes": size,
            "mime_type": mime_type,
            "web_url": web_url,
            "sharing_link_url": sharing_link_url,
            "sharing_links": sharing_links,
            "drive_id": drive_id,
            "item_id_graph": item_id,
        }
        await event_repo.update_event_metadata(event_id, db_metadata)

        # 5. Audit log
        await audit_repo.log(
            event_id=event_id,
            action="metadata_prescreen",
            details={
                "confirmed_file_name": name,
                "file_size_bytes": size,
                "mime_type": mime_type,
                "filename_flagged": filename_flagged,
                "matched_keywords": matched_keywords,
            },
        )

        # 6. Enrich user profile (non-blocking)
        user_id = getattr(job, "user_id", "")
        if user_id:
            profile_repo = UserProfileRepository(event_repo._pool)
            await self.enrich_user_profile(
                user_id=user_id,
                graph_client=graph_client,
                profile_repo=profile_repo,
                audit_repo=audit_repo,
                event_id=event_id,
                cache_days=user_profile_cache_days,
                upn_domain=upn_domain,
            )

        # 7. Build and return enriched metadata
        return {
            "name": name,
            "size": size,
            "mime_type": mime_type,
            "web_url": web_url,
            "drive_id": drive_id,
            "item_id": item_id,
            "parent_path": parent_path,
            "created_by": created_by,
            "modified_by": modified_by,
            "sharing_link_url": sharing_link_url,
            "sharing_links": sharing_links,
            "filename_flagged": filename_flagged,
            "filename_matched_keywords": matched_keywords,
        }

    async def enrich_user_profile(
        self,
        user_id: str,
        graph_client: GraphClient,
        profile_repo: UserProfileRepository,
        audit_repo: AuditLogRepository,
        event_id: str | None = None,
        cache_days: int = 7,
        upn_domain: str = "uvu.edu",
    ) -> None:
        """Fetch and cache user profile from Graph API.

        Non-blocking: failures are logged but never propagate.
        If *user_id* is a bare ID (no '@'), appends ``@{upn_domain}``
        for Graph API calls while storing the original ID in the DB.
        """
        try:
            cached = await profile_repo.get_cached(user_id, max_age_days=cache_days)
            if cached:
                logger.debug("User profile cache hit for %s", user_id)
                return

            # Build UPN for Graph API if user_id is bare
            upn = user_id if "@" in user_id else f"{user_id}@{upn_domain}"

            # Fetch profile
            raw = await graph_client.get_user_profile(upn)
            profile: Dict[str, Any] = {
                "display_name": raw.get("displayName"),
                "job_title": raw.get("jobTitle"),
                "department": raw.get("department"),
                "mail": raw.get("mail"),
            }

            # Fetch manager (graceful 404)
            try:
                mgr = await graph_client.get_user_manager(upn)
                profile["manager_name"] = mgr.get("displayName") if mgr else None
            except Exception:
                logger.debug("Could not fetch manager for %s", upn, exc_info=True)
                profile["manager_name"] = None

            # Fetch photo (graceful 404), resize to 96x96
            try:
                photo_bytes = await graph_client.get_user_photo(upn)
                if photo_bytes:
                    import base64
                    import io
                    from PIL import Image

                    img = Image.open(io.BytesIO(photo_bytes))
                    img = img.convert("RGB")
                    img.thumbnail((96, 96))
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=85)
                    profile["photo_base64"] = base64.b64encode(buf.getvalue()).decode()
                else:
                    profile["photo_base64"] = None
            except Exception:
                logger.debug("Could not fetch photo for %s", user_id, exc_info=True)
                profile["photo_base64"] = None

            await profile_repo.upsert(user_id, profile)

            await audit_repo.log(
                event_id=event_id,
                action="user_profile_enriched",
                details={"user_id": user_id, "display_name": profile.get("display_name")},
            )
            logger.info("Enriched user profile for %s", user_id)

        except Exception:
            logger.warning("User profile enrichment failed for %s", user_id, exc_info=True)

    @staticmethod
    def check_filename_keywords(
        filename: str, keywords: List[str]
    ) -> Tuple[bool, List[str]]:
        """Check *filename* against a list of regex keyword patterns.

        Parameters
        ----------
        filename:
            The file name to check (e.g. ``"2024_W2_JohnDoe.pdf"``).
        keywords:
            Regex patterns loaded from ``sensitivity_keywords`` in
            ``config/file_types.yml``.

        Returns
        -------
        tuple[bool, list[str]]
            ``(True, [matched_patterns])`` if any keywords matched,
            ``(False, [])`` otherwise.
        """
        if not filename or not keywords:
            return False, []

        # Strip extension for matching; also match against the full name
        stem = PurePosixPath(filename).stem
        targets = [filename.lower(), stem.lower()]
        matched: List[str] = []

        for pattern in keywords:
            try:
                regex = re.compile(pattern, re.IGNORECASE)
                for target in targets:
                    if regex.search(target):
                        matched.append(pattern)
                        break  # no need to match both targets for same pattern
            except re.error:
                logger.warning("Invalid sensitivity keyword regex: %s", pattern)

        return (bool(matched), matched)
