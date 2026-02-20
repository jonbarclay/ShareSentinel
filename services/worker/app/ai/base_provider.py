"""Abstract base class for AI providers and shared data structures."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional


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
class AnalysisResponse:
    """Output from the AI provider."""

    sensitivity_rating: int  # 1-5
    categories_detected: List[str]
    summary: str
    confidence: str  # "high", "medium", "low"
    recommendation: str
    raw_response: str
    provider: str  # "anthropic", "openai", "gemini"
    model: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    processing_time_seconds: float
    success: bool = True
    error: Optional[str] = None


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
