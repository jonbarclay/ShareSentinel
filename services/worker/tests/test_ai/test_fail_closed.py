"""Fail-closed behavior for AI analysis (2026-07-05 review fixes).

A file the system cannot analyze is an unknown, not a pass:
- unparseable AI output raises TransientAIError (retried, then failed) instead
  of silently returning a clean 'none' verdict;
- unknown category IDs escalate for review instead of matching no tier.
"""

from __future__ import annotations

import pytest

from app.ai.base_provider import compute_escalation_tier
from app.ai.exceptions import AIAnalysisFailedError, TransientAIError
from app.ai.response_parser import parse_ai_response


class TestParseFailureRaises:
    def test_no_json_raises_transient(self):
        with pytest.raises(TransientAIError):
            parse_ai_response("I'm sorry, I cannot analyze this document.")

    def test_empty_response_raises(self):
        with pytest.raises(TransientAIError):
            parse_ai_response("")

    def test_valid_json_still_parses(self):
        out = parse_ai_response(
            '{"categories": [{"id": "none", "confidence": "high", "evidence": ""}],'
            ' "context": "coursework", "summary": "s", "recommendation": "r"}'
        )
        assert [c.id for c in out["categories"]] == ["none"]


class TestUnknownCategoryEscalates:
    def test_known_tier1(self):
        assert compute_escalation_tier({"hipaa"}) == "tier_1"

    def test_known_tier3_none(self):
        assert compute_escalation_tier({"coursework"}) == "none"

    def test_unknown_category_escalates_tier2(self):
        # The model inventing an off-taxonomy label ("student_medical_records")
        # is the model saying "sensitive" in words we didn't anticipate.
        assert compute_escalation_tier({"student_medical_records"}) == "tier_2"

    def test_unknown_plus_tier1_still_tier1(self):
        assert compute_escalation_tier({"weird_label", "hipaa"}) == "tier_1"


class TestExceptionHierarchy:
    def test_analysis_failed_is_transient(self):
        # AIAnalysisFailedError must ride the bounded-retry path.
        assert issubclass(AIAnalysisFailedError, TransientAIError)
