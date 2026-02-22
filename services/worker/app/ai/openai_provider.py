"""OpenAI GPT provider implementation."""

import base64
import logging
import time
from typing import Dict, List

import openai

from .base_provider import AnalysisRequest, AnalysisResponse, BaseAIProvider
from .prompt_manager import SYSTEM_PROMPT, PromptManager
from .response_parser import parse_ai_response

logger = logging.getLogger(__name__)


class OpenAIProvider(BaseAIProvider):
    """AI provider backed by the OpenAI ChatCompletion API."""

    # Pricing per 1M tokens (update as pricing changes)
    PRICING: Dict[str, Dict[str, float]] = {
        "gpt-4o": {"input": 2.50, "output": 10.00},
        "gpt-4o-mini": {"input": 0.15, "output": 0.60},
        "gpt-5-nano": {"input": 0.10, "output": 0.40},
    }

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o",
        prompt_manager: PromptManager | None = None,
        max_tokens: int = 1024,
        temperature: float = 0,
    ) -> None:
        self.client = openai.AsyncOpenAI(api_key=api_key)
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

            kwargs: dict = dict(
                model=self.model,
                messages=messages,
                max_completion_tokens=self.max_tokens,
                response_format={"type": "json_object"},
            )
            if self.temperature:
                kwargs["temperature"] = self.temperature

            response = await self.client.chat.completions.create(**kwargs)

            processing_time = time.time() - start_time

            finish_reason = response.choices[0].finish_reason
            if finish_reason == "length":
                logger.warning(
                    "OpenAI response truncated (finish_reason=length, "
                    "max_completion_tokens=%d). Consider raising AI_MAX_TOKENS.",
                    self.max_tokens,
                )

            raw_text = response.choices[0].message.content or ""
            parsed = parse_ai_response(raw_text)

            input_tokens = response.usage.prompt_tokens if response.usage else 0
            output_tokens = response.usage.completion_tokens if response.usage else 0
            cost = self._calculate_cost(input_tokens, output_tokens)

            return AnalysisResponse(
                categories=parsed["categories"],
                context=parsed["context"],
                summary=parsed["summary"],
                recommendation=parsed["recommendation"],
                raw_response=raw_text,
                provider="openai",
                model=self.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=cost,
                processing_time_seconds=processing_time,
                affected_count=parsed.get("affected_count", 0),
                pii_types_found=parsed.get("pii_types_found", []),
            )
        except Exception as exc:
            logger.exception("OpenAI analysis failed")
            return AnalysisResponse(
                categories=[],
                context="mixed",
                summary="",
                recommendation="",
                raw_response="",
                provider="openai",
                model=self.model,
                input_tokens=0,
                output_tokens=0,
                estimated_cost_usd=0,
                processing_time_seconds=time.time() - start_time,
                success=False,
                error=str(exc),
            )

    def get_provider_name(self) -> str:
        return "openai"

    def get_model_name(self) -> str:
        return self.model

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_messages(self, request: AnalysisRequest) -> List[dict]:
        """Build the messages array for the OpenAI ChatCompletion API."""
        user_prompt = self.prompt_manager.render(request)

        system_msg = {"role": "system", "content": SYSTEM_PROMPT}

        if request.mode == "multimodal" and request.images:
            content: list = []
            mime_types = request.image_mime_types or ["image/jpeg"] * len(
                request.images
            )
            for img_bytes, mime_type in zip(request.images, mime_types):
                b64_data = base64.b64encode(img_bytes).decode("utf-8")
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{b64_data}",
                        },
                    }
                )
            content.append({"type": "text", "text": user_prompt})
            return [system_msg, {"role": "user", "content": content}]

        return [system_msg, {"role": "user", "content": user_prompt}]

    def _calculate_cost(self, input_tokens: int, output_tokens: int) -> float:
        pricing = self.PRICING.get(self.model, {"input": 0, "output": 0})
        return (input_tokens * pricing["input"] / 1_000_000) + (
            output_tokens * pricing["output"] / 1_000_000
        )
