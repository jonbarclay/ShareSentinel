"""Tests for the PromptManager template rendering."""

import pytest

from app.ai.base_provider import AnalysisRequest
from app.ai.prompt_manager import SYSTEM_PROMPT, PromptManager, format_file_size


# -----------------------------------------------------------------------
# format_file_size
# -----------------------------------------------------------------------


class TestFormatFileSize:
    def test_bytes(self):
        assert format_file_size(500) == "500 B"

    def test_kilobytes(self):
        assert format_file_size(2048) == "2.0 KB"

    def test_megabytes(self):
        assert format_file_size(2_400_000) == "2.3 MB"

    def test_gigabytes(self):
        assert format_file_size(1_500_000_000) == "1.4 GB"

    def test_zero(self):
        assert format_file_size(0) == "0 B"


# -----------------------------------------------------------------------
# PromptManager with on-disk templates
# -----------------------------------------------------------------------


class TestPromptManagerFromDisk:
    """Tests using the actual template file in config/prompt_templates/."""

    @pytest.fixture()
    def pm(self):
        return PromptManager(template_dir="config/prompt_templates")

    def test_system_prompt_loaded(self, pm: PromptManager):
        assert "sensitive information" in pm.system_prompt.lower()

    def test_text_mode_render(self, pm: PromptManager):
        request = AnalysisRequest(
            mode="text",
            text_content="Employee SSNs: 123-45-6789",
            file_name="employees.xlsx",
            file_path="/HR/Confidential/employees.xlsx",
            file_size=50_000,
            sharing_user="jdoe@org.com",
            sharing_type="Anonymous",
            sharing_permission="View",
            event_time="2025-06-01T12:00:00Z",
        )
        rendered = pm.render(request)
        assert "employees.xlsx" in rendered
        assert "/HR/Confidential/employees.xlsx" in rendered
        assert "48.8 KB" in rendered
        assert "jdoe@org.com" in rendered
        assert "Employee SSNs: 123-45-6789" in rendered
        assert "Apply all rules, escalation guidance, and the response format defined above" in rendered

    def test_multimodal_mode_render(self, pm: PromptManager):
        request = AnalysisRequest(
            mode="multimodal",
            images=[b"fake"],
            image_mime_types=["image/jpeg"],
            file_name="scan.pdf",
            file_path="/Documents/scan.pdf",
            file_size=4_000_000,
            sharing_user="user@org.com",
            sharing_type="Organization-wide",
            sharing_permission="Edit",
            event_time="2025-06-01T10:00:00Z",
        )
        rendered = pm.render(
            request,
            image_count="3",
            page_context="These are pages 1-3 of a 12-page PDF document.",
        )
        assert "3 image(s)" in rendered
        assert "pages 1-3 of a 12-page PDF" in rendered
        assert "scan.pdf" in rendered

    def test_filename_only_mode_render(self, pm: PromptManager):
        request = AnalysisRequest(
            mode="filename_only",
            file_name="tax_return_2024.pdf",
            file_path="/personal/jsmith/Financial/tax_return_2024.pdf",
            file_size=5_000_000,
            sharing_user="jsmith@org.com",
            sharing_type="Anonymous",
            sharing_permission="View",
            event_time="2025-01-15T10:00:00Z",
            filename_flagged=True,
            filename_flag_keywords=["tax"],
        )
        rendered = pm.render(request, reason="File too large to download")
        assert "tax_return_2024.pdf" in rendered
        assert "File too large to download" in rendered
        assert "NOTICE: Filename matches sensitivity keywords: [tax]" in rendered
        assert ".pdf" in rendered

    def test_sampling_notice_included(self, pm: PromptManager):
        request = AnalysisRequest(
            mode="text",
            text_content="sample content",
            file_name="big.docx",
            file_path="/docs/big.docx",
            file_size=200_000,
            sharing_user="u@org.com",
            sharing_type="Anonymous",
            sharing_permission="View",
            event_time="2025-06-01T00:00:00Z",
            was_sampled=True,
            sampling_description="First 100KB of 200KB document.",
        )
        rendered = pm.render(request)
        assert "NOTE: This is a sample" in rendered
        assert "First 100KB" in rendered

    def test_metadata_section_included(self, pm: PromptManager):
        request = AnalysisRequest(
            mode="text",
            text_content="data",
            file_name="report.xlsx",
            file_path="/reports/report.xlsx",
            file_size=10_000,
            sharing_user="u@org.com",
            sharing_type="Anonymous",
            sharing_permission="View",
            event_time="2025-06-01T00:00:00Z",
            file_metadata={"Author": "Jane Doe", "Sheet Names": "Salaries, Benefits"},
        )
        rendered = pm.render(request)
        assert "DOCUMENT PROPERTIES:" in rendered
        assert "Author: Jane Doe" in rendered
        assert "Sheet Names: Salaries, Benefits" in rendered


# -----------------------------------------------------------------------
# PromptManager with missing template directory
# -----------------------------------------------------------------------


class TestPromptManagerMissingDir:
    def test_fallback_system_prompt(self):
        pm = PromptManager(template_dir="/nonexistent/path")
        assert pm.system_prompt == SYSTEM_PROMPT

    def test_render_returns_empty_for_unknown_mode(self):
        pm = PromptManager(template_dir="/nonexistent/path")
        request = AnalysisRequest(mode="text", file_name="test.txt")
        assert pm.render(request) == ""
