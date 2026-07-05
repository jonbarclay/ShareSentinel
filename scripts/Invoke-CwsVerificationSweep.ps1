#requires -Modules @{ ModuleName="PnP.PowerShell"; ModuleVersion="3.0.0" }

<#
.SYNOPSIS
    Detect-only tenant-wide verification sweep of the
    "Company-Wide Sharing Links" policy.

.DESCRIPTION
    Enumerates every site in the tenant via `Get-PnPTenantSite -Detailed
    -IncludeOneDriveSites` (the SharePoint admin API view — most authoritative
    tenant-wide list, more complete than Graph /sites/delta for finding
    newly provisioned sites) as the inventory + exemption filter. The bulk
    enumeration does NOT carry a usable DisableCompanyWideSharingLinks value
    (it always returns 'Unknown'); the real value is only returned by a per-site
    `Get-PnPTenantSite -Identity` call, so each non-exempt site is resolved by a
    pool of read-only worker threads (-ThrottleLimit, default 8). For each site:

      - OneDrive personal site whose owning UPN is in the configured
        exception group  -> SKIP (no event written).
      - SharePoint site URL on site_company_wide_sharing_allowlist -> SKIP.
      - Real (per-site) DisableCompanyWideSharingLinks == 'Disabled' -> OK.
      - Real value is a genuine non-Disabled state -> record a 'drift' row.
      - Value cannot be resolved ('Unknown'/error/not_found) -> counted as an
        error, NOT recorded as drift (avoids the old all-sites-look-like-drift bug).

    This is the detect half of the CWS pipeline. Remediation is performed by
    Invoke-CwsDriftRemediation.ps1 (daily) and Invoke-CwsBulkRemediation.ps1
    (monthly safety net).

.PARAMETER ScanId
    Pre-existing cws_verification_scans row id to update. The lifecycle-cron
    loop creates a 'pending' row and passes its id here. If 0 (default), the
    script creates its own row with trigger_type='ps_verification'.

.PARAMETER MaxSites
    Process at most this many sites then stop. 0 = unlimited.

.PARAMETER EnvFile
    Path to the project .env file. Default: ../.env relative to the script.

.PARAMETER LogDir
    Directory for tee'd log files. Default: scripts/logs alongside the script.

.EXAMPLE
    pwsh -NoProfile -Command "& ./scripts/Invoke-CwsVerificationSweep.ps1 -MaxSites 1000"

.EXAMPLE
    pwsh -NoProfile -Command "& ./scripts/Invoke-CwsVerificationSweep.ps1 -ScanId 42"
#>

[CmdletBinding()]
param(
    [int]$ScanId = 0,
    [int]$MaxSites = 0,
    [int]$ThrottleLimit = 8,
    [string]$EnvFile = $null,
    [string]$LogDir = $null
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $EnvFile) {
    $EnvFile = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    $EnvFile = Join-Path $EnvFile ".env"
}
. (Join-Path $PSScriptRoot 'lib/Invoke-Psql.ps1')
. (Join-Path $PSScriptRoot 'lib/CwsSiteUrl.ps1')
if (-not $LogDir) { $LogDir = Resolve-CwsLogDir -ScriptRoot $PSScriptRoot }

$runStamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmssZ")
$logFile = Join-Path $LogDir "cws_verification_$runStamp.log"
Start-Transcript -Path $logFile -Force | Out-Null

function Write-Stage {
    param([string]$Msg, [string]$Level = 'INFO')
    $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ssZ")
    Write-Host "[$ts] [$Level] $Msg"
}

. (Join-Path $PSScriptRoot 'lib/Invoke-Psql.ps1')
Load-DotEnv -Path $EnvFile

function _Require([string]$name) {
    $v = [System.Environment]::GetEnvironmentVariable($name)
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
$certPath = if ([System.IO.Path]::IsPathRooted($certRel)) { $certRel } else { Join-Path $repoRoot ($certRel -replace '^\./', '') }
if (-not (Test-Path $certPath)) { throw "Certificate not found: $certPath" }

Write-Stage "Tenant ID         : $tenantId"
Write-Stage "Client ID         : $clientId"
Write-Stage "Admin URL         : $adminUrl"
Write-Stage "Cert path         : $certPath"
Write-Stage "Exception group   : $exceptionGroupId"
Write-Stage "Max sites         : $(if ($MaxSites -eq 0) { 'unlimited' } else { $MaxSites })"
Write-Stage "Scan id           : $(if ($ScanId -eq 0) { 'new (will allocate)' } else { $ScanId })"
Write-Stage "Log file          : $logFile"

# --- Postgres helpers (dual-mode: host docker exec or in-container psql) ----
. (Join-Path $PSScriptRoot 'lib/Invoke-Psql.ps1')

# --- Connect to PnP ---------------------------------------------------------
$securePass = ConvertTo-SecureString -String $certPass -AsPlainText -Force
Write-Stage "Connecting to $adminUrl..."
Connect-PnPOnline -Url $adminUrl `
    -ClientId $clientId `
    -Tenant $tenantId `
    -CertificatePath $certPath `
    -CertificatePassword $securePass `
    -WarningAction SilentlyContinue | Out-Null
Write-Stage "Connected."

# --- Resolve exception group UPNs via Graph ---------------------------------
Write-Stage "Resolving exception group members ($exceptionGroupId)..."
$exceptionUpns = [System.Collections.Generic.HashSet[string]]::new()
$gUrl = "v1.0/groups/$exceptionGroupId/members/microsoft.graph.user?`$select=id,userPrincipalName,accountEnabled&`$top=999"
while ($gUrl) {
    try {
        $gResp = Invoke-PnPGraphMethod -Url $gUrl -Method Get
    } catch {
        throw "Failed to call $gUrl : $($_.Exception.Message)"
    }
    foreach ($m in $gResp.value) {
        if ($m.userPrincipalName) {
            [void]$exceptionUpns.Add($m.userPrincipalName.ToLowerInvariant())
        }
    }
    $gUrl = if ($gResp.PSObject.Properties.Match('@odata.nextLink').Count -gt 0) { $gResp.'@odata.nextLink' } else { $null }
}
Write-Stage "Exception group UPN count: $($exceptionUpns.Count)"
if ($exceptionUpns.Count -eq 0) {
    # The Graph enumeration succeeded (otherwise the loop above would have
    # thrown). An empty group means no users are exempt — a legitimate config.
    # Logged loudly so an operator notices if it's unintended.
    Write-Stage "Exception group is empty — no OneDrive users will be exempt from CWS enforcement" 'WARN'
}

# Exemption matching is done on forward-encoded personal-site segments, never
# by reverse-decoding a URL to a UPN (that direction is ambiguous and dropped
# exemptions for any member with a dot/underscore in their UPN local part).
$exceptionSegments = [System.Collections.Generic.HashSet[string]]::new()
foreach ($u in $exceptionUpns) {
    $seg = ConvertTo-CwsPersonalSegment $u
    if ($seg) { [void]$exceptionSegments.Add($seg) }
}

# --- Load site allow list from postgres -------------------------------------
Write-Stage "Loading site allow list from postgres..."
$allowlistRaw = Invoke-Psql -TupleOnly -Sql "SELECT LOWER(RTRIM(site_url, '/')) FROM site_company_wide_sharing_allowlist;"
$allowlist = [System.Collections.Generic.HashSet[string]]::new()
foreach ($line in ($allowlistRaw -split "`r?`n")) {
    $t = $line.Trim()
    if ($t) { [void]$allowlist.Add($t) }
}
Write-Stage "Allow-listed sites: $($allowlist.Count)"

# --- Create or use scan row -------------------------------------------------
if ($ScanId -gt 0) {
    $statusOut = (Invoke-Psql -TupleOnly -Sql "SELECT status FROM cws_verification_scans WHERE id=$ScanId;").ToString().Trim()
    if (-not $statusOut) { throw "scan_id=$ScanId not found in cws_verification_scans" }
    Write-Stage "Using existing scan_id=$ScanId (was status='$statusOut')"
    Invoke-Psql -Sql "UPDATE cws_verification_scans SET status='in_progress', started_at=NOW(), error_message=NULL, exception_group_member_count=$($exceptionUpns.Count) WHERE id=$ScanId;" | Out-Null
} else {
    $insertOut = Invoke-Psql -TupleOnly -Sql "INSERT INTO cws_verification_scans (trigger_type, triggered_by, status, started_at, exception_group_member_count) VALUES ('ps_verification', 'pwsh', 'in_progress', NOW(), $($exceptionUpns.Count)) RETURNING id;"
    $ScanId = [int](($insertOut -split "`r?`n" | Where-Object { $_ -match '^\d+$' } | Select-Object -First 1))
    if (-not $ScanId) { throw "Failed to allocate scan id from postgres" }
    Write-Stage "Created scan_id=$ScanId"
}

# --- Helpers ----------------------------------------------------------------
# Best-effort reverse decode for DISPLAY/DB purposes ONLY (splitting at the
# first underscore mangles dotted UPNs: jon_barclay_uvu_edu -> jon@barclay...).
# Exemption decisions use $exceptionSegments (forward-encoded) instead.
function _UpnFromOneDrive([string]$url) {
    $lower = $url.ToLowerInvariant().TrimEnd('/')
    $marker = '/personal/'
    $idx = $lower.IndexOf($marker)
    if ($idx -lt 0) { return '' }
    $rest = $lower.Substring($idx + $marker.Length)
    $encoded = $rest.Split('/')[0]
    if ([string]::IsNullOrEmpty($encoded) -or -not $encoded.Contains('_')) { return '' }
    $firstUs = $encoded.IndexOf('_')
    $user = $encoded.Substring(0, $firstUs)
    $domain = $encoded.Substring($firstUs + 1).Replace('_', '.')
    if (-not $user -or -not $domain) { return '' }
    return "$user@$domain"
}

function Insert-DriftBatch {
    param([Parameter(Mandatory)][System.Collections.Generic.List[psobject]]$Rows)
    if ($Rows.Count -eq 0) { return }
    $sb = [System.Text.StringBuilder]::new()
    [void]$sb.Append("INSERT INTO cws_verification_events (scan_id, site_url, site_display_name, site_type, user_upn, expected_value, actual_value, drift_type, exempt_reason, trigger_source, error_message) VALUES ")
    $first = $true
    foreach ($r in $Rows) {
        if (-not $first) { [void]$sb.Append(',') }
        $first = $false
        $errSql = if ($r.error_message) { "'$(_EscSql $r.error_message)'" } else { 'NULL' }
        [void]$sb.AppendFormat(
            "({0},'{1}','{2}','{3}','{4}','{5}','{6}','{7}','{8}','{9}',{10})",
            $r.scan_id,
            (_EscSql $r.site_url),
            (_EscSql $r.site_display_name),
            (_EscSql $r.site_type),
            (_EscSql $r.user_upn),
            (_EscSql $r.expected_value),
            (_EscSql $r.actual_value),
            (_EscSql $r.drift_type),
            (_EscSql $r.exempt_reason),
            (_EscSql $r.trigger_source),
            $errSql
        )
    }
    [void]$sb.Append(';')
    Invoke-Psql -Sql $sb.ToString() | Out-Null
}

function Update-ScanCounters {
    param(
        [int]$Total, [int]$Checked, [int]$Drifts, [int]$Ok, [int]$Errors,
        [string]$Status = 'in_progress',
        [string]$ErrorMessage = $null
    )
    $completedClause = if ($Status -eq 'completed' -or $Status -eq 'failed') { ", completed_at = NOW()" } else { '' }
    $errClause = if ($ErrorMessage) { ", error_message = '$(_EscSql $ErrorMessage)'" } else { '' }
    $sql = "UPDATE cws_verification_scans SET status='$Status', total_candidates=$Total, checked=$Checked, drifts_found=$Drifts, ok_count=$Ok, errors=$Errors, exception_group_member_count=$($script:exceptionUpns.Count)$completedClause$errClause WHERE id=$ScanId;"
    Invoke-Psql -Sql $sql | Out-Null
}

# --- Unsupported-site-type filter ------------------------------------------
# Get-PnPTenantSite returns site collections that look like SharePoint sites
# but don't support Set-PnPTenantSite -DisableCompanyWideSharingLinks (Embedded
# containers, legacy public sites, Content Type Hub). The classification lives
# in lib/CwsSiteUrl.ps1 (Test-CwsUnsupportedSiteType) so the scan, remediation,
# and diagnostic scripts all agree with the Python audit-event path.

# --- Counters ---------------------------------------------------------------
$total = 0                  # sites seen during enumeration (incl. skipped)
$candidates = 0             # non-exempt sites queued for per-site resolution
$checked = 0                # results received from workers
$drifts = 0                 # confirmed real DisableCompanyWideSharingLinks <> Disabled
$okCount = 0                # confirmed real value == Disabled
$errors = 0                 # per-site read failed / value could not be resolved
$skippedAllowlist = 0
$skippedExceptionGroup = 0
$skippedUnsupported = 0
$startTime = Get-Date
$jobs = @()
$batch = [System.Collections.Generic.List[psobject]]::new()
$BATCH_SIZE = 200
$PROGRESS_EVERY = 1000

# --- Concurrent queues + read-only resolver pool ----------------------------
# DisableCompanyWideSharingLinks is NOT populated by the bulk Get-PnPTenantSite
# enumeration — it always comes back 'Unknown'. The real value is only returned
# by a per-site Get-PnPTenantSite -Identity call. So the bulk enumeration is used
# purely as the site inventory + exemption filter, and the real value is resolved
# by a pool of read-only workers. Mirrors the worker scaffolding in
# Invoke-CwsBulkRemediation.ps1, minus the Set-PnPTenantSite write.
$workQueue = [System.Collections.Concurrent.BlockingCollection[psobject]]::new()
$resultQueue = [System.Collections.Concurrent.BlockingCollection[psobject]]::new()

$workerScript = {
    param($Work, $Results, $Cfg)
    $ErrorActionPreference = 'Stop'
    Import-Module PnP.PowerShell -DisableNameChecking -WarningAction SilentlyContinue | Out-Null
    $sp = ConvertTo-SecureString -String $Cfg.CertPass -AsPlainText -Force
    try {
        Connect-PnPOnline -Url $Cfg.AdminUrl -ClientId $Cfg.ClientId -Tenant $Cfg.TenantId `
            -CertificatePath $Cfg.CertPath -CertificatePassword $sp -WarningAction SilentlyContinue | Out-Null
    } catch {
        $Results.Add([pscustomobject]@{ worker_init_failed = $true; error_message = "worker Connect-PnPOnline failed: $($_.Exception.Message)" })
        return
    }
    foreach ($item in $Work.GetConsumingEnumerable()) {
        $actual = ''
        $title = ''
        $errMsg = $null
        $attempt = 0
        $maxAttempts = 3
        while ($attempt -lt $maxAttempts) {
            $attempt++
            try {
                $s = Get-PnPTenantSite -Identity $item.url -Detailed -ErrorAction Stop
                $actual = "$($s.DisableCompanyWideSharingLinks)"
                $title = "$($s.Title)"
                break
            } catch {
                $e = $_.Exception.Message
                if ($e -match '429|throttle|IdleSession') {
                    Start-Sleep -Seconds ([int]([Math]::Min(180, 15 * [Math]::Pow(2, $attempt - 1))))
                    continue
                } elseif ($e -match '\b401\b|Unauthorized' -and $attempt -lt $maxAttempts) {
                    try { Connect-PnPOnline -Url $Cfg.AdminUrl -ClientId $Cfg.ClientId -Tenant $Cfg.TenantId -CertificatePath $Cfg.CertPath -CertificatePassword $sp -WarningAction SilentlyContinue | Out-Null } catch {}
                    continue
                } elseif ($e -match '^Cannot get site ') {
                    # Phantom / hard-deleted entry from the admin enumeration —
                    # not a real, manageable site. Don't treat as drift.
                    $actual = 'not_found'
                    break
                } else {
                    $errMsg = $e
                    break
                }
            }
        }
        $Results.Add([pscustomobject]@{
            scan_id = $item.scan_id; url = $item.url; siteType = $item.siteType
            upn = $item.upn; actual = $actual; title = $title; error_message = $errMsg
        })
    }
    try { Disconnect-PnPOnline -ErrorAction SilentlyContinue | Out-Null } catch {}
}

$workerConfig = @{ TenantId = $tenantId; ClientId = $clientId; CertPath = $certPath; CertPass = $certPass; AdminUrl = $adminUrl }

# Classify one resolved result; record drift ONLY when the real value is a
# genuine non-Disabled state. Unresolved values ('Unknown'/''/error/not_found)
# are counted as errors and NOT recorded as drift — that is the whole point of
# this rewrite (the old code recorded the bulk 'Unknown' as drift for every site).
function _HandleResult($r) {
    if ($r.PSObject.Properties.Match('worker_init_failed').Count -gt 0 -and $r.worker_init_failed) {
        $script:errors++
        Write-Stage "worker init failed: $($r.error_message)" 'WARN'
        return
    }
    $script:checked++
    $val = "$($r.actual)"
    if ($val -eq 'Disabled') {
        $script:okCount++
    } elseif ($r.error_message -or $val -eq '' -or $val -eq 'Unknown' -or $val -eq 'not_found') {
        $script:errors++
    } else {
        $script:drifts++
        $script:batch.Add([pscustomobject]@{
            scan_id           = $ScanId
            site_url          = $r.url
            site_display_name = $r.title
            site_type         = $r.siteType
            user_upn          = $r.upn
            expected_value    = 'Disabled'
            actual_value      = $val
            drift_type        = 'drift'
            exempt_reason     = ''
            trigger_source    = 'scheduled'
            error_message     = $null
        })
        if ($script:batch.Count -ge $BATCH_SIZE) {
            try { Insert-DriftBatch -Rows $script:batch } catch { Write-Stage "DB INSERT failed (will retry once): $($_.Exception.Message)" 'WARN'; Start-Sleep -Seconds 5; Insert-DriftBatch -Rows $script:batch }
            $script:batch.Clear()
        }
    }
}

function _DrainResults {
    $n = 0
    $item = $null
    while ($resultQueue.TryTake([ref]$item)) { _HandleResult $item; $n++ }
    return $n
}

trap {
    Write-Stage "TRAPPED: $($_.Exception.Message)" 'ERROR'
    try { $workQueue.CompleteAdding() } catch {}
    try { if ($batch.Count -gt 0) { Insert-DriftBatch -Rows $batch | Out-Null } } catch {}
    try {
        $em = _Sanitize $_.Exception.Message
        Update-ScanCounters -Total $total -Checked $checked -Drifts $drifts -Ok $okCount -Errors $errors -Status 'failed' -ErrorMessage $em
    } catch {}
    foreach ($j in $jobs) { try { Stop-Job -Job $j -ErrorAction SilentlyContinue | Out-Null; Remove-Job -Job $j -Force -ErrorAction SilentlyContinue } catch {} }
    try { Disconnect-PnPOnline -ErrorAction SilentlyContinue | Out-Null } catch {}
    Stop-Transcript | Out-Null
    exit 1
}

# --- Start resolver workers -------------------------------------------------
Write-Stage "Starting $ThrottleLimit read-only resolver workers..."
for ($i = 0; $i -lt $ThrottleLimit; $i++) {
    $jobs += Start-ThreadJob -Name "cws-verify-w$i" -ScriptBlock $workerScript -ArgumentList $workQueue, $resultQueue, $workerConfig
}

# --- Enumerate sites via Get-PnPTenantSite (inventory only) -----------------
# Admin API view; more complete than /sites/delta for new SharePoint sites.
# The bulk value of DisableCompanyWideSharingLinks is unusable ('Unknown'), so
# each non-exempt site is queued for per-site resolution by the worker pool.
Write-Stage "Enumerating sites via Get-PnPTenantSite -Detailed -IncludeOneDriveSites..."
$siteEnum = Get-PnPTenantSite -Detailed -IncludeOneDriveSites

foreach ($s in $siteEnum) {
    if ($MaxSites -gt 0 -and $total -ge $MaxSites) { break }
    $total++

    $webUrl = ''
    if ($s.Url) { $webUrl = $s.Url.Trim().ToLowerInvariant().TrimEnd('/') }
    if (-not $webUrl) { continue }

    # Collapse any subsite/folder/library path to the site-collection root that
    # Set-PnPTenantSite accepts. Get-PnPTenantSite normally only returns roots,
    # so this is defensive and keeps the key identical to the audit-event path.
    $webUrl = (Get-CwsSiteCollectionRoot $webUrl).ToLowerInvariant()

    if (Test-CwsUnsupportedSiteType $webUrl) {
        $skippedUnsupported++
        continue
    }

    $isOneDrive = $webUrl.Contains('/personal/')
    $siteType = if ($isOneDrive) { 'onedrive' } else { 'site' }
    $userUpn = ''
    # The CWS site allow list applies to any site collection, OneDrives
    # included — same semantics as the event-driven path (cws_drift_detector).
    if ($allowlist.Contains($webUrl)) {
        $skippedAllowlist++
        continue
    }
    if ($isOneDrive) {
        $userUpn = _UpnFromOneDrive $webUrl   # display/db only
        $segment = Get-CwsPersonalSegment $webUrl
        if ($segment -and $exceptionSegments.Contains($segment)) {
            $skippedExceptionGroup++
            continue
        }
    }

    $candidates++
    $workQueue.Add([pscustomobject]@{ scan_id = $ScanId; url = $webUrl; siteType = $siteType; upn = $userUpn })

    # Interleave draining so the result queue doesn't grow unbounded while the
    # producer is still enumerating.
    if (($total % 100) -eq 0) { $null = _DrainResults }

    if (($total % $PROGRESS_EVERY) -eq 0) {
        $elapsed = (Get-Date) - $startTime
        $rate = if ($elapsed.TotalSeconds -gt 0) { [Math]::Round($checked / $elapsed.TotalSeconds, 1) } else { 0 }
        Write-Stage ("progress total={0} queued={1} checked={2} drifts={3} ok={4} skip_allow={5} skip_excp={6} skip_unsup={7} err={8} qdepth={9} rate={10}/s elapsed={11}" -f `
            $total, $candidates, $checked, $drifts, $okCount, $skippedAllowlist, $skippedExceptionGroup, $skippedUnsupported, $errors, $workQueue.Count, $rate, $elapsed.ToString('hh\:mm\:ss'))
        try { Update-ScanCounters -Total $total -Checked $checked -Drifts $drifts -Ok $okCount -Errors $errors } catch {}
    }
}

Write-Stage "Enumeration complete: total=$total queued=$candidates skipped=$($skippedAllowlist + $skippedExceptionGroup + $skippedUnsupported). Waiting for resolver workers (qdepth=$($workQueue.Count))..."
$workQueue.CompleteAdding()

# --- Drain until all workers finish -----------------------------------------
while (@($jobs | Where-Object { $_.State -eq 'Running' }).Count -gt 0) {
    $drained = _DrainResults
    try { Update-ScanCounters -Total $total -Checked $checked -Drifts $drifts -Ok $okCount -Errors $errors } catch {}
    if ($drained -eq 0) { Start-Sleep -Seconds 2 } else { Start-Sleep -Milliseconds 200 }
}
$resultQueue.CompleteAdding()
do { $drained = _DrainResults } while ($drained -gt 0)

foreach ($j in $jobs) {
    try { Receive-Job -Job $j -ErrorAction SilentlyContinue | Out-Null } catch {}
    try { Remove-Job -Job $j -Force -ErrorAction SilentlyContinue } catch {}
}

# Flush remaining drift batch
if ($batch.Count -gt 0) {
    try { Insert-DriftBatch -Rows $batch } catch { Write-Stage "Final DB INSERT failed (will retry once): $($_.Exception.Message)" 'WARN'; Start-Sleep -Seconds 5; Insert-DriftBatch -Rows $batch }
    $batch.Clear()
}

Update-ScanCounters -Total $total -Checked $checked -Drifts $drifts -Ok $okCount -Errors $errors -Status 'completed'

$elapsed = (Get-Date) - $startTime
Write-Stage ("DONE scan_id={0} total={1} queued={2} checked={3} drifts={4} ok={5} skip_allow={6} skip_excp={7} skip_unsup={8} err={9} elapsed={10}" -f `
    $ScanId, $total, $candidates, $checked, $drifts, $okCount, $skippedAllowlist, $skippedExceptionGroup, $skippedUnsupported, $errors, $elapsed.ToString('hh\:mm\:ss'))

try { Disconnect-PnPOnline -ErrorAction SilentlyContinue | Out-Null } catch {}
Stop-Transcript | Out-Null
