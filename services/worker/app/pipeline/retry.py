"""Retry utility with exponential backoff for external calls."""

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional, Sequence, Type, TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_RETRYABLE: tuple[Type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    httpx.TimeoutException,
)


async def retry_with_backoff(
    func: Callable[..., Awaitable[T]],
    *args: Any,
    max_retries: int = 3,
    base_delay: float = 2.0,
    retryable_exceptions: Optional[Sequence[Type[BaseException]]] = None,
    **kwargs: Any,
) -> T:
    """Execute *func* with exponential-backoff retries on transient failures.

    Delay schedule: base_delay * (2 ** attempt) -> 2s, 4s, 8s by default.

    Args:
        func: Async callable to invoke.
        *args: Positional arguments forwarded to *func*.
        max_retries: Total number of attempts (including the first).
        base_delay: Base delay in seconds before the first retry.
        retryable_exceptions: Exception types that trigger a retry.
            Defaults to ConnectionError, TimeoutError, httpx.TimeoutException.
        **kwargs: Keyword arguments forwarded to *func*.

    Returns:
        The return value of *func* on success.

    Raises:
        The last exception encountered after all retries are exhausted.
    """
    catchable = tuple(retryable_exceptions) if retryable_exceptions else DEFAULT_RETRYABLE

    last_exc: BaseException | None = None
    for attempt in range(max_retries):
        try:
            return await func(*args, **kwargs)
        except catchable as exc:
            last_exc = exc
            if attempt == max_retries - 1:
                logger.error(
                    "All %d retries exhausted for %s: %s",
                    max_retries,
                    func.__qualname__,
                    exc,
                )
                raise
            delay = base_delay * (2 ** attempt)
            logger.warning(
                "Retry %d/%d for %s after %.1fs: %s",
                attempt + 1,
                max_retries,
                func.__qualname__,
                delay,
                exc,
            )
            await asyncio.sleep(delay)

    # Unreachable, but keeps type checkers happy
    raise last_exc  # type: ignore[misc]
