<#
.SYNOPSIS
    Install (or remove) the Windows scheduled task that keeps the dashboard up.

.DESCRIPTION
    Registers a per-user task "IntradaySentimentDashboard" that runs
    tools\dashboard_service.ps1 -Action Watch:

      * at every logon  -> the dashboard is already serving before you open the
                            browser, so the restored 127.0.0.1:8000 tab loads
                            normally instead of ERR_CONNECTION_REFUSED
      * every N minutes -> watchdog re-launches the server if it ever dies
      * restart on failure, no execution time limit, runs on battery power

    Runs hidden in the background as the current user. No admin rights needed.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\tools\install_autostart.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\tools\install_autostart.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    # Removes the task instead of installing it.
    [switch]$Uninstall,

    [string]$TaskName = 'IntradaySentimentDashboard',

    # Watchdog re-check interval in minutes.
    [ValidateRange(1, 60)]
    [int]$WatchdogMinutes = 5,

    # Extra delay after logon, before the first start (gives Windows time to
    # finish loading the profile/network). Raise it if you log on very fast.
    [int]$LogonDelaySeconds = 5
)

$ErrorActionPreference = 'Stop'

$ProjectDir = Split-Path -Parent $PSScriptRoot
$ServiceScript = Join-Path $PSScriptRoot 'dashboard_service.ps1'

if (-not (Test-Path -LiteralPath $ServiceScript)) {
    throw "Service script not found: $ServiceScript"
}

# --------------------------------------------------------------------------
# Uninstall
# --------------------------------------------------------------------------
if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'." -ForegroundColor Green
        Write-Host 'The dashboard will no longer start automatically.'
    } else {
        Write-Host "Scheduled task '$TaskName' is not installed." -ForegroundColor Yellow
    }
    return
}

# --------------------------------------------------------------------------
# Install
# --------------------------------------------------------------------------
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden ' +
             "-File `"$ServiceScript`" -Action Watch -Quiet"

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument $arguments `
    -WorkingDirectory $ProjectDir

# 1) At logon - the main fix for the restored browser tab.
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
if ($LogonDelaySeconds -gt 0) {
    $logonTrigger.Delay = "PT${LogonDelaySeconds}S"
}

# 2) Periodic watchdog - recovers from crashes even after logon.
$watchdogTrigger = New-ScheduledTaskTrigger `
    -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $WatchdogMinutes)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

# Interactive logon type = runs as the signed-in user, no stored password.
$principal = New-ScheduledTaskPrincipal `
    -UserId $userId `
    -LogonType Interactive `
    -RunLevel Limited

$description = 'Keeps the intraday sentiment dashboard (127.0.0.1:8000) running ' +
              'after logon so the browser tab never shows ERR_CONNECTION_REFUSED.'

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger @($logonTrigger, $watchdogTrigger) `
    -Settings $settings `
    -Principal $principal `
    -Description $description `
    -Force | Out-Null

Write-Host "Installed scheduled task '$TaskName'." -ForegroundColor Green
Write-Host "  Trigger 1 : at logon as $userId (after ${LogonDelaySeconds}s)"
Write-Host "  Trigger 2 : every $WatchdogMinutes minute(s)"
Write-Host "  Runs     : powershell.exe -File `"$ServiceScript`" -Action Watch -Quiet"
Write-Host '  Hidden window, no admin rights, survives crashes.'

# Bring it up right now instead of waiting for the next logon.
Write-Host ''
Write-Host 'Starting the dashboard now...' -ForegroundColor Cyan
Start-ScheduledTask -TaskName $TaskName

$deadline = (Get-Date).AddSeconds(90)
$ready = $false
while ((Get-Date) -lt $deadline) {
    try {
        $resp = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/health' `
                                  -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
        if ($resp.StatusCode -eq 200) { $ready = $true; break }
    } catch {
        Start-Sleep -Seconds 2
    }
}

if ($ready) {
    Write-Host 'Dashboard is live at http://127.0.0.1:8000/' -ForegroundColor Green
    exit 0
}

Write-Host 'The task was installed, but the API did not answer within 90s.' -ForegroundColor Yellow
Write-Host "Check: $ProjectDir\logs\service.log"
exit 1