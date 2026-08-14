#!/usr/bin/env python3
"""Before/after report for the density+layout algorithm change.

Documents the move from non-white-ink-based scoring (v1) to image-native
content-occupancy scoring (v2): what signals changed, how the formulas changed,
and the score impact on real posters. Both old and new scores are recomputed in
this one script so the comparison is reproducible (v1 formulas are reproduced
inline; v2 comes from the live evaluator).

Usage:
    uv run python scripts/visualize_metric_changes.py \
        --candidate P018=out/runs/<id>/final/preview.png ... --out-dir out/eval/changes
"""

from __future__ import annotations

import argparse
import html
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PIL import Image

from autodesign.evaluator.metrics import image_density_metrics
from autodesign.evaluator.ocr import run_ocr
from autodesign.evaluator.quality_rubric import compute_deterministic_report
from autodesign.evaluator.schema import ArtifactSnapshot
from autodesign.evaluator.spatial import content_occupancy, occupancy_overlay
from autodesign.evaluator.viz import density_overlay, image_b64
from autodesign.util.browser_render import screenshot_html


# --- v1 (old) formulas, reproduced for the comparison -----------------------
V1_REF_NONWHITE = 0.23
V1_REF_TEXT = 0.30
V1_BLANK_FLOOR = 0.08
V1_BLANK_K = 0.5


def _v1_density(nonwhite: float, text_cov: float, blank: float) -> float:
    ink = min(1.0, nonwhite / V1_REF_NONWHITE)
    text = min(1.0, text_cov / V1_REF_TEXT)
    base = (ink + text) / 2
    factor = 1.0
    if blank > V1_BLANK_FLOOR:
        factor = 1.0 - V1_BLANK_K * min(1.0, (blank - V1_BLANK_FLOOR) / V1_BLANK_FLOOR)
    return round(max(0.0, min(10.0, 10.0 * base * factor)), 2)


def _v1_layout(blank: float, edge: float) -> float:
    blank_comp = max(0.0, 1.0 - blank / 0.25)
    edge_comp = min(1.0, edge / 0.12)
    return round(max(0.0, min(10.0, 10.0 * (blank_comp + edge_comp) / 2)), 2)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--candidate", action="append", required=True, help="name=/path")
    p.add_argument("--out-dir", required=True, type=Path)
    args = p.parse_args()
    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    for item in args.candidate:
        name, raw = item.split("=", 1)
        path = Path(raw).expanduser()
        base = Image.open(path).convert("RGB")

        # v1 inputs
        snap = ArtifactSnapshot(artifact_path=str(path), artifact_kind="image", preview_image=str(path))
        img = image_density_metrics(snap, artifact_type="poster")[0].metrics
        ocr = run_ocr(path, include_segments=True)
        segs = ocr.get("segments") or []
        nonwhite = float(img.get("nonwhite_pixel_ratio") or 0)
        blank = float(img.get("longest_blank_vertical_run_ratio") or 0)
        edge = float(img.get("edge_density") or 0)
        text_cov = float(ocr.get("text_coverage_ratio") or 0)
        old_d = _v1_density(nonwhite, text_cov, blank)
        old_l = _v1_layout(blank, edge)

        # v2 (live)
        r = compute_deterministic_report(paper=None, candidate_artifact=path, out_dir=out_dir / "items" / name)
        sp = r.get("spatial", {})
        comps = r.get("dimension_components", {})
        new_d = (comps.get("information_density_and_synthesis") or {}).get("score_0_10")
        new_l = (comps.get("layout_readability") or {}).get("score_0_10")

        old_png = out_dir / "items" / name / "old_overlay.png"
        new_png = out_dir / "items" / name / "new_overlay.png"
        old_png.parent.mkdir(parents=True, exist_ok=True)
        density_overlay(base)[0].save(old_png)
        occupancy_overlay(base, content_occupancy(base, segments=segs)).save(new_png)

        rows.append({
            "name": name, "nonwhite": nonwhite, "blank": blank, "text_cov": text_cov,
            "content": sp.get("content_coverage"), "void": sp.get("largest_empty_rect_cell_ratio"),
            "old_d": old_d, "new_d": new_d, "old_l": old_l, "new_l": new_l,
            "old_png": old_png, "new_png": new_png,
        })

    rows.sort(key=lambda x: (x["content"] if x["content"] is not None else -1))
    html_path = out_dir / "changes.html"
    html_path.write_text(_build_html(rows), encoding="utf-8")
    report_png = out_dir / "changes.png"
    result = screenshot_html(html_path, report_png, viewport_width=1200, viewport_height=2200,
                             full_page=True, max_edge=4000, timeout_ms=30_000)
    print(f"wrote {html_path}")
    print(f"rendered: {report_png}" if result.paths and report_png.exists() else f"(warnings: {result.warnings})")
    return 0


def _delta(old: Any, new: Any) -> str:
    try:
        d = float(new) - float(old)
    except (TypeError, ValueError):
        return ""
    color = "#c0392b" if d < -0.05 else ("#1e8e3e" if d > 0.05 else "#777")
    return f"<span style='color:{color}'>{d:+.2f}</span>"


def _build_html(rows: list[dict[str, Any]]) -> str:
    def esc(v: Any) -> str:
        return html.escape(str(v))

    score_rows = "\n".join(
        f"<tr><td>{esc(r['name'])}</td>"
        f"<td>{r['old_d']} → <b>{esc(r['new_d'])}</b> {_delta(r['old_d'], r['new_d'])}</td>"
        f"<td>{r['old_l']} → <b>{esc(r['new_l'])}</b> {_delta(r['old_l'], r['new_l'])}</td></tr>"
        for r in rows
    )
    cards = "\n".join(_card(r, esc) for r in rows)
    return f"""<!doctype html><html><head><meta charset='utf-8'><style>
  body {{ font-family:-apple-system,Segoe UI,Roboto,sans-serif; margin:24px; color:#1a1a1a; background:#fafafa; max-width:1160px; }}
  h1 {{ font-size:22px; }} h2 {{ font-size:17px; margin-top:22px; }}
  table {{ border-collapse:collapse; width:100%; margin:10px 0; font-size:13px; }}
  th,td {{ border:1px solid #ddd; padding:7px 10px; text-align:left; vertical-align:top; }}
  th {{ background:#f0f2f5; }}
  .cmp th:first-child {{ width:30%; }}
  .old {{ color:#9a3b2f; }} .new {{ color:#1c6b3a; }}
  .card {{ background:#fff; border:1px solid #e3e3e3; border-radius:10px; padding:14px 16px; margin:14px 0; }}
  .imgs {{ display:flex; gap:16px; }} .imgs figure {{ margin:0; }} .imgs img {{ width:300px; border:1px solid #ddd; border-radius:6px; }}
  figcaption {{ font-size:11px; color:#777; text-align:center; }}
  code {{ background:#f0f0f3; padding:1px 5px; border-radius:4px; font-size:12px; }}
  .muted {{ color:#777; font-size:12px; }}
  .big {{ font-size:19px; font-weight:700; }}
</style></head><body>
<h1>Density &amp; Layout — what changed (v1 ink-based → v2 content-occupancy)</h1>

<h2>1. What changed</h2>
<table class='cmp'>
  <tr><th>aspect</th><th class='old'>BEFORE (v1)</th><th class='new'>AFTER (v2)</th><th>why</th></tr>
  <tr><td>density signal</td><td>non-white pixel ratio (ink)</td><td>content-occupancy grid: a cell is content only if it has real local variation (texture) or OCR text</td><td>colored panel backgrounds &amp; solid dark voids were counted as "ink" and inflated density</td></tr>
  <tr><td>blank/void detection</td><td>longest <b>full-width</b> blank row run (1-D)</td><td>largest <b>2-D</b> empty rectangle over the occupancy grid</td><td>empty regions inside one column (section bottoms, half-empty) never form a full-width blank row → were invisible</td></tr>
  <tr><td>density formula</td><td>10·mean(ink/0.23, text/0.30)·blankPenalty</td><td>10·(0.6·content/0.88 + 0.4·text/0.40)·voidPenalty</td><td>references raised toward gold actuals so good≠excellent stop both maxing at 10</td></tr>
  <tr><td>layout formula</td><td>10·mean(1−blank/0.25, edge/0.12)</td><td>10·content·(1 − void/0.15)</td><td>old layout saturated ~9.5 for every non-broken poster; new craters on big voids (e.g. empty bottom half)</td></tr>
  <tr><td>format fairness</td><td>ink/edge from pixels; DOM word-count crept in</td><td>100% from the rendered image; DOM only Tier-B</td><td>same poster as PNG/PDF/HTML now scores the same</td></tr>
</table>

<h2>2. Score impact (recomputed both versions)</h2>
<table>
  <tr><th>poster</th><th>density (old → new)</th><th>layout (old → new)</th></tr>
  {score_rows}
</table>
<p class='muted'>Red = score dropped (old version was over-rating it); the drops concentrate on sparse / void-heavy / colored-background posters.</p>

<h2>3. Per-poster: what the detector now sees</h2>
{cards}

<h2>4. Known remaining blind spots (for the next discussion)</h2>
<ul>
  <li><b>density over-depends on OCR</b>: a content-full poster whose text OCR can't read is capped near 6 (corner case <code>full_text</code> = 6.0).</li>
  <li><b>void penalty may be too harsh</b>: a decent poster with one real gap (ViT <code>beafef87</code>) lands at density ~4.6.</li>
  <li><b>noise read as content</b>: pure random noise scores layout 10 (variance ≠ information).</li>
  <li><b>layout has no alignment/regularity term yet</b>: a "jumbled" arrangement isn't penalized if coverage is fine.</li>
</ul>
</body></html>"""


def _card(r: dict[str, Any], esc) -> str:
    return f"""<div class='card'>
  <h3>{esc(r['name'])}</h3>
  <div class='imgs'>
    <figure><img src='data:image/png;base64,{image_b64(r['old_png'])}'><figcaption>BEFORE — 1-D blank band (red) + per-row ink</figcaption></figure>
    <figure><img src='data:image/png;base64,{image_b64(r['new_png'])}'><figcaption>AFTER — 2-D content occupancy (red=empty) + largest void box</figcaption></figure>
    <div class='muted' style='font-size:13px;line-height:1.7'>
      <b>v1 inputs:</b> nonwhite=<code>{r['nonwhite']:.3f}</code> · 1-D blank=<code>{r['blank']:.3f}</code> · text_cov=<code>{r['text_cov']:.3f}</code><br>
      <b>v2 inputs:</b> content=<code>{r['content']}</code> · largest_void=<code>{r['void']}</code><br><br>
      density <span class='old'>{r['old_d']}</span> → <span class='big new'>{esc(r['new_d'])}</span><br>
      layout <span class='old'>{r['old_l']}</span> → <span class='big new'>{esc(r['new_l'])}</span>
    </div>
  </div>
</div>"""


if __name__ == "__main__":
    raise SystemExit(main())
