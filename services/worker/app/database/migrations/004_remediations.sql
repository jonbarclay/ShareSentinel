-- Remediations table: tracks automated sharing link removal on true positive disposition.

CREATE TABLE IF NOT EXISTS remediations (
    id SERIAL PRIMARY KEY,
    event_id VARCHAR(64) NOT NULL REFERENCES events(event_id),
    requested_by VARCHAR(255) NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    action_type VARCHAR(30) NOT NULL DEFAULT 'remove_sharing',
    status VARCHAR(30) NOT NULL DEFAULT 'pending',
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    permissions_removed INT DEFAULT 0,
    permissions_failed INT DEFAULT 0,
    permission_details JSONB DEFAULT '[]'::jsonb,
    report_sent BOOLEAN DEFAULT FALSE,
    report_sent_at TIMESTAMPTZ,
    report_recipients JSONB DEFAULT '[]'::jsonb,
    error_message TEXT,
    retry_count INT DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_remediations_status ON remediations(status);
CREATE INDEX IF NOT EXISTS idx_remediations_event_id ON remediations(event_id);
