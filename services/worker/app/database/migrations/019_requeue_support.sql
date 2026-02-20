-- 019: Add partial index for efficient stuck-event recovery queries.

CREATE INDEX IF NOT EXISTS idx_events_processing_started
    ON events(processing_started_at) WHERE status = 'processing';
