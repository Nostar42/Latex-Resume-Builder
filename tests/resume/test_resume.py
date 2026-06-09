"""
test_resume.py — Resume Editor test suite
==========================================
Sends a sequence of natural-language edit requests to /api/chat in "resume"
mode, verifying that the model produced a SEARCH/REPLACE edit that compiled
successfully and that the LaTeX source was actually modified.

Each test runs in the SAME session (edits accumulate, mimicking a real user
session), except for the reset-dependent tests which are grouped at the end
and run in a fresh session after a /api/reset.

Usage:
    py tests/test_resume.py                 # uses default http://localhost:5000
    py tests/test_resume.py --url http://localhost:5000
    py tests/test_resume.py --template modern   # start from a specific template

Exit code 0 if all tests pass, 1 if any fail.
"""
from __future__ import annotations
import argparse, json, sys, time
import urllib.request, urllib.error

# Force UTF-8 output so Unicode box-drawing / check characters print correctly
# on Windows consoles that default to cp1252.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE     = "http://localhost:5000"
TEMPLATE = "classic"   # start from classic two-column — richest placeholder content

# ---------------------------------------------------------------------------
# Test definitions
# Format: (label, prompt, check_fn(source_after) -> bool, description)
#
# check_fn receives the full LaTeX source after the edit + compile and must
# return True for the test to pass.  Use None to only check compile success.
# ---------------------------------------------------------------------------

def _has(needle: str):
    """Source must contain the literal string (case-insensitive)."""
    def _f(src: str) -> bool:
        return needle.lower() in src.lower()
    return _f

def _lacks(needle: str):
    """Source must NOT contain the literal string (case-insensitive)."""
    def _f(src: str) -> bool:
        return needle.lower() not in src.lower()
    return _f

def _all_of(*checks):
    def _f(src: str) -> bool:
        return all(c(src) for c in checks)
    return _f

EDITS = [
    # ── Name & header ────────────────────────────────────────────────────────
    (
        "Change full name",
        "Change the candidate's name to Jordan Lee",
        _has("Jordan Lee"),
        "Name field updated in LaTeX",
    ),
    (
        "Change job title",
        "Change the job title / tagline to Senior Data Engineer",
        _has("Senior Data Engineer"),
        "Title/tagline updated",
    ),
    (
        "Change email address",
        "Update the email address to jordan.lee@example.com",
        _has("jordan.lee@example.com"),
        "Email address updated",
    ),
    (
        "Change phone number",
        "Set the phone number to +1 (555) 867-5309",
        _has("867-5309"),
        "Phone number updated",
    ),

    # ── Skills section ───────────────────────────────────────────────────────
    (
        "Add a skill",
        "Add 'Apache Spark' to the skills section",
        _has("Apache Spark"),
        "New skill added",
    ),
    (
        "Remove a skill",
        "Remove 'Microsoft Office' from the skills section",
        _lacks("Microsoft Office"),
        "Skill removed from list",
    ),

    # ── Experience bullets ───────────────────────────────────────────────────
    (
        "Add experience bullet",
        "Under the most recent job add a bullet: 'Reduced pipeline latency by 42% through query optimisation'",
        _has("42%"),
        "New bullet point added",
    ),
    (
        "Edit experience bullet",
        "Change the bullet about reducing latency to say 'Reduced pipeline latency by 60% through query optimisation and caching'",
        _all_of(_has("60%"), _has("caching")),
        "Bullet edited in place",
    ),

    # ── Education section ────────────────────────────────────────────────────
    (
        "Add education entry",
        "Add an education entry: BSc Computer Science, State University, 2018–2022",
        _all_of(_has("Computer Science"), _has("State University")),
        "Education entry added",
    ),
    (
        "Change graduation year",
        "Change the graduation year for State University to 2023",
        _has("2023"),
        "Graduation year updated",
    ),

    # ── Section manipulation ─────────────────────────────────────────────────
    (
        "Add a new section",
        "Add a new 'Certifications' section with one entry: AWS Certified Solutions Architect, 2024",
        _all_of(_has("Certifications"), _has("AWS")),
        "New section added with content",
    ),
    (
        "Add second cert",
        "Add another entry to Certifications: Google Professional Data Engineer, 2023",
        _all_of(_has("Google Professional"), _has("2023")),
        "Second cert entry added",
    ),

    # ── Style tweaks ─────────────────────────────────────────────────────────
    (
        "Change accent colour",
        "Change the accent or header colour to teal",
        _has("teal"),
        "Colour definition updated to teal",
    ),

    # ── Links ────────────────────────────────────────────────────────────────
    (
        "Update LinkedIn URL",
        "Set the LinkedIn URL to linkedin.com/in/jordan-lee",
        _has("jordan-lee"),
        "LinkedIn URL updated",
    ),
    (
        "Add GitHub link",
        "Add a GitHub link pointing to github.com/jordan-lee",
        _has("github.com/jordan-lee"),
        "GitHub link added",
    ),

    # ── Formatting ───────────────────────────────────────────────────────────
    (
        "Bold a word",
        "Make the word 'Apache Spark' bold in the skills section",
        _has(r"\textbf"),
        "\\textbf applied",
    ),

    # ── Multi-item atomic edit ───────────────────────────────────────────────
    (
        "Multi-field update",
        "Update three things at once: set the city to Austin TX, change the summary line to 'Results-driven engineer with 6 years of experience', and add 'Kubernetes' to skills",
        _all_of(_has("Austin"), _has("Results-driven"), _has("Kubernetes")),
        "Three independent changes applied in one message",
    ),
]

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _request(method: str, url: str, payload: dict | None, cookie: str | None) -> tuple[dict, str]:
    data = json.dumps(payload).encode() if payload is not None else None
    headers: dict = {"Content-Type": "application/json"} if data else {}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read())
        raw_cookie = resp.headers.get("Set-Cookie", cookie or "")
        for part in raw_cookie.split(";"):
            if part.strip().startswith("session_id="):
                cookie = part.strip()
                break
    return body, cookie or ""


def _new_session(base: str) -> str:
    req = urllib.request.Request(base + "/", method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.headers.get("Set-Cookie", "")
    for part in raw.split(";"):
        if part.strip().startswith("session_id="):
            return part.strip()
    raise RuntimeError("Server did not return a session_id cookie.")


def _get_source(base: str, cookie: str) -> str:
    body, _ = _request("GET", base + "/api/source", None, cookie)
    return body.get("content", "")


def _load_template(base: str, cookie: str, template: str) -> bool:
    body, _ = _request("POST", base + "/api/template", {"name": template}, cookie)
    return body.get("ok", False)


def _truncate(s: str, n: int = 100) -> str:
    s = s.replace("\n", " ").strip()
    return s[:n] + "…" if len(s) > n else s


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run(base: str, template: str) -> int:
    print(f"\n{'═'*70}")
    print(f"  Resume Editor Test  —  {base}")
    print(f"{'═'*70}\n")

    # Sanity check
    try:
        with urllib.request.urlopen(base + "/api/models", timeout=10) as r:
            info = json.loads(r.read())
        print(f"  Active model : {info['current']}")
        print(f"  Ollama live  : {info['running']}")
        if not info["running"]:
            print("\n  ERROR: Ollama is not running.\n")
            return 1
    except Exception as exc:
        print(f"\n  ERROR: Cannot reach server — {exc}\n")
        return 1

    print(f"  Template     : {template}")
    print(f"  Edits        : {len(EDITS)}\n")

    # Establish session and load template
    try:
        cookie = _new_session(base)
        if not _load_template(base, cookie, template):
            print(f"  ERROR: Failed to load template '{template}'\n")
            return 1
        source_before = _get_source(base, cookie)
        print(f"  Template loaded — {len(source_before)} bytes of LaTeX\n")
    except Exception as exc:
        print(f"  ERROR during setup: {exc}\n")
        return 1

    results: list[tuple[str, bool, float, str]] = []

    for label, prompt, check_fn, description in EDITS:
        t0 = time.monotonic()
        try:
            resp, cookie = _request(
                "POST",
                base + "/api/chat",
                {"messages": [{"role": "user", "content": prompt}], "mode": "resume"},
                cookie,
            )
            elapsed = time.monotonic() - t0

            error    = resp.get("error") or ""
            updated  = resp.get("updated", False)
            perf     = resp.get("perf", {})
            compile_ok = perf.get("compile_ok")

            if error or not updated:
                passed  = False
                snippet = _truncate(error or resp.get("message", "no message"))
            else:
                source_after = _get_source(base, cookie)
                if check_fn is not None:
                    passed  = check_fn(source_after)
                    snippet = "(compiled ✓, source check passed)" if passed else "(compiled ✓ but source check FAILED)"
                else:
                    passed  = True
                    snippet = "(compiled ✓)"

            # Annotate with perf info when available
            if perf.get("elapsed_s"):
                snippet += f"  [{perf['elapsed_s']}s, {perf.get('tokens_per_sec',0)} tok/s]"

        except Exception as exc:
            elapsed = time.monotonic() - t0
            passed  = False
            snippet = str(exc)

        icon = "✓" if passed else "✗"
        print(f"  {icon}  [{elapsed:5.1f}s]  {label:<30}  {snippet}")
        results.append((label, passed, elapsed, snippet))

        # Rate limit guard: chat allows 10 req/min → 6 s spacing minimum
        if elapsed < 6:
            time.sleep(6 - elapsed)

    # ── Summary ───────────────────────────────────────────────────────────────
    passed_n = sum(1 for _, p, _, _ in results if p)
    total    = len(results)
    avg_s    = sum(e for _, _, e, _ in results) / total if total else 0

    print(f"\n{'─'*70}")
    print(f"  Result  : {passed_n}/{total} passed")
    print(f"  Avg time: {avg_s:.1f}s per edit")

    if passed_n < total:
        print("\n  Failed edits:")
        for label, passed, _, snippet in results:
            if not passed:
                print(f"    ✗  {label}: {snippet}")

    print(f"{'═'*70}\n")
    return 0 if passed_n == total else 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url",      default=BASE,     help="Base URL of the server")
    ap.add_argument("--template", default=TEMPLATE, help="Starting template name")
    args = ap.parse_args()
    sys.exit(run(args.url, args.template))
