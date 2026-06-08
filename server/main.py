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
MAX_SOURCE_BYTES = 500_000  # 500 KB -- guard against runaway uploads

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
# ---------------------------------------------------------------------------
async def _llm(system: str, messages: list) -> str:
    model = _active_model()
    msgs  = [{"role": "system", "content": system}] + messages

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
                "options": {"temperature": 0.1, "num_ctx": 8192},
            })
        r.raise_for_status()
        data = r.json()
        return (data["choices"][0]["message"]["content"] if USE_VLLM
                else data["message"]["content"])


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
_CHAT_SYS = """\
You edit a LaTeX resume by making small, targeted text replacements.

When the user asks for a change, reply with ONE short sentence then one or more edit blocks:

<<<<<<< SEARCH
snippet copied verbatim from the current file
=======
the replacement snippet
>>>>>>> REPLACE

Always include ALL THREE marker lines AND the SEARCH snippet.
The snippet must appear verbatim in the file and be unique enough to identify the spot.

Worked example. User: "change the job title to Data Analyst". The file contains:
{{\\large\\bfseries Job Title $|$ Professional Field}}
Your reply:
Changed the job title to Data Analyst.
<<<<<<< SEARCH
Job Title $|$ Professional Field
=======
Data Analyst $|$ Professional Field
>>>>>>> REPLACE

Rules:
- Produce valid LaTeX; escape special chars (& % $ # _) in inserted text.
- Do NOT output the whole file, do NOT use code fences.
- If no change is requested, reply in plain text with NO edit blocks.

Current resume.tex:
{resume}"""

_FIX_SYS = """\
Fix the LaTeX compile error below using SEARCH/REPLACE blocks only. Do not output the whole file.

<<<<<<< SEARCH
verbatim text from the file
=======
corrected text
>>>>>>> REPLACE

resume.tex:
{resume}

pdflatex error:
{error}"""


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
        if name not in installed:
            return JSONResponse({"ok": False,
                                 "error": f"Model '{name}' is not installed. "
                                          f"Run: ollama pull {name}"})
        _set_model(name)
        return JSONResponse({"ok": True, "error": None})
    except Exception:
        return JSONResponse({"ok": False, "error": "Ollama isn't running."})


@app.post("/api/chat")
async def chat(request: Request, session_id: Optional[str] = Cookie(None)):
    if not await _rate_limit(request, _RL_CHAT): return _too_many()
    sid, is_new = _resolve(session_id)
    _init_session(sid)

    body     = await request.json()
    messages = body.get("messages", [])[-20:]   # cap at 20 to avoid context overflow

    tex    = _tex(sid)
    resume = tex.read_text(encoding="utf-8") if tex.exists() else ""
    system = _CHAT_SYS.format(resume=resume)

    # Call the LLM
    try:
        reply = await _llm(system, messages)
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
        return _resp({"message": parsed["message"], "updated": False, "error": None},
                     sid, is_new)

    # Apply edits; one corrective retry when SEARCH text isn't found
    applied = _apply(resume, parsed["edits"])
    if not applied["ok"]:
        try:
            retry = messages + [
                {"role": "assistant", "content": reply},
                {"role": "user",
                 "content": "Your SEARCH text was not found. "
                            "Copy the exact text from the current resume.tex (shown above) "
                            "and try again."},
            ]
            r2 = await _llm(system, retry)
            p2 = _parse(r2)
            if p2["edits"]:
                a2 = _apply(resume, p2["edits"])
                if a2["ok"]:
                    applied, parsed = a2, p2
        except Exception:
            pass

    if not applied["ok"]:
        return _resp(
            {"message": f"{parsed['message']}\n\n"
                        "[!] Couldn't locate the text to change. Try rephrasing.",
             "updated": False, "error": None},
            sid, is_new,
        )

    # Write + compile; one AI repair attempt on compile failure
    tex.write_text(applied["content"], encoding="utf-8")
    c = await _compile(sid)

    if not c["ok"]:
        broken     = tex.read_text(encoding="utf-8")
        fix_system = _FIX_SYS.format(resume=broken, error=c["error"])
        try:
            fr = await _llm(fix_system,
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
        return _resp({"message": msg, "updated": False, "error": None}, sid, is_new)

    return _resp({"message": parsed["message"], "updated": True, "error": None}, sid, is_new)
