#requires -Modules @{ ModuleName="PnP.PowerShell"; ModuleVersion="3.0.0" }

<#
.SYNOPSIS
    Reclassify OneDrive 'Cannot get site' failures from a prior CWS scan based on
    whether the user still exists in Entra ID.

.DESCRIPTION
    The bulk remediation script (Invoke-CwsBulkRemediation.ps1) records a
    OneDrive failure as action='failed' with error_message='Cannot get site ...'
    in two distinct cases:
      1. User is hard-deleted from Entra ID (purged from the 30-day recycle bin)
         and their OneDrive personal site no longer exists. -> skipped_user_deleted
      2. User account still exists but their OneDrive is gone (admin-deleted
         personal site, never provisioned, etc.) -> skipped_no_onedrive

    For each matching event in the source scan, this script queries Graph
    /users/{upn} to determine which case applies, then updates the event row
    with the correct action and exempt_reason.

.PARAMETER SourceScanId
    Pull OneDrive 'Cannot get site' rows from this scan_id.

.PARAMETER DryRun
    When $true (default) report what would change but don't write to postgres.

.PARAMETER EnvFile
.PARAMETER LogDir
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

if (-not $EnvFile) {
    $EnvFile = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    $EnvFile = Join-Path $EnvFile ".env"
}
. (Join-Path $PSScriptRoot 'lib/Invoke-Psql.ps1')
if (-not $LogDir) { $LogDir = Resolve-CwsLogDir -ScriptRoot $PSScriptRoot }

$runStamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmssZ")
$logFile = Join-Path $LogDir "cws_resolve_users_$runStamp.log"
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
Write-Stage "Dry run        : $DryRun"
Write-Stage "Log file       : $logFile"

# --- Postgres helpers (dual-mode: host docker exec or in-container psql) ----
. (Join-Path $PSScriptRoot 'lib/Invoke-Psql.ps1')

# --- Connect ----
$securePass = ConvertTo-SecureString -String $certPass -AsPlainText -Force
Write-Stage "Connecting to $adminUrl..."
Connect-PnPOnline -Url $adminUrl -ClientId $clientId -Tenant $tenantId `
    -CertificatePath $certPath -CertificatePassword $securePass `
    -WarningAction SilentlyContinue | Out-Null
Write-Stage "Connected."

# --- Pull rows ----
Write-Stage "Loading OneDrive 'Cannot get site' rows from scan $SourceScanId..."
$rowsOut = Invoke-Psql -TupleOnly -Sql "SELECT id || '|' || user_upn FROM company_wide_sharing_events WHERE scan_id=$SourceScanId AND site_type='onedrive' AND action='failed' AND error_message LIKE 'Cannot get site %' AND user_upn != '' ORDER BY id;"
$rows = @()
foreach ($line in ($rowsOut -split "`r?`n")) {
    $t = $line.Trim()
    if (-not $t) { continue }
    $p = $t -split '\|', 2
    if ($p.Count -eq 2) {
        $rows += [pscustomobject]@{ id=[int]$p[0]; upn=$p[1] }
    }
}
Write-Stage "Loaded $($rows.Count) rows to classify."
if ($rows.Count -eq 0) { Stop-Transcript | Out-Null; return }

# --- Classify each via Graph ----
$cDeleted = 0
$cNoDrive = 0
$cLookupErr = 0
$idsDeleted = [System.Collections.Generic.List[int]]::new()
$idsNoDrive = [System.Collections.Generic.List[int]]::new()
$startTime = Get-Date

$i = 0
foreach ($r in $rows) {
    $i++
    $found = $null
    $attempt = 0
    while ($attempt -lt 3) {
        $attempt++
        try {
            $u = $r.upn
            $resp = Invoke-PnPGraphMethod -Url "v1.0/users/$u`?`$select=id,userPrincipalName,accountEnabled" -Method Get -ErrorAction Stop
            $found = $true
            break
        } catch {
            $em = $_.Exception.Message
            if ($em -match '\b404\b|does not exist|Resource .* does not exist') {
                $found = $false
                break
            } elseif ($em -match '429|throttle|too many requests') {
                Start-Sleep -Seconds ([int]([Math]::Min(60, 5 * [Math]::Pow(2, $attempt - 1))))
                continue
            } else {
                $cLookupErr++
                Write-Stage ("  lookup error for {0}: {1}" -f $r.upn, $em) 'WARN'
                break
            }
        }
    }

    if ($null -eq $found) { continue }

    if ($found) {
        $idsNoDrive.Add($r.id) | Out-Null
        $cNoDrive++
    } else {
        $idsDeleted.Add($r.id) | Out-Null
        $cDeleted++
    }

    if (($i % 100) -eq 0) {
        $rate = if (((Get-Date) - $startTime).TotalSeconds -gt 0) {
            [Math]::Round($i / ((Get-Date) - $startTime).TotalSeconds, 2)
        } else { 0 }
        Write-Stage ("progress {0}/{1} deleted={2} no_onedrive={3} lookup_err={4} rate={5}/s" -f $i, $rows.Count, $cDeleted, $cNoDrive, $cLookupErr, $rate)
    }
}

$elapsed = (Get-Date) - $startTime
Write-Stage ""
Write-Stage "Classification complete in $($elapsed.ToString('hh\:mm\:ss')):"
Write-Stage "  user_deleted     : $cDeleted"
Write-Stage "  no_onedrive      : $cNoDrive"
Write-Stage "  lookup_errors    : $cLookupErr"

if ($DryRun) {
    Write-Stage "DRY-RUN: not writing updates."
} else {
    # --- Apply updates in batches ----
    function _UpdateBatch {
        param([System.Collections.Generic.List[int]]$Ids, [string]$Action, [string]$Reason)
        if ($Ids.Count -eq 0) { return }
        $batchSize = 500
        for ($start = 0; $start -lt $Ids.Count; $start += $batchSize) {
            $end = [Math]::Min($start + $batchSize - 1, $Ids.Count - 1)
            $slice = $Ids[$start..$end]
            $idsCsv = $slice -join ','
            $sql = "UPDATE company_wide_sharing_events SET action='$Action', exempt_reason='$(_EscSql $Reason)', error_message=NULL WHERE id IN ($idsCsv);"
            Invoke-Psql -Sql $sql | Out-Null
        }
    }
    Write-Stage "Updating $cDeleted rows to action=skipped_user_deleted..."
    _UpdateBatch -Ids $idsDeleted -Action 'skipped_user_deleted' -Reason 'user_not_in_entra'
    Write-Stage "Updating $cNoDrive rows to action=skipped_no_onedrive..."
    _UpdateBatch -Ids $idsNoDrive -Action 'skipped_no_onedrive' -Reason 'onedrive_site_missing'

    # Re-derive scan counters
    Write-Stage "Re-deriving scan $SourceScanId counters..."
    $sql = "WITH ec AS (SELECT COUNT(*) FILTER (WHERE action='remediated') AS remediated, COUNT(*) FILTER (WHERE action='failed') AS errors, COUNT(*) FILTER (WHERE action='dry_run_would_disable') AS would_remediate FROM company_wide_sharing_events WHERE scan_id=$SourceScanId) UPDATE company_wide_sharing_scans s SET errors=ec.errors, remediated=ec.remediated, would_remediate=ec.would_remediate, violations_found=ec.remediated+ec.errors+ec.would_remediate FROM ec WHERE s.id=$SourceScanId;"
    Invoke-Psql -Sql $sql | Out-Null
    Write-Stage "Scan counters updated."
}

try { Disconnect-PnPOnline -ErrorAction SilentlyContinue | Out-Null } catch {}
Stop-Transcript | Out-Null
