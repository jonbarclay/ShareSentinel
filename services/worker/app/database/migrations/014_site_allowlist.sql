-- Migration 014: Site allowlist for anonymous sharing enforcement
-- Sites permitted to have anonymous sharing
CREATE TABLE IF NOT EXISTS site_allowlist (
    id SERIAL PRIMARY KEY,
    site_id VARCHAR(500) NOT NULL UNIQUE,
    site_url TEXT NOT NULL,
    site_display_name VARCHAR(500) DEFAULT '',
    added_by VARCHAR(255) DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Sync execution history (one row per enforcement run)
CREATE TABLE IF NOT EXISTS site_allowlist_syncs (
    id SERIAL PRIMARY KEY,
    trigger_type VARCHAR(20) NOT NULL DEFAULT 'scheduled',
    triggered_by VARCHAR(255) DEFAULT '',
    status VARCHAR(30) NOT NULL DEFAULT 'pending',
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    total_sites_checked INT DEFAULT 0,
    sites_disabled INT DEFAULT 0,
    sites_enabled INT DEFAULT 0,
    sites_already_correct INT DEFAULT 0,
    sites_failed INT DEFAULT 0,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Per-site detail for each sync run
CREATE TABLE IF NOT EXISTS site_allowlist_sync_details (
    id SERIAL PRIMARY KEY,
    sync_id INT NOT NULL REFERENCES site_allowlist_syncs(id),
    site_id VARCHAR(500) NOT NULL,
    site_url TEXT NOT NULL,
    site_display_name VARCHAR(500) DEFAULT '',
    previous_capability VARCHAR(50),
    desired_capability VARCHAR(50),
    action_taken VARCHAR(30) NOT NULL,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_site_allowlist_site_id ON site_allowlist(site_id);
CREATE INDEX IF NOT EXISTS idx_site_allowlist_syncs_status_created ON site_allowlist_syncs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_site_allowlist_sync_details_sync_id ON site_allowlist_sync_details(sync_id);
