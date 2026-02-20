"""Tests for the AI response parser."""

import pytest

from app.ai.response_parser import _clamp, parse_ai_response


# -----------------------------------------------------------------------
# _clamp helper
# -----------------------------------------------------------------------


class TestClamp:
    def test_within_range(self):
        assert _clamp(3, 1, 5) == 3

    def test_below_min(self):
        assert _clamp(0, 1, 5) == 1

    def test_above_max(self):
        assert _clamp(9, 1, 5) == 5

    def test_string_number(self):
        assert _clamp("4", 1, 5) == 4

    def test_invalid_string(self):
        assert _clamp("abc", 1, 5) == 3

    def test_none(self):
        assert _clamp(None, 1, 5) == 3

    def test_float(self):
        assert _clamp(4.7, 1, 5) == 4


# -----------------------------------------------------------------------
# Valid JSON responses
# -----------------------------------------------------------------------


class TestValidJSON:
    def test_clean_json(self):
        raw = (
            '{"sensitivity_rating": 4, "categories_detected": ["PII"],'
            ' "summary": "Contains SSN", "confidence": "high",'
            ' "recommendation": "Review immediately"}'
        )
        result = parse_ai_response(raw)
        assert result["sensitivity_rating"] == 4
        assert result["categories_detected"] == ["PII"]
        assert result["confidence"] == "high"
        assert result["summary"] == "Contains SSN"

    def test_rating_1(self):
        raw = (
            '{"sensitivity_rating": 1, "categories_detected": [],'
            ' "summary": "Safe file", "confidence": "high",'
            ' "recommendation": "No action"}'
        )
        result = parse_ai_response(raw)
        assert result["sensitivity_rating"] == 1
        assert result["categories_detected"] == []

    def test_multiple_categories(self):
        raw = (
            '{"sensitivity_rating": 5, '
            '"categories_detected": ["PII - Tax Documents", "Financial Information"],'
            ' "summary": "Tax return", "confidence": "high",'
            ' "recommendation": "Alert analyst"}'
        )
        result = parse_ai_response(raw)
        assert len(result["categories_detected"]) == 2


# -----------------------------------------------------------------------
# Markdown code-fenced responses
# -----------------------------------------------------------------------


class TestMarkdownFenced:
    def test_json_code_fence(self):
        raw = (
            '```json\n'
            '{"sensitivity_rating": 2, "categories_detected": [],'
            ' "summary": "Low risk", "confidence": "medium",'
            ' "recommendation": "No action"}\n'
            '```'
        )
        result = parse_ai_response(raw)
        assert result["sensitivity_rating"] == 2

    def test_plain_code_fence(self):
        raw = (
            '```\n'
            '{"sensitivity_rating": 3, "categories_detected": ["FERPA"],'
            ' "summary": "Student data", "confidence": "medium",'
            ' "recommendation": "Review"}\n'
            '```'
        )
        result = parse_ai_response(raw)
        assert result["sensitivity_rating"] == 3
        assert "FERPA" in result["categories_detected"]


# -----------------------------------------------------------------------
# Malformed / edge-case responses
# -----------------------------------------------------------------------


class TestMalformed:
    def test_complete_garbage(self):
        result = parse_ai_response("I don't know what to say")
        assert result["sensitivity_rating"] == 3
        assert "parse_error" in result["categories_detected"]
        assert result["confidence"] == "low"

    def test_empty_string(self):
        result = parse_ai_response("")
        assert result["sensitivity_rating"] == 3
        assert "parse_error" in result["categories_detected"]

    def test_json_with_surrounding_text(self):
        raw = (
            'Here is my analysis:\n'
            '{"sensitivity_rating": 5, "categories_detected": ["HIPAA"],'
            ' "summary": "Medical records", "confidence": "high",'
            ' "recommendation": "Alert"}\n'
            'I hope this helps!'
        )
        result = parse_ai_response(raw)
        assert result["sensitivity_rating"] == 5

    def test_rating_out_of_range_high(self):
        raw = (
            '{"sensitivity_rating": 10, "categories_detected": [],'
            ' "summary": "test", "confidence": "high",'
            ' "recommendation": "test"}'
        )
        result = parse_ai_response(raw)
        assert result["sensitivity_rating"] == 5

    def test_rating_out_of_range_low(self):
        raw = (
            '{"sensitivity_rating": -1, "categories_detected": [],'
            ' "summary": "test", "confidence": "high",'
            ' "recommendation": "test"}'
        )
        result = parse_ai_response(raw)
        assert result["sensitivity_rating"] == 1

    def test_invalid_confidence(self):
        raw = (
            '{"sensitivity_rating": 3, "categories_detected": [],'
            ' "summary": "test", "confidence": "very_high",'
            ' "recommendation": "test"}'
        )
        result = parse_ai_response(raw)
        assert result["confidence"] == "medium"

    def test_categories_as_string(self):
        raw = (
            '{"sensitivity_rating": 3, "categories_detected": "PII",'
            ' "summary": "test", "confidence": "high",'
            ' "recommendation": "test"}'
        )
        result = parse_ai_response(raw)
        assert isinstance(result["categories_detected"], list)
        assert result["categories_detected"] == ["PII"]

    def test_missing_fields_use_defaults(self):
        raw = '{"sensitivity_rating": 4}'
        result = parse_ai_response(raw)
        assert result["sensitivity_rating"] == 4
        assert result["categories_detected"] == []
        assert result["confidence"] == "medium"
        assert result["summary"] == ""
        assert result["recommendation"] == ""

    def test_summary_truncation(self):
        raw = (
            '{"sensitivity_rating": 3, "categories_detected": [],'
            f' "summary": "{"x" * 3000}", "confidence": "low",'
            ' "recommendation": "test"}'
        )
        result = parse_ai_response(raw)
        assert len(result["summary"]) == 2000
