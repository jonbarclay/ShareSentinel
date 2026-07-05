#requires -Modules @{ ModuleName="PnP.PowerShell"; ModuleVersion="3.0.0" }

<#
.SYNOPSIS
    Daily drainer that remediates CWS drift detected by the verification sweep
    or the audit-event drift detector.

.DESCRIPTION
    Reads unresolved drift rows from cws_verification_events (where
    drift_type='drift' and the site has not been remediated since the drift was
    recorded), filters out allow-listed sites and OneDrives owned by the
    exception group, then runs:

        Set-PnPTenantSite -Identity $url -DisableCompanyWideSharingLinks Disabled

    on each. Verifies post-state with Get-PnPTenantSite and records the
    outcome to company_wide_sharing_events under a new
    company_wide_sharing_scans row with trigger_type='drift_remediation'.

    Counterpart of:
      - Invoke-CwsVerificationSweep.ps1 (detect)
      - Invoke-CwsBulkRemediation.ps1   (monthly full-tenant safety net)

.PARAMETER DryRun
    When $true (default) no Set-PnPTenantSite calls are made. Each decision is
    recorded as action='dry_run_would_disable' in company_wide_sharing_events.

.PARAMETER SinceDays
    Only consider drift rows from the last N days. Default 30.

.PARAMETER MaxSites
    Process at most this many sites then stop. 0 = unlimited.

.PARAMETER SiteUrlFilter
    Optional substring filter on site_url (case-insensitive LIKE %filter%).
    Useful for targeted diagnostic / re-test runs against a single user or
    site. Empty (default) = no filter.

.PARAMETER ScanId
    Pre-existing company_wide_sharing_scans row id to adopt. The dashboard's
    "Scan Now" button creates a pending row and passes the id here, so the
    UI's "Recent Scans" list shows the same row the script updates. If 0
    (default), the script creates its own row with trigger_type='drift_remediation'.
    When ScanId > 0, the row's dry_run column overrides the -DryRun parameter.

.PARAMETER EnvFile
.PARAMETER LogDir

.EXAMPLE
    pwsh -NoProfile -Command "& ./scripts/Invoke-CwsDriftRemediation.ps1 -DryRun \$false"

.EXAMPLE
    # Target a single user's OneDrive for re-test
    pwsh -NoProfile -Command "& ./scripts/Invoke-CwsDriftRemediation.ps1 -DryRun \$false -SiteUrlFilter '10005228_uvu_edu'"
#>

[CmdletBinding()]
param(
    [string]$DryRun = 'true',
    [int]$SinceDays = 30,
    [int]$MaxSites = 0,
    [string]$SiteUrlFilter = '',
    [int]$ScanId = 0,
    [string]$EnvFile = $null,
    [string]$LogDir = $null
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# pwsh -File passes args as strings; a [bool] param can't bind "0"/"1", so the
# param is [string] and coerced here. Mirrors Invoke-CwsExceptionMembershipDelta.ps1.
$DryRunBool = ($DryRun -match '^(true|1|\$true)$')

if (-not $EnvFile) {
    $EnvFile = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    $EnvFile = Join-Path $EnvFile ".env"
}
. (Join-Path $PSScriptRoot 'lib/Invoke-Psql.ps1')
. (Join-Path $PSScriptRoot 'lib/CwsSiteUrl.ps1')
if (-not $LogDir) { $LogDir = Resolve-CwsLogDir -ScriptRoot $PSScriptRoot }

$runStamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmssZ")
$logFile = Join-Path $LogDir "cws_drift_$runStamp.log"
Start-Transcript -Path $logFile -Force | Out-Null

function Write-Stage {
    param([string]$Msg, [string]$Level = 'INFO')
    $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ssZ")
    Write-Host "[$ts] [$Level] $Msg"
}

. (Join-Path $PSScriptRoot 'lib/Invoke-Psql.ps1')
Load-DotEnv -Path $EnvFile
function _Require([string]$name) {
    $v = [Environment]::GetEnvironmentVariable($name)
    if ([string]::IsNullOrWhiteSpace($v)) { throw "Missing env var: $name" }
    return $v
}

$tenantId         = _Require 'AZURE_TENANT_ID'
$clientId         = _Require 'AZURE_CLIENT_ID'
$certRel          = _Require 'AZURE_CERTIFICATE'
$certPass         = _Require 'AZURE_CERTIFICATE_PASS'
$adminUrl         = _Require 'SHAREPOINT_ADMIN_URL'
$exceptionGroupId = _Require 'COMPANY_WIDE_SHARING_EXCEPTION_GROUP_ID'

$repoRoot = Split-Path -Parent $EnvFile
$certPath = if ([IO.Path]::IsPathRooted($certRel)) { $certRel } else { Join-Path $repoRoot ($certRel -replace '^\./', '') }
if (-not (Test-Path $certPath)) { throw "Certificate not found: $certPath" }

Write-Stage "Tenant ID         : $tenantId"
Write-Stage "Admin URL         : $adminUrl"
Write-Stage "Exception group   : $exceptionGroupId"
Write-Stage "Dry run           : $DryRunBool (raw=$DryRun)"
Write-Stage "Since days        : $SinceDays"
Write-Stage "Max sites         : $(if ($MaxSites -eq 0) { 'unlimited' } else { $MaxSites })"
Write-Stage "Site URL filter   : $(if ($SiteUrlFilter) { $SiteUrlFilter } else { '(none)' })"
Write-Stage "Log file          : $logFile"

. (Join-Path $PSScriptRoot 'lib/Invoke-Psql.ps1')

# --- Connect ----------------------------------------------------------------
$securePass = ConvertTo-SecureString -String $certPass -AsPlainText -Force
Write-Stage "Connecting to $adminUrl..."
Connect-PnPOnline -Url $adminUrl -ClientId $clientId -Tenant $tenantId `
    -CertificatePath $certPath -CertificatePassword $securePass `
    -WarningAction SilentlyContinue | Out-Null
Write-Stage "Connected."

# --- Resolve exception group UPNs -------------------------------------------
Write-Stage "Resolving exception group members ($exceptionGroupId)..."
$exceptionUpns = [System.Collections.Generic.HashSet[string]]::new()
$gUrl = "v1.0/groups/$exceptionGroupId/members/microsoft.graph.user?`$select=userPrincipalName&`$top=999"
while ($gUrl) {
    try {
        $gResp = Invoke-PnPGraphMethod -Url $gUrl -Method Get
    } catch {
        throw "Failed to call $gUrl : $($_.Exception.Message)"
    }
    foreach ($m in $gResp.value) {
        if ($m.userPrincipalName) { [void]$exceptionUpns.Add($m.userPrincipalName.ToLowerInvariant()) }
    }
    $gUrl = if ($gResp.PSObject.Properties.Match('@odata.nextLink').Count -gt 0) { $gResp.'@odata.nextLink' } else { $null }
}
Write-Stage "Exception group UPN count: $($exceptionUpns.Count)"
if ($exceptionUpns.Count -eq 0) {
    Write-Stage "Exception group is empty — no OneDrive users will be exempt from CWS enforcement" 'WARN'
}

# Exemption matching uses forward-encoded personal-site segments derived from
# the site URL — the drift row's stored user_upn may be a mangled reverse
# decode (dotted UPNs) and must not be the only exemption signal.
$exceptionSegments = [System.Collections.Generic.HashSet[string]]::new()
foreach ($u in $exceptionUpns) {
    $seg = ConvertTo-CwsPersonalSegment $u
    if ($seg) { [void]$exceptionSegments.Add($seg) }
}

# --- Pull unresolved drift rows --------------------------------------------
# Latest drift event per site, where the site has NO later
# remediated/already_compliant event, and the site is not allow-listed.
$filterClause = if ($SiteUrlFilter) { "AND LOWER(site_url) LIKE LOWER('%$(_EscSql $SiteUrlFilter)%')" } else { '' }
$sql = @"
WITH latest_drift AS (
    SELECT DISTINCT ON (LOWER(RTRIM(site_url, '/')))
        site_url,
        COALESCE(user_upn, '') AS user_upn,
        COALESCE(site_type, 'site') AS site_type,
        COALESCE(actual_value, '') AS actual_value,
        created_at
    FROM cws_verification_events
    WHERE drift_type = 'drift'
      -- Only act on a CONFIRMED non-compliant value. 'NotDisabled' and
      -- 'NotSpecified' mean CWS links are allowed (a genuine drift to fix).
      -- 'Unknown'/''/'not_found'/error mean the value could NOT be resolved and
      -- must never be remediated. The pre-fix bulk verification sweep recorded
      -- ~4.2M 'Unknown' rows as drift_type='drift'; this whitelist keeps the
      -- daily drain from re-attempting the entire tenant. It mirrors the
      -- record-drift-only-on-genuine-non-Disabled logic in
      -- Invoke-CwsVerificationSweep.ps1 (_HandleResult) and the 'NotDisabled'
      -- value written by the audit-event path (cws_drift_detector.py).
      AND COALESCE(actual_value, '') IN ('NotDisabled', 'NotSpecified')
      AND created_at > NOW() - INTERVAL '$SinceDays days'
      -- Skip site classes that Set-PnPTenantSite cannot manage. Mirrors
      -- Test-CwsUnsupportedSiteType in lib/CwsSiteUrl.ps1 so these never
      -- become 'failed' rows. (Existing stale failures are cleared by the
      -- migration 029 backfill.)
      AND site_url NOT ILIKE '%/contentstorage/%'
      AND site_url NOT ILIKE '%/portals/%'
      AND site_url !~* '://[^/]+-public\.sharepoint\.com'
      AND site_url !~* '://[^/]+-my\.sharepoint\.com/?$'
      AND lower(rtrim(site_url, '/')) NOT LIKE '%/sites/contenttypehub'
      $filterClause
    ORDER BY LOWER(RTRIM(site_url, '/')), created_at DESC
)
SELECT d.site_url || '|' || d.user_upn || '|' || d.site_type || '|' || d.actual_value
FROM latest_drift d
WHERE LOWER(RTRIM(d.site_url, '/')) NOT IN (
        SELECT LOWER(RTRIM(site_url, '/')) FROM site_company_wide_sharing_allowlist
    )
  AND NOT EXISTS (
        SELECT 1 FROM company_wide_sharing_events ev
        WHERE LOWER(RTRIM(ev.site_url, '/')) = LOWER(RTRIM(d.site_url, '/'))
          AND ev.action IN ('remediated', 'already_compliant')
          AND ev.created_at > d.created_at
    )
ORDER BY d.site_url;
"@

Write-Stage "Querying unresolved drift rows from last $SinceDays days..."
$rowsOut = Invoke-Psql -TupleOnly -Sql $sql
$rows = New-Object System.Collections.Generic.List[psobject]
$seenRoots = [System.Collections.Generic.HashSet[string]]::new()
foreach ($line in ($rowsOut -split "`r?`n")) {
    $t = $line.Trim()
    if (-not $t) { continue }
    $p = $t -split '\|', 4
    if ($p.Count -ne 4) { continue }
    $url = $p[0].Trim()
    $upn = $p[1].Trim().ToLowerInvariant()
    $type = $p[2].Trim()
    $prevActual = $p[3].Trim()

    # Collapse subsite/folder paths (e.g. /sites/Foo/Marketing) to the
    # site-collection root Set-PnPTenantSite accepts. The CWS setting is
    # per-collection, so the root covers every subsite under it.
    $url = (Get-CwsSiteCollectionRoot $url).ToLowerInvariant()

    # Belt-and-suspenders: the SQL above already excludes these, but guard the
    # post-normalization URL too in case a subsite collapsed onto one.
    if (Test-CwsUnsupportedSiteType $url) { continue }

    # Dedup after normalization — multiple subsites collapse onto one root.
    if (-not $seenRoots.Add($url)) { continue }

    # OneDrive exception-group filter (PowerShell-side; allow list is SQL-side).
    # Match by URL segment (authoritative) OR stored UPN (secondary).
    if ($type -eq 'onedrive') {
        $segment = Get-CwsPersonalSegment $url
        if ($segment -and $exceptionSegments.Contains($segment)) { continue }
        if ($upn -and $exceptionUpns.Contains($upn)) { continue }
    }

    $rows.Add([pscustomobject]@{
        url       = $url
        upn       = $upn
        type      = $type
        prev      = $prevActual
    })
    if ($MaxSites -gt 0 -and $rows.Count -ge $MaxSites) { break }
}
Write-Stage "Loaded $($rows.Count) unresolved drift rows."
if ($rows.Count -eq 0) {
    Write-Stage "Nothing to do."
    try { Disconnect-PnPOnline -ErrorAction SilentlyContinue | Out-Null } catch {}
    Stop-Transcript | Out-Null
    return
}

# --- Create or adopt scan row -----------------------------------------------
if ($ScanId -gt 0) {
    # Adopt a pending row pre-created by the dashboard's "Scan Now" button.
    $rowOut = (Invoke-Psql -TupleOnly -Sql "SELECT status || '|' || dry_run::text FROM company_wide_sharing_scans WHERE id=$ScanId;").ToString().Trim()
    if (-not $rowOut) { throw "scan_id=$ScanId not found in company_wide_sharing_scans" }
    $parts = $rowOut -split '\|', 2
    $rowStatus = $parts[0]
    $rowDryRun = ($parts[1] -eq 't' -or $parts[1] -eq 'true')
    Write-Stage "Adopting scan_id=$ScanId (was status='$rowStatus', dry_run=$rowDryRun); -DryRun param overridden"
    # Row's dry_run is the source of truth — the dashboard set it from env when
    # the row was created; honor whatever value the operator saw at click time.
    $DryRunBool = $rowDryRun
    Invoke-Psql -Sql "UPDATE company_wide_sharing_scans SET status='in_progress', started_at=NOW(), error_message=NULL, exception_group_member_count=$($exceptionUpns.Count) WHERE id=$ScanId;" | Out-Null
    $scanId = $ScanId
} else {
    $dryRunSql = if ($DryRunBool) { 'true' } else { 'false' }
    $insertOut = Invoke-Psql -TupleOnly -Sql "INSERT INTO company_wide_sharing_scans (trigger_type, triggered_by, status, dry_run, started_at, exception_group_member_count) VALUES ('drift_remediation', 'pwsh', 'in_progress', $dryRunSql, NOW(), $($exceptionUpns.Count)) RETURNING id;"
    $scanId = [int](($insertOut -split "`r?`n" | Where-Object { $_ -match '^\d+$' } | Select-Object -First 1))
    if (-not $scanId) { throw "Failed to allocate scan id" }
    Write-Stage "Created scan_id=$scanId"
}

function _InsertEvent {
    param([hashtable]$Row)
    $errSql = if ($Row.error_message) { "'$(_EscSql $Row.error_message)'" } else { 'NULL' }
    $sql = "INSERT INTO company_wide_sharing_events (scan_id, site_url, site_display_name, site_type, user_upn, previous_value, new_value, action, exempt_reason, error_message) VALUES ($($Row.scan_id),'$(_EscSql $Row.site_url)','$(_EscSql $Row.site_display_name)','$(_EscSql $Row.site_type)','$(_EscSql $Row.user_upn)','$(_EscSql $Row.previous_value)','$(_EscSql $Row.new_value)','$(_EscSql $Row.action)','$(_EscSql $Row.exempt_reason)',$errSql);"
    Invoke-Psql -Sql $sql | Out-Null
}

function _GetCws([string]$url) {
    try {
        $s = Get-PnPTenantSite -Identity $url -Detailed -ErrorAction Stop
        return [pscustomobject]@{ ok=$true; cws="$($s.DisableCompanyWideSharingLinks)"; title="$($s.Title)"; lockState="$($s.LockState)"; template="$($s.Template)"; error=$null }
    } catch {
        return [pscustomobject]@{ ok=$false; cws=''; title=''; lockState=''; template=''; error=$_.Exception.Message }
    }
}

$remediated = 0
$wouldRemediate = 0
$alreadyCompliant = 0
$stillFailed = 0
$startTime = Get-Date

trap {
    Write-Stage "TRAPPED: $($_.Exception.Message)" 'ERROR'
    try {
        $em = _Sanitize $_.Exception.Message
        Invoke-Psql -Sql "UPDATE company_wide_sharing_scans SET status='failed', completed_at=NOW(), error_message='$(_EscSql $em)', remediated=$remediated, would_remediate=$wouldRemediate, already_compliant=$alreadyCompliant, errors=$stillFailed, violations_found=($remediated + $wouldRemediate + $stillFailed) WHERE id=$scanId;" | Out-Null
    } catch {}
    try { Disconnect-PnPOnline -ErrorAction SilentlyContinue | Out-Null } catch {}
    Stop-Transcript | Out-Null
    exit 1
}

$i = 0
foreach ($r in $rows) {
    $i++
    $st = _GetCws $r.url
    if (-not $st.ok) {
        $errMsg = $st.error
        # Classify "Cannot get site" — phantom or hard-deleted entries.
        if ($errMsg -match '^Cannot get site ') {
            Write-Stage ("[{0,4}/{1}] {2} -> site not found (skipped)" -f $i, $rows.Count, $r.url) 'WARN'
            _InsertEvent @{
                scan_id=$scanId; site_url=$r.url; site_display_name=''; site_type=$r.type; user_upn=$r.upn
                previous_value=$r.prev; new_value=''
                action='skipped_not_found'; exempt_reason='site_not_found_in_spo'
                error_message=$null
            }
            continue
        }
        Write-Stage ("[{0,4}/{1}] {2} -> Get failed: {3}" -f $i, $rows.Count, $r.url, $errMsg) 'WARN'
        _InsertEvent @{
            scan_id=$scanId; site_url=$r.url; site_display_name=''; site_type=$r.type; user_upn=$r.upn
            previous_value=$r.prev; new_value=''
            action='failed'; exempt_reason='get_site_failed'
            error_message=(_Sanitize $errMsg)
        }
        $stillFailed++
        continue
    }

    $prev = $st.cws
    if ($prev -eq 'Disabled') {
        # Drift was recorded earlier but has since been remediated (e.g.,
        # the monthly bulk caught it). Record idempotently and move on.
        _InsertEvent @{
            scan_id=$scanId; site_url=$r.url; site_display_name=$st.title; site_type=$r.type; user_upn=$r.upn
            previous_value=$prev; new_value=$prev
            action='already_compliant'; exempt_reason='cws_already_disabled'
            error_message=$null
        }
        $alreadyCompliant++
        continue
    }

    if ($DryRunBool) {
        Write-Stage ("[{0,4}/{1}] {2} -> DRY-RUN would set Disabled (cws_now={3})" -f $i, $rows.Count, $r.url, $prev)
        _InsertEvent @{
            scan_id=$scanId; site_url=$r.url; site_display_name=$st.title; site_type=$r.type; user_upn=$r.upn
            previous_value=$prev; new_value='Disabled'
            action='dry_run_would_disable'; exempt_reason=''
            error_message=$null
        }
        $wouldRemediate++
        continue
    }

    # Live remediation, with the same retry/error classification as the bulk script.
    $errMsg = $null
    $act = 'remediated'
    $exemptR = ''
    $attempt = 0
    $maxAttempts = 3
    while ($attempt -lt $maxAttempts) {
        $attempt++
        try {
            Set-PnPTenantSite -Identity $r.url `
                -DisableCompanyWideSharingLinks Disabled `
                -ErrorAction Stop `
                -WarningAction SilentlyContinue 2>$null | Out-Null
            $errMsg = $null
            break
        } catch {
            $e = $_.Exception.Message
            if ($e -match '429|throttle|IdleSession') {
                $backoff = [int]([Math]::Min(180, 15 * [Math]::Pow(2, $attempt - 1)))
                Write-Stage ("  throttled, sleeping {0}s (attempt {1}/{2})" -f $backoff, $attempt, $maxAttempts) 'WARN'
                Start-Sleep -Seconds $backoff
                continue
            } elseif ($e -match '\b401\b|Unauthorized' -and $attempt -lt $maxAttempts) {
                try {
                    Connect-PnPOnline -Url $adminUrl -ClientId $clientId -Tenant $tenantId -CertificatePath $certPath -CertificatePassword $securePass -WarningAction SilentlyContinue | Out-Null
                } catch {}
                continue
            } else {
                $errMsg = $e
                $act = 'failed'
                if ($e -match 'Access is denied') {
                    if ($st.template -like 'TEAMCHANNEL*' -and $st.lockState -eq 'ReadOnly') {
                        $act = 'skipped'
                        $exemptR = 'archived_teams_channel'
                        $errMsg = $null
                    }
                }
                break
            }
        }
    }

    $after = _GetCws $r.url
    $newCws = if ($after.ok) { $after.cws } else { 'unknown' }
    if ($act -eq 'remediated' -and $newCws -eq 'Disabled') {
        Write-Stage ("[{0,4}/{1}] {2} -> remediated ({3} -> {4})" -f $i, $rows.Count, $r.url, $prev, $newCws)
        _InsertEvent @{
            scan_id=$scanId; site_url=$r.url; site_display_name=$st.title; site_type=$r.type; user_upn=$r.upn
            previous_value=$prev; new_value=$newCws
            action='remediated'; exempt_reason=''
            error_message=$null
        }
        $remediated++
    } elseif ($act -eq 'skipped') {
        Write-Stage ("[{0,4}/{1}] {2} -> skipped ({3})" -f $i, $rows.Count, $r.url, $exemptR)
        _InsertEvent @{
            scan_id=$scanId; site_url=$r.url; site_display_name=$st.title; site_type=$r.type; user_upn=$r.upn
            previous_value=$prev; new_value=$prev
            action='skipped'; exempt_reason=$exemptR
            error_message=$null
        }
    } else {
        Write-Stage ("[{0,4}/{1}] {2} -> FAILED cws_after={3} err={4}" -f $i, $rows.Count, $r.url, $newCws, $errMsg) 'ERROR'
        _InsertEvent @{
            scan_id=$scanId; site_url=$r.url; site_display_name=$st.title; site_type=$r.type; user_upn=$r.upn
            previous_value=$prev; new_value=$newCws
            action='failed'; exempt_reason=''
            error_message=(_Sanitize $errMsg)
        }
        $stillFailed++
    }

    if (($i % 20) -eq 0) {
        Invoke-Psql -Sql "UPDATE company_wide_sharing_scans SET total_sites_scanned=$i, remediated=$remediated, would_remediate=$wouldRemediate, already_compliant=$alreadyCompliant, errors=$stillFailed, violations_found=($remediated + $wouldRemediate + $stillFailed) WHERE id=$scanId;" | Out-Null
    }
}

Invoke-Psql -Sql "UPDATE company_wide_sharing_scans SET status='completed', completed_at=NOW(), total_sites_scanned=$($rows.Count), remediated=$remediated, would_remediate=$wouldRemediate, already_compliant=$alreadyCompliant, errors=$stillFailed, violations_found=($remediated + $wouldRemediate + $stillFailed) WHERE id=$scanId;" | Out-Null

$elapsed = (Get-Date) - $startTime
Write-Stage "DONE scan_id=$scanId total=$($rows.Count) rem=$remediated would=$wouldRemediate already=$alreadyCompliant err=$stillFailed elapsed=$($elapsed.ToString('hh\:mm\:ss'))"

try { Disconnect-PnPOnline -ErrorAction SilentlyContinue | Out-Null } catch {}
Stop-Transcript | Out-Null
