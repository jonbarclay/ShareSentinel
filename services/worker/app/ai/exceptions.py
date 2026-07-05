"""AI provider exception hierarchy."""


class TransientAIError(Exception):
    """Raised by AI providers for retryable errors (rate limits, server errors, timeouts).

    When raised inside ``analyze()``, ``retry_with_backoff`` will catch and retry.
    If retries are exhausted, the error propagates to the orchestrator for
    event-level requeue handling.
    """


class AIAnalysisFailedError(TransientAIError):
    """The provider returned a non-retryable failure (auth error, bad request)
    or produced output that could not be parsed.

    Subclasses TransientAIError so the orchestrator's bounded requeue path
    handles it: a few retries, then the event is marked *failed* — never
    recorded as a clean 'none' verdict. A file the system cannot analyze is an
    unknown, not a pass.
    """
