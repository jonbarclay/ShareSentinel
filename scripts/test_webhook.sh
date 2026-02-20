#!/usr/bin/env bash
# =============================================================================
# ShareSentinel - Test Webhook Script
# Sends sample payloads to the webhook listener and validates responses.
# =============================================================================

set -euo pipefail

WEBHOOK_URL="${WEBHOOK_URL:-http://localhost:8000/webhook/splunk}"
AUTH_SECRET="${WEBHOOK_AUTH_SECRET:-test-secret}"
HEALTH_URL="${WEBHOOK_URL%/webhook/splunk}/health"

PASS=0
FAIL=0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

print_result() {
    local label="$1"
    local expected="$2"
    local actual="$3"
    local body="$4"

    if [[ "$actual" == "$expected" ]]; then
        echo "  [PASS] $label (HTTP $actual)"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] $label (expected HTTP $expected, got HTTP $actual)"
        echo "         Body: $body"
        FAIL=$((FAIL + 1))
    fi
}

do_post() {
    local payload="$1"
    local tmpfile
    tmpfile=$(mktemp)
    local http_code
    http_code=$(curl -s -o "$tmpfile" -w "%{http_code}" \
        -X POST "$WEBHOOK_URL" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer $AUTH_SECRET" \
        -d "$payload")
    local body
    body=$(cat "$tmpfile")
    rm -f "$tmpfile"
    echo "$http_code|$body"
}

# ---------------------------------------------------------------------------
# Test payloads
# ---------------------------------------------------------------------------

FILE_PAYLOAD='{
  "result": {
    "Operation": "AnonymousLinkCreated",
    "Workload": "OneDrive",
    "UserId": "testuser@organization.com",
    "ObjectId": "https://organization-my.sharepoint.com/personal/testuser_organization_com/Documents/test-file.pdf",
    "SiteUrl": "https://organization-my.sharepoint.com/personal/testuser_organization_com/",
    "SourceFileName": "test-file.pdf",
    "SourceRelativeUrl": "personal/testuser_organization_com/Documents",
    "ItemType": "File",
    "EventSource": "SharePoint",
    "CreationTime": "2024-01-15T10:30:00Z",
    "SharingType": "Anonymous",
    "SharingScope": "Anyone",
    "SharingPermission": "View"
  }
}'

FOLDER_PAYLOAD='{
  "result": {
    "Operation": "CompanySharingLinkCreated",
    "Workload": "SharePoint",
    "UserId": "manager@organization.com",
    "ObjectId": "https://organization.sharepoint.com/sites/finance/Shared Documents/Budget Reports",
    "SiteUrl": "https://organization.sharepoint.com/sites/finance/",
    "SourceFileName": "Budget Reports",
    "SourceRelativeUrl": "sites/finance/Shared Documents",
    "ItemType": "Folder",
    "EventSource": "SharePoint",
    "CreationTime": "2024-01-15T11:00:00Z",
    "SharingType": "Company",
    "SharingScope": "Organization",
    "SharingPermission": "Edit"
  }
}'

MALFORMED_PAYLOAD='{
  "result": {
    "Operation": "AnonymousLinkCreated"
  }
}'

# ---------------------------------------------------------------------------
# Run tests
# ---------------------------------------------------------------------------

echo "======================================================================"
echo "ShareSentinel Webhook Test Suite"
echo "======================================================================"
echo ""
echo "Target: $WEBHOOK_URL"
echo ""

# Test 1: Health check
echo "--- Test 1: Health endpoint ---"
tmpfile=$(mktemp)
health_code=$(curl -s -o "$tmpfile" -w "%{http_code}" "$HEALTH_URL")
health_body=$(cat "$tmpfile")
rm -f "$tmpfile"
print_result "GET /health" "200" "$health_code" "$health_body"
echo ""

# Test 2: Valid file share (AnonymousLinkCreated)
echo "--- Test 2: Valid file share payload (AnonymousLinkCreated) ---"
IFS='|' read -r code body <<< "$(do_post "$FILE_PAYLOAD")"
print_result "POST valid file share" "200" "$code" "$body"
if echo "$body" | grep -qi "queued"; then
    echo "         Response contains 'queued' as expected"
else
    echo "         WARNING: Response may not contain 'queued'"
fi
echo ""

# Test 3: Valid folder share (CompanySharingLinkCreated)
echo "--- Test 3: Valid folder share payload (CompanySharingLinkCreated) ---"
IFS='|' read -r code body <<< "$(do_post "$FOLDER_PAYLOAD")"
print_result "POST valid folder share" "200" "$code" "$body"
echo ""

# Test 4: Duplicate of first payload (should be detected)
echo "--- Test 4: Duplicate file share (should be deduplicated) ---"
IFS='|' read -r code body <<< "$(do_post "$FILE_PAYLOAD")"
print_result "POST duplicate payload" "200" "$code" "$body"
if echo "$body" | grep -qi "duplicate"; then
    echo "         Response contains 'duplicate' as expected"
    PASS=$((PASS + 1))
else
    echo "         WARNING: Response does not contain 'duplicate' -- dedup may not be working"
    FAIL=$((FAIL + 1))
fi
echo ""

# Test 5: Malformed payload (missing required fields)
echo "--- Test 5: Malformed payload (missing required fields) ---"
IFS='|' read -r code body <<< "$(do_post "$MALFORMED_PAYLOAD")"
# Accept 400 or 422 (FastAPI validation returns 422 by default)
if [[ "$code" == "400" || "$code" == "422" ]]; then
    echo "  [PASS] POST malformed payload (HTTP $code)"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] POST malformed payload (expected HTTP 400 or 422, got HTTP $code)"
    echo "         Body: $body"
    FAIL=$((FAIL + 1))
fi
echo ""

# Test 6: Missing auth header
echo "--- Test 6: Request without auth header ---"
tmpfile=$(mktemp)
no_auth_code=$(curl -s -o "$tmpfile" -w "%{http_code}" \
    -X POST "$WEBHOOK_URL" \
    -H "Content-Type: application/json" \
    -d "$FILE_PAYLOAD")
no_auth_body=$(cat "$tmpfile")
rm -f "$tmpfile"
if [[ "$no_auth_code" == "401" || "$no_auth_code" == "403" ]]; then
    echo "  [PASS] POST without auth (HTTP $no_auth_code)"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] POST without auth (expected HTTP 401 or 403, got HTTP $no_auth_code)"
    echo "         Body: $no_auth_body"
    FAIL=$((FAIL + 1))
fi
echo ""

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo "======================================================================"
echo "Results: $PASS passed, $FAIL failed"
echo "======================================================================"

if [[ "$FAIL" -gt 0 ]]; then
    exit 1
fi
