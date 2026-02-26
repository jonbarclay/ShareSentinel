-- 011: Sharing link lifecycle tracking for 180-day expiration policy
-- Tracks anonymous/org-wide sharing links from creation through notification milestones to removal.

CREATE TABLE IF NOT EXISTS sharing_link_lifecycle (
    id SERIAL PRIMARY KEY,
    event_id VARCHAR(64) NOT NULL REFERENCES events(event_id),
    permission_id VARCHAR(255) NOT NULL,
    drive_id VARCHAR(255) NOT NULL,
    item_id VARCHAR(255) NOT NULL,
    user_id VARCHAR(255) NOT NULL,

    link_created_at TIMESTAMPTZ NOT NULL,        -- Day zero (from event_time)
    ms_expiration_at TIMESTAMPTZ,                -- From Graph expirationDateTime (NULL if none)

    -- 'active', 'ms_managed', 'expired_removed', 'manually_removed', 'error'
    status VARCHAR(30) NOT NULL DEFAULT 'active',

    -- Notification milestones (NULL = not yet sent)
    notified_120d_at TIMESTAMPTZ,
    notified_150d_at TIMESTAMPTZ,
    notified_165d_at TIMESTAMPTZ,
    notified_173d_at TIMESTAMPTZ,
    notified_178d_at TIMESTAMPTZ,
    notified_180d_at TIMESTAMPTZ,

    -- Removal tracking
    removal_attempted_at TIMESTAMPTZ,
    removal_succeeded BOOLEAN,
    removal_error TEXT,

    -- Context for notifications (avoid joins)
    file_name VARCHAR(500),
    sharing_scope VARCHAR(50),
    sharing_type VARCHAR(50),
    link_url TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (event_id, permission_id)
);

CREATE INDEX idx_lifecycle_status ON sharing_link_lifecycle(status);
CREATE INDEX idx_lifecycle_active_due ON sharing_link_lifecycle(link_created_at) WHERE status = 'active';
CREATE INDEX idx_lifecycle_user_id ON sharing_link_lifecycle(user_id);
