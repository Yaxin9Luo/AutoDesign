#!/usr/bin/env python3
"""Build a Chinese HTML review report for professional_aesthetics judge outputs."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from statistics import median
from typing import Any


_REPO = Path(__file__).resolve().parent.parent


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _dimension(report: dict[str, Any], dim_id: str) -> dict[str, Any]:
    for dim in report.get("dimensions") or []:
        if isinstance(dim, dict) and dim.get("id") == dim_id:
            return dim
    return {}


def _candidate_reports(run_dir: Path) -> list[Path]:
    return sorted((run_dir / "candidates").glob("*/poster_quality_report.json"))


def _raw_judge_report(final: dict[str, Any], cand_dir: Path) -> dict[str, Any]:
    path = final.get("judge_report_path")
    if path:
        ap = Path(path)
        if ap.is_file():
            return _load_json(ap)
        if ap.is_dir():
            dim_path = ap / "professional_aesthetics.json"
            if dim_path.exists():
                return _load_json(dim_path)
    dim_path = cand_dir / "vlm" / "professional_aesthetics.json"
    if dim_path.exists():
        return _load_json(dim_path)
    return {}


def _raw_aesthetics(raw: dict[str, Any]) -> dict[str, Any]:
    if raw.get("dimension") == "professional_aesthetics" or "score_0_10" in raw:
        return raw
    scores = raw.get("dimension_scores") if isinstance(raw.get("dimension_scores"), dict) else {}
    aes = scores.get("professional_aesthetics") if isinstance(scores.get("professional_aesthetics"), dict) else None
    return aes or {}


def _deterministic(cand_dir: Path) -> dict[str, Any]:
    return _load_json(cand_dir / "deterministic" / "deterministic_report.json")


def _preview_uri(det: dict[str, Any], final: dict[str, Any]) -> str:
    for raw in (det.get("preview_image"), final.get("artifact")):
        if not raw:
            continue
        path = Path(str(raw))
        if path.exists():
            return path.resolve().as_uri()
    return ""


def _list_items(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
        elif isinstance(value, dict):
            text = value.get("evidence") or value.get("where") or value.get("defect")
            if text:
                out.append(str(text))
    return out


def _defects(raw_aes: dict[str, Any], final: dict[str, Any]) -> list[str]:
    defects = raw_aes.get("defects_found") if isinstance(raw_aes.get("defects_found"), list) else []
    out: list[str] = []
    for defect in defects:
        if not isinstance(defect, dict):
            continue
        name = str(defect.get("defect") or "").strip()
        where = str(defect.get("where") or "").strip()
        severity = str(defect.get("severity") or "").strip()
        parts = [p for p in (severity, name, where) if p]
        if parts:
            out.append(" / ".join(parts))
    for finding in final.get("findings") or []:
        if isinstance(finding, dict) and finding.get("dimension") == "professional_aesthetics":
            msg = str(finding.get("message") or finding.get("claim") or finding.get("id") or "").strip()
            if msg:
                out.append(msg)
    return out


def _manual_review(manual: dict[str, Any], run_name: str, candidate: str) -> tuple[str, str]:
    for key in (f"{run_name}/{candidate}", candidate):
        item = manual.get(key)
        if isinstance(item, dict):
            return str(item.get("verdict") or "待人工 review"), str(item.get("note") or "")
        if isinstance(item, str):
            return item, ""
    return "待人工 review", ""


def _collect(run_dirs: list[Path], manual: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for run_dir in run_dirs:
        run_name = run_dir.name
        for report_path in _candidate_reports(run_dir):
            final = _load_json(report_path)
            cand_dir = report_path.parent
            candidate = str(final.get("candidate_name") or cand_dir.name)
            dim = _dimension(final, "professional_aesthetics")
            raw = _raw_judge_report(final, cand_dir)
            raw_aes = _raw_aesthetics(raw)
            det = _deterministic(cand_dir)
            evidence = _list_items(dim.get("visible_evidence")) or _list_items(raw_aes.get("evidence"))
            defects = _defects(raw_aes, final)
            review, note = _manual_review(manual, run_name, candidate)
            rows.append({
                "run": run_name,
                "candidate": candidate,
                "score": dim.get("score_0_10"),
                "status": dim.get("status"),
                "source": dim.get("source"),
                "rationale": dim.get("rationale") or raw_aes.get("rationale") or "",
                "evidence": evidence,
                "defects": defects,
                "preview": _preview_uri(det, final),
                "manual_review": review,
                "manual_note": note,
                "judge_report_path": final.get("judge_report_path") or "",
            })
    rows.sort(key=lambda r: (float(r["score"]) if isinstance(r.get("score"), (int, float)) else 999.0, r["run"], r["candidate"]))
    return rows


def _score_text(score: Any) -> str:
    if isinstance(score, (int, float)):
        return f"{float(score):.2f}"
    return "N/A"


def _ul(items: list[str]) -> str:
    if not items:
        return "<span class='muted'>无</span>"
    lis = "".join(f"<li>{html.escape(str(item))}</li>" for item in items[:6])
    return f"<ul>{lis}</ul>"


def _render(rows: list[dict[str, Any]]) -> str:
    scored = [float(r["score"]) for r in rows if isinstance(r.get("score"), (int, float))]
    stats = "无可用分数"
    if scored:
        stats = f"{len(scored)} 个有分数；最低 {min(scored):.2f}，中位 {median(scored):.2f}，最高 {max(scored):.2f}"
    cards = []
    for row in rows:
        img = f"<img src='{html.escape(row['preview'])}' alt='poster preview'>" if row.get("preview") else "<div class='noimg'>无预览</div>"
        cards.append(f"""
<section class="card">
  <div class="thumb">{img}</div>
  <div class="body">
    <div class="topline">
      <h2>{html.escape(row['candidate'])}</h2>
      <span class="score">{_score_text(row.get('score'))}</span>
    </div>
    <p class="meta">Run: <code>{html.escape(row['run'])}</code> · source: {html.escape(str(row.get('source') or ''))} · status: {html.escape(str(row.get('status') or ''))}</p>
    <p><b>Judge rationale：</b>{html.escape(str(row.get('rationale') or ''))}</p>
    <div class="cols">
      <div><b>Visible evidence</b>{_ul(row['evidence'])}</div>
      <div><b>Defects / cap reason</b>{_ul(row['defects'])}</div>
      <div><b>人工 review</b><p class="review">{html.escape(row['manual_review'])}</p><p>{html.escape(row['manual_note'])}</p></div>
    </div>
    <p class="meta">Judge report: <code>{html.escape(str(row.get('judge_report_path') or ''))}</code></p>
  </div>
</section>""")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Professional Aesthetics Judge 中文汇总</title>
<style>
body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f5f6f8; color: #1f2933; }}
header {{ padding: 28px 36px; background: #111827; color: white; }}
h1 {{ margin: 0 0 8px; font-size: 28px; }}
h2 {{ margin: 0; font-size: 20px; }}
main {{ padding: 24px 36px 48px; }}
.summary {{ margin: 0 0 20px; color: #d7dde8; }}
.card {{ display: grid; grid-template-columns: 320px 1fr; gap: 20px; margin: 0 0 22px; padding: 18px; background: white; border: 1px solid #d9dee8; border-radius: 8px; }}
.thumb img {{ width: 100%; max-height: 460px; object-fit: contain; border: 1px solid #e2e8f0; background: #fff; }}
.noimg {{ height: 240px; display: grid; place-items: center; border: 1px dashed #aeb7c4; color: #6b7280; }}
.topline {{ display: flex; align-items: baseline; justify-content: space-between; gap: 16px; }}
.score {{ font-size: 28px; font-weight: 700; color: #0f766e; }}
.meta {{ color: #667085; font-size: 13px; }}
.cols {{ display: grid; grid-template-columns: 1fr 1fr 220px; gap: 18px; }}
ul {{ margin: 8px 0 0 18px; padding: 0; }}
li {{ margin-bottom: 6px; }}
.muted {{ color: #8a94a6; }}
.review {{ font-weight: 700; color: #374151; }}
code {{ white-space: normal; word-break: break-all; }}
@media (max-width: 900px) {{
  main {{ padding: 16px; }}
  .card {{ grid-template-columns: 1fr; }}
  .cols {{ grid-template-columns: 1fr; }}
}}
</style>
</head>
<body>
<header>
  <h1>Professional Aesthetics Judge 中文汇总</h1>
  <p class="summary">{html.escape(stats)}。按美学分数从低到高排序，用于人工检查 judge 是否偏高、偏低或证据不足。</p>
</header>
<main>
{''.join(cards)}
</main>
</body>
</html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", action="append", type=Path, required=True, help="Quality eval output dir containing candidates/*/poster_quality_report.json")
    parser.add_argument("--manual-review", type=Path, default=None, help="Optional JSON map of candidate or run/candidate to {verdict,note}")
    parser.add_argument("--out", type=Path, default=_REPO / "out/eval/report/professional_aesthetics_summary_zh.html")
    args = parser.parse_args()

    manual = _load_json(args.manual_review) if args.manual_review else {}
    rows = _collect([p.expanduser().resolve() for p in args.run_dir], manual)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(_render(rows), encoding="utf-8")
    print(f"Wrote {args.out} with {len(rows)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
