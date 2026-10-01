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
    [int]$StartupTimeoutSeconds = 90,

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
    <#  Liveness probe against the app's own /api/health endpoint. #>
    param([int]$TimeoutSec = 3)
    try {
        $response = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing `
                                     -TimeoutSec $TimeoutSec -ErrorAction Stop
        return ($response.StatusCode -eq 200 -and $response.Content -match '"status"\s*:\s*"ok"')
    } catch {
        return $false
    }
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

function Wait-DashboardHealthy {
    param([int]$TimeoutSeconds = 90)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-DashboardHealthy -TimeoutSec 2) { return $true }
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
    if (Test-DashboardHealthy -TimeoutSec 2) {
        Write-Log "Dashboard already healthy on http://127.0.0.1:$Port - nothing to do."
        return $true
    }

    # Something holds the port but is not answering yet.
    $owner = Get-PortOwner
    if ($owner) {
        Write-Log "Port $Port is in use by PID $owner but /api/health is not responding." 'WARN'
        if (Wait-DashboardHealthy -TimeoutSeconds 20) {
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
    $healthy = Test-DashboardHealthy -TimeoutSec 3
    $owner = Get-PortOwner
    $recorded = Get-RecordedPid

    if ($healthy) {
        Write-Host "Dashboard : UP" -ForegroundColor Green
    } elseif ($owner) {
        Write-Host "Dashboard : port $Port held by PID $owner but /api/health is not answering" -ForegroundColor Yellow
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

    if ($healthy) {
        try {
            $health = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 5 -ErrorAction Stop
            Write-Host ("Mode      : {0} (llm_enabled={1})" -f $health.analysis_mode, $health.llm_enabled)
        } catch {
            Write-Host 'Mode      : unknown (health payload unreadable)'
        }
    }
    return $healthy
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
            if (Test-DashboardHealthy -TimeoutSec 5) {
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
