# Deprecated: Browser-Based Inspection Pipeline

## What This Was

This directory contains the archived browser-based inspection pipeline that was used to process "delegated content" types (Loop components, OneNote notebooks, and Whiteboards) that cannot be fetched via the application-level Microsoft Graph API.

The pipeline used:
- **Playwright** with a headless Chromium browser to screenshot sharing URLs
- **Saved authentication cookies** obtained via an interactive browser session streamed to the dashboard
- **Multimodal AI analysis** on the captured screenshots

## Why It Was Replaced

Microsoft Graph API now supports server-side format conversion for **Loop** and **Whiteboard** files:
- Loop → HTML via `?format=html`
- Whiteboard → PDF via `?format=pdf`

This is faster, more reliable, and doesn't require manual SSO authentication or overlay dismissal heuristics.

## What About OneNote?

OneNote is **not yet supported** by the new pipeline. See `docs/onenote-phase2-plan.md` for the Phase 2 plan covering three approaches:
1. `?format=pdf` via the driveItem API
2. Raw `.one` binary file parsing
3. Delegated auth with a service account

If OneNote support requires browser-based screenshots again, this code can be restored.

## File Inventory

| File | Original Location | Purpose |
|------|-------------------|---------|
| `browser_auth.py` | `app/inspect/browser_auth.py` | Interactive browser auth session management |
| `browser_fetcher.py` | `app/inspect/browser_fetcher.py` | Playwright screenshot capture of sharing URLs |
| `processor.py` | `app/inspect/processor.py` | Screenshot → AI analysis orchestration |
| `ai_bridge.py` | `app/inspect/ai_bridge.py` | Bridge to worker AI provider for multimodal analysis |
| `__init__.py` | `app/inspect/__init__.py` | Package init |
| `inspect_api.py` | `app/api/inspect.py` | FastAPI router for inspection endpoints |
| `InspectionQueue.tsx` | `frontend/src/pages/InspectionQueue.tsx` | React page for inspection queue UI |
| `InspectionQueue.css` | `frontend/src/pages/InspectionQueue.css` | Styles for inspection queue |
| `BrowserAuthModal.tsx` | `frontend/src/components/BrowserAuthModal.tsx` | Browser auth modal component |
| `BrowserAuthModal.css` | `frontend/src/components/BrowserAuthModal.css` | Styles for auth modal |

## How to Restore

1. Move files back to their original locations
2. Re-add `playwright>=1.49.0` to `services/dashboard/requirements.txt`
3. Re-add Playwright browser install to `services/dashboard/Dockerfile`
4. Re-add inspect module registration in `services/dashboard/app/main.py`
5. Re-add InspectionQueue route in `services/dashboard/frontend/src/App.tsx`
