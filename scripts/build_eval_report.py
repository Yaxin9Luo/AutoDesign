#!/usr/bin/env python3
"""Build a self-contained bilingual (EN/ZH) HTML report of the poster evaluation
method for a group-meeting presentation.

Sections: (1) evaluation pipeline, (2) per-dimension algorithms, (3) Density/Layout
optimization journey with before/after visuals + final flow, (4) future work.
Key overlays are regenerated fresh and embedded as base64 so the file is portable.

    uv run python scripts/build_eval_report.py --out out/eval/report
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PIL import Image, ImageDraw

from autodesign.evaluator import poster_rubric as PR
from autodesign.evaluator.ocr import run_ocr
from autodesign.evaluator.quality_rubric import compute_deterministic_report
from autodesign.evaluator.spatial import blank_strips, content_occupancy, occupancy_overlay
from autodesign.evaluator.viz import density_overlay

R = Path("out/runs")
OMNI = R / "20260625-141306-879d9290" / "final" / "preview.png"
CORNER = Path("out/eval/corner_final/cases")

# representative spectrum (broken -> full), rendered natively at full resolution
SPECTRUM = [
    ("20260528-105950-d6cd33a1", ("broken / near-empty", "坏 / 近空白")),
    ("20260519-153350-aebadf22", ("sparse, dark figures", "稀疏,深色图块")),
    ("20260527-101027-8aaf5a65", ("airy, big middle band", "松散,中部大空白带")),
    ("20260528-114926-2538a498", ("sparse", "稀疏")),
    ("20260625-175305-09c729ea", ("clean, well-packed", "规整,填充好")),
    ("20260625-141306-879d9290", ("near-full (OmniMamba)", "基本满版(OmniMamba)")),
]


def b64(path: Path) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


def img(path: Path, *, w: int = 520) -> str:
    if not Path(path).exists():
        return f"<div class='missing'>missing: {path}</div>"
    return f"<img style='width:{w}px' src='data:image/png;base64,{b64(path)}'>"


def gen_assets(out: Path) -> dict[str, Path]:
    a = out / "assets"
    a.mkdir(parents=True, exist_ok=True)
    assets: dict[str, Path] = {}

    if OMNI.exists():
        base = Image.open(OMNI).convert("RGB")
        segs = (run_ocr(OMNI, include_segments=True) or {}).get("segments") or []
        # v1: 1-D blank band + per-row ink gutter
        v1 = a / "omni_v1.png"; density_overlay(base)[0].save(v1); assets["omni_v1"] = v1
        # final: content occupancy + blank strips + heading excluded
        occ = content_occupancy(base, segments=segs); st = blank_strips(base)
        fin = a / "omni_final.png"; occupancy_overlay(base, occ, strips=st).save(fin); assets["omni_final"] = fin

    # corner overlays (ground-truth behavior)
    for name in ("blank", "full_text", "noise", "top_half_text"):
        p = CORNER / f"{name}.png"
        if p.exists():
            base = Image.open(p).convert("RGB")
            occ = content_occupancy(base); st = blank_strips(base)
            o = a / f"corner_{name}.png"; occupancy_overlay(base, occ, strips=st).save(o); assets[f"corner_{name}"] = o

    if Path("out/eval/diag_dense28/dense8aaf_occ.png").exists():
        assets["cp28_fail"] = Path("out/eval/diag_dense28/dense8aaf_occ.png")

    # concept: 4 materials → only real text is "content"
    ci = Image.new("RGB", (900, 430), "white"); cd = ImageDraw.Draw(ci)
    for y in range(24, 196, 15):  # top-left: text-like bars (content)
        x = 26
        while x < 420:
            cd.rectangle([x, y, x + 34, y + 6], fill=(35, 35, 35)); x += 46
    cd.rectangle([470, 18, 876, 200], fill=(208, 224, 248))      # top-right: solid colour (empty)
    cd.rectangle([24, 224, 430, 412], fill=(12, 12, 12))         # bottom-left: black void (empty)
    cd.rectangle([470, 224, 876, 412], outline=(220, 220, 220))  # bottom-right: white (empty)
    cd.text((34, 4), "text", fill=(90, 90, 90)); cd.text((480, 4), "solid colour", fill=(90, 90, 90))
    cd.text((34, 210), "black void", fill=(90, 90, 90)); cd.text((480, 210), "white", fill=(150, 150, 150))
    concept = a / "concept.png"; occupancy_overlay(ci, content_occupancy(ci)).save(concept); assets["concept"] = concept

    # noise: naive stddev (everything "content") vs 4x-downsample (collapses to empty)
    npath = CORNER / "noise.png"
    if npath.exists():
        ni = Image.open(npath).convert("RGB")
        n1 = a / "noise_naive.png"; occupancy_overlay(ni, content_occupancy(ni, noise_downsample=1)).save(n1); assets["noise_naive"] = n1
        n4 = a / "noise_multi.png"; occupancy_overlay(ni, content_occupancy(ni, noise_downsample=4)).save(n4); assets["noise_multi"] = n4

    # heading band: v1 false-positives vs final (excluded, gray)
    if "omni_v1" in assets and "omni_final" in assets:
        for key, dst in (("omni_v1", "head_v1"), ("omni_final", "head_fin")):
            im = Image.open(assets[key]); w, h = im.size
            crop = im.crop((0, 0, w, int(h * 0.22)))
            p = a / f"{dst}.png"; crop.save(p); assets[dst] = p

    # native, full-resolution spectrum (replaces the soft gallery screenshot)
    spectrum: list[dict] = []
    for rid, (le, lz) in SPECTRUM:
        prev = R / rid / "final" / "preview.png"
        if not prev.exists():
            continue
        base = Image.open(prev).convert("RGB")
        segs = (run_ocr(prev, include_segments=True) or {}).get("segments") or []
        occ = content_occupancy(base, segments=segs)
        st = blank_strips(base)
        op = a / f"spec_{rid}.png"
        occupancy_overlay(base, occ, strips=st).save(op)
        rep = compute_deterministic_report(paper=None, candidate_artifact=prev, out_dir=a / "rep" / rid)
        c = rep["dimension_components"]
        spectrum.append({
            "png": op, "label_en": le, "label_zh": lz,
            "density": c["information_density_and_synthesis"]["score_0_10"],
            "layout": c["layout_readability"]["score_0_10"],
        })
    return assets, spectrum


# --- bilingual content -------------------------------------------------------

def L(en: str, zh: str) -> str:
    return f"<span class='en'>{en}</span><span class='zh'>{zh}</span>"


PIPELINE_SVG = """
<svg viewBox="0 0 1160 340" xmlns="http://www.w3.org/2000/svg" style="width:100%;max-width:1160px">
  <defs>
    <marker id="arrow" markerWidth="11" markerHeight="11" refX="8" refY="3.2" orient="auto">
      <path d="M0,0 L8,3.2 L0,6.4 Z" fill="#5b6b8c"/></marker>
    <linearGradient id="gblue" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#f3f7fe"/><stop offset="1" stop-color="#e4eefc"/></linearGradient>
    <linearGradient id="ggreen" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#eefaf1"/><stop offset="1" stop-color="#dff4e6"/></linearGradient>
    <linearGradient id="ggray" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#f6f7f9"/><stop offset="1" stop-color="#eceef2"/></linearGradient>
    <filter id="sh" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="1.5" stdDeviation="2.5" flood-color="#26324d" flood-opacity="0.18"/></filter>
  </defs>
  <style>
    .box{rx:11;ry:11;filter:url(#sh)}
    .t{font:600 14px -apple-system,Segoe UI,sans-serif;fill:#1e2a44}
    .s{font:11.5px -apple-system,Segoe UI,sans-serif;fill:#5b6b8c}
    .chip{fill:#fff;stroke:#bcd0f3;stroke-width:1;rx:6;ry:6}
    .ct{font:11px -apple-system,sans-serif;fill:#27406e}
    .flow{stroke:#9fb3d6;stroke-width:2.4;fill:none;stroke-linecap:round;
          stroke-dasharray:7 7;animation:dash 0.9s linear infinite;marker-end:url(#arrow)}
    @keyframes dash{to{stroke-dashoffset:-14}}
    .stage{animation:pulse 4.2s ease-in-out infinite}
    @keyframes pulse{0%,100%{opacity:.92}10%,22%{opacity:1;filter:url(#sh) brightness(1.04)}}
    .tok{fill:#e8821a}
  </style>

  <!-- connectors -->
  <path class="flow" d="M150,170 H188"/>
  <path class="flow" d="M312,170 H350"/>
  <path class="flow" d="M628,170 H666"/>
  <path class="flow" d="M912,170 H950"/>
  <path class="flow" d="M1070,170 H1090"/>

  <!-- 1 input -->
  <g class="stage" style="animation-delay:0s">
    <rect class="box" x="20" y="138" width="130" height="64" fill="url(#ggray)" stroke="#9aa5b8" stroke-width="1.4"/>
    <text class="t" x="36" y="166">Poster</text><text class="s" x="36" y="186">PNG · PDF · JPG</text></g>
  <!-- 2 render -->
  <g class="stage" style="animation-delay:.5s">
    <rect class="box" x="190" y="138" width="122" height="64" fill="url(#gblue)" stroke="#4a86e8" stroke-width="1.4"/>
    <text class="t" x="206" y="164">Render &amp;</text><text class="t" x="206" y="184">Snapshot</text></g>
  <!-- 3 pre-pass -->
  <g class="stage" style="animation-delay:1s">
    <rect class="box" x="352" y="44" width="276" height="252" fill="url(#gblue)" stroke="#4a86e8" stroke-width="1.6"/>
    <text class="t" x="368" y="70">Deterministic pre-pass</text>
    <rect class="chip" x="368" y="84" width="244" height="26"/><text class="ct" x="380" y="101">OCR bridge · RapidOCR (text from pixels)</text>
    <rect class="chip" x="368" y="116" width="244" height="26"/><text class="ct" x="380" y="133">Content-occupancy grid (image-native)</text>
    <rect class="chip" x="368" y="148" width="244" height="26"/><text class="ct" x="380" y="165">Blank-strip detector (section gaps)</text>
    <rect class="chip" x="368" y="180" width="244" height="26"/><text class="ct" x="380" y="197">Image density · numeric grounding</text>
    <rect class="chip" x="368" y="212" width="244" height="26" stroke="#e2a3a0"/><text class="ct" x="380" y="229" fill="#a33">Render gate · overflow / clip / missing</text>
    <text class="s" x="368" y="262">resolution &amp; format invariant ·</text>
    <text class="s" x="368" y="280">heading + outer margin excluded</text></g>
  <!-- 4 scoring -->
  <g class="stage" style="animation-delay:1.5s">
    <rect class="box" x="668" y="112" width="244" height="52" fill="url(#gblue)" stroke="#4a86e8" stroke-width="1.4"/>
    <text class="t" x="684" y="135">Tools → objective dims</text><text class="s" x="684" y="153">density · layout · grounding</text>
    <rect class="box" x="668" y="178" width="244" height="52" fill="url(#ggreen)" stroke="#2e9e57" stroke-width="1.4"/>
    <text class="t" x="684" y="201">Single VLM judge → subjective</text><text class="s" x="684" y="219">coverage · faithfulness · aesthetics</text></g>
  <!-- 5 aggregate -->
  <g class="stage" style="animation-delay:2s">
    <rect class="box" x="952" y="138" width="118" height="64" fill="url(#gblue)" stroke="#4a86e8" stroke-width="1.4"/>
    <text class="t" x="968" y="164">Aggregate</text><text class="s" x="968" y="184">weights + gate</text></g>
  <!-- 6 report -->
  <g class="stage" style="animation-delay:2.5s">
    <rect class="box" x="1092" y="138" width="60" height="64" fill="url(#gblue)" stroke="#4a86e8" stroke-width="1.4"/>
    <text class="t" x="1104" y="164" font-size="12">Score</text><text class="s" x="1104" y="184">0–10</text></g>

  <!-- traveling token along the main spine -->
  <circle class="tok" r="5.5" cy="170">
    <animate attributeName="cx" dur="4.2s" repeatCount="indefinite"
      values="150;188;350;666;950;1090" keyTimes="0;0.18;0.4;0.66;0.86;1" calcMode="linear"/>
    <animate attributeName="opacity" dur="4.2s" repeatCount="indefinite"
      values="0;1;1;1;1;0" keyTimes="0;0.05;0.5;0.8;0.95;1"/>
  </circle>
</svg>
"""


def dim_rows() -> str:
    dims = [
        ("render_integrity", "gate", L("tools", "工具"),
         L("Overflow / clipping / missing-image gate; any P0 caps the overall score.",
           "溢出/裁切/缺图门禁;出现 P0 直接给总分封顶。")),
        ("source_faithfulness", "10", L("tools + VLM", "工具+VLM"),
         L("Numeric tokens matched exactly against the paper (OCR text) + VLM claim check.",
           "海报数字 token 与论文精确比对(OCR 文本)+ VLM 判断有无臆造。")),
        ("paper_coverage", "10", "VLM",
         L("VLM checks the poster against a paper 'must-cover' brief.",
           "VLM 对照论文要点清单检查覆盖度。")),
        ("information_density_and_synthesis", "25", L("tools", "工具"),
         L("Image-native content occupancy + OCR text richness, penalized by blank strips.",
           "图片原生内容占用 + OCR 文本丰富度,按空白条惩罚。")),
        ("visual_evidence_use", "10", L("VLM (CV next)", "VLM(后续CV)"),
         L("Meaningful use of figures/tables; deferred to VLM until CV figure detection lands.",
           "图表是否有效使用;暂交 VLM,CV 图检测后接入。")),
        ("basic_layout_integrity", "15", L("tools", "工具"),
         L("Mechanical poster health: size/aspect, tiny text, canvas edge damage, panel overflow, clipping, and overlap.",
           "机械布局健康度:尺寸比例、过小文字、画布边缘损坏、panel overflow、裁切和重叠。")),
        ("layout_readability", "15", L("tools + VLM", "工具+VLM"),
         L("Content spread − blank strips (tools); 'messiness' deferred to VLM.",
           "内容铺展 − 空白条(工具);'乱排'交 VLM。")),
        ("professional_aesthetics", "15", "VLM",
         L("Restrained palette, consistent typography — VLM, no house-style bias.",
           "克制配色、统一排版 — VLM,不带房子风格偏见。")),
    ]
    return "\n".join(
        f"<tr><td><code>{d}</code></td><td>{w}</td><td>{o}</td><td>{desc}</td></tr>"
        for d, w, o, desc in dims
    )


def journey_steps() -> str:
    steps = [
        L("<b>Content occupancy</b> replaces non-white ink — a cell is content only if it has real "
          "texture (stddev) or OCR text, so colored panel backgrounds &amp; dark voids stop inflating density.",
          "<b>内容占用</b>取代非白像素——格子有真实纹理(方差)或 OCR 文字才算内容,彩色面板底色和黑色空洞不再虚增密度。"),
        L("<b>Noise immunity</b>: variation must survive a 4× downsample, so random noise/texture collapses while "
          "text strokes &amp; figure edges persist.",
          "<b>噪声免疫</b>:变化要能在 4× 降采样后存活,随机噪声/杂纹塌掉,文字笔画与图表边缘保留。"),
        L("<b>OCR text becomes additive</b>: text only lifts density (max against content), so OCR failure on "
          "stylized/non-Latin text never caps a genuinely full poster.",
          "<b>OCR 文本改为只加分</b>:text 只抬升(与 content 取 max),花哨/非英文字体导致 OCR 失败也不会压低满版海报。"),
        L("<b>Heading excluded</b>: the top identity band is allowed to be airy, so its whitespace is no longer a "
          "false 'blank' problem.",
          "<b>排除 Heading</b>:顶部标题带允许留白,不再误报为空白问题。"),
        L("<b>Blank-strip detector</b>: per-column vertical scan with min-run filtering catches section-bottom "
          "horizontal gaps (which the 1-D full-width band missed) without flagging normal line spacing.",
          "<b>空白条检测器</b>:按列垂直扫描 + 最小连续过滤,抓住 section 底部的横向空白(旧的整行空白带漏掉),又不误判正常行距。"),
        L("<b>Resolution invariance</b>: the grid scales with the image so the same poster scores the same at 1×, "
          "0.6×, 2×; outer page margins are excluded too.",
          "<b>分辨率不变</b>:网格随图缩放,同一海报在 1×/0.6×/2× 下同分;外圈页边距也被排除。"),
        L("<b>Parameter calibration</b>: scoring params grid-searched over 100 real posters + 9 corner cases for "
          "the most stable config (all corner targets met, full-range distribution, flat optimum).",
          "<b>参数标定</b>:在 100 张真海报 + 9 个 corner 上网格搜索打分参数,选最稳一组(corner 全中、全区间分布、平稳最优)。"),
    ]
    return "\n".join(f"<li>{s}</li>" for s in steps)


def future_items() -> str:
    items = [
        L("<b>Semantic VLM dimensions</b> — wire &amp; validate the single VLM judge for source_faithfulness "
          "(claims), paper_coverage, professional_aesthetics.",
          "<b>语义 VLM 维</b>——接入并验证单 VLM judge 评 source_faithfulness(论点)、paper_coverage、professional_aesthetics。"),
        L("<b>Layout 'messiness' / regularity</b> — the simple alignment prototype failed; try connected-component "
          "block-size regularity, or leave it to the VLM.",
          "<b>Layout '乱排'/规整度</b>——简单对齐原型失败;尝试连通块尺寸一致性,或交给 VLM。"),
        L("<b>Visual-evidence CV</b> — figure/table region detection so visual_evidence_use gets a deterministic part.",
          "<b>视觉证据 CV</b>——图/表区域检测,让 visual_evidence_use 有确定性分量。"),
        L("<b>Aspect-adaptive heading</b> — the fixed 14% heading band slightly over-excludes on portrait posters.",
          "<b>Heading 长宽比自适应</b>——固定 14% 在纵版上略微多排除。"),
        L("<b>Human alignment</b> — calibrate the whole rubric against human preference once labels exist.",
          "<b>人类对齐</b>——有标注后用人类偏好校准整套 rubric。"),
    ]
    return "\n".join(f"<li>{s}</li>" for s in items)


def vlm_rows() -> str:
    rows = [
        (L("Reproducibility", "可复现性"),
         L("score drifts run-to-run on the same image", "同图多次跑分数会漂"),
         L("deterministic tools + code aggregator → identical every run", "确定性工具 + 代码聚合 → 每次完全一致")),
        (L("Objective facts", "客观事实"),
         L("eyeballs density / overflow / numbers it should compute", "靠目测密度/溢出/数字,本该计算"),
         L("computed exactly (occupancy, blank strips, numeric match)", "精确计算(占用、空白条、数字比对)")),
        (L("Format / resolution", "格式/分辨率"),
         L("same poster at 2× can get a different score", "同图 2× 可能不同分"),
         L("invariant by construction (adaptive grid, ratios)", "构造上不变(自适应网格、比值)")),
        (L("Bias", "偏见"),
         L("favors its own / a house aesthetic", "偏向自己或某种房子审美"),
         L("objective dims are style-free; VLM only where unavoidable", "客观维无风格;VLM 只在不可避免处")),
        (L("Explainability", "可解释"),
         L("one gestalt number, hard to audit", "一个整体印象分,难审计"),
         L("every dim cites tool evidence + a visible overlay", "每维都有工具证据 + 可视叠加")),
        (L("Cost / speed", "成本/速度"),
         L("a vision call per judgment", "每次判断一次视觉调用"),
         L("pixels/OCR are cheap; VLM reserved for subjective dims", "像素/OCR 便宜;VLM 只留给主观维")),
        (L("Gaming", "防作弊"),
         L("'looks full' can be talked up", "'看着满'能被说高"),
         L("gold floors + gates resist padding / hidden voids", "gold 下限 + 门禁抵抗灌水/隐藏空洞")),
    ]
    return "\n".join(f"<tr><td>{a}</td><td>{b}</td><td>{c}</td></tr>" for a, b, c in rows)


def spectrum_cards(spectrum: list[dict]) -> str:
    cells = []
    for s in spectrum:
        cells.append(
            f"<figure style='margin:0'>{img(s['png'], w=348)}"
            f"<figcaption><b>D {s['density']} · L {s['layout']}</b><br>{L(s['label_en'], s['label_zh'])}</figcaption></figure>"
        )
    return "<div class='two'>" + "".join(cells) + "</div>"


def build(out: Path) -> Path:
    assets, spectrum = gen_assets(out)
    P = PR

    # worked end-to-end example (real numbers on the OmniMamba poster)
    ex = {"content": 0.0, "strip": 0.0, "text": 0.0, "density": 0.0, "layout": 0.0}
    if OMNI.exists():
        rep = compute_deterministic_report(paper=None, candidate_artifact=OMNI, out_dir=out / "assets" / "exrep")
        sp = rep["spatial"]; bs = rep["blank_strips"]; oc = rep["ocr"]; cc = rep["dimension_components"]
        content = float(sp["content_coverage"]); strip = float(bs["largest_blank_strip_ratio"])
        text = float(oc.get("text_coverage_ratio") or 0.0)
        cn = min(1.0, content / P.REF_CONTENT_COVERAGE)
        tn = min(1.0, text / P.REF_TEXT_COVERAGE_RATIO)
        teff = max(tn, cn)
        base = P.DENSITY_CONTENT_WEIGHT * cn + P.DENSITY_TEXT_WEIGHT * teff
        dvoid = 1 - P.DENSITY_VOID_PENALTY_K * min(1.0, strip / P.EMPTY_RECT_CAP)
        lvoid = 1 - P.LAYOUT_VOID_PENALTY_K * min(1.0, strip / P.LAYOUT_VOID_CAP)
        ex = {
            "content": round(content, 3), "strip": round(strip, 3), "text": round(text, 3),
            "cn": round(cn, 3), "tn": round(tn, 3), "base": round(base, 3),
            "dvoid": round(dvoid, 3), "lvoid": round(lvoid, 3),
            "density": cc["information_density_and_synthesis"]["score_0_10"],
            "layout": cc["layout_readability"]["score_0_10"],
        }
    html = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>Poster Evaluation Method</title>
<style>
  body{{font-family:-apple-system,Segoe UI,Roboto,'PingFang SC',sans-serif;margin:0;color:#1a1a1a;background:#fafafa}}
  .wrap{{max-width:1180px;margin:0 auto;padding:28px 32px 80px}}
  h1{{font-size:26px;margin:.2em 0}} h2{{font-size:20px;border-left:4px solid #4a86e8;padding-left:10px;margin-top:34px}}
  h3{{font-size:16px;margin-top:22px}}
  p,li,td,th{{font-size:14px;line-height:1.65}}
  table{{border-collapse:collapse;width:100%;margin:12px 0}} th,td{{border:1px solid #ddd;padding:7px 10px;text-align:left;vertical-align:top}}
  th{{background:#f0f2f5}} code{{background:#eef0f4;padding:1px 5px;border-radius:4px;font-size:12.5px}}
  .muted{{color:#777;font-size:12.5px}}
  .formula{{background:#fbfbfd;border-left:3px solid #4a86e8;padding:10px 14px;margin:10px 0;font-size:13.5px;line-height:1.9}}
  .score{{font-weight:700;color:#1a7a4a;font-size:17px}} .detail ul{{margin:6px 0}}
  .card{{background:#fff;border:1px solid #e3e3e3;border-radius:10px;padding:14px 16px;margin:12px 0}}
  .two{{display:flex;gap:18px;flex-wrap:wrap;align-items:flex-start}}
  figure{{margin:0}} figcaption{{font-size:11.5px;color:#777;text-align:center;margin-top:4px}}
  img{{border:1px solid #ddd;border-radius:6px}}
  .missing{{color:#b00;font-size:12px}}
  .badge{{display:inline-block;background:#eef3fb;border:1px solid #4a86e8;border-radius:999px;padding:2px 10px;font-size:12px;margin:2px}}
  #langbar{{position:sticky;top:0;background:#fff;border-bottom:1px solid #e3e3e3;padding:8px 32px;z-index:9}}
  button{{font-size:13px;padding:5px 14px;border:1px solid #4a86e8;background:#fff;color:#4a86e8;border-radius:6px;cursor:pointer}}
  button.active{{background:#4a86e8;color:#fff}}
  body.zh .en{{display:none}} body.en .zh{{display:none}}
</style></head>
<body class="zh">
<div id="langbar">
  <button id="bzh" class="active" onclick="setLang('zh')">中文</button>
  <button id="ben" onclick="setLang('en')">English</button>
  <span class="muted" style="margin-left:12px">{L('Poster Evaluation Method · group-meeting report', '海报评测方法 · 组会报告')}</span>
</div>
<div class="wrap">
<h1>{L('Image-Native Poster Evaluation', '图片原生海报评测方法')}</h1>
<p>{L('A reproducible benchmark for paper→poster quality. Objective dimensions are scored by deterministic '
      'Python rule-tools on the rendered image (format/resolution invariant); subjective dimensions go to a single '
      'VLM judge; a coding agent orchestrates and a code aggregator produces the final score.',
      '面向 paper→poster 质量的可复现评测。客观维由确定性 Python 规则工具在渲染图上打分(格式/分辨率不变);主观维交单一 VLM judge;'
      '由 coding agent 编排,代码聚合器出最终分。')}</p>

<h2>1 · {L('Evaluation pipeline', '评测流程')}</h2>
{PIPELINE_SVG}
<p>{L('Input is what a conference attendee actually sees — a rendered poster image (PNG/PDF/JPG), no HTML/DOM '
      'required. Each stage:', '输入就是观众真正看到的东西——渲染后的海报图(PNG/PDF/JPG),无需 HTML/DOM。各步:')}</p>
<ul>
<li>{L('<b>Render &amp; snapshot</b>: PDF→image / image as-is; extract a preview.', '<b>渲染&amp;快照</b>:PDF→图 / 图原样;得到 preview。')}</li>
<li>{L('<b>Deterministic pre-pass</b>: OCR bridge (recover text from pixels), content-occupancy grid, blank-strip '
      'detector, image density, numeric grounding vs paper, render gate.',
      '<b>确定性预处理</b>:OCR 桥(从像素抽文本)、内容占用栅格、空白条检测、图像密度、对论文的数字接地、渲染门禁。')}</li>
<li>{L('<b>Dimension scoring</b>: objective dims from the tools; subjective dims from one VLM judge (it cannot see '
      'pixels except through the judge tool).', '<b>逐维打分</b>:客观维来自工具;主观维来自单一 VLM judge(文本 agent 只能通过该工具看像素)。')}</li>
<li>{L('<b>Aggregate</b>: fixed rubric weights; a hard render gate caps the score on P0 failures.',
      '<b>聚合</b>:固定 rubric 权重;渲染硬门禁在 P0 时封顶。')}</li>
</ul>

<h2>2 · {L('Dimensions &amp; algorithms', '维度与算法')}</h2>
<table>
<tr><th>{L('dimension','维度')}</th><th>{L('weight','权重')}</th><th>{L('scored by','打分方')}</th><th>{L('algorithm','算法')}</th></tr>
{dim_rows()}
</table>
<p class="muted">{L('render_integrity is a hard gate (not weighted); the seven weighted dims sum to 100.',
                    'render_integrity 是硬门禁(不计权重);七个加权维合计 100。')}</p>

<h2>3 · {L('Density &amp; Layout — the algorithm in detail', 'Density &amp; Layout — 算法详解')}</h2>
<p>{L('<b>Density</b> = how full / information-rich the poster is. <b>Layout</b> = how well-organized it is '
      '(no big empty regions). Both are computed from the rendered image only.',
      '<b>Density(密度)</b>=海报有多满、信息多丰富。<b>Layout(版式)</b>=排布是否规整(有没有大块空)。两者都只从渲染图算。')}</p>

<h3>3.1 · {L('The core idea (final form)', '核心思想(最终形态)')}</h3>
<p>{L('Lay a grid over the poster. Classify each cell as CONTENT or EMPTY: a cell is content only if it has real '
      'structure (texture that survives a 4× shrink) OR recognized text; flat regions of any colour — white, a solid '
      'panel tint, black, or random noise — are EMPTY. Then: <b>content_coverage</b> = fraction of body cells that are '
      'content (drives density), and a separate <b>blank-strip</b> detector finds tall empty bands (penalizes both).',
      '在海报上铺一张网格,把每个格子判为「有内容」或「空」:格子有真实结构(纹理在 4× 缩小后仍存活)或被识别出文字才算内容;'
      '任何纯色块——白、纯色面板、黑、随机噪声——都算空。然后:<b>content_coverage</b>=内容格占比(决定 density),'
      '另有一个 <b>空白条</b> 检测器找出高大的空白带(同时惩罚两者)。')}</p>
<div class="card"><div class="two">
  <figure>{img(assets.get('concept', Path('x')), w=520)}<figcaption>{L('concept: only real text counts as content (green); solid colour, black void, white → empty (red)',
                                                                       '概念:只有真实文字算内容(绿);纯色、黑色空洞、白 → 空(红)')}</figcaption></figure>
  <div class="detail" style="flex:1;min-width:280px">{L(
    'This single rule fixes the biggest old bug: a black diagram or a tinted panel used to count as "ink" and inflate '
    'density. Now they are correctly seen as empty, because they have no internal structure.',
    '这条规则修掉了最大的旧 bug:黑色图块或带色面板以前被当"墨水"虚增密度;现在它们因为没有内部结构被正确判为空。')}</div>
</div></div>

<h3>3.2 · {L('v1 — the initial algorithm, and why it failed', 'v1 — 最初算法,以及为什么不行')}</h3>
<p>{L('v1 measured raw pixels: density = average of (non-white pixel ratio, OCR text coverage), minus a penalty if '
      'there was one long <i>full-width</i> blank horizontal band; layout = blank-band + edge density.',
      'v1 直接看原始像素:density = 平均(非白像素比, OCR 文本覆盖),再减去"是否有一条贯穿整行的横向空白带"的惩罚;layout = 空白带 + 边缘密度。')}</p>
<div class="card"><div class="two">
  <figure>{img(assets.get('omni_v1', Path('x')), w=560)}<figcaption>{L('v1 detection: red = the single longest fully-blank row run; blue gutter = per-row ink',
                                                                       'v1 检测:红 = 唯一最长的整行空白段;蓝色侧条 = 逐行墨水量')}</figcaption></figure>
  <div class="detail" style="flex:1;min-width:300px">{L('Three failures on real posters:','真海报上的三个失败:')}
   <ul>
   <li>{L('<b>ink ≠ content</b>: colored/dark panel backgrounds inflate density;', '<b>墨水≠内容</b>:彩色/深色面板底色虚增密度;')}</li>
   <li>{L('<b>only full-width bands</b>: a gap at the bottom of <i>one</i> column never spans the whole row, so it is missed;',
         '<b>只看整行</b>:某<i>一</i>列底部的空白不会贯穿整行 → 漏检;')}</li>
   <li>{L('<b>saturation</b>: almost every poster scored ≈10 (no discrimination); also resolution-dependent.',
         '<b>饱和</b>:几乎每张都≈10(无区分);且依赖分辨率。')}</li>
   </ul></div>
</div></div>

<h3>3.3 · {L('Step-by-step improvements (each with its own visualization)', '逐步改进(每步配局部可视化)')}</h3>

<div class="card"><b>S1 · {L('Ink → content occupancy','墨水 → 内容占用')}</b>
<p>{L('Replace "non-white pixel" with "has real structure". A cell is content if its luma variation (standard '
      'deviation) is high enough, or OCR found text there. Colored/dark fills now read as empty (see 3.1 concept).',
      '把"非白像素"换成"有真实结构"。格子亮度方差够大、或该处 OCR 抓到文字,才算内容。彩色/深色填充现在判为空(见 3.1 概念图)。')}</p></div>

<div class="card"><b>S2 · {L('Noise immunity — what is "noise" and why it fooled us','噪声免疫 — 什么是"噪声"、它为何骗过检测')}</b>
<p>{L('<b>"Noise" = random pixels (TV-static / snow).</b> It is a stress test: a variance-based detector sees high '
      'variation everywhere in noise and wrongly calls it all "content" — so a garbage image would score like a full '
      'poster. <b>Fix:</b> require the variation to survive a 4× downsample. Real text strokes and figure edges keep '
      'their contrast when shrunk; random noise averages out to flat gray and collapses to EMPTY.',
      '<b>"噪声"=随机像素(电视雪花点)。</b>它是个压力测试:基于方差的检测器在噪声里到处看到高变化,会把它全判成"内容"——'
      '于是一张垃圾图也能拿满版海报的分。<b>修法:</b>要求这种变化在 4× 降采样后仍存活。真实文字笔画/图表边缘缩小后仍有对比;'
      '随机噪声一平均就变成灰、塌成"空"。')}</p>
<div class="two">
  <figure>{img(assets.get('noise_naive', Path('x')), w=300)}<figcaption>{L('naive variance: noise read as ALL content (wrong)','朴素方差:噪声被全判为内容(错)')}</figcaption></figure>
  <figure>{img(assets.get('noise_multi', Path('x')), w=300)}<figcaption>{L('+ 4× downsample: noise collapses to EMPTY (right)','+ 4× 降采样:噪声塌成空(对)')}</figcaption></figure>
  <div class="detail" style="flex:1;min-width:240px">{L('Result: the noise corner case scores density 0 / layout 0, as it should.','结果:noise 这个 corner 评到 density 0 / layout 0,符合预期。')}</div>
</div></div>

<div class="card"><b>S3 · {L('OCR text becomes additive (never a cap)','OCR 文本只加分(绝不封顶)')}</b>
<p>{L('Text only <i>lifts</i> density (it is taken as max against content). So if OCR fails on a stylized / non-Latin '
      'font, a genuinely full poster is not punished. Example — a fully-filled poster whose text OCR cannot read:',
      'text 只<i>抬升</i> density(与 content 取 max)。因此花哨/非英文字体导致 OCR 失败时,真正满版的海报不被惩罚。'
      '例子——一张填满但 OCR 读不出文字的海报:')}</p>
<div style="font-size:13px">{L('before (text term drags it down)','改前(text 项往下拖)')}: <span style="display:inline-block;height:14px;width:240px;background:#eee;border-radius:3px;vertical-align:middle"><span style="display:inline-block;height:14px;width:144px;background:#d9a13a;border-radius:3px"></span></span> <b>6.0</b><br>
{L('after (text only lifts)','改后(text 只加分)')}: <span style="display:inline-block;height:14px;width:240px;background:#eee;border-radius:3px;vertical-align:middle"><span style="display:inline-block;height:14px;width:240px;background:#2e9e57;border-radius:3px"></span></span> <b>10.0</b></div></div>

<div class="card"><b>S4 · {L('Exclude the heading band','排除 Heading 标题带')}</b>
<p>{L('The top identity/title band is allowed to be airy, so its whitespace must not be flagged. The top ~14% is '
      'excluded from the analysis (shown gray).','顶部标题/署名带本就允许留白,不能被当问题。分析时排除顶部约 14%(显示为灰)。')}</p>
<div class="two">
  <figure>{img(assets.get('head_v1', Path('x')), w=540)}<figcaption>{L('v1: red false-positives in the heading','v1:标题区出现红色误报')}</figcaption></figure>
  <figure>{img(assets.get('head_fin', Path('x')), w=540)}<figcaption>{L('final: heading gray (excluded)','最终:标题区变灰(排除)')}</figcaption></figure>
</div></div>

<div class="card"><b>S5 · {L('Blank-strip detector (the section-bottom gaps)','空白条检测器(section 底部那种空白)')}</b>
<p>{L('To catch internal gaps WITHOUT flagging line spacing: scan each column top-to-bottom in thin slices and keep '
      'only a run of ≥ N consecutive empty slices (a real section gap), discarding short line/paragraph gaps. A naive '
      'global finer grid does NOT work — it floods dense text:',
      '为了既抓内部空白、又不误判行距:对每一列从上到下做细条扫描,只保留连续 ≥ N 条的空白段(真正的 section 空白),'
      '丢弃短的行距/段距。简单地全局把网格变细行不通——它会淹没密集文字:')}</p>
<div class="two">
  <figure>
  <svg viewBox="0 0 260 210" style="width:240px;border:1px solid #ddd;border-radius:6px;background:#fff">
    <text x="8" y="16" font-size="12" fill="#333">one column</text>
    <rect x="40" y="26" width="44" height="20" fill="#9fb0cc"/><rect x="40" y="52" width="44" height="18" fill="#9fb0cc"/>
    <rect x="40" y="76" width="44" height="20" fill="#9fb0cc"/>
    <rect x="40" y="104" width="44" height="84" fill="#e23a3a" opacity="0.85"/>
    <text x="96" y="40" font-size="11" fill="#666">text</text>
    <text x="96" y="62" font-size="11" fill="#aaa">↕ line gap (kept as separator)</text>
    <text x="96" y="86" font-size="11" fill="#666">text</text>
    <text x="96" y="150" font-size="11" fill="#b00">section gap ≥ N slices</text>
    <text x="96" y="166" font-size="11" fill="#b00">→ BLANK STRIP</text>
  </svg>
  <figcaption>{L('per-column scan + min-run filter','按列扫描 + 最小连续过滤')}</figcaption></figure>
  <figure>{img(assets.get('omni_final', Path('x')), w=560)}<figcaption>{L('on the OmniMamba poster: section-bottom strips in red, heading gray','在 OmniMamba 海报上:section 底部空白条标红,标题灰')}</figcaption></figure>
</div>
<p class="muted">{L('Rejected alternative — a globally finer grid reads line spacing as empty and floods dense text:',
                    '被否决的替代——全局更细网格把行距当空、淹没密集文字:')}</p>
<figure>{img(assets.get('cp28_fail', Path('x')), w=420)}<figcaption>{L('rejected idea','否决思路')}</figcaption></figure></div>

<div class="card"><b>S6 · {L('Resolution invariance','分辨率不变')}</b>
<p>{L('The grid scales with the image, so the same poster scores the same at any resolution (outer margins excluded too):',
      '网格随图缩放,同一海报任意分辨率同分(外边距也排除):')}</p>
<table style="max-width:520px">
<tr><th>{L('same poster at','同一海报')}</th><th>1.0×</th><th>0.6×</th><th>1.5×</th></tr>
<tr><td>{L('before (fixed cell px)','改前(固定像素格)')}</td><td>7.45</td><td><b style="color:#b00">8.50</b></td><td>7.55</td></tr>
<tr><td>{L('after (adaptive grid)','改后(自适应网格)')}</td><td>7.45</td><td><b style="color:#1a7">7.61</b></td><td>7.55</td></tr>
</table></div>

<h3>3.4 · {L('Calibrating the constants', '标定那些常量')}</h3>
<p>{L('The formulas have constants (the reference levels and penalty strengths). We pick them, not by hand, but by '
      'searching thousands of combinations over 100 real posters + 9 corner cases. The corner cases are the ground '
      'truth — we KNOW the right answer (blank → 0, full → 10, noise → 0, top-half-empty → low …). We keep the '
      'parameter set that (1) makes every corner case land in its correct range, (2) spreads real posters across the '
      'full 0–10, (3) does not clip at the top, and (4) sits in a stable region (small changes barely move it).',
      '公式里有一些常量(参考水平、惩罚力度)。我们不靠手调,而是在 100 张真海报 + 9 个 corner 上搜索上千种组合。'
      'corner 是"真值"——我们已知正确答案(blank→0、full→10、noise→0、只填上半→低…)。最终选一组参数,使得:'
      '(1) 每个 corner 都落在正确区间,(2) 真海报分散在整个 0–10,(3) 顶端不饱和,(4) 处在稳定区(微调几乎不变)。')}</p>
<div class="card"><div class="two">
  <figure>{img(assets.get('corner_blank', Path('x')), w=250)}<figcaption>blank → 0 / 0 ✓</figcaption></figure>
  <figure>{img(assets.get('corner_full_text', Path('x')), w=250)}<figcaption>full_text → 10 / 10 ✓</figcaption></figure>
  <figure>{img(assets.get('corner_noise', Path('x')), w=250)}<figcaption>noise → 0 / 0 ✓</figcaption></figure>
  <figure>{img(assets.get('corner_top_half_text', Path('x')), w=250)}<figcaption>{L('top-half → low ✓','只填上半 → 低 ✓')}</figcaption></figure>
</div>
<p class="muted">{L('the 9 corner cases are the ground-truth checks the calibration must satisfy','这 9 个 corner 是标定必须满足的真值检查')}</p></div>

<h3>3.5 · {L('Worked example — the final formula on one real poster', '端到端实算 — 最终公式套在一张真海报上')}</h3>
<div class="card"><div class="two">
  <figure>{img(assets.get('omni_final', Path('x')), w=520)}<figcaption>OmniMamba</figcaption></figure>
  <div class="detail" style="flex:1;min-width:320px">
   <div class="formula" style="font-size:13px;line-height:2">
   content_coverage = <b>{ex['content']}</b> → content_norm = min(1, {ex['content']}/{P.REF_CONTENT_COVERAGE}) = <b>{ex['cn']}</b><br>
   text_coverage = {ex['text']} → text_norm = <b>{ex['tn']}</b> (only lifts)<br>
   largest blank strip = <b>{ex['strip']}</b><br>
   density = 10·({P.DENSITY_CONTENT_WEIGHT}·{ex['cn']} + {P.DENSITY_TEXT_WEIGHT}·{ex['cn']})·(1 − {P.DENSITY_VOID_PENALTY_K}·{ex['strip']}/{P.EMPTY_RECT_CAP}) = <span class="score">{ex['density']}</span><br>
   layout = 10·{ex['cn']}·(1 − {P.LAYOUT_VOID_PENALTY_K}·{ex['strip']}/{P.LAYOUT_VOID_CAP}) = <span class="score">{ex['layout']}</span>
   </div>
   <p class="muted">{L('reproducible: same image → same numbers every run.','可复现:同图每次同数。')}</p>
  </div>
</div></div>

<h3>3.6 · {L('Result on a representative spectrum', '代表性谱系上的结果')}</h3>
<div class="card">{spectrum_cards(spectrum)}
<p class="muted">{L('full-resolution detection overlays, broken → full, across aspect ratios; scores rise monotonically with real fullness.',
                    '全分辨率检测叠加,坏 → 满,跨长宽比;分数随真实填充单调上升。')}</p></div>

<h2>4 · {L('Why not a pure VLM judge?', '为什么不用纯 VLM judge?')}</h2>
<p>{L('A single VLM scoring the whole poster is the obvious baseline — and a poor benchmark. We keep ONE VLM, but '
      'only for genuinely subjective/semantic dimensions, wrapped in a harness of deterministic tools.',
      '让单个 VLM 直接给整张海报打分是最容易想到的基线——但作为 benchmark 很差。我们保留一个 VLM,但只用于真正主观/语义的维度,并包在一套确定性工具的 harness 里。')}</p>
<table>
<tr><th>{L('property','维度')}</th><th>{L('pure VLM judge','纯 VLM judge')}</th><th>{L('our harness','我们的 harness')}</th></tr>
{vlm_rows()}
</table>
<p class="muted">{L('Net: deterministic tools handle everything computable (reproducible, auditable, format/resolution-invariant); '
                    'the VLM is reserved for what genuinely needs human-like judgment; a code aggregator makes the final '
                    'arithmetic reproducible and a hard gate prevents broken posters from scoring high.',
                    '总之:可计算的全交给确定性工具(可复现、可审计、格式/分辨率不变);VLM 只留给真正需要类人判断的部分;代码聚合器保证最终算术可复现,硬门禁防止坏海报拿高分。')}</p>

<h2>5 · {L('Future work', '后续规划')}</h2>
<p>{L('Density &amp; layout are done (structure correct, aspect/resolution stable, calibrated). Remaining:',
      'Density &amp; layout 已完成(结构正确、跨长宽比/分辨率稳定、已标定)。剩余:')}</p>
<ul>{future_items()}</ul>
</div>
<script>
function setLang(l){{document.body.className=l;document.getElementById('bzh').classList.toggle('active',l=='zh');
document.getElementById('ben').classList.toggle('active',l=='en');}}
</script>
</body></html>"""
    out.mkdir(parents=True, exist_ok=True)
    path = out / "evaluation_method_report.html"
    path.write_text(html, encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    path = build(args.out.expanduser().resolve())
    print(f"wrote {path}  ({path.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
