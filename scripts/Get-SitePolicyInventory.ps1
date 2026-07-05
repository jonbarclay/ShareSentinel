<#
.SYNOPSIS
    Emit the tenant site inventory for the site policy scanner as JSON lines.

.DESCRIPTION
    Enumerates site collections via Get-PnPTenantSite (which pages correctly at
    tenant scale, unlike the raw CSOM getSitePropertiesFromSharePointByFilters
    REST call the Python scanner previously used — that returns only the first
    ~1,400 rows with no continuation cursor).

    Writes one JSON object per line to -OutFile:
        {"url": "...", "title": "...", "sharing_capability": "ExternalUserSharingOnly"}

    Scope selection:
      default            SharePoint sites only (personal sites excluded)
      -IncludeOneDrive 1 SharePoint + OneDrive personal sites
      -OneDriveOnly 1    OneDrive personal sites only

    Read-only: no tenant settings are modified. The Python scanner
    (services/lifecycle-cron/app/site_policy_scanner.py) consumes the file and
    applies policy via its existing CSOM write path.
#>
param(
    [Parameter(Mandatory = $true)][string]$OutFile,
    [int]$IncludeOneDrive = 0,
    [int]$OneDriveOnly = 0,
    [string]$EnvFile = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $EnvFile) {
    $EnvFile = Join-Path (Resolve-Path (Join-Path $PSScriptRoot '..')).Path '.env'
}
. (Join-Path $PSScriptRoot 'lib/Invoke-Psql.ps1')
Load-DotEnv -Path $EnvFile

function _Require([string]$name) {
    $v = [System.Environment]::GetEnvironmentVariable($name)
    if ([string]::IsNullOrWhiteSpace($v)) { throw "Missing env var: $name" }
    return $v
}

$tenantId = _Require 'AZURE_TENANT_ID'
$clientId = _Require 'AZURE_CLIENT_ID'
$certRel  = _Require 'AZURE_CERTIFICATE'
$certPass = _Require 'AZURE_CERTIFICATE_PASS'
$adminUrl = _Require 'SHAREPOINT_ADMIN_URL'

$repoRoot = Split-Path -Parent $EnvFile
$certPath = if ([System.IO.Path]::IsPathRooted($certRel)) { $certRel } else { Join-Path $repoRoot ($certRel -replace '^\./', '') }
if (-not (Test-Path $certPath)) { throw "Certificate not found: $certPath" }

$ts = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm:ssZ')
Write-Host "[$ts] [INFO] Inventory scope: $(if ($OneDriveOnly) { 'onedrive-only' } elseif ($IncludeOneDrive) { 'sharepoint+onedrive' } else { 'sharepoint-only' }) -> $OutFile"

$securePass = ConvertTo-SecureString -String $certPass -AsPlainText -Force
Connect-PnPOnline -Url $adminUrl `
    -ClientId $clientId `
    -Tenant $tenantId `
    -CertificatePath $certPath `
    -CertificatePassword $securePass `
    -WarningAction SilentlyContinue | Out-Null

$needOneDrive = ($IncludeOneDrive -ne 0) -or ($OneDriveOnly -ne 0)
if ($needOneDrive) {
    $sites = Get-PnPTenantSite -Detailed -IncludeOneDriveSites
} else {
    $sites = Get-PnPTenantSite -Detailed
}

# UTF8Encoding($false): no BOM — the Python consumer parses each line as JSON
$writer = [System.IO.StreamWriter]::new($OutFile, $false, [System.Text.UTF8Encoding]::new($false))
$emitted = 0
$skipped = 0
try {
    foreach ($s in $sites) {
        $url = "$($s.Url)"
        if (-not $url) { continue }
        $isPersonal = $url.ToLowerInvariant().Contains('/personal/')
        if ($OneDriveOnly -ne 0 -and -not $isPersonal) { $skipped++; continue }
        if ($OneDriveOnly -eq 0 -and $IncludeOneDrive -eq 0 -and $isPersonal) { $skipped++; continue }
        $writer.WriteLine((@{
            url                = $url
            title              = "$($s.Title)"
            sharing_capability = "$($s.SharingCapability)"
        } | ConvertTo-Json -Compress))
        $emitted++
    }
} finally {
    $writer.Flush()
    $writer.Dispose()
}

$ts = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm:ssZ')
Write-Host "[$ts] [INFO] Inventory complete: emitted=$emitted skipped=$skipped"
Disconnect-PnPOnline -ErrorAction SilentlyContinue | Out-Null
exit 0
