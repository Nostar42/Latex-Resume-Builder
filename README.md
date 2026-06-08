# LaTeX Resume Builder

A local website that displays a resume as a live **PDF** and lets you edit it by **chatting with a local AI model**. Pick one of five templates, describe a change in plain English, and the model rewrites the LaTeX, the server recompiles, and the PDF updates — all on your own machine with no API key or cloud service.

![Resume Builder UI — PDF viewer above a chat bar](https://raw.githubusercontent.com/Nostar42/Latex-Resume-Builder/main/docs/screenshot.png)

---

## How it works

```
browser  ──HTTPS──►  nginx :5000  ──proxy──►  Uvicorn/FastAPI :8000
                                                     │
                          ┌──────────────────────────┤
                          │                          │
                    Ollama :11434              pdflatex (MiKTeX)
                  (local AI model)           (LaTeX → PDF compile)
```

- The **AI** is a local Ollama model running entirely on your machine — no internet required.
- Edits are sent as **SEARCH/REPLACE blocks** (not full-file rewrites), keeping things fast on CPU-only hardware.
- Each browser tab is **isolated**: its own working `.tex`, `.pdf`, and session directory under `temp/sessions/`.
- Old sessions are cleaned up automatically every hour (default TTL: 24 hours).

---

## Requirements

| Requirement | Notes |
|-------------|-------|
| **Windows 10 / 11** | The launcher scripts are PowerShell 5.1 |
| **MiKTeX** | Provides `pdflatex`. Download from [miktex.org](https://miktex.org) |
| **Ollama** | Local AI runner. Download from [ollama.com](https://ollama.com) |
| **Python 3.12+** | `setup.ps1` installs this automatically via winget |
| **nginx for Windows** | `setup.ps1` downloads and extracts this automatically |

> **Python and nginx are handled for you by `setup.ps1`.** You only need to install MiKTeX and Ollama yourself.

---

## Quick start

### Step 1 — Install MiKTeX

Download and run the installer from **[miktex.org/download](https://miktex.org/download)**.  
Choose *Install for all users* and tick *Install missing packages on-the-fly* so LaTeX packages download automatically on first compile.

### Step 2 — Install Ollama and pull a model

```powershell
winget install Ollama.Ollama
```

Then pull at least one model. Pick based on your available RAM:

**Qwen2.5-Coder** (best for code/LaTeX editing)

| Model | Size on disk | RAM needed | Speed (CPU) |
|-------|-------------|------------|-------------|
| `qwen2.5-coder:1.5b` | ~1.0 GB | ~2 GB | Very fast |
| `qwen2.5-coder:3b` | ~1.9 GB | ~4 GB | Fast |
| `qwen2.5-coder:7b` | ~4.7 GB | ~8 GB | Moderate |
| `qwen2.5-coder:14b` | ~9.0 GB | ~16 GB | Slow on CPU |

**Meta Llama 3.3** (general-purpose, strong reasoning)

| Model | Size on disk | RAM needed | Speed (CPU) |
|-------|-------------|------------|-------------|
| `llama3.3` | ~43 GB | ~48 GB | Very slow on CPU; GPU recommended |

**Meta CodeLlama** (code-focused, Llama-based)

| Model | Size on disk | RAM needed | Speed (CPU) |
|-------|-------------|------------|-------------|
| `codellama:7b` | ~3.8 GB | ~8 GB | Moderate |
| `codellama:13b` | ~7.4 GB | ~16 GB | Slow on CPU |
| `codellama:34b` | ~19 GB | ~32 GB | Very slow on CPU; GPU recommended |

```powershell
ollama pull qwen2.5-coder:3b   # recommended starting point
```

Ollama runs as a background service after install. Start it manually if needed:
```powershell
ollama serve
```

### Step 3 — Clone this repository

```powershell
git clone https://github.com/Nostar42/Latex-Resume-Builder.git
cd Latex-Resume-Builder
```

### Step 4 — Run one-time setup

```powershell
.\setup.ps1
```

This script:
- Installs Python 3.12 via `winget` (if not already installed)
- Installs `fastapi`, `uvicorn`, and `httpx` via `pip`
- Downloads nginx 1.26.2 for Windows into `nginx/`
- Creates the `temp/sessions/` and `logs/` working directories

You only need to run this once. It is safe to re-run.

### Step 5 — Launch

Double-click **`start.bat`**, or from PowerShell:

```powershell
.\start.ps1
```

Then open **[http://localhost:5000](http://localhost:5000)** in your browser.

The terminal shows the startup banner:

```
  LaTeX Resume Builder
  Python   : Python 3.12.x
  Starting : Uvicorn on 127.0.0.1:8000...
  Uvicorn  : PID 1234
  Starting : nginx on port 5000...
  nginx    : PID 5678

  Open:  http://localhost:5000

  Press Ctrl+C to stop all services.
```

Press **Ctrl+C** in the terminal to stop both services cleanly.

---

## Using the app

| Control | What it does |
|---------|-------------|
| **Model** dropdown | Switch between your installed Ollama models. The choice is remembered across restarts. |
| **Templates** button | Choose one of five designs. Loading a template replaces your current working resume. |
| **Chat bar** (bottom) | Describe a change in plain English. The PDF refreshes automatically when an edit compiles. |
| **LaTeX Source** button | Open a drawer to hand-edit the raw `.tex` and recompile directly. |
| **Reset** button | Discard all edits and reload the original (unedited) template. |
| **Download PDF** | Save the current compiled PDF. |
| Drag the divider | Resize the PDF viewer vs. chat area. |

**Example chat prompts:**
- "Set my name to Jordan Lee and title to Product Designer"
- "Add a bullet under my last job: reduced deployment time by 40%"
- "Change the accent color to teal"
- "Move the Skills section above Education"

On a CPU-only machine expect **30–60 seconds** per edit. The first message is slower while the model loads into RAM.

---

## Templates

| Template | Style |
|----------|-------|
| Classic Two-Column | Two-column layout with FontAwesome contact icons |
| Modern Accent | Colored header band, sans-serif, pill-style section labels |
| Minimalist Serif | Elegant Palatino, centered header, no icons or color |
| Sidebar Two-Tone | Full-height tinted sidebar + main content column |
| Timeline Professional | Colored header bar with a dated experience timeline |

All five fit on a single page and contain only placeholder data — no personal information.

---

## File structure

```
Latex-Resume-Builder/
├── index.html              # Web UI (PDF viewer + chat + templates + source drawer)
├── start.bat               # Double-click launcher
├── start.ps1               # Starts Uvicorn + nginx, handles Ctrl+C shutdown
├── setup.ps1               # One-time installer (Python, pip packages, nginx)
├── server/
│   ├── main.py             # FastAPI application (all API endpoints + AI + compile logic)
│   └── requirements.txt    # Python dependencies (fastapi, uvicorn, httpx)
├── nginx/
│   └── nginx.conf          # Reverse proxy config (port 5000 → 8000, rate limiting)
├── templates/
│   ├── classic.tex
│   ├── modern.tex
│   ├── minimalist.tex
│   ├── sidebar.tex
│   └── timeline.tex
├── temp/                   # ← git-ignored; created at runtime
│   ├── sessions/<uuid>/    # Per-browser working files (tex, pdf, log, aux)
│   └── model.txt           # Remembers your last-used model
└── logs/                   # ← git-ignored; nginx default log directory
```

---

## Environment variables

Set these before launching to override defaults:

```powershell
$env:OLLAMA_MODEL = "qwen2.5-coder:7b"          # default model
$env:OLLAMA_URL   = "http://localhost:11434"     # Ollama address
$env:SESSION_MAX_AGE = "86400"                  # session TTL in seconds (default 24 h)

# Switch to vLLM instead of Ollama (for GPU servers):
$env:USE_VLLM     = "true"
$env:LLM_BASE_URL = "http://localhost:8001"
```

---

## Troubleshooting

**"pdflatex not found"**  
Install MiKTeX from [miktex.org](https://miktex.org) and make sure it's on your PATH. You can verify with `pdflatex --version` in PowerShell.

**First compile is very slow or fails**  
MiKTeX downloads missing LaTeX packages on first use. This can take 60–120 seconds. The compile timeout is set to 120 s to accommodate this. Just try again — subsequent compiles are fast.

**"Ollama isn't running"** in the model dropdown  
Start Ollama: run `ollama serve` in a separate terminal, or check that the Ollama desktop app is running in the system tray.

**Chat says "Can't reach the model"**  
Run `ollama list` to confirm your model is installed. If the list is empty, pull a model first: `ollama pull qwen2.5-coder:3b`.

**Port 5000 already in use**  
Another process is using port 5000. `start.ps1` tries to free the port automatically, but you can also run:
```powershell
Get-NetTCPConnection -LocalPort 5000 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

**`setup.ps1` won't run**  
Run PowerShell as Administrator, or bypass the execution policy for the one script:
```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1
```

---

## Security notes

- **`-no-shell-escape`** is passed to every `pdflatex` call, disabling `\write18` so LaTeX source cannot execute arbitrary shell commands.
- Session IDs are validated as proper UUID4 before being used as filesystem paths (prevents path traversal).
- Rate limiting is enforced at both the nginx and FastAPI layers (10 AI requests/min, 20 compile requests/min, 60 read requests/min per IP).
- The server is designed for personal/LAN use. See the *Going further* section if you want to expose it publicly.

---

## Going further

To host this on the internet you would need:

1. A Linux VPS (e.g. Hetzner, DigitalOcean) — $6–$24/month
2. A domain name and a Let's Encrypt SSL certificate (`certbot`)
3. Replace the PowerShell launcher with a `systemd` service
4. Replace Ollama with a faster backend (vLLM on a GPU, or a cloud API like Groq)

The Python server (`server/main.py`) and the nginx config are already structured for this — `USE_VLLM=true` and `LLM_BASE_URL` switch the AI backend with no code changes.
