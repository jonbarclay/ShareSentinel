-- Add content_type to events for Loop/OneNote/Whiteboard classification
ALTER TABLE events ADD COLUMN IF NOT EXISTS content_type VARCHAR(50) DEFAULT 'file';

-- Index for dashboard filtering by content type
CREATE INDEX IF NOT EXISTS idx_events_content_type ON events(content_type);

-- Composite index for the inspection queue query
CREATE INDEX IF NOT EXISTS idx_events_pending_inspection
    ON events(status, content_type) WHERE status = 'pending_manual_inspection';
