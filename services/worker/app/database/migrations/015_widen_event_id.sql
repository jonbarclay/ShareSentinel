-- Migration 015: Widen event_id columns from VARCHAR(64) to VARCHAR(128)
-- Folder child event IDs use the format "{parent_event_id}:child:{N}" which
-- can exceed 64 chars when the parent event_id is a 64-char hex string.
-- PostgreSQL handles VARCHAR widening in-place with no table rewrite.

ALTER TABLE events ALTER COLUMN event_id TYPE VARCHAR(128);
ALTER TABLE events ALTER COLUMN parent_event_id TYPE VARCHAR(128);
ALTER TABLE events ALTER COLUMN hash_match_event_id TYPE VARCHAR(128);
ALTER TABLE verdicts ALTER COLUMN event_id TYPE VARCHAR(128);
ALTER TABLE audit_log ALTER COLUMN event_id TYPE VARCHAR(128);
ALTER TABLE file_hashes ALTER COLUMN first_event_id TYPE VARCHAR(128);
ALTER TABLE remediations ALTER COLUMN event_id TYPE VARCHAR(128);
ALTER TABLE sharing_link_lifecycle ALTER COLUMN event_id TYPE VARCHAR(128);
ALTER TABLE user_notifications ALTER COLUMN event_id TYPE VARCHAR(128);
