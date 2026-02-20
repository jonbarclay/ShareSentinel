"""Parse and validate structured JSON responses from AI providers."""

import json
import logging
import re
from typing import Dict

logger = logging.getLogger(__name__)


def _clamp(value: object, min_val: int, max_val: int) -> int:
    """Clamp *value* to [min_val, max_val], defaulting to 3 on error."""
    try:
        return max(min_val, min(max_val, int(value)))  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return 3


def parse_ai_response(raw_text: str) -> Dict:
    """Parse the AI response into a validated dict.

    Handles common formatting issues such as markdown code fences and extra
    text surrounding the JSON payload.  On complete parse failure the
    function returns a safe default with ``sensitivity_rating=3`` so the
    file is flagged for manual review.
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

    # Attempt 2: regex fallback - find a JSON object in the text
    if parsed is None:
        match = re.search(r"\{[^{}]*\}", cleaned, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
            except json.JSONDecodeError:
                pass

    # Attempt 3: try to find nested JSON (objects with nested braces)
    if parsed is None:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
            except json.JSONDecodeError:
                pass

    if parsed is None:
        logger.warning("Failed to parse AI response: %s", raw_text[:200])
        return {
            "sensitivity_rating": 3,
            "categories_detected": ["parse_error"],
            "summary": f"No JSON found in AI response. Raw response: {raw_text[:500]}",
            "confidence": "low",
            "recommendation": "Manual review recommended due to AI response parsing failure.",
        }

    # ------------------------------------------------------------------
    # Validate and sanitize fields
    # ------------------------------------------------------------------
    result = {
        "sensitivity_rating": _clamp(parsed.get("sensitivity_rating", 3), 1, 5),
        "categories_detected": parsed.get("categories_detected", []),
        "summary": str(parsed.get("summary", ""))[:2000],
        "confidence": parsed.get("confidence", "medium"),
        "recommendation": str(parsed.get("recommendation", ""))[:1000],
    }

    # Ensure categories is a list
    if not isinstance(result["categories_detected"], list):
        result["categories_detected"] = [str(result["categories_detected"])]

    # Ensure confidence is a valid enum value
    if result["confidence"] not in ("high", "medium", "low"):
        result["confidence"] = "medium"

    return result
