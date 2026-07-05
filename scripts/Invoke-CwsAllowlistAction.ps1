#requires -Modules @{ ModuleName="PnP.PowerShell"; ModuleVersion="3.0.0" }

<#
.SYNOPSIS
    Apply a targeted CWS state change to a single SharePoint site in response
    to a dashboard allowlist add/remove. Companion to Invoke-CwsExceptionMembershipDelta.ps1
    (which handles per-OneDrive flips driven by Entra group membership).

.DESCRIPTION
    Drains a `cws_allow_site` or `cws_disallow_site` action queued on Redis by
    the dashboard. cws_allow_site sets DisableCompanyWideSharingLinks=NotDisabled
    (re-enables CWS); cws_disallow_site sets it to Disabled. Idempotent — a
    site already in the target state records as `already_compliant`. Records
    one row to company_wide_sharing_events under a pre-allocated
    company_wide_sharing_scans row with trigger_type='allowlist_action'.

.PARAMETER Action
    cws_allow_site (enable CWS) or cws_disallow_site (disable CWS).

.PARAMETER SiteUrl
    Full https://… site URL.

.PARAMETER SiteDisplayName
    Optional, persisted to the event row for audit context.

.PARAMETER TriggeredBy
    Email/UPN of the dashboard user who initiated the change. Stored on the
    scan row for the audit log.

.PARAMETER ScanId
    Pre-existing company_wide_sharing_scans row id (the Python consumer
    allocates one per Redis message).
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('cws_allow_site','cws_disallow_site')][string]$Action,
    [Parameter(Mandatory)][string]$SiteUrl,
    [string]$SiteDisplayName = '',
    [string]$TriggeredBy = '',
    [Parameter(Mandatory)][int]$ScanId,
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
$logFile = Join-Path $LogDir "cws_allowlist_action_$runStamp.log"
Start-Transcript -Path $logFile -Force | Out-Null

function Write-Stage {
    param([string]$Msg, [string]$Level = 'INFO')
    $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ssZ")
    Write-Host "[$ts] [$Level] $Msg"
}

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

$targetValue = if ($Action -eq 'cws_allow_site') { 'NotDisabled' } else { 'Disabled' }

Write-Stage "Scan id          : $ScanId"
Write-Stage "Action           : $Action -> $targetValue"
Write-Stage "Site URL         : $SiteUrl"
Write-Stage "Site display name: $SiteDisplayName"
Write-Stage "Triggered by     : $TriggeredBy"
Write-Stage "Log file         : $logFile"

$siteType = if ($SiteUrl.ToLowerInvariant().Contains('/personal/')) { 'onedrive' } else { 'site' }

# --- Connect ----------------------------------------------------------------
$securePass = ConvertTo-SecureString -String $certPass -AsPlainText -Force
Write-Stage "Connecting to $adminUrl..."
Connect-PnPOnline -Url $adminUrl -ClientId $clientId -Tenant $tenantId `
    -CertificatePath $certPath -CertificatePassword $securePass `
    -WarningAction SilentlyContinue | Out-Null
Write-Stage "Connected."

# --- Adopt scan row ---------------------------------------------------------
$rowOut = (Invoke-Psql -TupleOnly -Sql "SELECT status FROM company_wide_sharing_scans WHERE id=$ScanId;").ToString().Trim()
if (-not $rowOut) { throw "scan_id=$ScanId not found in company_wide_sharing_scans" }
Invoke-Psql -Sql "UPDATE company_wide_sharing_scans SET status='in_progress', started_at=NOW(), error_message=NULL WHERE id=$ScanId;" | Out-Null

function _InsertEvent {
    param([hashtable]$Row)
    $errSql = if ($Row.error_message) { "'$(_EscSql $Row.error_message)'" } else { 'NULL' }
    $sql = "INSERT INTO company_wide_sharing_events (scan_id, site_url, site_display_name, site_type, user_upn, previous_value, new_value, action, exempt_reason, error_message) VALUES ($($Row.scan_id),'$(_EscSql $Row.site_url)','$(_EscSql $Row.site_display_name)','$(_EscSql $Row.site_type)','$(_EscSql $Row.user_upn)','$(_EscSql $Row.previous_value)','$(_EscSql $Row.new_value)','$(_EscSql $Row.action)','$(_EscSql $Row.exempt_reason)',$errSql);"
    Invoke-Psql -Sql $sql | Out-Null
}

trap {
    Write-Stage "TRAPPED: $($_.Exception.Message)" 'ERROR'
    try {
        $em = _Sanitize $_.Exception.Message
        Invoke-Psql -Sql "UPDATE company_wide_sharing_scans SET status='failed', completed_at=NOW(), error_message='$(_EscSql $em)', errors=1, violations_found=1 WHERE id=$ScanId;" | Out-Null
    } catch {}
    try { Disconnect-PnPOnline -ErrorAction SilentlyContinue | Out-Null } catch {}
    Stop-Transcript | Out-Null
    exit 1
}

# --- Read current state -----------------------------------------------------
$prev = 'unknown'
$title = ''
try {
    $site = Get-PnPTenantSite -Identity $SiteUrl -Detailed -ErrorAction Stop
    $prev = "$($site.DisableCompanyWideSharingLinks)"
    $title = "$($site.Title)"
} catch {
    $err = $_.Exception.Message
    Write-Stage "Get-PnPTenantSite failed: $err" 'ERROR'
    _InsertEvent @{
        scan_id=$ScanId; site_url=$SiteUrl; site_display_name=$SiteDisplayName; site_type=$siteType; user_upn=''
        previous_value=''; new_value=''
        action='failed'; exempt_reason='get_site_failed'
        error_message=(_Sanitize $err)
    }
    Invoke-Psql -Sql "UPDATE company_wide_sharing_scans SET status='failed', completed_at=NOW(), errors=1, violations_found=1, error_message='$(_EscSql (_Sanitize $err))' WHERE id=$ScanId;" | Out-Null
    try { Disconnect-PnPOnline -ErrorAction SilentlyContinue | Out-Null } catch {}
    Stop-Transcript | Out-Null
    exit 1
}

if (-not $SiteDisplayName -and $title) { $SiteDisplayName = $title }

if ($prev -eq $targetValue) {
    Write-Stage "Site already at target state ($prev) — recording already_compliant"
    _InsertEvent @{
        scan_id=$ScanId; site_url=$SiteUrl; site_display_name=$SiteDisplayName; site_type=$siteType; user_upn=''
        previous_value=$prev; new_value=$prev
        action='already_compliant'; exempt_reason=''
        error_message=$null
    }
    Invoke-Psql -Sql "UPDATE company_wide_sharing_scans SET status='completed', completed_at=NOW(), total_sites_scanned=1, already_compliant=1 WHERE id=$ScanId;" | Out-Null
    try { Disconnect-PnPOnline -ErrorAction SilentlyContinue | Out-Null } catch {}
    Stop-Transcript | Out-Null
    return
}

# --- Apply change with retry ------------------------------------------------
$errMsg = $null
$attempt = 0
$maxAttempts = 3
while ($attempt -lt $maxAttempts) {
    $attempt++
    try {
        Set-PnPTenantSite -Identity $SiteUrl `
            -DisableCompanyWideSharingLinks $targetValue `
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
                Connect-PnPOnline -Url $adminUrl -ClientId $clientId -Tenant $tenantId `
                    -CertificatePath $certPath -CertificatePassword $securePass `
                    -WarningAction SilentlyContinue | Out-Null
            } catch {}
            continue
        } else {
            $errMsg = $e
            break
        }
    }
}

if ($null -ne $errMsg) {
    Write-Stage "Set-PnPTenantSite failed: $errMsg" 'ERROR'
    _InsertEvent @{
        scan_id=$ScanId; site_url=$SiteUrl; site_display_name=$SiteDisplayName; site_type=$siteType; user_upn=''
        previous_value=$prev; new_value=$targetValue
        action='failed'; exempt_reason=''
        error_message=(_Sanitize $errMsg)
    }
    Invoke-Psql -Sql "UPDATE company_wide_sharing_scans SET status='failed', completed_at=NOW(), total_sites_scanned=1, errors=1, violations_found=1, error_message='$(_EscSql (_Sanitize $errMsg))' WHERE id=$ScanId;" | Out-Null
    try { Disconnect-PnPOnline -ErrorAction SilentlyContinue | Out-Null } catch {}
    Stop-Transcript | Out-Null
    exit 1
}

$eventAction = if ($Action -eq 'cws_allow_site') { 're_enabled' } else { 'remediated' }
Write-Stage "Set $SiteUrl -> $targetValue (was $prev)"
_InsertEvent @{
    scan_id=$ScanId; site_url=$SiteUrl; site_display_name=$SiteDisplayName; site_type=$siteType; user_upn=''
    previous_value=$prev; new_value=$targetValue
    action=$eventAction; exempt_reason=''
    error_message=$null
}
Invoke-Psql -Sql "UPDATE company_wide_sharing_scans SET status='completed', completed_at=NOW(), total_sites_scanned=1, violations_found=1, remediated=1 WHERE id=$ScanId;" | Out-Null

try { Disconnect-PnPOnline -ErrorAction SilentlyContinue | Out-Null } catch {}
Stop-Transcript | Out-Null
