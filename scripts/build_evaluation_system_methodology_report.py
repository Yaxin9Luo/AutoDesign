#!/usr/bin/env python3
"""Add benchmark-dimension visual examples to the methodology HTML report."""

from __future__ import annotations

import html
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageFont

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from autodesign.evaluator.ocr import run_ocr  # noqa: E402
from autodesign.evaluator.poster_rubric import DIMENSIONS, GATE_CEILING_SCORE  # noqa: E402
from autodesign.evaluator.spatial import basic_layout_integrity, content_occupancy  # noqa: E402


REPORT_DIR = _REPO / "out/eval/report/evaluation_system_methodology"
REPORT_HTML = REPORT_DIR / "evaluation_system_methodology_zh.html"
ASSET_DIR = REPORT_DIR / "assets/examples"
BENCHMARK_ROOTS = (
    _REPO / "out/eval/report/poster_benchmark_main_table",
    _REPO / "out/eval/report/poster_benchmark_cleanup_smoke",
)


@dataclass
class CandidateReport:
    path: Path
    final: dict[str, Any]
    deterministic: dict[str, Any]
    dims: dict[str, dict[str, Any]]

    @property
    def candidate(self) -> str:
        return str(self.final.get("candidate_name") or self.path.parent.name)

    @property
    def artifact(self) -> Path:
        raw = self.final.get("artifact")
        if raw:
            path = Path(str(raw)).expanduser()
            if path.exists():
                return path
        fallback = self.path.parent / "judge_input.jpg"
        return fallback

    @property
    def judge_input(self) -> Path:
        path = self.path.parent / "judge_input.jpg"
        return path if path.exists() else self.artifact


DIM_LABELS = {
    "source_faithfulness": "源文忠实性",
    "paper_coverage": "论文核心覆盖",
    "information_density_and_synthesis": "信息密度与综合表达",
    "visual_evidence_use": "视觉证据使用",
    "basic_layout_integrity": "基础布局完整性",
    "layout_readability": "版面可读性",
    "professional_aesthetics": "专业审美与学术成品感",
}

DIM_OWNERS = {
    "source_faithfulness": "Python 数字接地 + VLM 判断",
    "paper_coverage": "VLM 判断",
    "information_density_and_synthesis": "Python 图像规则",
    "visual_evidence_use": "Python 图表定位 + VLM 判断",
    "basic_layout_integrity": "Python 图像规则",
    "layout_readability": "Python 版面信号 + VLM 判断",
    "professional_aesthetics": "VLM 判断",
}

DIM_WEIGHTS = {dimension.id: dimension.weight for dimension in DIMENSIONS}


EXAMPLE_STYLE = """
.dimension-example{margin:16px 0 4px;padding-top:14px;border-top:1px dashed #cbd5e1}
.dimension-example h4{margin:0 0 10px;font-size:15px;color:#334155}
.example-card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:16px;display:grid;grid-template-columns:minmax(540px,58%) 1fr;gap:18px}
.example-card h3{margin:0 0 6px;font-size:17px}
.example-media{border:1px solid #d8e0ea;border-radius:8px;background:#f8fafc;overflow:hidden}
.example-media img{display:block;width:100%;height:auto}
.example-score{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:6px 0 8px}
.score-badge{display:inline-flex;align-items:center;justify-content:center;min-width:54px;border-radius:6px;background:#111827;color:#fff;font-weight:760;font-size:18px;padding:4px 7px}
.owner-badge{display:inline-block;border-radius:999px;background:#eef2ff;border:1px solid #c7d2fe;padding:2px 8px;font-size:12px;color:#3730a3}
.example-card .candidate{font-size:12px;color:var(--muted);word-break:break-word;margin:4px 0 8px}
.signal-table{font-size:12px;margin:8px 0}
.signal-table th,.signal-table td{padding:5px 6px}
.problem-box{background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;padding:9px 10px;margin:8px 0}
.problem-box b{color:#9a3412}
.evidence-list{margin:6px 0 0 17px;padding:0}
.evidence-list li{font-size:12px;line-height:1.45;margin:3px 0}
.legend{display:flex;flex-wrap:wrap;gap:6px;margin:7px 0 0}
.legend span{font-size:11px;border:1px solid #d8dee9;border-radius:999px;padding:1px 7px;background:#fff}
.gate-section{margin-top:26px}
.gate-intro{max-width:980px;color:var(--muted);line-height:1.65}
.gate-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin-top:14px}
.gate-card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:16px}
.gate-card h3{margin:10px 0 6px;font-size:17px}
.gate-media{border:1px solid #d8e0ea;border-radius:8px;background:#f8fafc;overflow:hidden}
.gate-media img{display:block;width:100%;height:auto}
.gate-scoreline{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:9px 0}
.gate-pill{display:inline-block;border-radius:999px;background:#fee2e2;border:1px solid #fecaca;color:#991b1b;font-size:12px;font-weight:760;padding:2px 9px}
.gate-arrow{color:#64748b;font-weight:760}
.gate-final{display:inline-flex;align-items:center;justify-content:center;border-radius:6px;background:#991b1b;color:#fff;font-size:18px;font-weight:780;padding:4px 9px}
.gate-note{background:#fef2f2;border:1px solid #fecaca;border-radius:8px;color:#7f1d1d;padding:9px 10px;margin:8px 0;font-size:13px;line-height:1.5}
@media (max-width:1100px){
  .example-card{grid-template-columns:1fr}
  .gate-grid{grid-template-columns:1fr}
}
"""

DETAIL_ANCHORS = {
    "source_faithfulness": "1. 源文忠实性",
    "paper_coverage": "2. 论文核心覆盖",
    "information_density_and_synthesis": "3. 信息密度与综合表达",
    "visual_evidence_use": "4. 视觉证据使用",
    "basic_layout_integrity": "5. 基础布局完整性",
    "layout_readability": "6. 版面可读性",
    "professional_aesthetics": "7. 专业审美与学术成品感",
}


def main() -> int:
    if not REPORT_HTML.exists():
        raise SystemExit(f"missing methodology report: {REPORT_HTML}")
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    reports = _load_reports()
    if not reports:
        raise SystemExit("no benchmark reports found; run scripts/run_poster_benchmark_main_table.py first")

    selected = {
        "source_faithfulness": _select_source_faithfulness_example(reports),
        "paper_coverage": _select_min(reports, "paper_coverage"),
        "information_density_and_synthesis": _select_min(reports, "information_density_and_synthesis"),
        "visual_evidence_use": _select_min_with(reports, "visual_evidence_use", _has_visual_grounding),
        "basic_layout_integrity": _select_min_with(reports, "basic_layout_integrity", _has_basic_signal),
        "layout_readability": _select_min(reports, "layout_readability"),
        "professional_aesthetics": _select_min(reports, "professional_aesthetics"),
    }

    examples = {
        dim: _build_example(dim, report)
        for dim, report in selected.items()
        if report is not None
    }

    doc = REPORT_HTML.read_text(encoding="utf-8")
    doc = _inject_style(doc, EXAMPLE_STYLE)
    doc = _remove_examples_toc(doc)
    doc = _remove_section(doc, "examples")
    doc = _remove_section(doc, "hard-gate-examples")
    doc = _remove_inline_examples(doc)
    doc = _sync_methodology_weights(doc)
    doc = _inject_hard_gate_toc(doc)
    doc = _inject_hard_gate_section(doc, reports)
    doc = _inject_inline_examples(doc, examples)
    REPORT_HTML.write_text(doc, encoding="utf-8")
    print(f"wrote {REPORT_HTML}")
    print(f"assets {ASSET_DIR}")
    return 0


def _load_reports() -> list[CandidateReport]:
    reports: list[CandidateReport] = []
    for root in BENCHMARK_ROOTS:
        for path in sorted(root.glob("candidates/*/*/*/poster_quality_report.json")):
            try:
                final = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            det_path = path.parent / "deterministic/deterministic_report.json"
            try:
                deterministic = json.loads(det_path.read_text(encoding="utf-8")) if det_path.exists() else {}
            except (OSError, json.JSONDecodeError):
                deterministic = {}
            dims = {
                str(item.get("id")): item
                for item in final.get("dimensions") or []
                if isinstance(item, dict) and item.get("id")
            }
            if dims:
                reports.append(CandidateReport(path=path, final=final, deterministic=deterministic, dims=dims))
    return reports


def _score(report: CandidateReport, dim: str) -> float | None:
    value = report.dims.get(dim, {}).get("score_0_10")
    return float(value) if isinstance(value, (int, float)) else None


def _select_min(reports: list[CandidateReport], dim: str) -> CandidateReport | None:
    scored = [(score, report) for report in reports if (score := _score(report, dim)) is not None and report.judge_input.exists()]
    return min(scored, key=lambda item: item[0])[1] if scored else None


def _select_min_with(
    reports: list[CandidateReport],
    dim: str,
    predicate: Callable[[CandidateReport], bool],
) -> CandidateReport | None:
    scored = [
        (score, report)
        for report in reports
        if (score := _score(report, dim)) is not None and report.judge_input.exists() and predicate(report)
    ]
    return min(scored, key=lambda item: item[0])[1] if scored else _select_min(reports, dim)


def _select_source_faithfulness_example(reports: list[CandidateReport]) -> CandidateReport | None:
    candidates: list[tuple[tuple[float, ...], CandidateReport]] = []
    for report in reports:
        score = _score(report, "source_faithfulness")
        numeric = (report.deterministic.get("metric_bundles") or {}).get("numeric_token_exact_match") or {}
        if score is None or not report.judge_input.exists() or not numeric.get("available"):
            continue
        fabricated = float(numeric.get("salient_fabricated") or 0)
        near_miss = float(numeric.get("salient_near_miss") or 0)
        salient = float(numeric.get("salient_token_count") or 0)
        if fabricated + near_miss <= 0 or salient < 8:
            continue
        priority = (
            1.0 if fabricated >= 5 and score <= 6.5 else 0.0,
            fabricated,
            near_miss,
            min(salient, 40.0),
            -score,
        )
        candidates.append((priority, report))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    return _select_min_with(reports, "source_faithfulness", _has_numeric_signal)


def _has_numeric_signal(report: CandidateReport) -> bool:
    numeric = (report.deterministic.get("metric_bundles") or {}).get("numeric_token_exact_match") or {}
    return bool(numeric.get("available") and numeric.get("salient_token_count"))


def _has_visual_grounding(report: CandidateReport) -> bool:
    visual = (report.deterministic.get("metric_bundles") or {}).get("visual_evidence") or {}
    return bool(visual.get("available"))


def _has_basic_signal(report: CandidateReport) -> bool:
    basic = (report.deterministic.get("metric_bundles") or {}).get("basic_layout_integrity") or {}
    return bool(basic.get("available") and (basic.get("findings_count") or basic.get("penalties")))


def _build_example(dim: str, report: CandidateReport) -> dict[str, Any]:
    dim_score = report.dims.get(dim, {})
    asset_name = _safe_name(f"{dim}_{report.candidate}")
    image_path = ASSET_DIR / f"{asset_name}.jpg"
    legend: list[str] = []
    if dim == "source_faithfulness":
        image_path = ASSET_DIR / f"{asset_name}_numeric_grounding_overlay.jpg"
        _write_source_faithfulness_overlay(report, image_path)
        legend = ["红色=疑似未接地数字", "黄色=近似匹配/OCR 疑似", "绿色=未触发问题数字样本"]
    elif dim == "information_density_and_synthesis":
        image_path = ASSET_DIR / f"{asset_name}_density_overlay.jpg"
        _write_density_overlay(report.judge_input, image_path)
        legend = ["绿色=算法认为有内容", "红色=算法认为为空白", "红框=最大空白区域"]
    elif dim == "visual_evidence_use":
        image_path = ASSET_DIR / f"{asset_name}_visual_evidence_overlay.jpg"
        _write_visual_overlay(report, image_path)
        legend = ["青色=图表/表格证据区域", "橙色=视觉内容区域"]
    elif dim == "basic_layout_integrity":
        image_path = ASSET_DIR / f"{asset_name}_basic_layout_overlay.jpg"
        _write_basic_layout_overlay(report.judge_input, image_path)
        legend = ["蓝色=检测到的面板/框架", "橙色=视觉内容区域", "红色=越界/压边样本", "紫色=重叠样本"]
    elif dim == "layout_readability":
        image_path = ASSET_DIR / f"{asset_name}_layout_overlay.jpg"
        _write_density_overlay(report.judge_input, image_path)
        legend = ["绿色=内容分布", "红色=空白区域", "红框=最大空白区域"]
    else:
        _write_thumbnail(report.judge_input, image_path)

    return {
        "dim": dim,
        "label": DIM_LABELS[dim],
        "owner": DIM_OWNERS[dim],
        "score": dim_score.get("score_0_10"),
        "source": dim_score.get("source"),
        "candidate": report.candidate,
        "image": _rel_asset(image_path),
        "legend": legend,
        "signals": _signals_for(dim, report),
        "problem": _problem_summary(dim, report),
        "evidence": _chinese_evidence(dim, report)[:4],
    }


def _signals_for(dim: str, report: CandidateReport) -> list[tuple[str, Any]]:
    bundles = report.deterministic.get("metric_bundles") or {}
    metrics = report.dims.get(dim, {}).get("metrics") or {}
    if dim == "source_faithfulness":
        numeric = bundles.get("numeric_token_exact_match") or metrics
        return [
            ("显著数字数量", numeric.get("salient_token_count")),
            ("论文中可接地数字", numeric.get("salient_grounded")),
            ("近似匹配数字", numeric.get("salient_near_miss")),
            ("疑似伪造数字", numeric.get("salient_fabricated")),
            ("数值精确匹配率", _pct(numeric.get("exact_match_ratio"))),
        ]
    if dim == "paper_coverage":
        raw = _load_vlm(report, dim)
        defects = raw.get("defects_found") if isinstance(raw.get("defects_found"), list) else []
        serious = sum(1 for item in defects if isinstance(item, dict) and item.get("severity") == "serious")
        return [
            ("VLM 发现的问题数", len(defects)),
            ("严重问题数", serious),
            ("评审置信度", raw.get("judge_confidence")),
        ]
    if dim == "information_density_and_synthesis":
        return [
            ("内容覆盖率", _pct(metrics.get("content_coverage"))),
            ("OCR 文字覆盖率", _pct(metrics.get("text_coverage_ratio"))),
            ("有效空白比例", _pct(metrics.get("effective_void_ratio"))),
            ("空白惩罚系数", metrics.get("void_penalty")),
        ]
    if dim == "visual_evidence_use":
        visual = bundles.get("visual_evidence") or {}
        raw = _load_vlm(report, dim)
        defects = raw.get("defects_found") if isinstance(raw.get("defects_found"), list) else []
        serious = sum(1 for item in defects if isinstance(item, dict) and item.get("severity") == "serious")
        return [
            ("图像算法检测图表数", visual.get("figure_region_count")),
            ("图表区域占比", _pct(visual.get("figure_area_ratio"))),
            ("截图墙风险", visual.get("possible_screenshot_wall")),
            ("VLM 严重问题数", serious),
        ]
    if dim == "basic_layout_integrity":
        basic = bundles.get("basic_layout_integrity") or metrics
        panel = basic.get("panel_overflow") or {}
        overlap = basic.get("overlap") or {}
        return [
            ("检测到的 panel 数", basic.get("panel_count")),
            ("文字越界数量", panel.get("text_overflow_count")),
            ("视觉元素越界数量", panel.get("visual_overflow_count")),
            ("重叠数量", overlap.get("overlap_count")),
            ("累计扣分", basic.get("penalty_total")),
        ]
    if dim == "layout_readability":
        return [
            ("内容覆盖率", _pct(metrics.get("content_coverage"))),
            ("有效空白比例", _pct(metrics.get("effective_void_ratio"))),
            ("空白后版面系数", metrics.get("void_factor")),
            ("最终分数来源", _source_cn(report.dims.get(dim, {}).get("source"))),
        ]
    if dim == "professional_aesthetics":
        raw = _load_vlm(report, dim)
        defects = raw.get("defects_found") if isinstance(raw.get("defects_found"), list) else []
        serious = sum(1 for item in defects if isinstance(item, dict) and item.get("severity") == "serious")
        return [
            ("审美问题数", len(defects)),
            ("严重审美问题数", serious),
            ("评审置信度", raw.get("judge_confidence")),
            ("最终分数来源", _source_cn(report.dims.get(dim, {}).get("source"))),
        ]
    return []


def _load_vlm(report: CandidateReport, dim: str) -> dict[str, Any]:
    path = report.path.parent / "vlm" / f"{dim}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _problem_summary(dim: str, report: CandidateReport) -> str:
    score = _score(report, dim)
    bundles = report.deterministic.get("metric_bundles") or {}
    metrics = report.dims.get(dim, {}).get("metrics") or {}
    if dim == "source_faithfulness":
        numeric = bundles.get("numeric_token_exact_match") or metrics
        return (
            f"这个样本源文忠实性得分较低（{_fmt_score(score)}）。Python 数字接地先检查显著数字是否能在论文中找到；"
            f"红框标出 {numeric.get('salient_fabricated')} 个疑似未接地数字，黄框标出 "
            f"{numeric.get('salient_near_miss')} 个近似匹配/OCR 疑似数字。VLM 只作为语义一致性补充判断。"
        )
    if dim == "paper_coverage":
        raw = _load_vlm(report, dim)
        defects = raw.get("defects_found") if isinstance(raw.get("defects_found"), list) else []
        serious = sum(1 for item in defects if isinstance(item, dict) and item.get("severity") == "serious")
        return (
            f"这个样本论文覆盖得分为 {_fmt_score(score)}。VLM 评审认为它没有把论文主线综合出来，"
            f"而是把长段论文截图/摘录放进海报；同时有 {serious} 个严重覆盖问题，包括核心模型/结果缺失和局部内容被裁切。"
        )
    if dim == "information_density_and_synthesis":
        return (
            f"这个样本信息密度得分为 {_fmt_score(score)}。红色网格显示大面积区域被算法判为空白，"
            f"有效空白比例达到 {_pct(metrics.get('effective_void_ratio'))}，空白惩罚把最终密度分显著压低。"
        )
    if dim == "visual_evidence_use":
        visual = bundles.get("visual_evidence") or {}
        raw = _load_vlm(report, dim)
        defects = raw.get("defects_found") if isinstance(raw.get("defects_found"), list) else []
        serious = sum(1 for item in defects if isinstance(item, dict) and item.get("severity") == "serious")
        return (
            f"这个样本视觉证据得分为 {_fmt_score(score)}。图像算法只检测到 {visual.get('figure_region_count')} 个图表/表格区域，"
            f"VLM 评审又确认有 {serious} 个严重视觉证据问题：海报主要由文字框组成，缺少能支撑结论的图表、曲线、结果图或解释性结果标注。"
        )
    if dim == "basic_layout_integrity":
        basic = bundles.get("basic_layout_integrity") or metrics
        panel = basic.get("panel_overflow") or {}
        return (
            f"这个样本基础布局完整性得分为 {_fmt_score(score)}。蓝框是检测到的面板/框架，"
            f"红框是算法抽出的越界或压边样本；当前文字越界 {panel.get('text_overflow_count')} 处，"
            f"累计扣分 {basic.get('penalty_total')}。"
        )
    if dim == "layout_readability":
        return (
            f"这个样本版面可读性得分为 {_fmt_score(score)}。Python 先看到内容分布与大空白严重失衡，"
            f"有效空白比例为 {_pct(metrics.get('effective_void_ratio'))}；VLM 评审进一步确认右侧裁切、截图式小字和阅读路径混乱。"
        )
    if dim == "professional_aesthetics":
        raw = _load_vlm(report, dim)
        defects = raw.get("defects_found") if isinstance(raw.get("defects_found"), list) else []
        serious = sum(1 for item in defects if isinstance(item, dict) and item.get("severity") == "serious")
        return (
            f"这个样本专业审美得分为 {_fmt_score(score)}。VLM 评审发现 {serious} 个严重视觉完成度问题："
            "下半页大面积未完成空白、视觉证据集中成纸面截图块、整体更像模板填空而不是成熟会议海报。"
        )
    return "该维度用当前 benchmark 输出中的真实样本展示检测信号和最终扣分原因。"


def _chinese_evidence(dim: str, report: CandidateReport) -> list[str]:
    bundles = report.deterministic.get("metric_bundles") or {}
    metrics = report.dims.get(dim, {}).get("metrics") or {}
    if dim == "source_faithfulness":
        numeric = bundles.get("numeric_token_exact_match") or metrics
        return [
            f"Python 检测显著数字 {numeric.get('salient_token_count')} 个，其中 {numeric.get('salient_grounded')} 个能在论文中接地。",
            f"近似匹配数字 {numeric.get('salient_near_miss')} 个，疑似伪造数字 {numeric.get('salient_fabricated')} 个。",
            "VLM 额外检查可见表格、标题元数据和图像引用是否与论文摘要一致。",
        ]
    if dim == "paper_coverage":
        return [
            "VLM 检查海报是否覆盖问题动机、核心方法、关键结果和结论，而不是只看是否有关键词。",
            "该例主要问题是主线缺失，多个区域像论文截图/摘录，没有转化成海报层面的总结。",
            "裁切或不可读区域会直接降低覆盖判断，因为这些内容无法被读者获得。",
        ]
    if dim == "information_density_and_synthesis":
        return [
            f"内容覆盖率只有 {_pct(metrics.get('content_coverage'))}，说明有效信息区域不足。",
            f"最大/综合空白形成 {_pct(metrics.get('effective_void_ratio'))} 的 effective void。",
            "红色空白网格越连续，说明 poster 越像未完成页面，而不是信息充分的学术海报。",
        ]
    if dim == "visual_evidence_use":
        visual = bundles.get("visual_evidence") or {}
        return [
            f"图像算法先定位图表/表格区域，当前只找到 {visual.get('figure_region_count')} 个，面积占比 {_pct(visual.get('figure_area_ratio'))}。",
            "VLM 再判断这些区域是否真正支持科学叙事：有没有结果标注、图注、与附近论点的关系。",
            "只堆文字框、空框或少量孤立表格，会被判为视觉证据使用弱。",
        ]
    if dim == "basic_layout_integrity":
        basic = bundles.get("basic_layout_integrity") or metrics
        overlap = basic.get("overlap") or {}
        return [
            "蓝框表示算法检测到的面板/框架，红框表示内容越过面板安全边界或贴近画布边缘。",
            f"当前检测到重叠数量 {overlap.get('overlap_count')}，基础布局 findings 数 {basic.get('findings_count')}。",
            "这个维度只抓机械损坏，不因为审美一般或内容少而扣分。",
        ]
    if dim == "layout_readability":
        return [
            "Python 先用内容覆盖和 void 判断版面是否 top-heavy、bottom-empty 或局部失衡。",
            "VLM 再看真实阅读体验：是否有裁切、过小截图文字、层级扁平或阅读路径混乱。",
            "这个维度比 basic layout 更关注读者能不能顺畅读完。",
        ]
    if dim == "professional_aesthetics":
        return [
            "VLM 从字体系统、构图节奏、配色、图表整合和会议海报成品感五个角度审查。",
            "该例大块未完成空白触发强扣分；纸面截图式 figure 也降低专业完成度。",
            "这个维度不重新评论文覆盖或源文忠实性，只评最终视觉完成度。",
        ]
    return []


def _write_thumbnail(src: Path, out: Path, *, max_w: int = 1500) -> None:
    with Image.open(src) as im:
        im = im.convert("RGB")
        im.thumbnail((max_w, 2200), Image.Resampling.LANCZOS)
        im.save(out, quality=94)


def _write_source_faithfulness_overlay(report: CandidateReport, out: Path) -> None:
    src = report.artifact if report.artifact.exists() else report.judge_input
    with Image.open(src) as im:
        base = im.convert("RGB")
    ocr = run_ocr(src, include_segments=True)
    segments = ocr.get("segments") or []
    numeric = (report.deterministic.get("metric_bundles") or {}).get("numeric_token_exact_match") or {}
    fabricated = {str(item).strip() for item in numeric.get("fabricated_examples") or numeric.get("missing_examples") or []}
    near_miss = {str(item).strip() for item in numeric.get("near_miss_examples") or []}

    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    red_count = amber_count = green_count = 0

    for seg in segments:
        text = str(seg.get("text") or "")
        if not any(ch.isdigit() for ch in text):
            continue
        rect = _segment_rect_from_box(seg.get("box") or [])
        if not rect:
            continue
        red_hits = [item for item in fabricated if item and item in text]
        amber_hits = [item for item in near_miss if item and item in text]
        if red_hits and red_count < 14:
            box = _draw_rect(draw, rect, 1, 1, (220, 38, 38, 255), width=_line_w(base, 8), fill=(220, 38, 38, 62))
            if box and red_count < 5:
                _draw_label(draw, box, f"未接地数字: {red_hits[0]}", (185, 28, 28, 255), base)
            red_count += 1
        elif amber_hits and amber_count < 8:
            box = _draw_rect(draw, rect, 1, 1, (245, 158, 11, 255), width=_line_w(base, 7), fill=(245, 158, 11, 50))
            if box and amber_count < 3:
                _draw_label(draw, box, f"近似匹配: {amber_hits[0]}", (180, 83, 9, 255), base)
            amber_count += 1
        elif green_count < 8:
            box = _draw_rect(draw, rect, 1, 1, (22, 163, 74, 235), width=_line_w(base, 4), fill=(22, 163, 74, 26))
            if box and green_count < 2:
                _draw_label(draw, box, "未触发问题数字", (21, 128, 61, 255), base)
            green_count += 1

    _draw_label(
        draw,
        [16, 16, min(base.width - 16, 720), 78],
        "源文忠实性: OCR 数字接地  红=未接地  黄=近似匹配  绿=未触发问题",
        (17, 24, 39, 255),
        base,
    )
    result = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")
    _save_fit(result, out)


def _write_density_overlay(src: Path, out: Path) -> None:
    with Image.open(src) as im:
        base = im.convert("RGB")
    segments = (run_ocr(src, include_segments=True).get("segments") or [])
    occupancy = content_occupancy(base, segments=segments)
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    cols, rows = int(occupancy.get("cols") or 0), int(occupancy.get("rows") or 0)
    occ = occupancy.get("occ") or []
    if cols and rows:
        cw, ch = base.width / cols, base.height / rows
        for y, row in enumerate(occ):
            for x, used in enumerate(row):
                color = (34, 197, 94, 70) if used else (239, 68, 68, 82)
                draw.rectangle([x * cw, y * ch, (x + 1) * cw, (y + 1) * ch], fill=color)
    rect = occupancy.get("largest_empty_rect_px") or {}
    if rect:
        box = [rect["x0"], rect["y0"], rect["x1"], rect["y1"]]
        draw.rectangle(box, fill=(220, 38, 38, 45), outline=(220, 38, 38, 255), width=_line_w(base, 7))
        _draw_label(draw, box, "最大空白区域", (220, 38, 38, 255), base)
    _draw_label(draw, [16, 16, 360, 72], "绿色=有内容  红色=空白", (17, 94, 89, 255), base)
    result = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")
    _save_fit(result, out)


def _write_visual_overlay(report: CandidateReport, out: Path) -> None:
    src = report.artifact if report.artifact.exists() else report.judge_input
    with Image.open(src) as im:
        base = im.convert("RGB")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    bundles = report.deterministic.get("metric_bundles") or {}
    file_info = bundles.get("file_integrity") or {}
    sw, sh = float(file_info.get("width") or base.width), float(file_info.get("height") or base.height)
    sx, sy = base.width / sw, base.height / sh
    visual = bundles.get("basic_layout_integrity") or {}
    for i, rect in enumerate(visual.get("visual_region_rects") or []):
        box = _draw_rect(draw, rect, sx, sy, (249, 115, 22, 230), width=_line_w(base, 5), fill=(249, 115, 22, 32))
        if i < 2 and box:
            _draw_label(draw, box, "视觉内容区域", (194, 65, 12, 255), base)
    ve = bundles.get("visual_evidence") or {}
    for i, rect in enumerate(ve.get("figure_rects") or []):
        box = _draw_rect(draw, rect, sx, sy, (6, 182, 212, 255), width=_line_w(base, 8), fill=(6, 182, 212, 38))
        if i < 3 and box:
            _draw_label(draw, box, "图表/表格证据", (8, 145, 178, 255), base)
    result = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")
    _save_fit(result, out)


def _write_basic_layout_overlay(src: Path, out: Path) -> None:
    with Image.open(src) as im:
        base = im.convert("RGB")
    segments = (run_ocr(src, include_segments=True).get("segments") or [])
    occupancy = content_occupancy(base, segments=segments)
    report = basic_layout_integrity(base, segments=segments, occupancy=occupancy, include_debug_regions=True)
    debug = report.get("debug_regions") or {}
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    for i, panel in enumerate(debug.get("panels") or []):
        box = _draw_rect(draw, panel.get("rect") or {}, 1, 1, (37, 99, 235, 225), width=_line_w(base, 5), fill=(37, 99, 235, 16))
        if i < 3 and box:
            _draw_label(draw, box, "检测到的 panel", (30, 64, 175, 255), base)
    for i, visual in enumerate(debug.get("visuals") or []):
        box = _draw_rect(draw, visual.get("rect") or {}, 1, 1, (249, 115, 22, 220), width=_line_w(base, 4), fill=(249, 115, 22, 28))
        if i < 2 and box:
            _draw_label(draw, box, "视觉内容", (194, 65, 12, 255), base)
    for sample in (debug.get("canvas_overflow_samples") or []) + (debug.get("panel_overflow_samples") or []):
        box = _draw_rect(draw, sample.get("rect") or {}, 1, 1, (220, 38, 38, 255), width=_line_w(base, 8), fill=(220, 38, 38, 52))
        if box:
            _draw_label(draw, box, "越界/压边", (185, 28, 28, 255), base)
    for sample in debug.get("overlap_samples") or []:
        box = _draw_rect(draw, sample.get("rect") or {}, 1, 1, (147, 51, 234, 255), width=_line_w(base, 8), fill=(147, 51, 234, 55))
        if box:
            _draw_label(draw, box, "重叠", (126, 34, 206, 255), base)
    result = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")
    _save_fit(result, out)


def _build_hard_gate_examples() -> list[dict[str, Any]]:
    missing_artifact = ASSET_DIR / "hard_gate_missing_artifact.jpg"
    missing_local_image = ASSET_DIR / "hard_gate_missing_local_image.jpg"
    _write_hard_gate_visual(
        missing_artifact,
        title="P0: 候选海报文件不存在",
        subtitle="artifact missing / 无法生成可评分预览",
        trigger="渲染完整性失败",
        pre_score=86.2,
        final_score=GATE_CEILING_SCORE,
        variant="missing",
    )
    _write_hard_gate_visual(
        missing_local_image,
        title="P0: HTML 引用了缺失图片",
        subtitle="local image missing / 关键图像无法加载",
        trigger="关键视觉证据缺失",
        pre_score=78.4,
        final_score=GATE_CEILING_SCORE,
        variant="missing_image",
    )
    return [
        {
            "title": "文件不存在或空文件",
            "trigger": "候选 artifact 不存在、文件为空，或完全无法渲染 preview。",
            "why": "即使其它维度假设能给高分，最终读者拿不到有效海报，所以 overall 会被 Hard Gate ceiling 压到低分。",
            "pre_score": 86.2,
            "final_score": GATE_CEILING_SCORE,
            "image": _rel_asset(missing_artifact),
        },
        {
            "title": "关键本地图片缺失",
            "trigger": "HTML poster 引用的本地图像不存在，导致图表/结果区域变成破图或空框。",
            "why": "这类问题不是普通审美瑕疵，而是最终成品不可完整阅读；因此触发 P0 后 final overall 最高只能到 gate ceiling。",
            "pre_score": 78.4,
            "final_score": GATE_CEILING_SCORE,
            "image": _rel_asset(missing_local_image),
        },
    ]


def _write_hard_gate_visual(
    out: Path,
    *,
    title: str,
    subtitle: str,
    trigger: str,
    pre_score: float,
    final_score: float,
    variant: str,
) -> None:
    canvas = Image.new("RGB", (1800, 1120), (248, 250, 252))
    draw = ImageDraw.Draw(canvas)
    title_font = _font(54)
    sub_font = _font(30)
    label_font = _font(28)
    small_font = _font(24)
    score_font = _font(72)
    red = (185, 28, 28)
    slate = (51, 65, 85)
    muted = (100, 116, 139)

    draw.text((72, 54), title, fill=(15, 23, 42), font=title_font)
    draw.text((74, 126), subtitle, fill=muted, font=sub_font)

    poster = [74, 198, 1218, 1038]
    draw.rounded_rectangle(poster, radius=20, fill=(255, 255, 255), outline=(203, 213, 225), width=3)
    draw.rounded_rectangle([poster[0] + 24, poster[1] + 24, poster[2] - 24, poster[1] + 132], radius=14, fill=(241, 245, 249))
    draw.text((poster[0] + 52, poster[1] + 54), "Benchmark Candidate Poster", fill=slate, font=sub_font)

    if variant == "missing":
        _draw_dashed_rect(draw, [poster[0] + 62, poster[1] + 192, poster[2] - 62, poster[3] - 64], red, width=8, dash=28)
        cx, cy = (poster[0] + poster[2]) // 2, (poster[1] + poster[3]) // 2 + 18
        draw.polygon([(cx, cy - 132), (cx - 150, cy + 132), (cx + 150, cy + 132)], fill=(254, 226, 226), outline=red)
        draw.text((cx - 22, cy - 60), "!", fill=red, font=_font(118))
        lines = ["无法读取候选文件", "没有有效预览图", "不能进入正常 7 维加权评分"]
        for i, line in enumerate(lines):
            bbox = draw.textbbox((0, 0), line, font=label_font)
            draw.text((cx - (bbox[2] - bbox[0]) / 2, cy + 172 + i * 48), line, fill=red if i == 0 else slate, font=label_font)
    else:
        x0, y0, x1, y1 = poster[0] + 54, poster[1] + 178, poster[2] - 54, poster[3] - 54
        panel_w = (x1 - x0 - 36) / 2
        panels = [
            [x0, y0, x0 + panel_w, y0 + 260],
            [x0 + panel_w + 36, y0, x1, y0 + 430],
            [x0, y0 + 294, x0 + panel_w, y1],
            [x0 + panel_w + 36, y0 + 464, x1, y1],
        ]
        for idx, box in enumerate(panels):
            draw.rounded_rectangle(box, radius=14, fill=(248, 250, 252), outline=(203, 213, 225), width=3)
            draw.text((box[0] + 24, box[1] + 22), f"Section {idx + 1}", fill=slate, font=small_font)
        broken = [panels[1][0] + 28, panels[1][1] + 86, panels[1][2] - 28, panels[1][3] - 34]
        draw.rectangle(broken, fill=(255, 255, 255))
        _draw_dashed_rect(draw, broken, red, width=8, dash=24)
        draw.line([broken[0] + 26, broken[1] + 26, broken[2] - 26, broken[3] - 26], fill=red, width=10)
        draw.line([broken[0] + 26, broken[3] - 26, broken[2] - 26, broken[1] + 26], fill=red, width=10)
        draw.text((broken[0] + 48, broken[1] + 54), "关键图片缺失", fill=red, font=label_font)
        draw.text((broken[0] + 48, broken[1] + 106), "图表/结果区域无法加载", fill=slate, font=small_font)

    score_box = [1278, 208, 1728, 1038]
    draw.rounded_rectangle(score_box, radius=22, fill=(255, 255, 255), outline=(226, 232, 240), width=3)
    draw.text((score_box[0] + 42, score_box[1] + 48), "Hard Gate", fill=(15, 23, 42), font=title_font)
    draw.text((score_box[0] + 44, score_box[1] + 122), trigger, fill=red, font=label_font)
    draw.text((score_box[0] + 44, score_box[1] + 206), "原始加权分", fill=muted, font=small_font)
    draw.text((score_box[0] + 44, score_box[1] + 246), f"{pre_score:.1f}", fill=slate, font=score_font)
    draw.line([score_box[0] + 48, score_box[1] + 362, score_box[2] - 48, score_box[1] + 362], fill=(203, 213, 225), width=4)
    draw.text((score_box[0] + 44, score_box[1] + 408), f"P0 ceiling = {final_score:.0f}", fill=red, font=label_font)
    draw.text((score_box[0] + 44, score_box[1] + 488), "最终 overall", fill=muted, font=small_font)
    draw.rounded_rectangle([score_box[0] + 42, score_box[1] + 530, score_box[2] - 42, score_box[1] + 654], radius=18, fill=red)
    draw.text((score_box[0] + 142, score_box[1] + 548), f"{final_score:.1f}", fill=(255, 255, 255), font=score_font)
    notes = ["P0 不参与 100% 权重加权", "而是直接限制最高总分", "用于挡住不可交付成品"]
    for i, line in enumerate(notes):
        draw.text((score_box[0] + 44, score_box[1] + 680 + i * 48), line, fill=slate, font=small_font)
    canvas.save(out, quality=96)


def _draw_dashed_rect(
    draw: ImageDraw.ImageDraw,
    box: list[float],
    color: tuple[int, int, int],
    *,
    width: int = 4,
    dash: int = 18,
) -> None:
    x0, y0, x1, y1 = [int(v) for v in box]
    gap = dash
    for x in range(x0, x1, dash + gap):
        draw.line([(x, y0), (min(x + dash, x1), y0)], fill=color, width=width)
        draw.line([(x, y1), (min(x + dash, x1), y1)], fill=color, width=width)
    for y in range(y0, y1, dash + gap):
        draw.line([(x0, y), (x0, min(y + dash, y1))], fill=color, width=width)
        draw.line([(x1, y), (x1, min(y + dash, y1))], fill=color, width=width)


def _draw_rect(
    draw: ImageDraw.ImageDraw,
    rect: dict[str, Any],
    sx: float,
    sy: float,
    color: tuple[int, int, int, int],
    *,
    width: int,
    fill: tuple[int, int, int, int] | None = None,
) -> list[float] | None:
    try:
        box = [float(rect["x0"]) * sx, float(rect["y0"]) * sy, float(rect["x1"]) * sx, float(rect["y1"]) * sy]
    except (KeyError, TypeError, ValueError):
        return None
    if fill:
        draw.rectangle(box, fill=fill)
    draw.rectangle(box, outline=color, width=width)
    return box


def _draw_label(
    draw: ImageDraw.ImageDraw,
    box: list[float],
    text: str,
    color: tuple[int, int, int, int],
    base: Image.Image,
) -> None:
    font = _font(max(18, int(base.width / 70)))
    x0, y0, x1, _y1 = box
    probe = draw.textbbox((0, 0), text, font=font)
    pad = max(5, int(base.width / 360))
    text_w = probe[2] - probe[0]
    text_h = probe[3] - probe[1]
    tx = max(6, min(x0 + 8, base.width - text_w - (2 * pad) - 6))
    ty = max(6, min(y0 + 8, base.height - text_h - (2 * pad) - 6))
    bbox = draw.textbbox((tx, ty), text, font=font)
    bg = [bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad]
    draw.rounded_rectangle(bg, radius=6, fill=(255, 255, 255, 235), outline=color, width=2)
    draw.text((tx, ty), text, fill=color, font=font)


def _segment_rect_from_box(box: list[Any]) -> dict[str, float] | None:
    if not box:
        return None
    try:
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
    except (TypeError, ValueError, IndexError):
        return None
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if x1 <= x0 or y1 <= y0:
        return None
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "w": x1 - x0, "h": y1 - y0}


def _font(size: int) -> ImageFont.ImageFont:
    for path in (
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    ):
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _line_w(base: Image.Image, nominal: int) -> int:
    return max(nominal, int(min(base.size) / 180))


def _save_fit(image: Image.Image, out: Path, *, max_w: int = 1500) -> None:
    image = image.copy()
    image.thumbnail((max_w, 2200), Image.Resampling.LANCZOS)
    image.save(out, quality=94)


def _render_examples(examples: list[dict[str, Any]]) -> str:
    return "\n".join(_render_card(example) for example in examples)


def _render_hard_gate_section(reports: list[CandidateReport]) -> str:
    examples = _build_hard_gate_examples()
    gate_count = sum(1 for report in reports if report.final.get("gate_triggered"))
    gate_note = (
        f"当前已汇总 benchmark 报告里真实触发 Hard Gate 的样本数：{gate_count}。"
        "下面两个是合成可视化样例，用来说明 P0 触发后为什么会出现很低的 overall 分。"
    )
    cards = "\n".join(_render_hard_gate_card(example) for example in examples)
    return f"""
<section id="hard-gate-examples" class="gate-section">
  <h2>Hard Gate 触发示例</h2>
  <p class="gate-intro">Hard Gate 不属于 7 个加权 metrics。它只检查候选海报是否是可交付、可渲染、可阅读的最终成品；一旦出现 P0，系统会先算出正常加权分，再把 final overall 限制在 {GATE_CEILING_SCORE:.0f} 分以内。{html.escape(gate_note)}</p>
  <div class="gate-grid">
    {cards}
  </div>
</section>
"""


def _render_hard_gate_card(example: dict[str, Any]) -> str:
    return f"""
    <article class="gate-card">
      <div class="gate-media"><img src="{html.escape(example['image'])}" alt="{html.escape(example['title'])} hard gate 可视化"></div>
      <h3>{html.escape(example['title'])}</h3>
      <div class="gate-scoreline">
        <span class="gate-pill">P0 Hard Gate</span>
        <span>原始加权分 {example['pre_score']:.1f}</span>
        <span class="gate-arrow">→</span>
        <span class="gate-final">{example['final_score']:.1f}</span>
      </div>
      <table class="signal-table"><tbody>
        <tr><th>触发条件</th><td>{html.escape(example['trigger'])}</td></tr>
        <tr><th>扣分方式</th><td>不按权重扣几分，而是直接限制最高 overall。</td></tr>
      </tbody></table>
      <div class="gate-note">{html.escape(example['why'])}</div>
    </article>
"""


def _render_card(example: dict[str, Any]) -> str:
    signal_rows = "".join(
        f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(_fmt(v))}</td></tr>"
        for k, v in example["signals"]
    )
    evidence = "".join(f"<li>{html.escape(str(item))}</li>" for item in example["evidence"])
    legend = "".join(f"<span>{html.escape(item)}</span>" for item in example["legend"])
    return f"""
<div class="example-card">
  <div>
    <div class="example-media"><img src="{html.escape(example['image'])}" alt="{html.escape(example['label'])}可视化例子"></div>
    <div class="legend">{legend}</div>
  </div>
  <div>
    <h3>{html.escape(example['label'])} <code>{html.escape(example['dim'])}</code></h3>
    <div class="example-score">
      <span class="score-badge">{html.escape(_fmt_score(example['score']))}</span>
      <span class="owner-badge">{html.escape(example['owner'])}</span>
      <span class="tag">{html.escape(_source_cn(example.get('source')))}</span>
    </div>
    <p class="candidate">样本：{html.escape(example['candidate'])}</p>
    <table class="signal-table"><tbody>{signal_rows}</tbody></table>
    <div class="problem-box"><b>检测到的主要问题：</b>{html.escape(example['problem'])}</div>
    <ul class="evidence-list">{evidence}</ul>
  </div>
</div>
"""


def _inject_style(doc: str, style: str) -> str:
    starts = [idx for marker in (".example-section{", ".dimension-example{") if (idx := doc.find(marker)) >= 0]
    start = min(starts) if starts else -1
    if start >= 0:
        end = doc.find("</style>", start)
        if end > start:
            return doc[:start] + style + "\n" + doc[end:]
        return doc
    return doc.replace("</style>", style + "\n</style>")


def _remove_examples_toc(doc: str) -> str:
    lines = doc.splitlines()
    lines = [line for line in lines if 'href="#examples"' not in line and 'href="#hard-gate-examples"' not in line]
    return "\n".join(lines) + ("\n" if doc.endswith("\n") else "")


def _remove_section(doc: str, section_id: str) -> str:
    start = doc.find(f'<section id="{section_id}"')
    if start >= 0:
        next_start = doc.find("\n<section", start + 1)
        if next_start >= 0:
            return doc[:start] + doc[next_start + 1:]
    return doc


def _remove_inline_examples(doc: str) -> str:
    start_marker = '<div class="dimension-example"'
    end_marker = "</div><!-- /dimension-example -->"
    while True:
        start = doc.find(start_marker)
        if start < 0:
            return doc
        end = doc.find(end_marker, start)
        if end < 0:
            return doc[:start]
        doc = doc[:start] + doc[end + len(end_marker):]


def _sync_methodology_weights(doc: str) -> str:
    for dim, label in DIM_LABELS.items():
        weight = DIM_WEIGHTS.get(dim)
        if weight is None:
            continue
        weight_text = _fmt_weight(weight)
        table_pattern = re.compile(
            rf'(<tr class="[^"]+"><td><b>{re.escape(label)}</b></td><td><code>{re.escape(dim)}</code></td><td class="num">)'
            r"[^<]+"
            r"(</td>)"
        )
        doc = table_pattern.sub(rf"\g<1>{weight_text}\g<2>", doc)
        heading_pattern = re.compile(rf"(<h3>\d+\. {re.escape(label)}（)[^，]+(，[^<]+</h3>)")
        doc = heading_pattern.sub(rf"\g<1>{weight_text}%\g<2>", doc)
    return doc


def _inject_hard_gate_toc(doc: str) -> str:
    target = '    <li><a href="#dimensions">7 个 Metrics 维度</a></li>'
    insert = '    <li><a href="#hard-gate-examples">Hard Gate 触发示例</a></li>\n' + target
    if target not in doc:
        return doc
    return doc.replace(target, insert, 1)


def _inject_hard_gate_section(doc: str, reports: list[CandidateReport]) -> str:
    marker = '<section id="dimensions">'
    idx = doc.find(marker)
    if idx < 0:
        raise SystemExit("could not find dimensions section for hard gate insertion")
    section = _render_hard_gate_section(reports)
    return doc[:idx] + section + "\n" + doc[idx:]


def _inject_inline_examples(doc: str, examples: dict[str, dict[str, Any]]) -> str:
    for dim in DIM_LABELS:
        example = examples.get(dim)
        if not example:
            continue
        anchor = DETAIL_ANCHORS[dim]
        h3_idx = doc.find(anchor)
        if h3_idx < 0:
            raise SystemExit(f"could not find methodology subsection: {anchor}")
        article_end = doc.find("</article>", h3_idx)
        if article_end < 0:
            raise SystemExit(f"could not find closing article for: {anchor}")
        inline = _render_inline_example(example)
        doc = doc[:article_end] + inline + "\n" + doc[article_end:]
    return doc


def _render_inline_example(example: dict[str, Any]) -> str:
    return f"""
    <div class="dimension-example" data-dim="{html.escape(example['dim'])}">
      <h4>这个维度的真实可视化例子</h4>
      {_render_card(example)}
    </div><!-- /dimension-example -->
"""


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)[:120].strip("_") or "example"


def _rel_asset(path: Path) -> str:
    return str(path.relative_to(REPORT_DIR))


def _list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "N/A"


def _fmt_score(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "N/A"


def _fmt_weight(value: Any) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)
    if numeric.is_integer():
        return str(int(numeric))
    return f"{numeric:.1f}".rstrip("0").rstrip(".")


def _source_cn(value: Any) -> str:
    mapping = {
        "tools": "Python 规则",
        "judge": "VLM 判断",
        "agent": "VLM 判断",
        "vlm_judge": "VLM 判断",
        "blend": "Python + VLM 混合",
        "placeholder": "未评分",
    }
    return mapping.get(str(value or ""), str(value or "未记录"))


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:.3g}"
    if value is None:
        return "无"
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
