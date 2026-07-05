#requires -Modules @{ ModuleName="PnP.PowerShell"; ModuleVersion="3.0.0" }

<#
.SYNOPSIS
    Apply targeted CWS remediation for a delta in OrgWideSharingLinkException
    membership. Adds re-enable CompanyWideSharingLinks on the user's OneDrive
    (NotDisabled); removes disable it (Disabled).

.DESCRIPTION
    The lifecycle-cron `cws_exception_group_sync_loop` polls Graph every 10
    minutes, diffs the group against cws_exception_group_snapshot, and on any
    change writes a small JSON file with the {adds, removes} UPN lists and
    invokes this script. 99% of polls produce no diff and this script never
    runs.

    For each UPN:
      1. Resolve OneDrive URL via Get-PnPUserProfileProperty -Account $upn.
      2. Get-PnPTenantSite to read current DisableCompanyWideSharingLinks.
      3. Apply the targeted change (or record dry-run / already_compliant).
      4. Record outcome in company_wide_sharing_events under a
         company_wide_sharing_scans row with trigger_type='exception_group_delta'.

    Allow-listed OneDrive URLs (extremely rare) are skipped on removes with
    exempt_reason='in_site_allowlist'.

.PARAMETER JobsFile
    Absolute path to a JSON file containing { "adds": [upn,...], "removes": [upn,...] }.

.PARAMETER ScanId
    Pre-existing company_wide_sharing_scans row id. The Python caller allocates
    the row with trigger_type='exception_group_delta' so the dashboard's
    "Recent Scans" list shows the same row this script updates.

.PARAMETER DryRun
    When $true (default), no Set-PnPTenantSite calls are made. Each decision
    records as action='dry_run_would_enable' or 'dry_run_would_disable'.

.EXAMPLE
    pwsh -NoProfile -File ./scripts/Invoke-CwsExceptionMembershipDelta.ps1 `
         -JobsFile /tmp/cws_exception_delta_42.json -ScanId 42 -DryRun $false
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$JobsFile,
    [Parameter(Mandatory)][int]$ScanId,
    # Accept as [string] not [bool] — pwsh -File cannot bind argv strings to a
    # [bool] parameter ("$false" / "0" both fail). The scan row's dry_run column
    # is the source of truth anyway; this param is effectively just a default.
    [string]$DryRun = 'true',
    [string]$EnvFile = $null,
    [string]$LogDir = $null
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$DryRunBool = ($DryRun -match '^(true|1|\$true)$')

if (-not $EnvFile) {
    $EnvFile = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    $EnvFile = Join-Path $EnvFile ".env"
}
. (Join-Path $PSScriptRoot 'lib/Invoke-Psql.ps1')
. (Join-Path $PSScriptRoot 'lib/CwsSiteUrl.ps1')
if (-not $LogDir) { $LogDir = Resolve-CwsLogDir -ScriptRoot $PSScriptRoot }

$runStamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmssZ")
$logFile = Join-Path $LogDir "cws_exception_delta_$runStamp.log"
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

if (-not (Test-Path $JobsFile)) { throw "JobsFile not found: $JobsFile" }
$jobs = Get-Content -Raw -Path $JobsFile | ConvertFrom-Json
$adds = @()
$removes = @()
if ($jobs.PSObject.Properties.Match('adds').Count -gt 0 -and $jobs.adds) {
    $adds = @($jobs.adds | ForEach-Object { ($_ -as [string]).ToLowerInvariant().Trim() } | Where-Object { $_ })
}
if ($jobs.PSObject.Properties.Match('removes').Count -gt 0 -and $jobs.removes) {
    $removes = @($jobs.removes | ForEach-Object { ($_ -as [string]).ToLowerInvariant().Trim() } | Where-Object { $_ })
}

Write-Stage "Scan id           : $ScanId"
Write-Stage "Dry run           : $DryRunBool (raw=$DryRun)"
Write-Stage "Adds (re-enable)  : $($adds.Count)"
Write-Stage "Removes (disable) : $($removes.Count)"
Write-Stage "Log file          : $logFile"

if ($adds.Count -eq 0 -and $removes.Count -eq 0) {
    Write-Stage "Nothing to do — empty jobs file."
    Invoke-Psql -Sql "UPDATE company_wide_sharing_scans SET status='completed', completed_at=NOW() WHERE id=$ScanId;" | Out-Null
    Stop-Transcript | Out-Null
    return
}

# --- Connect ----------------------------------------------------------------
$securePass = ConvertTo-SecureString -String $certPass -AsPlainText -Force
Write-Stage "Connecting to $adminUrl..."
Connect-PnPOnline -Url $adminUrl -ClientId $clientId -Tenant $tenantId `
    -CertificatePath $certPath -CertificatePassword $securePass `
    -WarningAction SilentlyContinue | Out-Null
Write-Stage "Connected."

# --- Load site allowlist (defensive — OneDrive URLs rarely overlap) ---------
$allowlistRaw = Invoke-Psql -TupleOnly -Sql "SELECT LOWER(RTRIM(site_url, '/')) FROM site_company_wide_sharing_allowlist;"
$allowlist = [System.Collections.Generic.HashSet[string]]::new()
foreach ($line in ($allowlistRaw -split "`r?`n")) {
    $t = $line.Trim()
    if ($t) { [void]$allowlist.Add($t) }
}

# --- Adopt scan row (honor its dry_run as the source of truth) --------------
$rowOut = (Invoke-Psql -TupleOnly -Sql "SELECT status || '|' || dry_run::text FROM company_wide_sharing_scans WHERE id=$ScanId;").ToString().Trim()
if (-not $rowOut) { throw "scan_id=$ScanId not found in company_wide_sharing_scans" }
$parts = $rowOut -split '\|', 2
$DryRunBool = ($parts[1] -eq 't' -or $parts[1] -eq 'true')
Write-Stage "Adopted scan_id=$ScanId (dry_run=$DryRunBool from scan row)"
Invoke-Psql -Sql "UPDATE company_wide_sharing_scans SET status='in_progress', started_at=NOW(), error_message=NULL WHERE id=$ScanId;" | Out-Null

# --- Helpers ----------------------------------------------------------------
function _InsertEvent {
    param([hashtable]$Row)
    $errSql = if ($Row.error_message) { "'$(_EscSql $Row.error_message)'" } else { 'NULL' }
    $sql = "INSERT INTO company_wide_sharing_events (scan_id, site_url, site_display_name, site_type, user_upn, previous_value, new_value, action, exempt_reason, error_message) VALUES ($($Row.scan_id),'$(_EscSql $Row.site_url)','$(_EscSql $Row.site_display_name)','$(_EscSql $Row.site_type)','$(_EscSql $Row.user_upn)','$(_EscSql $Row.previous_value)','$(_EscSql $Row.new_value)','$(_EscSql $Row.action)','$(_EscSql $Row.exempt_reason)',$errSql);"
    Invoke-Psql -Sql $sql | Out-Null
}

# Derive -my URL from the admin URL ("…-admin.sharepoint.com" → "…-my.sharepoint.com").
# Used to build OneDrive site URLs by string transformation rather than calling
# Get-PnPUserProfileProperty (which needs Profile permissions the app lacks).
$myUrlBase = $adminUrl.ToLowerInvariant().TrimEnd('/') -replace '-admin\.sharepoint\.com', '-my.sharepoint.com'

function _ResolveOneDriveUrl {
    param([Parameter(Mandatory)][string]$Upn)
    # SharePoint encodes a UPN into a OneDrive site segment by replacing every
    # character outside [a-z0-9-] with '_' (shared helper in lib/CwsSiteUrl.ps1
    # — keep all scripts on the same encoding). If the user has never logged
    # into OneDrive it won't be provisioned, but Get-PnPTenantSite will surface
    # that as 'Cannot get site' downstream.
    $encoded = ConvertTo-CwsPersonalSegment $Upn
    if (-not $encoded) {
        return [pscustomobject]@{ ok=$false; url=''; error='empty UPN after encoding' }
    }
    $url = "$myUrlBase/personal/$encoded"
    return [pscustomobject]@{ ok=$true; url=$url; error=$null }
}

function _GetSiteState {
    param([Parameter(Mandatory)][string]$Url)
    try {
        $s = Get-PnPTenantSite -Identity $Url -Detailed -ErrorAction Stop
        return [pscustomobject]@{ ok=$true; cws="$($s.DisableCompanyWideSharingLinks)"; title="$($s.Title)"; error=$null }
    } catch {
        return [pscustomobject]@{ ok=$false; cws=''; title=''; error=$_.Exception.Message }
    }
}

function _SetCws {
    param(
        [Parameter(Mandatory)][string]$Url,
        [Parameter(Mandatory)][ValidateSet('Disabled','NotDisabled')][string]$Value
    )
    $attempt = 0
    $maxAttempts = 3
    while ($attempt -lt $maxAttempts) {
        $attempt++
        try {
            Set-PnPTenantSite -Identity $Url `
                -DisableCompanyWideSharingLinks $Value `
                -ErrorAction Stop `
                -WarningAction SilentlyContinue 2>$null | Out-Null
            return $null
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
                return $e
            }
        }
    }
    return "exceeded $maxAttempts retries"
}

# --- Counters ---------------------------------------------------------------
$remediated = 0          # successful Set-PnPTenantSite calls (either direction)
$wouldRemediate = 0      # dry-run "would" rows
$alreadyCompliant = 0
$skipped = 0             # allowlist hit, OneDrive unprovisioned, etc.
$failed = 0

trap {
    Write-Stage "TRAPPED: $($_.Exception.Message)" 'ERROR'
    try {
        $em = _Sanitize $_.Exception.Message
        Invoke-Psql -Sql "UPDATE company_wide_sharing_scans SET status='failed', completed_at=NOW(), error_message='$(_EscSql $em)', remediated=$remediated, would_remediate=$wouldRemediate, already_compliant=$alreadyCompliant, errors=$failed, skipped_allowlist=$skipped, violations_found=($remediated + $wouldRemediate + $failed) WHERE id=$ScanId;" | Out-Null
    } catch {}
    try { Disconnect-PnPOnline -ErrorAction SilentlyContinue | Out-Null } catch {}
    Stop-Transcript | Out-Null
    exit 1
}

function _ProcessOne {
    param(
        [Parameter(Mandatory)][string]$Upn,
        [Parameter(Mandatory)][ValidateSet('add','remove')][string]$Direction
    )
    $targetValue = if ($Direction -eq 'add') { 'NotDisabled' } else { 'Disabled' }
    $oppositeValue = if ($Direction -eq 'add') { 'Disabled' } else { 'NotDisabled' }

    $resolve = _ResolveOneDriveUrl -Upn $Upn
    if (-not $resolve.ok) {
        Write-Stage ("  {0} {1} -> resolve failed: {2}" -f $Direction, $Upn, $resolve.error) 'WARN'
        _InsertEvent @{
            scan_id=$ScanId; site_url=''; site_display_name=''; site_type='onedrive'; user_upn=$Upn
            previous_value=''; new_value=''
            action='skipped_no_onedrive'; exempt_reason='onedrive_unresolvable'
            error_message=(_Sanitize $resolve.error)
        }
        $script:skipped++
        return
    }
    $url = $resolve.url

    if ($allowlist.Contains($url)) {
        Write-Stage ("  {0} {1} -> allow-listed OneDrive ({2}); skipping" -f $Direction, $Upn, $url)
        _InsertEvent @{
            scan_id=$ScanId; site_url=$url; site_display_name=''; site_type='onedrive'; user_upn=$Upn
            previous_value=''; new_value=''
            action='skipped'; exempt_reason='in_site_allowlist'
            error_message=$null
        }
        $script:skipped++
        return
    }

    $state = _GetSiteState -Url $url
    if (-not $state.ok) {
        $errMsg = $state.error
        if ($errMsg -match '^Cannot get site ') {
            Write-Stage ("  {0} {1} -> site not found in SPO" -f $Direction, $Upn) 'WARN'
            _InsertEvent @{
                scan_id=$ScanId; site_url=$url; site_display_name=''; site_type='onedrive'; user_upn=$Upn
                previous_value=''; new_value=''
                action='skipped_not_found'; exempt_reason='site_not_found_in_spo'
                error_message=$null
            }
            $script:skipped++
            return
        }
        Write-Stage ("  {0} {1} -> Get-PnPTenantSite failed: {2}" -f $Direction, $Upn, $errMsg) 'WARN'
        _InsertEvent @{
            scan_id=$ScanId; site_url=$url; site_display_name=''; site_type='onedrive'; user_upn=$Upn
            previous_value=''; new_value=''
            action='failed'; exempt_reason='get_site_failed'
            error_message=(_Sanitize $errMsg)
        }
        $script:failed++
        return
    }

    $prev = $state.cws
    if ($prev -eq $targetValue) {
        Write-Stage ("  {0} {1} -> already {2} (no-op)" -f $Direction, $Upn, $targetValue)
        _InsertEvent @{
            scan_id=$ScanId; site_url=$url; site_display_name=$state.title; site_type='onedrive'; user_upn=$Upn
            previous_value=$prev; new_value=$prev
            action='already_compliant'; exempt_reason=''
            error_message=$null
        }
        $script:alreadyCompliant++
        return
    }

    if ($DryRunBool) {
        $act = if ($Direction -eq 'add') { 'dry_run_would_enable' } else { 'dry_run_would_disable' }
        Write-Stage ("  {0} {1} -> DRY-RUN would set {2} (was {3})" -f $Direction, $Upn, $targetValue, $prev)
        _InsertEvent @{
            scan_id=$ScanId; site_url=$url; site_display_name=$state.title; site_type='onedrive'; user_upn=$Upn
            previous_value=$prev; new_value=$targetValue
            action=$act; exempt_reason=''
            error_message=$null
        }
        $script:wouldRemediate++
        return
    }

    $err = _SetCws -Url $url -Value $targetValue
    if ($null -ne $err) {
        Write-Stage ("  {0} {1} -> Set-PnPTenantSite failed: {2}" -f $Direction, $Upn, $err) 'ERROR'
        _InsertEvent @{
            scan_id=$ScanId; site_url=$url; site_display_name=$state.title; site_type='onedrive'; user_upn=$Upn
            previous_value=$prev; new_value=$targetValue
            action='failed'; exempt_reason=''
            error_message=(_Sanitize $err)
        }
        $script:failed++
        return
    }

    $act = if ($Direction -eq 'add') { 're_enabled' } else { 'remediated' }
    Write-Stage ("  {0} {1} -> {2} (was {3})" -f $Direction, $Upn, $targetValue, $prev)
    _InsertEvent @{
        scan_id=$ScanId; site_url=$url; site_display_name=$state.title; site_type='onedrive'; user_upn=$Upn
        previous_value=$prev; new_value=$targetValue
        action=$act; exempt_reason=''
        error_message=$null
    }
    $script:remediated++
}

# --- Process adds, then removes --------------------------------------------
foreach ($upn in $adds)    { _ProcessOne -Upn $upn -Direction 'add' }
foreach ($upn in $removes) { _ProcessOne -Upn $upn -Direction 'remove' }

$violations = $remediated + $wouldRemediate + $failed
Invoke-Psql -Sql "UPDATE company_wide_sharing_scans SET status='completed', completed_at=NOW(), total_sites_scanned=$($adds.Count + $removes.Count), violations_found=$violations, remediated=$remediated, would_remediate=$wouldRemediate, already_compliant=$alreadyCompliant, skipped_allowlist=$skipped, errors=$failed WHERE id=$ScanId;" | Out-Null

Write-Stage ("Done. remediated={0} would={1} already_compliant={2} skipped={3} failed={4}" -f `
    $remediated, $wouldRemediate, $alreadyCompliant, $skipped, $failed)

try { Disconnect-PnPOnline -ErrorAction SilentlyContinue | Out-Null } catch {}
Stop-Transcript | Out-Null
