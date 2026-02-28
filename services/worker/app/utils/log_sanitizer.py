"""Sanitize sensitive data from log messages.

Redacts Bearer tokens, API keys, and other secrets that may appear in
error response bodies before they are written to logs.
"""

import re

# Patterns that indicate sensitive values
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)
_SENSITIVE_JSON_RE = re.compile(
    r'"(access_token|refresh_token|id_token|client_secret|api_key|password|authorization)"'
    r'\s*:\s*"[^"]*"',
    re.IGNORECASE,
)


def sanitize_response_body(text: str, max_length: int = 200) -> str:
    """Truncate and redact sensitive content from an HTTP response body.

    Parameters
    ----------
    text:
        Raw response body (may contain tokens, secrets, PII).
    max_length:
        Maximum length of the returned string.

    Returns
    -------
    str
        Sanitized, truncated string safe for logging.
    """
    if not text:
        return ""

    # Redact Bearer tokens
    sanitized = _BEARER_RE.sub("Bearer [REDACTED]", text)

    # Redact sensitive JSON fields
    sanitized = _SENSITIVE_JSON_RE.sub(
        lambda m: f'"{m.group(1)}": "[REDACTED]"', sanitized
    )

    # Truncate
    if len(sanitized) > max_length:
        return sanitized[:max_length] + "...[truncated]"
    return sanitized
