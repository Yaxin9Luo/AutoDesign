#!/usr/bin/env python3
"""Score every REAL AutoDesign poster in DesignAnything_Poster (100 = 5 disciplines x 20) with the
full pipeline (deterministic dims + real-VLM dims + aggregate) and report the overall
distribution — by discipline, as a histogram, and per poster.

Unlike score_quality_distribution.py (which degrades posters to span quality), this
scores them AS-IS, to see where genuine generated posters land — and in particular how
many cluster high, which is the over-leniency the attention example flagged (a poster
with crammed figures / wrong crops / thin content still scoring ~80+).

Run:
    uv run python scripts/score_real_posters.py
"""
from __future__ import annotations
import html as H, json, statistics, sys, tempfile, time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.score_quality_distribution import histogram_svg, score_one  # noqa: E402
from scripts.visualize_numeric_grounding import _discover_poster_dirs  # noqa: E402

OUT = _REPO / "out/eval/report/real_posters_distribution_zh.html"
DIMS = ["source_faithfulness", "paper_coverage", "information_density_and_synthesis",
        "visual_evidence_use", "basic_layout_integrity", "layout_readability", "professional_aesthetics"]
# Baseline = same 100 posters scored with the OLD lenient judge (pre-sharpening run).
OLD_BASELINE = {"mean": 83.4, "median": 84.0, "std": 3.7, "min": 69.46, "max": 89.65, "ge80": 85}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    found = [(n, p, t) for n, p, t in _discover_poster_dirs(_REPO / "DesignAnything_Poster") if t.strip()]
    if args.limit:
        found = found[: args.limit]
    work = Path(tempfile.mkdtemp(prefix="realscore_"))
    print(f"scoring {len(found)} real posters, {args.workers} workers ...", flush=True)

    t0 = time.monotonic()
    results = []

    def run(item):
        name, preview, paper_text = item
        disc = name.split("/")[0]
        sub = work / name.replace("/", "__")  # unique per poster (all are named preview.png)
        sub.mkdir(parents=True, exist_ok=True)
        r = score_one(preview, paper_text, sub)
        return name, disc, r

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(run, it) for it in found]
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                name, disc, r = fut.result()
            except Exception as e:  # noqa: BLE001
                print(f"  [{i}] FAILED: {e}", flush=True); continue
            results.append((name, disc, r))
            if i % 10 == 0 or i == len(found):
                print(f"  [{i}/{len(found)}] latest {name.split('/')[-1][:30]} = {r['overall']}", flush=True)
    wall = time.monotonic() - t0

    scores = [r["overall"] for _, _, r in results if r["overall"] is not None]
    if not scores:
        print("\nNO SCORES — all posters failed; aborting report.", flush=True)
        return 1
    print("\n=== overall ===", flush=True)
    print(f"  n={len(scores)} mean={round(statistics.mean(scores),1)} median={round(statistics.median(scores),1)} "
          f"min={min(scores)} max={max(scores)} std={round(statistics.pstdev(scores),1)}", flush=True)
    for thr in (90, 80, 70):
        print(f"  >= {thr}: {sum(1 for s in scores if s>=thr)}/{len(scores)}", flush=True)
    print("=== by discipline ===", flush=True)
    by_disc = defaultdict(list)
    for _, d, r in results:
        if r["overall"] is not None:
            by_disc[d].append(r["overall"])
    disc_rows = ""
    for d in sorted(by_disc):
        xs = by_disc[d]
        print(f"  {d:26s} n={len(xs):>2} mean={round(statistics.mean(xs),1)} min={min(xs)} max={max(xs)}", flush=True)
        disc_rows += f"<tr><td>{d}</td><td class=num>{len(xs)}</td><td class=num>{round(statistics.mean(xs),1)}</td><td class=num>{min(xs)}</td><td class=num>{max(xs)}</td></tr>"

    prows = "".join(
        f"<tr><td>{H.escape(n)}</td><td class=num><b>{r['overall']}</b></td><td>{r['verdict']}</td>"
        + "".join(f"<td class=num>{r['dims'].get(dn)}</td>" for dn in DIMS) + "</tr>"
        for n, d, r in sorted(results, key=lambda x: (x[2]["overall"] is None, -(x[2]["overall"] or 0))))
    doc = f"""<!doctype html><meta charset="utf-8"><title>真实海报打分分布</title>
<style>
 body{{font:15px/1.6 -apple-system,'PingFang SC',Segoe UI,sans-serif;color:#1c2330;max-width:1120px;margin:0 auto;padding:24px 18px 70px}}
 h1{{font-size:24px}} h2{{font-size:18px;margin-top:24px}}
 .lede{{background:#fff7ed;border-left:4px solid #e0992a;padding:12px 16px;border-radius:0 10px 10px 0;font-size:14px}}
 table{{border-collapse:collapse;width:100%;margin:8px 0;font-size:12px}} td,th{{border:1px solid #e4e7ee;padding:4px 7px;text-align:left}} th{{background:#f6f7f9}} .num{{text-align:right;font-variant-numeric:tabular-nums}}
 .kpi{{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}} .kpi .b{{border:1px solid #e4e7ee;border-radius:10px;padding:9px 14px}} .kpi .v{{font-size:19px;font-weight:700}} .kpi .l{{font-size:12px;color:#69707d}}
</style>
<h1>真实生成海报打分分布:AutoDesign(100 张,5 学科)</h1>
<p class="lede"><b>优化后的 evaluation metrics</b>(锐化判官 + 图片拼贴检测器)重打这 100 张真实海报,对照<b>优化前(宽松判官)</b>的旧分布。
重点看:旧版"扎堆 83 / 85% ≥80"是否被治成<b>拉开、整体下移、有缺陷的掉到低分</b>。判官=真实 gpt-5.4,7 维加权。</p>
<h2>优化前 vs 优化后</h2>
<table><tr><th></th><th>均分</th><th>中位</th><th>标准差</th><th>区间</th><th>≥80 张</th></tr>
<tr><td><b>优化前(宽松)</b></td><td class=num>{OLD_BASELINE['mean']}</td><td class=num>{OLD_BASELINE['median']}</td><td class=num>{OLD_BASELINE['std']}</td><td class=num>{OLD_BASELINE['min']}–{OLD_BASELINE['max']}</td><td class=num>{OLD_BASELINE['ge80']}</td></tr>
<tr><td><b>优化后(本次)</b></td><td class=num><b>{round(statistics.mean(scores),1)}</b></td><td class=num>{round(statistics.median(scores),1)}</td><td class=num><b>{round(statistics.pstdev(scores),1)}</b></td><td class=num>{min(scores)}–{max(scores)}</td><td class=num><b>{sum(1 for s in scores if s>=80)}</b></td></tr></table>
<div class="kpi">
 <div class="b"><div class="v">{len(scores)}</div><div class="l">海报</div></div>
 <div class="b"><div class="v">{round(statistics.mean(scores),1)}</div><div class="l">均分(旧 {OLD_BASELINE['mean']})</div></div>
 <div class="b"><div class="v">{round(statistics.pstdev(scores),1)}</div><div class="l">标准差(旧 {OLD_BASELINE['std']})</div></div>
 <div class="b"><div class="v">{sum(1 for s in scores if s>=80)}</div><div class="l">≥80 张(旧 {OLD_BASELINE['ge80']})</div></div>
 <div class="b"><div class="v">{min(scores)}–{max(scores)}</div><div class="l">区间</div></div>
</div>
<h2>分数直方图</h2>{histogram_svg(scores)}
<h2>按学科</h2><table><tr><th>学科</th><th>n</th><th>均分</th><th>min</th><th>max</th></tr>{disc_rows}</table>
<h2>每张明细(按总分降序)</h2>
<table><tr><th>海报</th><th>总分</th><th>verdict</th><th>faith</th><th>cover</th><th>density</th><th>vis_ev</th><th>layout_int</th><th>readab</th><th>aesth</th></tr>{prows}</table>
<p style="color:#69707d;font-size:12px;margin-top:18px">Generated by scripts/score_real_posters.py · gpt-5.4 · wall {round(wall)}s.</p>
"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(doc, encoding="utf-8")
    json.dump([{"name": n, "discipline": d, **r} for n, d, r in results], open(work / "real_results.json", "w"), ensure_ascii=False, indent=1)
    print(f"\nwrote {OUT} | {len(scores)} scored | wall {round(wall)}s ({round(wall/max(1,len(found)),1)}s/poster)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
