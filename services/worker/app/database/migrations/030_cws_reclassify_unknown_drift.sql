-- Migration 030: Reclassify bogus 'Unknown' CWS drift rows -> 'unresolved'
--
-- Background: before Invoke-CwsVerificationSweep.ps1 was rewritten to resolve
-- each site's DisableCompanyWideSharingLinks value per-site (-Identity), the
-- scheduled sweep trusted the BULK Get-PnPTenantSite value, which is always
-- 'Unknown'. Every nightly run therefore recorded ~233K rows as
-- drift_type='drift' / actual_value='Unknown' — ~4.2M bogus "drift" rows in
-- total across scans 140-147.
--
-- Those rows are not real drift: 'Unknown' means the value could not be
-- resolved, not that company-wide sharing is enabled. They (a) flood the daily
-- drift-remediation drain (Invoke-CwsDriftRemediation.ps1), which re-attempts
-- the whole tenant, and (b) inflate the dashboard CWS verification "drift"
-- view (services/dashboard/app/api/allowlist.py).
--
-- The drain query now whitelists confirmed-non-compliant values
-- (actual_value IN ('NotDisabled','NotSpecified')), and the fixed sweep no
-- longer records 'Unknown' as drift. This migration is the one-time data
-- backfill that retires the existing bogus rows so consumers that filter on
-- drift_type='drift' stop seeing them. Genuine drift rows
-- ('NotDisabled'/'NotSpecified') are left untouched.
--
-- Predicate mirrors the drain whitelist in Invoke-CwsDriftRemediation.ps1.

UPDATE cws_verification_events
SET drift_type = 'unresolved'
WHERE drift_type = 'drift'
  AND COALESCE(actual_value, '') NOT IN ('NotDisabled', 'NotSpecified');
