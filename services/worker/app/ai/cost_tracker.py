"""In-memory cost tracking for AI analysis calls.

Per-analysis cost data is also persisted to the ``verdicts`` PostgreSQL table
(see doc 07).  This module provides a lightweight in-memory aggregation layer
for quick access to running totals without querying the database.
"""

import logging
import threading
from dataclasses import dataclass
from typing import Dict, List

logger = logging.getLogger(__name__)


@dataclass
class CostRecord:
    """Single cost record for one AI analysis call."""

    provider: str
    model: str
    mode: str  # "text", "multimodal", "filename_only"
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    file_name: str = ""


class CostTracker:
    """Thread-safe in-memory cost tracker for AI analysis calls."""

    def __init__(self) -> None:
        self._records: List[CostRecord] = []
        self._lock = threading.Lock()

    def record(
        self,
        provider: str,
        model: str,
        mode: str,
        input_tokens: int,
        output_tokens: int,
        estimated_cost_usd: float,
        file_name: str = "",
    ) -> None:
        """Add a cost record for a completed analysis."""
        entry = CostRecord(
            provider=provider,
            model=model,
            mode=mode,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=estimated_cost_usd,
            file_name=file_name,
        )
        with self._lock:
            self._records.append(entry)
        logger.debug(
            "Recorded cost: provider=%s model=%s cost=$%.6f",
            provider,
            model,
            estimated_cost_usd,
        )

    def get_total_cost(self) -> float:
        """Return the total estimated cost across all recorded analyses."""
        with self._lock:
            return sum(r.estimated_cost_usd for r in self._records)

    def get_cost_by_provider(self) -> Dict[str, float]:
        """Return estimated cost grouped by provider name."""
        totals: Dict[str, float] = {}
        with self._lock:
            for r in self._records:
                totals[r.provider] = totals.get(r.provider, 0) + r.estimated_cost_usd
        return totals

    def get_cost_by_model(self) -> Dict[str, float]:
        """Return estimated cost grouped by model name."""
        totals: Dict[str, float] = {}
        with self._lock:
            for r in self._records:
                totals[r.model] = totals.get(r.model, 0) + r.estimated_cost_usd
        return totals

    def get_total_tokens(self) -> Dict[str, int]:
        """Return total input and output tokens across all analyses."""
        with self._lock:
            return {
                "input_tokens": sum(r.input_tokens for r in self._records),
                "output_tokens": sum(r.output_tokens for r in self._records),
            }

    def get_record_count(self) -> int:
        """Return the number of recorded analyses."""
        with self._lock:
            return len(self._records)

    def reset(self) -> None:
        """Clear all in-memory records."""
        with self._lock:
            self._records.clear()
