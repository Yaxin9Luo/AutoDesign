#!/usr/bin/env python3
"""Run the paper-body screenshot detector over a saved poster population."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
import csv
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from autodesign.evaluator.metrics import (  # noqa: E402
    _extract_paper_text,
    paper_body_screenshot_metrics,
)
from autodesign.evaluator.ocr import run_ocr  # noqa: E402


KNOWN_SEVERE_CASES = {
    "2020-global-burden-of-369-diseases-and-injuries-in-204-countries-and-territories-1990-2019",
    "2023-global-carbon-budget-2023",
    "2016-observation-of-gravitational-waves-from-a-binary-black-hole-merger",
    "2019-first-m87-event-horizon-telescope-results-i-the-shadow-of-the-supermassive-black-hole",
}
KNOWN_CONTROL_CASE = "2016-mimic-iii-a-freely-accessible-critical-care-database"
DIRECT_SYSTEM = "direct_cc_deepseek_v4_pro"
SEVERITY_ORDER = {"catastrophic": 0, "severe": 1, "moderate": 2, "none": 3, "unknown": 4}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    manifest_path = Path(args.manifest).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    item_dir = out_dir / "items"
    item_dir.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = list(manifest.get("items") or [])
    if args.system_key:
        items = [item for item in items if item.get("system_key") == args.system_key]
    if args.limit:
        items = items[:args.limit]

    completed = _load_completed(item_dir, items)
    pending = [item for item in items if item["id"] not in completed]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in pending:
        groups[item["paper"]].append(item)

    print(
        f"population={len(items)} cached={len(completed)} pending={len(pending)} "
        f"paper_groups={len(groups)} workers={args.workers}",
        flush=True,
    )
    if groups:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(_scan_paper_group, group): group
                for group in groups.values()
            }
            finished_groups = 0
            for future in as_completed(futures):
                group = futures[future]
                finished_groups += 1
                try:
                    results = future.result()
                except Exception as exc:  # noqa: BLE001
                    results = [
                        {
                            **_identity(item),
                            "status": "error",
                            "severity_level": "unknown",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                        for item in group
                    ]
                for result in results:
                    _write_json(item_dir / f"{result['id']}.json", result)
                    completed[result["id"]] = result
                if finished_groups == 1 or finished_groups % 5 == 0 or finished_groups == len(groups):
                    severity = Counter(
                        result.get("severity_level", "unknown")
                        for result in completed.values()
                    )
                    print(
                        f"groups={finished_groups}/{len(groups)} items={len(completed)}/{len(items)} "
                        f"severity={dict(severity)}",
                        flush=True,
                    )

    results = [completed[item["id"]] for item in items if item["id"] in completed]
    summary = _build_summary(manifest, results)
    _write_json(out_dir / "paper_body_scan_summary.json", summary)
    _write_csv(out_dir / "paper_body_scan.csv", results)
    gallery_html = _gallery_html(summary)
    for file_name in ("paper_body_scan_gallery.html", "paper_body_scan_gallery_zh.html"):
        (out_dir / file_name).write_text(gallery_html, encoding="utf-8")
    print(json.dumps(summary["counts"], indent=2), flush=True)
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--system-key")
    return parser.parse_args(argv)


def _scan_paper_group(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    paper = Path(items[0]["paper"])
    paper_text = _extract_paper_text(paper)
    results: list[dict[str, Any]] = []
    for item in items:
        ocr = run_ocr(item["artifact"], include_segments=True)
        bundle, findings = paper_body_screenshot_metrics(
            paper,
            ocr,
            paper_text=paper_text,
        )
        metrics = bundle.metrics
        results.append({
            **_identity(item),
            "status": "ok" if metrics.get("available") else "unavailable",
            "severity_level": metrics.get("severity_level", "unknown"),
            "catastrophic_reason": metrics.get("catastrophic_reason", ""),
            "severe_reason": metrics.get("severe_reason", ""),
            "moderate_reason": metrics.get("moderate_reason", ""),
            "ocr_word_count": metrics.get("ocr_word_count"),
            "text_coverage_ratio": metrics.get("text_coverage_ratio"),
            "exact_ngram_hit_ratio": metrics.get("exact_ngram_hit_ratio"),
            "copied_token_count": metrics.get("copied_token_count"),
            "copied_token_ratio": metrics.get("copied_token_ratio"),
            "copied_body_segment_count": metrics.get("copied_body_segment_count"),
            "copied_body_segment_area_ratio": metrics.get("copied_body_segment_area_ratio"),
            "page_crop_layout": metrics.get("page_crop_layout"),
            "finding_ids": [finding.id for finding in findings],
            "examples": metrics.get("examples") or [],
        })
    return results


def _identity(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key)
        for key in (
            "id", "cohort", "system_key", "system_label", "discipline",
            "discipline_label", "case", "algorithm_score", "algorithm_verdict",
            "artifact", "artifact_uri", "paper", "source_dataset",
        )
    }


def _load_completed(
    item_dir: Path,
    items: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    allowed = {item["id"] for item in items}
    completed: dict[str, dict[str, Any]] = {}
    for path in item_dir.glob("*.json"):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if result.get("id") in allowed:
            completed[result["id"]] = result
    return completed


def _build_summary(
    manifest: dict[str, Any],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    severity = Counter(result.get("severity_level", "unknown") for result in results)
    by_system: list[dict[str, Any]] = []
    for system_key in sorted({result["system_key"] for result in results}):
        segment = [result for result in results if result["system_key"] == system_key]
        counts = Counter(result.get("severity_level", "unknown") for result in segment)
        by_system.append({
            "system_key": system_key,
            "system_label": segment[0]["system_label"],
            "n": len(segment),
            **{level: counts[level] for level in ("catastrophic", "severe", "moderate", "none", "unknown")},
        })

    direct = {
        result["case"]: result
        for result in results
        if result["system_key"] == DIRECT_SYSTEM
    }
    known_checks = [
        {
            "case": case,
            "expected": "severe_or_worse",
            "observed": (direct.get(case) or {}).get("severity_level"),
            "match": (direct.get(case) or {}).get("severity_level") in {"catastrophic", "severe"},
        }
        for case in sorted(KNOWN_SEVERE_CASES)
    ]
    known_checks.append({
        "case": KNOWN_CONTROL_CASE,
        "expected": "not_severe",
        "observed": (direct.get(KNOWN_CONTROL_CASE) or {}).get("severity_level"),
        "match": (direct.get(KNOWN_CONTROL_CASE) or {}).get("severity_level") not in {"catastrophic", "severe"},
    })
    return {
        "version": 1,
        "manifest_id": manifest.get("manifest_id"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "items": len(results),
            "catastrophic": severity["catastrophic"],
            "severe": severity["severe"],
            "moderate": severity["moderate"],
            "none": severity["none"],
            "unknown": severity["unknown"],
        },
        "known_regression": {
            "matched": sum(check["match"] for check in known_checks),
            "total": len(known_checks),
            "checks": known_checks,
        },
        "by_system": by_system,
        "triggered": sorted(
            (
                result for result in results
                if result.get("severity_level") in {"catastrophic", "severe", "moderate"}
            ),
            key=lambda result: (
                SEVERITY_ORDER.get(str(result.get("severity_level")), 99),
                -(float(result.get("copied_body_segment_area_ratio") or 0.0)),
                result.get("system_key", ""),
                result.get("case", ""),
            ),
        ),
        "results": results,
    }


def _write_csv(path: Path, results: list[dict[str, Any]]) -> None:
    fields = [
        "id", "cohort", "system_key", "system_label", "discipline", "case",
        "algorithm_score", "algorithm_verdict", "severity_level", "catastrophic_reason", "severe_reason",
        "moderate_reason", "ocr_word_count", "text_coverage_ratio",
        "exact_ngram_hit_ratio", "copied_token_count", "copied_token_ratio",
        "copied_body_segment_count", "copied_body_segment_area_ratio", "artifact", "paper",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: result.get(field) for field in fields} for result in results)


def _gallery_html(summary: dict[str, Any]) -> str:
    severity_labels = {"catastrophic": "灾难级", "severe": "严重", "moderate": "中度", "none": "未命中", "unknown": "不可用"}
    reason_labels = {
        "verbatim_mass": "大量逐字复制",
        "dense_paper_crop": "密集论文正文裁切",
        "paper_page_microtext_crop": "论文页面微小文字裁切",
        "verbatim_copying": "大量逐字复制",
        "copied_body_canvas_wall": "正文截图占满画布",
        "paper_page_microtext_wall": "论文页面截图墙",
        "regional_paper_crop": "区域级论文正文裁切",
        "sparse_paper_crop": "稀疏海报中的正文裁切",
    }
    cards: list[str] = []
    for result in summary["triggered"]:
        metrics = (
            f"OCR 词数 {result.get('ocr_word_count')} | 文字覆盖率 {result.get('text_coverage_ratio')} | "
            f"复制词占比 {result.get('copied_token_ratio')} | 复制正文面积 {result.get('copied_body_segment_area_ratio')}"
        )
        reason = result.get("catastrophic_reason") or result.get("severe_reason") or result.get("moderate_reason") or ""
        severity = str(result.get("severity_level") or "unknown")
        cards.append(f'''<article class="item {html.escape(str(result.get("severity_level")))}">
<img src="{html.escape(str(result.get("artifact_uri") or Path(result["artifact"]).as_uri()))}" alt="海报">
<div class="body"><div class="eyebrow">{html.escape(severity_labels.get(severity, severity))} · {html.escape(reason_labels.get(str(reason), str(reason)))}</div>
<h2>{html.escape(str(result.get("case")))}</h2><p>{html.escape(str(result.get("system_label")))}</p>
<p class="metrics">旧算法分数 {html.escape(str(result.get("algorithm_score")))} | {html.escape(metrics)}</p></div></article>''')
    rows = "".join(
        f"<tr><td>{html.escape(row['system_label'])}</td><td>{row['n']}</td><td>{row['catastrophic']}</td><td>{row['severe']}</td><td>{row['moderate']}</td><td>{row['none']}</td><td>{row['unknown']}</td></tr>"
        for row in summary["by_system"]
    )
    counts = summary["counts"]
    template = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark"><title>论文正文截图检测全量扫描</title><style>
:root{--bg:#f4f5f6;--surface:#fff;--ink:#17191c;--muted:#66707a;--line:#d8dde2;--danger:#a63c2f;--warn:#926400}@media(prefers-color-scheme:dark){:root{--bg:#151719;--surface:#202327;--ink:#f4f5f6;--muted:#adb4bc;--line:#3a4148;--danger:#ff9c91;--warn:#f0c36b}}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,sans-serif}main{width:min(1600px,calc(100% - 28px));margin:24px auto 70px}h1{font-size:28px;letter-spacing:0;margin:0 0 6px}.summary{color:var(--muted);margin:0 0 20px}table{width:100%;border-collapse:collapse;background:var(--surface);margin:0 0 24px}th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:right}th:first-child,td:first-child{text-align:left}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.item{overflow:hidden;border:1px solid var(--line);border-radius:8px;background:var(--surface)}.item img{display:block;width:100%;aspect-ratio:2/1;object-fit:contain;background:#fff}.body{padding:10px}.eyebrow{color:var(--warn);font-size:12px;font-weight:700}.severe .eyebrow{color:var(--danger)}h2{font-size:14px;letter-spacing:0;margin:4px 0;overflow-wrap:anywhere}p{margin:3px 0;color:var(--muted)}.metrics{font-variant-numeric:tabular-nums}@media(max-width:800px){.grid{grid-template-columns:1fr}}
</style></head><body><main><h1>论文正文截图检测全量扫描</h1><p class="summary">__SUMMARY__</p><table><thead><tr><th>系统</th><th>数量</th><th>灾难级</th><th>严重</th><th>中度</th><th>未命中</th><th>不可用</th></tr></thead><tbody>__ROWS__</tbody></table><div class="grid">__CARDS__</div></main></body></html>'''
    return (
        template
        .replace("__SUMMARY__", f"共 {counts['items']} 张海报 · {counts['catastrophic']} 张灾难级 · {counts['severe']} 张严重命中 · {counts['moderate']} 张中度命中 · {counts['unknown']} 张不可用")
        .replace("__ROWS__", rows)
        .replace("__CARDS__", "".join(cards))
    )


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
