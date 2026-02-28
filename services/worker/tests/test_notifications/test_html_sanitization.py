"""Tests for HTML sanitization of AI-generated email bodies."""

import bleach

from app.notifications.user_notifier import _SAFE_TAGS, _SAFE_ATTRS, _CSS_SANITIZER


class TestBleachAllowlist:
    """Verify the bleach allowlist strips dangerous tags while keeping safe ones."""

    def _clean(self, html: str) -> str:
        return bleach.clean(html, tags=_SAFE_TAGS, attributes=_SAFE_ATTRS, css_sanitizer=_CSS_SANITIZER, strip=True)

    def test_strips_script_tags(self):
        result = self._clean('<p>Hello</p><script>alert("xss")</script>')
        assert "<script>" not in result
        assert "</script>" not in result
        assert "<p>Hello</p>" in result

    def test_strips_img_tags(self):
        result = self._clean('<img src="x" onerror="alert(1)">')
        assert "<img" not in result
        assert "onerror" not in result

    def test_strips_iframe(self):
        result = self._clean('<iframe src="https://evil.com"></iframe>')
        assert "<iframe" not in result

    def test_strips_event_handlers(self):
        result = self._clean('<p onclick="alert(1)">Click me</p>')
        assert "onclick" not in result
        assert "<p>" in result

    def test_preserves_safe_tags(self):
        html = "<p>Hello <strong>world</strong>, <em>important</em>!</p>"
        result = self._clean(html)
        assert result == html

    def test_preserves_lists(self):
        html = "<ul><li>Item 1</li><li>Item 2</li></ul>"
        result = self._clean(html)
        assert result == html

    def test_preserves_headings(self):
        for tag in ("h2", "h3", "h4"):
            html = f"<{tag}>Title</{tag}>"
            result = self._clean(html)
            assert result == html

    def test_preserves_span_with_style(self):
        html = '<span style="color: red;">Warning</span>'
        result = self._clean(html)
        assert 'style="color: red;"' in result

    def test_strips_span_with_onclick(self):
        result = self._clean('<span onclick="evil()">text</span>')
        assert "onclick" not in result
        assert "<span>" in result

    def test_strips_link_tags(self):
        result = self._clean('<a href="https://evil.com">Click</a>')
        assert "<a" not in result
        assert "Click" in result

    def test_strips_form_tags(self):
        result = self._clean('<form action="/steal"><input type="text"></form>')
        assert "<form" not in result
        assert "<input" not in result

    def test_strips_style_tags(self):
        result = self._clean("<style>body{display:none}</style><p>Content</p>")
        assert "<style>" not in result
        assert "<p>Content</p>" in result

    def test_complex_xss_payload(self):
        payload = (
            '<p>Normal text</p>'
            '<img src=x onerror=alert(1)>'
            '<svg onload=alert(1)>'
            '<script>document.cookie</script>'
            '"><script>alert(1)</script>'
        )
        result = self._clean(payload)
        assert "<script>" not in result
        assert "<img" not in result
        assert "<svg" not in result
        assert "onerror" not in result
        assert "onload" not in result
        assert "<p>Normal text</p>" in result
