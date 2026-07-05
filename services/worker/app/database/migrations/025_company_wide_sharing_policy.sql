-- Migration 025: Company-wide sharing links policy enforcement
-- Disables the "Anyone in your organization" sharing option (DisableCompanyWideSharingLinks)
-- on all SharePoint sites and OneDrive personal sites, with two exception lists:
--   1. Sites on site_company_wide_sharing_allowlist (managed via dashboard)
--   2. OneDrive users whose UPN is a member of the Entra ID group OrgWideSharingLinkException

-- SharePoint sites allowed to keep "Anyone in your organization" links enabled
CREATE TABLE IF NOT EXISTS site_company_wide_sharing_allowlist (
    id SERIAL PRIMARY KEY,
    site_id VARCHAR(500) NOT NULL UNIQUE,
    site_url TEXT NOT NULL,
    site_display_name VARCHAR(500) DEFAULT '',
    added_by VARCHAR(255) DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Scan execution history
CREATE TABLE IF NOT EXISTS company_wide_sharing_scans (
    id SERIAL PRIMARY KEY,
    trigger_type VARCHAR(20) NOT NULL DEFAULT 'scheduled',
    triggered_by VARCHAR(255) DEFAULT '',
    status VARCHAR(30) NOT NULL DEFAULT 'pending',
    dry_run BOOLEAN NOT NULL DEFAULT TRUE,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    total_sites_scanned INT DEFAULT 0,
    violations_found INT DEFAULT 0,
    remediated INT DEFAULT 0,
    would_remediate INT DEFAULT 0,
    skipped_allowlist INT DEFAULT 0,
    skipped_exception_group INT DEFAULT 0,
    already_compliant INT DEFAULT 0,
    errors INT DEFAULT 0,
    exception_group_member_count INT DEFAULT 0,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Per-action log of every enforcement decision (including dry-run "would disable" entries)
CREATE TABLE IF NOT EXISTS company_wide_sharing_events (
    id SERIAL PRIMARY KEY,
    scan_id INT NOT NULL REFERENCES company_wide_sharing_scans(id),
    site_url TEXT NOT NULL DEFAULT '',
    site_display_name VARCHAR(500) DEFAULT '',
    site_type VARCHAR(20) NOT NULL DEFAULT 'site',
    user_upn VARCHAR(320) DEFAULT '',
    previous_value VARCHAR(30) DEFAULT '',
    new_value VARCHAR(30) DEFAULT '',
    action VARCHAR(30) NOT NULL,
    exempt_reason VARCHAR(50) DEFAULT '',
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_site_cws_allowlist_site_id ON site_company_wide_sharing_allowlist(site_id);
CREATE INDEX IF NOT EXISTS idx_cws_scans_status_created ON company_wide_sharing_scans(status, created_at);
CREATE INDEX IF NOT EXISTS idx_cws_scans_trigger_type ON company_wide_sharing_scans(trigger_type, completed_at DESC);
CREATE INDEX IF NOT EXISTS idx_cws_events_scan_id ON company_wide_sharing_events(scan_id);
CREATE INDEX IF NOT EXISTS idx_cws_events_created ON company_wide_sharing_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cws_events_action ON company_wide_sharing_events(action, created_at DESC);
