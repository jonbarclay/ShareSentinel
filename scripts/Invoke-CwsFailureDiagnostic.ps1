#requires -Modules @{ ModuleName="PnP.PowerShell"; ModuleVersion="3.0.0" }

<#
.SYNOPSIS
    Diagnose why a single site/OneDrive failed CWS remediation and persist the
    structured reason to `cws_failure_diagnostics`.

.DESCRIPTION
    Wraps the SharePoint-Oversharing-Toolkit's Diagnose-OneDriveSite.ps1 (which
    is mounted read-only at /opt/toolkit/ in the lifecycle-cron container).
    Runs the toolkit script for the given SiteUrl (or UserPrincipalName for
    OneDrive personal sites), captures stdout, parses the labelled "Found N
    potential blocker(s)" section into a single failure_category enum value
    + a one-line human summary, and UPSERTs into cws_failure_diagnostics.

    The wrapper never crashes on missing toolkit / PS exceptions — it records
    `diagnostic_unavailable` or `diagnostic_error` so the dashboard always has
    a row to display.

.PARAMETER SiteUrl
    Full https://… site URL of the site whose remediation failed. Primary key.

.PARAMETER UserPrincipalName
    UPN if this is a OneDrive personal site. Passed through to the toolkit so
    it can correlate license SKU caps with quota issues.

.PARAMETER SiteDisplayName
    Optional, persisted for audit context.

.PARAMETER SiteType
    'site' or 'onedrive'. Derived from SiteUrl when blank.

.PARAMETER SourceEventId
    company_wide_sharing_events.id that surfaced the failure (0 if unknown).

.PARAMETER SourceErrorMessage
    The original error_message recorded against the failing event row.
    Categorised when the toolkit itself can't reach the site.

.PARAMETER ToolkitPath
    Filesystem path to the SharePoint-Oversharing-Toolkit checkout.
    Defaults to $env:TOOLKIT_PATH or /opt/toolkit.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$SiteUrl,
    [string]$UserPrincipalName = '',
    [string]$SiteDisplayName = '',
    [string]$SiteType = '',
    [int]$SourceEventId = 0,
    [string]$SourceErrorMessage = '',
    [string]$ToolkitPath = $null,
    [string]$LogDir = $null
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'lib/Invoke-Psql.ps1')
. (Join-Path $PSScriptRoot 'lib/CwsSiteUrl.ps1')

if (-not $ToolkitPath) {
    $ToolkitPath = if ($env:TOOLKIT_PATH) { $env:TOOLKIT_PATH } else { '/opt/toolkit' }
}
if (-not $LogDir) { $LogDir = Resolve-CwsLogDir -ScriptRoot $PSScriptRoot }

$runStamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmssZ")
$logFile = Join-Path $LogDir "cws_failure_diagnostic_$runStamp.log"
Start-Transcript -Path $logFile -Force | Out-Null

function Write-Stage {
    param([string]$Msg, [string]$Level = 'INFO')
    $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ssZ")
    Write-Host "[$ts] [$Level] $Msg"
}

if (-not $SiteType) {
    $SiteType = if ($SiteUrl.ToLowerInvariant().Contains('/personal/')) { 'onedrive' } else { 'site' }
}

Write-Stage "Site URL          : $SiteUrl"
Write-Stage "User UPN          : $UserPrincipalName"
Write-Stage "Site type         : $SiteType"
Write-Stage "Source event id   : $SourceEventId"
Write-Stage "Toolkit path      : $ToolkitPath"
Write-Stage "Log file          : $logFile"

# --- Category classifier ----------------------------------------------------
# Order matters: first match wins. Each pattern is anchored to a single issue
# string (either the toolkit's "[N] …" line or the upstream error_message).
# Categories produced by the Graph pre-check (no pattern row needed; they are
# emitted directly): user_purged, user_recently_deleted.
$CATEGORY_PATTERNS = @(
    # SharePoint Embedded containers / legacy public sites / Content Type Hub:
    # not manageable via Set-PnPTenantSite. Matched first so the raw PnP error
    # ("not supported for site") isn't mistaken for a OneDrive license/quota
    # blocker. The URL-shape short-circuit below catches these before the
    # toolkit runs; this pattern is the fallback when only the error is known.
    @{ Cat = 'unsupported_site_type';   Pattern = 'not supported for site' },
    @{ Cat = 'lockstate_locked';        Pattern = "LockState is '" },
    @{ Cat = 'archived';                Pattern = "ArchiveStatus is '" },
    @{ Cat = 'quota_exceeded';          Pattern = 'at or above quota' },
    @{ Cat = 'quota_warning';           Pattern = '% of quota' },
    @{ Cat = 'account_disabled';        Pattern = 'Azure AD account is disabled' },
    @{ Cat = 'no_usage_location';       Pattern = 'UsageLocation is empty' },
    @{ Cat = 'a1_license_cap';          Pattern = 'A1 SKU' },
    @{ Cat = 'no_license';              Pattern = 'zero assigned licenses' },
    @{ Cat = 'no_onedrive_provisioned'; Pattern = 'has never been provisioned' },
    @{ Cat = 'no_onedrive_provisioned'; Pattern = 'Cannot get site' },
    @{ Cat = 'no_onedrive_provisioned'; Pattern = 'does not exist' },
    @{ Cat = 'no_onedrive_provisioned'; Pattern = 'File Not Found' },
    @{ Cat = 'user_deleted';            Pattern = 'user was not found' },
    @{ Cat = 'user_deleted';            Pattern = 'Request_ResourceNotFound' },
    @{ Cat = 'access_denied';           Pattern = '\bAccess denied\b' },
    @{ Cat = 'access_denied';           Pattern = '\bUnauthorized\b' },
    @{ Cat = 'access_denied';           Pattern = '\b401\b' },
    @{ Cat = 'access_denied';           Pattern = '\b403\b' },
    @{ Cat = 'throttled';               Pattern = '\b429\b' },
    @{ Cat = 'throttled';               Pattern = 'throttle' }
)

# --- OneDrive UPN extraction -----------------------------------------------
# Recover the user UPN from an OD4B URL when the caller didn't supply one.
# Example: /personal/jane_doe_uvu_edu  ->  jane.doe@uvu.edu
function _UpnFromOneDriveUrl([string]$url) {
    if (-not $url) { return '' }
    $marker = '/personal/'
    $lower = $url.ToLowerInvariant().TrimEnd('/')
    $idx = $lower.IndexOf($marker)
    if ($idx -lt 0) { return '' }
    $encoded = $lower.Substring($idx + $marker.Length).Split('/')[0]
    if ([string]::IsNullOrEmpty($encoded) -or -not $encoded.Contains('_')) { return '' }
    $firstUs = $encoded.IndexOf('_')
    $user = $encoded.Substring(0, $firstUs)
    $domain = $encoded.Substring($firstUs + 1).Replace('_', '.')
    if (-not $user -or -not $domain) { return '' }
    return "$user@$domain"
}

# --- Graph pre-check for OneDrive failures ---------------------------------
# Distinguishes three states the toolkit conflates under no_onedrive_provisioned:
#   * user_purged           - user not in /users AND not in deletedItems
#                             (deleted > 30 days ago, past Microsoft's soft-delete
#                             retention). Unrecoverable - operator should skip.
#   * user_recently_deleted - user not in /users but IS in deletedItems
#                             (recoverable within 30d). Operator decides.
#   * $null                 - user is active, fall through to toolkit
#                             (no_onedrive_provisioned, no_license, etc.).
# Best-effort: any error returns $null and the script proceeds to the toolkit.
function Get-UserExistenceState {
    param([string]$Upn)
    if (-not $Upn) { return @{ state = $null; detail = '' } }
    try {
        $u = Invoke-PnPGraphMethod -Url "v1.0/users/$Upn`?`$select=id,userPrincipalName,accountEnabled" -Method Get -ErrorAction Stop
        if ($u -and $u.id) {
            return @{ state = $null; detail = "active: enabled=$($u.accountEnabled)" }
        }
        return @{ state = $null; detail = 'graph returned no user object' }
    } catch {
        # 404 from /users -> check deletedItems before declaring purged
    }
    try {
        $localPart = ($Upn -split '@')[0]
        $escLocal = $localPart -replace "'", "''"
        $escUpn   = $Upn -replace "'", "''"
        $filter = "userPrincipalName eq '$escUpn' or startsWith(userPrincipalName,'$escLocal')"
        $resp = Invoke-PnPGraphMethod -Url ("v1.0/directory/deletedItems/microsoft.graph.user?`$filter=" + [Uri]::EscapeDataString($filter)) -Method Get -ErrorAction Stop
        $items = @($resp.value)
        if ($items.Count -gt 0) {
            $first = $items[0]
            $deleted = "$($first.deletedDateTime)"
            return @{ state = 'user_recently_deleted'; detail = "deletedDateTime=$deleted; recoverable until ~30d after deletion" }
        }
        return @{ state = 'user_purged'; detail = 'absent from /users and /directory/deletedItems' }
    } catch {
        # Permission missing or transient - don't lie, fall through to toolkit
        return @{ state = $null; detail = "deletedItems lookup failed: $($_.Exception.Message)" }
    }
}

function Classify-Text {
    param([string]$Text)
    if (-not $Text) { return 'unknown' }
    foreach ($p in $CATEGORY_PATTERNS) {
        if ($Text -match $p.Pattern) { return $p.Cat }
    }
    return 'unknown'
}

function Parse-ToolkitOutput {
    param([string[]]$Lines)
    # Returns @{ issues = @(...); summary = ''; category = '...' }
    $issues = @()
    $inIssueList = $false
    foreach ($line in $Lines) {
        if ($line -match 'Found \d+ potential blocker') { $inIssueList = $true; continue }
        if ($inIssueList) {
            if ($line -match '^\s*\[\d+\]\s*(.+)$') {
                $issues += $Matches[1].Trim()
            } elseif ($line -match '^={3,}' -or $line -match '^Suggested next actions') {
                $inIssueList = $false
            }
        }
    }
    # Pick the category from the first issue that matches; if no issues parsed,
    # try classifying the whole transcript (catches early "Get-PnPTenantSite failed").
    $cat = 'unknown'
    foreach ($issue in $issues) {
        $c = Classify-Text $issue
        if ($c -ne 'unknown') { $cat = $c; break }
    }
    if ($cat -eq 'unknown') {
        $cat = Classify-Text ($Lines -join "`n")
    }
    $summary = if ($issues.Count -gt 0) {
        ($issues | Select-Object -First 3) -join ' | '
    } else { '' }
    return @{ issues = $issues; summary = $summary; category = $cat }
}

function Truncate {
    param([string]$Text, [int]$Max)
    if (-not $Text) { return '' }
    if ($Text.Length -le $Max) { return $Text }
    return $Text.Substring(0, $Max) + "`n…[truncated, original length=$($Text.Length)]"
}

function Upsert-Diagnostic {
    param(
        [string]$Category,
        [string]$Summary,
        [string]$RawOutput,
        [int]$ExitCode
    )
    $rawCapped = Truncate -Text $RawOutput -Max 50000
    $sumCapped = Truncate -Text $Summary  -Max 800
    $sql = @"
INSERT INTO cws_failure_diagnostics
    (site_url, user_upn, site_display_name, site_type,
     failure_category, diagnostic_summary, raw_output,
     source_event_id, source_error_message, diagnostic_exit_code,
     last_diagnosed_at, updated_at)
VALUES (
    '$(_EscSql $SiteUrl)',
    '$(_EscSql $UserPrincipalName)',
    '$(_EscSql $SiteDisplayName)',
    '$(_EscSql $SiteType)',
    '$(_EscSql $Category)',
    '$(_EscSql $sumCapped)',
    '$(_EscSql $rawCapped)',
    $(if ($SourceEventId -gt 0) { $SourceEventId } else { 'NULL' }),
    '$(_EscSql (_Sanitize $SourceErrorMessage))',
    $ExitCode,
    NOW(), NOW()
)
ON CONFLICT (site_url) DO UPDATE SET
    user_upn = EXCLUDED.user_upn,
    site_display_name = CASE WHEN EXCLUDED.site_display_name <> '' THEN EXCLUDED.site_display_name ELSE cws_failure_diagnostics.site_display_name END,
    site_type = EXCLUDED.site_type,
    failure_category = EXCLUDED.failure_category,
    diagnostic_summary = EXCLUDED.diagnostic_summary,
    raw_output = EXCLUDED.raw_output,
    source_event_id = EXCLUDED.source_event_id,
    source_error_message = EXCLUDED.source_error_message,
    diagnostic_exit_code = EXCLUDED.diagnostic_exit_code,
    last_diagnosed_at = NOW(),
    updated_at = NOW();
"@
    Invoke-Psql -Sql $sql | Out-Null
}

# --- Unsupported-site-type short-circuit -----------------------------------
# SharePoint Embedded containers (/contentstorage/), legacy public sites, and
# the Content Type Hub can never be remediated via Set-PnPTenantSite. Classify
# them conclusively here and skip the (slow, OneDrive-oriented) toolkit, which
# would otherwise chase a derived UPN's license and mislabel them.
if (Test-CwsUnsupportedSiteType $SiteUrl) {
    $summary = 'Site class is not manageable via Set-PnPTenantSite -DisableCompanyWideSharingLinks (SharePoint Embedded container, legacy public site, or Content Type Hub). No remediation is possible or required via this pipeline; excluded from the CWS policy.'
    Upsert-Diagnostic -Category 'unsupported_site_type' `
        -Summary $summary `
        -RawOutput "UNSUPPORTED_SITE_TYPE: $SiteUrl" `
        -ExitCode 0
    Write-Stage "Recorded category=unsupported_site_type (skipped toolkit)"
    Stop-Transcript | Out-Null
    return
}

# --- OneDrive Graph pre-check ----------------------------------------------
# For OneDrive failures, distinguish purged vs. soft-deleted vs. unprovisioned
# before running the (slow) toolkit. Requires Connect-PnPOnline so that
# Invoke-PnPGraphMethod can authenticate against Graph.
if ($SiteType -eq 'onedrive') {
    $effectiveUpn = if ($UserPrincipalName) { $UserPrincipalName } else { _UpnFromOneDriveUrl $SiteUrl }
    if ($effectiveUpn) {
        try {
            $tenantId = $env:AZURE_TENANT_ID
            $clientId = $env:AZURE_CLIENT_ID
            $adminUrl = $env:SHAREPOINT_ADMIN_URL
            $certEnv  = $env:AZURE_CERTIFICATE
            $certPassEnv = $env:AZURE_CERTIFICATE_PASS
            if ($tenantId -and $clientId -and $adminUrl -and $certEnv -and $certPassEnv) {
                $securePass = ConvertTo-SecureString -String $certPassEnv -AsPlainText -Force
                Write-Stage "Pre-check: connecting to $adminUrl for Graph lookups..."
                Connect-PnPOnline -Url $adminUrl -ClientId $clientId -Tenant $tenantId `
                    -CertificatePath $certEnv -CertificatePassword $securePass `
                    -WarningAction SilentlyContinue -ErrorAction Stop | Out-Null

                Write-Stage "Pre-check: Graph lookup for $effectiveUpn..."
                $state = Get-UserExistenceState -Upn $effectiveUpn
                Write-Stage "Pre-check: state=$($state.state) detail=$($state.detail)"

                if ($state.state -eq 'user_purged' -or $state.state -eq 'user_recently_deleted') {
                    $summary = if ($state.state -eq 'user_purged') {
                        "User $effectiveUpn absent from Entra (not in /users, not in /directory/deletedItems). Past 30d soft-delete retention - unrecoverable. OneDrive site no longer exists."
                    } else {
                        "User $effectiveUpn is soft-deleted in Entra ($($state.detail)). Recoverable within 30d via Restore-MgDirectoryDeletedItem; if not recovered, will progress to user_purged."
                    }
                    $raw = "===== GRAPH PRE-CHECK =====`nUPN: $effectiveUpn`nState: $($state.state)`nDetail: $($state.detail)`n(toolkit not invoked - pre-check is conclusive)"
                    Upsert-Diagnostic -Category $state.state -Summary $summary -RawOutput $raw -ExitCode 0
                    Write-Stage "Recorded category=$($state.state) (skipped toolkit)"
                    try { Disconnect-PnPOnline -ErrorAction SilentlyContinue | Out-Null } catch {}
                    Stop-Transcript | Out-Null
                    return
                }
                # state=$null means active or pre-check inconclusive -> fall through to toolkit
                try { Disconnect-PnPOnline -ErrorAction SilentlyContinue | Out-Null } catch {}
            } else {
                Write-Stage "Pre-check: skipped (PnP credentials env vars not all set)" 'WARN'
            }
        } catch {
            Write-Stage "Pre-check failed (non-fatal, will run toolkit): $($_.Exception.Message)" 'WARN'
            try { Disconnect-PnPOnline -ErrorAction SilentlyContinue | Out-Null } catch {}
        }
    }
}

# --- Toolkit availability check --------------------------------------------
$toolkitScript = Join-Path $ToolkitPath 'Diagnose-OneDriveSite.ps1'
if (-not (Test-Path $toolkitScript)) {
    Write-Stage "Toolkit script not found at $toolkitScript" 'WARN'
    # Best-effort classification from upstream error alone.
    $cat = Classify-Text $SourceErrorMessage
    if ($cat -eq 'unknown') { $cat = 'diagnostic_unavailable' }
    Upsert-Diagnostic -Category $cat `
        -Summary "Toolkit not mounted; categorised from upstream error: $(_Sanitize $SourceErrorMessage)" `
        -RawOutput "TOOLKIT_NOT_FOUND: $toolkitScript" `
        -ExitCode 0
    Write-Stage "Recorded category=$cat (toolkit unavailable)"
    Stop-Transcript | Out-Null
    return
}

# --- Invoke the toolkit ----------------------------------------------------
$tkArgs = @('-NoProfile', '-File', $toolkitScript)
if ($UserPrincipalName) {
    $tkArgs += @('-UserPrincipalName', $UserPrincipalName)
} else {
    $tkArgs += @('-SiteUrl', $SiteUrl)
}

Write-Stage "Launching toolkit diagnostic..."
$proc = Start-Process -FilePath 'pwsh' -ArgumentList $tkArgs `
    -NoNewWindow -Wait -PassThru `
    -RedirectStandardOutput "$logFile.stdout" `
    -RedirectStandardError  "$logFile.stderr"
$exit = $proc.ExitCode

$stdoutText = if (Test-Path "$logFile.stdout") { Get-Content "$logFile.stdout" -Raw } else { '' }
$stderrText = if (Test-Path "$logFile.stderr") { Get-Content "$logFile.stderr" -Raw } else { '' }
$combined = "===== STDOUT =====`n$stdoutText`n===== STDERR =====`n$stderrText"

Write-Stage "Toolkit exited rc=$exit"

# --- Parse + classify ------------------------------------------------------
$parsed = Parse-ToolkitOutput -Lines ($stdoutText -split "`r?`n")
$category = $parsed.category
$summary = $parsed.summary

if ($exit -ne 0 -and $category -eq 'unknown') {
    # Toolkit crashed before producing structured output. Use stderr / upstream.
    $category = Classify-Text $combined
    if ($category -eq 'unknown') { $category = Classify-Text $SourceErrorMessage }
    if ($category -eq 'unknown') { $category = 'diagnostic_error' }
    if (-not $summary) {
        $firstStderrLine = ($stderrText -split "`r?`n" | Where-Object { $_ -and $_.Trim() } | Select-Object -First 1)
        $summary = if ($firstStderrLine) {
            "Diagnostic script exited rc=${exit}: $(_Sanitize $firstStderrLine)"
        } else {
            "Diagnostic script exited rc=${exit} with no parseable issues"
        }
    }
} elseif ($category -eq 'unknown' -and -not $summary) {
    $summary = 'No blockers detected by diagnostic — Set-PnPTenantSite should normally succeed. Re-attempt remediation.'
}

Write-Stage "Category : $category"
Write-Stage "Summary  : $summary"

Upsert-Diagnostic -Category $category -Summary $summary -RawOutput $combined -ExitCode $exit
Write-Stage "Diagnostic row upserted for $SiteUrl"

# Cleanup transient stdout/stderr files; the transcript log is enough.
Remove-Item "$logFile.stdout" -ErrorAction SilentlyContinue
Remove-Item "$logFile.stderr" -ErrorAction SilentlyContinue

Stop-Transcript | Out-Null
