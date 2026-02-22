-- Migration 005: Replace 1-5 sensitivity rating with category-based rubric
-- Adds category_assessments, overall_context, escalation_tier to verdicts
-- Adds category_ids to file_hashes
-- Makes sensitivity_rating nullable for new rows

BEGIN;

-- 1. Add new columns to verdicts
ALTER TABLE verdicts
    ADD COLUMN IF NOT EXISTS category_assessments JSONB DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS overall_context VARCHAR(20),
    ADD COLUMN IF NOT EXISTS escalation_tier VARCHAR(10);

-- 2. Add category_ids to file_hashes
ALTER TABLE file_hashes
    ADD COLUMN IF NOT EXISTS category_ids JSONB DEFAULT '[]'::jsonb;

-- 3. Drop the CHECK constraint on sensitivity_rating and make it nullable
ALTER TABLE verdicts DROP CONSTRAINT IF EXISTS verdicts_sensitivity_rating_check;
ALTER TABLE verdicts ALTER COLUMN sensitivity_rating DROP NOT NULL;

-- 4. Backfill escalation_tier for existing rows
UPDATE verdicts
SET escalation_tier = CASE
    WHEN sensitivity_rating >= 4 THEN 'tier_1'
    ELSE 'none'
END
WHERE escalation_tier IS NULL;

-- 5. Add index on escalation_tier for dashboard queries
CREATE INDEX IF NOT EXISTS idx_verdicts_escalation_tier ON verdicts(escalation_tier);

-- 6. Record migration
INSERT INTO schema_migrations (version, filename)
VALUES (5, '005_categories.sql')
ON CONFLICT (version) DO NOTHING;

COMMIT;
