<#
.SYNOPSIS
    Install (or remove) the Windows scheduled task for Streamlit dashboard.

.DESCRIPTION
    Registers a per-user task "IntradaySentimentStreamlit" that runs
    Streamlit on port 8501.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\tools\install_streamlit_autostart.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\tools\install_streamlit_autostart.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    # Removes the task instead of installing it.
    [switch]$Uninstall,

    [string]$TaskName = 'IntradaySentimentStreamlit',

    # Extra delay after logon, before the first start
    [int]$LogonDelaySeconds = 10
)

$ErrorActionPreference = 'Stop'

$ProjectDir = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $ProjectDir '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python not found: $PythonExe"
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
    } else {
        Write-Host "Scheduled task '$TaskName' is not installed." -ForegroundColor Yellow
    }
    return
}

# --------------------------------------------------------------------------
# Install
# --------------------------------------------------------------------------
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$arguments = "-m streamlit run app/dashboard/dashboard.py --server.port 8501 --server.headless true"

$action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument $arguments `
    -WorkingDirectory $ProjectDir

# At logon - the main fix for the restored browser tab
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
if ($LogonDelaySeconds -gt 0) {
    $logonTrigger.Delay = "PT${LogonDelaySeconds}S"
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

# Interactive logon type = runs as the signed-in user, no stored password
$principal = New-ScheduledTaskPrincipal `
    -UserId $userId `
    -LogonType Interactive `
    -RunLevel Limited

$description = 'Keeps the Streamlit dashboard (127.0.0.1:8501) running after logon.'

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $logonTrigger `
    -Settings $settings `
    -Principal $principal `
    -Description $description `
    -Force | Out-Null

Write-Host "Installed scheduled task '$TaskName'." -ForegroundColor Green
Write-Host "  Trigger  : at logon as $userId (after ${LogonDelaySeconds}s)"
Write-Host "  Runs     : $PythonExe -m streamlit run app/dashboard/dashboard.py --server.port 8501 --server.headless true"
Write-Host '  Hidden window, no admin rights, survives crashes.'

# Bring it up right now instead of waiting for the next logon
Write-Host ''
Write-Host 'Starting Streamlit now...' -ForegroundColor Cyan
Start-ScheduledTask -TaskName $TaskName

$deadline = (Get-Date).AddSeconds(60)
$ready = $false
while ((Get-Date) -lt $deadline) {
    try {
        $resp = Invoke-WebRequest -Uri 'http://127.0.0.1:8501' `
                                  -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
        if ($resp.StatusCode -eq 200) { $ready = $true; break }
    } catch {
        Start-Sleep -Seconds 2
    }
}

if ($ready) {
    Write-Host 'Streamlit is live at http://127.0.0.1:8501/' -ForegroundColor Green
    exit 0
}

Write-Host 'The task was installed, but Streamlit did not answer within 60s.' -ForegroundColor Yellow
exit 1