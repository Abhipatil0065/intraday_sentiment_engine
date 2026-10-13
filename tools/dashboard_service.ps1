<#
.SYNOPSIS
    Start / stop / supervise the intraday sentiment dashboard (port 8000).

.DESCRIPTION
    The dashboard is a plain console process ("python run.py --api"), so it dies
    on every reboot and the browser's restored tab hits ERR_CONNECTION_REFUSED.
    This script turns it into a managed service:

      Start    start it, but only if it is not already healthy (safe to re-run)
      Stop     stop it and release the port
      Restart  stop, then start
      Status   report health / PID / port state
      Watch    supervisor loop - restarts the server whenever it dies.
               This is what the scheduled task runs at logon.

    Everything is logged to logs\service.log.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\tools\dashboard_service.ps1 -Action Status

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\tools\dashboard_service.ps1 -Action Start

.NOTES
    Compatible with Windows PowerShell 5.1 and PowerShell 7+.
    Keep -Port in sync with API_PORT in .env (default 8000).
#>
[CmdletBinding()]
param(
    [ValidateSet('Start', 'Stop', 'Restart', 'Status', 'Watch')]
    [string]$Action = 'Start',

    # Must match API_PORT in .env
    [int]$Port = 8000,

    # How long Start waits for /api/health before reporting failure.
    # 300s because the first pipeline tick primes the database with a full
    # news fetch + calendar render + sentiment recompute, which can take
    # 1-2 minutes when the LLM endpoint is degraded.
    [int]$StartupTimeoutSeconds = 300,

    # Seconds between health checks while supervising.
    [int]$WatchIntervalSeconds = 30,

    # Suppress console output (the scheduled task uses this).
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'

# --------------------------------------------------------------------------
# Paths - everything is derived from the project root so the script works no
# matter which directory the scheduler launches it from.
# --------------------------------------------------------------------------
$ProjectDir = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $ProjectDir 'logs'
$ServiceLog = Join-Path $LogDir 'service.log'
$PidFile = Join-Path $LogDir 'dashboard.pid'
# Single-instance lock. Two watchdogs can be running at once (the scheduled
# task and a manual install_autostart run), and without this guard they race:
# both see the port empty, both Start-Process, and the second instance either
# fights for the port or silently shadows the first. The lock is created
# exclusively, so only ONE caller may launch at a time.
$LockFile = Join-Path $LogDir 'dashboard.lock'
$StdOutLog = Join-Path $LogDir 'api_stdout.log'
$StdErrLog = Join-Path $LogDir 'api_stderr.log'
$HealthUrl = "http://127.0.0.1:$Port/api/health"
$MaxLogBytes = 10MB

if (-not (Test-Path -LiteralPath $ProjectDir)) {
    throw "Project directory not found: $ProjectDir"
}
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
function Write-Log {
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [ValidateSet('INFO', 'OK', 'WARN', 'ERROR')][string]$Level = 'INFO'
    )
    $line = '{0} | {1,-5} | dashboard_service | {2}' -f `
        (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    try {
        Add-Content -LiteralPath $ServiceLog -Value $line -Encoding UTF8
    } catch {
        # Never fail because of logging.
    }
    if (-not $Quiet) {
        Write-Host $line
    }
}

function Get-PythonInterpreter {
    <#
        Prefer a project virtualenv, fall back to the machine Python.
        Resolved at run time so the task survives a Python upgrade.
    #>
    $candidates = @(
        (Join-Path $ProjectDir '.venv\Scripts\python.exe'),
        (Join-Path $ProjectDir 'venv\Scripts\python.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }

    foreach ($name in @('python.exe', 'python3.exe', 'py.exe')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue |
               Where-Object { $_.Source -and $_.Source -notlike '*WindowsApps*python3.exe' } |
               Select-Object -First 1
        if ($cmd) { return $cmd.Source }
    }

    throw 'No Python interpreter found. Install Python 3.10+ or create .venv in the project.'
}

function Test-DashboardHealthy {
    <#  Liveness probe against the app's own /api/health endpoint.

          Returns $true when the server answers - even with HTTP 503 "degraded"
          - because that means the process is alive and the pipeline simply has
          no successful tick yet (e.g. still priming on startup). The watchdog
          only restarts when the server CANNOT answer at all (connection
          refused), never when the pipeline is merely slow.

          Pass -Strict to also require status:ok (used by Status reporting). #>
    param(
        [int]$TimeoutSec = 3,
        [switch]$Strict
    )
    try {
        $response = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing `
                                     -TimeoutSec $TimeoutSec -ErrorAction Stop
        if ($Strict) {
            return ($response.StatusCode -eq 200 -and
                    $response.Content -match '"status"\s*:\s*"ok"')
        }
        # Any HTTP response means the server is alive.
        return $true
    } catch {
        return $false
    }
}

function Test-DashboardHealthyStrict {
    <#  True only when /api/health returns 200 with status:ok. #>
    param([int]$TimeoutSec = 3)
    return Test-DashboardHealthy -TimeoutSec $TimeoutSec -Strict
}

function Get-PortOwner {
    <#  PID of the process listening on the port, or $null. #>
    param([int]$TargetPort = $Port)
    try {
        $conn = Get-NetTCPConnection -LocalPort $TargetPort -State Listen `
                                     -ErrorAction Stop | Select-Object -First 1
        if ($conn) { return [int]$conn.OwningProcess }
    } catch {
        # No listener, or the cmdlet is unavailable.
    }
    return $null
}

function Get-RecordedPid {
    <#  PID written by the last successful launch, if that process is alive. #>
    if (-not (Test-Path -LiteralPath $PidFile)) { return $null }
    try {
        $raw = (Get-Content -LiteralPath $PidFile -Raw).Trim()
        if ($raw -eq '') { return $null }
        $candidate = [int]$raw
        if (Get-Process -Id $candidate -ErrorAction SilentlyContinue) { return $candidate }
    } catch {
        # Unreadable PID file - fall through to the port lookup.
    }
    return $null
}

function Test-IsEngineProcess {
    <#  True when the PID is one of our own uvicorn / run.py servers. #>
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    try {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
    } catch {
        return $false
    }
    if (-not $proc) { return $false }
    $cmd = [string]$proc.CommandLine
    if ($cmd -eq '') { return $false }
    return (($cmd -like '*run.py*--api*') -or ($cmd -like '*app.main:app*'))
}

function Get-LockOwner {
    <#  PID that currently holds the launch lock, or $null.

          The lock file is written with CreateNew semantics, so the writer's
          PID is the only thing ever in it - no other process can have written
          a number there. The holder is the watchdog process that is launching,
          so a PID that is no longer alive means the lock holder died and the
          lock is stale (otherwise a dead watchdog would block every restart
          forever). #>
    if (-not (Test-Path -LiteralPath $LockFile)) { return $null }
    try {
        $raw = (Get-Content -LiteralPath $LockFile -Raw).Trim()
        if ($raw -eq '') { return $null }
        $candidate = [int]$raw
        if (Get-Process -Id $candidate -ErrorAction SilentlyContinue) {
            return $candidate
        }
    } catch {
        # Unreadable lock - treat as stale.
    }
    return $null
}

function Test-LockHeld {
    <#  True when another watchdog is currently launching the server. #>
    return (Get-LockOwner) -ne $null
}

function Acquire-Lock {
    <#  Take the launch lock. Returns $true on success, $false if it is held. #>
    try {
        $stream = [System.IO.File]::Open(
            $LockFile,
            [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
        try {
            $bytes = [System.Text.Encoding]::ASCII.GetBytes($PID.ToString())
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush()
        } finally {
            $stream.Close()
        }
        return $true
    } catch [System.IO.IOException] {
        # File exists and is open by another process - someone else is launching.
        return $false
    } catch {
        return $false
    }
}

function Release-Lock {
    try {
        if (Test-Path -LiteralPath $LockFile) {
            Remove-Item -LiteralPath $LockFile -Force -ErrorAction SilentlyContinue
        }
    } catch {
        # Best effort; never fail the caller because of lock cleanup.
    }
}

function Wait-DashboardHealthy {
    param([int]$TimeoutSeconds = 300)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        # During startup the pipeline is priming, so /api/health may return
        # 503 "degraded" for the first minute or two. Any HTTP response means
        # the server is up and serving; only a connection failure blocks.
        if (Test-DashboardHealthy -TimeoutSec 3) { return $true }
        Start-Sleep -Milliseconds 750
    }
    return $false
}

function Clear-VeryLargeLog {
    param([string]$Path)
    if ((Test-Path -LiteralPath $Path) -and (Get-Item -LiteralPath $Path).Length -gt $MaxLogBytes) {
        Move-Item -LiteralPath $Path -Destination "$Path.1" -Force
    }
}
# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------
function Start-Dashboard {
    # A live engine already answering /api/health means the port is owned, so
    # every watchdog must bail out immediately. This check runs BEFORE the
    # lock: the lock only serialises launchers, it does not hide a running
    # server from a second watchdog that skips the health check.
    if (Test-DashboardHealthy -TimeoutSec 2) {
        Write-Log "Dashboard already healthy on http://127.0.0.1:$Port - nothing to do."
        return $true
    }

    # --- SINGLE-INSTANCE GUARD -----------------------------------------------
    # Only one watchdog may launch at a time. Two watchdogs running concurrently
    # (the scheduled task + a manual install_autostart run) both see the port
    # empty and both Start-Process, which is what produced the duplicate
    # 12392/22788 and 4152/18508 pairs in service.log. The lock is exclusive,
    # so the second caller waits instead of racing.
    if (Test-LockHeld) {
        $holder = Get-LockOwner
        Write-Log "Another watchdog is already launching the dashboard (PID $holder) - deferring." 'INFO'
        return $true
    }
    if (-not (Acquire-Lock)) {
        Write-Log "Could not acquire launch lock - another watchdog is starting the dashboard." 'WARN'
        return $true
    }
    try {
        # Re-check after acquiring the lock: the port may have been claimed
        # by the watchdog that won the race while we were waiting for it.
        if (Test-DashboardHealthy -TimeoutSec 2) {
            Write-Log "Dashboard became healthy while waiting for the launch lock - nothing to do."
            return $true
        }
        _Start-Dashboard
    } finally {
        Release-Lock
    }
}

function _Start-Dashboard {
    if (Test-DashboardHealthy -TimeoutSec 2) {
        Write-Log "Dashboard already healthy on http://127.0.0.1:$Port - nothing to do."
        return $true
    }

    # Something holds the port but is not answering yet.
    $owner = Get-PortOwner
    if ($owner) {
        Write-Log "Port $Port is in use by PID $owner but /api/health is not responding." 'WARN'
        # Give the busy engine a long grace before declaring it dead: a tick
        # (23 sources, LLM retries) plus the boot morning brief can keep the
        # event loop busy for several minutes. Killing a healthy-but-slow
        # engine mid-tick resets its in-memory tick state, so the dashboard
        # then reads "NO TICK" on every restart forever. Only a genuinely
        # hung process (no response at all for this long) gets replaced.
        if (Wait-DashboardHealthy -TimeoutSeconds 300) {
            Write-Log "Existing instance became healthy (PID $owner)." 'OK'
            return $true
        }
        if (Test-IsEngineProcess -ProcessId $owner) {
            Write-Log "Replacing unhealthy engine PID $owner." 'WARN'
            Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
        } else {
            Write-Log "Port $Port is held by an unrelated process (PID $owner). Free the port or change API_PORT in .env." 'ERROR'
            return $false
        }
    }

    try {
        $python = Get-PythonInterpreter
    } catch {
        Write-Log $_.Exception.Message 'ERROR'
        return $false
    }

    Clear-VeryLargeLog -Path $StdOutLog
    Clear-VeryLargeLog -Path $StdErrLog

    Write-Log "Launching: $python run.py --api  (cwd: $ProjectDir)"
    try {
        $proc = Start-Process -FilePath $python `
                              -ArgumentList @('run.py', '--api') `
                              -WorkingDirectory $ProjectDir `
                              -WindowStyle Hidden `
                              -RedirectStandardOutput $StdOutLog `
                              -RedirectStandardError $StdErrLog `
                              -PassThru `
                              -ErrorAction Stop
    } catch {
        Write-Log "Could not launch Python: $($_.Exception.Message)" 'ERROR'
        return $false
    }

    Set-Content -LiteralPath $PidFile -Value $proc.Id -Encoding ASCII
    Write-Log "Started PID $($proc.Id); waiting for $HealthUrl ..."

    if (Wait-DashboardHealthy -TimeoutSeconds $StartupTimeoutSeconds) {
        Write-Log "Dashboard is UP at http://127.0.0.1:$Port (PID $($proc.Id))." 'OK'
        return $true
    }

    Write-Log "No healthy response within $StartupTimeoutSeconds s. See $StdErrLog" 'ERROR'
    return $false
}

function Stop-Dashboard {
    $targets = New-Object 'System.Collections.Generic.List[int]'
    $recorded = Get-RecordedPid
    if ($recorded) { [void]$targets.Add($recorded) }

    $owner = Get-PortOwner
    if ($owner -and -not $targets.Contains($owner)) { [void]$targets.Add($owner) }

    # Any other dashboard server started manually from this project.
    $servers = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -match '^python(w)?\.exe$' -and [string]$_.CommandLine -like '*run.py*--api*'
        }
    foreach ($proc in $servers) {
        $procId = [int]$proc.ProcessId
        if (-not $targets.Contains($procId)) { [void]$targets.Add($procId) }
    }

    if ($targets.Count -eq 0) {
        Write-Log "Nothing to stop - port $Port is free."
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $LockFile -Force -ErrorAction SilentlyContinue
        return
    }

    foreach ($procId in $targets) {
        try {
            Stop-Process -Id $procId -Force -ErrorAction Stop
            Write-Log "Stopped PID $procId." 'INFO'
        } catch {
            Write-Log "PID $procId already gone." 'INFO'
        }
    }

    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $LockFile -Force -ErrorAction SilentlyContinue

    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-PortOwner)) {
            Write-Log "Port $Port released." 'OK'
            return
        }
        Start-Sleep -Milliseconds 500
    }
    Write-Log "Port $Port is still in use after stopping the server." 'WARN'
}
function Show-Status {
    $alive = Test-DashboardHealthy -TimeoutSec 3
    $healthy = Test-DashboardHealthyStrict -TimeoutSec 3
    $owner = Get-PortOwner
    $recorded = Get-RecordedPid

    if ($alive) {
        if ($healthy) {
            Write-Host "Dashboard : UP" -ForegroundColor Green
        } else {
            Write-Host "Dashboard : UP (degraded - pipeline priming or stale)" -ForegroundColor Yellow
        }
    } elseif ($owner) {
        Write-Host "Dashboard : port $Port held by PID $owner but /api/health is not responding" -ForegroundColor Yellow
    } else {
        Write-Host "Dashboard : STOPPED (nothing listening on port $Port)" -ForegroundColor Red
    }
    Write-Host "URL       : http://127.0.0.1:$Port/"
    Write-Host "Health    : $HealthUrl"
    if ($recorded) {
        Write-Host "PID file  : $PidFile (PID $recorded)"
    } else {
        Write-Host "PID file  : $PidFile (no live PID recorded)"
    }
    Write-Host "Logs      : $ServiceLog"
    Write-Host "             $StdOutLog"
    Write-Host "             $StdErrLog"

    if ($alive) {
        try {
            $health = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 5 -ErrorAction Stop
            $mode = $health.analysis_mode
            $llm = $health.llm_enabled
            $last = $health.last_poll_ok
            $stale = if ($health.PSObject -and $health.stale) { " [STALE]" } else { "" }
            Write-Host ("Mode      : {0} (llm_enabled={1}) last_poll={2}{3}" -f $mode, $llm, $last, $stale)
        } catch {
            Write-Host 'Mode      : unknown (health payload unreadable)'
        }
    }
    return $alive
}

function Watch-Dashboard {
    <#
        Long-running supervisor used by the scheduled task:
          - brings the dashboard up if it is down
          - restarts it if it ever crashes
          - never touches a port owned by an unrelated program
    #>
    Write-Log "Watchdog started (health check every $WatchIntervalSeconds s)."
    $reportedDown = $false

    while ($true) {
        try {
                # Non-strict: any HTTP response (even 503 degraded) means the
                # process is alive. Only a connection failure triggers a restart.
                # Long HTTP timeout on purpose: while a pipeline tick plus the
                # boot morning brief are running, /api/health can be slow for
                # minutes. The engine is alive and mid-work, not dead - a
                # short timeout here made the watchdog kill it every couple of
                # minutes, which is what kept the dashboard at "NO TICK".
                if (Test-DashboardHealthy -TimeoutSec 300) {
                if ($reportedDown) {
                    Write-Log 'Dashboard is healthy again.' 'OK'
                    $reportedDown = $false
                }
            } else {
                $owner = Get-PortOwner
                if ($owner -and -not (Test-IsEngineProcess -ProcessId $owner)) {
                    if (-not $reportedDown) {
                        Write-Log "Port $Port is held by unrelated PID $owner - leaving it alone." 'WARN'
                        $reportedDown = $true
                    }
                } else {
                    Write-Log 'Dashboard is not responding - restarting it.' 'WARN'
                    $reportedDown = $true
                    [void](Start-Dashboard)
                }
            }
        } catch {
            Write-Log "Watchdog error: $($_.Exception.Message)" 'ERROR'
        }
        Start-Sleep -Seconds $WatchIntervalSeconds
    }
}

# --------------------------------------------------------------------------
# Rotate our own log once per launch, then dispatch.
# --------------------------------------------------------------------------
Clear-VeryLargeLog -Path $ServiceLog

switch ($Action) {
    'Start'   { if (Start-Dashboard) { exit 0 } else { exit 1 } }
    'Stop'    { Stop-Dashboard; exit 0 }
    'Restart' { Stop-Dashboard; if (Start-Dashboard) { exit 0 } else { exit 1 } }
    'Status'  { if (Show-Status) { exit 0 } else { exit 1 } }
    'Watch'   { Watch-Dashboard }
}
