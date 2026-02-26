"""Prompt template loading and rendering for AI sensitivity analysis."""

import logging
import os
from pathlib import Path
from typing import Dict, Optional

from .base_provider import AnalysisRequest

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are an AI assistant tasked with scanning files for sensitive information. "
    "You work for an organization's information security team. Your goal is to identify "
    "documents that, if exposed to a wide audience via anonymous or organization-wide "
    "sharing links, could cause concern, reputational damage, policy violations, or harm "
    "to the individuals mentioned in the document.\n\n"
    "You must evaluate the content and return a structured JSON response. Do not include "
    "any text outside the JSON object."
)


def format_file_size(size_bytes: int) -> str:
    """Format byte count as human-readable string (e.g. '2.3 MB')."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


class PromptManager:
    """Loads and renders prompt templates for AI sensitivity analysis.

    Templates are stored in a configurable directory (default:
    ``config/prompt_templates/``).  The main template file is
    ``sensitivity_analysis.txt`` which contains sections for each analysis
    mode delimited by ``### MODE: <mode> ###`` headers.
    """

    def __init__(self, template_dir: Optional[str] = None) -> None:
        if template_dir is None:
            template_dir = os.environ.get(
                "PROMPT_TEMPLATE_DIR", "config/prompt_templates"
            )
        self.template_dir = Path(template_dir)
        self._templates: Dict[str, str] = {}
        self._system_prompt: str = SYSTEM_PROMPT
        self._load_templates()

    # ------------------------------------------------------------------
    # Template loading
    # ------------------------------------------------------------------

    def _load_templates(self) -> None:
        """Parse the sensitivity_analysis.txt template into per-mode sections."""
        template_path = self.template_dir / "sensitivity_analysis_v2.txt"
        if not template_path.exists():
            logger.warning(
                "Prompt template not found at %s; using built-in defaults",
                template_path,
            )
            return

        raw = template_path.read_text(encoding="utf-8")
        sections = raw.split("### MODE: ")

        # First section before any MODE marker is the system prompt header
        for section in sections:
            if not section.strip():
                continue
            # Check for system prompt section (may start with "### SYSTEM PROMPT ###")
            stripped = section.strip().lstrip("#").strip()
            if stripped.startswith("SYSTEM PROMPT"):
                # Everything after the "### SYSTEM PROMPT ###" header
                # Remove the header line, then take the body
                idx = section.find("###", section.find("SYSTEM PROMPT"))
                if idx != -1:
                    body = section[idx + 3:].strip()
                else:
                    body = stripped.replace("SYSTEM PROMPT", "", 1).strip()
                if body:
                    self._system_prompt = body
                continue
            # Mode sections: "text ###\n<body>"
            if "###" in section:
                mode, body = section.split("###", 1)
                mode = mode.strip()
                body = body.strip()
                self._templates[mode] = body

        logger.info(
            "Loaded prompt templates for modes: %s",
            list(self._templates.keys()),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    def render(self, request: AnalysisRequest, **extra_vars: str) -> str:
        """Render the user prompt for *request*.

        Parameters
        ----------
        request:
            The ``AnalysisRequest`` whose fields supply template variables.
        **extra_vars:
            Additional variables that override or supplement those derived
            from *request* (e.g. ``image_count``, ``page_context``,
            ``reason``, ``archive_manifest``).
        """
        variables = self._build_variables(request)
        variables.update(extra_vars)
        template = self._templates.get(request.mode, "")
        if not template:
            logger.error("No template found for mode '%s'", request.mode)
            return ""
        return self._fill(template, variables)

    # ------------------------------------------------------------------
    # Variable building
    # ------------------------------------------------------------------

    def _build_variables(self, request: AnalysisRequest) -> Dict[str, str]:
        """Derive template variables from an AnalysisRequest."""
        # Filename flag notice
        if request.filename_flagged and request.filename_flag_keywords:
            filename_flag_notice = (
                "NOTICE: Filename matches sensitivity keywords: "
                f"[{', '.join(request.filename_flag_keywords)}]"
            )
        else:
            filename_flag_notice = ""

        # Sampling notice
        if request.was_sampled:
            sampling_notice = (
                f"NOTE: This is a sample of the file. {request.sampling_description}"
            )
        else:
            sampling_notice = ""

        # Metadata section
        metadata_section = self._format_metadata(request.file_metadata)

        # File extension
        file_extension = Path(request.file_name).suffix or "unknown"

        return {
            "file_name": request.file_name,
            "file_path": request.file_path,
            "file_size_human": format_file_size(request.file_size),
            "sharing_user": request.sharing_user,
            "sharing_type": request.sharing_type,
            "sharing_permission": request.sharing_permission,
            "event_time": request.event_time,
            "filename_flag_notice": filename_flag_notice,
            "sampling_notice": sampling_notice,
            "metadata_section": metadata_section,
            "text_content": request.text_content or "",
            "file_extension": file_extension,
            # Defaults for optional fields; callers can override via extra_vars
            "image_count": "0",
            "page_context": "",
            "reason": "",
            "archive_manifest": "",
        }

    @staticmethod
    def _format_metadata(metadata: Dict) -> str:
        """Format document metadata dict as a readable section."""
        if not metadata:
            return ""
        lines = ["DOCUMENT PROPERTIES:"]
        for key, value in metadata.items():
            lines.append(f"  - {key}: {value}")
        return "\n".join(lines)

    @staticmethod
    def _fill(template: str, variables: Dict[str, str]) -> str:
        """Substitute ``{variable}`` placeholders in *template*.

        Uses ``str.format_map`` with a default-dict wrapper so that missing
        keys are left as-is rather than raising ``KeyError``.
        """

        class _Default(dict):
            def __missing__(self, key: str) -> str:
                return "{" + key + "}"

        return template.format_map(_Default(variables))
