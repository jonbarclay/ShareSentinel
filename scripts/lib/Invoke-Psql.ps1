<#
.SYNOPSIS
    Dual-mode psql helper for ShareSentinel PowerShell scripts.

.DESCRIPTION
    Dot-source this file to get an Invoke-Psql function that works in two modes:

      - In-container (when $env:IN_CONTAINER='true'):
            Calls psql directly. Requires POSTGRES_HOST, POSTGRES_PORT (default 5432),
            POSTGRES_USER, POSTGRES_DB, POSTGRES_PASSWORD env vars.

      - Host (default):
            Calls `docker exec sharesentinel-postgres psql` — the historical pattern
            used by the bulk + retry scripts when run from the Docker host.

    Also exports _EscSql (single-quote escape) and _Sanitize (Bearer-token redaction
    + length cap) so callers do not have to redefine them.

.NOTES
    Dot-source: . (Join-Path $PSScriptRoot 'lib/Invoke-Psql.ps1')
#>

function Invoke-Psql {
    param(
        [Parameter(Mandatory)][string]$Sql,
        [switch]$TupleOnly
    )
    $inContainer = ($env:IN_CONTAINER -eq 'true')

    if ($inContainer) {
        $pgHost = if ($env:POSTGRES_HOST) { $env:POSTGRES_HOST } else { 'postgres' }
        $pgPort = if ($env:POSTGRES_PORT) { $env:POSTGRES_PORT } else { '5432' }
        $pgUser = if ($env:POSTGRES_USER) { $env:POSTGRES_USER } else { 'sharesentinel' }
        $pgDb   = if ($env:POSTGRES_DB)   { $env:POSTGRES_DB }   else { 'sharesentinel' }
        $pgPass = $env:POSTGRES_PASSWORD
        if (-not $pgPass) { throw "Invoke-Psql: POSTGRES_PASSWORD env var required in container mode" }

        $args = @('-h', $pgHost, '-p', $pgPort, '-U', $pgUser, '-d', $pgDb, '-v', 'ON_ERROR_STOP=1')
        if ($TupleOnly) { $args += @('-tA') }
        $args += @('-c', $Sql)

        $prevPgPass = $env:PGPASSWORD
        try {
            $env:PGPASSWORD = $pgPass
            $out = & psql @args 2>&1
            if ($LASTEXITCODE -ne 0) { throw "psql failed (exit $LASTEXITCODE): $out" }
            return $out
        } finally {
            $env:PGPASSWORD = $prevPgPass
        }
    }

    $cmd = @('exec', '-i', 'sharesentinel-postgres', 'psql', '-U', 'sharesentinel', '-d', 'sharesentinel', '-v', 'ON_ERROR_STOP=1')
    if ($TupleOnly) { $cmd += @('-tA') }
    $cmd += @('-c', $Sql)
    $out = & docker @cmd 2>&1
    if ($LASTEXITCODE -ne 0) { throw "psql failed (exit $LASTEXITCODE): $out" }
    return $out
}

function _EscSql {
    param([string]$s)
    if ($null -eq $s) { return '' }
    return $s.Replace("'", "''")
}

function _Sanitize {
    param([string]$msg)
    if (-not $msg) { return $null }
    $clean = $msg -replace 'Bearer [A-Za-z0-9\-._~+/]+=*', 'Bearer [REDACTED]'
    if ($clean.Length -gt 500) { $clean = $clean.Substring(0, 500) }
    return $clean
}

function Resolve-CwsLogDir {
    <#
    .SYNOPSIS
        Return a writable log directory for CWS scripts.
        In the lifecycle-cron container, /app/scripts is mounted read-only,
        so we redirect logs to /tmp/cws_logs. On the host, use the standard
        scripts/logs path next to the script.
    #>
    param([Parameter(Mandatory)][string]$ScriptRoot)
    if ($env:IN_CONTAINER -eq 'true') {
        $dir = '/tmp/cws_logs'
    } else {
        $dir = Join-Path $ScriptRoot 'logs'
    }
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    return $dir
}

function Load-DotEnv {
    <#
    .SYNOPSIS
        Load KEY=VALUE pairs from a .env file into the current process's
        environment variables. Optional — when run inside the lifecycle-cron
        container, env vars are already set by docker-compose and the .env
        file is not mounted; this helper logs a warning and returns.
    #>
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path $Path)) {
        Write-Host "[WARN] .env file not found at $Path -- relying on inherited environment variables"
        return
    }
    Get-Content $Path | ForEach-Object {
        if ($_ -match '^\s*#' -or [string]::IsNullOrWhiteSpace($_)) { return }
        $parts = $_ -split '=', 2
        if ($parts.Count -eq 2) {
            [System.Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim())
        }
    }
}
