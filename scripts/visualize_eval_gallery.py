#!/usr/bin/env python3
"""Multi-poster evaluation gallery: validate the image-native density/layout metrics.

Runs the deterministic detectors + scoring on several posters and writes ONE HTML
comparison page: a summary table + per-poster content-occupancy overlay (empty
cells shaded, largest void boxed) + OCR boxes + scores + a short auto-analysis,
so you can eyeball whether density/layout rank posters the way you'd expect.

Usage:
    uv run python scripts/visualize_eval_gallery.py \
        --candidate P018=out/runs/<id>/final/preview.png \
        --candidate P083=out/runs/<id2>/final/preview.png \
        --out-dir out/eval/gallery
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PIL import Image

from autodesign.evaluator import poster_rubric as PR
from autodesign.evaluator.ocr import run_ocr
from autodesign.evaluator.quality_rubric import (
    compute_deterministic_report,
    _score_density,
    _score_layout,
)
from autodesign.evaluator.spatial import blank_strips, content_occupancy, occupancy_overlay
from autodesign.evaluator.viz import image_b64, ocr_overlay
from autodesign.util.browser_render import screenshot_html


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    candidates = [_parse_candidate(v) for v in args.candidate]

    cards: list[dict[str, Any]] = []
    for name, path in candidates:
        print(f"  evaluating {name} ...", file=sys.stderr)
        cdir = out_dir / "items" / name
        report = compute_deterministic_report(paper=None, candidate_artifact=path, out_dir=cdir, profile=args.profile)
        preview = report.get("preview_image")
        if not preview or not Path(preview).exists():
            print(f"  (skipped {name}: no preview)", file=sys.stderr)
            continue
        base = Image.open(preview).convert("RGB")
        segments = (run_ocr(preview, include_segments=True) or {}).get("segments") or []
        occ = content_occupancy(base, segments=segments)
        strips = blank_strips(base)
        occ_png = cdir / "occupancy.png"
        ocr_png = cdir / "ocr.png"
        occupancy_overlay(base, occ, strips=strips).save(occ_png)
        ocr_overlay(base, segments).save(ocr_png)
        cards.append(_card(name, report, occ_png, ocr_png))

    if not cards:
        print("error: no candidates produced a preview", file=sys.stderr)
        return 2
    cards.sort(key=lambda c: (c["content"] if c["content"] is not None else -1))

    dist = _calib_distribution(args.calib_cache) if args.calib_cache else None
    html_path = out_dir / "gallery.html"
    html_path.write_text(_build_html(cards, dist), encoding="utf-8")
    report_png = out_dir / "gallery.png"
    result = screenshot_html(
        html_path, report_png,
        viewport_width=1180, viewport_height=2200, full_page=True, max_edge=3800, timeout_ms=30_000,
    )
    print(f"wrote {html_path}")
    print(f"rendered gallery image: {report_png}" if result.paths and report_png.exists()
          else f"(gallery render warnings: {result.warnings})")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--candidate", action="append", required=True, help="name=/path (png/pdf/run_dir). Repeatable.")
    p.add_argument("--out-dir", required=True, type=Path)
    p.add_argument("--profile", default=None)
    p.add_argument("--calib-cache", type=Path, default=None, help="calib cache.json to summarize the score distribution.")
    return p.parse_args(argv)


def _calib_distribution(cache_path: Path) -> dict[str, Any] | None:
    if not cache_path or not cache_path.exists():
        return None
    import statistics as st

    data = json.loads(cache_path.read_text(encoding="utf-8"))
    ds: list[float] = []
    ls: list[float] = []
    for it in data.get("real", []):
        occ = {"content_coverage": it.get("content"), "largest_blank_strip_ratio": it.get("strip")}
        ocr = {"available": it.get("text_cov") is not None, "text_coverage_ratio": it.get("text_cov")}
        d, _ = _score_density(occ, ocr)
        ln, _ = _score_layout(occ)
        if d is not None:
            ds.append(d)
        if ln is not None:
            ls.append(ln)

    def summ(x: list[float]) -> dict[str, Any]:
        x = sorted(x)
        n = len(x)
        return {
            "n": n, "min": x[0], "p25": x[n // 4], "median": x[n // 2], "p75": x[(3 * n) // 4],
            "max": x[-1], "mean": round(st.mean(x), 2), "std": round(st.pstdev(x), 2),
            "saturated_top": sum(1 for v in x if v >= 9.5), "floored": sum(1 for v in x if v <= 0.5),
        }

    return {"density": summ(ds), "layout": summ(ls)} if ds else None


def _parse_candidate(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise SystemExit("--candidate must be name=/path")
    name, raw = value.split("=", 1)
    return name.strip() or "candidate", Path(raw).expanduser()


def _card(name: str, report: dict[str, Any], occ_png: Path, ocr_png: Path) -> dict[str, Any]:
    sp = report.get("spatial", {})
    bs = report.get("blank_strips", {})
    ocr = report.get("ocr", {})
    comps = report.get("dimension_components", {})

    def score(key: str) -> Any:
        return (comps.get(key) or {}).get("score_0_10")

    content = sp.get("content_coverage")
    strip = bs.get("largest_blank_strip_ratio")
    strip_area = bs.get("blank_strip_area_ratio")
    return {
        "name": name,
        "canvas": report.get("canvas"),
        "content": content,
        "strip": strip,
        "strip_area": strip_area,
        "text_cov": ocr.get("text_coverage_ratio") if ocr.get("available") else None,
        "density": score("information_density_and_synthesis"),
        "layout": score("layout_readability"),
        "occ_png": occ_png,
        "ocr_png": ocr_png,
        "analysis": _analysis(content, strip, strip_area),
    }


def _analysis(content: Any, strip: Any, strip_area: Any) -> str:
    notes: list[str] = []
    cc = float(content or 0)
    sa = float(strip_area or 0)
    ls = float(strip or 0)
    if cc < 0.5:
        notes.append("mostly empty / broken")
    elif cc < 0.7:
        notes.append("under-filled")
    elif cc < 0.85:
        notes.append("well-filled")
    else:
        notes.append("dense")
    if ls >= 0.10:
        notes.append(f"large blank strip {ls:.2f}")
    elif sa >= 0.30:
        notes.append(f"many blank strips ({sa:.2f} area)")
    else:
        notes.append("no major blank strip")
    return "; ".join(notes)


def _params_html() -> str:
    return f"""<h2>Calibrated parameters &amp; scoring</h2>
<p class='muted'>Calibrated by scripts/calibrate_density_layout.py over 100 stratified real
posters + 9 corner cases (most stable config: all corner targets satisfied, good
spread, low saturation).</p>
<div class='formula'>
content_norm = min(1, content_coverage / {PR.REF_CONTENT_COVERAGE}) &nbsp;·&nbsp;
text_norm = min(1, text_coverage / {PR.REF_TEXT_COVERAGE_RATIO})<br>
<b>density</b> = 10 · ({PR.DENSITY_CONTENT_WEIGHT}·content_norm + {PR.DENSITY_TEXT_WEIGHT}·max(text_norm, content_norm))
 · (1 − {PR.DENSITY_VOID_PENALTY_K}·min(1, strip/{PR.EMPTY_RECT_CAP}))<br>
<b>layout</b> = 10 · content_norm · (1 − {PR.LAYOUT_VOID_PENALTY_K}·min(1, strip/{PR.LAYOUT_VOID_CAP}))<br>
<span class='muted'>strip = largest blank strip (heading band + outer page margin excluded); content/strip from the
image-native occupancy grid; resolution-invariant.</span>
</div>"""


def _dist_html(dist: dict[str, Any] | None) -> str:
    if not dist:
        return ""
    def row(name: str, d: dict[str, Any]) -> str:
        return (f"<tr><td>{name}</td><td>{d['min']}</td><td>{d['p25']}</td><td>{d['median']}</td>"
                f"<td>{d['p75']}</td><td>{d['max']}</td><td>{d['mean']}</td><td>{d['std']}</td>"
                f"<td>{d['saturated_top']}</td><td>{d['floored']}</td></tr>")
    return f"""<h2>Score distribution on the 100-poster calibration sample</h2>
<table>
  <tr><th>dim</th><th>min</th><th>p25</th><th>median</th><th>p75</th><th>max</th><th>mean</th><th>std</th><th>≥9.5</th><th>≤0.5</th></tr>
  {row('density', dist['density'])}
  {row('layout', dist['layout'])}
</table>
<p class='muted'>Full 0–10 range used; near-zero saturation at the top confirms the metric
discriminates rather than clipping.</p>"""


def _build_html(cards: list[dict[str, Any]], dist: dict[str, Any] | None = None) -> str:
    def esc(v: Any) -> str:
        return html.escape(str(v))

    def cell(v: Any) -> str:
        return "—" if v is None else (f"{v:.3f}" if isinstance(v, float) else esc(v))

    rows = "\n".join(
        f"<tr><td>{esc(c['name'])}</td><td>{cell(c['content'])}</td><td>{cell(c['strip'])}</td>"
        f"<td>{cell(c['strip_area'])}</td><td class='sc'>{cell(c['density'])}</td>"
        f"<td class='sc'>{cell(c['layout'])}</td><td>{esc(c['analysis'])}</td></tr>"
        for c in cards
    )
    cards_html = "\n".join(_card_html(c, esc) for c in cards)
    return f"""<!doctype html><html><head><meta charset='utf-8'><style>
  body {{ font-family:-apple-system,Segoe UI,Roboto,sans-serif; margin:24px; color:#1a1a1a; background:#fafafa; max-width:1140px; }}
  h1 {{ font-size:22px; }} h2 {{ font-size:16px; }}
  table {{ border-collapse:collapse; width:100%; margin:12px 0 24px; font-size:13px; }}
  th,td {{ border:1px solid #ddd; padding:6px 9px; text-align:left; }}
  th {{ background:#f0f2f5; }} td.sc {{ font-weight:700; }}
  .card {{ background:#fff; border:1px solid #e3e3e3; border-radius:10px; padding:14px 16px; margin:14px 0; }}
  .imgs {{ display:flex; gap:14px; }}
  .imgs figure {{ margin:0; }} .imgs img {{ width:300px; border:1px solid #ddd; border-radius:6px; }}
  figcaption {{ font-size:11px; color:#777; text-align:center; }}
  .meta {{ font-size:13px; line-height:1.7; }}
  code {{ background:#f0f0f3; padding:1px 5px; border-radius:4px; font-size:12px; }}
  .big {{ font-size:20px; font-weight:700; }}
  .muted {{ color:#777; font-size:12px; }}
</style></head><body>
<h1>Final density / layout report — image-native, calibrated</h1>
{_params_html()}
{_dist_html(dist)}
<h2>Per-poster detection &amp; scores</h2>
<p class='muted'>Sorted by content coverage (emptiest → densest). Detection overlay legend:
<b>gray</b> = heading band (excluded) · <b>green outline</b> = content cell · <b>light red</b> = empty cell ·
<b>strong red + box</b> = blank strip / largest void (the section-bottom gaps).</p>
<table>
  <tr><th>poster</th><th>content_cov</th><th>largest_strip</th><th>strip_area</th><th>density /10</th><th>layout /10</th><th>auto-analysis</th></tr>
  {rows}
</table>
{cards_html}
<p class='muted'>source_faithfulness needs the paper and is N/A in this gallery.</p>
</body></html>"""


def _card_html(c: dict[str, Any], esc) -> str:
    return f"""<div class='card'>
  <h2>{esc(c['name'])} <span class='muted'>canvas {esc(c['canvas'])}</span></h2>
  <div class='imgs'>
    <figure><img src='data:image/png;base64,{image_b64(c['occ_png'])}'><figcaption>content occupancy: red=empty, box=largest void</figcaption></figure>
    <figure><img src='data:image/png;base64,{image_b64(c['ocr_png'])}'><figcaption>OCR text boxes</figcaption></figure>
    <div class='meta'>
      content coverage = <code>{c['content']}</code> · text coverage = <code>{c['text_cov']}</code><br>
      largest blank strip = <code>{c['strip']}</code> · strip area = <code>{c['strip_area']}</code><br>
      <br>density = <span class='big'>{esc(c['density'])}</span>/10 ·
      layout = <span class='big'>{esc(c['layout'])}</span>/10<br>
      <span class='muted'>{esc(c['analysis'])}</span>
    </div>
  </div>
</div>"""


if __name__ == "__main__":
    raise SystemExit(main())
