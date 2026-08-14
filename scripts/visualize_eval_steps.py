#!/usr/bin/env python3
"""Visualize the EARLY steps of the poster evaluator: detection + scoring.

This renders, on top of the real poster pixels, exactly what the deterministic
rule tools "see", and then shows the scoring formulas with the real numbers
plugged in. Scope is intentionally the early/deterministic stage only:

  1. pixel density detection (ink coverage + longest blank vertical band)
  2. OCR text detection (boxes + text coverage)
  3. numeric grounding (which numbers were found, which matched the paper)
  4. how those measurements become 0-10 dimension scores

Usage:
    uv run python scripts/visualize_eval_steps.py \
        --candidate out/runs/<id>/final/preview.png \
        --paper data/author_artifacts/neurips2024_vript/paper.pdf \
        --out-dir out/eval/viz_vript
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

from autodesign.evaluator.ocr import run_ocr
from autodesign.evaluator.quality_rubric import compute_deterministic_report
from autodesign.evaluator.poster_rubric import (
    BLANK_BAND_FLOOR,
    BLANK_BAND_PENALTY_K,
    REF_NONWHITE_RATIO,
    REF_TEXT_COVERAGE_RATIO,
)
from autodesign.evaluator.viz import (
    density_overlay,
    image_b64,
    numeric_overlay,
    ocr_overlay,
    paper_numeric_tokens,
)
from autodesign.util.browser_render import screenshot_html


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    report = compute_deterministic_report(
        paper=args.paper,
        candidate_artifact=args.candidate,
        out_dir=out_dir / "deterministic",
        profile=args.profile,
    )
    preview_path = report.get("preview_image")
    if not preview_path or not Path(preview_path).exists():
        print("error: candidate produced no preview image", file=sys.stderr)
        return 2
    base = Image.open(preview_path).convert("RGB")

    ocr = run_ocr(preview_path, include_segments=True)
    segments = ocr.get("segments") or []
    paper_tokens = paper_numeric_tokens(args.paper)

    blank_png = out_dir / "step1_density.png"
    ocr_png = out_dir / "step2_ocr.png"
    numeric_png = out_dir / "step3_numeric.png"
    density_overlay(base)[0].save(blank_png)
    ocr_overlay(base, segments).save(ocr_png)
    numeric_img, matched_n, missing_n = numeric_overlay(base, segments, paper_tokens)
    numeric_img.save(numeric_png)

    html_path = out_dir / "index.html"
    html_path.write_text(
        _build_html(report, ocr, matched_n, missing_n, blank_png, ocr_png, numeric_png),
        encoding="utf-8",
    )
    report_png = out_dir / "eval_steps_report.png"
    result = screenshot_html(
        html_path, report_png,
        viewport_width=1280, viewport_height=2000, full_page=True, max_edge=2400, timeout_ms=20_000,
    )
    print(f"wrote {html_path}")
    print(f"overlays: {blank_png.name}, {ocr_png.name}, {numeric_png.name}")
    print(f"rendered report image: {report_png}" if result.paths and report_png.exists()
          else f"(report image render warnings: {result.warnings})")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--candidate", required=True, type=Path, help="Poster image / PDF / run dir.")
    p.add_argument("--paper", type=Path, default=None, help="Source paper PDF/text (for numeric grounding).")
    p.add_argument("--out-dir", required=True, type=Path)
    p.add_argument("--profile", default=None)
    return p.parse_args(argv)


def _bar(value: float, *, color: str = "#2b6") -> str:
    pct = max(0.0, min(1.0, value)) * 100
    return (
        f"<div class='bar'><div class='fill' style='width:{pct:.0f}%;background:{color}'></div>"
        f"<span>{value:.3f}</span></div>"
    )


def _build_html(
    report: dict[str, Any], ocr: dict[str, Any], matched_n: int, missing_n: int,
    blank_png: Path, ocr_png: Path, numeric_png: Path,
) -> str:
    bundles = report.get("metric_bundles", {})
    img = bundles.get("image_density", {})
    num = bundles.get("numeric_token_exact_match", {})
    comps = report.get("dimension_components", {})
    dens_score = (comps.get("information_density_and_synthesis") or {}).get("score_0_10")
    faith_score = (comps.get("source_faithfulness") or {}).get("score_0_10")
    lay_score = (comps.get("layout_readability") or {}).get("score_0_10")

    nonwhite = float(img.get("nonwhite_pixel_ratio") or 0)
    text_cov = float(ocr.get("text_coverage_ratio") or 0) if ocr.get("available") else 0.0
    blank = float(img.get("longest_blank_vertical_run_ratio") or 0)
    edge = float(img.get("edge_density") or 0)
    ink_comp = min(1.0, nonwhite / REF_NONWHITE_RATIO) if REF_NONWHITE_RATIO else 0
    text_comp = min(1.0, text_cov / REF_TEXT_COVERAGE_RATIO) if REF_TEXT_COVERAGE_RATIO else 0
    over = min(1.0, max(0.0, (blank - BLANK_BAND_FLOOR) / max(BLANK_BAND_FLOOR, 1e-6)))
    penalty = 1.0 - BLANK_BAND_PENALTY_K * over
    ratio = float(num.get("exact_match_ratio") or 0)

    def esc(v: Any) -> str:
        return html.escape(str(v))

    return f"""<!doctype html><html><head><meta charset='utf-8'><style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 24px; color:#1a1a1a; background:#fafafa; max-width:1200px; }}
  h1 {{ font-size: 22px; }} h2 {{ font-size:17px; margin-top:6px; }}
  .step {{ background:#fff; border:1px solid #e3e3e3; border-radius:10px; padding:16px 18px; margin:16px 0; }}
  .row {{ display:flex; gap:20px; align-items:flex-start; }}
  .imgcol img {{ width:340px; border:1px solid #ddd; border-radius:6px; }}
  .detail {{ flex:1; font-size:13.5px; line-height:1.6; }}
  code {{ background:#f0f0f3; padding:1px 5px; border-radius:4px; font-size:12.5px; }}
  .bar {{ position:relative; height:18px; background:#eee; border-radius:4px; margin:3px 0 8px; width:340px; }}
  .bar .fill {{ height:100%; border-radius:4px; }}
  .bar span {{ position:absolute; right:6px; top:0; font-size:11px; line-height:18px; color:#222; }}
  .score {{ font-size:26px; font-weight:700; }}
  .legend span {{ display:inline-block; margin-right:14px; font-size:12px; }}
  .sw {{ display:inline-block; width:11px; height:11px; border-radius:2px; vertical-align:middle; margin-right:4px; }}
  .formula {{ background:#fbfbfd; border-left:3px solid #4a86e8; padding:8px 12px; margin:8px 0; font-size:13px; }}
  .muted {{ color:#777; font-size:12px; }}
</style></head><body>
<h1>Poster Evaluation — early steps (detection &amp; scoring)</h1>
<p class='muted'>Candidate: <code>{esc(report.get('candidate_artifact'))}</code> · canvas {esc(report.get('canvas'))} · OCR engine: <code>{esc(ocr.get('engine'))}</code></p>

<div class='step'>
  <h2>Step 1 — Pixel density detection <span class='muted'>(image_density rules)</span></h2>
  <div class='row'>
    <div class='imgcol'><img src='data:image/png;base64,{image_b64(blank_png)}'>
      <div class='legend'><span><span class='sw' style='background:rgba(255,40,40,.5)'></span>longest blank vertical band</span>
      <span><span class='sw' style='background:#2b5ac8'></span>per-row ink (right gutter)</span></div>
    </div>
    <div class='detail'>What the detector measures straight from pixels:
      <ul><li>ink coverage (non-white px ratio) = <code>{nonwhite:.3f}</code></li>
        <li>longest blank vertical run = <code>{blank:.3f}</code> (floor {BLANK_BAND_FLOOR})</li>
        <li>edge density = <code>{edge:.3f}</code></li></ul>
      The red band is the single longest stretch of near-empty rows.</div>
  </div>
</div>

<div class='step'>
  <h2>Step 2 — OCR text detection <span class='muted'>(RapidOCR, image-native)</span></h2>
  <div class='row'>
    <div class='imgcol'><img src='data:image/png;base64,{image_b64(ocr_png)}'>
      <div class='legend'><span><span class='sw' style='background:#0a0'></span>recognized text box</span></div>
    </div>
    <div class='detail'>Text is recovered from the rendered image (no DOM), so this works for PNG/JPG/PDF:
      <ul><li>text boxes = <code>{esc(ocr.get('segment_count'))}</code>, words = <code>{esc(ocr.get('word_count'))}</code></li>
        <li>text coverage (box area / image) = <code>{text_cov:.3f}</code> <span class='muted'>(resolution-invariant)</span></li>
        <li>mean OCR confidence = <code>{esc(ocr.get('mean_confidence'))}</code></li></ul>
      Text coverage is the signal the density score uses (a ratio → stable across formats).</div>
  </div>
</div>

<div class='step'>
  <h2>Step 3 — Numeric grounding <span class='muted'>(faithfulness detection)</span></h2>
  <div class='row'>
    <div class='imgcol'><img src='data:image/png;base64,{image_b64(numeric_png)}'>
      <div class='legend'><span><span class='sw' style='background:#1e6ee6'></span>number found in paper</span>
      <span><span class='sw' style='background:#dc1e1e'></span>not in paper</span></div>
    </div>
    <div class='detail'>Every number on the poster is matched, exactly, against the paper text:
      <ul><li>numeric tokens on poster = <code>{esc(num.get('artifact_numeric_token_count'))}</code> (text source: <code>{esc(num.get('text_source'))}</code>)</li>
        <li>matched in paper = <code>{esc(num.get('matched_count'))}</code>, missing = <code>{esc(num.get('missing_count'))}</code></li>
        <li>boxes highlighted: blue {matched_n} / red {missing_n}</li></ul>
      A red box is a number the detector could not find in the paper — possible fabrication or OCR misread.</div>
  </div>
</div>

<div class='step'>
  <h2>Step 4 — How measurements become 0-10 scores</h2>
  <h3>Information density &amp; synthesis</h3>
  <div class='formula'>
    ink = min(1, {nonwhite:.3f} / {REF_NONWHITE_RATIO}) = <b>{ink_comp:.3f}</b><br>
    text = min(1, {text_cov:.3f} / {REF_TEXT_COVERAGE_RATIO}) = <b>{text_comp:.3f}</b><br>
    base = 0.5·ink + 0.5·text = <b>{(0.5*ink_comp+0.5*text_comp):.3f}</b><br>
    blank penalty = 1 − {BLANK_BAND_PENALTY_K}·{over:.2f} = <b>{penalty:.3f}</b> (multiplicative)<br>
    score = 10 · base · penalty = <span class='score'>{esc(dens_score)}</span> / 10
  </div>
  {_bar(ink_comp, color='#4a86e8')}{_bar(text_comp, color='#16a766')}
  <h3>Source faithfulness</h3>
  <div class='formula'>score = exact_match_ratio · 10 = {ratio:.3f} · 10 = <span class='score'>{esc(faith_score)}</span> / 10</div>
  {_bar(ratio, color='#1e6ee6')}
  <h3>Layout readability</h3>
  <div class='formula'>
    blank component = max(0, 1 − {blank:.3f}/0.25) = <b>{max(0.0,1-blank/0.25):.3f}</b><br>
    edge component = min(1, {edge:.3f}/0.12) = <b>{min(1.0,edge/0.12):.3f}</b><br>
    score = 10 · mean(components) − overflow penalty = <span class='score'>{esc(lay_score)}</span> / 10
  </div>
  <p class='muted'>Density &amp; layout are computed purely from the rendered image, so PNG/JPG/PDF/HTML of the
  same poster get the same score. DOM signals are kept as optional Tier-B diagnostics only.</p>
</div>
</body></html>"""


if __name__ == "__main__":
    raise SystemExit(main())
