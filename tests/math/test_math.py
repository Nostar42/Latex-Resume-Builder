"""
test_math.py — Math Solver test suite
======================================
Sends a progression of math problems to the /api/chat endpoint in "math" mode,
from basic arithmetic up to definite integrals.  Each problem is sent as a
fresh single-turn conversation so one bad model response can't poison the next.

Usage:
    py tests/test_math.py                  # uses default http://localhost:5000
    py tests/test_math.py --url http://localhost:5000
    py tests/test_math.py --model phi4     # override model first

Results are printed to stdout; a summary table is printed at the end.
Exit code 0 if all tests pass, 1 if any fail.
"""
from __future__ import annotations
import argparse, json, re, sys, time
from pathlib import Path
import urllib.request, urllib.error

# Force UTF-8 output so Unicode box-drawing / check characters print correctly
# on Windows consoles that default to cp1252.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://localhost:5000"

# ---------------------------------------------------------------------------
# Math problems: (label, prompt, keywords that must appear in the response)
# The keyword list is intentionally loose — we check that the AI produced a
# plausible answer, not that it matches a single numeric string exactly.
# ---------------------------------------------------------------------------
PROBLEMS = [
    # ── Basic arithmetic ────────────────────────────────────────────────────
    ("Addition",           "What is 347 + 589?",                           ["936"]),
    ("Subtraction",        "What is 1024 - 377?",                          ["647"]),
    ("Multiplication",     "What is 48 × 76?",                             ["3648"]),
    ("Long division",      "What is 9856 ÷ 32?",                           ["308"]),
    ("PEMDAS / order",     "Evaluate: 3 + 4 × 2 − (6 ÷ 3)",               ["9"]),

    # ── Fractions & decimals ────────────────────────────────────────────────
    ("Fraction add",       "Simplify: 3/8 + 5/12",                         ["19/24"]),
    ("Fraction multiply",  "Simplify: (2/3) × (9/14)",                     ["3/7"]),
    ("Decimal multiply",   "What is 3.14 × 2.5?",                          ["7.85"]),
    ("Percentage",         "What is 17.5% of 240?",                        ["42"]),

    # ── Algebra ─────────────────────────────────────────────────────────────
    ("Linear equation",    "Solve for x: 3x + 7 = 22",                     ["5"]),
    ("Two-variable",       "Solve the system: 2x + y = 10,  x − y = 2",    ["4", "2"]),
    ("Quadratic formula",  "Find the roots of x² − 5x + 6 = 0",            ["2", "3"]),
    ("Factoring",          "Factor completely: x² − 9",                    ["(x-3)(x+3)", "x+3", "x-3"]),
    ("Inequality",         "Solve for x: 2x − 3 < 7",                      ["x < 5", "x<5"]),
    ("Absolute value",     "Solve |2x − 4| = 6",                           ["5", "-1"]),

    # ── Exponents & logarithms ──────────────────────────────────────────────
    ("Exponent rules",     "Simplify: (x³)² × x⁻¹",                       ["x^5", "x5"]),
    ("Log evaluation",     "Evaluate: log₂(64)",                           ["6"]),
    ("Natural log",        "Solve for x: ln(x) = 3",                       ["e^3", "e3", "20.09"]),
    ("Log equation",       "Solve: log(x) + log(x−3) = 1",                 ["5"]),

    # ── Trigonometry ────────────────────────────────────────────────────────
    ("Basic trig",         "What is sin(30°)?",                            ["1/2", "0.5"]),
    ("Pythagorean id",     "Simplify: sin²θ + cos²θ",                      ["1"]),
    ("Trig equation",      "Solve for θ in [0°,360°]: 2cos(θ) = √2",       ["45", "315"]),

    # ── Pre-calculus ─────────────────────────────────────────────────────────
    ("Limits (basic)",     "Find the limit as x→2 of (x² − 4)/(x − 2)",    ["4"]),
    ("Limit at infinity",  "Find the limit as x→∞ of (3x² + 1)/(x² − 5)",  ["3"]),

    # ── Derivatives ─────────────────────────────────────────────────────────
    ("Power rule",         "Find d/dx of x⁴",                              ["4x^3", "4x3"]),
    ("Product rule",       "Find d/dx of x²·sin(x)",                       ["2x", "cos"]),
    ("Chain rule",         "Find d/dx of sin(3x²)",                        ["6x", "cos"]),
    ("Derivative app",     "Find the derivative of f(x) = e^(2x) + ln(x)", ["2e^{2x}", "2e^2x", "1/x"]),

    # ── Integrals ───────────────────────────────────────────────────────────
    ("Indefinite integral",    "Find ∫ x³ dx",                             ["x^4/4", "x4/4"]),
    ("Indefinite trig",        "Find ∫ cos(x) dx",                         ["sin"]),
    ("U-substitution",         "Find ∫ 2x·e^(x²) dx",                     ["e^{x^2}", "e^x^2", "e^(x²)"]),
    ("Definite integral",      "Evaluate ∫₀² (3x² + 1) dx",               ["10"]),
    ("Definite trig integral", "Evaluate ∫₀^π sin(x) dx",                  ["2"]),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _post(url: str, payload: dict, session_cookie: str | None) -> tuple[dict, str]:
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            **({"Cookie": session_cookie} if session_cookie else {}),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body  = json.loads(resp.read())
        # Capture Set-Cookie for the next request
        cookie = resp.headers.get("Set-Cookie", session_cookie or "")
        if "session_id=" in cookie:
            # Keep only the session_id= part
            cookie = next(p for p in cookie.split(";") if "session_id=" in p).strip()
    return body, cookie


def _new_session(base: str) -> str:
    """Hit the root to create a fresh session; return the cookie string."""
    req = urllib.request.Request(base + "/", method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.headers.get("Set-Cookie", "")
    for part in raw.split(";"):
        part = part.strip()
        if part.startswith("session_id="):
            return part
    raise RuntimeError("Server did not return a session_id cookie.")


def _get_source(base: str, cookie: str) -> str:
    """Fetch the current session's LaTeX source via GET /api/source."""
    req = urllib.request.Request(
        base + "/api/source",
        headers={"Cookie": cookie},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read()).get("content", "")


def _check(text: str, keywords: list[str]) -> bool:
    """Return True if at least one keyword appears in the text (case-insensitive)."""
    lower = text.lower()
    return any(kw.lower() in lower for kw in keywords)


def _truncate(s: str, n: int = 120) -> str:
    s = s.replace("\n", " ").strip()
    return s[:n] + "…" if len(s) > n else s


def _save_pdf(base: str, cookie: str, dest: Path) -> bool:
    """Download the session's current PDF and write it to dest. Returns True on success."""
    try:
        req = urllib.request.Request(
            base + "/api/pdf",
            headers={"Cookie": cookie},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                return False
            dest.write_bytes(resp.read())
        return True
    except Exception:
        return False


def _safe_filename(label: str) -> str:
    """Turn a test label into a safe filename (no special chars, spaces → underscores)."""
    return re.sub(r"[^\w\-]", "_", label).strip("_") + ".pdf"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run(base: str, binder: Path | None = None) -> int:
    print(f"\n{'═'*70}")
    print(f"  Math Solver Test  —  {base}")
    print(f"{'═'*70}\n")

    # Warm up: make sure the server is up and the model endpoint works
    try:
        with urllib.request.urlopen(base + "/api/models", timeout=10) as r:
            info = json.loads(r.read())
        print(f"  Active model : {info['current']}")
        print(f"  Ollama live  : {info['running']}")
        if not info["running"]:
            print("\n  ERROR: Ollama is not running. Start it and retry.\n")
            return 1
    except Exception as exc:
        print(f"\n  ERROR: Cannot reach server — {exc}\n")
        return 1

    if binder:
        binder.mkdir(parents=True, exist_ok=True)
        print(f"  Binder       : {binder.resolve()}")
    print(f"  Problems     : {len(PROBLEMS)}\n")

    results: list[tuple[str, bool, float, str]] = []   # (label, pass, elapsed_s, snippet)
    saved_pdfs: list[str] = []

    for i, (label, prompt, keywords) in enumerate(PROBLEMS, 1):
        # Fresh session per problem → no cross-contamination
        try:
            cookie = _new_session(base)
        except Exception as exc:
            results.append((label, False, 0.0, f"Session error: {exc}"))
            continue

        t0 = time.monotonic()
        try:
            resp, cookie = _post(
                base + "/api/chat",
                {"messages": [{"role": "user", "content": prompt}], "mode": "math"},
                cookie,
            )
            elapsed = time.monotonic() - t0
            error   = resp.get("error") or ""
            if error:
                passed  = False
                snippet = _truncate(error)
            elif resp.get("updated"):
                # The answer lives in the compiled LaTeX source, not the message.
                source  = _get_source(base, cookie)
                passed  = _check(source, keywords)
                snippet = ("answer found in source" if passed
                           else f"keywords {keywords} not found in source")
            else:
                passed  = False
                snippet = _truncate(resp.get("message", "no response"))
        except Exception as exc:
            elapsed = time.monotonic() - t0
            passed  = False
            snippet = str(exc)

        # Save PDF to binder regardless of answer correctness — if the server
        # compiled a PDF (updated=True), capture it.  Name: NN_Label.pdf
        pdf_note = ""
        if binder and resp.get("updated"):
            fname = f"{i:02d}_{_safe_filename(label)}"
            dest  = binder / fname
            if _save_pdf(base, cookie, dest):
                saved_pdfs.append(fname)
                pdf_note = f"  → {fname}"
            else:
                pdf_note = "  → PDF save failed"

        icon = "✓" if passed else "✗"
        print(f"  {icon}  [{elapsed:5.1f}s]  {label:<28}  {snippet}{pdf_note}")
        results.append((label, passed, elapsed, snippet))

        # Stay well under the rate limit (10 AI req/min → 6 s gap minimum).
        # Tests already take time so we only sleep if we went unusually fast.
        if elapsed < 6:
            time.sleep(6 - elapsed)

    # ── Summary ──────────────────────────────────────────────────────────────
    passed_n = sum(1 for _, p, _, _ in results if p)
    total    = len(results)
    avg_s    = sum(e for _, _, e, _ in results) / total if total else 0

    print(f"\n{'─'*70}")
    print(f"  Result  : {passed_n}/{total} passed")
    print(f"  Avg time: {avg_s:.1f}s per problem")

    if passed_n < total:
        print("\n  Failed problems:")
        for label, passed, _, snippet in results:
            if not passed:
                print(f"    ✗  {label}: {snippet}")

    if binder and saved_pdfs:
        print(f"\n  PDFs saved ({len(saved_pdfs)}/{total}) → {binder.resolve()}")
        for name in saved_pdfs:
            print(f"    {name}")

    print(f"{'═'*70}\n")
    return 0 if passed_n == total else 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    _default_binder = str(Path(__file__).parent / "Math Test")
    ap = argparse.ArgumentParser()
    ap.add_argument("--url",    default=BASE,           help="Base URL of the server")
    ap.add_argument("--binder", default=_default_binder, help="Folder to save PDFs into")
    args = ap.parse_args()
    sys.exit(run(args.url, binder=Path(args.binder)))
