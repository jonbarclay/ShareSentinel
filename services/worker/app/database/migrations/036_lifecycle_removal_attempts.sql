-- Retry-with-cap for sharing-link removal failures.
-- Previously ANY removal failure set status='error' permanently; the daily
-- processor only selects 'active' rows, so a transient Graph failure (5xx,
-- timeout, throttle) left an expired link live forever with no retry.
-- removal_attempts lets the processor retry daily and only park the row as
-- 'error' after repeated failures.

ALTER TABLE sharing_link_lifecycle
    ADD COLUMN IF NOT EXISTS removal_attempts INTEGER NOT NULL DEFAULT 0;
