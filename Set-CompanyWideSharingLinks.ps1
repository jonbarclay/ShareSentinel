#requires -Modules @{ ModuleName="PnP.PowerShell"; ModuleVersion="1.12.0" }

<#
.SYNOPSIS
    Enable or disable company-wide sharing links on a user's OneDrive or a SharePoint site.

.DESCRIPTION
    Sets the DisableCompanyWideSharingLinks property on a SharePoint site or OneDrive
    personal site. When disabled, the "Anyone in your organization" option is removed
    from the sharing dialog for that site.

    Connects using the same Azure AD App certificate authentication as other scripts
    in this toolkit.

.PARAMETER UserPrincipalName
    The UPN of the user whose OneDrive site to modify (e.g. jbarclay@uvu.edu).
    The script derives the OneDrive URL from the tenant name and UPN.

.PARAMETER SiteUrl
    The full URL of a SharePoint site to modify (e.g. https://contoso.sharepoint.com/sites/Finance).
    Use this instead of -UserPrincipalName for non-OneDrive sites.

.PARAMETER DisableCompanyWideSharingLinks
    Removes the "Anyone in your organization" sharing option from the site.

.PARAMETER EnableCompanyWideSharingLinks
    Restores the "Anyone in your organization" sharing option on the site.

.PARAMETER LogFile
    Optional log file path for unified logging.

.EXAMPLE
    # Disable org-wide sharing on a user's OneDrive
    .\Set-CompanyWideSharingLinks.ps1 -UserPrincipalName jbarclay@uvu.edu -DisableCompanyWideSharingLinks

.EXAMPLE
    # Re-enable org-wide sharing on a user's OneDrive
    .\Set-CompanyWideSharingLinks.ps1 -UserPrincipalName jbarclay@uvu.edu -EnableCompanyWideSharingLinks

.EXAMPLE
    # Disable org-wide sharing on a SharePoint site
    .\Set-CompanyWideSharingLinks.ps1 -SiteUrl https://contoso.sharepoint.com/sites/Finance -DisableCompanyWideSharingLinks
#>

[CmdletBinding()]
param(
    # Target: a user's OneDrive (derived from UPN)
    [Parameter(Mandatory, ParameterSetName = "OneDriveDisable")]
    [Parameter(Mandatory, ParameterSetName = "OneDriveEnable")]
    [string]$UserPrincipalName,

    # Target: a SharePoint site URL
    [Parameter(Mandatory, ParameterSetName = "SiteDisable")]
    [Parameter(Mandatory, ParameterSetName = "SiteEnable")]
    [string]$SiteUrl,

    # Action: disable company-wide sharing links
    [Parameter(Mandatory, ParameterSetName = "OneDriveDisable")]
    [Parameter(Mandatory, ParameterSetName = "SiteDisable")]
    [switch]$DisableCompanyWideSharingLinks,

    # Action: enable company-wide sharing links
    [Parameter(Mandatory, ParameterSetName = "OneDriveEnable")]
    [Parameter(Mandatory, ParameterSetName = "SiteEnable")]
    [switch]$EnableCompanyWideSharingLinks,

    # Log file path (passed by Run-All.ps1 for unified logging)
    [string]$LogFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptContext = "CompanyWideSharing"

# --- Logging ---
. (Join-Path $PSScriptRoot "PipelineLogger.ps1")
if ($LogFile) { Set-PipelineLogFile -Path $LogFile }

# --- Load .env ---
$envFile = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path $envFile)) {
    throw ".env file not found at $envFile"
}

Get-Content $envFile | ForEach-Object {
    if ($_ -match '^\s*#' -or [string]::IsNullOrWhiteSpace($_)) { return }
    $parts = $_ -split '=', 2
    if ($parts.Count -eq 2) {
        [System.Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim())
    }
}

# --- Validate environment variables ---
$tenantId    = $env:AZURE_TENANT_ID
$tenantName  = $env:AZURE_TENANT_NAME
$clientId    = $env:AZURE_CLIENT_ID
$certPath    = Join-Path $PSScriptRoot ($env:AZURE_CERTIFICATE -replace '^\.\/', '')
$certPass    = $env:AZURE_CERTIFICATE_PASS

$missingVars = @()
if ([string]::IsNullOrWhiteSpace($tenantId))                    { $missingVars += 'AZURE_TENANT_ID' }
if ([string]::IsNullOrWhiteSpace($tenantName))                  { $missingVars += 'AZURE_TENANT_NAME' }
if ([string]::IsNullOrWhiteSpace($clientId))                    { $missingVars += 'AZURE_CLIENT_ID' }
if ([string]::IsNullOrWhiteSpace($env:AZURE_CERTIFICATE))       { $missingVars += 'AZURE_CERTIFICATE' }
if ([string]::IsNullOrWhiteSpace($certPass))                    { $missingVars += 'AZURE_CERTIFICATE_PASS' }
if ($missingVars.Count -gt 0) {
    throw "Missing required .env variables: $($missingVars -join ', ')"
}

if (-not (Test-Path $certPath)) {
    throw "Certificate file not found: $certPath"
}

# --- Derive URLs ---
$tenantPrefix = ($tenantName -split '\.')[0]
$adminUrl     = "https://$tenantPrefix-admin.sharepoint.com"

# Determine target site URL
if ($UserPrincipalName) {
    # Convert UPN to OneDrive URL: user@domain.com -> user_domain_com
    $encodedUpn = $UserPrincipalName.Replace('@', '_').Replace('.', '_')
    $targetSiteUrl = "https://$tenantPrefix-my.sharepoint.com/personal/$encodedUpn"
    Write-Log "Target     : OneDrive for $UserPrincipalName" -Context $ScriptContext
} else {
    $targetSiteUrl = $SiteUrl
    Write-Log "Target     : SharePoint site $SiteUrl" -Context $ScriptContext
}

# Determine action
if ($DisableCompanyWideSharingLinks) {
    $action = "Disabled"
    $actionDescription = "Disabling"
} else {
    $action = "NotDisabled"
    $actionDescription = "Enabling"
}

Write-Log "Tenant ID  : $tenantId" -Context $ScriptContext
Write-Log "Client ID  : $clientId" -Context $ScriptContext
Write-Log "Admin URL  : $adminUrl" -Context $ScriptContext
Write-Log "Cert Path  : $certPath" -Context $ScriptContext
Write-Log "Site URL   : $targetSiteUrl" -Context $ScriptContext
Write-Log "Action     : $actionDescription company-wide sharing links" -Context $ScriptContext

# --- Connect to SharePoint Admin ---
$securePassword = ConvertTo-SecureString -String $certPass -AsPlainText -Force

Write-Log "`nConnecting to $adminUrl..." -Context $ScriptContext

Connect-PnPOnline -Url $adminUrl `
    -ClientId  $clientId `
    -Tenant    $tenantId `
    -CertificatePath     $certPath `
    -CertificatePassword $securePassword

Write-Log "Connected successfully." -Context $ScriptContext

# --- Apply the setting ---
try {
    Write-Log "`n$actionDescription company-wide sharing links on $targetSiteUrl..." -Context $ScriptContext

    Set-PnPTenantSite -Identity $targetSiteUrl -DisableCompanyWideSharingLinks $action

    Write-Log "$actionDescription company-wide sharing links completed successfully." -Context $ScriptContext -ForegroundColor Green
}
catch {
    Write-Log "Failed to set DisableCompanyWideSharingLinks: $_" -Context $ScriptContext -Level ERROR -ForegroundColor Red
    throw
}
finally {
    Disconnect-PnPOnline -ErrorAction SilentlyContinue
}
