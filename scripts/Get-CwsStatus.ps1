#requires -Modules @{ ModuleName="PnP.PowerShell"; ModuleVersion="3.0.0" }

<#
.SYNOPSIS
    Report the current "Company-Wide Sharing Links" status for a user's
    OneDrive or a SharePoint site.

.DESCRIPTION
    Connects to SharePoint admin via PnP with the same cert auth as the
    bulk remediation script, then calls Get-PnPTenantSite to read the
    current value of DisableCompanyWideSharingLinks on the target site.

    Possible values:
      - Disabled    : "Anyone in your organization" links are BLOCKED.
                      The user CANNOT create company-wide sharing links.
      - NotDisabled : Company-wide sharing is ALLOWED on this site.
      - NotSpecified: Site default (behaves as NotDisabled / allowed).

.PARAMETER UserPrincipalName
    The UPN of a user whose OneDrive status you want to check
    (e.g. jbarclay@uvu.edu). The OneDrive URL is derived from
    SHAREPOINT_ADMIN_URL + the UPN.

.PARAMETER SiteUrl
    The full URL of a SharePoint site to check
    (e.g. https://uvu365.sharepoint.com/sites/Finance).

.PARAMETER CsvFile
    Path to a CSV file with a single column "UserPrincipalName".
    The script reports status for each UPN in the file, one row per UPN.
    Output is also written to ~/cws_status_<UTC-timestamp>.csv.

.PARAMETER EnvFile
    Path to the project .env file. Default: ../.env relative to the script.

.EXAMPLE
    pwsh -NoProfile -Command "& ./scripts/Get-CwsStatus.ps1 -UserPrincipalName jbarclay@uvu.edu"

.EXAMPLE
    pwsh -NoProfile -Command "& ./scripts/Get-CwsStatus.ps1 -SiteUrl https://uvu365.sharepoint.com/sites/Finance"

.EXAMPLE
    pwsh -NoProfile -Command "& ./scripts/Get-CwsStatus.ps1 -CsvFile ~/users_to_check.csv"
#>

[CmdletBinding(DefaultParameterSetName='OneDrive')]
param(
    [Parameter(Mandatory, Position=0, ParameterSetName='OneDrive')]
    [string]$UserPrincipalName,

    [Parameter(Mandatory, ParameterSetName='SharePoint')]
    [string]$SiteUrl,

    [Parameter(Mandatory, ParameterSetName='Csv')]
    [string]$CsvFile,

    [string]$EnvFile = $null
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# --- Load .env --------------------------------------------------------------
if (-not $EnvFile) {
    $EnvFile = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    $EnvFile = Join-Path $EnvFile ".env"
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

# Derive tenant prefix from admin URL: https://uvu365-admin.sharepoint.com -> uvu365
$tenantPrefix = ($adminUrl -replace '^https://', '' -split '-')[0]
$mySiteHost = "https://${tenantPrefix}-my.sharepoint.com"

# --- Connect ----------------------------------------------------------------
$securePass = ConvertTo-SecureString -String $certPass -AsPlainText -Force
Connect-PnPOnline -Url $adminUrl `
    -ClientId $clientId `
    -Tenant $tenantId `
    -CertificatePath $certPath `
    -CertificatePassword $securePass `
    -WarningAction SilentlyContinue | Out-Null

# --- Helpers ---------------------------------------------------------------
function _UpnToOneDriveUrl([string]$upn) {
    # jbarclay@uvu.edu -> https://uvu365-my.sharepoint.com/personal/jbarclay_uvu_edu
    $encoded = $upn.ToLowerInvariant().Replace('@', '_').Replace('.', '_')
    return "$mySiteHost/personal/$encoded"
}

function _Interpret([string]$value) {
    switch ($value) {
        'Disabled'     { 'BLOCKED   (cannot create "Anyone in your organization" links)' }
        'NotDisabled'  { 'ALLOWED   (CAN create company-wide sharing links)' }
        'NotSpecified' { 'ALLOWED   (site default - behaves as NotDisabled)' }
        default        { "unknown ($value)" }
    }
}

function _GetStatus([string]$url, [string]$displayUpn) {
    $result = [pscustomobject]@{
        UserPrincipalName              = $displayUpn
        SiteUrl                        = $url
        Status                         = $null
        Title                          = $null
        Owner                          = $null
        DisableCompanyWideSharingLinks = $null
        Interpretation                 = $null
        Error                          = $null
    }
    try {
        $site = Get-PnPTenantSite -Identity $url -Detailed -ErrorAction Stop
        $result.Status = "$($site.Status)"
        $result.Title  = "$($site.Title)"
        $result.Owner  = "$($site.Owner)"
        $cws = "$($site.DisableCompanyWideSharingLinks)"
        $result.DisableCompanyWideSharingLinks = $cws
        $result.Interpretation = _Interpret $cws
    } catch {
        $result.Error = $_.Exception.Message
    }
    return $result
}

# --- Single target ----------------------------------------------------------
if ($PSCmdlet.ParameterSetName -in @('OneDrive', 'SharePoint')) {
    if ($PSCmdlet.ParameterSetName -eq 'OneDrive') {
        $url = _UpnToOneDriveUrl $UserPrincipalName
        $displayUpn = $UserPrincipalName
    } else {
        $url = $SiteUrl
        $displayUpn = ''
    }

    $r = _GetStatus $url $displayUpn
    if ($r.Error) {
        Write-Host ""
        Write-Host ("ERROR: " + $r.Error) -ForegroundColor Red
    } else {
        Write-Host ""
        if ($displayUpn) { Write-Host ("User           : " + $displayUpn) }
        Write-Host        ("Site URL       : " + $r.SiteUrl)
        Write-Host        ("Title          : " + $r.Title)
        Write-Host        ("Status         : " + $r.Status)
        if ($r.Owner)   { Write-Host ("Owner          : " + $r.Owner) }
        Write-Host        ("CWS setting    : " + $r.DisableCompanyWideSharingLinks)
        $col = if ($r.DisableCompanyWideSharingLinks -eq 'Disabled') { 'Green' } else { 'Yellow' }
        Write-Host        ("Result         : " + $r.Interpretation) -ForegroundColor $col
        Write-Host ""
    }
    Disconnect-PnPOnline -ErrorAction SilentlyContinue | Out-Null
    return
}

# --- CSV batch mode ---------------------------------------------------------
$rows = Import-Csv -Path $CsvFile
$upnHeader = ($rows | Get-Member -MemberType NoteProperty | Where-Object { $_.Name -ieq 'UserPrincipalName' -or $_.Name -ieq 'upn' -or $_.Name -ieq 'user_upn' } | Select-Object -First 1).Name
if (-not $upnHeader) {
    throw "CSV must have a 'UserPrincipalName' (or 'upn' or 'user_upn') column."
}

$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmssZ")
$outCsv = Join-Path $HOME "cws_status_$stamp.csv"
$report = New-Object System.Collections.Generic.List[psobject]
$i = 0
foreach ($row in $rows) {
    $i++
    $upn = "$($row.$upnHeader)".Trim()
    if (-not $upn) { continue }
    $url = _UpnToOneDriveUrl $upn
    $r = _GetStatus $url $upn
    $report.Add($r) | Out-Null

    $status = if ($r.Error) { "ERROR" } else { $r.DisableCompanyWideSharingLinks }
    Write-Host ("[{0,4}] {1,-40} -> {2}" -f $i, $upn, $status)
}

$report | Export-Csv -Path $outCsv -NoTypeInformation
Write-Host ""
Write-Host "Wrote $($report.Count) rows to $outCsv"

$summary = $report | Where-Object { -not $_.Error } | Group-Object DisableCompanyWideSharingLinks | Sort-Object Count -Descending
Write-Host ""
Write-Host "Summary:"
foreach ($g in $summary) { Write-Host (" {0,-15} {1}" -f $g.Name, $g.Count) }
$errCount = ($report | Where-Object { $_.Error }).Count
if ($errCount -gt 0) { Write-Host (" {0,-15} {1}" -f 'ERROR', $errCount) }

Disconnect-PnPOnline -ErrorAction SilentlyContinue | Out-Null
