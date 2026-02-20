"""Interactive browser authentication session for org-wide SharePoint screenshots.

Launches a Playwright browser that streams JPEG frames over WebSocket,
allowing an analyst to complete Microsoft SSO interactively.  Once
authenticated the browser storage state (cookies + local-storage) is
persisted to Redis so that subsequent headless screenshot requests in
browser_fetcher.py can access org-wide links.
"""

import asyncio
import json
import logging
import time
from urllib.parse import urlparse

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

_AUTH_STATE_KEY = "ss:browser_auth_state"
_AUTH_STATE_TTL = 8 * 3600  # 8 hours
_INACTIVITY_TIMEOUT = 5 * 60  # 5 minutes
_FRAME_INTERVAL = 0.2  # ~200 ms between screenshots

_VIEWPORT_WIDTH = 1920
_VIEWPORT_HEIGHT = 1080

# Only allow navigation to trusted Microsoft domains
_ALLOWED_DOMAINS = {
    "sharepoint.com",
    "microsoft.com",
    "microsoftonline.com",
    "office.com",
    "live.com",
    "officeppe.com",
    # Auth CDN / iframe domains used by the modern login flow
    "msftauth.net",
    "msauth.net",
    "msauthimages.net",
    "msecnd.net",
    "msftauthimages.net",
}


# ── URL helpers ────────────────────────────────────────────────────────────

def _is_allowed_url(url: str) -> bool:
    try:
        host = urlparse(url).hostname
        if not host:
            return False
        host = host.lower()
        return any(host == d or host.endswith(f".{d}") for d in _ALLOWED_DOMAINS)
    except Exception:
        return False


def _is_auth_page(url: str) -> bool:
    """Return True when the browser is still on a Microsoft login page."""
    try:
        host = urlparse(url).hostname
        if not host:
            return True
        host = host.lower()
        return (
            host.endswith(".microsoftonline.com")
            or host.endswith(".login.microsoft.com")
            or host == "login.microsoftonline.com"
            or host == "login.microsoft.com"
        )
    except Exception:
        return True


# ── BrowserAuthSession ─────────────────────────────────────────────────────

class BrowserAuthSession:
    """Manages a single interactive Playwright browser for SSO login."""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._last_input_time: float = time.monotonic()
        self._saw_login_page: bool = False

    async def create(self, sharepoint_url: str) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=[
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-extensions",
            ],
        )
        self._context = await self._browser.new_context(
            viewport={"width": _VIEWPORT_WIDTH, "height": _VIEWPORT_HEIGHT},
            device_scale_factor=1,
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        # Only intercept top-level navigations — allow subresources (JS, CSS,
        # images, fonts) from any CDN so pages actually render.
        await self._context.route("**/*", self._route_handler)

        self._page = await self._context.new_page()

        # Hide headless indicators that Microsoft's login page checks.
        # Also stub WebAuthn — headless Chromium has no authenticator, so
        # navigator.credentials.get() hangs forever.  Rejecting immediately
        # with NotAllowedError mimics the user cancelling the FIDO prompt,
        # which makes the login page fall back to password / authenticator app.
        await self._page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

            if (navigator.credentials) {
                const _origGet = navigator.credentials.get.bind(navigator.credentials);
                navigator.credentials.get = function(opts) {
                    if (opts && opts.publicKey) {
                        return Promise.reject(new DOMException(
                            'The operation either timed out or was not allowed.',
                            'NotAllowedError'
                        ));
                    }
                    return _origGet(opts);
                };
                const _origCreate = navigator.credentials.create.bind(navigator.credentials);
                navigator.credentials.create = function(opts) {
                    if (opts && opts.publicKey) {
                        return Promise.reject(new DOMException(
                            'The operation either timed out or was not allowed.',
                            'NotAllowedError'
                        ));
                    }
                    return _origCreate(opts);
                };
            }
        """)


        logger.info("BrowserAuthSession: navigating to %s", sharepoint_url)
        await self._page.goto(sharepoint_url, wait_until="domcontentloaded", timeout=30_000)
        self._last_input_time = time.monotonic()

    # ── Route interception ─────────────────────────────────────────────

    async def _route_handler(self, route):
        # Only gate top-level document navigations; let subresources
        # (scripts, stylesheets, images, fonts, XHR, fetch) through so
        # the page renders properly.
        if route.request.resource_type != "document":
            await route.continue_()
            return
        url = route.request.url
        if _is_allowed_url(url):
            await route.continue_()
        else:
            logger.debug("BrowserAuthSession: blocked document navigation to %s", url)
            await route.abort("blockedbyclient")

    # ── Screenshot ─────────────────────────────────────────────────────

    async def get_screenshot(self) -> bytes:
        return await self._page.screenshot(type="jpeg", quality=70)

    # ── Input dispatch ─────────────────────────────────────────────────

    async def dispatch_click(self, x: float, y: float, button: str = "left") -> None:
        await self._page.mouse.click(x, y, button=button)
        self._last_input_time = time.monotonic()

    async def dispatch_dblclick(self, x: float, y: float) -> None:
        await self._page.mouse.dblclick(x, y)
        self._last_input_time = time.monotonic()

    async def dispatch_key(self, key: str) -> None:
        await self._page.keyboard.press(key)
        self._last_input_time = time.monotonic()

    async def dispatch_type(self, text: str) -> None:
        await self._page.keyboard.type(text)
        self._last_input_time = time.monotonic()

    async def dispatch_scroll(self, x: float, y: float, delta_y: float) -> None:
        await self._page.mouse.move(x, y)
        await self._page.mouse.wheel(0, delta_y)
        self._last_input_time = time.monotonic()

    # ── Auth state ─────────────────────────────────────────────────────

    def check_auth_complete(self) -> bool:
        url = self._page.url
        if _is_auth_page(url):
            self._saw_login_page = True
            return False
        # Auth is complete only if we went through a login page first
        # and are now on a SharePoint/Office page (not the initial load).
        return self._saw_login_page

    async def save_auth_state(self, redis) -> None:
        state = await self._context.storage_state()
        # storage_state() returns a dict — serialize to JSON string for Redis
        await redis.set(_AUTH_STATE_KEY, json.dumps(state), ex=_AUTH_STATE_TTL)
        logger.info("BrowserAuthSession: auth state saved to Redis (TTL %ds)", _AUTH_STATE_TTL)

    @property
    def last_input_time(self) -> float:
        return self._last_input_time

    @property
    def current_url(self) -> str:
        return self._page.url if self._page else ""

    # ── Teardown ───────────────────────────────────────────────────────

    async def close(self) -> None:
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None
        logger.info("BrowserAuthSession: closed")


# ── Module-level session management ────────────────────────────────────────

_auth_session: BrowserAuthSession | None = None
_session_lock = asyncio.Lock()


async def start_session(sharepoint_url: str) -> BrowserAuthSession:
    """Create a new browser auth session. Raises RuntimeError if one is active."""
    global _auth_session
    async with _session_lock:
        if _auth_session is not None:
            raise RuntimeError("A browser auth session is already active")
        session = BrowserAuthSession()
        await session.create(sharepoint_url)
        _auth_session = session
        return session


async def close_session(redis) -> bool:
    """Close the active session. Saves auth state if login completed.

    Returns True if auth state was saved, False otherwise.
    """
    global _auth_session
    async with _session_lock:
        if _auth_session is None:
            return False
        saved = False
        try:
            if _auth_session.check_auth_complete():
                await _auth_session.save_auth_state(redis)
                saved = True
        except Exception:
            logger.exception("BrowserAuthSession: failed to save auth state")
        await _auth_session.close()
        _auth_session = None
        return saved


def get_or_none() -> BrowserAuthSession | None:
    """Return the active session or None (non-blocking)."""
    return _auth_session


async def get_auth_state_status(redis) -> dict:
    """Check whether a saved browser auth state exists in Redis."""
    ttl = await redis.ttl(_AUTH_STATE_KEY)
    if ttl and ttl > 0:
        return {"authenticated": True, "expires_in_seconds": ttl}
    return {"authenticated": False, "expires_in_seconds": 0}


# ── WebSocket handler ──────────────────────────────────────────────────────

async def handle_browser_stream(websocket, redis) -> None:
    """Stream JPEG frames and accept input events over a WebSocket."""
    await websocket.accept()

    session = get_or_none()
    if session is None:
        await websocket.send_json({"error": "No active browser session"})
        await websocket.close(code=4002, reason="No active browser session")
        return

    auth_saved = False
    screenshot_task = None

    async def _screenshot_loop():
        """Send JPEG frames + status messages at regular intervals."""
        nonlocal auth_saved
        try:
            while True:
                s = get_or_none()
                if s is None:
                    break

                # Check inactivity timeout
                if time.monotonic() - s.last_input_time > _INACTIVITY_TIMEOUT:
                    await websocket.send_json({"type": "timeout", "reason": "inactivity"})
                    break

                # Send screenshot frame (binary)
                try:
                    frame = await s.get_screenshot()
                    await websocket.send_bytes(frame)
                except Exception:
                    break

                # Send status (JSON)
                auth_complete = s.check_auth_complete()
                await websocket.send_json({
                    "type": "status",
                    "url": s.current_url,
                    "auth_complete": auth_complete,
                })

                # Auto-save on auth completion
                if auth_complete and not auth_saved:
                    try:
                        await s.save_auth_state(redis)
                        auth_saved = True
                        await websocket.send_json({
                            "type": "auth_saved",
                            "message": "Authentication state saved",
                        })
                    except Exception:
                        logger.exception("Auto-save auth state failed")

                await asyncio.sleep(_FRAME_INTERVAL)
        except Exception:
            pass

    screenshot_task = asyncio.create_task(_screenshot_loop())

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            await _dispatch_input(session, msg)
    except Exception:
        # WebSocket closed or error
        pass
    finally:
        screenshot_task.cancel()
        try:
            await screenshot_task
        except asyncio.CancelledError:
            pass


async def _dispatch_input(session: BrowserAuthSession, msg: dict) -> None:
    """Validate and forward an input event to the browser session."""
    kind = msg.get("type")
    if not kind:
        return

    if kind == "click":
        x = max(0.0, min(float(msg.get("x", 0)), _VIEWPORT_WIDTH))
        y = max(0.0, min(float(msg.get("y", 0)), _VIEWPORT_HEIGHT))
        button = msg.get("button", "left")
        if button not in ("left", "right", "middle"):
            button = "left"
        await session.dispatch_click(x, y, button)

    elif kind == "dblclick":
        x = max(0.0, min(float(msg.get("x", 0)), _VIEWPORT_WIDTH))
        y = max(0.0, min(float(msg.get("y", 0)), _VIEWPORT_HEIGHT))
        await session.dispatch_dblclick(x, y)

    elif kind == "keypress":
        key = str(msg.get("key", ""))[:50]
        if key:
            await session.dispatch_key(key)

    elif kind == "type":
        text = str(msg.get("text", ""))[:500]
        if text:
            await session.dispatch_type(text)

    elif kind == "scroll":
        x = max(0.0, min(float(msg.get("x", 0)), _VIEWPORT_WIDTH))
        y = max(0.0, min(float(msg.get("y", 0)), _VIEWPORT_HEIGHT))
        delta_y = max(-1000.0, min(float(msg.get("deltaY", 0)), 1000.0))
        await session.dispatch_scroll(x, y, delta_y)
