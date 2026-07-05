-- Migration 033: site_breach_review results table
--
-- Standalone, legal-driven breach investigation of two formerly-PUBLIC SharePoint
-- sites (UVU Ombuds Office, REFUND DECISIONS). Because the sites were public, ALL
-- files and channel messages were accessible to every internal user without an
-- explicit share, so these items never generated ShareSentinel sharing events and
-- are absent from `events`/`verdicts`.
--
-- scripts/site_breach_review.py enumerates EVERY file in each site's drives (and
-- the connected Teams channel messages) live via Graph, re-analyzes each with the
-- dedicated `targeted_pii` prompt extended for HEALTH/PHI, and writes the
-- determination here. Separate from production `verdicts`: never overwrites a
-- verdict or triggers analyst notifications.
--
-- Mirrors targeted_pii_review (migration 032) but is source-agnostic (file vs.
-- message), carries the originating site, and adds the HEALTH/PHI finding.
--
-- NOTE: the migration runner records the version in schema_migrations itself, so
-- this file must NOT insert that row.

CREATE TABLE IF NOT EXISTS site_breach_review (
    id                      SERIAL PRIMARY KEY,
    run_id                  VARCHAR(40),                 -- one per script invocation
    site                    VARCHAR(120),                -- e.g. UVUOmbudsOffice | REFUNDDECISIONS
    source                  VARCHAR(12) NOT NULL,        -- file | message
    item_key                TEXT NOT NULL,               -- driveId:itemId for files, channelId:messageId for messages
    file_name               TEXT,                        -- file name, or a synthetic label for a message
    drive_id                TEXT,
    item_id_graph           TEXT,
    web_url                 TEXT,
    relative_path           TEXT,
    file_size_bytes         BIGINT,
    analysis_mode_used      VARCHAR(30),                 -- targeted_pii | targeted_pii_multimodal | filename_only
    download_status         VARCHAR(40),                 -- downloaded | skipped | error:<Type>
    any_critical_found      BOOLEAN,
    full_ssn_found          BOOLEAN, ssn_confidence       VARCHAR(10), ssn_evidence       TEXT,
    full_dl_number_found    BOOLEAN, dl_number_confidence VARCHAR(10), dl_number_evidence TEXT,
    full_dl_image_found     BOOLEAN, dl_image_confidence  VARCHAR(10), dl_image_evidence  TEXT,
    full_financial_found    BOOLEAN, financial_confidence VARCHAR(10), financial_evidence TEXT,
    full_health_found       BOOLEAN, health_confidence    VARCHAR(10), health_evidence    TEXT,
    is_coursework           BOOLEAN,
    ai_provider             VARCHAR(20),
    ai_model                VARCHAR(100),
    targeted_reasoning      TEXT,
    review_status           VARCHAR(30),                 -- likely_match | no_critical | needs_manual_review
    estimated_cost_usd      NUMERIC(10,6) DEFAULT 0,
    saved_path              TEXT,                        -- where the original file/message was saved for legal review
    created_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    UNIQUE (item_key, run_id)
);

CREATE INDEX IF NOT EXISTS idx_sbr_any_critical ON site_breach_review(any_critical_found);
CREATE INDEX IF NOT EXISTS idx_sbr_site ON site_breach_review(site);
CREATE INDEX IF NOT EXISTS idx_sbr_run ON site_breach_review(run_id);
CREATE INDEX IF NOT EXISTS idx_sbr_review_status ON site_breach_review(review_status);
