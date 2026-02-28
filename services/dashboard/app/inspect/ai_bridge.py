"""Bridge to AI providers for dashboard content inspection.

Provides a simplified AI analysis interface that loads the shared prompt
template and calls Anthropic, OpenAI, or Gemini APIs based on configuration.
"""

import base64
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Sensitivity tier mapping for escalation decisions
TIER_1 = {"pii_government_id", "pii_financial", "ferpa", "hipaa", "security_credentials"}
TIER_2 = {"hr_personnel", "legal_confidential", "pii_contact"}

# Prompt template location (mounted via docker-compose config volume)
PROMPT_TEMPLATE_PATH = Path("/app/config/prompt_templates/sensitivity_analysis_v2.txt")

_prompt_cache: Optional[str] = None


def _load_prompt_template() -> str:
    """Load and cache the sensitivity analysis prompt template."""
    global _prompt_cache
    if _prompt_cache is not None:
        return _prompt_cache
    if PROMPT_TEMPLATE_PATH.exists():
        _prompt_cache = PROMPT_TEMPLATE_PATH.read_text()
        return _prompt_cache
    logger.error("Prompt template not found at %s", PROMPT_TEMPLATE_PATH)
    raise FileNotFoundError(f"Prompt template not found: {PROMPT_TEMPLATE_PATH}")


def _build_text_prompt(text: str, file_name: str, content_type: str) -> tuple[str, str]:
    """Build a text-mode analysis prompt from the template."""
    template = _load_prompt_template()

    # Extract the system prompt (everything before ### MODE: text ###)
    parts = template.split("### MODE: text ###")
    if len(parts) < 2:
        # Fallback: use entire template as system prompt
        system_prompt = template
        user_section = ""
    else:
        system_prompt = parts[0].replace("### SYSTEM PROMPT ###", "").strip()
        # Extract text mode section (up to next ### MODE:)
        user_section = parts[1].split("### MODE:")[0].strip()

    # Fill in placeholders with inspection defaults
    user_section = user_section.replace("{file_name}", file_name)
    user_section = user_section.replace("{file_path}", "N/A")
    user_section = user_section.replace("{file_size_human}", "N/A")
    user_section = user_section.replace("{sharing_user}", "N/A")
    user_section = user_section.replace("{sharing_type}", "N/A")
    user_section = user_section.replace("{sharing_permission}", "N/A")
    user_section = user_section.replace("{event_time}", "N/A")
    user_section = user_section.replace("{filename_flag_notice}", "")
    user_section = user_section.replace("{sampling_notice}", "")
    user_section = user_section.replace("{metadata_section}", f"- Content type: {content_type}")
    user_section = user_section.replace("{text_content}", text[:100_000])

    return system_prompt, user_section


def _build_multimodal_prompt(file_name: str, content_type: str) -> tuple[str, str]:
    """Build a multimodal-mode analysis prompt from the template."""
    template = _load_prompt_template()

    parts = template.split("### MODE: multimodal ###")
    if len(parts) < 2:
        system_prompt = template.split("### MODE:")[0].replace("### SYSTEM PROMPT ###", "").strip()
        user_section = "Analyze the attached image for sensitive information."
    else:
        system_prompt = parts[0].split("### MODE: text ###")[0].replace("### SYSTEM PROMPT ###", "").strip()
        user_section = parts[1].split("### MODE:")[0].strip()

    user_section = user_section.replace("{file_name}", file_name)
    user_section = user_section.replace("{file_path}", "N/A")
    user_section = user_section.replace("{file_size_human}", "N/A")
    user_section = user_section.replace("{sharing_user}", "N/A")
    user_section = user_section.replace("{sharing_type}", "N/A")
    user_section = user_section.replace("{sharing_permission}", "N/A")
    user_section = user_section.replace("{event_time}", "N/A")
    user_section = user_section.replace("{filename_flag_notice}", "")
    user_section = user_section.replace("{metadata_section}", f"- Content type: {content_type}")
    user_section = user_section.replace("{image_count}", "1")
    user_section = user_section.replace("{page_context}", "")

    return system_prompt, user_section


def _parse_ai_json(raw_text: str) -> dict:
    """Extract and parse JSON from AI response text.

    Looks for ```json fenced blocks first, then raw JSON between braces.
    """
    # Try fenced JSON block
    match = re.search(r"```json\s*\n?(.*?)\n?\s*```", raw_text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try raw JSON between outermost braces
    match = re.search(r"\{.*\}", raw_text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    logger.error("Failed to parse AI JSON response: %s", raw_text[:500])
    return {
        "categories": [{"id": "none", "confidence": "low", "evidence": "AI response parsing failed"}],
        "summary": "Failed to parse AI response",
        "recommendation": "Manual review required",
        "context": "mixed",
        "risk_score": 0,
    }


def _determine_escalation_tier(categories: list[dict]) -> str:
    """Determine the highest escalation tier from category assessments."""
    cat_ids = {c.get("id", "") for c in categories}
    if cat_ids & TIER_1:
        return "tier_1"
    if cat_ids & TIER_2:
        return "tier_2"
    return "tier_3"


def _determine_notification_required(categories: list[dict]) -> bool:
    """Check if any Tier 1 or Tier 2 categories are present."""
    cat_ids = {c.get("id", "") for c in categories}
    return bool(cat_ids & (TIER_1 | TIER_2))


# ---------------------------------------------------------------------------
# Provider-specific API calls
# ---------------------------------------------------------------------------

TIMEOUT = httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0)


async def _call_anthropic(
    system_prompt: str,
    user_content: list,
    api_key: str,
    model: str,
    max_tokens: int,
) -> str:
    """Call Anthropic Messages API."""
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_content}],
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"]


async def _call_openai(
    system_prompt: str,
    user_content: list,
    api_key: str,
    model: str,
    max_tokens: int,
) -> str:
    """Call OpenAI Chat Completions API."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "max_completion_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


async def _call_gemini(
    system_prompt: str,
    user_content: list,
    api_key: str,
    model: str,
    max_tokens: int,
) -> str:
    """Call Google Gemini generateContent API."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    # Build parts for Gemini
    parts = [{"text": system_prompt}]
    for item in user_content:
        if isinstance(item, dict) and item.get("type") == "image":
            parts.append({
                "inline_data": {
                    "mime_type": item["source"]["media_type"],
                    "data": item["source"]["data"],
                }
            })
        elif isinstance(item, dict) and item.get("type") == "text":
            parts.append({"text": item["text"]})
        elif isinstance(item, str):
            parts.append({"text": item})

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def analyze_text(
    text: str,
    file_name: str,
    content_type: str,
    ai_config: dict,
) -> dict:
    """Analyze text content for sensitivity using the configured AI provider.

    Returns a parsed result dict with categories, summary, recommendation, etc.
    """
    provider = ai_config["provider"]
    api_key = ai_config["api_key"]
    model = ai_config["model"]
    max_tokens = ai_config.get("max_tokens", 1024)

    system_prompt, user_section = _build_text_prompt(text, file_name, content_type)

    start = time.monotonic()

    if provider == "anthropic":
        user_content = [{"type": "text", "text": user_section}]
        raw = await _call_anthropic(system_prompt, user_content, api_key, model, max_tokens)
    elif provider == "openai":
        user_content = [{"type": "text", "text": user_section}]
        raw = await _call_openai(system_prompt, user_content, api_key, model, max_tokens)
    elif provider == "gemini":
        user_content = [{"type": "text", "text": user_section}]
        raw = await _call_gemini(system_prompt, user_content, api_key, model, max_tokens)
    else:
        raise ValueError(f"Unsupported AI provider: {provider}")

    elapsed = time.monotonic() - start
    result = _parse_ai_json(raw)
    result["ai_provider"] = provider
    result["ai_model"] = model
    result["processing_time_seconds"] = round(elapsed, 2)
    result["analysis_mode"] = "text"
    return result


async def analyze_image(
    image_path: str,
    file_name: str,
    content_type: str,
    ai_config: dict,
) -> dict:
    """Analyze an image file for sensitivity using multimodal AI.

    Returns a parsed result dict with categories, summary, recommendation, etc.
    """
    provider = ai_config["provider"]
    api_key = ai_config["api_key"]
    model = ai_config["model"]
    max_tokens = ai_config.get("max_tokens", 1024)

    image_data = Path(image_path).read_bytes()
    b64_data = base64.standard_b64encode(image_data).decode("ascii")

    # Detect media type from extension
    ext = Path(image_path).suffix.lower()
    media_type_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".pdf": "application/pdf",
    }
    media_type = media_type_map.get(ext, "image/png")

    system_prompt, user_section = _build_multimodal_prompt(file_name, content_type)

    start = time.monotonic()

    if provider == "anthropic":
        user_content = [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64_data}},
            {"type": "text", "text": user_section},
        ]
        raw = await _call_anthropic(system_prompt, user_content, api_key, model, max_tokens)
    elif provider == "openai":
        user_content = [
            {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64_data}"}},
            {"type": "text", "text": user_section},
        ]
        raw = await _call_openai(system_prompt, user_content, api_key, model, max_tokens)
    elif provider == "gemini":
        user_content = [
            {"type": "image", "source": {"media_type": media_type, "data": b64_data}},
            {"type": "text", "text": user_section},
        ]
        raw = await _call_gemini(system_prompt, user_content, api_key, model, max_tokens)
    else:
        raise ValueError(f"Unsupported AI provider: {provider}")

    elapsed = time.monotonic() - start
    result = _parse_ai_json(raw)
    result["ai_provider"] = provider
    result["ai_model"] = model
    result["processing_time_seconds"] = round(elapsed, 2)
    result["analysis_mode"] = "multimodal"
    return result


async def save_verdict(
    event_id: str,
    result: dict,
    analysis_mode: str,
    db_pool,
) -> Optional[int]:
    """Save an AI analysis verdict to the verdicts table.

    Returns the verdict row ID, or None on failure.
    """
    categories = result.get("categories", [])
    category_ids = [c.get("id", "none") for c in categories]
    escalation_tier = _determine_escalation_tier(categories)
    notification_required = _determine_notification_required(categories)

    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO verdicts (
                    event_id, categories_detected,
                    category_assessments, overall_context, escalation_tier,
                    summary, recommendation,
                    analysis_mode, ai_provider, ai_model,
                    notification_required,
                    affected_count, pii_types_found,
                    reasoning, data_recency, risk_score
                ) VALUES (
                    $1, $2::jsonb,
                    $3::jsonb, $4, $5,
                    $6, $7,
                    $8, $9, $10,
                    $11,
                    $12, $13::jsonb,
                    $14, $15, $16
                )
                RETURNING id
                """,
                event_id,
                json.dumps(category_ids),
                json.dumps(categories),
                result.get("context", "mixed"),
                escalation_tier,
                result.get("summary", ""),
                result.get("recommendation", ""),
                analysis_mode,
                result.get("ai_provider", ""),
                result.get("ai_model", ""),
                notification_required,
                result.get("affected_count", 0),
                json.dumps(result.get("pii_types_found", [])),
                result.get("reasoning", None),
                result.get("data_recency", None),
                result.get("risk_score", 0),
            )
            return row["id"]
    except Exception:
        logger.exception("Failed to save verdict for event %s", event_id)
        return None
