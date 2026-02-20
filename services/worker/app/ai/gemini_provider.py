"""Google Gemini provider implementation."""

import io
import logging
import time
from typing import Dict, List

import google.generativeai as genai
from PIL import Image

from .base_provider import AnalysisRequest, AnalysisResponse, BaseAIProvider
from .prompt_manager import SYSTEM_PROMPT, PromptManager
from .response_parser import parse_ai_response

logger = logging.getLogger(__name__)


class GeminiProvider(BaseAIProvider):
    """AI provider backed by the Google Generative AI (Gemini) SDK."""

    # Pricing per 1M tokens (update as pricing changes)
    PRICING: Dict[str, Dict[str, float]] = {
        "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
        "gemini-2.5-pro-preview-05-06": {"input": 1.25, "output": 10.00},
    }

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.0-flash",
        prompt_manager: PromptManager | None = None,
        max_tokens: int = 1024,
        temperature: float = 0,
    ) -> None:
        genai.configure(api_key=api_key)
        self.genai_model = genai.GenerativeModel(
            model,
            system_instruction=SYSTEM_PROMPT,
            generation_config=genai.GenerationConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
                response_mime_type="application/json",
            ),
        )
        self.model_name = model
        self.prompt_manager = prompt_manager or PromptManager()

    # ------------------------------------------------------------------
    # BaseAIProvider interface
    # ------------------------------------------------------------------

    async def analyze(self, request: AnalysisRequest) -> AnalysisResponse:
        start_time = time.time()
        try:
            parts = self._build_parts(request)

            response = await self.genai_model.generate_content_async(parts)

            processing_time = time.time() - start_time

            raw_text = response.text or ""
            parsed = parse_ai_response(raw_text)

            # Extract token counts from usage metadata
            usage = getattr(response, "usage_metadata", None)
            input_tokens = getattr(usage, "prompt_token_count", 0) or 0
            output_tokens = getattr(usage, "candidates_token_count", 0) or 0
            cost = self._calculate_cost(input_tokens, output_tokens)

            return AnalysisResponse(
                sensitivity_rating=parsed["sensitivity_rating"],
                categories_detected=parsed["categories_detected"],
                summary=parsed["summary"],
                confidence=parsed["confidence"],
                recommendation=parsed["recommendation"],
                raw_response=raw_text,
                provider="gemini",
                model=self.model_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=cost,
                processing_time_seconds=processing_time,
            )
        except Exception as exc:
            logger.exception("Gemini analysis failed")
            return AnalysisResponse(
                sensitivity_rating=0,
                categories_detected=[],
                summary="",
                confidence="",
                recommendation="",
                raw_response="",
                provider="gemini",
                model=self.model_name,
                input_tokens=0,
                output_tokens=0,
                estimated_cost_usd=0,
                processing_time_seconds=time.time() - start_time,
                success=False,
                error=str(exc),
            )

    def get_provider_name(self) -> str:
        return "gemini"

    def get_model_name(self) -> str:
        return self.model_name

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_parts(self, request: AnalysisRequest) -> List:
        """Build the content parts for the Gemini API.

        For multimodal requests, images are converted to PIL Image objects
        which the Gemini SDK accepts natively.
        """
        user_prompt = self.prompt_manager.render(request)
        parts: list = []

        if request.mode == "multimodal" and request.images:
            for img_bytes in request.images:
                pil_image = Image.open(io.BytesIO(img_bytes))
                parts.append(pil_image)
            parts.append(user_prompt)
        else:
            parts.append(user_prompt)

        return parts

    def _calculate_cost(self, input_tokens: int, output_tokens: int) -> float:
        pricing = self.PRICING.get(self.model_name, {"input": 0, "output": 0})
        return (input_tokens * pricing["input"] / 1_000_000) + (
            output_tokens * pricing["output"] / 1_000_000
        )
