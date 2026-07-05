#requires -Modules @{ ModuleName="PnP.PowerShell"; ModuleVersion="3.0.0" }
#requires -Modules @{ ModuleName="ThreadJob"; ModuleVersion="2.0.0" }

<#
.SYNOPSIS
    Tenant-wide bulk remediation of the "Company-Wide Sharing Links" policy
    (parallelized via Start-ThreadJob worker pool).

.DESCRIPTION
    Enumerates every provisioned site in the tenant via Microsoft Graph
    /sites/delta (no result cap; PnP 3.x's Invoke-PnPGraphMethod uses the
    same connection as Set-PnPTenantSite). For each site:
      - OneDrive personal site whose owning UPN is in the configured
        exception group  -> SKIP (leave DisableCompanyWideSharingLinks alone)
      - SharePoint site URL on site_company_wide_sharing_allowlist -> SKIP
      - Everything else -> Set-PnPTenantSite -DisableCompanyWideSharingLinks Disabled

    Architecture:
      - Main thread:   /sites/delta enumeration, classification, result
                       draining, postgres batch INSERTs, progress logging.
      - Worker pool:   N Start-ThreadJob workers each with their own
                       Connect-PnPOnline; they pull from a BlockingCollection
                       and push results onto a second BlockingCollection.
      - Worker errors stay inside the worker runspace and never reach the
        main Start-Transcript, so the live tail stays readable.

    DRY-RUN by default - flip with -DryRun:$false.

.PARAMETER DryRun
    When $true (default) no Set-PnPTenantSite calls are made. Every decision
    is recorded as action='dry_run_would_disable' in company_wide_sharing_events.

.PARAMETER MaxSites
    Process at most this many sites then stop. 0 = unlimited.

.PARAMETER ResumeScanId
    Re-use an existing company_wide_sharing_scans row and skip any site URL
    that already has an events row under it. Use to continue after Ctrl+C.

.PARAMETER ThrottleLimit
    Number of parallel worker threads. Default 5. Each worker holds one
    Connect-PnPOnline. Don't push much past 10 - SPO admin throttles hard.

.PARAMETER EnvFile
    Path to the project .env file. Default: ../.env relative to the script.

.PARAMETER LogDir
    Directory for tee'd log files. Default: scripts/logs alongside the script.

.EXAMPLE
    pwsh -NoProfile -Command "& ./scripts/Invoke-CwsBulkRemediation.ps1 -DryRun \$true -MaxSites 1000"

.EXAMPLE
    pwsh -NoProfile -Command "& ./scripts/Invoke-CwsBulkRemediation.ps1 -DryRun \$false -ThrottleLimit 5"

.EXAMPLE
    pwsh -NoProfile -Command "& ./scripts/Invoke-CwsBulkRemediation.ps1 -DryRun \$false -ResumeScanId 33"
#>

[CmdletBinding()]
param(
    [string]$DryRun = 'true',
    [int]$MaxSites = 0,
    [int]$ResumeScanId = 0,
    [int]$ThrottleLimit = 5,
    [string]$EnvFile = $null,
    [string]$LogDir = $null
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# pwsh -File passes args as strings; a [bool] param can't bind "0"/"1", so the
# param is [string] and coerced here. Mirrors Invoke-CwsExceptionMembershipDelta.ps1.
$DryRunBool = ($DryRun -match '^(true|1|\$true)$')

# --- Paths -------------------------------------------------------------------
if (-not $EnvFile) {
    $EnvFile = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    $EnvFile = Join-Path $EnvFile ".env"
}
. (Join-Path $PSScriptRoot 'lib/Invoke-Psql.ps1')
. (Join-Path $PSScriptRoot 'lib/CwsSiteUrl.ps1')
if (-not $LogDir) { $LogDir = Resolve-CwsLogDir -ScriptRoot $PSScriptRoot }

$runStamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmssZ")
$logFile = Join-Path $LogDir "cws_bulk_$runStamp.log"
Start-Transcript -Path $logFile -Force | Out-Null

function Write-Stage {
    param([string]$Msg, [string]$Level = 'INFO')
    $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ssZ")
    Write-Host "[$ts] [$Level] $Msg"
}

# --- .env loader -------------------------------------------------------------
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
Write-Stage "Dry run           : $DryRunBool (raw=$DryRun)"
Write-Stage "Max sites         : $(if ($MaxSites -eq 0) { 'unlimited' } else { $MaxSites })"
Write-Stage "Resume scan id    : $(if ($ResumeScanId -eq 0) { 'no (new scan)' } else { $ResumeScanId })"
Write-Stage "Throttle limit    : $ThrottleLimit worker threads"
Write-Stage "Log file          : $logFile"

# --- Postgres helpers (dual-mode: host docker exec or in-container psql) ----
. (Join-Path $PSScriptRoot 'lib/Invoke-Psql.ps1')

function Insert-EventBatch {
    param([Parameter(Mandatory)][System.Collections.Generic.List[psobject]]$Rows)
    if ($Rows.Count -eq 0) { return }
    $sb = [System.Text.StringBuilder]::new()
    [void]$sb.Append("INSERT INTO company_wide_sharing_events (scan_id, site_url, site_display_name, site_type, user_upn, previous_value, new_value, action, exempt_reason, error_message) VALUES ")
    $first = $true
    foreach ($r in $Rows) {
        if (-not $first) { [void]$sb.Append(',') }
        $first = $false
        $errSql = if ($r.error_message) { "'$(_EscSql $r.error_message)'" } else { 'NULL' }
        [void]$sb.AppendFormat(
            "({0},'{1}','{2}','{3}','{4}','{5}','{6}','{7}','{8}',{9})",
            $r.scan_id,
            (_EscSql $r.site_url),
            (_EscSql $r.site_display_name),
            (_EscSql $r.site_type),
            (_EscSql $r.user_upn),
            (_EscSql $r.previous_value),
            (_EscSql $r.new_value),
            (_EscSql $r.action),
            (_EscSql $r.exempt_reason),
            $errSql
        )
    }
    [void]$sb.Append(';')
    Invoke-Psql -Sql $sb.ToString() | Out-Null
}

function Update-ScanCounters {
    param(
        [int]$ScanId,
        [int]$Total, [int]$Violations, [int]$Remediated, [int]$WouldRemediate,
        [int]$SkippedAllowlist, [int]$SkippedExceptionGroup, [int]$AlreadyCompliant,
        [int]$Errors, [int]$ExceptionGroupMemberCount,
        [string]$Status = 'in_progress',
        [string]$ErrorMessage = $null
    )
    $completedClause = if ($Status -eq 'completed' -or $Status -eq 'failed') { ", completed_at = NOW()" } else { '' }
    $errClause = if ($ErrorMessage) { ", error_message = '$(_EscSql $ErrorMessage)'" } else { '' }
    $sql = "UPDATE company_wide_sharing_scans SET status='$Status', total_sites_scanned=$Total, violations_found=$Violations, remediated=$Remediated, would_remediate=$WouldRemediate, skipped_allowlist=$SkippedAllowlist, skipped_exception_group=$SkippedExceptionGroup, already_compliant=$AlreadyCompliant, errors=$Errors, exception_group_member_count=$ExceptionGroupMemberCount$completedClause$errClause WHERE id=$ScanId;"
    Invoke-Psql -Sql $sql | Out-Null
}

# --- Main-thread PnP connect (for Graph delta + group enumeration) ----------
$securePass = ConvertTo-SecureString -String $certPass -AsPlainText -Force
Write-Stage "Connecting to $adminUrl (main thread)..."
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
    Write-Stage "Exception group is empty — no OneDrive users will be exempt from CWS enforcement" 'WARN'
}

# Exemption matching uses forward-encoded personal-site segments — reversing a
# URL to a UPN is ambiguous and drops exemptions for dotted UPNs.
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

# --- Create or resume scan row ----------------------------------------------
if ($ResumeScanId -gt 0) {
    $scanId = $ResumeScanId
    $statusOut = (Invoke-Psql -TupleOnly -Sql "SELECT status FROM company_wide_sharing_scans WHERE id=$scanId;").ToString().Trim()
    if (-not $statusOut) { throw "scan_id=$scanId not found" }
    Write-Stage "Resuming scan_id=$scanId (was status='$statusOut')"
    Invoke-Psql -Sql "UPDATE company_wide_sharing_scans SET status='in_progress', error_message=NULL WHERE id=$scanId;" | Out-Null
} else {
    $dryRunSql = if ($DryRunBool) { 'true' } else { 'false' }
    $insertOut = Invoke-Psql -TupleOnly -Sql "INSERT INTO company_wide_sharing_scans (trigger_type, triggered_by, status, dry_run, started_at) VALUES ('ps_bulk_tenant', 'pwsh-host', 'in_progress', $dryRunSql, NOW()) RETURNING id;"
    $scanId = [int](($insertOut -split "`r?`n" | Where-Object { $_ -match '^\d+$' } | Select-Object -First 1))
    if (-not $scanId) { throw "Failed to allocate scan id from postgres" }
    Write-Stage "Created scan_id=$scanId"
}

# --- Resume skip-set --------------------------------------------------------
$processed = [System.Collections.Generic.HashSet[string]]::new()
if ($ResumeScanId -gt 0) {
    $procOut = Invoke-Psql -TupleOnly -Sql "SELECT LOWER(site_url) FROM company_wide_sharing_events WHERE scan_id=$scanId;"
    foreach ($line in ($procOut -split "`r?`n")) {
        $t = $line.Trim()
        if ($t) { [void]$processed.Add($t) }
    }
    Write-Stage "Resume skip-set: $($processed.Count) URLs already in events"
}

# --- Helpers ----------------------------------------------------------------
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

# --- Concurrent queues + worker pool ----------------------------------------
$workQueue = [System.Collections.Concurrent.BlockingCollection[psobject]]::new()
$resultQueue = [System.Collections.Concurrent.BlockingCollection[psobject]]::new()

$workerScript = {
    param($Work, $Results, $Cfg)
    # Workers run in isolated runspaces; no Start-Transcript here, so caught
    # cmdlet errors never reach the main thread's log file.
    $ErrorActionPreference = 'Stop'
    Import-Module PnP.PowerShell -DisableNameChecking -WarningAction SilentlyContinue | Out-Null
    $sp = ConvertTo-SecureString -String $Cfg.CertPass -AsPlainText -Force
    try {
        Connect-PnPOnline -Url $Cfg.AdminUrl `
            -ClientId $Cfg.ClientId `
            -Tenant $Cfg.TenantId `
            -CertificatePath $Cfg.CertPath `
            -CertificatePassword $sp `
            -WarningAction SilentlyContinue | Out-Null
    } catch {
        $Results.Add([pscustomobject]@{
            worker_init_failed = $true
            error_message = "worker Connect-PnPOnline failed: $($_.Exception.Message)"
        })
        return
    }
    foreach ($item in $Work.GetConsumingEnumerable()) {
        $act = 'remediated'
        $errMsg = $null
        $exemptR = ''
        $attempt = 0
        $maxAttempts = 3
        while ($attempt -lt $maxAttempts) {
            $attempt++
            try {
                $null = Set-PnPTenantSite -Identity $item.url `
                    -DisableCompanyWideSharingLinks Disabled `
                    -ErrorAction Stop `
                    -WarningAction SilentlyContinue 2>$null
                break
            } catch {
                $e = $_.Exception.Message
                if ($e -match '429|throttle|IdleSession') {
                    $backoff = [int]([Math]::Min(180, 15 * [Math]::Pow(2, $attempt - 1)))
                    Start-Sleep -Seconds $backoff
                    continue
                } elseif ($e -match '\b401\b|Unauthorized' -and $attempt -lt $maxAttempts) {
                    try {
                        Connect-PnPOnline -Url $Cfg.AdminUrl -ClientId $Cfg.ClientId -Tenant $Cfg.TenantId -CertificatePath $Cfg.CertPath -CertificatePassword $sp -WarningAction SilentlyContinue | Out-Null
                    } catch {}
                    continue
                } else {
                    # Default to failed; classify recoverable patterns more precisely.
                    $act = 'failed'
                    $errMsg = $e
                    if ($e -match '^Cannot get site ') {
                        if ($item.siteType -eq 'onedrive' -and $item.upn) {
                            # OneDrive failures usually mean the user is hard-deleted
                            # from Entra (purged past the 30-day soft-delete window),
                            # or the user still exists but their OneDrive personal
                            # site was de-provisioned. Disambiguate with a Graph
                            # lookup so the row tells the operator which case.
                            $act = 'skipped_not_found'
                            $exemptR = 'site_not_found_in_spo_or_graph'
                            $errMsg = $null
                            try {
                                $null = Invoke-PnPGraphMethod -Url "v1.0/users/$($item.upn)?`$select=id" -Method Get -ErrorAction Stop
                                $act = 'skipped_no_onedrive'
                                $exemptR = 'onedrive_site_missing'
                            } catch {
                                if ($_.Exception.Message -match '\b404\b|does not exist|Resource .* does not exist') {
                                    $act = 'skipped_user_deleted'
                                    $exemptR = 'user_not_in_entra'
                                }
                                # Any other Graph error: leave as skipped_not_found
                            }
                        } else {
                            # SharePoint site: phantom entry from /sites/delta — site is hard-deleted.
                            $act = 'skipped_not_found'
                            $exemptR = 'site_not_found_in_spo_or_graph'
                            $errMsg = $null
                        }
                    } elseif ($e -match 'Access is denied') {
                        # Distinguish "archived Teams private/shared channel ReadOnly
                        # site" (no risk — site is write-locked) from real access denied.
                        try {
                            $info = Get-PnPTenantSite -Identity $item.url -Detailed -ErrorAction Stop
                            if ($info.Template -like 'TEAMCHANNEL*' -and $info.LockState -eq 'ReadOnly') {
                                $act = 'skipped'
                                $exemptR = 'archived_teams_channel'
                                $errMsg = $null
                            }
                        } catch {}
                    }
                    break
                }
            }
        }
        $Results.Add([pscustomobject]@{
            scan_id           = $item.scan_id
            site_url          = $item.url
            site_display_name = ''
            site_type         = $item.siteType
            user_upn          = $item.upn
            previous_value    = 'unknown'
            new_value         = 'Disabled'
            action            = $act
            exempt_reason     = $exemptR
            error_message     = $errMsg
        })
    }
    try { Disconnect-PnPOnline -ErrorAction SilentlyContinue | Out-Null } catch {}
}

$workerConfig = @{
    TenantId = $tenantId
    ClientId = $clientId
    CertPath = $certPath
    CertPass = $certPass
    AdminUrl = $adminUrl
}

$jobs = @()
if (-not $DryRunBool) {
    for ($i = 0; $i -lt $ThrottleLimit; $i++) {
        $jobs += Start-ThreadJob -Name "cws-w$i" -ScriptBlock $workerScript -ArgumentList $workQueue, $resultQueue, $workerConfig
    }
    Write-Stage "Spawned $ThrottleLimit worker threads"
}

# --- Session counters (this run only) ---------------------------------------
# Cumulative scan-row counters are derived from the events table on every
# _PersistCounters call so resume sessions don't lose context.
$total = 0
$violations = 0
$remediated = 0
$wouldRemediate = 0
$skippedAllowlist = 0
$skippedExceptionGroup = 0
$skippedArchivedChannel = 0
$skippedNotFound = 0
$skippedUserDeleted = 0
$skippedNoOnedrive = 0
$alreadyCompliant = 0
$errors = 0
$workerInitFailures = 0
$startTime = Get-Date

# In-flight result drain
function _DrainResults {
    param([int]$MaxBatch = 500)
    $batch = [System.Collections.Generic.List[psobject]]::new()
    $r = $null
    while ($batch.Count -lt $MaxBatch -and $script:resultQueue.TryTake([ref]$r, 0)) {
        if ($r.PSObject.Properties.Match('worker_init_failed').Count -gt 0 -and $r.worker_init_failed) {
            $script:workerInitFailures++
            Write-Stage $r.error_message 'ERROR'
            continue
        }
        if ($r.error_message) { $r.error_message = _Sanitize $r.error_message }
        [void]$batch.Add($r)
        switch ($r.action) {
            'remediated'             { $script:remediated++ }
            'failed'                 { $script:errors++ }
            'dry_run_would_disable'  { $script:wouldRemediate++ }
            'skipped'                { $script:skippedArchivedChannel++ }
            'skipped_not_found'      { $script:skippedNotFound++ }
            'skipped_user_deleted'   { $script:skippedUserDeleted++ }
            'skipped_no_onedrive'    { $script:skippedNoOnedrive++ }
        }
    }
    if ($batch.Count -gt 0) {
        try {
            Insert-EventBatch -Rows $batch
        } catch {
            Write-Stage "DB INSERT failed: $($_.Exception.Message). Sleeping 5s and retrying once." 'ERROR'
            Start-Sleep -Seconds 5
            Insert-EventBatch -Rows $batch
        }
    }
    return $batch.Count
}

function _PersistCounters {
    # Derive cumulative counters from the events table so the scan row stays
    # honest across resume sessions. Skip counts only live in memory (skips
    # don't write events at tenant scale) so add the session values on top.
    $sqlCount = "SELECT COUNT(*) || '|' || COALESCE(SUM(CASE WHEN action='remediated' THEN 1 ELSE 0 END),0) || '|' || COALESCE(SUM(CASE WHEN action='failed' THEN 1 ELSE 0 END),0) || '|' || COALESCE(SUM(CASE WHEN action='dry_run_would_disable' THEN 1 ELSE 0 END),0) FROM company_wide_sharing_events WHERE scan_id=$($script:scanId);"
    $countsRaw = (Invoke-Psql -TupleOnly -Sql $sqlCount) -join ''
    $p = $countsRaw.Trim() -split '\|'
    if ($p.Count -eq 4) {
        $cEvents = [int]$p[0]
        $cRem    = [int]$p[1]
        $cErr    = [int]$p[2]
        $cWould  = [int]$p[3]
    } else {
        $cEvents = $script:remediated + $script:errors + $script:wouldRemediate
        $cRem    = $script:remediated
        $cErr    = $script:errors
        $cWould  = $script:wouldRemediate
    }
    $cViolations = $cRem + $cErr + $cWould
    $cTotal = $cEvents + $script:skippedAllowlist + $script:skippedExceptionGroup
    Update-ScanCounters -ScanId $script:scanId `
        -Total $cTotal -Violations $cViolations `
        -Remediated $cRem -WouldRemediate $cWould `
        -SkippedAllowlist $script:skippedAllowlist -SkippedExceptionGroup $script:skippedExceptionGroup `
        -AlreadyCompliant $script:alreadyCompliant -Errors $cErr `
        -ExceptionGroupMemberCount $script:exceptionUpns.Count
}

function _LogProgress {
    $elapsed = (Get-Date) - $script:startTime
    $perSec = if ($elapsed.TotalSeconds -gt 0) { [Math]::Round(($script:remediated + $script:errors + $script:wouldRemediate) / $elapsed.TotalSeconds, 2) } else { 0 }
    $queueDepth = $script:workQueue.Count
    Write-Stage ("progress total={0} viol={1} rem={2} would={3} skip_allow={4} skip_excp={5} skip_chan={6} skip_nf={7} skip_udel={8} skip_nood={9} err={10} queue={11} rate={12}/s elapsed={13}" -f `
        $script:total, $script:violations, $script:remediated, $script:wouldRemediate,
        $script:skippedAllowlist, $script:skippedExceptionGroup,
        $script:skippedArchivedChannel, $script:skippedNotFound,
        $script:skippedUserDeleted, $script:skippedNoOnedrive, $script:errors,
        $queueDepth, $perSec, $elapsed.ToString('hh\:mm\:ss'))
}

# --- Trap for unexpected exceptions -----------------------------------------
trap {
    Write-Stage "TRAPPED: $($_.Exception.Message)" 'ERROR'
    try { $workQueue.CompleteAdding() } catch {}
    try {
        # Drain anything workers already produced
        for ($k = 0; $k -lt 20; $k++) { $null = _DrainResults; Start-Sleep -Milliseconds 200 }
    } catch {}
    try {
        _PersistCounters
        $em = _Sanitize $_.Exception.Message
        Invoke-Psql -Sql "UPDATE company_wide_sharing_scans SET status='failed', completed_at=NOW(), error_message='$(_EscSql $em)' WHERE id=$scanId;" | Out-Null
    } catch {}
    foreach ($j in $jobs) { try { Stop-Job -Job $j -PassThru -ErrorAction SilentlyContinue | Out-Null; Remove-Job -Job $j -Force -ErrorAction SilentlyContinue } catch {} }
    try { Disconnect-PnPOnline -ErrorAction SilentlyContinue | Out-Null } catch {}
    Stop-Transcript | Out-Null
    exit 1
}

# --- Enumerate sites via Graph /sites/delta ---------------------------------
Write-Stage "Enumerating sites via Graph /sites/delta ..."
$nextUrl = 'v1.0/sites/delta'
$stopEnumerating = $false
$lastProgress = 0

while ($nextUrl -and -not $stopEnumerating) {
    try {
        $resp = Invoke-PnPGraphMethod -Url $nextUrl -Method Get
    } catch {
        $em = $_.Exception.Message
        if ($em -match '429|throttle') {
            Write-Stage "Graph /sites/delta throttled; sleeping 30s" 'WARN'
            Start-Sleep -Seconds 30
        } else {
            Write-Stage "Graph delta error: $em; sleeping 15s and retrying" 'WARN'
            Start-Sleep -Seconds 15
        }
        continue
    }

    foreach ($s in $resp.value) {
        if ($s.PSObject.Properties.Match('@removed').Count -gt 0) { continue }
        if ($s.PSObject.Properties.Match('deleted').Count -gt 0) { continue }
        $webUrl = ''
        if ($s.PSObject.Properties.Match('webUrl').Count -gt 0 -and $s.webUrl) {
            $webUrl = $s.webUrl.Trim().ToLowerInvariant().TrimEnd('/')
        }
        if (-not $webUrl) { continue }
        # Collapse subsite/folder paths to the site-collection root, then skip
        # site classes Set-PnPTenantSite cannot manage (Embedded containers,
        # legacy public sites, Content Type Hub). See lib/CwsSiteUrl.ps1.
        $webUrl = (Get-CwsSiteCollectionRoot $webUrl).ToLowerInvariant()
        if (Test-CwsUnsupportedSiteType $webUrl) { continue }
        if ($processed.Contains($webUrl)) { continue }
        if ($MaxSites -gt 0 -and $total -ge $MaxSites) { $stopEnumerating = $true; break }

        $total++

        $isOneDrive = $webUrl.Contains('/personal/')
        $siteType = if ($isOneDrive) { 'onedrive' } else { 'site' }
        $userUpn = ''
        $exemptReason = ''
        # Allow list applies to any site collection, OneDrives included —
        # same semantics as the event-driven path (cws_drift_detector).
        if ($allowlist.Contains($webUrl)) { $exemptReason = 'in_allowlist' }
        if (-not $exemptReason -and $isOneDrive) {
            $userUpn = _UpnFromOneDrive $webUrl   # display/db only
            $segment = Get-CwsPersonalSegment $webUrl
            if ($segment -and $exceptionSegments.Contains($segment)) { $exemptReason = 'in_exception_group' }
        }

        if ($exemptReason) {
            if ($exemptReason -eq 'in_exception_group') { $skippedExceptionGroup++ } else { $skippedAllowlist++ }
            continue
        }

        $violations++

        if ($DryRunBool) {
            # No workers needed — push the dry-run record straight to results
            $resultQueue.Add([pscustomobject]@{
                scan_id = $scanId; site_url = $webUrl; site_display_name = ''
                site_type = $siteType; user_upn = $userUpn
                previous_value = 'unknown'; new_value = 'Disabled'
                action = 'dry_run_would_disable'; exempt_reason = ''
                error_message = $null
            })
        } else {
            $workQueue.Add([pscustomobject]@{
                scan_id = $scanId; url = $webUrl; siteType = $siteType; upn = $userUpn
            })
        }

        # Drain results often; in live mode workers run in parallel with the
        # producer so this keeps the result queue from growing unbounded.
        if (($total % 100) -eq 0) {
            $null = _DrainResults
            _PersistCounters
        }

        if (($total - $lastProgress) -ge 500) {
            _LogProgress
            $lastProgress = $total
        }

        if ($MaxSites -gt 0 -and $total -ge $MaxSites) { $stopEnumerating = $true; break }
    }

    if ($stopEnumerating) { break }
    $nextUrl = if ($resp.PSObject.Properties.Match('@odata.nextLink').Count -gt 0 -and $resp.'@odata.nextLink') { $resp.'@odata.nextLink' } else { $null }
}

Write-Stage "Enumeration complete. total=$total violations=$violations skipped=$($skippedAllowlist + $skippedExceptionGroup). Waiting for workers (queue depth: $($workQueue.Count))..."

# Signal no more work
$workQueue.CompleteAdding()

# --- Drain phase: keep flushing results until workers are done --------------
while (@($jobs | Where-Object { $_.State -eq 'Running' }).Count -gt 0) {
    $drained = _DrainResults
    _PersistCounters
    _LogProgress
    if ($drained -eq 0) { Start-Sleep -Seconds 5 } else { Start-Sleep -Seconds 1 }
}

# Final drain (workers exited; result queue may still have a few)
$resultQueue.CompleteAdding()
do {
    $drained = _DrainResults
} while ($drained -gt 0)

# Receive worker output for diagnostics, then clean up
foreach ($j in $jobs) {
    try { Receive-Job -Job $j -ErrorAction SilentlyContinue | Out-Null } catch {}
    try { Remove-Job -Job $j -Force -ErrorAction SilentlyContinue } catch {}
}

# Final scan completion — write cumulative counts from events table, then flip status.
_PersistCounters
Invoke-Psql -Sql "UPDATE company_wide_sharing_scans SET status='completed', completed_at=NOW() WHERE id=$scanId;" | Out-Null

# Read back the final row for the DONE line so the operator sees cumulative totals
$finalOut = Invoke-Psql -TupleOnly -Sql "SELECT total_sites_scanned || '|' || violations_found || '|' || remediated || '|' || would_remediate || '|' || errors FROM company_wide_sharing_scans WHERE id=$scanId;"
$f = ($finalOut -join '').Trim() -split '\|'
$elapsed = (Get-Date) - $startTime
Write-Stage "DONE scan_id=$scanId cumulative: total=$($f[0]) viol=$($f[1]) rem=$($f[2]) would=$($f[3]) err=$($f[4]) | this session: added=$total worker_init_failures=$workerInitFailures elapsed=$($elapsed.ToString('hh\:mm\:ss'))"

try { Disconnect-PnPOnline -ErrorAction SilentlyContinue | Out-Null } catch {}
Stop-Transcript | Out-Null
