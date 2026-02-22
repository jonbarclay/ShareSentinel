"""Tests for the AI response parser (category-based rubric)."""

import pytest

from app.ai.base_provider import CategoryDetection
from app.ai.response_parser import parse_ai_response


# -----------------------------------------------------------------------
# Valid JSON responses
# -----------------------------------------------------------------------


class TestValidJSON:
    def test_clean_json_single_category(self):
        raw = (
            '{"categories": [{"id": "ferpa", "confidence": "high",'
            ' "evidence": "Student names with grades"}],'
            ' "context": "institutional",'
            ' "summary": "Contains FERPA data",'
            ' "recommendation": "Revoke sharing link"}'
        )
        result = parse_ai_response(raw)
        assert len(result["categories"]) == 1
        assert result["categories"][0].id == "ferpa"
        assert result["categories"][0].confidence == "high"
        assert result["context"] == "institutional"

    def test_no_sensitive_content(self):
        raw = (
            '{"categories": [{"id": "none", "confidence": "high", "evidence": ""}],'
            ' "context": "personal",'
            ' "summary": "Safe file",'
            ' "recommendation": "No action"}'
        )
        result = parse_ai_response(raw)
        assert result["categories"][0].id == "none"
        assert result["context"] == "personal"

    def test_multiple_categories(self):
        raw = (
            '{"categories": ['
            '  {"id": "pii_government_id", "confidence": "high", "evidence": "SSN visible"},'
            '  {"id": "hipaa", "confidence": "medium", "evidence": "Medical diagnosis"}'
            '],'
            ' "context": "institutional",'
            ' "summary": "Tax return with medical info",'
            ' "recommendation": "Alert analyst"}'
        )
        result = parse_ai_response(raw)
        assert len(result["categories"]) == 2
        assert {c.id for c in result["categories"]} == {"pii_government_id", "hipaa"}

    def test_coursework_context(self):
        raw = (
            '{"categories": [{"id": "coursework", "confidence": "high",'
            ' "evidence": "Student homework with fake data"}],'
            ' "context": "coursework",'
            ' "summary": "Student assignment",'
            ' "recommendation": "No action needed"}'
        )
        result = parse_ai_response(raw)
        assert result["categories"][0].id == "coursework"
        assert result["context"] == "coursework"


# -----------------------------------------------------------------------
# Markdown code-fenced responses
# -----------------------------------------------------------------------


class TestMarkdownFenced:
    def test_json_code_fence(self):
        raw = (
            '```json\n'
            '{"categories": [{"id": "none", "confidence": "high", "evidence": ""}],'
            ' "context": "personal",'
            ' "summary": "Low risk",'
            ' "recommendation": "No action"}\n'
            '```'
        )
        result = parse_ai_response(raw)
        assert result["categories"][0].id == "none"

    def test_plain_code_fence(self):
        raw = (
            '```\n'
            '{"categories": [{"id": "ferpa", "confidence": "medium", "evidence": "Student data"}],'
            ' "context": "institutional",'
            ' "summary": "Student data",'
            ' "recommendation": "Review"}\n'
            '```'
        )
        result = parse_ai_response(raw)
        assert result["categories"][0].id == "ferpa"


# -----------------------------------------------------------------------
# Malformed / edge-case responses
# -----------------------------------------------------------------------


class TestMalformed:
    def test_complete_garbage(self):
        result = parse_ai_response("I don't know what to say")
        assert len(result["categories"]) == 1
        assert result["categories"][0].id == "none"
        assert result["categories"][0].evidence == "parse_error"
        assert result["context"] == "mixed"

    def test_empty_string(self):
        result = parse_ai_response("")
        assert result["categories"][0].id == "none"
        assert result["categories"][0].evidence == "parse_error"

    def test_json_with_surrounding_text(self):
        raw = (
            'Here is my analysis:\n'
            '{"categories": [{"id": "hipaa", "confidence": "high", "evidence": "Medical records"}],'
            ' "context": "institutional",'
            ' "summary": "Medical records",'
            ' "recommendation": "Alert"}\n'
            'I hope this helps!'
        )
        result = parse_ai_response(raw)
        assert result["categories"][0].id == "hipaa"

    def test_invalid_context_defaults_to_mixed(self):
        raw = (
            '{"categories": [{"id": "none", "confidence": "high", "evidence": ""}],'
            ' "context": "invalid_context",'
            ' "summary": "test",'
            ' "recommendation": "test"}'
        )
        result = parse_ai_response(raw)
        assert result["context"] == "mixed"

    def test_invalid_confidence_defaults_to_medium(self):
        raw = (
            '{"categories": [{"id": "ferpa", "confidence": "very_high", "evidence": "grades"}],'
            ' "context": "institutional",'
            ' "summary": "test",'
            ' "recommendation": "test"}'
        )
        result = parse_ai_response(raw)
        assert result["categories"][0].confidence == "medium"

    def test_categories_as_string_list(self):
        """Handle case where AI returns strings instead of objects."""
        raw = (
            '{"categories": ["ferpa", "hipaa"],'
            ' "context": "institutional",'
            ' "summary": "test",'
            ' "recommendation": "test"}'
        )
        result = parse_ai_response(raw)
        assert len(result["categories"]) == 2
        assert all(isinstance(c, CategoryDetection) for c in result["categories"])

    def test_empty_categories_defaults_to_none(self):
        raw = (
            '{"categories": [],'
            ' "context": "personal",'
            ' "summary": "test",'
            ' "recommendation": "test"}'
        )
        result = parse_ai_response(raw)
        assert result["categories"][0].id == "none"

    def test_missing_fields_use_defaults(self):
        raw = '{"categories": [{"id": "ferpa"}]}'
        result = parse_ai_response(raw)
        assert result["categories"][0].id == "ferpa"
        assert result["categories"][0].confidence == "medium"
        assert result["context"] == "mixed"
        assert result["summary"] == ""
        assert result["recommendation"] == ""

    def test_summary_truncation(self):
        raw = (
            '{"categories": [{"id": "none", "confidence": "low", "evidence": ""}],'
            f' "context": "personal", "summary": "{"x" * 3000}",'
            ' "recommendation": "test"}'
        )
        result = parse_ai_response(raw)
        assert len(result["summary"]) == 2000

    def test_unknown_category_id_kept(self):
        raw = (
            '{"categories": [{"id": "custom_category", "confidence": "high", "evidence": "test"}],'
            ' "context": "institutional",'
            ' "summary": "test",'
            ' "recommendation": "test"}'
        )
        result = parse_ai_response(raw)
        assert result["categories"][0].id == "custom_category"


# -----------------------------------------------------------------------
# Off-taxonomy category normalization (false positive reduction)
# -----------------------------------------------------------------------


class TestOffTaxonomyNormalization:
    """Verify that off-taxonomy IDs the AI invents get normalized to valid taxonomy IDs."""

    def test_educational_records_normalized_to_coursework(self):
        raw = (
            '{"categories": [{"id": "educational_records", "confidence": "high",'
            ' "evidence": "GPA and demographics"}],'
            ' "context": "coursework",'
            ' "summary": "Anonymized student dataset",'
            ' "recommendation": "No action"}'
        )
        result = parse_ai_response(raw)
        assert result["categories"][0].id == "coursework"

    def test_demographic_information_normalized_to_coursework(self):
        raw = (
            '{"categories": [{"id": "demographic_information", "confidence": "high",'
            ' "evidence": "Gender and race columns"}],'
            ' "context": "coursework",'
            ' "summary": "Dataset demographics",'
            ' "recommendation": "No action"}'
        )
        result = parse_ai_response(raw)
        assert result["categories"][0].id == "coursework"

    def test_confidential_financial_information_normalized_to_none(self):
        raw = (
            '{"categories": [{"id": "confidential_financial_information", "confidence": "high",'
            ' "evidence": "Revenue and expense data"}],'
            ' "context": "institutional",'
            ' "summary": "Financial model",'
            ' "recommendation": "Review"}'
        )
        result = parse_ai_response(raw)
        assert result["categories"][0].id == "none"

    def test_internal_financial_data_normalized_to_none(self):
        raw = (
            '{"categories": [{"id": "internal_financial_data", "confidence": "high",'
            ' "evidence": "Apportionment calculations"}],'
            ' "context": "coursework",'
            ' "summary": "Tax exercise",'
            ' "recommendation": "No action"}'
        )
        result = parse_ai_response(raw)
        assert result["categories"][0].id == "none"

    def test_mixed_off_taxonomy_and_valid(self):
        """When one category is off-taxonomy and another is valid, both are processed."""
        raw = (
            '{"categories": ['
            '  {"id": "educational_records", "confidence": "high", "evidence": "GPA data"},'
            '  {"id": "pii_contact", "confidence": "medium", "evidence": "Phone numbers"}'
            '],'
            ' "context": "mixed",'
            ' "summary": "Mixed content",'
            ' "recommendation": "Review"}'
        )
        result = parse_ai_response(raw)
        ids = {c.id for c in result["categories"]}
        assert ids == {"coursework", "pii_contact"}
