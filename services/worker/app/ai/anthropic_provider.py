"""Anthropic Claude provider implementation."""

import base64
import logging
import time
from typing import Dict, List

import anthropic

from .base_provider import AnalysisRequest, AnalysisResponse, BaseAIProvider
from .prompt_manager import SYSTEM_PROMPT, PromptManager
from .response_parser import parse_ai_response

logger = logging.getLogger(__name__)


class AnthropicProvider(BaseAIProvider):
    """AI provider backed by the Anthropic Messages API."""

    # Pricing per 1M tokens (update as pricing changes)
    PRICING: Dict[str, Dict[str, float]] = {
        "claude-sonnet-4-5-20250929": {"input": 3.00, "output": 15.00},
        "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    }

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-5-20250929",
        prompt_manager: PromptManager | None = None,
        max_tokens: int = 1024,
        temperature: float = 0,
    ) -> None:
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model
        self.prompt_manager = prompt_manager or PromptManager()
        self.max_tokens = max_tokens
        self.temperature = temperature

    # ------------------------------------------------------------------
    # BaseAIProvider interface
    # ------------------------------------------------------------------

    async def analyze(self, request: AnalysisRequest) -> AnalysisResponse:
        start_time = time.time()
        try:
            messages = self._build_messages(request)

            response = await self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                system=SYSTEM_PROMPT,
                messages=messages,
            )

            processing_time = time.time() - start_time

            raw_text = response.content[0].text
            parsed = parse_ai_response(raw_text)

            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            cost = self._calculate_cost(input_tokens, output_tokens)

            return AnalysisResponse(
                sensitivity_rating=parsed["sensitivity_rating"],
                categories_detected=parsed["categories_detected"],
                summary=parsed["summary"],
                confidence=parsed["confidence"],
                recommendation=parsed["recommendation"],
                raw_response=raw_text,
                provider="anthropic",
                model=self.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=cost,
                processing_time_seconds=processing_time,
            )
        except Exception as exc:
            logger.exception("Anthropic analysis failed")
            return AnalysisResponse(
                sensitivity_rating=0,
                categories_detected=[],
                summary="",
                confidence="",
                recommendation="",
                raw_response="",
                provider="anthropic",
                model=self.model,
                input_tokens=0,
                output_tokens=0,
                estimated_cost_usd=0,
                processing_time_seconds=time.time() - start_time,
                success=False,
                error=str(exc),
            )

    def get_provider_name(self) -> str:
        return "anthropic"

    def get_model_name(self) -> str:
        return self.model

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_messages(self, request: AnalysisRequest) -> List[dict]:
        """Build the messages array for the Anthropic Messages API."""
        user_prompt = self.prompt_manager.render(request)

        if request.mode == "multimodal" and request.images:
            content: list = []
            mime_types = request.image_mime_types or ["image/jpeg"] * len(
                request.images
            )
            for img_bytes, mime_type in zip(request.images, mime_types):
                content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": base64.b64encode(img_bytes).decode("utf-8"),
                        },
                    }
                )
            content.append({"type": "text", "text": user_prompt})
            return [{"role": "user", "content": content}]

        return [{"role": "user", "content": user_prompt}]

    def _calculate_cost(self, input_tokens: int, output_tokens: int) -> float:
        pricing = self.PRICING.get(self.model, {"input": 0, "output": 0})
        return (input_tokens * pricing["input"] / 1_000_000) + (
            output_tokens * pricing["output"] / 1_000_000
        )
