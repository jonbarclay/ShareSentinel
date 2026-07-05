-- Migration 027: CWS exception-group membership snapshot + delta remediation
-- A 10-minute loop in lifecycle-cron polls the OrgWideSharingLinkException Entra
-- group, diffs against this snapshot, and fires targeted remediation on
-- additions (re-enable CWS on the user's OneDrive) and removals (disable CWS).
-- Steady state: 99% of polls are a no-op.

-- One row per current UPN in the exception group.
CREATE TABLE IF NOT EXISTS cws_exception_group_snapshot (
    user_upn VARCHAR(320) PRIMARY KEY,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cws_exception_group_snapshot_last_seen
    ON cws_exception_group_snapshot(last_seen_at);

-- `trigger_type` on the two scan tables was VARCHAR(20); 'exception_group_delta'
-- is 21 chars. Widen both columns so the new trigger fits without truncation.
ALTER TABLE company_wide_sharing_scans
    ALTER COLUMN trigger_type TYPE VARCHAR(30);

ALTER TABLE cws_verification_scans
    ALTER COLUMN trigger_type TYPE VARCHAR(30);
