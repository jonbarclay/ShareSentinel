"""Second-look AI review to reduce false positives on high-FP-risk patterns."""

from __future__ import annotations

import logging
from typing import Optional

from ..ai.base_provider import (
    AnalysisRequest,
    AnalysisResponse,
    BaseAIProvider,
    TIER_1,
)
from ..database.repositories import AuditLogRepository
from .retry import retry_with_backoff

logger = logging.getLogger(__name__)

# Analysis modes that are never eligible for second-look
_EXEMPT_MODES = frozenset({"filename_only", "hash_reuse", "folder_flag"})


def needs_second_look(response: AnalysisResponse, analysis_mode: str) -> bool:
    """Decide whether the initial verdict should be re-examined by a second model.

    Returns True whenever the primary model escalates (tier_1 or tier_2),
    unless the analysis mode is exempt (filename_only, hash_reuse, folder_flag).
    """
    if analysis_mode in _EXEMPT_MODES:
        return False

    return response.should_escalate


async def run_second_look(
    second_look_provider: BaseAIProvider,
    request: AnalysisRequest,
    initial_response: AnalysisResponse,
    event_id: str,
    audit_repo: AuditLogRepository,
) -> AnalysisResponse:
    """Run a second AI analysis and return the verdict to use.

    Decision logic:
    - If the second model says no escalation -> return the second-look response (downgrade)
    - If the second model agrees escalation is needed -> return the initial response (keep original)
    - On failure -> return the initial response unchanged (fail-open)
    """
    await audit_repo.log(event_id, "second_look_start", {
        "provider": second_look_provider.get_provider_name(),
        "model": second_look_provider.get_model_name(),
        "initial_categories": [c.id for c in initial_response.categories],
        "initial_tier": initial_response.escalation_tier,
    })

    try:
        second_response = await retry_with_backoff(
            second_look_provider.analyze, request, call_timeout=120,
        )
    except Exception:
        logger.exception("[%s] Second-look analysis failed, keeping initial verdict", event_id)
        await audit_repo.log(
            event_id, "second_look_failed",
            {"reason": "provider_error"},
            status="error",
            error="Second-look provider call failed",
        )
        return initial_response

    agreed = second_response.should_escalate
    second_cat_ids = [c.id for c in second_response.categories]

    await audit_repo.log(event_id, "second_look_complete", {
        "provider": second_response.provider,
        "model": second_response.model,
        "second_categories": second_cat_ids,
        "second_tier": second_response.escalation_tier,
        "agreed": agreed,
        "cost_usd": second_response.estimated_cost_usd,
        "duration_s": second_response.processing_time_seconds,
    })

    second_look_meta = {
        "performed": True,
        "provider": second_response.provider,
        "model": second_response.model,
        "agreed": agreed,
        "categories": second_cat_ids,
        "tier": second_response.escalation_tier,
        "summary": second_response.summary,
        "reasoning": second_response.reasoning,
        "cost_usd": second_response.estimated_cost_usd,
    }

    if not agreed:
        # Second model disagrees — downgrade
        logger.info(
            "[%s] Second-look DISAGREES: initial_tier=%s, second_tier=%s -> downgrading",
            event_id, initial_response.escalation_tier, second_response.escalation_tier,
        )
        second_response.second_look = second_look_meta
        return second_response

    # Second model agrees — keep original verdict, annotate
    logger.info(
        "[%s] Second-look AGREES: tier=%s categories=%s -> keeping initial verdict",
        event_id, initial_response.escalation_tier, second_cat_ids,
    )
    initial_response.second_look = second_look_meta
    return initial_response
