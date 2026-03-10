-- Migration 024: Watchdog support
-- Adds error tracking to audit_poll_state and creates watchdog_alerts table.

-- Add error_message column to audit_poll_state
ALTER TABLE audit_poll_state ADD COLUMN IF NOT EXISTS error_message TEXT;

-- Alert history for dashboard visibility and dedup
CREATE TABLE IF NOT EXISTS watchdog_alerts (
    id SERIAL PRIMARY KEY,
    alert_type VARCHAR(50) NOT NULL,       -- loop_dead, loop_stale, loop_recovered, restart_failed
    severity VARCHAR(20) NOT NULL,         -- warning, critical, info
    loop_name VARCHAR(30),                 -- audit_poller, lifecycle, site_policy, folder_rescan
    message TEXT NOT NULL,
    details JSONB DEFAULT '{}'::jsonb,
    remediation_action VARCHAR(50),        -- container_restart, none, escalation
    remediation_success BOOLEAN,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_watchdog_alerts_created ON watchdog_alerts(created_at DESC);
