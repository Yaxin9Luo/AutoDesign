#!/usr/bin/env python3
"""Visualize deterministic numeric grounding.

Colors every number on a poster by whether it is grounded in the source paper,
a near-miss (likely OCR), fabricated (no paper counterpart), or trivial (a small
structural integer the metric ignores). Mirrors visualize_basic_layout_integrity:
it draws exactly the signals the metric uses, then builds a self-contained HTML
report for human inspection.

Usage:
    uv run python scripts/visualize_numeric_grounding.py
    uv run python scripts/visualize_numeric_grounding.py \
        --candidate omni=out/.../preview.png::out/uploads/.../omnimamba.pdf
"""

from __future__ import annotations

import argparse
import base64
import html as H
import io
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from autodesign.evaluator.metrics import (
    _extract_paper_text,
    _is_salient,
    _match_number,
    _median_segment_height,
    _numbers_are_figure_reproductions,
    _parse_numeric_tokens,
    numeric_token_metrics,
)
from autodesign.evaluator.ocr import run_ocr
from autodesign.evaluator.schema import ArtifactSnapshot
from autodesign.evaluator.spatial import basic_layout_integrity

COLORS = {
    "grounded": (24, 140, 70),
    "near_miss": (210, 140, 20),
    "fabricated": (200, 30, 40),
    "skipped": (120, 135, 165),
    "trivial": (150, 150, 160),
}
LABELS = {
    "grounded": ("grounded · 已接地", COLORS["grounded"]),
    "near_miss": ("near-miss (OCR) · 近似", COLORS["near_miss"]),
    "fabricated": ("fabricated · 伪造", COLORS["fabricated"]),
    "skipped": ("figure/label/citation (skipped) · 图表·标签·引文(不计)", COLORS["skipped"]),
    "trivial": ("trivial · 琐碎", COLORS["trivial"]),
}
_ORDER = ["fabricated", "near_miss", "grounded", "skipped", "trivial"]


def _font(size: int):
    for p in ("/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _bbox(points: list) -> tuple[float, float, float, float]:
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _classify(tok: dict[str, Any], paper_tokens: list[dict[str, Any]]) -> str:
    if not _is_salient(tok):
        return "trivial"
    status = _match_number(tok, paper_tokens) if paper_tokens else "ungrounded"
    return "fabricated" if status == "ungrounded" else status


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Dedup salient AUTHORED numbers by (value, kind) and tally — matches
    numeric_token_metrics. Figure/chart/table-label numbers (status 'skipped') are
    counted separately and never enter the grounding ratio."""
    seen: set[tuple[float, str]] = set()
    g = n = fab = skipped = 0
    fab_ex: list[str] = []
    for r in rows:
        if r["status"] == "skipped":
            skipped += 1
            continue
        if not r["salient"]:
            continue
        key = (round(r["value"], 6), r["kind"])
        if key in seen:
            continue
        seen.add(key)
        if r["status"] == "grounded":
            g += 1
        elif r["status"] == "near_miss":
            n += 1
        else:
            fab += 1
            fab_ex.append(r["raw"])
    sal = g + n + fab
    return {"grounded": g, "near": n, "fab": fab, "salient": sal, "skipped": skipped,
            "ratio": round((g + 0.5 * n) / sal, 3) if sal else None, "fab_ex": fab_ex}


def annotate(poster: Path, paper_text: str) -> dict[str, Any]:
    img = Image.open(poster).convert("RGB")
    ocr = run_ocr(poster, include_segments=True)
    segments = ocr.get("segments") or []
    # Figure/visual regions, via the same detector the metric uses, so the report
    # excludes figure/label/citation numbers exactly as scoring does.
    try:
        visual_rects = basic_layout_integrity(img.copy(), segments=segments).get("visual_region_rects") or []
    except Exception:
        visual_rects = []
    paper_tokens, _g = _parse_numeric_tokens(paper_text)
    paper_values = [p["value"] for p in paper_tokens]
    median_h = _median_segment_height(segments)

    draw = ImageDraw.Draw(img, "RGBA")
    scale = max(img.width, img.height) / 1600.0
    fsize = max(13, int(15 * scale))
    font = _font(fsize)
    rows: list[dict[str, Any]] = []

    for seg in segments:
        text = str(seg.get("text") or "")
        toks, _gg = _parse_numeric_tokens(text)
        if not toks:
            continue
        excluded, reason = _numbers_are_figure_reproductions(
            text, seg.get("box"), visual_rects, median_h)  # not the poster's own claim
        statuses = []
        for tok in toks:
            st = "skipped" if excluded else _classify(tok, paper_tokens)
            statuses.append(st)
            nearest = min(paper_values, key=lambda v: abs(v - tok["value"])) if paper_values else None
            rows.append({
                "raw": tok["raw"], "value": tok["value"], "kind": tok["kind"],
                "salient": _is_salient(tok), "status": st,
                "reason": reason,
                "nearest": round(nearest, 4) if nearest is not None else None,
            })
        worst = next((s for s in _ORDER if s in statuses), "trivial")
        x0, y0, x1, y1 = _bbox(seg["box"])
        color = COLORS[worst]
        fill_a, outline_w = (22, max(1, int(2 * scale))) if excluded else (45, max(2, int(3 * scale)))
        draw.rectangle([x0, y0, x1, y1], fill=color + (fill_a,))
        draw.rectangle([x0, y0, x1, y1], outline=color + (200 if excluded else 255,), width=outline_w)
        if not excluded:
            label = ",".join(t["raw"] for t in toks)[:20]
            draw.text((x0 + 2, max(0, y0 - fsize - 2)), label, fill=color + (255,), font=font)

    return {"image": img, "rows": rows, "summary": _summarize(rows)}


def _thumb_b64(img: Image.Image, max_w: int = 920) -> str:
    if img.width > max_w:
        img = img.resize((max_w, int(img.height * max_w / img.width)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _default_candidates() -> list[tuple[str, str, str, str]]:
    omni_paper = "out/uploads/09c1af5980ea/omnimamba.pdf"
    cands = [
        ("OmniMamba — real poster (faithful)", "真实海报(忠实)",
         "out/typesetting_replay_native_marker/20260624-201023-592f6072/preview.png", omni_paper),
        ("OmniMamba + fabricated banner", "真实海报 + 伪造横幅",
         "out/eval/real_degraded/omnimamba_fabricated.png", omni_paper),
    ]
    return [(en, zh, p, pa) for en, zh, p, pa in cands if (_REPO / p).exists() and (_REPO / pa).exists()]


def _card(en: str, zh: str, result: dict[str, Any], anchor: str = "") -> str:
    s = result["summary"]
    fnd = (
        f'<span class="pill" style="background:#fde3e3;color:#a01b1b">P1 fabrication · 伪造</span>'
        if s["fab"] > 0 else
        (f'<span class="pill" style="background:#fdeede;color:#9a5b09">P2 near-miss · 近似</span>'
         if s["near"] > 0 else
         '<span class="pill" style="background:#dcf3e3;color:#10683a">clean · 无</span>')
    )
    rows = sorted(result["rows"], key=lambda r: _ORDER.index(r["status"]))
    trows = "".join(
        f'<tr><td class="mono">{H.escape(str(r["raw"]))}</td><td class="num">{r["value"]:g}</td>'
        f'<td>{H.escape(r["kind"])}</td><td>{"✓" if r["salient"] else "·"}</td>'
        f'<td><span class="dot" style="background:rgb{COLORS[r["status"]]}"></span>{H.escape(r["status"])}</td>'
        f'<td class="num">{r["nearest"] if r["nearest"] is not None else "—"}</td></tr>'
        for r in rows[:60]
    )
    return f"""
<div class="card" id="{H.escape(anchor)}">
 <h3>{H.escape(en)} <span class="s">/ {H.escape(zh)}</span></h3>
 <div class="meta">
   <b>salient grounding · 显著接地率</b> {s['ratio']}
   &nbsp;·&nbsp; grounded {s['grounded']} / near {s['near']} /
   <b style="color:#b3261e">fab {s['fab']}</b>
   &nbsp;·&nbsp; <span class="s">figure/label skipped · 图表标签不计 {s['skipped']}</span>
   &nbsp;·&nbsp; {fnd}
   {('<br><span class="s">fabricated/未接地: ' + H.escape(', '.join(s['fab_ex'][:14])) + '</span>') if s['fab_ex'] else ''}
 </div>
 <img src="{_thumb_b64(result['image'])}">
 <details><summary>every number · 每个数字 ({len(result['rows'])})</summary>
  <table><tr><th>raw</th><th>value</th><th>kind</th><th>salient</th><th>status</th><th>nearest paper · 最近论文值</th></tr>
  {trows}</table>
 </details>
</div>"""


def _paper_text_for_run(run_dir: Path) -> str:
    """Build the grounding reference from a generation run: the ingested paper memory
    + the numeric evidence packs (the richest per-poster number coverage available)."""
    parts: list[str] = []
    for name in ("paper_memory.md", "paper_memory_dossier.md"):
        f = run_dir / name
        if f.exists():
            parts.append(f.read_text(errors="replace"))
    packs = run_dir / "paper_evidence_packs"
    if packs.is_dir():
        for f in sorted(packs.glob("*.md")):
            parts.append(f.read_text(errors="replace"))
    return "\n".join(parts)


def _discover_poster_dirs(root: Path) -> list[tuple[str, Path, str]]:
    out: list[tuple[str, Path, str]] = []
    for preview in sorted(root.rglob("preview.png")):
        d = preview.parent
        run_txt = d / "run_dir.txt"
        run_dir = Path(run_txt.read_text().strip()) if run_txt.exists() else (d / "run")
        if not run_dir.exists():
            continue
        paper_text = _paper_text_for_run(run_dir.resolve())
        if paper_text.strip():
            out.append((f"{d.parent.name}/{d.name}", preview, paper_text))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--posters-dir", type=Path, default=None,
                    help="dir of poster runs (each <sub>/preview.png + run/paper_memory.md).")
    ap.add_argument("--candidate", action="append", default=[],
                    help="name=poster.png::paper.(pdf|txt). Repeatable.")
    ap.add_argument("--limit", type=int, default=0, help="cap number of posters (0=all).")
    ap.add_argument("--out", type=Path, default=_REPO / "out/eval/report/numeric_grounding_report.html")
    args = ap.parse_args(argv)

    items: list[tuple[str, str, dict[str, Any]]] = []
    if args.posters_dir:
        root = args.posters_dir if args.posters_dir.is_absolute() else _REPO / args.posters_dir
        found = _discover_poster_dirs(root)
        if args.limit:
            found = found[:args.limit]
        print(f"annotating {len(found)} posters from {root} ...")
        for i, (name, preview, paper_text) in enumerate(found, 1):
            res = annotate(preview, paper_text)
            items.append((name, name, res))
            print(f"  [{i}/{len(found)}] {name}: ratio={res['summary']['ratio']} fab={res['summary']['fab']}")
    elif args.candidate:
        for spec in args.candidate:
            name, _, rest = spec.partition("=")
            poster, _, paper = rest.partition("::")
            items.append((name, name, annotate(_REPO / poster, _extract_paper_text(_REPO / paper))))
    else:
        for en, zh, poster, paper in _default_candidates():
            items.append((en, zh, annotate(_REPO / poster, _extract_paper_text(_REPO / paper))))

    # rank worst-first (lowest grounding ratio) and build a summary table + anchored cards
    ranked = sorted(items, key=lambda it: it[2]["summary"]["ratio"] if it[2]["summary"]["ratio"] is not None else 1.1)
    ratios = [r["summary"]["ratio"] for _, _, r in ranked if r["summary"]["ratio"] is not None]
    n_fab = sum(1 for _, _, r in ranked if r["summary"]["fab"] > 0)
    mean_ratio = round(sum(ratios) / len(ratios), 3) if ratios else None
    srows = "".join(
        f'<tr><td><a href="#p{i}">{H.escape(en)}</a></td><td class="num">{r["summary"]["ratio"]}</td>'
        f'<td class="num">{r["summary"]["grounded"]}</td><td class="num">{r["summary"]["near"]}</td>'
        f'<td class="num" style="color:#b3261e">{r["summary"]["fab"]}</td></tr>'
        for i, (en, zh, r) in enumerate(ranked)
    )
    cards = "".join(_card(en, zh, r, anchor=f"p{i}") for i, (en, zh, r) in enumerate(ranked))

    legend = "".join(
        f'<span class="pill"><span class="dot" style="background:rgb{c}"></span>{H.escape(lab)}</span>'
        for lab, c in LABELS.values()
    )
    html_doc = f"""<!doctype html><meta charset="utf-8"><title>Numeric grounding · 数字接地可视化</title>
<style>
 body{{font:15px/1.6 -apple-system,'PingFang SC',Segoe UI,Roboto,sans-serif;color:#1c2330;max-width:1000px;margin:0 auto;padding:24px 18px 70px}}
 h1{{font-size:25px}} h2{{font-size:18px;margin-top:30px}} h3{{font-size:15.5px;margin:4px 0}} .s{{color:#69707d;font-weight:400;font-size:13px}}
 .lede{{background:#f3f8fc;border-left:4px solid #5b8def;padding:11px 16px;border-radius:0 10px 10px 0;font-size:14px}}
 .legend{{margin:14px 0}} .pill{{display:inline-block;font-size:12.5px;border-radius:11px;padding:3px 10px;margin:2px 6px 2px 0;background:#eef1f6}}
 .dot{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px;vertical-align:middle}}
 .card{{border:1px solid #e4e7ee;border-radius:12px;padding:14px 16px;margin:16px 0;background:#fff;scroll-margin-top:14px}}
 .card img{{width:100%;border:1px solid #eceff3;border-radius:8px;margin-top:8px}}
 .meta{{font-size:13.5px;color:#3a4250;margin:4px 0 6px}}
 table{{border-collapse:collapse;width:100%;margin:8px 0;font-size:12.5px}} td,th{{border:1px solid #e4e7ee;padding:5px 8px;text-align:left}}
 th{{background:#f6f7f9}} .num{{text-align:right;font-variant-numeric:tabular-nums}} .mono{{font-family:ui-monospace,Menlo,monospace}}
 details{{margin-top:8px}} summary{{cursor:pointer;color:#46506a;font-size:13px}} a{{color:#2a52a0}}
 .kpi{{display:flex;gap:14px;flex-wrap:wrap;margin:12px 0}} .kpi .b{{border:1px solid #e4e7ee;border-radius:10px;padding:10px 16px}} .kpi .v{{font-size:22px;font-weight:700}} .kpi .l{{font-size:12px;color:#69707d}}
</style>
<h1>数字接地可视化 · Numeric Grounding</h1>
<p class="lede">海报上每个<b>作者撰写</b>的数字被框出上色:它在论文里能找到吗?<b>绿=已接地</b>,<b style="color:#d28c14">橙=近似(疑似 OCR)</b>,
<b style="color:#b3261e">红=伪造/未接地(论文里没有)</b>,灰=琐碎(算法忽略)。<b style="color:#6e7ba0">蓝灰=图表标签/引文元数据(不计入接地率)</b>——
这些数字不是作者论断:论文图表的复刻、坐标轴裸标签、以及引文/书目元数据(含 DOI、et al.、会议/卷期 —— 无论在标题区还是页脚)。
注意是按"元数据性质"排除而非按位置,所以放在顶部的伪造头条数字仍会被检测。参照 = 完整论文 PDF(没有 PDF 时该维度应判 N/A)。</p>
<div class="legend">{legend}</div>
<div class="kpi">
 <div class="b"><div class="v">{len(ranked)}</div><div class="l">posters · 海报数</div></div>
 <div class="b"><div class="v">{mean_ratio}</div><div class="l">mean grounding · 平均接地率</div></div>
 <div class="b"><div class="v">{n_fab}</div><div class="l">with ≥1 ungrounded · 有未接地数字</div></div>
</div>
<h2>Overview · 总览(按接地率升序,最差在前)</h2>
<table><tr><th>poster · 海报</th><th>grounding</th><th>grounded</th><th>near</th><th>ungrounded</th></tr>{srows}</table>
{cards}
<p class="s" style="margin-top:30px">Generated by scripts/visualize_numeric_grounding.py</p>
"""
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html_doc, encoding="utf-8")
    print(f"wrote {args.out} ({len(html_doc)//1024} KB, {len(ranked)} posters)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
