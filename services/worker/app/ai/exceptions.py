"""AI provider exception hierarchy."""


class TransientAIError(Exception):
    """Raised by AI providers for retryable errors (rate limits, server errors, timeouts).

    When raised inside ``analyze()``, ``retry_with_backoff`` will catch and retry.
    If retries are exhausted, the error propagates to the orchestrator for
    event-level requeue handling.
    """
