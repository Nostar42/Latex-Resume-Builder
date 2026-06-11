"""
run_model_comparison.py — Run the math test against multiple models
====================================================================
Fetches all installed text models from the server, filters out any in
EXCLUDE, then for each:
  1. Switches the server to that model via /api/model
  2. Runs test_math.py saving PDFs to  "Math Test - <model>/"
  3. Runs build_binder.py to produce    "Math Test - <model>.pdf"

Prints a comparison leaderboard at the end.

Usage:
    py tests/math/run_model_comparison.py
    py tests/math/run_model_comparison.py --url http://localhost:5000
    py tests/math/run_model_comparison.py --exclude codellama:34b phi4
"""
from __future__ import annotations
import argparse, json, subprocess, sys, time, urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).parent
BASE = "http://localhost:5000"

# Default models to skip (passed via --exclude to override)
DEFAULT_EXCLUDE = ["codellama:34b"]


def _get_installed_models(base: str) -> list[str]:
    """Return installed text-mode models from the server catalog."""
    with urllib.request.urlopen(base + "/api/models", timeout=10) as r:
        data = json.loads(r.read())
    return [
        m["name"] for m in data.get("models", [])
        if m.get("installed") and m.get("mode") == "text"
    ]


def _switch_model(base: str, model: str) -> bool:
    req = urllib.request.Request(
        base + "/api/model",
        data=json.dumps({"name": model}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        resp = json.loads(r.read())
    return bool(resp.get("ok"))


def _binder_slug(model: str) -> str:
    return model.replace(":", "-").replace("/", "-")


def run(base: str, exclude: list[str]) -> None:
    print(f"\n{'═'*70}")
    print(f"  Model Comparison — Math Solver Test")
    print(f"  Server : {base}")
    print(f"{'═'*70}\n")

    # Fetch installed text models from the server
    try:
        models = _get_installed_models(base)
    except Exception as exc:
        print(f"ERROR: Cannot reach server — {exc}")
        sys.exit(1)

    # Normalise exclude list: strip :latest for comparison
    norm = lambda n: n[:-7] if n.endswith(":latest") else n
    exclude_norm = {norm(e) for e in exclude}
    models = [m for m in models if norm(m) not in exclude_norm]

    if not models:
        print("ERROR: No installed text models found (after exclusions).")
        sys.exit(1)

    print(f"  Models to test ({len(models)}):")
    for m in models:
        print(f"    • {m}")
    if exclude:
        print(f"  Excluded: {', '.join(exclude)}")
    print()

    # Verify Ollama is running
    try:
        with urllib.request.urlopen(base + "/api/models", timeout=10) as r:
            info = json.loads(r.read())
        if not info.get("running"):
            print("ERROR: Ollama is not running.")
            sys.exit(1)
    except Exception as exc:
        print(f"ERROR: Cannot reach server — {exc}")
        sys.exit(1)

    scorecard: list[dict] = []

    for model in models:
        slug        = _binder_slug(model)
        binder_dir  = HERE / f"Math Test - {slug}"
        binder_pdf  = HERE / f"Math Test - {slug}.pdf"

        print(f"\n{'─'*70}")
        print(f"  Model : {model}")
        print(f"{'─'*70}")

        # Switch model
        try:
            ok = _switch_model(base, model)
        except Exception as exc:
            print(f"  SKIP — could not switch model: {exc}")
            scorecard.append({"model": model, "passed": None, "total": None, "avg_s": None, "skipped": True})
            continue
        if not ok:
            print(f"  SKIP — model not installed (run: ollama pull {model})")
            scorecard.append({"model": model, "passed": None, "total": None, "avg_s": None, "skipped": True})
            continue

        print(f"  Switched to {model} ✓")
        # Give Ollama a moment to evict the previous model from VRAM
        time.sleep(3)

        # Run test_math.py
        result = subprocess.run(
            [sys.executable, str(HERE / "test_math.py"),
             "--url", base,
             "--binder", str(binder_dir)],
            check=False,
        )

        # Build the binder PDF
        if (binder_dir / "results.json").exists():
            subprocess.run(
                [sys.executable, str(HERE / "build_binder.py"),
                 "--binder", str(binder_dir),
                 "--out",    str(binder_pdf)],
                check=False,
            )
            results = json.loads((binder_dir / "results.json").read_text(encoding="utf-8"))
            scorecard.append({
                "model":   model,
                "passed":  results.get("passed"),
                "total":   results.get("total"),
                "avg_s":   results.get("avg_elapsed_s"),
                "skipped": False,
            })
        else:
            scorecard.append({"model": model, "passed": 0, "total": 33, "avg_s": None, "skipped": False})

    # ── Leaderboard ───────────────────────────────────────────────────────────
    print(f"\n\n{'═'*70}")
    print(f"  LEADERBOARD — Math Solver ({scorecard[0]['total'] if scorecard else 33} problems)")
    print(f"{'═'*70}")
    print(f"  {'Model':<28} {'Score':>8}  {'Pass %':>7}  {'Avg time':>9}  Binder")
    print(f"  {'─'*28} {'─'*8}  {'─'*7}  {'─'*9}  {'─'*20}")

    ranked = sorted(
        [s for s in scorecard if not s["skipped"] and s["passed"] is not None],
        key=lambda s: (s["passed"] / s["total"] if s["total"] else 0, -(s["avg_s"] or 999)),
        reverse=True,
    )
    for s in ranked:
        pct   = round(s["passed"] / s["total"] * 100, 1) if s["total"] else 0
        avg   = f"{s['avg_s']}s" if s["avg_s"] else "—"
        slug  = _binder_slug(s["model"])
        bname = f"Math Test - {slug}.pdf"
        print(f"  {s['model']:<28} {s['passed']:>3}/{s['total']:<4}  {pct:>6.1f}%  {avg:>9}  {bname}")

    for s in scorecard:
        if s["skipped"]:
            print(f"  {s['model']:<28}   SKIPPED")

    print(f"{'═'*70}\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=BASE)
    ap.add_argument("--exclude", nargs="*", default=DEFAULT_EXCLUDE,
                    help="Model names to skip (default: codellama:34b)")
    args = ap.parse_args()
    run(args.url, args.exclude)
