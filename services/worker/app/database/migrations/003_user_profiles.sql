-- 003_user_profiles.sql
-- User profile enrichment: cache Graph API user details for analyst display

CREATE TABLE IF NOT EXISTS user_profiles (
    user_id VARCHAR(255) PRIMARY KEY,
    display_name VARCHAR(255),
    job_title VARCHAR(255),
    department VARCHAR(255),
    mail VARCHAR(255),
    manager_name VARCHAR(255),
    photo_base64 TEXT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_profiles_fetched ON user_profiles(fetched_at);
