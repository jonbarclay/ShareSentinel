"""Google Gemini provider implementation via Vertex AI REST API."""

import base64
import json
import logging
import time
from typing import Any, Dict, List

import httpx

from .base_provider import AnalysisRequest, AnalysisResponse, BaseAIProvider
from .prompt_manager import SYSTEM_PROMPT, PromptManager
from .response_parser import parse_ai_response

logger = logging.getLogger(__name__)


class GeminiProvider(BaseAIProvider):
    """AI provider backed by Google Gemini via the Vertex AI REST API with API key auth."""

    PRICING: Dict[str, Dict[str, float]] = {
        "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
        "gemini-2.5-pro-preview-05-06": {"input": 1.25, "output": 10.00},
        "gemini-2.5-flash": {"input": 0.15, "output": 0.60},
    }

    def __init__(
        self,
        api_key: str = "",
        model: str = "gemini-2.0-flash",
        prompt_manager: PromptManager | None = None,
        max_tokens: int = 1024,
        temperature: float = 0,
        project: str = "",
        location: str = "us-central1",
    ) -> None:
        self._base_url = (
            f"https://{location}-aiplatform.googleapis.com/v1/"
            f"projects/{project}/locations/{location}/publishers/google/models"
        )
        self._api_key = api_key
        self.model_name = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.prompt_manager = prompt_manager or PromptManager()
        self._http = httpx.AsyncClient(timeout=120)

    async def analyze(self, request: AnalysisRequest) -> AnalysisResponse:
        start_time = time.time()
        try:
            contents = self._build_contents(request)

            body: Dict[str, Any] = {
                "contents": contents,
                "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "generationConfig": {
                    "temperature": self.temperature,
                    "maxOutputTokens": self.max_tokens,
                    "responseMimeType": "application/json",
                },
            }

            url = f"{self._base_url}/{self.model_name}:generateContent?key={self._api_key}"
            resp = await self._http.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()

            processing_time = time.time() - start_time

            # Extract text from response
            raw_text = ""
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    raw_text = parts[0].get("text", "")

            parsed = parse_ai_response(raw_text)

            # Extract token counts
            usage = data.get("usageMetadata", {})
            input_tokens = usage.get("promptTokenCount", 0)
            output_tokens = usage.get("candidatesTokenCount", 0)
            cost = self._calculate_cost(input_tokens, output_tokens)

            return AnalysisResponse(
                categories=parsed["categories"],
                context=parsed["context"],
                summary=parsed["summary"],
                recommendation=parsed["recommendation"],
                raw_response=raw_text,
                provider="gemini",
                model=self.model_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=cost,
                processing_time_seconds=processing_time,
                affected_count=parsed.get("affected_count", 0),
                pii_types_found=parsed.get("pii_types_found", []),
            )
        except Exception as exc:
            logger.exception("Gemini analysis failed")
            return AnalysisResponse(
                categories=[],
                context="mixed",
                summary="",
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

    def _build_contents(self, request: AnalysisRequest) -> List[Dict]:
        """Build the contents array for the Vertex AI REST API."""
        user_prompt = self.prompt_manager.render(request)
        parts: list = []

        if request.mode == "multimodal" and request.images:
            mime_types = request.image_mime_types or ["image/jpeg"] * len(request.images)
            for img_bytes, mime_type in zip(request.images, mime_types):
                parts.append({
                    "inlineData": {
                        "mimeType": mime_type,
                        "data": base64.b64encode(img_bytes).decode("utf-8"),
                    }
                })
            parts.append({"text": user_prompt})
        else:
            parts.append({"text": user_prompt})

        return [{"role": "user", "parts": parts}]

    def _calculate_cost(self, input_tokens: int, output_tokens: int) -> float:
        pricing = self.PRICING.get(self.model_name, {"input": 0, "output": 0})
        return (input_tokens * pricing["input"] / 1_000_000) + (
            output_tokens * pricing["output"] / 1_000_000
        )
