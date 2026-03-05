-- 021: Folder content snapshots for weekly rescan change detection.
-- Stores cTag-based snapshots of folder contents to detect new/modified files
-- without downloading every file on each rescan cycle.

-- One row per file per folder — tracks content state via cTag
CREATE TABLE IF NOT EXISTS folder_content_snapshots (
    id SERIAL PRIMARY KEY,
    parent_event_id VARCHAR(512) NOT NULL,
    folder_drive_id VARCHAR(255) NOT NULL,
    folder_item_id VARCHAR(255) NOT NULL,
    child_item_id VARCHAR(255) NOT NULL,
    child_name VARCHAR(500),
    child_size BIGINT DEFAULT 0,
    child_mime_type VARCHAR(255),
    child_web_url TEXT,
    child_parent_path TEXT,
    child_ctag VARCHAR(500),
    child_etag VARCHAR(500),
    file_hash VARCHAR(64),
    last_verdict_tier VARCHAR(20),
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_scanned_at TIMESTAMPTZ,
    times_scanned INT NOT NULL DEFAULT 1,
    deleted_at TIMESTAMPTZ,

    UNIQUE (parent_event_id, child_item_id)
);

CREATE INDEX IF NOT EXISTS idx_fcs_parent_event ON folder_content_snapshots(parent_event_id);
CREATE INDEX IF NOT EXISTS idx_fcs_folder ON folder_content_snapshots(folder_drive_id, folder_item_id);
CREATE INDEX IF NOT EXISTS idx_fcs_child_item ON folder_content_snapshots(child_item_id);

-- One row per folder — tracks rescan scheduling state
CREATE TABLE IF NOT EXISTS folder_rescan_state (
    id SERIAL PRIMARY KEY,
    parent_event_id VARCHAR(512) UNIQUE NOT NULL,
    folder_drive_id VARCHAR(255) NOT NULL,
    folder_item_id VARCHAR(255) NOT NULL,
    folder_name VARCHAR(500),
    last_rescan_at TIMESTAMPTZ,
    last_rescan_status VARCHAR(30) NOT NULL DEFAULT 'pending',
    new_files_found INT NOT NULL DEFAULT 0,
    modified_files_found INT NOT NULL DEFAULT 0,
    total_files INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_frs_rescan_due
    ON folder_rescan_state(last_rescan_at)
    WHERE last_rescan_status NOT IN ('link_removed', 'folder_deleted');

-- Seed rescan configuration rows
INSERT INTO configuration (key, value, description, category, data_type, display_name) VALUES
    ('folder_rescan_enabled',        '', 'Enable weekly folder rescan for new/modified files',  'processing', 'boolean', 'Folder Rescan Enabled'),
    ('folder_rescan_interval_hours', '', 'Hours between folder rescan cycles (default 168 = 7 days)', 'processing', 'int', 'Folder Rescan Interval (hours)'),
    ('folder_rescan_batch_size',     '', 'Max folders to rescan per cycle',                     'processing', 'int',     'Folder Rescan Batch Size')
ON CONFLICT (key) DO UPDATE SET
    category = EXCLUDED.category,
    data_type = EXCLUDED.data_type,
    display_name = EXCLUDED.display_name,
    description = EXCLUDED.description;
