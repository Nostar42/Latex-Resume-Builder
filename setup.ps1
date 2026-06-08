# =============================================================================
#  LaTeX Resume Builder -- one-time setup
#  Installs Python (if missing), pip packages, and nginx for Windows.
#  Run this once before the first launch. Safe to re-run.
# =============================================================================
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

function Step($msg) { Write-Host "" ; Write-Host "  $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "  [OK]  $msg" -ForegroundColor Green  }
function Warn($msg) { Write-Host "  [!]   $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "  [X]   $msg" -ForegroundColor Red; exit 1 }

# ── Python ────────────────────────────────────────────────────────────────────
Step "Checking Python 3..."
$py = $null
foreach ($cmd in @("python", "python3", "py")) {
    try {
        $v = & $cmd --version 2>&1
        if ($v -match "Python 3\.") { $py = $cmd; break }
    } catch {}
}

if (-not $py) {
    Step "Python not found. Installing Python 3.12 via winget..."
    winget install -e --id Python.Python.3.12 `
        --accept-source-agreements --accept-package-agreements
    # Reload PATH so the new python is visible in this session
    $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("PATH","User")
    foreach ($cmd in @("python","python3","py")) {
        try { $v = & $cmd --version 2>&1; if ($v -match "Python 3\.") { $py=$cmd; break } } catch {}
    }
    if (-not $py) { Fail "Python still not found after install. Restart your terminal and re-run." }
}
Ok "Python: $(& $py --version 2>&1)"

# ── pip packages ──────────────────────────────────────────────────────────────
Step "Installing Python packages..."
& $py -m pip install --upgrade pip --quiet
& $py -m pip install -r "$root\server\requirements.txt" --quiet
Ok "fastapi, uvicorn, httpx installed."

# ── nginx ─────────────────────────────────────────────────────────────────────
Step "Checking nginx..."
$nginxExe = "$root\nginx\nginx.exe"

if (Test-Path $nginxExe) {
    $ver = & $nginxExe -v 2>&1
    Ok "nginx already present ($ver)."
} else {
    $version = "1.26.2"
    $url     = "https://nginx.org/download/nginx-$version.zip"
    $zip     = "$env:TEMP\nginx-$version.zip"

    Write-Host "  Downloading nginx $version from nginx.org..."
    try {
        [System.Net.ServicePointManager]::SecurityProtocol = "Tls12,Tls13"
        Invoke-WebRequest $url -OutFile $zip -UseBasicParsing

        # FIX #4: $$ in PowerShell is the last token of the previous line, NOT
        # the PID. Use $PID (the real process ID) to avoid invalid/colliding paths.
        $tmp = "$env:TEMP\nginx_extract_$PID"
        Expand-Archive $zip -DestinationPath $tmp -Force

        # FIX #5: ensure the nginx/ destination directory exists before copying.
        New-Item -ItemType Directory -Force "$root\nginx" | Out-Null

        # Copy the extracted nginx-X.Y.Z/* into nginx/
        $extracted = Get-ChildItem $tmp -Directory | Select-Object -First 1
        Get-ChildItem $extracted.FullName | ForEach-Object {
            $dst = "$root\nginx\$($_.Name)"
            if ($_.PSIsContainer) { Copy-Item $_.FullName $dst -Recurse -Force }
            else                  { Copy-Item $_.FullName $dst -Force }
        }
        Remove-Item $tmp -Recurse -Force -EA SilentlyContinue
        Remove-Item $zip -Force -EA SilentlyContinue
        Ok "nginx $version downloaded and extracted."
    } catch {
        Warn "Could not auto-download nginx: $_"
        Warn "Manual steps:"
        Warn "  1. Download https://nginx.org/en/download.html (Windows version)"
        Warn "  2. Extract nginx.exe and its folders into: $root\nginx\"
        Warn "  The server will still start without nginx (Uvicorn serves directly)."
    }
}

# ── pdflatex ──────────────────────────────────────────────────────────────────
Step "Checking pdflatex..."
$pl = Get-Command pdflatex -ErrorAction SilentlyContinue
if ($pl) { Ok "pdflatex: $($pl.Source)" }
else {
    Warn "pdflatex not on PATH."
    Warn "Install MiKTeX from https://miktex.org and run setup.ps1 again."
}

# ── temp/ + logs/ directories ─────────────────────────────────────────────────
Step "Preparing temp/ and logs/ directories..."
New-Item -ItemType Directory -Force "$root\temp\sessions" | Out-Null
New-Item -ItemType Directory -Force "$root\logs"          | Out-Null
Ok "temp/sessions/ and logs/ ready."

Write-Host ""
Write-Host "  Setup complete!" -ForegroundColor Green
Write-Host "  Launch with:  .\start.bat" -ForegroundColor DarkGray
Write-Host "  Then open:    http://localhost:5000" -ForegroundColor DarkGray
Write-Host ""
