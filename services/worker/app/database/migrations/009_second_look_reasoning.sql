-- Migration 009: Add reasoning column for second-look AI review
ALTER TABLE verdicts
    ADD COLUMN IF NOT EXISTS second_look_reasoning TEXT;

INSERT INTO schema_migrations (version, filename)
VALUES (9, '009_second_look_reasoning.sql')
ON CONFLICT (version) DO NOTHING;
