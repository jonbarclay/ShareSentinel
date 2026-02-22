"""Tests for post-processing escalation overrides."""

import pytest

from app.ai.base_provider import apply_escalation_overrides


class TestFerpaNameLinkage:
    """Rule 1: FERPA requires name in pii_types_found to escalate."""

    def test_ferpa_with_name_keeps_escalation(self):
        result = apply_escalation_overrides(
            base_tier="tier_1",
            category_ids={"ferpa"},
            context="institutional",
            pii_types_found=["name", "student_id"],
            file_name="class_roster.xlsx",
        )
        assert result.adjusted_tier == "tier_1"
        assert not result.applied

    def test_ferpa_without_name_downgrades(self):
        result = apply_escalation_overrides(
            base_tier="tier_1",
            category_ids={"ferpa"},
            context="institutional",
            pii_types_found=["student_id"],
            file_name="College1stYr_Group 17.xlsx",
        )
        assert result.adjusted_tier == "none"
        assert result.reason == "ferpa_no_name_linkage"
        # Non-coursework context → replacement is "none"
        assert result.replacement_category == "none"

    def test_ferpa_without_name_coursework_context_replaces_with_coursework(self):
        result = apply_escalation_overrides(
            base_tier="tier_1",
            category_ids={"ferpa"},
            context="coursework",
            pii_types_found=["student_id"],
            file_name="data.xlsx",
        )
        assert result.adjusted_tier == "none"
        assert result.replacement_category == "coursework"

    def test_ferpa_without_any_pii_types_downgrades(self):
        result = apply_escalation_overrides(
            base_tier="tier_1",
            category_ids={"ferpa"},
            context="coursework",
            pii_types_found=[],
            file_name="data.xlsx",
        )
        assert result.adjusted_tier == "none"
        assert result.applied

    def test_ferpa_plus_other_tier1_keeps_escalation(self):
        """If another Tier 1 category is present alongside FERPA, don't downgrade."""
        result = apply_escalation_overrides(
            base_tier="tier_1",
            category_ids={"ferpa", "pii_government_id"},
            context="institutional",
            pii_types_found=["ssn"],
            file_name="student_records.xlsx",
        )
        assert result.adjusted_tier == "tier_1"
        assert not result.applied

    def test_ferpa_with_government_id_pii_keeps_escalation(self):
        """Safety net: if SSN is in pii_types, keep escalation even without name."""
        result = apply_escalation_overrides(
            base_tier="tier_1",
            category_ids={"ferpa"},
            context="institutional",
            pii_types_found=["ssn"],
            file_name="records.xlsx",
        )
        assert result.adjusted_tier == "tier_1"
        assert not result.applied


class TestCourseworkContextDowngrade:
    """Rule 2: Coursework context without hard identifiers downgrades."""

    def test_coursework_context_no_hard_ids_downgrades(self):
        result = apply_escalation_overrides(
            base_tier="tier_1",
            category_ids={"pii_financial"},
            context="coursework",
            pii_types_found=["financial_data"],
            file_name="Mini Case - State Apportionment.xlsx",
        )
        assert result.adjusted_tier == "none"
        assert result.reason == "coursework_context_no_hard_ids"
        assert result.replacement_category == "coursework"

    def test_coursework_context_with_ssn_keeps_escalation(self):
        result = apply_escalation_overrides(
            base_tier="tier_1",
            category_ids={"pii_government_id"},
            context="coursework",
            pii_types_found=["ssn", "name"],
            file_name="homework_with_real_ssns.xlsx",
        )
        assert result.adjusted_tier == "tier_1"
        assert not result.applied

    def test_coursework_context_with_financial_account_keeps(self):
        result = apply_escalation_overrides(
            base_tier="tier_1",
            category_ids={"pii_financial"},
            context="coursework",
            pii_types_found=["financial_account"],
            file_name="budget.xlsx",
        )
        assert result.adjusted_tier == "tier_1"
        assert not result.applied

    def test_non_coursework_context_not_affected(self):
        result = apply_escalation_overrides(
            base_tier="tier_1",
            category_ids={"pii_financial"},
            context="institutional",
            pii_types_found=["financial_data"],
            file_name="budget.xlsx",
        )
        assert result.adjusted_tier == "tier_1"
        assert not result.applied

    def test_coursework_tier2_also_downgrades(self):
        result = apply_escalation_overrides(
            base_tier="tier_2",
            category_ids={"hr_personnel"},
            context="coursework",
            pii_types_found=["salary"],
            file_name="HR Exercise Group 5.xlsx",
        )
        assert result.adjusted_tier == "none"
        assert result.reason == "coursework_context_no_hard_ids"


class TestStudentPathHeuristic:
    """Rule 3: Student OneDrive path + coursework filename downgrades."""

    def test_student_path_with_coursework_name_downgrades(self):
        result = apply_escalation_overrides(
            base_tier="tier_1",
            category_ids={"pii_financial"},
            context="institutional",  # AI didn't say coursework
            pii_types_found=["financial_data"],
            file_name="Mini Case - State Apportionment.xlsx",
            site_url="https://uvu365-my.sharepoint.com/personal/10688471_uvu_edu",
        )
        assert result.adjusted_tier == "none"
        assert result.reason == "student_path_coursework_filename"
        assert result.replacement_category == "coursework"

    def test_student_path_without_coursework_name_no_override(self):
        result = apply_escalation_overrides(
            base_tier="tier_1",
            category_ids={"pii_financial"},
            context="institutional",
            pii_types_found=["financial_data"],
            file_name="Budget_2026.xlsx",  # Not a coursework filename
            site_url="https://uvu365-my.sharepoint.com/personal/10688471_uvu_edu",
        )
        assert result.adjusted_tier == "tier_1"
        assert not result.applied

    def test_non_student_path_with_coursework_name_no_override(self):
        result = apply_escalation_overrides(
            base_tier="tier_1",
            category_ids={"pii_financial"},
            context="institutional",
            pii_types_found=["financial_data"],
            file_name="Mini Case - State Apportionment.xlsx",
            site_url="https://uvu365.sharepoint.com/sites/Finance",
        )
        assert result.adjusted_tier == "tier_1"
        assert not result.applied

    def test_student_path_with_hard_ids_keeps_escalation(self):
        result = apply_escalation_overrides(
            base_tier="tier_1",
            category_ids={"pii_government_id"},
            context="institutional",
            pii_types_found=["ssn", "name"],
            file_name="Unit 3 Project.xlsx",
            site_url="https://uvu365-my.sharepoint.com/personal/10688471_uvu_edu",
        )
        assert result.adjusted_tier == "tier_1"
        assert not result.applied

    def test_object_id_also_checked_for_student_path(self):
        result = apply_escalation_overrides(
            base_tier="tier_2",
            category_ids={"hr_personnel"},
            context="institutional",
            pii_types_found=["salary"],
            file_name="Group Project Final.xlsx",
            object_id="https://uvu365-my.sharepoint.com/personal/10916467_uvu_edu/Documents/Group Project Final.xlsx",
        )
        assert result.adjusted_tier == "none"
        assert result.reason == "student_path_coursework_filename"

    def test_prodject_misspelling_matches(self):
        """Verify common misspelling 'prodject' still matches."""
        result = apply_escalation_overrides(
            base_tier="tier_1",
            category_ids={"ferpa"},
            context="institutional",
            pii_types_found=["student_id"],
            file_name="Unit 2 prodject Goup 19.docx",
            site_url="https://uvu365-my.sharepoint.com/personal/10947750_uvu_edu",
        )
        assert result.adjusted_tier == "none"
        assert result.applied


class TestNoOverrideWhenNotEscalated:
    """Base tier 'none' should pass through untouched."""

    def test_none_tier_passes_through(self):
        result = apply_escalation_overrides(
            base_tier="none",
            category_ids={"coursework"},
            context="coursework",
            pii_types_found=[],
            file_name="homework.docx",
        )
        assert result.adjusted_tier == "none"
        assert not result.applied


class TestRulePriority:
    """Rules are checked in order; first match wins."""

    def test_ferpa_rule_fires_before_coursework_when_context_is_not_coursework(self):
        """FERPA name-linkage fires even if context isn't coursework."""
        result = apply_escalation_overrides(
            base_tier="tier_1",
            category_ids={"ferpa"},
            context="institutional",
            pii_types_found=["student_id"],
            file_name="data.xlsx",
        )
        assert result.adjusted_tier == "none"
        assert result.reason == "ferpa_no_name_linkage"

    def test_coursework_context_fires_for_non_ferpa_categories(self):
        result = apply_escalation_overrides(
            base_tier="tier_1",
            category_ids={"pii_financial"},
            context="coursework",
            pii_types_found=[],
            file_name="apportionment.xlsx",
        )
        assert result.adjusted_tier == "none"
        assert result.reason == "coursework_context_no_hard_ids"


class TestReplacementCategory:
    """Verify replacement_category is set correctly for each rule."""

    def test_ferpa_institutional_replaces_with_none(self):
        result = apply_escalation_overrides(
            base_tier="tier_1",
            category_ids={"ferpa"},
            context="institutional",
            pii_types_found=[],
            file_name="weighted-scoring.xlsx",
        )
        assert result.replacement_category == "none"

    def test_ferpa_coursework_replaces_with_coursework(self):
        result = apply_escalation_overrides(
            base_tier="tier_1",
            category_ids={"ferpa"},
            context="coursework",
            pii_types_found=[],
            file_name="data.xlsx",
        )
        assert result.replacement_category == "coursework"

    def test_coursework_context_always_replaces_with_coursework(self):
        result = apply_escalation_overrides(
            base_tier="tier_1",
            category_ids={"hipaa"},
            context="coursework",
            pii_types_found=["medical"],
            file_name="health_exercise.xlsx",
        )
        assert result.replacement_category == "coursework"

    def test_student_path_always_replaces_with_coursework(self):
        result = apply_escalation_overrides(
            base_tier="tier_2",
            category_ids={"legal_confidential"},
            context="institutional",
            pii_types_found=[],
            file_name="Group Project.docx",
            site_url="https://uvu365-my.sharepoint.com/personal/10688471_uvu_edu",
        )
        assert result.replacement_category == "coursework"
