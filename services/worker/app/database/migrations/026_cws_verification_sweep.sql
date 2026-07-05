-- Migration 026: Company-wide sharing verification sweep
-- Detect-only cross-check of every enabled user's OneDrive + every Graph-discovered
-- SharePoint site against the CWS policy. Complements migrations/025 (enforcement)
-- by covering the sites the tenant-admin CSOM enumeration misses at scale.

-- Scan execution history for the weekly sweep
CREATE TABLE IF NOT EXISTS cws_verification_scans (
    id SERIAL PRIMARY KEY,
    trigger_type VARCHAR(20) NOT NULL DEFAULT 'scheduled',
    triggered_by VARCHAR(255) DEFAULT '',
    status VARCHAR(30) NOT NULL DEFAULT 'pending',
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    total_candidates INT DEFAULT 0,
    checked INT DEFAULT 0,
    drifts_found INT DEFAULT 0,
    unprovisioned_count INT DEFAULT 0,
    ok_count INT DEFAULT 0,
    errors INT DEFAULT 0,
    exception_group_member_count INT DEFAULT 0,
    last_checkpoint_cursor TEXT DEFAULT '',
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Per-site detection events. scan_id is NULL for event-driven (audit_event) inserts
-- so we do not have to synthesize a fake scan row for each audit hit.
CREATE TABLE IF NOT EXISTS cws_verification_events (
    id SERIAL PRIMARY KEY,
    scan_id INT REFERENCES cws_verification_scans(id),
    site_url TEXT NOT NULL DEFAULT '',
    site_display_name VARCHAR(500) DEFAULT '',
    site_type VARCHAR(20) NOT NULL DEFAULT 'site',
    user_upn VARCHAR(320) DEFAULT '',
    expected_value VARCHAR(30) DEFAULT '',
    actual_value VARCHAR(30) DEFAULT '',
    drift_type VARCHAR(30) NOT NULL,
    exempt_reason VARCHAR(50) DEFAULT '',
    trigger_source VARCHAR(20) NOT NULL DEFAULT 'scheduled',
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cws_verif_scans_status_created
    ON cws_verification_scans(status, created_at);
CREATE INDEX IF NOT EXISTS idx_cws_verif_scans_trigger_type
    ON cws_verification_scans(trigger_type, completed_at DESC);
CREATE INDEX IF NOT EXISTS idx_cws_verif_events_scan_id
    ON cws_verification_events(scan_id);
CREATE INDEX IF NOT EXISTS idx_cws_verif_events_created
    ON cws_verification_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cws_verif_events_drift_type
    ON cws_verification_events(drift_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cws_verif_events_trigger_source
    ON cws_verification_events(trigger_source, created_at DESC);
