-- Second-look AI review columns on verdicts
ALTER TABLE verdicts
    ADD COLUMN IF NOT EXISTS second_look_performed BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS second_look_provider VARCHAR(20),
    ADD COLUMN IF NOT EXISTS second_look_model VARCHAR(100),
    ADD COLUMN IF NOT EXISTS second_look_agreed BOOLEAN,
    ADD COLUMN IF NOT EXISTS second_look_categories JSONB DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS second_look_tier VARCHAR(10),
    ADD COLUMN IF NOT EXISTS second_look_summary TEXT,
    ADD COLUMN IF NOT EXISTS second_look_cost_usd NUMERIC(10,6) DEFAULT 0;
