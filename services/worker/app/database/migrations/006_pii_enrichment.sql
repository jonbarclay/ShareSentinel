-- Migration 006: Add PII enrichment columns to verdicts
-- Supports volume-aware and richness-aware escalation decisions

ALTER TABLE verdicts
    ADD COLUMN IF NOT EXISTS affected_count INT DEFAULT 0,
    ADD COLUMN IF NOT EXISTS pii_types_found JSONB DEFAULT '[]'::jsonb;

INSERT INTO schema_migrations (version, filename) VALUES (6, '006_pii_enrichment.sql')
ON CONFLICT (version) DO NOTHING;
