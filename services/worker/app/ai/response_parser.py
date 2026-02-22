"""Parse and validate structured JSON responses from AI providers."""

import json
import logging
import re
from typing import Dict, List

from .base_provider import ALL_CATEGORY_IDS, CategoryDetection

logger = logging.getLogger(__name__)

VALID_CONFIDENCES = {"high", "medium", "low"}
VALID_CONTEXTS = {"coursework", "institutional", "personal", "mixed"}

# Maps off-taxonomy category IDs the AI may produce to correct taxonomy IDs
CATEGORY_NORMALIZATION = {
    "pii_personal_data": "pii_contact",
    "pii_contact_information": "pii_contact",
    "pii_phone_number": "pii_contact",
    "pii_email_address": "pii_contact",
    "pii_name": "pii_contact",
    "pii_home_address": "pii_contact",
    "pii_address": "pii_contact",
    "pii_date_of_birth": "pii_contact",
    "pii_dob": "pii_contact",
    "pii_age": "pii_contact",
    "pii_nationality": "pii_contact",
    "pii_personal_identifiers": "pii_contact",
    "pii_professional_information": "pii_contact",
    "pii_passport_number": "pii_government_id",
    "pii_ssn": "pii_government_id",
    "pii_drivers_license": "pii_government_id",
    "pii_financial_information": "pii_contact",
    "pii_contact_details": "pii_contact",
    # Raw PII type names the AI sometimes uses as category IDs
    "name": "pii_contact",
    "email": "pii_contact",
    "phone": "pii_contact",
    "home_address": "pii_contact",
    "dob": "pii_contact",
    "age": "pii_contact",
    "salary": "pii_contact",
    "medical": "hipaa",
    "passport": "pii_government_id",
    "ssn": "pii_government_id",
    "drivers_license": "pii_government_id",
    "student_id": "pii_contact",
    "financial_account": "pii_financial",
    "financial_data": "pii_contact",
    "institutional": "none",
    # Off-taxonomy categories the AI invents for coursework/benign content
    "educational_records": "coursework",
    "demographic_information": "coursework",
    "academic_records": "coursework",
    "academic_data": "coursework",
    "student_data": "coursework",
    "anonymized_data": "coursework",
    "sample_data": "coursework",
    "internal_financial_data": "none",
    "confidential_financial_information": "none",
    "business_financial": "none",
    "financial_model": "none",
}


def parse_ai_response(raw_text: str) -> Dict:
    """Parse the AI response into a validated dict.

    Expected JSON schema::

        {
            "categories": [{"id": "...", "confidence": "...", "evidence": "..."}],
            "context": "coursework|institutional|personal|mixed",
            "summary": "...",
            "recommendation": "..."
        }

    On complete parse failure the function returns a safe default that
    flags the file for manual review via a ``parse_error`` category.
    """
    cleaned = raw_text.strip()

    # Strip markdown code fences
    cleaned = re.sub(r"^```json\s*", "", cleaned)
    cleaned = re.sub(r"^```\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = cleaned.strip()

    parsed = None

    # Attempt 1: direct JSON parse
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Attempt 2: greedy regex - find the outermost JSON object (handles nested braces)
    if parsed is None:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
            except json.JSONDecodeError:
                pass

    # Attempt 3: simple regex fallback - find a flat JSON object
    if parsed is None:
        match = re.search(r"\{[^{}]*\}", cleaned, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
            except json.JSONDecodeError:
                pass

    if parsed is None:
        logger.warning("Failed to parse AI response: %s", raw_text[:200])
        return {
            "categories": [CategoryDetection(id="none", confidence="low", evidence="parse_error")],
            "context": "mixed",
            "summary": f"No JSON found in AI response. Raw response: {raw_text[:500]}",
            "recommendation": "Manual review recommended due to AI response parsing failure.",
            "affected_count": 0,
            "pii_types_found": [],
        }

    # ------------------------------------------------------------------
    # Validate and sanitize fields
    # ------------------------------------------------------------------
    categories = _parse_categories(parsed.get("categories", []))
    context = parsed.get("context", "mixed")
    if context not in VALID_CONTEXTS:
        context = "mixed"

    # Extract new PII enrichment fields
    affected_count = parsed.get("affected_count", 0)
    if not isinstance(affected_count, int):
        try:
            affected_count = int(affected_count)
        except (ValueError, TypeError):
            affected_count = 0

    pii_types_found = parsed.get("pii_types_found", [])
    if not isinstance(pii_types_found, list):
        pii_types_found = []
    pii_types_found = [str(t).lower().strip() for t in pii_types_found]

    return {
        "categories": categories,
        "context": context,
        "summary": str(parsed.get("summary", ""))[:2000],
        "recommendation": str(parsed.get("recommendation", ""))[:1000],
        "affected_count": affected_count,
        "pii_types_found": pii_types_found,
    }


def _parse_categories(raw: object) -> List[CategoryDetection]:
    """Parse the categories array from AI response, validating each entry."""
    if not isinstance(raw, list):
        return [CategoryDetection(id="none", confidence="low", evidence="categories field was not a list")]

    if not raw:
        return [CategoryDetection(id="none", confidence="high", evidence="")]

    result: List[CategoryDetection] = []
    for item in raw:
        if isinstance(item, dict):
            cat_id = str(item.get("id", "none")).lower().strip()
            # Normalize off-taxonomy category IDs to correct taxonomy
            if cat_id in CATEGORY_NORMALIZATION:
                logger.info("Normalizing category '%s' -> '%s'", cat_id, CATEGORY_NORMALIZATION[cat_id])
                cat_id = CATEGORY_NORMALIZATION[cat_id]
            # Accept unknown category IDs but log a warning
            if cat_id not in ALL_CATEGORY_IDS:
                logger.warning("Unknown category ID '%s' from AI response, keeping as-is", cat_id)
            confidence = str(item.get("confidence", "medium")).lower()
            if confidence not in VALID_CONFIDENCES:
                confidence = "medium"
            evidence = str(item.get("evidence", ""))[:500]
            result.append(CategoryDetection(id=cat_id, confidence=confidence, evidence=evidence))
        elif isinstance(item, str):
            # Handle case where AI returns plain strings instead of objects
            str_id = item.lower().strip()
            if str_id in CATEGORY_NORMALIZATION:
                str_id = CATEGORY_NORMALIZATION[str_id]
            result.append(CategoryDetection(id=str_id, confidence="medium", evidence=""))

    return result if result else [CategoryDetection(id="none", confidence="high", evidence="")]
