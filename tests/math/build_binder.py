"""
build_binder.py — Assemble the Math Test PDF binder
=====================================================
Reads the per-problem PDFs and results.json from the Math Test folder,
generates a stats cover page, and merges everything into one PDF.

Usage:
    py tests/math/build_binder.py
    py tests/math/build_binder.py --binder "tests/math/Math Test" --out "Math Test Binder.pdf"

Requires: pypdf, reportlab  (pip install pypdf reportlab)
"""
from __future__ import annotations
import argparse, json, math, sys, time
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pypdf import PdfWriter, PdfReader
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
_HERE         = Path(__file__).parent
DEFAULT_BINDER = _HERE / "Math Test"
DEFAULT_OUT    = _HERE / "Math Test Binder.pdf"

# ---------------------------------------------------------------------------
# Stats page builder (reportlab)
# ---------------------------------------------------------------------------

def _teal():
    return colors.HexColor("#008080")

def _lightgray():
    return colors.HexColor("#f4f4f4")

def _darkgray():
    return colors.HexColor("#333333")


def build_stats_page(results: dict, out_path: Path) -> None:
    """Render a single-PDF stats cover page from results.json data."""
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=letter,
        leftMargin=0.85*inch, rightMargin=0.85*inch,
        topMargin=0.8*inch,   bottomMargin=0.8*inch,
    )

    styles = getSampleStyleSheet()
    teal   = _teal()
    dgray  = _darkgray()

    title_style = ParagraphStyle(
        "Title", parent=styles["Title"],
        fontSize=22, textColor=teal, spaceAfter=4,
        alignment=TA_CENTER,
    )
    sub_style = ParagraphStyle(
        "Sub", parent=styles["Normal"],
        fontSize=11, textColor=dgray, spaceAfter=2,
        alignment=TA_CENTER,
    )
    label_style = ParagraphStyle(
        "Label", parent=styles["Normal"],
        fontSize=9, textColor=colors.white, fontName="Helvetica-Bold",
    )
    value_style = ParagraphStyle(
        "Value", parent=styles["Normal"],
        fontSize=9, textColor=dgray,
    )
    section_style = ParagraphStyle(
        "Section", parent=styles["Normal"],
        fontSize=11, textColor=teal, fontName="Helvetica-Bold",
        spaceBefore=14, spaceAfter=6,
    )

    # ── Derived stats ────────────────────────────────────────────────────────
    total      = results.get("total", 0)
    passed     = results.get("passed", 0)
    failed     = total - passed
    pct        = round(passed / total * 100, 1) if total else 0
    avg_s      = results.get("avg_elapsed_s", 0)
    model      = results.get("model", "unknown")
    run_ts     = results.get("run_ts", 0)
    run_dt     = datetime.fromtimestamp(run_ts).strftime("%Y-%m-%d  %H:%M:%S") if run_ts else "—"
    problems   = results.get("problems", [])

    story = []

    # ── Header ───────────────────────────────────────────────────────────────
    story.append(Spacer(1, 0.1*inch))
    story.append(Paragraph("Math Solver Test", title_style))
    story.append(Paragraph("AI Performance Binder", sub_style))
    story.append(Paragraph(f"Model: <b>{model}</b>  ·  Run: {run_dt}", sub_style))
    story.append(Spacer(1, 0.15*inch))
    story.append(HRFlowable(width="100%", thickness=2, color=teal))
    story.append(Spacer(1, 0.15*inch))

    # ── Summary tiles ────────────────────────────────────────────────────────
    tile_data = [
        [
            Paragraph("PROBLEMS", label_style),
            Paragraph("PASSED", label_style),
            Paragraph("FAILED", label_style),
            Paragraph("PASS RATE", label_style),
            Paragraph("AVG TIME", label_style),
        ],
        [
            Paragraph(str(total),        ParagraphStyle("V", fontSize=18, textColor=teal, fontName="Helvetica-Bold", alignment=TA_CENTER)),
            Paragraph(str(passed),       ParagraphStyle("V", fontSize=18, textColor=colors.HexColor("#2e7d32"), fontName="Helvetica-Bold", alignment=TA_CENTER)),
            Paragraph(str(failed),       ParagraphStyle("V", fontSize=18, textColor=colors.HexColor("#c62828"), fontName="Helvetica-Bold", alignment=TA_CENTER)),
            Paragraph(f"{pct}%",         ParagraphStyle("V", fontSize=18, textColor=teal, fontName="Helvetica-Bold", alignment=TA_CENTER)),
            Paragraph(f"{avg_s}s",       ParagraphStyle("V", fontSize=18, textColor=dgray, fontName="Helvetica-Bold", alignment=TA_CENTER)),
        ],
    ]
    tile_style = TableStyle([
        ("BACKGROUND",   (0,0), (-1,0), teal),
        ("BACKGROUND",   (0,1), (-1,1), _lightgray()),
        ("ALIGN",        (0,0), (-1,-1), "CENTER"),
        ("VALIGN",       (0,0), (-1,-1), "MIDDLE"),
        ("GRID",         (0,0), (-1,-1), 0.5, colors.white),
        ("ROWBACKGROUNDS",(0,0),(-1,-1), [teal, _lightgray()]),
        ("TOPPADDING",   (0,0), (-1,-1), 8),
        ("BOTTOMPADDING",(0,0), (-1,-1), 8),
        ("LEFTPADDING",  (0,0), (-1,-1), 6),
        ("RIGHTPADDING", (0,0), (-1,-1), 6),
        ("ROUNDEDCORNERS", (0,0), (-1,-1), 4),
    ])
    col_w = (doc.width) / 5
    story.append(Table(tile_data, colWidths=[col_w]*5, style=tile_style))
    story.append(Spacer(1, 0.2*inch))

    # ── Per-problem results table ─────────────────────────────────────────────
    story.append(Paragraph("Problem Results", section_style))
    story.append(HRFlowable(width="100%", thickness=0.5, color=_teal()))
    story.append(Spacer(1, 0.08*inch))

    hdr = ["#", "Problem", "Prompt", "Time", "Result"]
    rows = [hdr]
    for p in problems:
        icon   = "✓" if p["passed"] else "✗"
        result = Paragraph(
            f'<font color="{"#2e7d32" if p["passed"] else "#c62828"}"><b>{icon}</b></font>',
            ParagraphStyle("ic", fontSize=10, alignment=TA_CENTER),
        )
        rows.append([
            str(p["num"]),
            Paragraph(p["label"],  ParagraphStyle("lbl", fontSize=8, textColor=dgray)),
            Paragraph(p["prompt"][:70] + ("…" if len(p["prompt"]) > 70 else ""),
                      ParagraphStyle("prm", fontSize=7, textColor=colors.HexColor("#555555"))),
            f'{p["elapsed_s"]}s',
            result,
        ])

    tbl_style = TableStyle([
        ("BACKGROUND",    (0,0), (-1,0), teal),
        ("TEXTCOLOR",     (0,0), (-1,0), colors.white),
        ("FONTNAME",      (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",      (0,0), (-1,0), 8),
        ("ALIGN",         (0,0), (-1,-1), "LEFT"),
        ("ALIGN",         (0,0), (0,-1), "CENTER"),
        ("ALIGN",         (3,0), (4,-1), "CENTER"),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("ROWBACKGROUNDS",(0,1), (-1,-1), [colors.white, _lightgray()]),
        ("GRID",          (0,0), (-1,-1), 0.3, colors.HexColor("#dddddd")),
        ("TOPPADDING",    (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ("LEFTPADDING",   (0,0), (-1,-1), 5),
        ("RIGHTPADDING",  (0,0), (-1,-1), 5),
        ("FONTSIZE",      (0,1), (-1,-1), 8),
    ])
    col_widths = [0.28*inch, 1.2*inch, 3.4*inch, 0.55*inch, 0.55*inch]
    story.append(Table(rows, colWidths=col_widths, style=tbl_style, repeatRows=1))

    # ── Footer note ───────────────────────────────────────────────────────────
    story.append(Spacer(1, 0.2*inch))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cccccc")))
    story.append(Paragraph(
        f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}  ·  "
        f"LaTeX Resume Builder — AI Testing Center  ·  "
        f"PDFs compiled via pdflatex (MiKTeX)",
        ParagraphStyle("foot", fontSize=7, textColor=colors.HexColor("#999999"), alignment=TA_CENTER, spaceBefore=6),
    ))

    doc.build(story)


# ---------------------------------------------------------------------------
# Binder assembler
# ---------------------------------------------------------------------------

def build_binder(binder_dir: Path, out_path: Path) -> int:
    results_file = binder_dir / "results.json"
    if not results_file.exists():
        print(f"ERROR: {results_file} not found. Run test_math.py first.")
        return 1

    results = json.loads(results_file.read_text(encoding="utf-8"))

    # Collect numbered PDFs in order
    pdf_files = sorted(
        [f for f in binder_dir.glob("*.pdf") if f.name[0].isdigit()],
        key=lambda p: int(p.stem.split("_")[0])
    )
    if not pdf_files:
        print(f"ERROR: No numbered PDFs found in {binder_dir}")
        return 1

    print(f"\n  Binder source : {binder_dir.resolve()}")
    print(f"  PDFs found    : {len(pdf_files)}")
    print(f"  Output        : {out_path.resolve()}")
    print(f"  Model         : {results.get('model', '?')}")
    print(f"  Score         : {results.get('passed')}/{results.get('total')}\n")

    # 1. Render stats cover page to a temp PDF
    stats_pdf = binder_dir / "_stats_cover.pdf"
    print("  Generating stats cover page…")
    build_stats_page(results, stats_pdf)
    print(f"  Stats page → {stats_pdf.name}")

    # 2. Merge: stats cover + all numbered problem PDFs
    writer = PdfWriter()

    for src in [stats_pdf] + pdf_files:
        reader = PdfReader(str(src))
        for page in reader.pages:
            writer.add_page(page)
        print(f"  + {src.name}  ({len(reader.pages)} page{'s' if len(reader.pages)!=1 else ''})")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as fh:
        writer.write(fh)

    # Clean up temp stats PDF
    stats_pdf.unlink(missing_ok=True)

    size_kb = round(out_path.stat().st_size / 1024)
    print(f"\n  ✓  Binder complete → {out_path.resolve()}  ({size_kb} KB, {len(pdf_files)+1} pages total)\n")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--binder", default=str(DEFAULT_BINDER), help="Folder containing numbered PDFs + results.json")
    ap.add_argument("--out",    default=str(DEFAULT_OUT),    help="Output binder PDF path")
    args = ap.parse_args()
    sys.exit(build_binder(Path(args.binder), Path(args.out)))
