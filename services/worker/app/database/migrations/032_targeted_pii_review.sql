-- Migration 032: targeted_pii_review results table
--
-- Standalone, legal-driven side investigation. Legal counsel reviewed the
-- files ShareSentinel already flagged Tier 1/Tier 2 and asked for a NARROW,
-- targeted re-check: which files actually contain a FULL/real Social Security
-- Number, a FULL Driver's License number (or a picture/scan of a DL), or a
-- FULL financial account number.
--
-- scripts/targeted_pii_review.py mines existing verdicts for candidates, then
-- re-downloads + re-analyzes each one with a dedicated `targeted_pii` prompt
-- and writes the determination here. This table is deliberately SEPARATE from
-- the production `verdicts` table so the investigation never overwrites
-- production verdicts or triggers analyst notifications.
--
-- NOTE: the migration runner (connection.run_migrations) records the version
-- in schema_migrations itself, so this file must NOT insert that row.

CREATE TABLE IF NOT EXISTS targeted_pii_review (
    id                      SERIAL PRIMARY KEY,
    run_id                  VARCHAR(40),                 -- one per script invocation
    verdict_id              INT,
    event_id                VARCHAR(128) NOT NULL,       -- matches events/verdicts; child events use a ":child:N" suffix
    file_name               TEXT,
    drive_id                TEXT,
    item_id_graph           TEXT,
    web_url                 TEXT,
    sharing_link_url        TEXT,
    candidate_class         VARCHAR(20),                 -- known_match | needs_reverify
    analysis_mode_used      VARCHAR(30),                 -- targeted_pii | targeted_pii_multimodal | filename_only
    download_status         VARCHAR(40),                 -- downloaded | skipped | error:<Type>
    any_critical_found      BOOLEAN,
    full_ssn_found          BOOLEAN, ssn_confidence       VARCHAR(10), ssn_evidence       TEXT,
    full_dl_number_found    BOOLEAN, dl_number_confidence VARCHAR(10), dl_number_evidence TEXT,
    full_dl_image_found     BOOLEAN, dl_image_confidence  VARCHAR(10), dl_image_evidence  TEXT,
    full_financial_found    BOOLEAN, financial_confidence VARCHAR(10), financial_evidence TEXT,
    is_coursework           BOOLEAN,
    ai_provider             VARCHAR(20),
    ai_model                VARCHAR(100),
    targeted_reasoning      TEXT,
    prior_evidence          TEXT,                        -- fallback when download fails
    review_status           VARCHAR(30),                 -- confirmed_match | likely_match | no_critical | needs_manual_review
    estimated_cost_usd      NUMERIC(10,6) DEFAULT 0,
    saved_path              TEXT,                        -- where the original file was saved for legal review (when a critical item was found)
    created_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    UNIQUE (event_id, run_id)
);

CREATE INDEX IF NOT EXISTS idx_tpr_any_critical ON targeted_pii_review(any_critical_found);
CREATE INDEX IF NOT EXISTS idx_tpr_event ON targeted_pii_review(event_id);
CREATE INDEX IF NOT EXISTS idx_tpr_run ON targeted_pii_review(run_id);
CREATE INDEX IF NOT EXISTS idx_tpr_review_status ON targeted_pii_review(review_status);
