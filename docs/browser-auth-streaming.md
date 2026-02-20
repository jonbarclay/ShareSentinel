# Browser Authentication Streaming for Inspection Queue

## Problem Statement

When Graph API strategies fail for Whiteboards, Loop, and OneNote items (HTTP 500 for PDF conversion, 406 for thumbnails), we fall back to Playwright headless browser screenshots of the sharing URL. This works for **anonymous sharing links**, but **company-wide (org) sharing links** render Microsoft's sign-in page instead of the actual content because the headless browser has no Microsoft session.

We need a way to authenticate the Playwright browser so it can render content behind org-wide sharing links.

## Current State (as of 2026-02-28)

### What works now
- `browser_fetcher.py` — Playwright singleton browser, URL allowlisting, isolated contexts, image preprocessing
- `processor.py` — browser screenshot as last-resort fallback after all Graph API strategies fail in `_process_whiteboard`, `_process_loop`, `_process_onenote`
- Anonymous sharing links render correctly and get multimodal AI analysis
- Org-wide sharing links render the Microsoft sign-in page (AI correctly identifies as "sign-in UI" — no false positives, but no useful analysis)

### What the dashboard auth already provides
- OIDC login via Entra ID (`auth.py`)
- Graph API delegated tokens (access + refresh) stored in Redis session (`auth.py:361-363`)
- Token refresh working via `auth_graph.py:63-85`
- Current scopes: `openid profile email offline_access https://graph.microsoft.com/Notes.Read.All https://graph.microsoft.com/Files.Read.All`

### Verified test results from initial browser fallback deployment
| Item | Graph API Result | Browser Fallback | AI Verdict |
|------|-----------------|-----------------|------------|
| Meeting Whiteboard 1.whiteboard | PDF 500, thumbnail 406 | Screenshot captured | "sign-in UI with placeholder content" |
| ENGR+2010 whiteboard | PDF 500, thumbnail 406 | Screenshot captured | "campus sign-in page" |
| MATH College Algebra whiteboard | PDF 500, thumbnail 406 | Screenshot captured | AI parse error (screenshot worked) |
| Voting table 2.loop | Graph API text extraction worked | N/A | "group project table, 5 names" |

---

## Options Evaluated

### Option A: WebSocket Browser Streaming (RECOMMENDED)

Stream the Playwright browser's screen to a `<canvas>` in the dashboard via WebSocket. User logs into Microsoft once in this virtual browser, cookies persist for the session, then the entire queue processes authenticated.

**Why this is the best option:**
- Guaranteed to work with any Entra ID configuration (MFA, conditional access, device compliance)
- Handles the full Microsoft auth flow naturally — the user does exactly what they'd do in a normal browser
- Playwright's `context.storage_state()` captures all cookies and localStorage for reuse
- Proven pattern used by Browserless.io, Playwright trace viewer, and RPA tools
- Session cookies typically last hours, so one login covers an entire batch processing session

### Option B: PRT (Primary Refresh Token) Injection — NOT VIABLE

The PRT is a device-level credential managed by the OS token broker (Windows WAM / macOS keychain) on domain-joined machines. A headless Chromium in a Docker container has no device identity — there is no PRT to extract or inject. Dead end.

### Option C: iframe Embedding of Login Page — NOT VIABLE

Microsoft login pages (`login.microsoftonline.com`) set `X-Frame-Options: DENY`. The browser will refuse to render them in an iframe. Dead end.

### Option D: MSAL Silent Auth in Playwright — UNLIKELY TO WORK

Use the existing refresh token to get a SharePoint-scoped token, then navigate Playwright to the OAuth authorize endpoint with `prompt=none` and `login_hint={email}`.

**Why this fails:** `prompt=none` requires existing Azure AD session cookies (`ESTSAUTH`/`ESTSAUTHPERSISTENT`) already present in the browser. Playwright's fresh context has none, so Azure AD returns `interaction_required`. Would also fail with MFA/conditional access policies.

### Option E: OAuth Token to SharePoint Cookie Exchange — NO RELIABLE METHOD

Investigated exchanging an OAuth access token for SharePoint `FedAuth`/`rtFa` cookies server-side:
- SharePoint REST API endpoints (`_api/contextinfo`, etc.) accept Bearer tokens but don't return browser session cookies in the response
- The `_forms/default.aspx` WS-Federation endpoint expects SAML assertions, not OAuth tokens
- The `_trust/` endpoint is for ADFS federation, not OAuth
- SharePoint's browser auth flow requires redirect-based cookie setting through `login.microsoftonline.com`, which needs interactive browser participation

There is no documented or reliable way to convert an OAuth access token into SharePoint browser session cookies server-side.

### Option F: Popup Redirect with Cookie Transfer — NOT VIABLE

Have the user log into Microsoft in a regular browser popup, then transfer cookies to Playwright. This fails because:
- Browser security prevents reading cross-origin cookies from Microsoft domains
- No browser API allows extracting httpOnly cookies from third-party domains
- Would require a browser extension, which is impractical

---

## Option A: Detailed Design

### Architecture Overview

```
 Dashboard Frontend                    Dashboard Backend (FastAPI)
+------------------------+           +-----------------------------+
|                        |  WebSocket |                             |
| [Auth Modal]           |<=========>| /api/inspect/browser-session|
|  <canvas> element      |  Binary   |                             |
|  renders screenshots   |  frames + |  Playwright persistent      |
|  forwards mouse/kbd    |  input    |  browser context            |
|  events                |  events   |                             |
+------------------------+           +-----------------------------+
                                              |
                                              | Saves storage_state
                                              v
                                     +------------------+
                                     | Redis            |
                                     | ss:browser_auth  |
                                     | (cookies + LS)   |
                                     +------------------+
                                              |
                                              | Loaded by take_screenshot()
                                              v
                                     +------------------+
                                     | browser_fetcher  |
                                     | Screenshot with  |
                                     | auth cookies     |
                                     +------------------+
```

### User Flow

1. User navigates to Inspection Queue page on the dashboard
2. User clicks **"Authenticate Inspection Browser"** button
3. A modal opens showing a canvas element (the virtual browser viewport)
4. Backend creates a Playwright persistent context and navigates to `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize?...`
5. The Microsoft login page renders in the virtual browser; screenshots stream to the canvas at ~5 fps via WebSocket binary frames
6. User clicks on the canvas to interact — coordinates mapped and forwarded to Playwright via WebSocket
7. User types credentials, completes MFA — keyboard events forwarded to Playwright
8. Microsoft auth completes, browser redirects to SharePoint/target page
9. Backend detects auth success (URL no longer on `login.microsoftonline.com`), saves `context.storage_state()` to Redis
10. Frontend shows success state, user closes modal
11. User clicks "Process Next Batch" — all `take_screenshot()` calls now load the saved storage state, rendering actual content instead of sign-in pages
12. Auth session lasts until Microsoft cookies expire (typically several hours)

### Backend Components

#### 1. WebSocket Endpoint: `/api/inspect/browser-session`

```
POST /api/inspect/browser-session/start   — Create auth session
WS   /api/inspect/browser-session/stream  — Bidirectional WebSocket
POST /api/inspect/browser-session/close   — Close auth session
GET  /api/inspect/browser-session/status  — Check if valid auth state exists
```

**WebSocket protocol:**
- **Server → Client (binary):** JPEG screenshot frames (~200ms interval, ~5 fps)
- **Client → Server (JSON):** Input events

```json
{"type": "click", "x": 450, "y": 300, "button": "left"}
{"type": "dblclick", "x": 450, "y": 300}
{"type": "keypress", "key": "Enter"}
{"type": "type", "text": "user@example.com"}
{"type": "scroll", "x": 450, "y": 300, "deltaY": -100}
```

#### 2. Persistent Browser Context Manager

```python
class BrowserAuthSession:
    """Manage a persistent Playwright context for interactive auth."""

    async def create(self) -> None:
        """Launch browser with persistent context."""
        # Use new_context() not persistent_context — we'll save/load state manually
        # This avoids filesystem state and keeps everything in Redis

    async def navigate(self, url: str) -> None:
        """Navigate to URL (validated against allowlist)."""

    async def get_screenshot(self) -> bytes:
        """Capture current page as JPEG bytes."""

    async def dispatch_click(self, x: int, y: int, button: str) -> None:
        """Forward mouse click to page."""

    async def dispatch_key(self, key: str) -> None:
        """Forward keyboard event to page."""

    async def dispatch_type(self, text: str) -> None:
        """Type text into the focused element."""

    async def save_auth_state(self, redis) -> None:
        """Save context.storage_state() to Redis."""
        state = await self.context.storage_state()
        await redis.set("ss:browser_auth_state", json.dumps(state), ex=28800)  # 8 hours

    async def check_auth_complete(self) -> bool:
        """Check if the page URL indicates auth is complete (not on login.microsoftonline.com)."""

    async def close(self) -> None:
        """Close the auth context."""
```

#### 3. Integration with `browser_fetcher.py`

Modify `take_screenshot()` to load saved auth state when available:

```python
async def take_screenshot(url, dest_path, timeout_ms=30000) -> bool:
    # ... existing URL allowlist check ...

    # Load auth state from Redis if available
    auth_state = await _load_auth_state()

    browser = await _get_browser()
    context_kwargs = {
        "viewport": {"width": 1920, "height": 1080},
        "device_scale_factor": 1,
        "locale": "en-US",
    }
    if auth_state:
        context_kwargs["storage_state"] = auth_state

    context = await browser.new_context(**context_kwargs)
    # ... rest of existing logic ...
```

#### 4. Auth State in Redis

```
Key:    ss:browser_auth_state
Value:  JSON blob from Playwright's context.storage_state()
TTL:    28800 (8 hours, matches dashboard session TTL)

Contents (Playwright storage_state format):
{
    "cookies": [
        {"name": "FedAuth", "value": "...", "domain": ".sharepoint.com", ...},
        {"name": "rtFa", "value": "...", "domain": ".sharepoint.com", ...},
        {"name": "ESTSAUTH", "value": "...", "domain": "login.microsoftonline.com", ...},
        ...
    ],
    "origins": [
        {"origin": "https://tenant.sharepoint.com", "localStorage": [...]},
        ...
    ]
}
```

### Frontend Components

#### 1. Auth Modal Component

A modal dialog on the Inspection Queue page containing:
- A `<canvas>` element (1920x1080 logical, scaled to fit modal)
- Mouse event listeners (`click`, `dblclick`, `mousemove`, `contextmenu`) that map canvas coordinates to viewport coordinates and send via WebSocket
- Keyboard event listener (`keydown`, `keypress`) that forwards to WebSocket
- Status bar showing current URL and auth state
- "Close" button to end the auth session

#### 2. WebSocket Client

```javascript
const ws = new WebSocket(`wss://${host}/api/inspect/browser-session/stream`);

// Receive screenshot frames
ws.onmessage = (event) => {
    if (event.data instanceof Blob) {
        const img = new Image();
        img.onload = () => ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        img.src = URL.createObjectURL(event.data);
    }
};

// Send input events
canvas.addEventListener('click', (e) => {
    const rect = canvas.getBoundingClientRect();
    const scaleX = 1920 / rect.width;
    const scaleY = 1080 / rect.height;
    ws.send(JSON.stringify({
        type: 'click',
        x: Math.round((e.clientX - rect.left) * scaleX),
        y: Math.round((e.clientY - rect.top) * scaleY),
        button: 'left',
    }));
});
```

#### 3. Auth Status Indicator

On the Inspection Queue page, show auth state:
- **No auth session:** "Browser not authenticated — org-wide links will show sign-in page"
- **Auth active:** "Browser authenticated as user@example.com — expires in 6h 23m"
- **Auth expired:** "Browser session expired — re-authenticate to inspect org-wide links"

This status is fetched from `GET /api/inspect/browser-session/status`.

### Security Considerations

- **URL allowlisting** — The auth browser context should only be navigable to `*.microsoft.com`, `*.microsoftonline.com`, `*.sharepoint.com`, `*.office.com`, `*.live.com`. Reject all other URLs.
- **Auth state isolation** — The saved storage state contains Microsoft session cookies. Store in Redis with the same TTL and access controls as the dashboard session. Never expose cookies to the frontend.
- **WebSocket authentication** — The `/browser-session/stream` WebSocket endpoint must validate the dashboard session cookie before accepting the connection. Require analyst or admin role.
- **Single auth session** — Only one auth browser session at a time (singleton). Prevent multiple users from fighting over the same context.
- **Input sanitization** — Validate all input events from the WebSocket (x/y within viewport bounds, key values are valid, text length limits).
- **Rate limiting** — Cap screenshot frame rate server-side (~5 fps) to prevent resource exhaustion.
- **Timeout** — Auto-close the auth session after 5 minutes of inactivity to free resources.

### Files to Create/Modify

| File | Change |
|------|--------|
| `services/dashboard/app/inspect/browser_auth.py` | **NEW** — `BrowserAuthSession` class, WebSocket handler, auth state management |
| `services/dashboard/app/api/inspect.py` | Add WebSocket route, auth session start/close/status endpoints |
| `services/dashboard/app/inspect/browser_fetcher.py` | Load auth state from Redis when available in `take_screenshot()` |
| `services/dashboard/frontend/src/pages/InspectionQueue.tsx` | Add "Authenticate Browser" button, auth modal with canvas, WebSocket client |
| `services/dashboard/frontend/src/components/BrowserAuthModal.tsx` | **NEW** — Modal component with canvas rendering and input forwarding |

### Dependencies

No new Python dependencies needed — FastAPI already supports WebSocket endpoints, and Playwright is already installed.

No new frontend dependencies needed — WebSocket API and Canvas API are built into browsers.

### Estimated Scope

- Backend: ~300 lines (browser_auth.py + API endpoints + browser_fetcher integration)
- Frontend: ~250 lines (modal component + WebSocket client)
- Total: ~550 lines of new code across 3-4 files

### Open Questions

1. **Multi-user auth state** — Should each dashboard user have their own browser auth session, or is a single shared session sufficient? Shared is simpler but means one user's login covers everyone. Per-user requires storing state keyed by user OID.
2. **Auto-refresh** — Should the system detect when auth cookies are about to expire and prompt re-authentication, or just let it fail and fall back to the unauthenticated screenshot path?
3. **Auth scope** — Should the auth browser navigate to a specific SharePoint site or to a generic Microsoft login URL? A generic login sets cookies for all Microsoft services, which is broader but more useful.
4. **Screencast method** — Use Playwright's periodic `page.screenshot()` in a loop (~5 fps) or CDP's native `Page.startScreencast` for smoother streaming? The screenshot loop is simpler; CDP screencast is more efficient but adds protocol complexity.
