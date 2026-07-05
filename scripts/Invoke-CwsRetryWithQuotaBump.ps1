#requires -Modules @{ ModuleName="PnP.PowerShell"; ModuleVersion="3.0.0" }

<#
.SYNOPSIS
    Retry CWS remediation on sites that failed because they're over storage
    quota. Bumps the quota, sets DisableCompanyWideSharingLinks=Disabled,
    then restores the original quota.

.DESCRIPTION
    `Set-PnPTenantSite -DisableCompanyWideSharingLinks Disabled` writes a
    site-level policy record, which SPO rejects on sites that have exceeded
    their storage quota. For each matching failure in a prior scan this
    script:
      1. Reads StorageMaximumLevel, StorageUsageCurrent, and DisableCompanyWideSharingLinks.
      2. If CWS is already Disabled, records 'already_compliant' and skips.
      3. Bumps StorageMaximumLevel to max(currentMax, usage) + BumpMB.
      4. Sets DisableCompanyWideSharingLinks=Disabled.
      5. Restores StorageMaximumLevel to its original value (via finally so
         the restore runs even if the CWS set throws).
      6. Verifies post-state and records the outcome.

    DRY-RUN by default - flip with -DryRun:$false.

.PARAMETER SourceScanId
    Pull failed URLs from this scan_id. Default 37 (the OneDrive retry scan
    where the 181 quota failures live).

.PARAMETER ErrorPattern
    SQL LIKE pattern restricting which failures to retry. Default catches
    the quota error.

.PARAMETER BumpMB
    Megabytes of headroom to add above max(currentMax, usage). Default 1024
    (1 GB) - small enough to be safe, big enough to let any policy write
    through.

.PARAMETER DryRun
    When $true (default) reads state but doesn't mutate anything.

.PARAMETER EnvFile
.PARAMETER LogDir

.EXAMPLE
    pwsh -NoProfile -Command "& ./scripts/Invoke-CwsRetryWithQuotaBump.ps1 -DryRun \$false"

.EXAMPLE
    pwsh -NoProfile -Command "& ./scripts/Invoke-CwsRetryWithQuotaBump.ps1 -SourceScanId 37 -BumpMB 2048 -DryRun \$false"
#>

[CmdletBinding()]
param(
    [int]$SourceScanId = 37,
    [string]$ErrorPattern = 'Site has exceeded its maximum file storage limit%',
    [int]$BumpMB = 1024,
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
$logFile = Join-Path $LogDir "cws_quota_bump_$runStamp.log"
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
Write-Stage "Error pattern  : $ErrorPattern"
Write-Stage "Bump (MB)      : $BumpMB"
Write-Stage "Dry run        : $DryRun"
Write-Stage "Log file       : $logFile"

. (Join-Path $PSScriptRoot 'lib/Invoke-Psql.ps1')

$securePass = ConvertTo-SecureString -String $certPass -AsPlainText -Force
Write-Stage "Connecting to $adminUrl..."
Connect-PnPOnline -Url $adminUrl -ClientId $clientId -Tenant $tenantId `
    -CertificatePath $certPath -CertificatePassword $securePass `
    -WarningAction SilentlyContinue | Out-Null
Write-Stage "Connected."

# --- Pull rows --------------------------------------------------------------
$rowsOut = Invoke-Psql -TupleOnly -Sql "SELECT site_url || '|' || COALESCE(user_upn,'') || '|' || COALESCE(site_type,'') FROM company_wide_sharing_events WHERE scan_id=$SourceScanId AND action='failed' AND error_message LIKE '$(_EscSql $ErrorPattern)' ORDER BY site_url;"
$rows = @()
foreach ($line in ($rowsOut -split "`r?`n")) {
    $t = $line.Trim()
    if (-not $t) { continue }
    $p = $t -split '\|', 3
    if ($p.Count -eq 3) {
        $rows += [pscustomobject]@{ url=$p[0]; upn=$p[1]; type=$p[2] }
    }
}
Write-Stage "Loaded $($rows.Count) failed rows matching error pattern."
if ($rows.Count -eq 0) { Stop-Transcript | Out-Null; return }

# --- Create new scan row ----------------------------------------------------
$dryRunSql = if ($DryRun) { 'true' } else { 'false' }
$insertOut = Invoke-Psql -TupleOnly -Sql "INSERT INTO company_wide_sharing_scans (trigger_type, triggered_by, status, dry_run, started_at) VALUES ('ps_quota_bump', 'pwsh-host', 'in_progress', $dryRunSql, NOW()) RETURNING id;"
$scanId = [int](($insertOut -split "`r?`n" | Where-Object { $_ -match '^\d+$' } | Select-Object -First 1))
if (-not $scanId) { throw "Failed to allocate scan id" }
Write-Stage "Created scan_id=$scanId"

function _InsertEvent {
    param([hashtable]$Row)
    $errSql = if ($Row.error_message) { "'$(_EscSql $Row.error_message)'" } else { 'NULL' }
    $sql = "INSERT INTO company_wide_sharing_events (scan_id, site_url, site_display_name, site_type, user_upn, previous_value, new_value, action, exempt_reason, error_message) VALUES ($($Row.scan_id),'$(_EscSql $Row.site_url)','$(_EscSql $Row.site_display_name)','$(_EscSql $Row.site_type)','$(_EscSql $Row.user_upn)','$(_EscSql $Row.previous_value)','$(_EscSql $Row.new_value)','$(_EscSql $Row.action)','$(_EscSql $Row.exempt_reason)',$errSql);"
    Invoke-Psql -Sql $sql | Out-Null
}

function _GetSite([string]$url) {
    try {
        $s = Get-PnPTenantSite -Identity $url -Detailed -ErrorAction Stop
        # PnP quirk: returned property is StorageQuota; the Set-PnPTenantSite
        # parameter that writes the same value is named -StorageMaximumLevel.
        return [pscustomobject]@{
            ok = $true
            title = "$($s.Title)"
            cws = "$($s.DisableCompanyWideSharingLinks)"
            storage_max = [int64]$s.StorageQuota
            storage_warning = [int64]$s.StorageQuotaWarningLevel
            storage_used = [int64]$s.StorageUsageCurrent
            error = $null
        }
    } catch {
        return [pscustomobject]@{
            ok=$false; title=''; cws=''; storage_max=0; storage_warning=0; storage_used=0
            error=$_.Exception.Message
        }
    }
}

# OneDrive personal sites silently ignore Set-PnPTenantSite -StorageMaximumLevel.
# Set-PnPUserOneDriveQuota is the only cmdlet that actually writes per-user
# OneDrive quotas. Quota and warning are in BYTES (1 MB = 1048576).
function _SetOneDriveQuotaMB {
    param([string]$upn, [int64]$quotaMB, [int64]$warningMB)
    $quotaBytes = [int64]$quotaMB * 1048576L
    $warnBytes  = [int64]$warningMB * 1048576L
    # Warning must be < quota; if input is bad, pin warning to 90% of quota.
    if ($warnBytes -le 0 -or $warnBytes -ge $quotaBytes) {
        $warnBytes = [int64]([Math]::Floor($quotaBytes * 0.9))
    }
    Set-PnPUserOneDriveQuota -Account $upn -Quota $quotaBytes -QuotaWarning $warnBytes `
        -ErrorAction Stop -WarningAction SilentlyContinue | Out-Null
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
    $st = _GetSite $r.url
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
    $origMax = $st.storage_max
    $origWarn = $st.storage_warning
    $used    = $st.storage_used
    $newMax  = [int64]([Math]::Max($origMax, $used)) + [int64]$BumpMB
    $headroom = $newMax - $used
    $isOneDrive = ($r.type -eq 'onedrive')

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
        Write-Stage ("[{0,4}/{1}] {2} -> DRY-RUN would bump quota {3}MB->{4}MB (used={5}MB) then set Disabled" -f $i, $rows.Count, $r.url, $origMax, $newMax, $used)
        _InsertEvent @{
            scan_id=$scanId; site_url=$r.url; site_display_name=$st.title; site_type=$r.type; user_upn=$r.upn
            previous_value=$prev; new_value='Disabled'
            action='dry_run_would_disable'
            exempt_reason="bump_${origMax}_to_${newMax}MB"
            error_message=$null
        }
        $wouldRemediate++
        continue
    }

    # LIVE: bump quota, set CWS, restore quota.
    # OneDrive personal sites need Set-PnPUserOneDriveQuota; team sites use
    # Set-PnPTenantSite -StorageMaximumLevel.
    if ($isOneDrive -and [string]::IsNullOrWhiteSpace($r.upn)) {
        Write-Stage ("[{0,4}/{1}] {2} -> SKIP missing upn (cannot call Set-PnPUserOneDriveQuota)" -f $i, $rows.Count, $r.url) 'WARN'
        _InsertEvent @{
            scan_id=$scanId; site_url=$r.url; site_display_name=$st.title; site_type=$r.type; user_upn=$r.upn
            previous_value=$prev; new_value=$prev
            action='failed'; exempt_reason='missing_upn'
            error_message='No user_upn in source row; cannot raise OneDrive quota.'
        }
        $stillFailed++
        continue
    }

    $errMsg = $null
    $bumpOk = $false
    $setOk = $false
    try {
        if ($isOneDrive) {
            _SetOneDriveQuotaMB -upn $r.upn -quotaMB $newMax -warningMB ([int64]([Math]::Floor($newMax * 0.9)))
        } else {
            Set-PnPTenantSite -Identity $r.url -StorageMaximumLevel $newMax -ErrorAction Stop -WarningAction SilentlyContinue | Out-Null
        }
        $bumpOk = $true
        Set-PnPTenantSite -Identity $r.url -DisableCompanyWideSharingLinks Disabled -ErrorAction Stop -WarningAction SilentlyContinue | Out-Null
        $setOk = $true
    } catch {
        $errMsg = $_.Exception.Message
    } finally {
        if ($bumpOk) {
            try {
                if ($isOneDrive) {
                    _SetOneDriveQuotaMB -upn $r.upn -quotaMB $origMax -warningMB $origWarn
                } else {
                    Set-PnPTenantSite -Identity $r.url -StorageMaximumLevel $origMax -ErrorAction Stop -WarningAction SilentlyContinue | Out-Null
                }
            } catch {
                $restoreErr = $_.Exception.Message
                $errMsg = if ($errMsg) { "$errMsg | RESTORE-FAILED: $restoreErr" } else { "RESTORE-FAILED: $restoreErr" }
            }
        }
    }

    # Verify
    $after = _GetSite $r.url
    $newCws  = if ($after.ok) { $after.cws } else { 'unknown' }
    $afterMax = if ($after.ok) { $after.storage_max } else { -1 }

    if ($setOk -and $newCws -eq 'Disabled') {
        Write-Stage ("[{0,4}/{1}] {2} -> remediated (bumped {3}->{4}, restored to {5}MB)" -f $i, $rows.Count, $r.url, $origMax, $newMax, $afterMax)
        _InsertEvent @{
            scan_id=$scanId; site_url=$r.url; site_display_name=$st.title; site_type=$r.type; user_upn=$r.upn
            previous_value=$prev; new_value=$newCws
            action='remediated'
            exempt_reason="quota_${origMax}MB"
            error_message=$null
        }
        $remediated++
    } else {
        Write-Stage ("[{0,4}/{1}] {2} -> FAILED cws_after={3} quota_after={4}MB err={5}" -f $i, $rows.Count, $r.url, $newCws, $afterMax, $errMsg) 'ERROR'
        _InsertEvent @{
            scan_id=$scanId; site_url=$r.url; site_display_name=$st.title; site_type=$r.type; user_upn=$r.upn
            previous_value=$prev; new_value=$newCws
            action='failed'
            exempt_reason="quota_${origMax}MB_to_${newMax}MB"
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
