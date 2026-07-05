-- Migration 034: targeted_phi_review results table
--
-- Standalone, legal-driven side investigation (sibling of 032_targeted_pii_review).
-- Legal counsel asked for a NARROW, targeted re-check of the files ShareSentinel
-- already flagged as containing medical/health information: which of them actually
-- contain PROTECTED HEALTH INFORMATION (PHI) — individually identifiable health
-- information about a real, identifiable person.
--
-- scripts/targeted_phi_review.py mines existing verdicts for candidates whose AI
-- analysis indicates medical/health content (hipaa category, or the `medical` /
-- `disability_accommodation` pii_types), then re-downloads + re-analyzes each one
-- with a dedicated `targeted_phi` prompt (GPT-5.4) and writes the determination
-- here. This table is deliberately SEPARATE from the production `verdicts` table
-- so the investigation never overwrites production verdicts or triggers analyst
-- notifications.
--
-- NOTE: the migration runner (connection.run_migrations) records the version in
-- schema_migrations itself, so this file must NOT insert that row.

CREATE TABLE IF NOT EXISTS targeted_phi_review (
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
    analysis_mode_used      VARCHAR(30),                 -- targeted_phi | targeted_phi_multimodal | filename_only
    download_status         VARCHAR(40),                 -- downloaded | skipped | error:<Type>
    phi_found               BOOLEAN,
    phi_confidence          VARCHAR(10),                 -- high | medium | low
    health_info_present     BOOLEAN,                     -- health content present even if not identifiable
    individually_identifiable BOOLEAN,
    identifiability_basis   VARCHAR(20),                 -- named | student_id | ... | contextual | none
    phi_types               JSONB,                       -- array of PHI sub-types (diagnosis_condition, ...)
    subject_count_estimate  VARCHAR(10),                 -- single | few | many | unknown
    is_coursework           BOOLEAN,
    ai_provider             VARCHAR(20),
    ai_model                VARCHAR(100),
    phi_evidence            TEXT,                        -- masked description (no transcription of the record)
    targeted_reasoning      TEXT,
    prior_evidence          TEXT,                        -- fallback when download fails
    review_status           VARCHAR(30),                 -- confirmed_phi | likely_phi | health_not_identifiable | no_phi | needs_manual_review
    estimated_cost_usd      NUMERIC(10,6) DEFAULT 0,
    saved_path              TEXT,                        -- where the original file was saved for legal review (when PHI confirmed)
    created_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    UNIQUE (event_id, run_id)
);

CREATE INDEX IF NOT EXISTS idx_tphir_phi_found ON targeted_phi_review(phi_found);
CREATE INDEX IF NOT EXISTS idx_tphir_event ON targeted_phi_review(event_id);
CREATE INDEX IF NOT EXISTS idx_tphir_run ON targeted_phi_review(run_id);
CREATE INDEX IF NOT EXISTS idx_tphir_review_status ON targeted_phi_review(review_status);
