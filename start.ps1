# =============================================================================
#  LaTeX Resume Builder -- launch script
#  Starts Uvicorn (FastAPI, port 8000) and nginx (port 5000).
#  If nginx is not yet installed, Uvicorn listens on port 5000 directly.
#
#  ASCII-only: PowerShell 5.1 reads .ps1 without BOM as Windows-1252.
# =============================================================================
$ErrorActionPreference = "Stop"
$root      = $PSScriptRoot
$uvPort    = 8000
$pubPort   = 5000
$nginxExe  = Join-Path $root "nginx\nginx.exe"
$nginxConf = Join-Path $root "nginx\nginx.conf"

# ── Find Python ───────────────────────────────────────────────────────────────
$py = $null
foreach ($cmd in @("python", "python3", "py")) {
    try {
        $v = & $cmd --version 2>&1
        if ($v -match "Python 3\.") { $py = $cmd; break }
    } catch {}
}
if (-not $py) {
    Write-Host "  Python 3 not found. Run setup.ps1 first." -ForegroundColor Red
    exit 1
}

# ── Verify uvicorn is installed ───────────────────────────────────────────────
try { & $py -m uvicorn --version 2>&1 | Out-Null } catch {
    Write-Host "  uvicorn not installed. Run setup.ps1 first." -ForegroundColor Red
    exit 1
}

# ── Free ports ────────────────────────────────────────────────────────────────
foreach ($port in @($uvPort, $pubPort)) {
    Get-NetTCPConnection -LocalPort $port -State Listen -EA SilentlyContinue |
        ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -EA SilentlyContinue }
}
Start-Sleep 1

Write-Host ""
Write-Host "  LaTeX Resume Builder" -ForegroundColor Cyan
Write-Host "  Python   : $(& $py --version 2>&1)" -ForegroundColor DarkGray

# ── Start Uvicorn ─────────────────────────────────────────────────────────────
$useNginx  = Test-Path $nginxExe
$listenHost = if ($useNginx) { "127.0.0.1" } else { "0.0.0.0" }
$listenPort = if ($useNginx) { $uvPort      } else { $pubPort  }

Write-Host "  Starting : Uvicorn on $listenHost`:$listenPort..." -ForegroundColor DarkGray
$uvArgs = @("-m", "uvicorn", "server.main:app",
            "--host", $listenHost, "--port", "$listenPort",
            "--log-level", "warning")
$uvProc = Start-Process $py -ArgumentList $uvArgs `
    -WorkingDirectory $root -PassThru -NoNewWindow
Write-Host "  Uvicorn  : PID $($uvProc.Id)" -ForegroundColor DarkGray

# Poll until Uvicorn is actually listening on its port (up to 15 s).
# A flat sleep is fragile on slow machines or cold Python starts.
$deadline = (Get-Date).AddSeconds(15)
$bound    = $false
while ((Get-Date) -lt $deadline) {
    $conn = Test-NetConnection -ComputerName 127.0.0.1 -Port $uvPort `
                -InformationLevel Quiet -EA SilentlyContinue
    if ($conn) { $bound = $true; break }
    Start-Sleep -Milliseconds 400
}
if (-not $bound) {
    Write-Host "  WARN: Uvicorn did not bind on port $uvPort within 15 s." -ForegroundColor Yellow
}

# ── Start nginx (if available) ────────────────────────────────────────────────
$ngProc = $null
if ($useNginx) {
    Write-Host "  Starting : nginx on port $pubPort..." -ForegroundColor DarkGray
    $ngProc = Start-Process $nginxExe `
        -ArgumentList "-p", $root, "-c", $nginxConf `
        -WorkingDirectory $root -PassThru -NoNewWindow
    Write-Host "  nginx    : PID $($ngProc.Id)" -ForegroundColor DarkGray
} else {
    Write-Host "  nginx not found -- Uvicorn serving directly on port $pubPort." -ForegroundColor Yellow
    Write-Host "  Run setup.ps1 to add nginx." -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "  Open:  http://localhost:$pubPort" -ForegroundColor Green
Write-Host ""
Write-Host "  Env overrides (set before launch):" -ForegroundColor DarkGray
Write-Host "    OLLAMA_MODEL  -- which model to default to" -ForegroundColor DarkGray
Write-Host "    OLLAMA_URL    -- Ollama address (default http://localhost:11434)" -ForegroundColor DarkGray
Write-Host "    USE_VLLM=true -- switch inference to vLLM" -ForegroundColor DarkGray
Write-Host "    LLM_BASE_URL  -- vLLM address (e.g. http://localhost:8001)" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Press Ctrl+C to stop all services." -ForegroundColor DarkGray
Write-Host ""

# ── Wait + graceful shutdown ──────────────────────────────────────────────────
try {
    # Block until Uvicorn exits (e.g. Ctrl+C kills it)
    $uvProc | Wait-Process -EA SilentlyContinue
} finally {
    Write-Host "" ; Write-Host "  Stopping services..." -ForegroundColor DarkGray
    try { Stop-Process -Id $uvProc.Id -Force -EA SilentlyContinue } catch {}
    if ($ngProc) {
        try {
            # Graceful nginx stop
            Start-Process $nginxExe `
                -ArgumentList "-p", $root, "-c", $nginxConf, "-s", "stop" `
                -WorkingDirectory $root -Wait -NoNewWindow
        } catch {
            try { Stop-Process -Id $ngProc.Id -Force -EA SilentlyContinue } catch {}
        }
    }
    Write-Host "  Stopped." -ForegroundColor DarkGray
}
