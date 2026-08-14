#!/usr/bin/env python3
"""Run the REAL critic (gpt-5.4) visual_evidence_use judge on real posters and build a
Chinese HTML report showing, per poster, the full trajectory:
  ① CV detection (figure boxes + signals)
  ② the grounding injected into the judge (verbatim)
  ③ the VLM judge output (score / rationale / visible evidence)

Run:
    uv run python scripts/run_visual_evidence_report.py
"""
from __future__ import annotations
import base64, html as H, io, sys, tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from autodesign.evaluator.quality_rubric import compute_deterministic_report  # noqa: E402
from autodesign.evaluator.tools import tool_vlm_judge, _format_grounding  # noqa: E402
from scripts.visualize_visual_evidence import analyze  # noqa: E402

OUT = _REPO / "out/eval/report/visual_evidence_judged_zh.html"
PICKS = [
    ("Attention Is All You Need", "2017-attention-is-all-you-need"),
    ("Mask R-CNN", "2017-mask-r-cnn"),
    ("PatchRot (自监督 ViT)", "nips2022_patchrot"),
    ("NeRF", "2020-nerf-representing"),
    ("DDPM (扩散模型)", "2020-denoising-diffusion"),
    ("Color-Equivariant CNN", "nips2023_color_equivariant_cnn"),
    ("放疗+替莫唑胺 (NEJM, 假空图)", "2005-radiotherapy-plus-concomitant"),
    ("结直肠癌分子分型 (表格密集)", "2015-the-consensus-molecular-subtypes"),
    ("尘埃红外发射图 (真空图)", "1998-maps-of-dust-infrared"),
    ("GWTC-1 引力波星表", "gwtc-1-a-gravitational-wave"),
]


def b64(img, max_w=860):
    if img.width > max_w:
        img = img.resize((max_w, int(img.height * max_w / img.width)))
    buf = io.BytesIO(); img.save(buf, format="JPEG", quality=82)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def collect():
    recs = []
    for label, frag in PICKS:
        cands = [p for p in (_REPO / "DesignAnything_Poster").rglob("preview.png") if frag in str(p)]
        if not cands:
            print(f"  SKIP {label}: not found ({frag})", flush=True); continue
        prev = cands[0]
        res = analyze(prev)
        with tempfile.TemporaryDirectory() as d:
            rep = compute_deterministic_report(paper=None, candidate_artifact=prev, out_dir=Path(d))
        ve = rep["metric_bundles"]["visual_evidence"]
        grounding = _format_grounding("visual_evidence_use", ve)
        try:
            out = tool_vlm_judge(dimension="visual_evidence_use", image=prev, paper_brief={}, grounding=ve)
            score, rationale = out.get("score_0_10"), str(out.get("rationale") or "")
            evidence = out.get("visible_evidence") or []
            model, status = out.get("model"), out.get("status")
        except Exception as e:  # noqa: BLE001
            score, rationale, evidence, model, status = None, f"判分失败: {type(e).__name__}: {e}", [], None, "error"
        recs.append({"label": label, "img": b64(res["image"]),
                     "raw": res["raw_region_count"], "count": res["figure_region_count"],
                     "area": res["figure_area_ratio"], "text": res["text_coverage"],
                     "empty": ve["no_figures_detected"], "wall": ve["possible_screenshot_wall"],
                     "grounding": grounding.strip(), "score": score, "rationale": rationale,
                     "evidence": evidence, "model": model, "status": status})
        print(f"  {label[:34]:36s} figs={res['raw_region_count']}->{res['figure_region_count']} "
              f"empty={ve['no_figures_detected']} wall={ve['possible_screenshot_wall']} -> score={score}", flush=True)
    return recs


def card(r):
    flags = []
    if r["empty"]:
        flags.append('<span class="pill" style="color:#a01b1b;background:#fde3e3">no_figures_detected</span>')
    if r["wall"]:
        flags.append('<span class="pill" style="color:#a01b1b;background:#fde3e3">possible_screenshot_wall</span>')
    ev = "".join(f"<li>{H.escape(str(x))}</li>" for x in (r["evidence"] or [])[:6])
    sc = r["score"]
    sc_color = "#10683a" if (sc is not None and sc >= 7) else ("#9a5b09" if (sc is not None and sc >= 4) else "#a01b1b")
    return f"""
<div class="card">
 <h3>{H.escape(r['label'])} <span class="pill" style="color:#fff;background:{sc_color}">score {sc}</span></h3>
 <div class="cols">
  <div class="col"><img src="{r['img']}">
    <div class="s">绿=正文图区,蓝=标题 logo,灰=被过滤的细条/噪声</div></div>
  <div class="col">
   <div class="step"><b>① CV 检测(确定性)</b><br>
     图区 <b>{r['raw']}→{r['count']}</b>(原始→shape 过滤) · 面积 <b>{round(r['area']*100)}%</b> · 文字覆盖 {r['text']}<br>
     flags: {' '.join(flags) if flags else '<span class=s>无</span>'}</div>
   <div class="arrow">↓ 注入 VLM</div>
   <div class="step"><b>② 注入 judge 的 grounding(逐字)</b><pre>{H.escape(r['grounding'])}</pre></div>
   <div class="arrow">↓ judge 看图判分</div>
   <div class="step"><b>③ VLM judge 输出</b>(model {H.escape(str(r['model']))})
     <div class="score">score <b style="font-size:19px;color:{sc_color}">{sc}</b>/10</div>
     <div class="rat"><b>rationale:</b> {H.escape(r['rationale'][:600])}</div>
     {f'<div class="rat"><b>visible_evidence:</b><ul>{ev}</ul></div>' if ev else ''}</div>
  </div>
 </div>
</div>"""


def main():
    recs = collect()
    ranked = sorted(recs, key=lambda r: (r["score"] is None, r["score"] if r["score"] is not None else 0))
    srows = "".join(
        f'<tr><td>{H.escape(r["label"])}</td><td class="num">{r["count"]}</td>'
        f'<td class="num">{round(r["area"]*100)}%</td><td class="num">{r["text"]}</td>'
        f'<td>{"空图" if r["empty"] else ""}{" 墙" if r["wall"] else ""}</td>'
        f'<td class="num"><b>{r["score"]}</b></td></tr>'
        for r in ranked)
    scored = [r["score"] for r in recs if r["score"] is not None]
    mean = round(sum(scored)/len(scored), 2) if scored else None
    doc = f"""<!doctype html><meta charset="utf-8"><title>visual_evidence_use 真实判分</title>
<style>
 body{{font:15px/1.65 -apple-system,'PingFang SC',Segoe UI,sans-serif;color:#1c2330;max-width:1080px;margin:0 auto;padding:24px 18px 70px}}
 h1{{font-size:24px}} h2{{font-size:18px;margin-top:24px}} h3{{font-size:15.5px;margin:2px 0 10px}}
 .lede{{background:#f3f8fc;border-left:4px solid #5b8def;padding:12px 16px;border-radius:0 10px 10px 0;font-size:14px}}
 .card{{border:1px solid #e4e7ee;border-radius:12px;padding:14px 16px;margin:18px 0;background:#fff}}
 .cols{{display:flex;gap:16px;flex-wrap:wrap}} .col{{flex:1;min-width:330px}} .col img{{width:100%;border:1px solid #eceff3;border-radius:8px}}
 .step{{background:#fafbfc;border:1px solid #e9edf2;border-radius:8px;padding:9px 11px;margin:5px 0}}
 .arrow{{text-align:center;color:#8a93a6;font-size:12px;margin:1px 0}}
 pre{{background:#f6f8fa;border:1px solid #e4e7ee;border-radius:7px;padding:8px 10px;font-size:11px;white-space:pre-wrap;line-height:1.45;color:#33415c;margin:5px 0 0}}
 .score{{margin:5px 0}} .rat{{font-size:12.5px;color:#33415c;background:#f7f9fc;border-radius:7px;padding:7px 10px;margin-top:5px}}
 .rat ul{{margin:4px 0 0 18px;padding:0}} .rat li{{font-size:12px}}
 .pill{{display:inline-block;font-size:12px;border-radius:11px;padding:2px 9px;margin:1px 2px}} .s{{color:#69707d;font-size:12.5px}}
 table{{border-collapse:collapse;width:100%;margin:8px 0;font-size:13px}} td,th{{border:1px solid #e4e7ee;padding:5px 8px;text-align:left}} th{{background:#f6f7f9}} .num{{text-align:right;font-variant-numeric:tabular-nums}}
 .kpi{{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}} .kpi .b{{border:1px solid #e4e7ee;border-radius:10px;padding:9px 14px}} .kpi .v{{font-size:19px;font-weight:700}} .kpi .l{{font-size:12px;color:#69707d}}
</style>
<h1>visual_evidence_use:真实海报上的检测 + 判分 trajectory</h1>
<p class="lede"><b>判官 = 生产 gpt-5.4(真实模型,非替身)。</b>每张海报展示完整链路:<b>① CV 检测(只接地)→ ② 注入 judge 的 grounding(逐字)→ ③ VLM judge 看图打分</b>。
确定性信号只接地、从不硬扣分;最终分由 judge 给出。本轮已含 P3(grounding v3 恢复区分度)+ P4(墙阈值改文字驱动)。</p>
<div class="kpi">
 <div class="b"><div class="v">{len(recs)}</div><div class="l">真实海报</div></div>
 <div class="b"><div class="v">{mean}</div><div class="l">平均分</div></div>
 <div class="b"><div class="v">gpt-5.4</div><div class="l">判官模型</div></div>
</div>
<h2>总览(按分数升序)</h2>
<table><tr><th>海报</th><th>图数</th><th>图面积</th><th>文字</th><th>flag</th><th>score</th></tr>{srows}</table>
{''.join(card(r) for r in ranked)}
<p class="s" style="margin-top:24px">Generated by scripts/run_visual_evidence_report.py · 判官为真实 gpt-5.4。</p>
"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(doc, encoding="utf-8")
    print(f"\nwrote {OUT} ({len(doc)//1024} KB, {len(recs)} posters, mean score {mean})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
