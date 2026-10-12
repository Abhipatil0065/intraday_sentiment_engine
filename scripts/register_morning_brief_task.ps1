# ---------------------------------------------------------------------------
# Register the Windows logon task that runs the overnight catch-up
# ("morning brief") every time you log on in the morning.
#
# What it does: resolves the window your laptop was off, sends every headline
# published in that gap through the LLM for a HIGH / MEDIUM / LOW impact
# verdict, rebuilds sentiment, and writes a dated report to
#   data\reports\morning_brief_<date>.json
# and a rolling log to
#   logs\morning_brief.log
#
# Usage (elevated PowerShell):
#   powershell -ExecutionPolicy Bypass -File scripts\register_morning_brief_task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\register_morning_brief_task.ps1 -WithEngine
#   powershell -ExecutionPolicy Bypass -File scripts\register_morning_brief_task.ps1 -Unregister
# ---------------------------------------------------------------------------
[CmdletBinding()]
param(
    # Also register a logon task that starts the engine server (FastAPI +
    # background pipeline) on 127.0.0.1:8000. Off by default: the morning
    # brief runs standalone, so the engine is only needed when you actually
    # want the live dashboard.
    [switch]$WithEngine,
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

$taskName   = "IntradaySentiment-MorningBrief"
$engineTask = "IntradaySentiment-Engine"
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Split-Path -Parent $scriptRoot
$briefBat   = Join-Path $scriptRoot "morning_brief.bat"
$engineBat  = Join-Path $projectDir "start_server.bat"

if ($Unregister) {
    foreach ($name in @($taskName, $engineTask)) {
        $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($existing) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "Removed scheduled task: $name"
        } else {
            Write-Host "Not registered: $name"
        }
    }
    return
}

if (-not (Test-Path -LiteralPath $briefBat)) {
    throw "morning_brief.bat not found at $briefBat"
}

# --- the morning brief task -------------------------------------------------
$action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c `"$briefBat`"" `
    -WorkingDirectory $projectDir

# At logon, delayed 2 minutes so the network is up before the first fetch.
# StartWhenAvailable catches a missed fire (laptop still booting at logon).
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT2M"

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "LLM-judge every headline published while the laptop was off (HIGH/MEDIUM/LOW impact) and write the morning brief." `
    -Force | Out-Null

Write-Host "Registered '$taskName' - runs 2 minutes after logon:"
Write-Host "  $briefBat"

# --- optional: start the engine server at logon ------------------------------
if ($WithEngine) {
    if (-not (Test-Path -LiteralPath $engineBat)) {
        Write-Warning "start_server.bat not found at $engineBat - engine task skipped"
    } else {
        $engineAction = New-ScheduledTaskAction `
            -Execute "cmd.exe" `
            -Argument "/c `"$engineBat`"" `
            -WorkingDirectory $projectDir

        $engineTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $engineTrigger.Delay = "PT2M"

        $engineSettings = New-ScheduledTaskSettingsSet `
            -StartWhenAvailable `
            -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
            -MultipleInstances IgnoreNew

        Register-ScheduledTask `
            -TaskName $engineTask `
            -Action $engineAction `
            -Trigger $engineTrigger `
            -Settings $engineSettings `
            -Description "Start the intraday sentiment engine (FastAPI + pipeline) at logon." `
            -Force | Out-Null

        Write-Host "Registered '$engineTask' - starts the engine at logon:"
        Write-Host "  $engineBat"
    }
}

Write-Host ""
Write-Host "Verify with:  Get-ScheduledTask -TaskName '$taskName'"
Write-Host "Remove with:  powershell -File scripts\register_morning_brief_task.ps1 -Unregister"
Write-Host "Run now with: schtasks /Run /TN '$taskName'"
