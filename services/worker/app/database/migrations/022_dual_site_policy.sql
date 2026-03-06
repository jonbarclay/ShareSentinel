-- Migration 022: Dual site policy enforcement (visibility + sharing)

-- Scan execution history (must exist before events reference it)
CREATE TABLE IF NOT EXISTS site_policy_scans (
    id SERIAL PRIMARY KEY,
    trigger_type VARCHAR(20) NOT NULL DEFAULT 'scheduled',
    triggered_by VARCHAR(255) DEFAULT '',
    status VARCHAR(30) NOT NULL DEFAULT 'pending',
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    total_sites_scanned INT DEFAULT 0,
    visibility_violations_found INT DEFAULT 0,
    visibility_remediated INT DEFAULT 0,
    sharing_violations_found INT DEFAULT 0,
    sharing_remediated INT DEFAULT 0,
    errors INT DEFAULT 0,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Sites allowed to be Public (visibility is an M365 Group property)
CREATE TABLE IF NOT EXISTS site_visibility_allowlist (
    id SERIAL PRIMARY KEY,
    group_id VARCHAR(500) NOT NULL UNIQUE,
    site_url TEXT NOT NULL DEFAULT '',
    group_display_name VARCHAR(500) DEFAULT '',
    added_by VARCHAR(255) DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Per-action log of every enforcement action taken
CREATE TABLE IF NOT EXISTS site_policy_events (
    id SERIAL PRIMARY KEY,
    scan_id INT NOT NULL REFERENCES site_policy_scans(id),
    policy_type VARCHAR(30) NOT NULL,
    site_url TEXT NOT NULL DEFAULT '',
    site_display_name VARCHAR(500) DEFAULT '',
    group_id VARCHAR(500) DEFAULT '',
    previous_value VARCHAR(50) DEFAULT '',
    new_value VARCHAR(50) DEFAULT '',
    action VARCHAR(30) NOT NULL,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_site_visibility_allowlist_group_id ON site_visibility_allowlist(group_id);
CREATE INDEX IF NOT EXISTS idx_site_policy_events_created ON site_policy_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_site_policy_events_policy_type ON site_policy_events(policy_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_site_policy_events_scan_id ON site_policy_events(scan_id);
CREATE INDEX IF NOT EXISTS idx_site_policy_scans_status ON site_policy_scans(status, created_at);
