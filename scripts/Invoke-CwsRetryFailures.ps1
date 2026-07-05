#requires -Modules @{ ModuleName="PnP.PowerShell"; ModuleVersion="3.0.0" }

<#
.SYNOPSIS
    Retry CWS remediation on sites that failed in a prior scan.

.DESCRIPTION
    Pulls rows from company_wide_sharing_events where the prior scan recorded
    action='failed' (optionally filtered by site_type), then attempts
    Set-PnPTenantSite -DisableCompanyWideSharingLinks Disabled on each. Use
    after fixing the underlying cause (e.g. raising OneDrive storage quotas,
    clearing tenant throttles, etc.). DRY-RUN by default.

    Each retry attempt:
      1. Reads the current CWS setting.
      2. If already Disabled, records 'already_compliant'.
      3. Otherwise sets DisableCompanyWideSharingLinks=Disabled.
      4. Verifies post-state and records the outcome.

.PARAMETER SourceScanId
    Pull failed URLs from this prior scan_id. Default 33.

.PARAMETER SiteType
    Restrict to 'onedrive' or 'site'. Empty/omitted retries both.

.PARAMETER DryRun
    When $true (default) no writes happen. Records 'dry_run_would_disable' rows.

.PARAMETER EnvFile
.PARAMETER LogDir

.EXAMPLE
    pwsh -NoProfile -Command "& ./scripts/Invoke-CwsRetryFailures.ps1 -SourceScanId 33 -SiteType onedrive -DryRun \$false"
#>

[CmdletBinding()]
param(
    [int]$SourceScanId = 33,
    [string]$SiteType = '',
    [bool]$DryRun = $true,
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
if (-not $LogDir) { $LogDir = Resolve-CwsLogDir -ScriptRoot $PSScriptRoot }

$runStamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmssZ")
$logFile = Join-Path $LogDir "cws_retry_failures_$runStamp.log"
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

$tenantId = _Require 'AZURE_TENANT_ID'
$clientId = _Require 'AZURE_CLIENT_ID'
$certRel  = _Require 'AZURE_CERTIFICATE'
$certPass = _Require 'AZURE_CERTIFICATE_PASS'
$adminUrl = _Require 'SHAREPOINT_ADMIN_URL'

$repoRoot = Split-Path -Parent $EnvFile
$certPath = if ([IO.Path]::IsPathRooted($certRel)) { $certRel } else { Join-Path $repoRoot ($certRel -replace '^\./', '') }
if (-not (Test-Path $certPath)) { throw "Certificate not found: $certPath" }

Write-Stage "Source scan id : $SourceScanId"
Write-Stage "Site type      : $(if ($SiteType) { $SiteType } else { 'all' })"
Write-Stage "Dry run        : $DryRun"
Write-Stage "Log file       : $logFile"

. (Join-Path $PSScriptRoot 'lib/Invoke-Psql.ps1')

$securePass = ConvertTo-SecureString -String $certPass -AsPlainText -Force
Write-Stage "Connecting to $adminUrl..."
Connect-PnPOnline -Url $adminUrl -ClientId $clientId -Tenant $tenantId `
    -CertificatePath $certPath -CertificatePassword $securePass `
    -WarningAction SilentlyContinue | Out-Null
Write-Stage "Connected."

# --- Pull rows -------------------------------------------------------------
$typeClause = if ($SiteType) { "AND site_type='$(_EscSql $SiteType)'" } else { '' }
$rowsOut = Invoke-Psql -TupleOnly -Sql "SELECT site_url || '|' || COALESCE(user_upn,'') || '|' || COALESCE(site_type,'') FROM company_wide_sharing_events WHERE scan_id=$SourceScanId AND action='failed' $typeClause ORDER BY site_url;"
$rows = @()
foreach ($line in ($rowsOut -split "`r?`n")) {
    $t = $line.Trim()
    if (-not $t) { continue }
    $p = $t -split '\|', 3
    if ($p.Count -eq 3) {
        $rows += [pscustomobject]@{ url=$p[0]; upn=$p[1]; type=$p[2] }
    }
}
Write-Stage "Loaded $($rows.Count) failed rows to retry."
if ($rows.Count -eq 0) { Stop-Transcript | Out-Null; return }

# --- Create new scan row ---------------------------------------------------
$dryRunSql = if ($DryRun) { 'true' } else { 'false' }
$insertOut = Invoke-Psql -TupleOnly -Sql "INSERT INTO company_wide_sharing_scans (trigger_type, triggered_by, status, dry_run, started_at) VALUES ('ps_retry_failed', 'pwsh-host', 'in_progress', $dryRunSql, NOW()) RETURNING id;"
$scanId = [int](($insertOut -split "`r?`n" | Where-Object { $_ -match '^\d+$' } | Select-Object -First 1))
if (-not $scanId) { throw "Failed to allocate scan id" }
Write-Stage "Created scan_id=$scanId"

function _InsertEvent {
    param([hashtable]$Row)
    $errSql = if ($Row.error_message) { "'$(_EscSql $Row.error_message)'" } else { 'NULL' }
    $sql = "INSERT INTO company_wide_sharing_events (scan_id, site_url, site_display_name, site_type, user_upn, previous_value, new_value, action, exempt_reason, error_message) VALUES ($($Row.scan_id),'$(_EscSql $Row.site_url)','$(_EscSql $Row.site_display_name)','$(_EscSql $Row.site_type)','$(_EscSql $Row.user_upn)','$(_EscSql $Row.previous_value)','$(_EscSql $Row.new_value)','$(_EscSql $Row.action)','$(_EscSql $Row.exempt_reason)',$errSql);"
    Invoke-Psql -Sql $sql | Out-Null
}

function _GetCws([string]$url) {
    try {
        $s = Get-PnPTenantSite -Identity $url -Detailed -ErrorAction Stop
        return [pscustomobject]@{ ok=$true; cws="$($s.DisableCompanyWideSharingLinks)"; title="$($s.Title)"; error=$null }
    } catch {
        return [pscustomobject]@{ ok=$false; cws=''; title=''; error=$_.Exception.Message }
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
        Write-Stage ("[{0,4}/{1}] {2} -> Get failed: {3}" -f $i, $rows.Count, $r.url, $st.error) 'WARN'
        _InsertEvent @{
            scan_id=$scanId; site_url=$r.url; site_display_name=''; site_type=$r.type; user_upn=$r.upn
            previous_value=''; new_value=''
            action='failed'; exempt_reason='get_site_failed'
            error_message=(_Sanitize $st.error)
        }
        $stillFailed++
        continue
    }
    $prev = $st.cws
    if ($prev -eq 'Disabled') {
        Write-Stage ("[{0,4}/{1}] {2} -> already Disabled" -f $i, $rows.Count, $r.url)
        _InsertEvent @{
            scan_id=$scanId; site_url=$r.url; site_display_name=$st.title; site_type=$r.type; user_upn=$r.upn
            previous_value=$prev; new_value=$prev
            action='already_compliant'; exempt_reason='cws_already_disabled'
            error_message=$null
        }
        $alreadyCompliant++
        continue
    }
    if ($DryRun) {
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

    # Live retry
    $errMsg = $null
    try {
        Set-PnPTenantSite -Identity $r.url -DisableCompanyWideSharingLinks Disabled -ErrorAction Stop -WarningAction SilentlyContinue | Out-Null
    } catch {
        $errMsg = $_.Exception.Message
    }
    $after = _GetCws $r.url
    $newCws = if ($after.ok) { $after.cws } else { 'unknown' }
    if (-not $errMsg -and $newCws -eq 'Disabled') {
        Write-Stage ("[{0,4}/{1}] {2} -> remediated ({3} -> {4})" -f $i, $rows.Count, $r.url, $prev, $newCws)
        _InsertEvent @{
            scan_id=$scanId; site_url=$r.url; site_display_name=$st.title; site_type=$r.type; user_upn=$r.upn
            previous_value=$prev; new_value=$newCws
            action='remediated'; exempt_reason=''
            error_message=$null
        }
        $remediated++
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
