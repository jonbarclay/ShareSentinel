-- Migration 031: Clear stale CWS failures for PointPublishing portals & MySite host
--
-- Background: two more site classes that Set-PnPTenantSite
-- -DisableCompanyWideSharingLinks cannot manage surfaced once provisioning
-- audit events (SiteCollectionCreated) started feeding the CWS action queue:
--
--   * /portals/* — PointPublishing site collections ("... is a
--     PointPublishing site collection. The only valid parameters for this
--     type ...").
--   * The MySite host root (https://<tenant>-my.sharepoint.com) — "The
--     property DisableCompanyWideSharingLinks cannot be set on the MySite
--     host." Personal sites UNDER the host are managed normally.
--
-- Test-CwsUnsupportedSiteType (lib/CwsSiteUrl.ps1),
-- _is_unsupported_site_type (worker cws_drift_detector.py and lifecycle-cron
-- allowlist_enforcer.py), and the drift-remediation SQL pre-filter now skip
-- both classes; this backfill supersedes the failure rows recorded before
-- those filters landed so they drop out of cws_current_failures.

WITH cleanup_scan AS (
    INSERT INTO company_wide_sharing_scans
        (trigger_type, triggered_by, status, dry_run, started_at, completed_at, created_at)
    VALUES ('cleanup', 'migration_031', 'completed', FALSE, NOW(), NOW(), NOW())
    RETURNING id
)
INSERT INTO company_wide_sharing_events
    (scan_id, site_url, site_display_name, site_type, user_upn,
     previous_value, new_value, action, exempt_reason, error_message)
SELECT
    (SELECT id FROM cleanup_scan),
    f.site_url,
    f.site_display_name,
    f.site_type,
    f.user_upn,
    f.previous_value,
    '',
    'skipped_unsupported',
    'unsupported_site_type',
    NULL
FROM cws_current_failures f
WHERE lower(f.site_url) LIKE '%/portals/%'
   OR f.site_url ~* '://[^/]+-my\.sharepoint\.com/?$';
