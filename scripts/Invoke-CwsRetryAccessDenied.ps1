#requires -Modules @{ ModuleName="PnP.PowerShell"; ModuleVersion="3.0.0" }

<#
.SYNOPSIS
    Retry CWS remediation on sites that failed the bulk run with "Access is denied".

.DESCRIPTION
    Investigation of the 96 access-denied failures from scan 33 showed they are
    Teams private/shared channel SharePoint sites (Template TEAMCHANNEL#0/#1)
    whose parent Team is archived, putting the site into LockState=ReadOnly.
    Even tenant admins cannot write to a read-only site, so Set-PnPTenantSite
    returns the misleading "Access is denied" error.

    For each access-denied URL from a prior scan, this script:
      1. Reads current LockState and DisableCompanyWideSharingLinks.
      2. If CWS is already Disabled, records 'already_compliant' and moves on.
      3. If CWS is NotDisabled and LockState is ReadOnly (or NoAccess):
           a. Unlock
           b. Set DisableCompanyWideSharingLinks=Disabled
           c. Restore LockState to its original value
      4. If CWS is NotDisabled and LockState is Unlock, just sets CWS.
      5. Verifies by reading the site again, records the outcome.

    DRY-RUN by default - flip with -DryRun:$false.

.PARAMETER SourceScanId
    Pull access-denied URLs from this prior scan_id. Default 33.

.PARAMETER DryRun
    When $true (default) no writes happen. Records 'dry_run_would_disable'
    rows so the operator can review the plan before going live.

.PARAMETER EnvFile
    Path to the project .env file. Default: ../.env relative to the script.

.PARAMETER LogDir
    Directory for tee'd log files. Default: scripts/logs.

.EXAMPLE
    pwsh -NoProfile -Command "& ./scripts/Invoke-CwsRetryAccessDenied.ps1 -DryRun \$true"

.EXAMPLE
    pwsh -NoProfile -Command "& ./scripts/Invoke-CwsRetryAccessDenied.ps1 -DryRun \$false"
#>

[CmdletBinding()]
param(
    [int]$SourceScanId = 33,
    [bool]$DryRun = $true,
    [string]$EnvFile = $null,
    [string]$LogDir = $null
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# --- Paths -------------------------------------------------------------------
if (-not $EnvFile) {
    $EnvFile = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    $EnvFile = Join-Path $EnvFile ".env"
}
. (Join-Path $PSScriptRoot 'lib/Invoke-Psql.ps1')
if (-not $LogDir) { $LogDir = Resolve-CwsLogDir -ScriptRoot $PSScriptRoot }

$runStamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmssZ")
$logFile = Join-Path $LogDir "cws_retry_$runStamp.log"
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
    $v = [Environment]::GetEnvironmentVariable($name)
    if ([string]::IsNullOrWhiteSpace($v)) { throw "Missing env var: $name" }
    return $v
}

$tenantId = _Require 'AZURE_TENANT_ID'
$clientId = _Require 'AZURE_CLIENT_ID'
$certRel  = _Require 'AZURE_CERTIFICATE'
$certPass = _Require 'AZURE_CERTIFICATE_PASS'
$adminUrl = _Require 'SHAREPOINT_ADMIN_URL'

$repoRoot = Split-Path -Parent $EnvFile
$certPath = if ([IO.Path]::IsPathRooted($certRel)) { $certRel } else { Join-Path $repoRoot ($certRel -replace '^\./', '') }
if (-not (Test-Path $certPath)) { throw "Certificate not found: $certPath" }

Write-Stage "Source scan id : $SourceScanId"
Write-Stage "Dry run        : $DryRun"
Write-Stage "Admin URL      : $adminUrl"
Write-Stage "Log file       : $logFile"

# --- Postgres helpers (dual-mode: host docker exec or in-container psql) ----
. (Join-Path $PSScriptRoot 'lib/Invoke-Psql.ps1')

# --- Connect ----------------------------------------------------------------
$securePass = ConvertTo-SecureString -String $certPass -AsPlainText -Force
Write-Stage "Connecting to $adminUrl..."
Connect-PnPOnline -Url $adminUrl -ClientId $clientId -Tenant $tenantId `
    -CertificatePath $certPath -CertificatePassword $securePass `
    -WarningAction SilentlyContinue | Out-Null
Write-Stage "Connected."

# --- Pull access-denied URLs from source scan -------------------------------
Write-Stage "Loading access-denied URLs from scan $SourceScanId..."
$urlsOut = Invoke-Psql -TupleOnly -Sql "SELECT site_url FROM company_wide_sharing_events WHERE scan_id=$SourceScanId AND site_type='site' AND action='failed' AND error_message LIKE 'Access is denied.%' ORDER BY site_url;"
$urls = @()
foreach ($line in ($urlsOut -split "`r?`n")) {
    $t = $line.Trim()
    if ($t) { $urls += $t }
}
Write-Stage "Loaded $($urls.Count) URLs to retry."
if ($urls.Count -eq 0) { Stop-Transcript | Out-Null; return }

# --- Create new scan row -----------------------------------------------------
$dryRunSql = if ($DryRun) { 'true' } else { 'false' }
$insertOut = Invoke-Psql -TupleOnly -Sql "INSERT INTO company_wide_sharing_scans (trigger_type, triggered_by, status, dry_run, started_at) VALUES ('ps_retry_denied', 'pwsh-host', 'in_progress', $dryRunSql, NOW()) RETURNING id;"
$scanId = [int](($insertOut -split "`r?`n" | Where-Object { $_ -match '^\d+$' } | Select-Object -First 1))
if (-not $scanId) { throw "Failed to allocate scan id" }
Write-Stage "Created scan_id=$scanId"

# --- Helpers ----------------------------------------------------------------
function _InsertEvent {
    param([hashtable]$Row)
    $errSql = if ($Row.error_message) { "'$(_EscSql $Row.error_message)'" } else { 'NULL' }
    $sql = "INSERT INTO company_wide_sharing_events (scan_id, site_url, site_display_name, site_type, user_upn, previous_value, new_value, action, exempt_reason, error_message) VALUES ($($Row.scan_id),'$(_EscSql $Row.site_url)','$(_EscSql $Row.site_display_name)','site','','$(_EscSql $Row.previous_value)','$(_EscSql $Row.new_value)','$(_EscSql $Row.action)','$(_EscSql $Row.exempt_reason)',$errSql);"
    Invoke-Psql -Sql $sql | Out-Null
}

function _GetSiteState([string]$url) {
    try {
        $s = Get-PnPTenantSite -Identity $url -Detailed -ErrorAction Stop
        return [pscustomobject]@{
            ok           = $true
            title        = "$($s.Title)"
            template     = "$($s.Template)"
            lock_state   = "$($s.LockState)"
            cws          = "$($s.DisableCompanyWideSharingLinks)"
            channel_type = "$($s.TeamsChannelType)"
            error        = $null
        }
    } catch {
        return [pscustomobject]@{
            ok=$false; title=''; template=''; lock_state=''; cws=''; channel_type=''
            error=$_.Exception.Message
        }
    }
}

# --- Counters ---------------------------------------------------------------
$remediated = 0
$wouldRemediate = 0
$alreadyCompliant = 0
$stillFailed = 0
$startTime = Get-Date

# --- Trap -------------------------------------------------------------------
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

# --- Process each URL --------------------------------------------------------
$i = 0
foreach ($url in $urls) {
    $i++
    $st = _GetSiteState $url

    if (-not $st.ok) {
        Write-Stage ("[{0,3}/{1}] {2} -> Get failed: {3}" -f $i, $urls.Count, $url, $st.error) 'WARN'
        _InsertEvent @{
            scan_id=$scanId; site_url=$url; site_display_name=''
            previous_value=''; new_value=''
            action='failed'; exempt_reason='get_site_failed'
            error_message=(_Sanitize $st.error)
        }
        $stillFailed++
        continue
    }

    $prev = $st.cws
    if ($prev -eq 'Disabled') {
        Write-Stage ("[{0,3}/{1}] {2} -> already Disabled" -f $i, $urls.Count, $url)
        _InsertEvent @{
            scan_id=$scanId; site_url=$url; site_display_name=$st.title
            previous_value=$prev; new_value=$prev
            action='already_compliant'; exempt_reason='cws_already_disabled'
            error_message=$null
        }
        $alreadyCompliant++
        continue
    }

    if ($DryRun) {
        $tag = if ($st.lock_state -eq 'ReadOnly' -and $st.template -like 'TEAMCHANNEL*') { 'archived_teams_channel' } elseif ($st.lock_state -eq 'ReadOnly') { 'site_readonly' } else { "lock=$($st.lock_state)" }
        Write-Stage ("[{0,3}/{1}] {2} -> DRY-RUN would set Disabled (lock={3} template={4} channel={5})" -f $i, $urls.Count, $url, $st.lock_state, $st.template, $st.channel_type)
        _InsertEvent @{
            scan_id=$scanId; site_url=$url; site_display_name=$st.title
            previous_value=$prev; new_value='Disabled'
            action='dry_run_would_disable'
            exempt_reason=$tag
            error_message=$null
        }
        $wouldRemediate++
        continue
    }

    # LIVE PATH: unlock if necessary, set CWS, relock
    $origLock = $st.lock_state
    $needUnlock = ($origLock -eq 'ReadOnly' -or $origLock -eq 'NoAccess')
    $errMsg = $null
    $setSucceeded = $false
    try {
        if ($needUnlock) {
            Set-PnPTenantSite -Identity $url -LockState Unlock -ErrorAction Stop -WarningAction SilentlyContinue | Out-Null
        }
        Set-PnPTenantSite -Identity $url -DisableCompanyWideSharingLinks Disabled -ErrorAction Stop -WarningAction SilentlyContinue | Out-Null
        $setSucceeded = $true
    } catch {
        $errMsg = $_.Exception.Message
    } finally {
        if ($needUnlock) {
            try {
                Set-PnPTenantSite -Identity $url -LockState $origLock -ErrorAction Stop -WarningAction SilentlyContinue | Out-Null
            } catch {
                $relockErr = $_.Exception.Message
                $errMsg = if ($errMsg) { "$errMsg | RELOCK-FAILED: $relockErr" } else { "RELOCK-FAILED: $relockErr" }
                $setSucceeded = $false
            }
        }
    }

    # Verify post-state
    $after = _GetSiteState $url
    $newCws = if ($after.ok) { $after.cws } else { 'unknown' }
    $newLock = if ($after.ok) { $after.lock_state } else { 'unknown' }

    $remTag = if ($needUnlock) { "unlock_set_relock;lock=$origLock" } else { 'simple_set' }
    if ($remTag.Length -gt 50) { $remTag = $remTag.Substring(0, 50) }

    if ($setSucceeded -and $newCws -eq 'Disabled') {
        Write-Stage ("[{0,3}/{1}] {2} -> remediated (lock {3} -> {4})" -f $i, $urls.Count, $url, $origLock, $newLock)
        _InsertEvent @{
            scan_id=$scanId; site_url=$url; site_display_name=$st.title
            previous_value=$prev; new_value=$newCws
            action='remediated'
            exempt_reason=$remTag
            error_message=$null
        }
        $remediated++
    } else {
        Write-Stage ("[{0,3}/{1}] {2} -> FAILED: cws_after={3} lock_after={4} err={5}" -f $i, $urls.Count, $url, $newCws, $newLock, $errMsg) 'ERROR'
        $failDetail = "lock_before=$origLock lock_after=$newLock template=$($st.template) channel=$($st.channel_type) | $errMsg"
        _InsertEvent @{
            scan_id=$scanId; site_url=$url; site_display_name=$st.title
            previous_value=$prev; new_value=$newCws
            action='failed'
            exempt_reason=$remTag
            error_message=(_Sanitize $failDetail)
        }
        $stillFailed++
    }

    if (($i % 10) -eq 0) {
        Invoke-Psql -Sql "UPDATE company_wide_sharing_scans SET total_sites_scanned=$i, remediated=$remediated, would_remediate=$wouldRemediate, already_compliant=$alreadyCompliant, errors=$stillFailed, violations_found=($remediated + $wouldRemediate + $stillFailed) WHERE id=$scanId;" | Out-Null
    }
}

# --- Finalize ---------------------------------------------------------------
Invoke-Psql -Sql "UPDATE company_wide_sharing_scans SET status='completed', completed_at=NOW(), total_sites_scanned=$($urls.Count), remediated=$remediated, would_remediate=$wouldRemediate, already_compliant=$alreadyCompliant, errors=$stillFailed, violations_found=($remediated + $wouldRemediate + $stillFailed) WHERE id=$scanId;" | Out-Null

$elapsed = (Get-Date) - $startTime
Write-Stage "DONE scan_id=$scanId total=$($urls.Count) rem=$remediated would=$wouldRemediate already=$alreadyCompliant err=$stillFailed elapsed=$($elapsed.ToString('hh\:mm\:ss'))"

try { Disconnect-PnPOnline -ErrorAction SilentlyContinue | Out-Null } catch {}
Stop-Transcript | Out-Null
