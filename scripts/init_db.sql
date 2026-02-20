-- ShareSentinel database initialization
-- This script is mounted into the PostgreSQL container and runs on first start.
-- It duplicates the migration 001_initial.sql for Docker entrypoint compatibility.

CREATE TABLE IF NOT EXISTS events (
    id SERIAL PRIMARY KEY,
    event_id VARCHAR(64) UNIQUE NOT NULL,
    operation VARCHAR(100) NOT NULL,
    workload VARCHAR(50),
    user_id VARCHAR(255) NOT NULL,
    object_id TEXT NOT NULL,
    site_url TEXT,
    file_name VARCHAR(500),
    relative_path TEXT,
    item_type VARCHAR(20) NOT NULL,
    sharing_type VARCHAR(50),
    sharing_scope VARCHAR(50),
    sharing_permission VARCHAR(20),
    event_time TIMESTAMP WITH TIME ZONE,
    confirmed_file_name VARCHAR(500),
    file_size_bytes BIGINT,
    mime_type VARCHAR(100),
    web_url TEXT,
    sharing_link_url TEXT,
    drive_id VARCHAR(255),
    item_id_graph VARCHAR(255),
    status VARCHAR(30) NOT NULL DEFAULT 'queued',
    processing_started_at TIMESTAMP WITH TIME ZONE,
    processing_completed_at TIMESTAMP WITH TIME ZONE,
    file_category VARCHAR(30),
    extraction_method VARCHAR(50),
    was_sampled BOOLEAN DEFAULT FALSE,
    sampling_description TEXT,
    file_hash VARCHAR(64),
    hash_match_reuse BOOLEAN DEFAULT FALSE,
    hash_match_event_id VARCHAR(64),
    filename_flagged BOOLEAN DEFAULT FALSE,
    filename_flag_keywords TEXT,
    failure_reason TEXT,
    retry_count INT DEFAULT 0,
    temp_file_deleted BOOLEAN DEFAULT FALSE,
    received_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    raw_payload JSONB
);

CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
CREATE INDEX IF NOT EXISTS idx_events_event_time ON events(event_time);
CREATE INDEX IF NOT EXISTS idx_events_user_id ON events(user_id);
CREATE INDEX IF NOT EXISTS idx_events_file_hash ON events(file_hash);
CREATE INDEX IF NOT EXISTS idx_events_received_at ON events(received_at);

CREATE TABLE IF NOT EXISTS verdicts (
    id SERIAL PRIMARY KEY,
    event_id VARCHAR(64) NOT NULL REFERENCES events(event_id),
    sensitivity_rating INT,
    categories_detected JSONB DEFAULT '[]'::jsonb,
    category_assessments JSONB DEFAULT '[]'::jsonb,
    overall_context VARCHAR(20),
    escalation_tier VARCHAR(10),
    summary TEXT,
    confidence VARCHAR(10),
    recommendation TEXT,
    analysis_mode VARCHAR(20) NOT NULL,
    ai_provider VARCHAR(20) NOT NULL,
    ai_model VARCHAR(100) NOT NULL,
    input_tokens INT DEFAULT 0,
    output_tokens INT DEFAULT 0,
    estimated_cost_usd DECIMAL(10, 6) DEFAULT 0,
    processing_time_seconds DECIMAL(8, 2),
    affected_count INT DEFAULT 0,
    pii_types_found JSONB DEFAULT '[]'::jsonb,
    notification_required BOOLEAN DEFAULT FALSE,
    notification_sent BOOLEAN DEFAULT FALSE,
    notification_sent_at TIMESTAMP WITH TIME ZONE,
    notification_channel VARCHAR(20),
    notification_reference VARCHAR(255),
    analyst_reviewed BOOLEAN DEFAULT FALSE,
    analyst_reviewed_at TIMESTAMP WITH TIME ZONE,
    analyst_reviewed_by VARCHAR(255),
    analyst_disposition VARCHAR(50),
    analyst_notes TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_verdicts_event_id ON verdicts(event_id);
CREATE INDEX IF NOT EXISTS idx_verdicts_rating ON verdicts(sensitivity_rating);
CREATE INDEX IF NOT EXISTS idx_verdicts_notification ON verdicts(notification_required, notification_sent);
CREATE INDEX IF NOT EXISTS idx_verdicts_provider ON verdicts(ai_provider);
CREATE INDEX IF NOT EXISTS idx_verdicts_created ON verdicts(created_at);
CREATE INDEX IF NOT EXISTS idx_verdicts_escalation_tier ON verdicts(escalation_tier);

CREATE TABLE IF NOT EXISTS file_hashes (
    id SERIAL PRIMARY KEY,
    file_hash VARCHAR(64) UNIQUE NOT NULL,
    first_event_id VARCHAR(64) NOT NULL,
    sensitivity_rating INT,
    category_ids JSONB DEFAULT '[]'::jsonb,
    last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    times_seen INT DEFAULT 1,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_file_hashes_hash ON file_hashes(file_hash);
CREATE INDEX IF NOT EXISTS idx_file_hashes_last_seen ON file_hashes(last_seen_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id SERIAL PRIMARY KEY,
    event_id VARCHAR(64),
    action VARCHAR(100) NOT NULL,
    details JSONB,
    status VARCHAR(20),
    error_message TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_event_id ON audit_log(event_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_log_status ON audit_log(status);

CREATE TABLE IF NOT EXISTS configuration (
    key VARCHAR(100) PRIMARY KEY,
    value TEXT NOT NULL,
    description TEXT,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_by VARCHAR(255)
);

INSERT INTO configuration (key, value, description) VALUES
    ('max_file_size_bytes', '52428800', 'Maximum file size to download (50MB)'),
    ('text_content_limit', '100000', 'Maximum extracted text size in characters'),
    ('hash_reuse_days', '30', 'Days to consider a previous hash analysis valid for reuse'),
    ('dedup_ttl_seconds', '86400', 'Deduplication cache TTL in seconds')
ON CONFLICT (key) DO NOTHING;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INT PRIMARY KEY,
    filename VARCHAR(255) NOT NULL,
    applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

INSERT INTO schema_migrations (version, filename) VALUES
    (1, '001_initial.sql'),
    (5, '005_categories.sql'),
    (6, '006_pii_enrichment.sql')
ON CONFLICT (version) DO NOTHING;

CREATE TABLE IF NOT EXISTS remediations (
    id SERIAL PRIMARY KEY,
    event_id VARCHAR(64) NOT NULL REFERENCES events(event_id),
    requested_by VARCHAR(255) NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    action_type VARCHAR(30) NOT NULL DEFAULT 'remove_sharing',
    status VARCHAR(30) NOT NULL DEFAULT 'pending',
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    permissions_removed INT DEFAULT 0,
    permissions_failed INT DEFAULT 0,
    permission_details JSONB DEFAULT '[]'::jsonb,
    report_sent BOOLEAN DEFAULT FALSE,
    report_sent_at TIMESTAMPTZ,
    report_recipients JSONB DEFAULT '[]'::jsonb,
    error_message TEXT,
    retry_count INT DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_remediations_status ON remediations(status);
CREATE INDEX IF NOT EXISTS idx_remediations_event_id ON remediations(event_id);
