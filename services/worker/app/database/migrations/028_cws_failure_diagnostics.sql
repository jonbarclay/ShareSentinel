-- Migration 028: CWS failure diagnostics cache + current-failures view
-- Captures the *why* behind a failed Set-PnPTenantSite -DisableCompanyWideSharingLinks
-- attempt, so the dashboard can surface actionable reasons (locked OneDrive,
-- license issue, quota exceeded, etc.) instead of just the raw PnP error string.
-- One row per site_url; refreshed by Invoke-CwsFailureDiagnostic.ps1 which
-- shells out to the SharePoint-Oversharing-Toolkit's Diagnose-OneDriveSite.ps1.

CREATE TABLE IF NOT EXISTS cws_failure_diagnostics (
    site_url TEXT PRIMARY KEY,
    user_upn VARCHAR(320) DEFAULT '',
    site_display_name VARCHAR(500) DEFAULT '',
    site_type VARCHAR(20) NOT NULL DEFAULT 'site',
    -- Parsed category from Diagnose-OneDriveSite.ps1 output or emitted by the
    -- wrapper's Graph pre-check. Values used by the UI:
    --   unknown, lockstate_locked, archived, quota_exceeded, quota_warning,
    --   account_disabled, no_usage_location, a1_license_cap, no_license,
    --   no_onedrive_provisioned, user_deleted, user_recently_deleted (Entra
    --   soft-deleted, recoverable <30d), user_purged (past 30d retention,
    --   unrecoverable), access_denied, throttled, diagnostic_unavailable,
    --   diagnostic_error, unsupported_site_type (SharePoint Embedded container,
    --   legacy public site, or Content Type Hub — not manageable via
    --   Set-PnPTenantSite; excluded from the CWS policy)
    failure_category VARCHAR(40) NOT NULL DEFAULT 'unknown',
    diagnostic_summary TEXT DEFAULT '',
    raw_output TEXT DEFAULT '',
    source_event_id INT REFERENCES company_wide_sharing_events(id) ON DELETE SET NULL,
    source_error_message TEXT DEFAULT '',
    diagnostic_exit_code INT DEFAULT 0,
    last_diagnosed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cws_failure_diag_category
    ON cws_failure_diagnostics(failure_category);
CREATE INDEX IF NOT EXISTS idx_cws_failure_diag_last_diagnosed
    ON cws_failure_diagnostics(last_diagnosed_at);

-- View: the latest event per site_url, filtered to those whose latest action is 'failed'.
-- A site drops out of this view as soon as a later 'remediated' / 'already_compliant' /
-- 'skipped_*' event arrives for it, which is exactly the "currently broken" set we want
-- to surface in the UI.
CREATE OR REPLACE VIEW cws_current_failures AS
WITH latest AS (
    SELECT DISTINCT ON (site_url)
        id,
        scan_id,
        site_url,
        site_display_name,
        site_type,
        user_upn,
        previous_value,
        new_value,
        action,
        error_message,
        created_at
    FROM company_wide_sharing_events
    WHERE site_url <> ''
    ORDER BY site_url, created_at DESC, id DESC
)
SELECT
    id AS event_id,
    scan_id,
    site_url,
    site_display_name,
    site_type,
    user_upn,
    previous_value,
    new_value,
    error_message,
    created_at AS failed_at
FROM latest
WHERE action = 'failed';
