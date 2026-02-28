-- User notifications table: tracks AI-generated emails sent to file owners
-- after analyst disposition (true_positive or moderate_risk).

CREATE TABLE IF NOT EXISTS user_notifications (
    id SERIAL PRIMARY KEY,
    event_id VARCHAR(64) NOT NULL REFERENCES events(event_id),
    trigger_disposition VARCHAR(30) NOT NULL,  -- 'true_positive' or 'moderate_risk'

    -- Recipient info
    recipient_email VARCHAR(255) NOT NULL,
    recipient_name VARCHAR(255) DEFAULT '',
    recipient_type VARCHAR(30) NOT NULL DEFAULT 'sharing_user',  -- 'sharing_user' or 'site_owner'

    -- AI generation details
    ai_provider VARCHAR(30),
    ai_model VARCHAR(100),
    generated_subject TEXT,
    generated_body TEXT,
    input_tokens INT DEFAULT 0,
    output_tokens INT DEFAULT 0,
    estimated_cost_usd NUMERIC(10, 6) DEFAULT 0,
    category_labels TEXT[],  -- human-readable category names included in email

    -- Delivery status
    status VARCHAR(30) NOT NULL DEFAULT 'pending',  -- 'pending', 'sent', 'failed'
    sent_at TIMESTAMPTZ,
    error_message TEXT,

    -- Override tracking (dev mode)
    override_active BOOLEAN NOT NULL DEFAULT FALSE,
    original_recipient_email VARCHAR(255),

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_notifications_event_id ON user_notifications(event_id);
CREATE INDEX IF NOT EXISTS idx_user_notifications_status ON user_notifications(status);
