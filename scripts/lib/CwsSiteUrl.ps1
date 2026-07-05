<#
.SYNOPSIS
    Shared SharePoint/OneDrive site-URL helpers for the CWS PowerShell scripts.

.DESCRIPTION
    Dot-source this file to get two functions used everywhere the CWS pipeline
    decides whether a URL is a remediable site collection:

      - Test-CwsUnsupportedSiteType : True for URL classes that look like sites
            but cannot be managed via
            Set-PnPTenantSite -DisableCompanyWideSharingLinks.
      - Get-CwsSiteCollectionRoot   : Trim any subsite / folder / library path
            down to the site-collection root that Set-PnPTenantSite accepts.

    These are the PowerShell twins of the Python helpers in
    services/worker/app/pipeline/cws_drift_detector.py
    (_is_unsupported_site_type and _site_collection_root). Keep the two sides in
    sync — the audit-event path (Python) and the scan/remediation path
    (PowerShell) must agree on what is unsupported and what the canonical root
    is, or sites will bounce between "drift" and "failed".

.NOTES
    Dot-source: . (Join-Path $PSScriptRoot 'lib/CwsSiteUrl.ps1')
#>

# SharePoint managed paths: a site-collection URL has exactly one path segment
# after the managed path (e.g. /sites/Foo). Anything deeper is a subsite,
# folder, or library and must be trimmed to the root before remediation.
$script:CwsManagedPaths = @('sites', 'teams', 'personal')

function Test-CwsUnsupportedSiteType {
    <#
    .SYNOPSIS
        Return $true for site classes that can't be managed via
        Set-PnPTenantSite -DisableCompanyWideSharingLinks.

        * /contentstorage/* — SharePoint Embedded containers (Loop, Whiteboard,
            Designer, Copilot, Teams private/shared channels). PnP returns
            "The requested operation is not supported for site".
        * *-public.sharepoint.com — legacy SharePoint Online Public Website,
            deprecated by Microsoft in 2018. PnP returns
            "Attempted to perform an unauthorized operation".
        * /sites/contenttypehub — Content Type Hub system site. PnP returns
            "The requested operation is not supported for site".
        * /portals/* — PointPublishing site collections. PnP returns
            "... is a PointPublishing site collection. The only valid
            parameters for this type ...".
        * MySite host root — PnP returns "The property
            DisableCompanyWideSharingLinks cannot be set on the MySite host."
            (Personal sites UNDER the host are fine.)
    #>
    param([string]$Url)
    if ([string]::IsNullOrWhiteSpace($Url)) { return $false }
    $l = $Url.ToLowerInvariant().TrimEnd('/')
    if ($l.Contains('/contentstorage/')) { return $true }
    if ($l.Contains('/portals/')) { return $true }
    if ($l.EndsWith('/sites/contenttypehub')) { return $true }
    if ($l -match '://[^/]*-public\.sharepoint\.com') { return $true }
    if ($l -match '://[^/]*-my\.sharepoint\.com$') { return $true }
    return $false
}

function ConvertTo-CwsPersonalSegment {
    <#
    .SYNOPSIS
        Forward-encode a UPN into its OneDrive personal-site path segment,
        the way SharePoint does (jane.doe@uvu.edu -> jane_doe_uvu_edu).

    .DESCRIPTION
        Exemption matching MUST use this direction. Reversing a path segment
        into a UPN is ambiguous — splitting jane_doe_uvu_edu at the first
        underscore yields jane@doe.uvu.edu, so any exception-group member with
        a dot (or underscore/extra separator) in their UPN local part would
        silently LOSE their exemption and have their OneDrive remediated.
        PowerShell twin of provisioning_enforcer._upn_to_personal_segment.
    #>
    param([string]$Upn)
    if ([string]::IsNullOrWhiteSpace($Upn)) { return '' }
    return ($Upn.Trim().ToLowerInvariant() -replace '[^a-z0-9\-]', '_')
}

function Get-CwsPersonalSegment {
    <#
    .SYNOPSIS
        Extract the personal-site path segment from a OneDrive URL
        (https://x-my.sharepoint.com/personal/jane_doe_uvu_edu/... ->
        jane_doe_uvu_edu). Empty string for non-personal URLs.
    #>
    param([string]$Url)
    if ([string]::IsNullOrWhiteSpace($Url)) { return '' }
    $l = $Url.ToLowerInvariant().TrimEnd('/')
    $marker = '/personal/'
    $idx = $l.IndexOf($marker)
    if ($idx -lt 0) { return '' }
    return $l.Substring($idx + $marker.Length).Split('/')[0]
}

function Get-CwsSiteCollectionRoot {
    <#
    .SYNOPSIS
        Return the site-collection root URL for any SharePoint/OneDrive URL.

    .EXAMPLE
        /sites/Foo/Marketing/Shared Documents  ->  /sites/Foo
        /personal/jane_uvu_edu/Documents/x.docx ->  /personal/jane_uvu_edu
        /sites/Foo                              ->  /sites/Foo  (unchanged)
        https://host/                           ->  https://host  (root web)
    #>
    param([string]$Url)
    if ([string]::IsNullOrWhiteSpace($Url)) { return $Url }
    $trimmed = $Url.TrimEnd('/')
    try {
        $uri = [System.Uri]$trimmed
    } catch {
        return $trimmed
    }
    $segments = @($uri.AbsolutePath.Split('/') | Where-Object { $_ -ne '' })
    if ($segments.Count -ge 2 -and ($script:CwsManagedPaths -contains $segments[0].ToLowerInvariant())) {
        return '{0}://{1}/{2}/{3}' -f $uri.Scheme, $uri.Authority, $segments[0], $segments[1]
    }
    return $trimmed
}
