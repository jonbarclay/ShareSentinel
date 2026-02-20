"""Abstract base class for AI providers and shared data structures."""

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Set

logger = logging.getLogger(__name__)

# ======================================================================
# Category taxonomy
# ======================================================================

TIER_1: FrozenSet[str] = frozenset({
    "pii_government_id",
    "pii_financial",
    "ferpa",
    "hipaa",
    "security_credentials",
})

TIER_2: FrozenSet[str] = frozenset({
    "hr_personnel",
    "legal_confidential",
    "sensitive_personal",
})

TIER_3: FrozenSet[str] = frozenset({
    "coursework",
    "casual_personal",
    "none",
    "directory_info",
})

CONDITIONAL: FrozenSet[str] = frozenset({
    "pii_contact",
})

ALL_CATEGORY_IDS: FrozenSet[str] = TIER_1 | TIER_2 | TIER_3 | CONDITIONAL

CATEGORY_LABELS: Dict[str, str] = {
    "pii_government_id": "Government-Issued PII",
    "pii_financial": "Real Financial Account Data",
    "ferpa": "FERPA-Protected Student Records",
    "hipaa": "HIPAA-Protected Health Info",
    "security_credentials": "Security Credentials",
    "hr_personnel": "HR/Personnel Records",
    "legal_confidential": "Legal/Confidential Documents",
    "sensitive_personal": "Sensitive Personal Content",
    "pii_contact": "Personal Identifiable Information",
    "directory_info": "Institutional/Directory Info",
    "coursework": "Coursework/Sample Data",
    "casual_personal": "Casual Personal Content",
    "none": "No Sensitive Content Detected",
}

# PII type sets for escalation rules
GOVERNMENT_ID_PII: FrozenSet[str] = frozenset({"ssn", "passport", "drivers_license", "itin"})
RICH_PII: FrozenSet[str] = frozenset({
    "phone", "home_address", "age",
    "financial_data", "financial_account", "salary", "medical",
})


def compute_escalation_tier(
    category_ids: Set[str],
    affected_count: int = 0,
    pii_types_found: Optional[List[str]] = None,
) -> str:
    """Return 'tier_1', 'tier_2', or 'none' based on categories, volume, and PII richness."""
    # 1. Tier 1 categories always escalate immediately
    if category_ids & TIER_1:
        return "tier_1"

    # 2. Tier 2 categories always escalate at normal priority
    if category_ids & TIER_2:
        return "tier_2"

    # 3. Safety net: government IDs in pii_types regardless of category used
    pii_set = set(pii_types_found or [])
    if pii_set & GOVERNMENT_ID_PII:
        return "tier_1"

    # 4. DOB + name = always escalate, even for 1 person
    if "dob" in pii_set and "name" in pii_set:
        return "tier_2"

    # 5. Conditional escalation for pii_contact
    if "pii_contact" in category_ids and "name" in pii_set:
        rich_types_found = pii_set & RICH_PII

        # 5+ individuals: name + any rich PII type -> tier_2
        if affected_count >= 5 and rich_types_found:
            return "tier_2"

        # 2+ individuals: name + 2 or more rich PII types -> tier_2
        if affected_count >= 2 and len(rich_types_found) >= 2:
            return "tier_2"

    return "none"


# ======================================================================
# Post-processing escalation overrides
# ======================================================================

# Identifiers strong enough to sustain escalation even for coursework files
HARD_IDENTIFIERS: FrozenSet[str] = frozenset({
    "ssn", "passport", "drivers_license", "financial_account",
})

# Filename keywords that indicate coursework/student assignments
_COURSEWORK_FILENAME_RE = re.compile(
    r"(?:group|project|prodject|unit\s*\d|mini\s*case|case\s*study|homework|"
    r"lab\b|assignment|exercise|worksheet|quiz|exam\b|midterm|final\b)",
    re.IGNORECASE,
)

# Student personal OneDrive path pattern
_STUDENT_ONEDRIVE_RE = re.compile(
    r"-my\.sharepoint\.com/personal/\d+_",
    re.IGNORECASE,
)


@dataclass
class EscalationOverride:
    """Result of post-processing escalation override check."""

    adjusted_tier: str
    reason: Optional[str] = None
    replacement_category: Optional[str] = None

    @property
    def applied(self) -> bool:
        return self.reason is not None


def apply_escalation_overrides(
    base_tier: str,
    category_ids: Set[str],
    context: str,
    pii_types_found: Optional[List[str]],
    file_name: str = "",
    file_path: str = "",
    site_url: str = "",
    object_id: str = "",
) -> EscalationOverride:
    """Apply post-processing overrides that can downgrade an escalation.

    Returns an ``EscalationOverride`` with the adjusted tier, the reason
    for the override (or ``None``), and the replacement category to use
    when rewriting the stored verdict.

    Three override rules:

    1. **FERPA name-linkage** — ``ferpa`` requires ``name`` in
       pii_types_found.  Anonymous applicant IDs don't constitute a
       FERPA violation.

    2. **Coursework context downgrade** — When the AI reports
       ``context="coursework"`` but still picks Tier 1/2 categories,
       only sustain escalation if hard identifiers (SSN, passport,
       driver's license, financial account numbers) are present.

    3. **Student-path heuristic** — Files in student OneDrive paths
       whose filenames match coursework patterns get the same
       hard-identifier requirement, even if the AI didn't say
       ``context="coursework"``.
    """
    if base_tier == "none":
        return EscalationOverride(adjusted_tier=base_tier)

    pii_set = set(pii_types_found or [])

    # ------------------------------------------------------------------
    # Rule 1: FERPA requires name linkage
    # ------------------------------------------------------------------
    # If ferpa is the sole driver of escalation and there's no "name"
    # in pii_types, the data is anonymous → downgrade.
    if "ferpa" in category_ids and "name" not in pii_set:
        # Check if ferpa is the only escalating category
        other_escalating = (category_ids & (TIER_1 - {"ferpa"})) | (category_ids & TIER_2)
        if not other_escalating:
            # Also check if the safety-net rules would still escalate
            if not (pii_set & GOVERNMENT_ID_PII):
                if not ("dob" in pii_set and "name" in pii_set):
                    # Choose replacement based on context
                    replacement = "coursework" if context == "coursework" else "none"
                    logger.info(
                        "Override rule 1: FERPA without name linkage — "
                        "downgrading from %s to none (file=%s)",
                        base_tier, file_name,
                    )
                    return EscalationOverride(
                        adjusted_tier="none",
                        reason="ferpa_no_name_linkage",
                        replacement_category=replacement,
                    )

    # ------------------------------------------------------------------
    # Rule 2: Coursework context downgrade
    # ------------------------------------------------------------------
    if context == "coursework" and not (pii_set & HARD_IDENTIFIERS):
        logger.info(
            "Override rule 2: coursework context without hard identifiers — "
            "downgrading from %s to none (file=%s)",
            base_tier, file_name,
        )
        return EscalationOverride(
            adjusted_tier="none",
            reason="coursework_context_no_hard_ids",
            replacement_category="coursework",
        )

    # ------------------------------------------------------------------
    # Rule 3: Student-path + filename heuristic
    # ------------------------------------------------------------------
    # Check if the file is in a student's personal OneDrive
    full_path = site_url or object_id or ""
    is_student_path = bool(_STUDENT_ONEDRIVE_RE.search(full_path))
    is_coursework_name = bool(_COURSEWORK_FILENAME_RE.search(file_name))

    if is_student_path and is_coursework_name and not (pii_set & HARD_IDENTIFIERS):
        logger.info(
            "Override rule 3: student path + coursework filename without "
            "hard identifiers — downgrading from %s to none (file=%s)",
            base_tier, file_name,
        )
        return EscalationOverride(
            adjusted_tier="none",
            reason="student_path_coursework_filename",
            replacement_category="coursework",
        )

    return EscalationOverride(adjusted_tier=base_tier)


# ======================================================================
# Dataclasses
# ======================================================================


@dataclass
class AnalysisRequest:
    """Input to the AI provider."""

    mode: str  # "text", "multimodal", "filename_only"
    text_content: Optional[str] = None
    images: Optional[List[bytes]] = None
    image_mime_types: Optional[List[str]] = None
    file_name: str = ""
    file_path: str = ""
    file_size: int = 0
    sharing_user: str = ""
    sharing_type: str = ""
    sharing_permission: str = ""
    event_time: str = ""
    was_sampled: bool = False
    sampling_description: str = ""
    file_metadata: Dict = field(default_factory=dict)
    filename_flagged: bool = False
    filename_flag_keywords: List[str] = field(default_factory=list)


@dataclass
class CategoryDetection:
    """A single detected sensitivity category with evidence."""

    id: str  # one of ALL_CATEGORY_IDS
    confidence: str = "high"  # "high", "medium", "low"
    evidence: str = ""


@dataclass
class AnalysisResponse:
    """Output from the AI provider."""

    categories: List[CategoryDetection]
    context: str  # "coursework", "institutional", "personal", "mixed"
    summary: str
    recommendation: str
    raw_response: str
    provider: str  # "anthropic", "openai", "gemini"
    model: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    processing_time_seconds: float
    affected_count: int = 0
    pii_types_found: List[str] = field(default_factory=list)
    success: bool = True
    error: Optional[str] = None
    reasoning: str = ""
    data_recency: str = "unknown"
    risk_score: int = 0
    second_look: Optional[Dict[str, Any]] = None

    @property
    def category_ids(self) -> Set[str]:
        """Return the set of detected category IDs."""
        return {c.id for c in self.categories}

    @property
    def escalation_tier(self) -> str:
        """Return 'tier_1', 'tier_2', or 'none'."""
        return compute_escalation_tier(self.category_ids, self.affected_count, self.pii_types_found)

    @property
    def should_escalate(self) -> bool:
        """Whether this response warrants analyst notification."""
        return self.escalation_tier in ("tier_1", "tier_2")


class BaseAIProvider(ABC):
    """Abstract base class for AI providers."""

    @abstractmethod
    async def analyze(self, request: AnalysisRequest) -> AnalysisResponse:
        """Send content to the AI for sensitivity analysis."""
        pass

    @abstractmethod
    def get_provider_name(self) -> str:
        """Return the provider name (e.g., 'anthropic')."""
        pass

    @abstractmethod
    def get_model_name(self) -> str:
        """Return the model name (e.g., 'claude-sonnet-4-5-20250929')."""
        pass

    def calculate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Calculate the cost for a given token count using provider pricing."""
        if hasattr(self, "_calculate_cost"):
            return self._calculate_cost(input_tokens, output_tokens)
        return 0.0
