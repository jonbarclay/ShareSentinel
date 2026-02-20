# OneNote Processing — Phase 2 Plan

## Current State

OneNote notebooks (`.one` files, `package.type = "onenote"`) are parked as `pending_manual_inspection` by the worker pipeline. They were previously handled by a browser-based Playwright screenshot pipeline (now archived in `services/dashboard/_deprecated_browser_inspection/`).

Loop and Whiteboard files were migrated to Graph API server-side format conversion in Phase 1. OneNote was deferred because its Graph API support is more limited and requires additional investigation.

## Approaches

### Approach 1: `?format=pdf` via driveItem API (Recommended First Try)

**Concept:** Use the same `download_file_converted()` mechanism from Phase 1 but with `format=pdf` on the OneNote driveItem.

**Endpoint:** `GET /drives/{driveId}/items/{itemId}/content?format=pdf`

**Key difference from deprecated OneNote API:** This uses the standard driveItem content endpoint (same as regular file downloads), NOT the deprecated `/onenote/pages/{id}/content` endpoint. The driveItem approach works with application-level permissions (`Files.Read.All`) and doesn't require the `Notes.Read.All` permission.

**Status:** Needs testing. Microsoft documentation indicates this may work for OneNote notebooks stored as driveItems, but the conversion quality and reliability are unconfirmed.

**Steps to test:**
1. Identify a OneNote notebook's driveItem ID via Graph Explorer
2. Call `GET /drives/{driveId}/items/{itemId}/content?format=pdf`
3. Verify: Does it return a PDF? Is the content complete? How are multi-section notebooks handled?

**Pros:**
- Uses existing `download_file_converted()` infrastructure
- No new permissions required
- Application-level auth (no user interaction)

**Cons:**
- May not be supported for all OneNote notebook formats
- Multi-section notebooks may not convert cleanly
- Conversion quality unknown

### Approach 2: Download Raw `.one` Binary + Parse/OCR

**Concept:** Download the raw `.one` file as a driveItem (standard `download_file()` works — it's just a file in OneDrive/SharePoint), then parse or OCR the content.

**Endpoint:** `GET /drives/{driveId}/items/{itemId}/content` (standard download, no format conversion)

**Parsing options:**
- **Azure AI Document Intelligence** (formerly Form Recognizer): Send the `.one` file to the Document Intelligence API for OCR/extraction. Supports many document formats but `.one` support needs verification.
- **python-one** or similar library: Parse the `.one` binary format directly. The `.one` format is partially documented by Microsoft (MS-ONE specification).
- **LibreOffice conversion**: Convert `.one` to PDF via headless LibreOffice, then use existing `PDFExtractor`.

**Pros:**
- Uses standard download path (no format conversion needed)
- Full content access including embedded images
- No special permissions required

**Cons:**
- `.one` binary format is complex and poorly supported by open-source tools
- Azure AI Document Intelligence adds a service dependency and cost
- LibreOffice adds a container dependency (~200MB)

### Approach 3: Delegated Auth with Service Account

**Concept:** Use a service account with delegated `Notes.Read.All` permission and `offline_access` to read OneNote content via the OneNote-specific Graph API endpoints.

**Endpoints:**
- `GET /users/{userId}/onenote/notebooks`
- `GET /users/{userId}/onenote/sections/{sectionId}/pages`
- `GET /users/{userId}/onenote/pages/{pageId}/content`

**Auth flow:**
1. One-time interactive login with the service account to obtain refresh token
2. Store refresh token securely (encrypted in database or secrets manager)
3. Use refresh token for ongoing access, refreshing automatically with `offline_access` scope

**Required permissions:**
- `Notes.Read.All` (delegated, not application) — reads any user's notebooks
- `offline_access` — allows refresh token issuance

**SPE Permissions Note:** In SharePoint Embedded (SPE) environments, the OneNote API may require additional `Container.Selected` or `FileStorageContainer.Selected` permissions depending on where the notebook is stored.

**Pros:**
- Full OneNote API access (pages, sections, content)
- Rich HTML content output (not just screenshots)
- Can enumerate notebook structure

**Cons:**
- Requires a service account with delegated permissions
- Interactive auth required at least once (for initial refresh token)
- Refresh tokens can expire (90 days without use)
- More complex permission model
- May not work for notebooks in all storage locations (SPE containers)

## Recommendation

**Try approaches in order: 1 → 2 → 3**

1. First, test `?format=pdf` on a real OneNote driveItem. If it produces usable PDF output, this is by far the simplest path — it reuses all existing infrastructure.

2. If format conversion doesn't work, try downloading the raw `.one` file and processing it through Azure AI Document Intelligence or LibreOffice conversion.

3. Only pursue delegated auth if the first two approaches fail, as it introduces significant operational complexity (service account management, token refresh).

## Reference

- Archived browser inspection code: `services/dashboard/_deprecated_browser_inspection/`
- Phase 1 format conversion: `services/worker/app/graph_api/client.py` (`download_file_converted()`)
- MS-ONE file format spec: [MS-ONE](https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-one/)
- OneNote API reference: [Microsoft Graph OneNote API](https://learn.microsoft.com/en-us/graph/api/resources/onenote-api-overview)
