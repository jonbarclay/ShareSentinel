-- 020: Admin panel — dashboard_users tracking + configuration table enhancements.

-- Track users who have logged into the dashboard
CREATE TABLE IF NOT EXISTS dashboard_users (
    id SERIAL PRIMARY KEY,
    oid VARCHAR(255) UNIQUE NOT NULL,
    email VARCHAR(255) NOT NULL,
    display_name VARCHAR(255) NOT NULL,
    groups JSONB DEFAULT '[]'::jsonb,
    roles JSONB DEFAULT '[]'::jsonb,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dashboard_users_email ON dashboard_users(email);
CREATE INDEX IF NOT EXISTS idx_dashboard_users_last_seen ON dashboard_users(last_seen_at);

-- Enhance configuration table with UI grouping and type metadata
ALTER TABLE configuration ADD COLUMN IF NOT EXISTS category VARCHAR(50) DEFAULT 'general';
ALTER TABLE configuration ADD COLUMN IF NOT EXISTS data_type VARCHAR(20) DEFAULT 'string';
ALTER TABLE configuration ADD COLUMN IF NOT EXISTS display_name VARCHAR(255);

-- Seed admin-configurable settings.
-- Empty value = "use env var default". Only non-empty DB values override env vars.
INSERT INTO configuration (key, value, description, category, data_type, display_name) VALUES
    -- Email
    ('email_from',                     '', 'Sender address for all outgoing emails',                   'email',         'string',  'From Address'),
    ('email_to',                       '', 'Comma-separated analyst notification recipients',          'email',         'string',  'Analyst Recipients'),
    ('security_email',                 '', 'Security team BCC address',                                'email',         'string',  'Security Email'),
    -- AI
    ('ai_provider',                    '', 'Primary AI provider (anthropic, openai, gemini)',           'ai',            'select',  'AI Provider'),
    ('ai_temperature',                 '', 'AI sampling temperature (0.0–1.0)',                        'ai',            'float',   'Temperature'),
    ('anthropic_model',                '', 'Anthropic model name',                                     'ai',            'string',  'Anthropic Model'),
    ('openai_model',                   '', 'OpenAI model name',                                        'ai',            'string',  'OpenAI Model'),
    ('gemini_model',                   '', 'Google Gemini model name',                                 'ai',            'string',  'Gemini Model'),
    ('second_look_enabled',            '', 'Enable cross-provider AI verification',                    'ai',            'boolean', 'Second Look Enabled'),
    ('second_look_provider',           '', 'Second-look AI provider',                                  'ai',            'select',  'Second Look Provider'),
    ('second_look_model',              '', 'Second-look AI model name',                                'ai',            'string',  'Second Look Model'),
    -- Notifications
    ('analyst_notification_enabled',   '', 'Enable analyst alert emails',                              'notifications', 'boolean', 'Analyst Notifications'),
    ('notification_channels',          '', 'Comma-separated channels (email, jira)',                   'notifications', 'string',  'Notification Channels'),
    ('notify_on_folder_share',         '', 'Notify analysts on folder shares',                         'notifications', 'boolean', 'Notify on Folder Share'),
    ('notify_on_failure',              '', 'Notify analysts on processing failures',                   'notifications', 'boolean', 'Notify on Failure'),
    ('user_notification_enabled',      '', 'Enable post-disposition emails to file owners',            'notifications', 'boolean', 'User Notifications'),
    ('user_notification_bcc',          '', 'BCC address for user notification emails',                 'notifications', 'string',  'User Notification BCC'),
    ('user_notification_override_email','','Override recipient for user notifications (testing)',       'notifications', 'string',  'User Notification Override'),
    -- Processing
    ('max_file_size_bytes',            '', 'Maximum file size to download (bytes)',                    'processing',    'int',     'Max File Size (bytes)'),
    ('text_content_limit',             '', 'Maximum text content sent to AI (bytes)',                  'processing',    'int',     'Text Content Limit'),
    ('hash_reuse_days',                '', 'Days to reuse file hash results',                         'processing',    'int',     'Hash Reuse Days'),
    ('max_concurrent_jobs',            '', 'Maximum concurrent processing jobs',                      'processing',    'int',     'Max Concurrent Jobs'),
    ('max_concurrent_av_jobs',         '', 'Maximum concurrent audio/video jobs',                     'processing',    'int',     'Max Concurrent A/V Jobs'),
    -- Lifecycle
    ('lifecycle_check_interval_hours', '', 'Hours between lifecycle milestone checks',                'lifecycle',     'int',     'Check Interval (hours)'),
    ('lifecycle_max_days',             '', 'Days before sharing links are removed',                   'lifecycle',     'int',     'Max Lifecycle Days'),
    -- Audit
    ('audit_poll_enabled',             '', 'Enable audit log polling',                                'audit',         'boolean', 'Audit Polling Enabled'),
    ('audit_poll_interval_minutes',    '', 'Minutes between audit log polls',                         'audit',         'int',     'Poll Interval (minutes)'),
    -- General
    ('dashboard_url',                  '', 'Dashboard base URL (used in emails and links)',           'general',       'string',  'Dashboard URL'),
    ('log_level',                      '', 'Logging level (DEBUG, INFO, WARNING, ERROR)',             'general',       'select',  'Log Level')
ON CONFLICT (key) DO UPDATE SET
    category = EXCLUDED.category,
    data_type = EXCLUDED.data_type,
    display_name = EXCLUDED.display_name,
    description = EXCLUDED.description;

-- Exclude pre-existing internal settings that got the default 'general' category
UPDATE configuration SET category = NULL
WHERE key IN ('dedup_ttl_seconds', 'sensitivity_threshold')
  AND display_name IS NULL;
