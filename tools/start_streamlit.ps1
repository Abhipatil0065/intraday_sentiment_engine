$ErrorActionPreference = 'Stop'
$ProjectDir = 'C:\Users\HP\.vscode\intraday_sentiment_engine'
$PythonExe = Join-Path $ProjectDir '.venv\Scripts\python.exe'
$Script = Join-Path $ProjectDir 'app\dashboard\dashboard.py'
$OutLog = Join-Path $ProjectDir 'logs\streamlit_out.txt'
$ErrLog = Join-Path $ProjectDir 'logs\streamlit_err.txt'

# Kill any process already listening on 8501
$listener = Get-NetTCPConnection -LocalPort 8501 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    Write-Output "Killing existing listener on 8501 (PID $($listener.OwningProcess))"
    Stop-Process -Id $listener.OwningProcess -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

$args = @('-m', 'streamlit', 'run', $Script, '--server.port', '8501', '--server.headless', 'true')
Write-Output "Starting: $PythonExe -m streamlit $($args -join ' ')"
$proc = Start-Process -FilePath $PythonExe `
    -ArgumentList $args `
    -WorkingDirectory $ProjectDir `
    -WindowStyle Hidden `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError $ErrLog `
    -PassThru
Write-Output "Started Streamlit PID $($proc.Id)"
Start-Sleep -Seconds 8

# Probe
$ok = $false
for ($i = 0; $i -lt 10; $i++) {
    try {
        $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8501/_stcore/health' -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
        Write-Output "health probe: HTTP $($r.StatusCode) body=$($r.Content)"
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch {
        Write-Output "probe $i failed: $($_.Exception.Message)"
    }
    Start-Sleep -Seconds 2
}
if ($ok) { Write-Output "STREAMLIT_READY" } else { Write-Output "STREAMLIT_NOT_READY" }
