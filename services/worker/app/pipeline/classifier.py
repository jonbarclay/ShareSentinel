"""File classification: determine processing route based on extension, type, and size."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Optional

import yaml

from ..config import Config

logger = logging.getLogger(__name__)

_FILE_TYPES_CONFIG = "config/file_types.yml"


class Category(str, Enum):
    """Classification category for a shared item."""

    PROCESSABLE = "processable"
    EXCLUDED = "excluded"
    ARCHIVE = "archive"
    IMAGE = "image"
    AUDIO_VIDEO = "audio_video"
    OVERSIZED = "oversized"
    FOLDER = "folder"
    UNKNOWN = "unknown"
    DELEGATED_CONTENT = "delegated_content"
    CONVERTIBLE_CONTENT = "convertible_content"


class Action(str, Enum):
    """Processing action derived from the classification."""

    FULL_ANALYSIS = "full_analysis"
    FILENAME_ONLY = "filename_only"
    ARCHIVE_MANIFEST = "archive_manifest"
    MULTIMODAL = "multimodal"
    TRANSCRIPT_ANALYSIS = "transcript_analysis"
    FOLDER_FLAG = "folder_flag"
    PENDING_MANUAL = "pending_manual"
    FORMAT_CONVERSION = "format_conversion"


@dataclass(frozen=True)
class ClassificationResult:
    """Outcome of classifying a shared item."""

    category: Category
    action: Action
    extraction_method: Optional[str] = None
    reason: str = ""


def _load_file_types(config_path: str = _FILE_TYPES_CONFIG) -> dict:
    """Load extension lists from the file_types YAML config."""
    try:
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        logger.exception("Failed to load file_types config from %s", config_path)
        return {}


class FileClassifier:
    """Classify a shared item and determine its processing route.

    Combines pipeline Steps 2, 4, and 5 into a single classification call.
    """

    def __init__(self, config_path: str = _FILE_TYPES_CONFIG) -> None:
        data = _load_file_types(config_path)
        self._excluded: set[str] = set(data.get("excluded_extensions", []))
        self._archives: set[str] = set(data.get("archive_extensions", []))
        self._images: set[str] = set(data.get("image_extensions", []))
        # text_extractable_extensions is a dict: ext -> extractor name
        self._text_extractable: dict[str, str] = data.get("text_extractable_extensions", {})
        self._delegated: set[str] = set(data.get("delegated_content_extensions", []))
        # Audio/video extensions (nested dict with "video" and "audio" keys)
        av_data = data.get("audio_video_extensions", {})
        self._audio_video: set[str] = set(
            av_data.get("video", []) + av_data.get("audio", [])
        )

    def classify(
        self,
        file_name: str,
        item_type: str,
        file_size: int,
        config: Config,
    ) -> ClassificationResult:
        """Classify an item and return the processing route.

        Parameters
        ----------
        file_name:
            Confirmed file name from Graph API metadata.
        item_type:
            ``"File"`` or ``"Folder"`` (from the event payload).
        file_size:
            File size in bytes from Graph API metadata.
        config:
            Worker ``Config`` for ``max_file_size_bytes``.

        Returns
        -------
        ClassificationResult
        """
        # Step 2: Folder check
        if item_type.lower() == "folder":
            return ClassificationResult(
                category=Category.FOLDER,
                action=Action.FOLDER_FLAG,
                reason="Folder shared with broad access; automatic analyst flag.",
            )

        # Get the extension (lowercase, with leading dot)
        ext = self._get_extension(file_name)

        # Convertible content (Loop) — server-side format conversion via Graph API
        _CONVERTIBLE_FORMATS = {
            ".loop": "html", ".fluid": "html",
        }
        if ext and ext in _CONVERTIBLE_FORMATS:
            return ClassificationResult(
                category=Category.CONVERTIBLE_CONTENT,
                action=Action.FORMAT_CONVERSION,
                extraction_method=_CONVERTIBLE_FORMATS[ext],
                reason=f"Extension {ext} supports Graph API format conversion.",
            )

        # Delegated content (OneNote, Whiteboard) — requires manual inspection
        # Whiteboard: Graph API ?format=pdf returns 500, ?format=html returns 406,
        # and raw .whiteboard is a Fluid Framework binary with no extractable text.
        if ext and ext in self._delegated and ext not in _CONVERTIBLE_FORMATS:
            return ClassificationResult(
                category=Category.DELEGATED_CONTENT,
                action=Action.PENDING_MANUAL,
                reason=f"Extension {ext} requires delegated auth for content inspection.",
            )

        # Audio/video check (transcription pipeline)
        if ext and ext in self._audio_video:
            # Use a separate (higher) size limit for A/V files
            av_limit = config.max_av_file_size_bytes
            if file_size > av_limit:
                return ClassificationResult(
                    category=Category.OVERSIZED,
                    action=Action.FILENAME_ONLY,
                    reason=(
                        f"Audio/video file size {file_size:,} bytes exceeds A/V limit "
                        f"{av_limit:,} bytes."
                    ),
                )
            return ClassificationResult(
                category=Category.AUDIO_VIDEO,
                action=Action.TRANSCRIPT_ANALYSIS,
                reason=f"Audio/video extension {ext}; route to transcription pipeline.",
            )

        # No extension -> filename-only analysis
        if not ext:
            return ClassificationResult(
                category=Category.UNKNOWN,
                action=Action.FILENAME_ONLY,
                reason="File has no extension; cannot determine type.",
            )

        # Step 4: Exclusion rules
        if ext in self._excluded:
            return ClassificationResult(
                category=Category.EXCLUDED,
                action=Action.FILENAME_ONLY,
                reason=f"Extension {ext} is in the excluded list.",
            )

        # Step 5: File size check (before categorising type, since oversized
        # files of any processable type should not be downloaded)
        if file_size > config.max_file_size_bytes:
            return ClassificationResult(
                category=Category.OVERSIZED,
                action=Action.FILENAME_ONLY,
                reason=(
                    f"File size {file_size:,} bytes exceeds limit "
                    f"{config.max_file_size_bytes:,} bytes."
                ),
            )

        # Archive check
        if ext in self._archives:
            return ClassificationResult(
                category=Category.ARCHIVE,
                action=Action.ARCHIVE_MANIFEST,
                extraction_method="archive_extractor",
                reason=f"Archive extension {ext}.",
            )

        # Image check
        if ext in self._images:
            return ClassificationResult(
                category=Category.IMAGE,
                action=Action.MULTIMODAL,
                extraction_method="image_preprocessor",
                reason=f"Image extension {ext}.",
            )

        # Text-extractable check (extension without leading dot for the dict key)
        ext_key = ext.lstrip(".")
        if ext_key in self._text_extractable:
            return ClassificationResult(
                category=Category.PROCESSABLE,
                action=Action.FULL_ANALYSIS,
                extraction_method=self._text_extractable[ext_key],
                reason=f"Text-extractable extension {ext}.",
            )

        # Fallback: unrecognised type
        return ClassificationResult(
            category=Category.UNKNOWN,
            action=Action.FILENAME_ONLY,
            reason=f"Extension {ext} is not in any known category.",
        )

    def classify_with_metadata(
        self,
        file_name: str,
        item_type: str,
        file_size: int,
        config: Config,
        metadata: dict | None = None,
    ) -> ClassificationResult:
        """Classify with optional Graph API metadata for package-type detection.

        If metadata contains a ``package`` facet (OneNote, Whiteboard), classify
        as delegated content regardless of extension.
        """
        if metadata:
            package_type = (metadata.get("package") or {}).get("type", "").lower()
            if package_type == "loop":
                return ClassificationResult(
                    category=Category.CONVERTIBLE_CONTENT,
                    action=Action.FORMAT_CONVERSION,
                    extraction_method="html",
                    reason=f"Graph metadata package.type={package_type} supports format conversion.",
                )
            if package_type in ("onenote", "whiteboard"):
                return ClassificationResult(
                    category=Category.DELEGATED_CONTENT,
                    action=Action.PENDING_MANUAL,
                    reason=f"Graph metadata package.type={package_type} requires delegated auth.",
                )
        return self.classify(file_name, item_type, file_size, config)

    @staticmethod
    def _get_extension(file_name: str) -> str:
        """Return the lowered extension including the dot, or empty string.

        Handles compound extensions like ``.tar.gz`` by checking the last two
        suffixes joined together first.
        """
        p = PurePosixPath(file_name.lower())
        suffixes = p.suffixes
        if not suffixes:
            return ""
        # Check compound extension (e.g. ".tar.gz")
        if len(suffixes) >= 2:
            compound = "".join(suffixes[-2:])
            # If a compound like ".tar.gz" is useful, return it
            if compound in (".tar.gz",):
                return compound
        return suffixes[-1]
