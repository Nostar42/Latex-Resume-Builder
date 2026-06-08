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
| **Python 3.12+** | `setup.bat` installs this automatically via winget |
| **nginx for Windows** | `setup.bat` downloads and extracts this automatically |

> **Python and nginx are handled for you by `setup.bat`.** You only need to install MiKTeX and Ollama yourself.

---

## Quick start

### Step 1 — Install MiKTeX

Download and run the installer from **[miktex.org/download](https://miktex.org/download)**.  
Choose *Install for all users* and tick *Install missing packages on-the-fly* so LaTeX packages download automatically on first compile.

### Step 2 — Install Ollama and pull a model

```powershell
winget install Ollama.Ollama
ollama pull qwen2.5-coder:3b   # recommended starting point (~2 GB, ~4 GB RAM)
```

Pick a model based on your available RAM:

| Model | Size | RAM |
|-------|------|-----|
| `qwen2.5-coder:1.5b` | ~1.0 GB | ~2 GB |
| `qwen2.5-coder:3b` ⭐ | ~1.9 GB | ~4 GB |
| `qwen2.5-coder:7b` | ~4.7 GB | ~8 GB |
| `phi4-mini` | ~2.5 GB | ~4 GB |
| `deepseek-r1:1.5b` | ~1.1 GB | ~2 GB |
| `mistral` | ~4.1 GB | ~8 GB |

You can switch models any time from the dropdown in the app. Run `ollama list` to see what you have installed.

### Step 3 — Clone this repository

```powershell
git clone https://github.com/Nostar42/Latex-Resume-Builder.git
cd Latex-Resume-Builder
```

### Step 4 — Run one-time setup

Double-click **`setup.bat`**.

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

Press **Ctrl+C** in the terminal to stop both services cleanly.

---

## Using the app

| Control | What it does |
|---------|-------------|
| **Mode** dropdown (top-left) | Switch between Resume Builder, Math Solver, and Image → LaTeX PDF |
| **Model** dropdown | Switch between your installed Ollama models |
| **Templates** button | Choose one of five designs. Loading a template replaces your current working resume |
| **Chat bar** (bottom) | Describe a change in plain English. The PDF refreshes automatically when an edit compiles |
| **LaTeX Source** button | Open a drawer to hand-edit the raw `.tex` and recompile directly |
| **📊 Metrics** button | View AI performance stats across all sessions |
| **Setup Guide** button | In-app documentation and troubleshooting |
| **Reset** button | Discard all edits and reload the original template |
| **Download PDF** | Save the current compiled PDF |
| Drag the divider | Resize the PDF viewer vs. chat area |

**Example chat prompts:**
- "Set my name to Jordan Lee and title to Product Designer"
- "Add a bullet under my last job: reduced deployment time by 40%"
- "Change the accent color to teal"
- "Move the Skills section above Education"

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
├── index.html              # Web UI (PDF viewer + chat + mode switcher)
├── start.bat               # Double-click launcher
├── start.ps1               # Starts Uvicorn + nginx, handles Ctrl+C shutdown
├── setup.bat               # Double-click one-time setup (use this, not setup.ps1)
├── setup.ps1               # Setup logic (called by setup.bat)
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
│   ├── timeline.tex
│   ├── blank.tex
│   └── math.tex
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

**`setup.bat` flashes and disappears**  
Right-click `setup.bat` → *Run as administrator*. If it still fails, open PowerShell manually and run:
```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1
```

**"Python not found" when running `start.bat`**  
`setup.bat` hasn't been run yet, or Python didn't install correctly. Run `setup.bat` first and check the output before closing the window.

**"pdflatex not found"**  
Install MiKTeX from [miktex.org](https://miktex.org) and make sure it's on your PATH. Verify with `pdflatex --version` in PowerShell.

**First compile is very slow or fails**  
MiKTeX downloads missing LaTeX packages on first use. This can take 60–120 seconds. Just try again — subsequent compiles are fast.

**"Ollama isn't running"** in the model dropdown  
Start Ollama: run `ollama serve` in a separate terminal, or check that the Ollama desktop app is running in the system tray.

**Chat says "Can't reach the model"**  
Run `ollama list` to confirm your model is installed. If the list is empty, pull a model first: `ollama pull qwen2.5-coder:3b`.

**Port 5000 already in use**  
`start.ps1` tries to free the port automatically, but you can also run:
```powershell
Get-NetTCPConnection -LocalPort 5000 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
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

---

## Model variants & GPU requirements

Disk sizes are at default 4-bit quantisation. **Min VRAM** is what you need to load the model entirely onto the GPU — less than that and Ollama offloads layers to system RAM, making responses 5–20× slower.

### All supported models

| Model | Family | Mode | Disk | Min VRAM |
|-------|--------|------|------|----------|
| `qwen2.5-coder:0.5b` | Qwen2.5-Coder | Resume / Math | 0.4 GB | 2 GB |
| `deepseek-r1:1.5b` | DeepSeek-R1 | Resume / Math | 1.1 GB | 3 GB |
| `qwen2.5-coder:1.5b` | Qwen2.5-Coder | Resume / Math | 1.0 GB | 3 GB |
| `moondream` | Vision / OCR | Image → LaTeX | 1.7 GB | 3 GB |
| `llava-phi3` | Vision / OCR | Image → LaTeX | 2.9 GB | 4 GB |
| `phi4-mini` | Microsoft Phi-4 | Resume / Math | 2.5 GB | 4 GB |
| `qwen2.5-coder:3b` ⭐ | Qwen2.5-Coder | Resume / Math | 1.9 GB | 4 GB |
| `codellama:7b` | Meta CodeLlama | Resume / Math | 3.8 GB | 6 GB |
| `deepseek-r1:7b` | DeepSeek-R1 | Resume / Math | 4.7 GB | 6 GB |
| `llava` | Vision / OCR | Image → LaTeX | 4.7 GB | 6 GB |
| `mistral` | Mistral | Resume / Math | 4.1 GB | 6 GB |
| `qwen2.5-coder:7b` | Qwen2.5-Coder | Resume / Math | 4.7 GB | 6 GB |
| `deepseek-r1:8b` | DeepSeek-R1 | Resume / Math | 5.2 GB | 8 GB |
| `minicpm-v` | Vision / OCR | Image → LaTeX | 5.5 GB | 8 GB |
| `codellama:13b` | Meta CodeLlama | Resume / Math | 7.4 GB | 10 GB |
| `llava:13b` | Vision / OCR | Image → LaTeX | 8.0 GB | 10 GB |
| `deepseek-r1:14b` | DeepSeek-R1 | Resume / Math | 9.0 GB | 12 GB |
| `phi4` | Microsoft Phi-4 | Resume / Math | 9.1 GB | 12 GB |
| `qwen2.5-coder:14b` | Qwen2.5-Coder | Resume / Math | 9.0 GB | 12 GB |
| `mistral-small` | Mistral | Resume / Math | 14 GB | 16 GB |
| `deepseek-r1:32b` | DeepSeek-R1 | Resume / Math | 20 GB | 24 GB |
| `qwen2.5-coder:32b` | Qwen2.5-Coder | Resume / Math | 20 GB | 24 GB |
| `codellama:34b` | Meta CodeLlama | Resume / Math | 19 GB | 24 GB |
| `deepseek-r1:70b` | DeepSeek-R1 | Resume / Math | 43 GB | 48 GB |
| `llama3.3` | Meta Llama 3.3 | Resume / Math | 43 GB | 48 GB |

---

### NVIDIA GeForce

| GPU | VRAM | Largest model that fits fully on GPU |
|-----|------|--------------------------------------|
| RTX 4090 | 24 GB | `codellama:34b`, `qwen2.5-coder:32b`, `deepseek-r1:32b` |
| RTX 3090 / 3090 Ti | 24 GB | `codellama:34b`, `qwen2.5-coder:32b`, `deepseek-r1:32b` |
| RTX 4080 / 4080 Super | 16 GB | `phi4`, `qwen2.5-coder:14b`, `deepseek-r1:14b` |
| RTX 4070 Ti Super | 16 GB | `phi4`, `qwen2.5-coder:14b`, `deepseek-r1:14b` |
| RTX 4060 Ti 16GB | 16 GB | `phi4`, `qwen2.5-coder:14b`, `deepseek-r1:14b` |
| RTX 3080 Ti | 12 GB | `codellama:13b`, `llava:13b` |
| RTX 4070 / 4070 Super / 4070 Ti | 12 GB | `codellama:13b`, `llava:13b` |
| RTX 3060 12GB | 12 GB | `codellama:13b`, `llava:13b` |
| RTX 3080 10GB | 10 GB | `codellama:13b`, `llava:13b` |
| RTX 4060 / 4060 Ti 8GB | 8 GB | `deepseek-r1:7b`, `qwen2.5-coder:7b`, `mistral`, `llava` |
| RTX 3060 Ti / 3070 / 3070 Ti | 8 GB | `deepseek-r1:7b`, `qwen2.5-coder:7b`, `mistral`, `llava` |
| RTX 3050 / 3050 Ti | 8 GB | `deepseek-r1:7b`, `qwen2.5-coder:7b`, `mistral`, `llava` |
| RTX 3060 Laptop | 6 GB | `qwen2.5-coder:7b`, `mistral`, `codellama:7b` |
| GTX 1660 / 1660 Super / Ti | 6 GB | `qwen2.5-coder:7b`, `mistral`, `codellama:7b` |
| GTX 1060 6GB | 6 GB | `qwen2.5-coder:7b`, `mistral`, `codellama:7b` |
| GTX 1650 / 1650 Super | 4 GB | `qwen2.5-coder:3b`, `phi4-mini`, `llava-phi3` |

---

### AMD Radeon

| GPU | VRAM | Largest model that fits fully on GPU |
|-----|------|--------------------------------------|
| RX 7900 XTX | 24 GB | `codellama:34b`, `qwen2.5-coder:32b`, `deepseek-r1:32b` |
| RX 7900 XT | 20 GB | `codellama:34b`, `qwen2.5-coder:32b`, `deepseek-r1:32b` |
| RX 7900 GRE / 6900 XT / 6950 XT | 16 GB | `phi4`, `qwen2.5-coder:14b`, `deepseek-r1:14b` |
| RX 7800 XT / 6800 XT | 16 GB | `phi4`, `qwen2.5-coder:14b`, `deepseek-r1:14b` |
| RX 7700 XT / 6700 XT | 12 GB | `codellama:13b`, `llava:13b` |
| RX 6700 | 10 GB | `codellama:13b`, `llava:13b` |
| RX 7600 / 6600 XT / 5700 XT | 8 GB | `deepseek-r1:7b`, `qwen2.5-coder:7b`, `mistral`, `llava` |
| RX 6600 | 8 GB | `deepseek-r1:7b`, `qwen2.5-coder:7b`, `mistral`, `llava` |
| RX 5500 XT / 580 8GB | 8 GB | `deepseek-r1:7b`, `qwen2.5-coder:7b`, `mistral`, `llava` |

> AMD GPU support requires ROCm. Works best on Linux — Windows ROCm support is limited. Check [ollama.com](https://ollama.com) for current status.

---

### Apple Silicon (unified memory)

Apple Silicon shares memory between CPU and GPU. The full system RAM is available to Ollama — subtract ~3 GB for macOS overhead on 8 GB systems.

| Chip | Memory | Largest model that fits |
|------|--------|------------------------|
| M2 Ultra / M3 Ultra | 128–192 GB | All models including 70B |
| M2 Max / M3 Max | 36–96 GB | 70B on 48 GB+; up to 32B on 36 GB |
| M1 Max | 32–64 GB | Up to 32B on 32 GB; 70B on 64 GB |
| M1 Pro / M2 Pro / M3 Pro / M4 Pro | 16–48 GB | Up to 32B on 48 GB; up to 14B on 32 GB; up to 13B on 16 GB |
| M1 / M2 / M3 / M4 (24 GB) | 24 GB | `codellama:34b`, `qwen2.5-coder:32b` |
| M1 / M2 / M3 / M4 (16 GB) | 16 GB | `phi4`, `qwen2.5-coder:14b`, `deepseek-r1:14b` |
| M1 / M2 / M3 / M4 (8 GB) | ~5 GB free | `qwen2.5-coder:3b`, `phi4-mini` |

> Apple Silicon is exceptionally efficient for inference — a 7B model on an M2 often matches or beats the same model on an RTX 3070.
