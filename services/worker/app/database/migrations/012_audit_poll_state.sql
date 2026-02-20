-- Migration 012: Audit log poll state tracking
-- Single-row table to persist the last successful poll timestamp

CREATE TABLE IF NOT EXISTS audit_poll_state (
    id INTEGER PRIMARY KEY DEFAULT 1,
    last_poll_time TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    last_poll_status TEXT DEFAULT 'success',
    events_found INTEGER DEFAULT 0,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    CONSTRAINT single_row CHECK (id = 1)
);

INSERT INTO audit_poll_state (id, last_poll_time)
VALUES (1, NOW() - INTERVAL '1 hour')
ON CONFLICT DO NOTHING;
