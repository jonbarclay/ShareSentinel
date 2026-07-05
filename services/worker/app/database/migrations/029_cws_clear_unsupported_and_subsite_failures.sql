-- Migration 029: Clear stale CWS failures for unsupported site types & subsites
--
-- Background: Set-PnPTenantSite -DisableCompanyWideSharingLinks cannot manage
-- SharePoint Embedded containers (/contentstorage/*), legacy public sites
-- (*-public.sharepoint.com), or the Content Type Hub. Remediation also cannot
-- target a subsite/folder URL (e.g. /sites/Foo/Marketing) — the CWS setting is
-- per site-collection, so the root (/sites/Foo) is the only valid identity.
--
-- Before the scan/remediation/diagnostic scripts learned to skip these (via
-- lib/CwsSiteUrl.ps1), every run re-attempted them and re-recorded
-- action='failed', so they were stuck permanently in the cws_current_failures
-- view. This migration supersedes each currently-failing row with a terminal
-- 'skipped_*' event so it drops out of that view, and re-categorises any
-- mislabelled diagnostics rows. It is a one-time data backfill; the script
-- changes prevent these from recurring.
--
-- The unsupported-type predicate below mirrors Test-CwsUnsupportedSiteType in
-- scripts/lib/CwsSiteUrl.ps1 and _is_unsupported_site_type in
-- services/worker/app/pipeline/cws_drift_detector.py.

WITH cleanup_scan AS (
    INSERT INTO company_wide_sharing_scans
        (trigger_type, triggered_by, status, dry_run, started_at, completed_at, created_at)
    VALUES ('cleanup', 'migration_029', 'completed', FALSE, NOW(), NOW(), NOW())
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
    CASE
        WHEN lower(f.site_url) LIKE '%/contentstorage/%'
          OR f.site_url ~* '://[^/]+-public\.sharepoint\.com'
          OR lower(rtrim(f.site_url, '/')) LIKE '%/sites/contenttypehub'
            THEN 'skipped_unsupported'
        ELSE 'skipped_subsite'
    END AS action,
    CASE
        WHEN lower(f.site_url) LIKE '%/contentstorage/%'
          OR f.site_url ~* '://[^/]+-public\.sharepoint\.com'
          OR lower(rtrim(f.site_url, '/')) LIKE '%/sites/contenttypehub'
            THEN 'unsupported_site_type'
        ELSE 'subsite_path_normalized'
    END AS exempt_reason,
    NULL
FROM cws_current_failures f
WHERE lower(f.site_url) LIKE '%/contentstorage/%'
   OR f.site_url ~* '://[^/]+-public\.sharepoint\.com'
   OR lower(rtrim(f.site_url, '/')) LIKE '%/sites/contenttypehub'
   OR f.site_url ~* '^https?://[^/]+/(sites|teams|personal)/[^/]+/.+';

-- Re-categorise any diagnostics rows that mislabelled an unsupported site type
-- (these were classified by the OneDrive-oriented toolkit as a1_license_cap,
-- lockstate_locked, unknown, etc.).
UPDATE cws_failure_diagnostics
SET failure_category = 'unsupported_site_type',
    diagnostic_summary = 'Site class is not manageable via Set-PnPTenantSite -DisableCompanyWideSharingLinks (SharePoint Embedded container, legacy public site, or Content Type Hub). Excluded from the CWS policy.',
    updated_at = NOW()
WHERE failure_category <> 'unsupported_site_type'
  AND ( lower(site_url) LIKE '%/contentstorage/%'
     OR site_url ~* '://[^/]+-public\.sharepoint\.com'
     OR lower(rtrim(site_url, '/')) LIKE '%/sites/contenttypehub' );
