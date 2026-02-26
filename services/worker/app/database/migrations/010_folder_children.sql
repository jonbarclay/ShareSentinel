-- Migration 010: Add parent-child linking for folder enumeration
-- Allows folder events to track their enumerated child file events

ALTER TABLE events ADD COLUMN parent_event_id VARCHAR(64) REFERENCES events(event_id);
ALTER TABLE events ADD COLUMN child_index INT;
ALTER TABLE events ADD COLUMN folder_total_children INT;
ALTER TABLE events ADD COLUMN folder_processed_children INT DEFAULT 0;
ALTER TABLE events ADD COLUMN folder_flagged_children INT DEFAULT 0;

CREATE INDEX idx_events_parent_event_id ON events(parent_event_id);
