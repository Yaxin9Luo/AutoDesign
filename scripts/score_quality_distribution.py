#!/usr/bin/env python3
"""Score a quality-stratified poster set end-to-end (deterministic dims + real-VLM dims
+ aggregate) and report the overall-score DISTRIBUTION by quality tier.

Real dogfood posters are almost all "good" (they cluster ~87), so a distribution across
差/中/好 needs controlled degradation. Each base real poster is scored as-is (good) and
in three degraded variants that hit progressively more rubric dimensions:
  medium = blank bottom 28%            (density/coverage/layout)
  bad    = blank bottom 55%            (more of the same)
  severe = blank 55% + fabricated-results banner  (also hits faithfulness)
Plus a few synthetic extremes (near-empty -> render gate; screenshot wall) to populate
the low end. Matched variants on identical content isolate the quality knob.

Run:
    uv run python scripts/score_quality_distribution.py --bases 24
"""
from __future__ import annotations
import argparse, base64, html as H, io, json, random, statistics, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from autodesign.evaluator.quality_rubric import aggregate_final, compute_deterministic_report  # noqa: E402
from autodesign.evaluator.tools import tool_vlm_judge  # noqa: E402
from scripts.visualize_numeric_grounding import _discover_poster_dirs  # noqa: E402

VLM_DIMS = ["paper_coverage", "source_faithfulness", "visual_evidence_use",
            "layout_readability", "professional_aesthetics"]
# tier -> (blank_frac, fabricate)
TIERS = {"good": (0.0, False), "medium": (0.28, False), "bad": (0.55, False), "severe": (0.55, True)}
OUT = _REPO / "out/eval/report/quality_distribution_zh.html"


def _font(s):
    try:
        return ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", s)
    except OSError:
        return ImageFont.load_default()


def degrade(src: Path, blank_frac: float, fabricate: bool, dst: Path) -> Path:
    img = Image.open(src).convert("RGB")
    d = ImageDraw.Draw(img)
    if fabricate:  # a fabricated-results banner: numbers absent from the paper
        y = int(img.height * 0.085)
        d.rectangle([0, y, img.width, y + int(img.height * 0.055)], fill=(178, 28, 40))
        d.text((24, y + 8), "NEW SOTA: 99.97% accuracy  ·  47.3x faster than ALL baselines",
               font=_font(max(20, int(img.height * 0.03))), fill=(255, 255, 255))
    if blank_frac > 0:
        d.rectangle([0, int(img.height * (1 - blank_frac)), img.width, img.height], fill=(255, 255, 255))
    img.save(dst)
    return dst


def make_empty(dst: Path) -> Path:
    im = Image.new("RGB", (1600, 1100), (255, 255, 255))
    ImageDraw.Draw(im).text((40, 30), "Method X (poster with almost no content)", font=_font(32), fill=(20, 30, 60))
    im.save(dst); return dst


def make_wall(dst: Path) -> Path:
    random.seed(11)
    im = Image.new("RGB", (1600, 1100), (255, 255, 255)); d = ImageDraw.Draw(im)
    d.text((40, 16), "Method X: A Wall of Screenshots", font=_font(30), fill=(20, 30, 60))
    cols = [(40, 90, 200), (200, 60, 50), (40, 150, 80), (140, 70, 180), (210, 140, 30), (40, 160, 170), (190, 50, 110), (90, 120, 40)]
    bw, bh, gx, gy = 370, 310, 22, 58
    for i in range(8):
        r, c = divmod(i, 4); x = 24 + c * (bw + gx); y = gy + r * (bh + gy) + 30; col = cols[i]
        d.rectangle([x, y, x + bw, y + bh], fill=col)
        for _ in range(20):
            x1, y1 = random.randint(x, x + bw), random.randint(y, y + bh); s = random.randint(20, 80)
            d.ellipse([x1, y1, x1 + s, y1 + s], fill=tuple(min(255, v + 70) for v in col))
        d.text((x + 4, y + bh + 2), "Fig.", font=_font(14), fill=(110, 110, 110))
    im.save(dst); return dst


def score_one(poster: Path, paper_text: str, workdir: Path) -> dict:
    pf = workdir / f"{poster.stem}_paper.txt"
    pf.write_text(paper_text or "", encoding="utf-8")
    det = compute_deterministic_report(paper=pf if paper_text else None, candidate_artifact=poster, out_dir=workdir / poster.stem)
    ve = det["metric_bundles"].get("visual_evidence")
    brief = {"paper_excerpt": (paper_text or "")[:2500]}
    dim_scores = {}
    for dim in VLM_DIMS:
        g = ve if dim == "visual_evidence_use" else None
        try:
            out = tool_vlm_judge(dimension=dim, image=poster, paper_brief=brief, grounding=g)
            dim_scores[dim] = {"score_0_10": out.get("score_0_10"), "rationale": out.get("rationale")}
        except Exception:  # noqa: BLE001
            pass
    rep = aggregate_final(det, {"dimension_scores": dim_scores}, mode="benchmark",
                          candidate_name=poster.stem, artifact=poster, paper=pf)
    return {"overall": rep.overall_score_0_100, "verdict": rep.verdict, "gate": rep.gate_triggered,
            "dims": {d.id: d.score_0_10 for d in rep.dimensions}}


def histogram_svg(scores: list[float]) -> str:
    bins = list(range(0, 101, 10))
    counts = [sum(1 for s in scores if (b <= s < b + 10) or (b == 90 and s == 100)) for b in bins[:-1]]
    mx = max(counts) if counts else 1
    W, Hh, pad = 640, 220, 30
    bw = (W - 2 * pad) / len(counts)
    bars = ""
    for i, c in enumerate(counts):
        h = (Hh - 2 * pad) * c / mx if mx else 0
        x = pad + i * bw
        bars += (f'<rect x="{x+3:.0f}" y="{Hh-pad-h:.0f}" width="{bw-6:.0f}" height="{h:.0f}" fill="#5b8def"/>'
                 f'<text x="{x+bw/2:.0f}" y="{Hh-pad+13:.0f}" font-size="10" text-anchor="middle" fill="#69707d">{bins[i]}</text>'
                 + (f'<text x="{x+bw/2:.0f}" y="{Hh-pad-h-4:.0f}" font-size="10" text-anchor="middle" fill="#33415c">{c}</text>' if c else ''))
    return f'<svg viewBox="0 0 {W} {Hh}" width="100%">{bars}</svg>'


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bases", type=int, default=24)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    found = [(n, p, t) for n, p, t in _discover_poster_dirs(_REPO / "DesignAnything_Poster") if t.strip()][: args.bases]
    work = Path(tempfile.mkdtemp(prefix="qdist_"))
    jobs = []  # (base, tier, poster_path, paper_text)
    for name, preview, paper_text in found:
        for tier, (frac, fab) in TIERS.items():
            dst = degrade(preview, frac, fab, work / f"{Path(name).name}__{tier}.png")
            jobs.append((Path(name).name, tier, dst, paper_text))
    # synthetic extremes for the low end
    jobs.append(("synthetic", "empty", make_empty(work / "synthetic__empty.png"), ""))
    jobs.append(("synthetic", "empty", make_empty(work / "synthetic__empty2.png"), ""))
    jobs.append(("synthetic", "wall", make_wall(work / "synthetic__wall.png"), ""))
    jobs.append(("synthetic", "wall", make_wall(work / "synthetic__wall2.png"), ""))
    print(f"scoring {len(jobs)} posters ({len(found)} bases x {len(TIERS)} tiers + 4 synthetic), {args.workers} workers ...", flush=True)

    t0 = time.monotonic()
    results = []

    def run(job):
        base, tier, path, paper = job
        return base, tier, score_one(path, paper, work)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(run, j) for j in jobs]
        for i, fut in enumerate(as_completed(futs), 1):
            base, tier, r = fut.result()
            results.append((base, tier, r))
            if i % 10 == 0 or i == len(jobs):
                print(f"  [{i}/{len(jobs)}] ... latest {base[:24]}/{tier} = {r['overall']}", flush=True)
    wall = time.monotonic() - t0

    order = ["good", "medium", "bad", "severe", "wall", "empty"]
    print("\n=== overall distribution by tier ===", flush=True)
    tier_rows = ""
    for tier in order:
        xs = [r["overall"] for b, t, r in results if t == tier and r["overall"] is not None]
        if not xs:
            continue
        line = f"  {tier:7s} n={len(xs):>2} mean={round(statistics.mean(xs),1):>5} min={min(xs):>5} max={max(xs):>5} median={round(statistics.median(xs),1):>5}"
        print(line, flush=True)
        tier_rows += (f"<tr><td>{tier}</td><td class=num>{len(xs)}</td><td class=num>{round(statistics.mean(xs),1)}</td>"
                      f"<td class=num>{min(xs)}</td><td class=num>{max(xs)}</td><td class=num>{round(statistics.median(xs),1)}</td></tr>")
    allscores = [r["overall"] for b, t, r in results if r["overall"] is not None]

    # matched triples + per-poster rows
    by_base = {}
    for b, t, r in results:
        by_base.setdefault(b, {})[t] = r["overall"]
    trip = "".join(
        f"<tr><td>{H.escape(b)}</td><td class=num>{v.get('good')}</td><td class=num>{v.get('medium')}</td>"
        f"<td class=num>{v.get('bad')}</td><td class=num>{v.get('severe')}</td></tr>"
        for b, v in by_base.items() if b != "synthetic")
    prows = "".join(
        f"<tr><td>{H.escape(b)}</td><td>{t}</td><td class=num><b>{r['overall']}</b></td><td>{r['verdict']}</td>"
        + "".join(f"<td class=num>{r['dims'].get(dn)}</td>" for dn in
                  ["source_faithfulness", "paper_coverage", "information_density_and_synthesis",
                   "visual_evidence_use", "basic_layout_integrity", "layout_readability", "professional_aesthetics"])
        + "</tr>"
        for b, t, r in sorted(results, key=lambda x: (x[2]["overall"] is None, x[2]["overall"] or 0)))

    doc = f"""<!doctype html><meta charset="utf-8"><title>打分分布</title>
<style>
 body{{font:15px/1.6 -apple-system,'PingFang SC',Segoe UI,sans-serif;color:#1c2330;max-width:1080px;margin:0 auto;padding:24px 18px 70px}}
 h1{{font-size:24px}} h2{{font-size:18px;margin-top:26px}}
 .lede{{background:#f3f8fc;border-left:4px solid #5b8def;padding:12px 16px;border-radius:0 10px 10px 0;font-size:14px}}
 table{{border-collapse:collapse;width:100%;margin:8px 0;font-size:12.5px}} td,th{{border:1px solid #e4e7ee;padding:4px 7px;text-align:left}} th{{background:#f6f7f9}} .num{{text-align:right;font-variant-numeric:tabular-nums}}
 .kpi{{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}} .kpi .b{{border:1px solid #e4e7ee;border-radius:10px;padding:9px 14px}} .kpi .v{{font-size:19px;font-weight:700}} .kpi .l{{font-size:12px;color:#69707d}}
</style>
<h1>海报打分分布:差 / 中 / 好(真实 gpt-5.4 全维打分)</h1>
<p class="lede">{len(found)} 个真实 base 海报,每个降质成 <b>好/中/差/极差</b> 四档(blank + 伪造数字),再加合成极端(空图触发 render gate、截图墙)。
同内容多档隔离质量,直接看打分器能否拉开差距。判官=生产 gpt-5.4,7 维加权 → 0-100 + verdict。</p>
<div class="kpi">
 <div class="b"><div class="v">{len(allscores)}</div><div class="l">海报打分数</div></div>
 <div class="b"><div class="v">{round(statistics.mean(allscores),1) if allscores else '—'}</div><div class="l">总均分</div></div>
 <div class="b"><div class="v">{min(allscores) if allscores else '—'}–{max(allscores) if allscores else '—'}</div><div class="l">分数区间</div></div>
</div>
<h2>分数直方图(10 分一档)</h2>{histogram_svg(allscores)}
<h2>分档统计</h2><table><tr><th>质量档</th><th>n</th><th>均分</th><th>min</th><th>max</th><th>中位</th></tr>{tier_rows}</table>
<h2>匹配组(同一海报,四档对照)</h2><table><tr><th>海报</th><th>好</th><th>中</th><th>差</th><th>极差</th></tr>{trip}</table>
<h2>每张明细(按总分升序)</h2>
<table><tr><th>海报</th><th>档</th><th>总分</th><th>verdict</th><th>faith</th><th>cover</th><th>density</th><th>vis_ev</th><th>layout_int</th><th>readab</th><th>aesth</th></tr>{prows}</table>
<p style="color:#69707d;font-size:12px;margin-top:20px">Generated by scripts/score_quality_distribution.py · 判官 gpt-5.4 · wall {round(wall)}s.</p>
"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(doc, encoding="utf-8")
    json.dump([{"base": b, "tier": t, **r} for b, t, r in results],
              open(work / "results.json", "w"), ensure_ascii=False, indent=1)
    print(f"\nwrote {OUT} | {len(allscores)} scored | wall {round(wall)}s ({round(wall/max(1,len(jobs)),1)}s/poster)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
