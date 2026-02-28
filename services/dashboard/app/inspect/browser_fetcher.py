"""Headless browser screenshot fallback for content inspection.

When Graph API strategies fail (HTTP 500 for PDF, 406 for thumbnails),
navigate to the item's sharing URL in headless Chromium and capture a
screenshot for multimodal AI analysis.

Phase 1 scope: anonymous sharing links only.  Company-wide links that
require authentication are skipped.
"""

import asyncio
import json
import logging
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Browser

logger = logging.getLogger(__name__)

# Singleton browser state
_playwright_instance = None
_browser: Browser | None = None
_browser_lock = asyncio.Lock()

# Redis client for auth state loading (set during lifespan startup)
_redis = None


def set_redis(redis_client):
    """Set the Redis client for auth state loading."""
    global _redis
    _redis = redis_client


async def _load_auth_state() -> dict | None:
    """Load saved browser auth state from Redis, if available."""
    if _redis is None:
        return None
    try:
        raw = await _redis.get("ss:browser_auth_state")
        if raw is None:
            return None
        return json.loads(raw)
    except Exception:
        logger.debug("Failed to load browser auth state from Redis")
        return None


# Only navigate to trusted Microsoft domains
_ALLOWED_DOMAINS = {
    "sharepoint.com",
    "microsoft.com",
    "office.com",
    "live.com",
    "officeppe.com",
}


def _is_allowed_url(url: str) -> bool:
    """Check that the URL resolves to a trusted Microsoft domain."""
    try:
        host = urlparse(url).hostname
        if not host:
            return False
        host = host.lower()
        return any(host == d or host.endswith(f".{d}") for d in _ALLOWED_DOMAINS)
    except Exception:
        return False


async def _get_browser() -> Browser:
    """Return the singleton Chromium instance, launching if needed."""
    global _playwright_instance, _browser

    async with _browser_lock:
        if _browser and _browser.is_connected():
            return _browser

        _playwright_instance = await async_playwright().start()
        _browser = await _playwright_instance.chromium.launch(
            headless=True,
            args=[
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-extensions",
            ],
        )
        logger.info("Playwright: launched headless Chromium")
        return _browser


async def take_screenshot(
    url: str,
    dest_path: Path,
    timeout_ms: int = 30_000,
) -> bool:
    """Navigate to *url* in a fresh browser context and save a screenshot.

    The screenshot is resized and JPEG-compressed for multimodal AI input.

    Returns True on success, False on failure.
    """
    if not _is_allowed_url(url):
        logger.warning("Browser screenshot: URL rejected by allowlist: %s", url)
        return False

    context = None
    try:
        browser = await _get_browser()

        # Load auth state if available (enables org-wide link screenshots)
        context_kwargs = {
            "viewport": {"width": 1920, "height": 1080},
            "device_scale_factor": 1,
            "locale": "en-US",
        }
        auth_state = await _load_auth_state()
        if auth_state:
            context_kwargs["storage_state"] = auth_state
            logger.info("Browser screenshot: using saved auth state")

        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()

        logger.info("Browser screenshot: navigating to %s", url)
        await page.goto(url, wait_until="networkidle", timeout=timeout_ms)

        # Extra wait for JavaScript rendering
        await page.wait_for_timeout(2000)

        # Dismiss common Microsoft overlay dialogs
        await _dismiss_overlays(page)

        # Short pause after dismissals for re-render
        await page.wait_for_timeout(500)

        # Take PNG screenshot to a temp location, then preprocess
        raw_bytes = await page.screenshot(full_page=False, type="png")

        # Preprocess: resize and JPEG-compress
        from .processor import _preprocess_image_bytes

        processed = _preprocess_image_bytes(raw_bytes)

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(processed)
        logger.info(
            "Browser screenshot saved: %s (%d bytes)", dest_path, len(processed)
        )
        return True

    except Exception:
        logger.exception("Browser screenshot failed for %s", url)
        return False
    finally:
        if context:
            try:
                await context.close()
            except Exception:
                pass


async def _dismiss_overlays(page) -> None:
    """Click away common Microsoft cookie/sign-in overlays."""
    selectors = [
        # Cookie consent buttons
        "button#onetrust-accept-btn-handler",
        "button[id*='accept']",
        "button[aria-label*='Accept']",
        "button[aria-label*='accept']",
        # "Got it" / "OK" banners
        "button:has-text('Got it')",
        "button:has-text('OK')",
        # Close buttons on sign-in prompts
        "button[aria-label='Close']",
        "button[aria-label='Dismiss']",
    ]
    for selector in selectors:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=300):
                await btn.click(timeout=1000)
                await page.wait_for_timeout(300)
        except Exception:
            pass


async def close_browser() -> None:
    """Shut down the singleton browser (called during lifespan cleanup)."""
    global _playwright_instance, _browser

    async with _browser_lock:
        if _browser:
            try:
                await _browser.close()
            except Exception:
                pass
            _browser = None
        if _playwright_instance:
            try:
                await _playwright_instance.stop()
            except Exception:
                pass
            _playwright_instance = None
        logger.info("Playwright: browser shut down")
