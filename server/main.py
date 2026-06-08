"""
LaTeX Resume Builder -- FastAPI server
Serves the website, compiles LaTeX server-side, and calls a local AI model
(Ollama by default; switch to vLLM by setting USE_VLLM=true).

Each browser session gets its own working directory under temp/sessions/,
so multiple users are fully isolated. The pdflatex runs in a thread pool so
AI calls and compile jobs can overlap without blocking each other.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
from fastapi import Cookie, FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR      = Path(__file__).parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
TEMP_DIR      = BASE_DIR / "temp"
INDEX_PATH    = BASE_DIR / "index.html"
SESSIONS_DIR  = TEMP_DIR / "sessions"
MODEL_FILE    = TEMP_DIR / "model.txt"

SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Config -- override any of these with environment variables
# ---------------------------------------------------------------------------
OLLAMA_URL    = os.getenv("OLLAMA_URL",   "http://localhost:11434")
DEFAULT_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:3b")

# Set USE_VLLM=true and LLM_BASE_URL=http://localhost:8001 (wherever vLLM
# is listening) to switch inference from Ollama to vLLM with no other changes.
USE_VLLM     = os.getenv("USE_VLLM", "").lower() in ("1", "true", "yes")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", OLLAMA_URL)

# MiKTeX path is auto-detected; the hard-coded fallback matches this machine.
PDFLATEX = (
    shutil.which("pdflatex")
    or r"C:\Users\hp\AppData\Local\Programs\MiKTeX\miktex\bin\x64\pdflatex.exe"
)

MAX_COMPILES = 2   # concurrent pdflatex processes
MAX_SOURCE_BYTES = 500_000   # 500 KB -- guard against runaway source uploads
MAX_IMAGE_BYTES  = 8_000_000 # ~6 MB image (base64) — enough for a scanned page

# Session lifetime: how long a session can sit idle before it is deleted.
# Override with SESSION_MAX_AGE env var (seconds).  Default = 24 h.
SESSION_MAX_AGE      = int(os.getenv("SESSION_MAX_AGE", str(86400)))
SESSION_CLEANUP_INTERVAL = 3600   # run the janitor once per hour

# ---------------------------------------------------------------------------
# In-process rate limiter (sliding-window, keyed by client IP)
# Works on any OS; complements the nginx limit_req layer.
#
# Limits (per IP, per 60-second window):
#   chat    — 10  (AI + compile; one every ~6 s is comfortable)
#   compile — 20  (template/source/reset; compile-only)
#   api     — 60  (lightweight reads)
# ---------------------------------------------------------------------------
_rl_store: dict[str, list[float]] = defaultdict(list)
_rl_lock = asyncio.Lock()   # created fresh in the event loop via _rate_limit()

# ---------------------------------------------------------------------------
# Global AI performance metrics (all sessions combined).
# Stored in memory only; cleared on server restart.  Capped at _METRICS_CAP
# to bound memory use on long-running servers.
# ---------------------------------------------------------------------------
_METRICS_CAP = 500
_metrics: list[dict] = []


def _record_metric(entry: dict) -> None:
    global _metrics
    _metrics.append(entry)
    if len(_metrics) > _METRICS_CAP:
        _metrics = _metrics[-_METRICS_CAP:]

_RL_WINDOW  = 60.0   # sliding window width in seconds
_RL_CHAT    = 10
_RL_COMPILE = 20
_RL_API     = 60


async def _rate_limit(request: Request, max_requests: int) -> bool:
    """
    Returns True if the request is within the limit, False if it should be
    rejected. Uses a per-(IP, limit-tier) sliding window stored in memory,
    so hitting the API read limit never blocks the chat or compile limits.
    """
    ip  = (request.client.host if request.client else "unknown")
    key = f"{ip}:{max_requests}"   # different tiers get separate buckets
    now = time.monotonic()
    cutoff = now - _RL_WINDOW

    async with _rl_lock:
        times = _rl_store[key]
        # Evict timestamps outside the current window
        _rl_store[key] = [t for t in times if t > cutoff]
        if len(_rl_store[key]) >= max_requests:
            return False
        _rl_store[key].append(now)
    return True


def _too_many() -> JSONResponse:
    return JSONResponse(
        {"error": "Too many requests — please wait a moment and try again."},
        status_code=429,
    )


# ---------------------------------------------------------------------------
# Session cleanup -- background task, runs every SESSION_CLEANUP_INTERVAL s
# ---------------------------------------------------------------------------
async def _cleanup_sessions() -> None:
    """
    Delete session directories whose files haven't been touched in
    SESSION_MAX_AGE seconds.  Uses the newest file-mtime inside the
    directory as the "last active" timestamp, so an in-progress compile
    (which writes .aux / .log / .pdf) is never mistakenly deleted.
    """
    while True:
        await asyncio.sleep(SESSION_CLEANUP_INTERVAL)
        cutoff  = time.time() - SESSION_MAX_AGE
        removed = 0
        errors  = 0
        try:
            for d in SESSIONS_DIR.iterdir():
                if not (d.is_dir() and _UUID_RE.match(d.name)):
                    continue
                try:
                    files = [f for f in d.iterdir() if f.is_file()]
                    last_active = (
                        max(f.stat().st_mtime for f in files)
                        if files else d.stat().st_mtime
                    )
                    if last_active < cutoff:
                        shutil.rmtree(d, ignore_errors=True)
                        removed += 1
                except Exception as exc:
                    errors += 1
                    print(f"  WARN cleanup [{d.name[:8]}]: {exc}")
        except Exception as exc:
            print(f"  WARN cleanup scan failed: {exc}")
        if removed or errors:
            print(f"  cleanup: removed {removed} expired session(s)"
                  + (f", {errors} error(s)" if errors else ""))


# FIX #10: separate connect vs. read timeouts.
# connect=5 s means a firewalled (non-refused) host surfaces an error in 5 s
# instead of hanging for 10 full minutes.
_LLM_TIMEOUT = httpx.Timeout(connect=5.0, read=600.0, write=60.0, pool=60.0)

# FIX #3: only accept well-formed UUID4 session IDs from cookies.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# ---------------------------------------------------------------------------
# App lifecycle  (FIX #1 + #2: uses lifespan instead of the deprecated on_event hooks)
# ---------------------------------------------------------------------------
_sem:  asyncio.Semaphore               # created inside the running event loop
_pool = ThreadPoolExecutor(max_workers=MAX_COMPILES, thread_name_prefix="latex")
_index_html: bytes = b""              # FIX #15: cached at startup, not re-read per request


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── startup ──────────────────────────────────────────────────────────────
    global _sem, _index_html
    _sem        = asyncio.Semaphore(MAX_COMPILES)
    _index_html = INDEX_PATH.read_bytes()      # cache once; file never changes at runtime
    print(f"  pdflatex  : {PDFLATEX}")
    print(f"  LLM URL   : {LLM_BASE_URL}  ({'vLLM' if USE_VLLM else 'Ollama'})")
    print(f"  Model     : {_active_model()}")
    print(f"  Sessions  : {SESSIONS_DIR}")
    ttl_h = SESSION_MAX_AGE // 3600
    print(f"  Session TTL: {ttl_h} h  (cleanup every {SESSION_CLEANUP_INTERVAL // 60} min)")

    # Start session janitor as a background asyncio task.
    # It sleeps first, so it never runs during startup.
    _cleanup_task = asyncio.create_task(_cleanup_sessions(), name="session-cleanup")

    yield

    # ── shutdown ─────────────────────────────────────────────────────────────
    _cleanup_task.cancel()
    try:
        await _cleanup_task
    except asyncio.CancelledError:
        pass
    # FIX #14: clean up the thread pool so pdflatex workers aren't left dangling.
    _pool.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)


# ---------------------------------------------------------------------------
# Active model (shared across all sessions, persisted in temp/model.txt)
# ---------------------------------------------------------------------------
def _active_model() -> str:
    if MODEL_FILE.exists():
        m = MODEL_FILE.read_text(encoding="utf-8").strip()
        if m:
            return m
    return DEFAULT_MODEL


def _set_model(name: str) -> None:
    MODEL_FILE.write_text(name.strip(), encoding="utf-8")


# ---------------------------------------------------------------------------
# Sessions -- each browser gets a UUID stored in a cookie
# ---------------------------------------------------------------------------
def _valid_sid(session_id: Optional[str]) -> bool:
    """FIX #3: reject any cookie value that isn't a well-formed UUID4."""
    return bool(session_id and _UUID_RE.match(session_id))


def _sd(sid: str) -> Path:
    d = SESSIONS_DIR / sid
    d.mkdir(parents=True, exist_ok=True)
    return d

def _tex(sid: str)  -> Path: return _sd(sid) / "working.tex"
def _pdf(sid: str)  -> Path: return _sd(sid) / "working.pdf"
def _log(sid: str)  -> Path: return _sd(sid) / "working.log"
def _last(sid: str) -> Path: return _sd(sid) / "last_good.tex"
def _cur(sid: str)  -> Path: return _sd(sid) / "current.txt"


def _resolve(session_id: Optional[str]) -> tuple[str, bool]:
    """Return (sid, is_new). Generates a fresh UUID when session is absent or invalid."""
    # FIX #3: validate UUID format before trusting the value as a filesystem path.
    if _valid_sid(session_id) and (SESSIONS_DIR / session_id).exists():  # type: ignore[arg-type]
        return session_id, False  # type: ignore[return-value]
    return str(uuid.uuid4()), True


def _set_cookie(resp: Response | JSONResponse, sid: str, is_new: bool) -> None:
    if is_new:
        resp.set_cookie("session_id", sid, max_age=86400 * 30,
                        samesite="lax", httponly=True)


def _init_session(sid: str) -> None:
    """Copy the default template into a fresh session if working.tex is missing."""
    if not _tex(sid).exists():
        names = _tnames()
        tmpl  = _cur_tmpl(sid, names)
        if tmpl:
            shutil.copy2(TEMPLATES_DIR / f"{tmpl}.tex", _tex(sid))


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
def _tnames() -> list[str]:
    if not TEMPLATES_DIR.exists():
        return []
    return sorted(p.stem for p in TEMPLATES_DIR.glob("*.tex"))


def _tlabel(name: str) -> str:
    p = TEMPLATES_DIR / f"{name}.tex"
    if p.exists():
        first = p.read_text(encoding="utf-8").split("\n", 1)[0]
        m = re.match(r"^%\s*!TEMPLATE:\s*(.+?)\s*$", first)
        if m:
            return m.group(1)
    return name


def _cur_tmpl(sid: str, names: list[str] | None = None) -> str:
    if names is None:
        names = _tnames()
    cf = _cur(sid)
    if cf.exists():
        c = cf.read_text(encoding="utf-8").strip()
        if c in names:
            return c
    if "classic" in names:
        return "classic"
    return names[0] if names else ""


def _load_tmpl(sid: str, name: str) -> bool:
    if name not in _tnames():
        return False
    shutil.copy2(TEMPLATES_DIR / f"{name}.tex", _tex(sid))
    _cur(sid).write_text(name, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# LaTeX compilation
# ---------------------------------------------------------------------------
def _compile_sync(sid: str) -> dict:
    """Blocking; always runs in the thread pool via _compile()."""
    d   = _sd(sid)
    tex = _tex(sid)
    pdf = _pdf(sid)
    log = _log(sid)
    try:
        subprocess.run(
            [PDFLATEX,
             "-interaction=nonstopmode",
             "-halt-on-error",
             # Sandboxing: disable \write18 so LaTeX source cannot execute
             # arbitrary shell commands, even if a user crafts malicious input.
             "-no-shell-escape",
             f"-output-directory={d}", str(tex)],
            capture_output=True, cwd=str(d),
            # FIX #11: 45 s was too short for first MiKTeX run (font-cache rebuild
            # + on-demand package downloads can take 90-120 s).
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False,
                "error": "Compilation timed out (120 s). A LaTeX package may be downloading -- try again."}
    except FileNotFoundError:
        return {"ok": False, "error": f"pdflatex not found at: {PDFLATEX}"}

    log_text = log.read_text(encoding="utf-8", errors="ignore") if log.exists() else ""

    if pdf.exists() and "Output written on" in log_text:
        shutil.copy2(tex, _last(sid))
        return {"ok": True, "error": None}

    lines = log_text.splitlines()
    hits  = [l for l in lines if l.startswith("!") or re.match(r"^l\.\d+", l)]
    err   = "\n".join(hits[:8]) if hits else "\n".join(lines[-12:]) if lines else "Unknown error."
    return {"ok": False, "error": err}


async def _compile(sid: str) -> dict:
    # FIX #1: get_running_loop() is the correct call inside an async context.
    # get_event_loop() is deprecated in Python 3.10+ and raises in future versions.
    loop = asyncio.get_running_loop()
    async with _sem:
        return await loop.run_in_executor(_pool, _compile_sync, sid)


async def _revert(sid: str) -> None:
    """Restore last_good.tex and recompile. Logs if the revert itself fails."""
    lg = _last(sid)
    if lg.exists():
        shutil.copy2(lg, _tex(sid))
        rv = await _compile(sid)
        if not rv["ok"]:
            print(f"  WARN [{sid[:8]}]: revert compile failed: {rv['error']}")


# ---------------------------------------------------------------------------
# Edit parsing + application
# ---------------------------------------------------------------------------
_EDIT_RX = re.compile(
    r"<{5,}\s*SEARCH[^\n]*\n(.*?)\n={5,}[^\n]*\n(.*?)\n>{5,}\s*REPLACE",
    re.DOTALL,
)


def _parse(text: str) -> dict:
    edits = [{"search": m.group(1), "replace": m.group(2)}
             for m in _EDIT_RX.finditer(text)]
    first = _EDIT_RX.search(text)
    msg   = text[: first.start()].strip() if first else text.strip()
    return {"message": msg or "Done.", "edits": edits}


def _apply(content: str, edits: list) -> dict:
    c = content.replace("\r", "")
    for e in edits:
        s = e["search"].replace("\r", "")
        r = e["replace"].replace("\r", "")
        if not s or s not in c:
            return {"ok": False, "content": content,
                    "error": f"SEARCH text not found:\n{s}"}
        c = c.replace(s, r, 1)
    return {"ok": True, "content": c, "error": None}


# ---------------------------------------------------------------------------
# LLM client -- Ollama now; set USE_VLLM=true + LLM_BASE_URL to switch
# Returns (content, perf_dict | None).  perf_dict contains Ollama's native
# timing and token stats; it is None when using vLLM (not exposed there).
# ---------------------------------------------------------------------------
async def _llm(system: str, messages: list,
               images: list[str] | None = None) -> tuple[str, dict | None]:
    """Call the active model.  images is a list of base64-encoded image strings
    (for vision models); they are attached to the last user message."""
    model = _active_model()
    msgs  = [{"role": "system", "content": system}] + messages

    # Attach images to the last user message when provided
    if images and msgs and msgs[-1].get("role") == "user":
        msgs[-1] = {**msgs[-1], "images": images}

    # Use a larger context window for image-to-LaTeX tasks so the model can
    # output a complete document without being cut off.
    num_ctx = 16384 if images else 8192

    # FIX #10: separate connect vs. read timeout.
    async with httpx.AsyncClient(timeout=_LLM_TIMEOUT) as client:
        if USE_VLLM:
            # vLLM exposes the OpenAI-compatible endpoint
            r = await client.post(f"{LLM_BASE_URL}/v1/chat/completions", json={
                "model": model, "messages": msgs,
                "temperature": 0.1, "stream": False,
            })
        else:
            # Ollama native endpoint (supports num_ctx for context window)
            r = await client.post(f"{OLLAMA_URL}/api/chat", json={
                "model": model, "messages": msgs, "stream": False,
                "options": {"temperature": 0.1, "num_ctx": num_ctx},
            })
        r.raise_for_status()
        data = r.json()
        content = (data["choices"][0]["message"]["content"] if USE_VLLM
                   else data["message"]["content"])

        # Extract Ollama's native performance counters from the response body.
        # These are only present on the Ollama path (stream:false returns them
        # in the single response object).  vLLM does not expose them.
        perf: dict | None = None
        if not USE_VLLM:
            eval_dur_ns  = data.get("eval_duration") or 0
            total_dur_ns = data.get("total_duration") or 0
            gen_tok      = data.get("eval_count") or 0
            perf = {
                "model":         data.get("model", model),
                "prompt_tokens": data.get("prompt_eval_count") or 0,
                "gen_tokens":    gen_tok,
                "total_ms":      round(total_dur_ns / 1_000_000),
                "tokens_per_sec": (round(gen_tok / (eval_dur_ns / 1e9), 1)
                                   if eval_dur_ns > 0 else 0),
            }
        return content, perf


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
_CHAT_SYS = """\
You edit a LaTeX document by making small, targeted text replacements.

When the user asks for a change, reply with ONE short sentence then one or more edit blocks:

<<<<<<< SEARCH
snippet copied verbatim from the current file
=======
the replacement snippet
>>>>>>> REPLACE

Always include ALL THREE marker lines AND the SEARCH snippet.
The snippet must appear verbatim in the file and be unique enough to identify the spot.

Worked example. User: "change the section title to Methods". The file contains:
\\section{{Introduction}}
Your reply:
Changed the section title to Methods.
<<<<<<< SEARCH
\\section{{Introduction}}
=======
\\section{{Methods}}
>>>>>>> REPLACE

Rules:
- Produce valid LaTeX; escape special chars (& % $ # _) in inserted text.
- Do NOT output the whole file, do NOT use code fences.
- If no change is requested, reply in plain text with NO edit blocks.

Current document.tex:
{document}"""

# Prompt used when the user uploads an image and wants it recreated as LaTeX.
# No SEARCH/REPLACE — the model outputs the whole document from scratch.
_CREATE_SYS = """\
You are a LaTeX expert. Recreate the document shown in the image as a complete, compilable LaTeX source file.

Rules:
- Output ONLY raw LaTeX, starting with \\documentclass and ending with \\end{{document}}.
- Do NOT wrap in markdown code fences (no ``` or ```latex).
- Do NOT add any explanation, commentary, or preamble text before \\documentclass.
- Reproduce the layout, structure, fonts, and content as faithfully as possible.
- Use only standard packages (geometry, fontenc, inputenc, amsmath, graphicx, etc.).
- The document must compile cleanly with pdflatex -no-shell-escape."""

_FIX_SYS = """\
Fix the LaTeX compile error below using SEARCH/REPLACE blocks only. Do not output the whole file.

<<<<<<< SEARCH
verbatim text from the file
=======
corrected text
>>>>>>> REPLACE

document.tex:
{document}

pdflatex error:
{error}"""

# Strips ```latex ... ``` or ``` ... ``` fences that vision models sometimes add.
_FENCE_RX = re.compile(r"^```[a-zA-Z]*\n(.*?)\n```\s*$", re.DOTALL)

def _strip_fences(text: str) -> str:
    m = _FENCE_RX.match(text.strip())
    return m.group(1).strip() if m else text.strip()


# ---------------------------------------------------------------------------
# Helper: build a JSONResponse and attach a session cookie when needed
# ---------------------------------------------------------------------------
def _resp(data: dict, sid: str, is_new: bool, status: int = 200) -> JSONResponse:
    r = JSONResponse(content=data, status_code=status)
    _set_cookie(r, sid, is_new)
    return r


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
async def root(request: Request, session_id: Optional[str] = Cookie(None)):
    if not await _rate_limit(request, _RL_API): return _too_many()
    sid, is_new = _resolve(session_id)
    _init_session(sid)
    if is_new or not _pdf(sid).exists():
        await _compile(sid)          # ensure PDF is ready before returning HTML
    # FIX #15: serve from in-memory cache instead of reading the file every time.
    resp = Response(_index_html, media_type="text/html; charset=utf-8")
    _set_cookie(resp, sid, is_new)
    return resp


@app.get("/api/pdf")
async def get_pdf(request: Request, download: str = "", session_id: Optional[str] = Cookie(None)):
    if not await _rate_limit(request, _RL_API): return _too_many()
    # FIX #3: validate UUID before using it as a path component.
    if not _valid_sid(session_id) or not _pdf(session_id).exists():  # type: ignore[arg-type]
        return Response(status_code=404, content="No PDF yet.")
    headers = {"Cache-Control": "no-store"}
    if download:
        headers["Content-Disposition"] = 'attachment; filename="resume.pdf"'
    return FileResponse(str(_pdf(session_id)), media_type="application/pdf",  # type: ignore[arg-type]
                        headers=headers)


@app.get("/api/source")
async def get_source(request: Request, session_id: Optional[str] = Cookie(None)):
    if not await _rate_limit(request, _RL_API): return _too_many()
    # FIX #3: validate UUID.
    tex = _tex(session_id) if _valid_sid(session_id) else None  # type: ignore[arg-type]
    content = tex.read_text(encoding="utf-8") if tex and tex.exists() else ""
    return JSONResponse({"content": content})


@app.post("/api/source")
async def post_source(request: Request, session_id: Optional[str] = Cookie(None)):
    if not await _rate_limit(request, _RL_COMPILE): return _too_many()
    if not _valid_sid(session_id):
        return JSONResponse({"ok": False, "error": "No session."}, status_code=400)
    body    = await request.json()
    content = body.get("content", "")
    # FIX #13: reject runaway payloads before writing to disk.
    if len(content.encode("utf-8")) > MAX_SOURCE_BYTES:
        return JSONResponse({"ok": False,
                             "error": f"Source too large (max {MAX_SOURCE_BYTES // 1000} KB)."})
    _tex(session_id).write_text(content, encoding="utf-8")  # type: ignore[arg-type]
    c = await _compile(session_id)  # type: ignore[arg-type]
    if not c["ok"]:
        await _revert(session_id)  # type: ignore[arg-type]
    return JSONResponse({"ok": c["ok"], "error": c["error"]})


@app.post("/api/reset")
async def reset(request: Request, session_id: Optional[str] = Cookie(None)):
    if not await _rate_limit(request, _RL_COMPILE): return _too_many()
    if not _valid_sid(session_id):
        return JSONResponse({"ok": False, "error": "No session."}, status_code=400)
    names = _tnames()
    tmpl  = _cur_tmpl(session_id, names)  # type: ignore[arg-type]
    if tmpl:
        shutil.copy2(TEMPLATES_DIR / f"{tmpl}.tex", _tex(session_id))  # type: ignore[arg-type]
    c = await _compile(session_id)  # type: ignore[arg-type]
    return JSONResponse({"ok": c["ok"], "error": c["error"]})


@app.get("/api/templates")
async def get_templates(request: Request, session_id: Optional[str] = Cookie(None)):
    if not await _rate_limit(request, _RL_API): return _too_many()
    names = _tnames()
    return JSONResponse({
        "templates": [{"name": n, "label": _tlabel(n)} for n in names],
        "current":   _cur_tmpl(session_id or "", names) if _valid_sid(session_id) else "",  # type: ignore[arg-type]
    })


@app.post("/api/template")
async def post_template(request: Request, session_id: Optional[str] = Cookie(None)):
    if not await _rate_limit(request, _RL_COMPILE): return _too_many()
    sid, is_new = _resolve(session_id)
    body = await request.json()
    name = str(body.get("name", ""))
    if not _load_tmpl(sid, name):
        return _resp({"ok": False, "error": f"Unknown template: {name}"}, sid, is_new)
    c = await _compile(sid)
    return _resp({"ok": c["ok"], "error": c["error"]}, sid, is_new)


@app.get("/api/models")
async def get_models(request: Request):
    if not await _rate_limit(request, _RL_API): return _too_many()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=4, read=4, write=4, pool=4)) as client:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
            r.raise_for_status()
            models  = [m["name"] for m in r.json().get("models", [])]
            running = True
    except Exception:
        models, running = [], False
    return JSONResponse({"models": models, "current": _active_model(), "running": running})


@app.post("/api/model")
async def post_model(request: Request):
    if not await _rate_limit(request, _RL_API): return _too_many()
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        return JSONResponse({"ok": False, "error": "No model name provided."})
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=4, read=4, write=4, pool=4)) as client:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
            r.raise_for_status()
            installed = [m["name"] for m in r.json().get("models", [])]
        # Normalize: Ollama stores tag-less pulls as "model:latest".
        # Accept bare names (e.g. "phi4") that match a "phi4:latest" entry.
        installed_norm = set(installed)
        for m in installed:
            if m.endswith(":latest"):
                installed_norm.add(m[: -len(":latest")])
        if name not in installed_norm:
            return JSONResponse({"ok": False,
                                 "error": f"Model '{name}' is not installed. "
                                          f"Run: ollama pull {name}"})
        _set_model(name)
        return JSONResponse({"ok": True, "error": None})
    except Exception:
        return JSONResponse({"ok": False, "error": "Ollama isn't running."})


@app.get("/api/metrics")
async def get_metrics(request: Request):
    """Return global AI performance metrics + aggregate summary (all sessions)."""
    if not await _rate_limit(request, _RL_API): return _too_many()

    entries: list[dict] = _metrics
    if not entries:
        return JSONResponse({"entries": [], "summary": {}})

    total = len(entries)

    # Compile success rate (only entries that attempted a compile)
    attempted  = [e for e in entries if e.get("compile_ok") is not None]
    compiled   = [e for e in attempted if e["compile_ok"] is True]
    compile_rate = (round(len(compiled) / len(attempted) * 100, 1)
                    if attempted else None)

    # Average wall-clock response time
    elapsed_vals = [e["elapsed_ms"] for e in entries if e.get("elapsed_ms")]
    avg_elapsed_ms = round(sum(elapsed_vals) / len(elapsed_vals)) if elapsed_vals else 0

    # Average token generation speed
    tok_s_vals = [e["tokens_per_sec"] for e in entries if e.get("tokens_per_sec")]
    avg_tok_s  = round(sum(tok_s_vals) / len(tok_s_vals), 1) if tok_s_vals else 0

    # Total tokens generated this session
    total_tokens = sum(e.get("gen_tokens", 0) for e in entries)

    # Per-model breakdown
    seen_models: dict[str, list[dict]] = {}
    for e in entries:
        m = e.get("model", "unknown")
        seen_models.setdefault(m, []).append(e)
    by_model = []
    for model_name, mes in sorted(seen_models.items(),
                                  key=lambda kv: len(kv[1]), reverse=True):
        m_elapsed = [e["elapsed_ms"] for e in mes if e.get("elapsed_ms")]
        m_tok_s   = [e["tokens_per_sec"] for e in mes if e.get("tokens_per_sec")]
        by_model.append({
            "model":              model_name,
            "count":              len(mes),
            "avg_elapsed_ms":     round(sum(m_elapsed)/len(m_elapsed)) if m_elapsed else 0,
            "avg_tokens_per_sec": round(sum(m_tok_s)/len(m_tok_s), 1) if m_tok_s else 0,
        })

    summary = {
        "total":              total,
        "compile_rate":       compile_rate,
        "avg_elapsed_ms":     avg_elapsed_ms,
        "avg_tokens_per_sec": avg_tok_s,
        "total_tokens":       total_tokens,
        "by_model":           by_model,
    }
    return JSONResponse({"entries": entries[-50:], "summary": summary})


@app.post("/api/chat")
async def chat(request: Request, session_id: Optional[str] = Cookie(None)):
    if not await _rate_limit(request, _RL_CHAT): return _too_many()
    sid, is_new = _resolve(session_id)
    _init_session(sid)

    t0: float = time.monotonic()
    llm_perf: dict | None = None

    body     = await request.json()
    messages = body.get("messages", [])[-20:]
    images   = body.get("images") or []   # list of base64 strings from image upload

    # Guard against oversized image payloads
    total_img_bytes = sum(len(b) for b in images)
    if total_img_bytes > MAX_IMAGE_BYTES:
        return _resp(
            {"message": "", "updated": False,
             "error": f"Image too large ({total_img_bytes // 1_000_000} MB). Max ~6 MB."},
            sid, is_new, status=400,
        )

    tex      = _tex(sid)
    document = tex.read_text(encoding="utf-8") if tex.exists() else ""

    # ── inline helper: record metric + build response ─────────────────────
    def _finish(data: dict, *, edit_ok: bool, compile_ok: bool | None) -> JSONResponse:
        elapsed_ms = round((time.monotonic() - t0) * 1000)
        entry: dict = {
            "ts":            round(time.time()),
            "model":         (llm_perf or {}).get("model", _active_model()),
            "elapsed_ms":    elapsed_ms,
            "prompt_tokens": (llm_perf or {}).get("prompt_tokens", 0),
            "gen_tokens":    (llm_perf or {}).get("gen_tokens", 0),
            "tokens_per_sec":(llm_perf or {}).get("tokens_per_sec", 0),
            "edit_ok":       edit_ok,
            "compile_ok":    compile_ok,
        }
        _record_metric(entry)
        perf_out = {
            "elapsed_s":      round(elapsed_ms / 1000, 1),
            "gen_tokens":     entry["gen_tokens"],
            "tokens_per_sec": entry["tokens_per_sec"],
            "compile_ok":     compile_ok,
        }
        return _resp({**data, "perf": perf_out}, sid, is_new)

    # ══════════════════════════════════════════════════════════════════════
    # IMAGE-TO-LaTeX CREATE MODE
    # When images are attached the model generates a complete new document
    # from scratch — no SEARCH/REPLACE, full document output.
    # ══════════════════════════════════════════════════════════════════════
    if images:
        try:
            raw, llm_perf = await _llm(_CREATE_SYS, messages, images=images)
        except Exception:
            model = _active_model()
            return _resp(
                {"message": "", "updated": False,
                 "error": (f"Can't reach the model at {LLM_BASE_URL}. "
                           f"Make sure Ollama is running and the active model supports vision "
                           f"(llava, minicpm-v, moondream). Try: ollama run llava")},
                sid, is_new, status=500,
            )

        latex = _strip_fences(raw)

        # Sanity-check: must look like LaTeX
        if "\\documentclass" not in latex and "\\begin{document}" not in latex:
            return _finish(
                {"message": ("The model didn't return LaTeX. "
                             "Make sure you're using a vision-capable model "
                             "(llava, minicpm-v, moondream) and try again.\n\n"
                             + raw[:500]),
                 "updated": False, "error": None},
                edit_ok=False, compile_ok=None,
            )

        tex.write_text(latex, encoding="utf-8")
        c = await _compile(sid)

        if c["ok"]:
            shutil.copy2(tex, _last(sid))
            return _finish(
                {"message": "Document recreated from image and compiled successfully.", "updated": True, "error": None},
                edit_ok=True, compile_ok=True,
            )
        # Try one auto-fix pass on compile failure
        broken     = tex.read_text(encoding="utf-8")
        fix_system = _FIX_SYS.format(document=broken, error=c["error"])
        try:
            fr, _ = await _llm(fix_system,
                               [{"role": "user", "content": "Fix the compile error."}])
            fp = _parse(fr)
            if fp["edits"]:
                fa = _apply(broken, fp["edits"])
                if fa["ok"]:
                    tex.write_text(fa["content"], encoding="utf-8")
                    c = await _compile(sid)
                    if c["ok"]:
                        shutil.copy2(tex, _last(sid))
                        return _finish(
                            {"message": "Document recreated from image (with auto-fix) and compiled.", "updated": True, "error": None},
                            edit_ok=True, compile_ok=True,
                        )
        except Exception:
            pass
        await _revert(sid)
        return _finish(
            {"message": (f"Generated LaTeX from image but it wouldn't compile. "
                         f"Try a different vision model or edit the source manually.\n\n{c['error']}"),
             "updated": False, "error": None},
            edit_ok=True, compile_ok=False,
        )

    # ══════════════════════════════════════════════════════════════════════
    # STANDARD EDIT MODE  (SEARCH/REPLACE)
    # ══════════════════════════════════════════════════════════════════════
    system = _CHAT_SYS.format(document=document)

    try:
        reply, llm_perf = await _llm(system, messages)
    except Exception:
        model = _active_model()
        return _resp(
            {"message": "", "updated": False,
             "error": f"Can't reach the model at {LLM_BASE_URL}. "
                      f"Is Ollama running? Try: ollama run {model}"},
            sid, is_new, status=500,
        )

    parsed = _parse(reply)

    if not parsed["edits"]:
        return _finish(
            {"message": parsed["message"], "updated": False, "error": None},
            edit_ok=False, compile_ok=None,
        )

    # Apply edits; one corrective retry when SEARCH text isn't found
    applied = _apply(document, parsed["edits"])
    if not applied["ok"]:
        try:
            retry = messages + [
                {"role": "assistant", "content": reply},
                {"role": "user",
                 "content": "Your SEARCH text was not found. "
                            "Copy the exact text from the current document.tex (shown above) "
                            "and try again."},
            ]
            r2, _ = await _llm(system, retry)
            p2 = _parse(r2)
            if p2["edits"]:
                a2 = _apply(document, p2["edits"])
                if a2["ok"]:
                    applied, parsed = a2, p2
        except Exception:
            pass

    if not applied["ok"]:
        return _finish(
            {"message": f"{parsed['message']}\n\n"
                        "[!] Couldn't locate the text to change. Try rephrasing.",
             "updated": False, "error": None},
            edit_ok=False, compile_ok=None,
        )

    # Write + compile; one AI repair attempt on compile failure
    tex.write_text(applied["content"], encoding="utf-8")
    c = await _compile(sid)

    if not c["ok"]:
        broken     = tex.read_text(encoding="utf-8")
        fix_system = _FIX_SYS.format(document=broken, error=c["error"])
        try:
            fr, _ = await _llm(fix_system,
                               [{"role": "user", "content": "Fix the compile error."}])
            fp = _parse(fr)
            if fp["edits"]:
                fa = _apply(broken, fp["edits"])
                if fa["ok"]:
                    tex.write_text(fa["content"], encoding="utf-8")
                    c = await _compile(sid)
        except Exception:
            pass

    if not c["ok"]:
        await _revert(sid)
        msg = (f"{parsed['message']}\n\n"
               f"[!] That change wouldn't compile, so I kept the previous version.\n"
               f"{c['error']}")
        return _finish({"message": msg, "updated": False, "error": None},
                       edit_ok=True, compile_ok=False)

    return _finish({"message": parsed["message"], "updated": True, "error": None},
                   edit_ok=True, compile_ok=True)
