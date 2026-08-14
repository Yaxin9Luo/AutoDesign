#!/usr/bin/env python3
"""Plot per-case metric score distributions from the benchmark scores CSV.

The script intentionally avoids matplotlib so it can run in the repo's current
evaluation environment. It writes high-resolution SVG histograms plus a Chinese
HTML summary report.
"""

from __future__ import annotations

import argparse
import csv
import html as H
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np


REPO = Path(__file__).resolve().parent.parent
DEFAULT_SCORES = REPO / "out/eval/report/poster_benchmark_main_table/scores.csv"
DEFAULT_OUT = REPO / "out/eval/report/poster_benchmark_main_table/metric_distributions"

METRICS = [
    "source_faithfulness",
    "paper_coverage",
    "information_density_and_synthesis",
    "visual_evidence_use",
    "basic_layout_integrity",
    "layout_readability",
    "professional_aesthetics",
]

METRIC_LABELS_ZH = {
    "source_faithfulness": "源文忠实性",
    "paper_coverage": "论文覆盖度",
    "information_density_and_synthesis": "信息密度与综合",
    "visual_evidence_use": "视觉证据使用",
    "basic_layout_integrity": "基础布局完整性",
    "layout_readability": "布局可读性",
    "professional_aesthetics": "专业美学",
}


@dataclass(frozen=True)
class MetricSummary:
    metric: str
    label: str
    n: int
    mean: float
    std: float
    median: float
    min_score: float
    max_score: float
    skewness: float
    excess_kurtosis: float
    jarque_bera: float
    jb_p_value: float
    normality_label: str
    shape_note: str
    svg_name: str


def _float_or_none(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except ValueError:
        return None
    if not math.isfinite(out):
        return None
    return out


def _read_metric_values(scores_csv: Path, metrics: list[str]) -> tuple[dict[str, list[float]], list[str], int]:
    values = {metric: [] for metric in metrics}
    systems: set[str] = set()
    row_count = 0
    with scores_csv.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        missing = [metric for metric in metrics if metric not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"Missing metric columns in {scores_csv}: {', '.join(missing)}")
        for row in reader:
            row_count += 1
            if row.get("system_label"):
                systems.add(row["system_label"])
            for metric in metrics:
                score = _float_or_none(row.get(metric))
                if score is not None:
                    values[metric].append(score)
    return values, sorted(systems), row_count


def _fmt(value: float | None, digits: int = 2) -> str:
    if value is None or not math.isfinite(value):
        return "NA"
    return f"{value:.{digits}f}"


def _normal_pdf(x: np.ndarray, mean: float, std: float) -> np.ndarray:
    if std <= 1e-12:
        return np.zeros_like(x)
    z = (x - mean) / std
    return np.exp(-0.5 * z * z) / (std * math.sqrt(2 * math.pi))


def _describe_shape(skewness: float, excess_kurtosis: float, jb_p_value: float) -> tuple[str, str]:
    if not all(math.isfinite(x) for x in [skewness, excess_kurtosis, jb_p_value]):
        return "无法判断", "有效分数太少或方差接近 0。"

    if jb_p_value >= 0.05:
        normality = "未拒绝正态"
    else:
        normality = "明显非正态"

    notes: list[str] = []
    if skewness <= -0.5:
        notes.append("左偏，高分段堆积更明显")
    elif skewness >= 0.5:
        notes.append("右偏，低分段堆积更明显")
    else:
        notes.append("偏度较小，整体相对对称")

    if excess_kurtosis >= 1.0:
        notes.append("尖峰/厚尾")
    elif excess_kurtosis <= -1.0:
        notes.append("扁平或多峰风险")
    else:
        notes.append("峰度接近正态范围")

    return normality, "；".join(notes) + "。"


def _summarize(metric: str, scores: list[float]) -> MetricSummary:
    arr = np.asarray(scores, dtype=float)
    label = METRIC_LABELS_ZH.get(metric, metric)
    if arr.size == 0:
        return MetricSummary(metric, label, 0, math.nan, math.nan, math.nan, math.nan, math.nan, math.nan, math.nan, math.nan, math.nan, "无数据", "没有可用分数。", f"{metric}.svg")

    mean = float(np.mean(arr))
    std = float(np.std(arr, ddof=0))
    median = float(np.median(arr))
    min_score = float(np.min(arr))
    max_score = float(np.max(arr))

    if arr.size >= 3 and std > 1e-12:
        z = (arr - mean) / std
        skewness = float(np.mean(z**3))
        excess_kurtosis = float(np.mean(z**4) - 3.0)
        jarque_bera = float(arr.size / 6.0 * (skewness**2 + 0.25 * excess_kurtosis**2))
        jb_p_value = float(math.exp(-0.5 * jarque_bera))
    else:
        skewness = math.nan
        excess_kurtosis = math.nan
        jarque_bera = math.nan
        jb_p_value = math.nan

    normality_label, shape_note = _describe_shape(skewness, excess_kurtosis, jb_p_value)
    return MetricSummary(
        metric=metric,
        label=label,
        n=int(arr.size),
        mean=mean,
        std=std,
        median=median,
        min_score=min_score,
        max_score=max_score,
        skewness=skewness,
        excess_kurtosis=excess_kurtosis,
        jarque_bera=jarque_bera,
        jb_p_value=jb_p_value,
        normality_label=normality_label,
        shape_note=shape_note,
        svg_name=f"{metric}.svg",
    )


def _score_to_x(score: float, left: float, plot_w: float) -> float:
    return left + (score / 10.0) * plot_w


def _svg_text(x: float, y: float, text: str, *, size: int = 16, anchor: str = "start", weight: int = 400, fill: str = "#223043") -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{weight}" '
        f'text-anchor="{anchor}" fill="{fill}">{H.escape(text)}</text>'
    )


def _write_metric_svg(metric: str, scores: list[float], summary: MetricSummary, out_path: Path, bins: int = 20) -> None:
    width = 1280
    height = 620
    left = 76
    right = 34
    top = 86
    bottom = 72
    plot_w = width - left - right
    plot_h = height - top - bottom

    arr = np.asarray(scores, dtype=float)
    edges = np.linspace(0.0, 10.0, bins + 1)
    counts, _ = np.histogram(arr, bins=edges)
    bin_width = float(edges[1] - edges[0])

    xs = np.linspace(0.0, 10.0, 260)
    expected = _normal_pdf(xs, summary.mean, summary.std) * summary.n * bin_width
    y_max = max(float(np.max(counts)) if counts.size else 0.0, float(np.max(expected)) if expected.size else 0.0, 1.0)
    y_max *= 1.15

    def y_pos(value: float) -> float:
        return top + plot_h - (value / y_max) * plot_h

    lines: list[str] = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="620" viewBox="0 0 1280 620">',
        '<rect width="1280" height="620" fill="#fbfcff"/>',
        _svg_text(36, 42, f"{summary.label} per-case 分数分布", size=28, weight=800),
        _svg_text(
            36,
            70,
            f"n={summary.n}  mean={_fmt(summary.mean)}  std={_fmt(summary.std)}  "
            f"skew={_fmt(summary.skewness)}  excess kurtosis={_fmt(summary.excess_kurtosis)}  "
            f"JB p={_fmt(summary.jb_p_value, 4)}",
            size=16,
            fill="#5e697a",
        ),
    ]

    # Horizontal grid.
    for i in range(6):
        val = y_max * i / 5.0
        y = y_pos(val)
        color = "#dfe5ef" if i == 0 else "#edf1f7"
        lines.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="{color}" stroke-width="1"/>')
        lines.append(_svg_text(left - 12, y + 5, f"{val:.0f}", size=13, anchor="end", fill="#687386"))

    # X-axis ticks.
    for tick in range(0, 11):
        x = _score_to_x(float(tick), left, plot_w)
        lines.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top+plot_h}" stroke="#f0f3f8" stroke-width="1"/>')
        lines.append(_svg_text(x, top + plot_h + 28, str(tick), size=14, anchor="middle", fill="#687386"))

    # Bars.
    for i, count in enumerate(counts):
        x0 = _score_to_x(float(edges[i]), left, plot_w)
        x1 = _score_to_x(float(edges[i + 1]), left, plot_w)
        y = y_pos(float(count))
        bar_h = top + plot_h - y
        lines.append(
            f'<rect x="{x0+3:.1f}" y="{y:.1f}" width="{max(1.0, x1-x0-6):.1f}" height="{bar_h:.1f}" '
            'rx="2" fill="#86a9f6" opacity="0.78"/>'
        )
        if count:
            lines.append(_svg_text((x0 + x1) / 2, max(top + 14, y - 6), str(int(count)), size=12, anchor="middle", fill="#40506a"))

    # Normal curve scaled to expected bin counts.
    if summary.std > 1e-12 and np.max(expected) > 0:
        points = " ".join(f"{_score_to_x(float(x), left, plot_w):.1f},{y_pos(float(y)):.1f}" for x, y in zip(xs, expected))
        lines.append(f'<polyline points="{points}" fill="none" stroke="#e05a47" stroke-width="4" stroke-linejoin="round" stroke-linecap="round"/>')
        lines.append('<rect x="990" y="28" width="230" height="42" rx="8" fill="#fff4f1" stroke="#ffd1c9"/>')
        lines.append('<line x1="1010" y1="49" x2="1060" y2="49" stroke="#e05a47" stroke-width="4" stroke-linecap="round"/>')
        lines.append(_svg_text(1070, 55, "同均值/方差正态曲线", size=15, fill="#5e3f38"))

    # Axes and labels.
    note_y = height - 40
    lines.extend(
        [
            f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top+plot_h}" stroke="#2f3a4d" stroke-width="1.4"/>',
            f'<line x1="{left}" y1="{top+plot_h}" x2="{width-right}" y2="{top+plot_h}" stroke="#2f3a4d" stroke-width="1.4"/>',
            _svg_text(left + plot_w / 2, height - 22, "分数（0-10）", size=16, anchor="middle", fill="#40506a"),
            f'<text x="0" y="0" font-size="16" text-anchor="middle" fill="#40506a" transform="translate(22 {top + plot_h / 2:.1f}) rotate(-90)">case 数量</text>',
            f'<rect x="{left}" y="{note_y-23}" width="{plot_w}" height="34" rx="8" fill="#ffffff" stroke="#e2e7f0"/>',
            _svg_text(left + 14, note_y, f"{summary.normality_label}：{summary.shape_note}", size=15, fill="#354052"),
        ]
    )

    lines.append("</svg>")
    out_path.write_text("\n".join(lines), encoding="utf-8")


def _summary_table_rows(summaries: list[MetricSummary]) -> str:
    rows = []
    for s in summaries:
        cls = "ok" if s.normality_label == "未拒绝正态" else "warn"
        rows.append(
            "<tr>"
            f"<td>{H.escape(s.label)}</td>"
            f"<td><code>{H.escape(s.metric)}</code></td>"
            f"<td class='num'>{s.n}</td>"
            f"<td class='num'>{_fmt(s.mean)}</td>"
            f"<td class='num'>{_fmt(s.std)}</td>"
            f"<td class='num'>{_fmt(s.median)}</td>"
            f"<td class='num'>{_fmt(s.min_score)}</td>"
            f"<td class='num'>{_fmt(s.max_score)}</td>"
            f"<td class='num'>{_fmt(s.skewness)}</td>"
            f"<td class='num'>{_fmt(s.excess_kurtosis)}</td>"
            f"<td class='num'>{_fmt(s.jb_p_value, 4)}</td>"
            f"<td><span class='{cls}'>{H.escape(s.normality_label)}</span></td>"
            f"<td>{H.escape(s.shape_note)}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _write_html_report(out_dir: Path, scores_csv: Path, row_count: int, systems: list[str], summaries: list[MetricSummary]) -> Path:
    cards = []
    for s in summaries:
        cards.append(
            f"""
<section class="metric-card" id="{H.escape(s.metric)}">
  <div class="metric-head">
    <div>
      <h2>{H.escape(s.label)}</h2>
      <p><code>{H.escape(s.metric)}</code> · {s.n} 个 per-case 分数 · {H.escape(s.normality_label)}</p>
    </div>
    <div class="statline">
      <span>均值 <b>{_fmt(s.mean)}</b></span>
      <span>标准差 <b>{_fmt(s.std)}</b></span>
      <span>偏度 <b>{_fmt(s.skewness)}</b></span>
      <span>峰度 <b>{_fmt(s.excess_kurtosis)}</b></span>
    </div>
  </div>
  <img src="{H.escape(s.svg_name)}" alt="{H.escape(s.label)} 分布图">
  <p class="note">{H.escape(s.shape_note)} 红线是使用该维度实际均值和标准差拟合出来的正态曲线；如果柱状分布明显偏离红线，说明这个维度不是正态分布。</p>
</section>
"""
        )

    normal_like = sum(1 for s in summaries if s.normality_label == "未拒绝正态")
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Benchmark Metrics Per-case Scores 分布</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #202a3a;
      --muted: #667085;
      --line: #dfe5ef;
      --panel: #ffffff;
      --soft: #f5f7fb;
      --blue: #486dd8;
      --warn: #b24d31;
      --ok: #257250;
    }}
    body {{
      margin: 0;
      background: #f7f9fc;
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      line-height: 1.55;
    }}
    main {{
      max-width: 1260px;
      margin: 0 auto;
      padding: 36px 28px 72px;
    }}
    h1 {{
      margin: 0 0 10px;
      font-size: 34px;
      letter-spacing: 0;
    }}
    h2 {{
      margin: 0;
      font-size: 23px;
      letter-spacing: 0;
    }}
    p {{
      margin: 6px 0;
    }}
    code {{
      background: #eef2f8;
      border-radius: 5px;
      padding: 1px 5px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.92em;
    }}
    .lede {{
      max-width: 980px;
      color: var(--muted);
      font-size: 16px;
    }}
    .summary-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin: 22px 0 20px;
    }}
    .kpi {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px 16px;
    }}
    .kpi .value {{
      font-size: 28px;
      font-weight: 800;
    }}
    .kpi .label {{
      color: var(--muted);
      font-size: 13px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      margin: 20px 0 28px;
      font-size: 13px;
    }}
    th, td {{
      border-bottom: 1px solid var(--line);
      padding: 8px 9px;
      text-align: left;
      vertical-align: top;
    }}
    th {{
      background: #eef2f7;
      font-weight: 750;
    }}
    tr:last-child td {{
      border-bottom: none;
    }}
    .num {{
      text-align: right;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }}
    .ok, .warn {{
      display: inline-block;
      border-radius: 999px;
      padding: 2px 8px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .ok {{
      color: var(--ok);
      background: #e7f5ee;
    }}
    .warn {{
      color: var(--warn);
      background: #fff0ea;
    }}
    .metric-card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 10px;
      margin: 18px 0;
      padding: 18px;
      box-shadow: 0 8px 22px rgba(25, 38, 60, 0.06);
    }}
    .metric-head {{
      display: flex;
      justify-content: space-between;
      gap: 20px;
      align-items: flex-start;
      margin-bottom: 10px;
    }}
    .metric-head p {{
      color: var(--muted);
    }}
    .statline {{
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 8px;
      min-width: 360px;
    }}
    .statline span {{
      background: var(--soft);
      border: 1px solid #e8edf5;
      border-radius: 8px;
      padding: 6px 9px;
      color: var(--muted);
      font-size: 13px;
    }}
    .statline b {{
      color: var(--ink);
      font-variant-numeric: tabular-nums;
    }}
    img {{
      display: block;
      width: 100%;
      border: 1px solid #e6ebf3;
      border-radius: 8px;
      background: #fbfcff;
    }}
    .note {{
      color: var(--muted);
      font-size: 14px;
      margin-top: 10px;
    }}
    @media (max-width: 860px) {{
      main {{ padding: 24px 14px 52px; }}
      .summary-grid {{ grid-template-columns: 1fr 1fr; }}
      .metric-head {{ display: block; }}
      .statline {{ justify-content: flex-start; min-width: 0; margin-top: 10px; }}
      table {{ font-size: 12px; }}
    }}
  </style>
</head>
<body>
<main>
  <h1>Benchmark Metrics Per-case Scores 分布</h1>
  <p class="lede">输入：<code>{H.escape(str(scores_csv))}</code>。每个维度用 0-10 分的真实 per-case 分数画直方图，并叠加同均值/方差的正态曲线，用于快速判断当前评分分布是否接近 normal distribution。</p>
  <div class="summary-grid">
    <div class="kpi"><div class="value">{row_count}</div><div class="label">benchmark rows</div></div>
    <div class="kpi"><div class="value">{len(systems)}</div><div class="label">systems: {H.escape(', '.join(systems))}</div></div>
    <div class="kpi"><div class="value">{len(summaries)}</div><div class="label">metrics dimensions</div></div>
    <div class="kpi"><div class="value">{normal_like}/{len(summaries)}</div><div class="label">Jarque-Bera 未拒绝正态</div></div>
  </div>

  <h2>统计汇总</h2>
  <table>
    <thead>
      <tr>
        <th>维度</th><th>metric id</th><th class="num">n</th><th class="num">mean</th><th class="num">std</th><th class="num">median</th>
        <th class="num">min</th><th class="num">max</th><th class="num">skew</th><th class="num">excess kurtosis</th><th class="num">JB p</th><th>正态性</th><th>形态判断</th>
      </tr>
    </thead>
    <tbody>
      {_summary_table_rows(summaries)}
    </tbody>
  </table>

  {"".join(cards)}
</main>
</body>
</html>
"""
    out_path = out_dir / "metric_distribution_report_zh.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path


def _write_summary_files(out_dir: Path, summaries: list[MetricSummary]) -> None:
    csv_path = out_dir / "metric_distribution_summary.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "metric",
                "label",
                "n",
                "mean",
                "std",
                "median",
                "min",
                "max",
                "skewness",
                "excess_kurtosis",
                "jarque_bera",
                "jb_p_value",
                "normality_label",
                "shape_note",
                "svg_name",
            ],
        )
        writer.writeheader()
        for s in summaries:
            writer.writerow(
                {
                    "metric": s.metric,
                    "label": s.label,
                    "n": s.n,
                    "mean": _fmt(s.mean, 6),
                    "std": _fmt(s.std, 6),
                    "median": _fmt(s.median, 6),
                    "min": _fmt(s.min_score, 6),
                    "max": _fmt(s.max_score, 6),
                    "skewness": _fmt(s.skewness, 6),
                    "excess_kurtosis": _fmt(s.excess_kurtosis, 6),
                    "jarque_bera": _fmt(s.jarque_bera, 6),
                    "jb_p_value": _fmt(s.jb_p_value, 8),
                    "normality_label": s.normality_label,
                    "shape_note": s.shape_note,
                    "svg_name": s.svg_name,
                }
            )

    json_path = out_dir / "metric_distribution_summary.json"
    json_path.write_text(
        json.dumps(
            [
                {
                    "metric": s.metric,
                    "label": s.label,
                    "n": s.n,
                    "mean": s.mean,
                    "std": s.std,
                    "median": s.median,
                    "min": s.min_score,
                    "max": s.max_score,
                    "skewness": s.skewness,
                    "excess_kurtosis": s.excess_kurtosis,
                    "jarque_bera": s.jarque_bera,
                    "jb_p_value": s.jb_p_value,
                    "normality_label": s.normality_label,
                    "shape_note": s.shape_note,
                    "svg_name": s.svg_name,
                }
                for s in summaries
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scores-csv", type=Path, default=DEFAULT_SCORES, help="Benchmark scores.csv path.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT, help="Directory for SVG/HTML outputs.")
    parser.add_argument("--bins", type=int, default=20, help="Histogram bin count across score range 0-10.")
    args = parser.parse_args()

    if args.bins < 5:
        raise SystemExit("--bins must be >= 5")
    if not args.scores_csv.exists():
        raise SystemExit(f"scores CSV not found: {args.scores_csv}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    values, systems, row_count = _read_metric_values(args.scores_csv, METRICS)

    summaries: list[MetricSummary] = []
    for metric in METRICS:
        summary = _summarize(metric, values[metric])
        summaries.append(summary)
        _write_metric_svg(metric, values[metric], summary, args.out_dir / summary.svg_name, bins=args.bins)

    _write_summary_files(args.out_dir, summaries)
    report_path = _write_html_report(args.out_dir, args.scores_csv, row_count, systems, summaries)

    print(f"Wrote {report_path}")
    print(f"Wrote {args.out_dir / 'metric_distribution_summary.csv'}")
    print()
    print("metric,label,n,mean,std,skewness,excess_kurtosis,jb_p_value,normality")
    for s in summaries:
        print(
            ",".join(
                [
                    s.metric,
                    s.label,
                    str(s.n),
                    _fmt(s.mean),
                    _fmt(s.std),
                    _fmt(s.skewness),
                    _fmt(s.excess_kurtosis),
                    _fmt(s.jb_p_value, 4),
                    s.normality_label,
                ]
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
