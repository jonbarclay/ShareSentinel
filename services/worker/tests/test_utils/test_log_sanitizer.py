"""Tests for log sanitization utility."""

from app.utils.log_sanitizer import sanitize_response_body


class TestSanitizeResponseBody:
    """Verify sensitive data is redacted from response bodies."""

    def test_redacts_bearer_token(self):
        text = 'Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.long.token'
        result = sanitize_response_body(text, max_length=500)
        assert "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9" not in result
        assert "Bearer [REDACTED]" in result

    def test_redacts_access_token_json(self):
        text = '{"access_token": "secret-token-value", "expires_in": 3600}'
        result = sanitize_response_body(text, max_length=500)
        assert "secret-token-value" not in result
        assert '"access_token": "[REDACTED]"' in result

    def test_redacts_refresh_token_json(self):
        text = '{"refresh_token": "refresh-secret", "token_type": "bearer"}'
        result = sanitize_response_body(text, max_length=500)
        assert "refresh-secret" not in result
        assert '"refresh_token": "[REDACTED]"' in result

    def test_redacts_client_secret(self):
        text = '{"client_secret": "my-app-secret", "client_id": "abc"}'
        result = sanitize_response_body(text, max_length=500)
        assert "my-app-secret" not in result

    def test_redacts_password_field(self):
        text = '{"password": "hunter2", "username": "admin"}'
        result = sanitize_response_body(text, max_length=500)
        assert "hunter2" not in result

    def test_truncates_long_text(self):
        text = "x" * 500
        result = sanitize_response_body(text, max_length=200)
        assert len(result) < 250  # 200 + truncation marker
        assert "...[truncated]" in result

    def test_preserves_short_safe_text(self):
        text = '{"error": "not_found", "message": "Item does not exist"}'
        result = sanitize_response_body(text, max_length=200)
        assert result == text

    def test_empty_string(self):
        assert sanitize_response_body("") == ""

    def test_default_max_length(self):
        text = "a" * 300
        result = sanitize_response_body(text)
        assert len(result) < 250

    def test_multiple_tokens_in_one_response(self):
        text = (
            'Bearer abc123 and also Bearer def456 '
            'with {"access_token": "ghi789"}'
        )
        result = sanitize_response_body(text, max_length=500)
        assert "abc123" not in result
        assert "def456" not in result
        assert "ghi789" not in result
