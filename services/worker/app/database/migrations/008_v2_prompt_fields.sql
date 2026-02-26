-- Migration 008: Add v2 prompt fields to verdicts table
-- New fields: reasoning (chain-of-thought), data_recency, risk_score

ALTER TABLE verdicts
    ADD COLUMN IF NOT EXISTS reasoning TEXT,
    ADD COLUMN IF NOT EXISTS data_recency VARCHAR(20),
    ADD COLUMN IF NOT EXISTS risk_score SMALLINT;

CREATE INDEX IF NOT EXISTS idx_verdicts_risk_score ON verdicts(risk_score);

INSERT INTO schema_migrations (version, filename)
VALUES (8, '008_v2_prompt_fields.sql')
ON CONFLICT (version) DO NOTHING;
