#!/usr/bin/env python3
"""Build and analyze a blind, paired calibration set for poster evaluation.

The primary sample is grouped by paper: each selected paper contributes every
system output in the benchmark. This controls paper difficulty and makes score
inversions directly measurable. Optional ablation and direct-harness CSVs are
added as a targeted failure cohort without changing the primary sample.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import html
import itertools
import json
import math
from pathlib import Path
import random
import re
import statistics
import subprocess
import sys
from typing import Any, Iterable


KNOWN_SEVERE_CASES = {
    "2020-global-burden-of-369-diseases-and-injuries-in-204-countries-and-territories-1990-2019",
    "2023-global-carbon-budget-2023",
    "2016-observation-of-gravitational-waves-from-a-binary-black-hole-merger",
    "2019-first-m87-event-horizon-telescope-results-i-the-shadow-of-the-supermassive-black-hole",
}
KNOWN_SEVERE_SCORES = {
    "2020-global-burden-of-369-diseases-and-injuries-in-204-countries-and-territories-1990-2019": 25,
    "2023-global-carbon-budget-2023": 25,
    "2016-observation-of-gravitational-waves-from-a-binary-black-hole-merger": 0,
    "2019-first-m87-event-horizon-telescope-results-i-the-shadow-of-the-supermassive-black-hole": 0,
}

FAILURE_TAXONOMY = [
    {"id": "paper_body_screenshot", "label": "论文正文截图"},
    {"id": "wrong_large_crop", "label": "错误的大面积裁切"},
    {"id": "unreadable_microtext", "label": "不可读微小文字"},
    {"id": "broken_layout", "label": "布局破损"},
    {"id": "sparse_or_unfinished", "label": "稀疏或未完成"},
    {"id": "weak_visual_hierarchy", "label": "视觉层级弱"},
    {"id": "source_mismatch", "label": "来源不匹配"},
    {"id": "generic_or_decorative", "label": "泛化或装饰性内容"},
]

TIER_DEFAULT_SCORE = {
    "severe": 25,
    "fail": 43,
    "revise": 60,
    "pass": 78,
}

SPLIT_LABELS = {
    "development": "开发集",
    "holdout": "保留集",
    "diagnostic": "诊断集",
}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    base_items, quality = _load_base_items(Path(args.records_csv).expanduser().resolve())
    paper_index = _paper_index(base_items)
    extra_items: list[dict[str, Any]] = []
    if args.ablation_csv:
        extra_items.extend(_load_ablation_items(Path(args.ablation_csv).expanduser().resolve(), paper_index))
    if args.direct_comparison_csv:
        extra_items.extend(
            _load_direct_items(Path(args.direct_comparison_csv).expanduser().resolve(), paper_index)
        )

    selected_cases = _select_cases(
        base_items,
        groups_per_discipline=args.groups_per_discipline,
        seed=args.seed,
    )
    manifest = _build_manifest(
        base_items=base_items,
        extra_items=extra_items,
        selected_cases=selected_cases,
        quality=quality,
        seed=args.seed,
        source_paths={
            "base_records": str(Path(args.records_csv).expanduser().resolve()),
            "ablation_records": str(Path(args.ablation_csv).expanduser().resolve()) if args.ablation_csv else None,
            "direct_comparison": str(Path(args.direct_comparison_csv).expanduser().resolve()) if args.direct_comparison_csv else None,
        },
    )

    _write_json(out_dir / "calibration_manifest.json", manifest)
    _write_manifest_csv(out_dir / "calibration_manifest.csv", manifest["items"])
    population_items = base_items + extra_items
    population_id = hashlib.sha256(
        "|".join(sorted(item["id"] for item in population_items)).encode()
    ).hexdigest()[:16]
    _write_json(out_dir / "detector_population_manifest.json", {
        "version": 1,
        "manifest_id": f"poster-detector-population-{population_id}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_paths": manifest["source_paths"],
        "data_quality": quality,
        "items": population_items,
    })
    _write_manifest_csv(out_dir / "detector_population_manifest.csv", population_items)
    _write_json(out_dir / "labels_template.json", {
        "version": 1,
        "manifest_id": manifest["manifest_id"],
        "labels": {},
    })
    _write_json(out_dir / "known_anchor_labels.json", {
        "version": 1,
        "manifest_id": manifest["manifest_id"],
        "labels": _known_anchor_labels(manifest["items"]),
    })
    blind_review_html = _blind_review_html(manifest)
    for file_name in ("blind_review.html", "blind_review_zh.html"):
        (out_dir / file_name).write_text(blind_review_html, encoding="utf-8")

    analysis: dict[str, Any] | None = None
    if args.labels:
        labels = json.loads(Path(args.labels).expanduser().read_text(encoding="utf-8"))
        analysis = _analyze_labels(manifest, labels)
        _write_json(out_dir / "calibration_analysis.json", analysis)
        _write_analysis_csv(out_dir / "calibration_analysis_rows.csv", analysis["rows"])

    body_scan: dict[str, Any] | None = None
    if args.body_scan_summary:
        body_scan = json.loads(
            Path(args.body_scan_summary).expanduser().read_text(encoding="utf-8")
        )

    legacy_label_count = _legacy_label_count(
        Path(args.legacy_labels).expanduser().resolve() if args.legacy_labels else None
    )
    _write_report(
        out_dir=out_dir,
        manifest=manifest,
        analysis=analysis,
        body_scan=body_scan,
        legacy_label_count=legacy_label_count,
        shell_path=Path(args.report_shell).expanduser().resolve() if args.report_shell else None,
        embed_helper=Path(args.embed_helper).expanduser().resolve() if args.embed_helper else None,
    )

    print(json.dumps({
        "manifest_id": manifest["manifest_id"],
        "items": len(manifest["items"]),
        "groups": len(manifest["groups"]),
        "base_sample_items": manifest["coverage"]["base_sample_items"],
        "targeted_items": manifest["coverage"]["targeted_items"],
        "out_dir": str(out_dir),
    }, indent=2))
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records-csv", required=True, help="Canonical benchmark records CSV.")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--ablation-csv", help="Optional combined ablation scores CSV.")
    parser.add_argument("--direct-comparison-csv", help="Optional direct-vs-harness comparison CSV.")
    parser.add_argument("--legacy-labels", help="Existing case-level labels JSON for coverage reporting.")
    parser.add_argument("--labels", help="Exported blind-review labels JSON to analyze.")
    parser.add_argument("--body-scan-summary", help="Optional full detector sweep summary JSON.")
    parser.add_argument("--groups-per-discipline", type=int, default=6)
    parser.add_argument("--seed", type=int, default=20260710)
    parser.add_argument("--report-shell", help="Optional Data Analytics HTML report shell.")
    parser.add_argument("--embed-helper", help="Optional Recharts HTML embedding helper.")
    return parser.parse_args(argv)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _load_base_items(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = _read_csv(path)
    required = {"system_key", "system", "discipline_label", "case", "overall", "artifact", "paper"}
    missing_columns = sorted(required - set(rows[0] if rows else []))
    if missing_columns:
        raise ValueError(f"missing required columns in {path}: {', '.join(missing_columns)}")

    items: list[dict[str, Any]] = []
    key_counts: Counter[tuple[str, str, str]] = Counter()
    missing_artifacts: list[str] = []
    missing_papers: list[str] = []
    label_mismatches: Counter[tuple[str, str, str]] = Counter()
    label_to_systems: dict[str, set[str]] = defaultdict(set)
    recovered_artifacts: list[str] = []
    for row in rows:
        source_artifact = Path(row["artifact"]).expanduser()
        paper = Path(row["paper"]).expanduser()
        discipline = paper.parent.parent.name if len(paper.parents) >= 2 else row["discipline_label"]
        system_key = row["system_key"].strip()
        system_name = row["system"].strip() or system_key
        reported_label = row.get("system_label", "").strip()
        if reported_label:
            label_to_systems[reported_label].add(system_key)
        if reported_label and reported_label != system_name:
            label_mismatches[(system_key, system_name, reported_label)] += 1
        artifact = source_artifact
        if not artifact.exists():
            fallback = _snapshot_fallback(path, system_key, discipline, row["case"].strip())
            if fallback:
                artifact = fallback
                recovered_artifacts.append(str(source_artifact))
        item = _item(
            cohort="base_sample",
            system_key=system_key,
            system_label=system_name,
            discipline=discipline,
            discipline_label=row["discipline_label"].strip(),
            case=row["case"].strip(),
            score=_float(row.get("overall")),
            verdict=row.get("verdict", "").strip(),
            artifact=artifact,
            paper=paper,
            source_dataset=path.name,
        )
        items.append(item)
        key_counts[(system_key, discipline, item["case"])] += 1
        if not artifact.exists():
            missing_artifacts.append(str(source_artifact))
        if not paper.exists():
            missing_papers.append(str(paper))

    systems = sorted({item["system_key"] for item in items})
    groups: dict[tuple[str, str], int] = Counter(
        (item["discipline"], item["case"]) for item in items
    )
    collision_labels = {
        label: sorted(system_keys)
        for label, system_keys in label_to_systems.items()
        if len(system_keys) > 1
    }
    quality = {
        "row_count": len(items),
        "system_count": len(systems),
        "paper_group_count": len(groups),
        "complete_paper_groups": sum(count == len(systems) for count in groups.values()),
        "duplicate_grain_keys": sum(count > 1 for count in key_counts.values()),
        "source_artifact_missing_count": len(missing_artifacts) + len(recovered_artifacts),
        "recovered_artifact_count": len(recovered_artifacts),
        "unresolved_artifact_count": len(missing_artifacts),
        "missing_paper_count": len(missing_papers),
        "system_label_mismatch_rows": sum(label_mismatches.values()),
        "system_label_collision_rows": sum(
            1 for row in rows if row.get("system_label", "").strip() in collision_labels
        ),
        "system_label_collisions": collision_labels,
        "system_label_mismatches": [
            {
                "system_key": key[0],
                "canonical_system": key[1],
                "reported_system_label": key[2],
                "rows": count,
            }
            for key, count in sorted(label_mismatches.items())
        ],
    }
    return items, quality


def _snapshot_fallback(
    records_csv: Path,
    system_key: str,
    discipline: str,
    case: str,
) -> Path | None:
    candidates = list((records_csv.parent / system_key / "candidates").glob(
        f"*/{discipline}/{case}/deterministic/snapshot/artifact_preview.png"
    ))
    return candidates[0].resolve() if len(candidates) == 1 else None


def _paper_index(items: Iterable[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        record = {
            "paper": Path(item["paper"]),
            "discipline": item["discipline"],
            "discipline_label": item["discipline_label"],
        }
        index[(item["discipline_label"], item["case"])] = record
        index[(item["discipline"], item["case"])] = record
    return index


def _load_ablation_items(
    path: Path,
    paper_index: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in _read_csv(path):
        paper_record = _lookup_paper(paper_index, row.get("discipline_label", ""), row["case"])
        paper = paper_record["paper"]
        artifact = Path(row["artifact"]).expanduser()
        model_label = row.get("model_label", "").strip() or "Ablation model"
        route = row.get("route", "").strip()
        items.append(_item(
            cohort="targeted",
            system_key=f"ablation_{_slug(model_label)}_{_slug(route)}".rstrip("_"),
            system_label=f"{model_label} ({route})" if route else model_label,
            discipline=paper_record["discipline"],
            discipline_label=paper_record["discipline_label"],
            case=row["case"].strip(),
            score=_float(row.get("overall")),
            verdict=row.get("verdict", "").strip(),
            artifact=artifact,
            paper=paper,
            source_dataset=path.name,
        ))
    return items


def _load_direct_items(
    path: Path,
    paper_index: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in _read_csv(path):
        paper_record = _lookup_paper(paper_index, row.get("discipline_label", ""), row["case"])
        paper = paper_record["paper"]
        artifact = Path(row["direct_png"]).expanduser()
        if not artifact.exists():
            recovered = _recover_direct_artifact(paper, row["case"].strip())
            if recovered:
                artifact = recovered
        items.append(_item(
            cohort="targeted",
            system_key="direct_cc_deepseek_v4_pro",
            system_label="Direct Claude Code + DeepSeek V4 Pro",
            discipline=paper_record["discipline"],
            discipline_label=paper_record["discipline_label"],
            case=row["case"].strip(),
            score=_float(row.get("direct_score")),
            verdict=row.get("direct_verdict", "").strip(),
            artifact=artifact,
            paper=paper,
            source_dataset=path.name,
        ))
    return items


def _recover_direct_artifact(paper: Path, case: str) -> Path | None:
    try:
        corpus_root = next(parent for parent in paper.parents if parent.name == "AutoDeisgn-PosterBench")
    except StopIteration:
        return None
    matches = list((corpus_root / "benchmark").glob(
        f"*deepseek-v4-pro-small-subset-20260708-154000/png/*-{case}.png"
    ))
    return matches[0].resolve() if len(matches) == 1 else None


def _lookup_paper(
    paper_index: dict[tuple[str, str], dict[str, Any]],
    discipline: str,
    case: str,
) -> dict[str, Any]:
    key = (discipline.strip(), case.strip())
    if key in paper_index:
        return paper_index[key]
    matches = {
        (str(record["paper"]), record["discipline"], record["discipline_label"])
        for (disc, slug), record in paper_index.items()
        if slug == case.strip()
    }
    if len(matches) == 1:
        paper, canonical_discipline, discipline_label = next(iter(matches))
        return {
            "paper": Path(paper),
            "discipline": canonical_discipline,
            "discipline_label": discipline_label,
        }
    raise KeyError(f"paper not found for {discipline} / {case}")


def _item(
    *,
    cohort: str,
    system_key: str,
    system_label: str,
    discipline: str,
    discipline_label: str,
    case: str,
    score: float | None,
    verdict: str,
    artifact: Path,
    paper: Path,
    source_dataset: str,
) -> dict[str, Any]:
    artifact = artifact.expanduser().resolve()
    paper = paper.expanduser().resolve()
    raw_id = f"{cohort}|{system_key}|{artifact}"
    return {
        "id": hashlib.sha256(raw_id.encode()).hexdigest()[:16],
        "cohort": cohort,
        "system_key": system_key,
        "system_label": system_label,
        "discipline": discipline,
        "discipline_label": discipline_label,
        "case": case,
        "algorithm_score": score,
        "algorithm_verdict": verdict,
        "artifact": str(artifact),
        "artifact_uri": artifact.as_uri(),
        "paper": str(paper),
        "source_dataset": source_dataset,
    }


def _select_cases(
    items: list[dict[str, Any]],
    *,
    groups_per_discipline: int,
    seed: int,
) -> set[tuple[str, str]]:
    grouped: dict[tuple[str, str], list[float]] = defaultdict(list)
    labels: dict[tuple[str, str], str] = {}
    for item in items:
        key = (item["discipline"], item["case"])
        if item["algorithm_score"] is not None:
            grouped[key].append(float(item["algorithm_score"]))
        labels[key] = item["discipline_label"]

    by_discipline: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for key, scores in grouped.items():
        if not scores:
            continue
        by_discipline[key[0]].append({
            "key": key,
            "mean": statistics.fmean(scores),
            "spread": max(scores) - min(scores),
            "forced": key[1] in KNOWN_SEVERE_CASES,
            "discipline_label": labels[key],
        })

    rng = random.Random(seed)
    selected: set[tuple[str, str]] = set()
    for discipline, rows in sorted(by_discipline.items()):
        if groups_per_discipline > len(rows):
            raise ValueError(f"requested {groups_per_discipline} groups for {discipline}, only {len(rows)} exist")
        chosen: list[tuple[str, str]] = []

        def add(row: dict[str, Any]) -> None:
            if row["key"] not in chosen and len(chosen) < groups_per_discipline:
                chosen.append(row["key"])

        for row in sorted((row for row in rows if row["forced"]), key=lambda row: row["key"]):
            add(row)
        by_mean = sorted(rows, key=lambda row: (row["mean"], row["key"]))
        priority = [
            max(rows, key=lambda row: (row["spread"], row["key"])),
            by_mean[0],
            by_mean[len(by_mean) // 2],
            by_mean[-1],
        ]
        for row in priority:
            add(row)
        remaining = [row for row in rows if row["key"] not in chosen]
        rng.shuffle(remaining)
        remaining.sort(key=lambda row: row["spread"], reverse=True)
        for row in remaining:
            add(row)
        selected.update(chosen)
    return selected


def _build_manifest(
    *,
    base_items: list[dict[str, Any]],
    extra_items: list[dict[str, Any]],
    selected_cases: set[tuple[str, str]],
    quality: dict[str, Any],
    seed: int,
    source_paths: dict[str, str | None],
) -> dict[str, Any]:
    selected_base = [
        item for item in base_items
        if (item["discipline"], item["case"]) in selected_cases
    ]
    all_items = selected_base + extra_items
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in all_items:
        grouped[(item["cohort"], item["discipline"], item["case"])].append(item)
    split_by_group = _assign_group_splits(grouped, seed=seed)

    rng = random.Random(seed)
    group_rows: list[dict[str, Any]] = []
    ordered_keys = sorted(grouped, key=lambda key: (key[0] != "base_sample", key[1], key[2]))
    for group_number, key in enumerate(ordered_keys, start=1):
        members = list(grouped[key])
        rng.shuffle(members)
        group_code = f"G{group_number:03d}"
        for offset, item in enumerate(members):
            item["blind_code"] = f"{group_code}-{chr(65 + offset)}"
            item["group_code"] = group_code
            item["split"] = split_by_group[key]
        group_rows.append({
            "group_code": group_code,
            "cohort": key[0],
            "split": split_by_group[key],
            "discipline": key[1],
            "discipline_label": members[0]["discipline_label"],
            "case": key[2],
            "item_ids": [item["id"] for item in members],
        })

    all_items.sort(key=lambda item: (item["group_code"], item["blind_code"]))
    digest_input = "|".join(f"{item['id']}:{item['split']}" for item in all_items)
    digest = hashlib.sha256(f"{seed}|{digest_input}".encode()).hexdigest()[:16]
    known_anchor_labels = _known_anchor_labels(all_items)
    split_group_counts = Counter(group["split"] for group in group_rows)
    split_item_counts = Counter(item["split"] for item in all_items)
    return {
        "version": 2,
        "manifest_id": f"poster-eval-calibration-{digest}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seed": seed,
        "source_paths": source_paths,
        "data_quality": quality,
        "coverage": {
            "base_population_items": len(base_items),
            "base_population_systems": len({item["system_key"] for item in base_items}),
            "base_population_papers": len({(item["discipline"], item["case"]) for item in base_items}),
            "base_population_disciplines": len({item["discipline"] for item in base_items}),
            "selected_base_papers": len(selected_cases),
            "base_sample_items": len(selected_base),
            "targeted_items": len(extra_items),
            "known_severe_anchors": len(known_anchor_labels),
            "review_items": len(all_items),
            "review_groups": len(group_rows),
            "development_groups": split_group_counts["development"],
            "development_items": split_item_counts["development"],
            "holdout_groups": split_group_counts["holdout"],
            "holdout_items": split_item_counts["holdout"],
            "diagnostic_groups": split_group_counts["diagnostic"],
            "diagnostic_items": split_item_counts["diagnostic"],
        },
        "label_schema": {
            "tiers": [
                {"id": "severe", "label": "严重失败", "score_range": [0, 30]},
                {"id": "fail", "label": "失败", "score_range": [31, 49]},
                {"id": "revise", "label": "需修改", "score_range": [50, 69]},
                {"id": "pass", "label": "通过", "score_range": [70, 100]},
            ],
            "failure_taxonomy": FAILURE_TAXONOMY,
        },
        "groups": group_rows,
        "items": all_items,
    }


def _assign_group_splits(
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]],
    *,
    seed: int,
) -> dict[tuple[str, str, str], str]:
    splits: dict[tuple[str, str, str], str] = {}
    eligible_by_discipline: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for key in grouped:
        cohort, discipline, case = key
        if cohort != "base_sample" or case in KNOWN_SEVERE_CASES:
            splits[key] = "diagnostic"
        else:
            eligible_by_discipline[discipline].append(key)

    for discipline, keys in sorted(eligible_by_discipline.items()):
        ordered = sorted(keys)
        random.Random(f"{seed}:{discipline}").shuffle(ordered)
        holdout_count = (
            0
            if len(ordered) < 2
            else min(len(ordered) - 1, max(1, math.ceil(len(ordered) / 3)))
        )
        holdout = set(ordered[:holdout_count])
        for key in ordered:
            splits[key] = "holdout" if key in holdout else "development"
    return splits


def _known_anchor_labels(items: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        item["id"]: {
            "tier": "severe",
            "human_score": KNOWN_SEVERE_SCORES[item["case"]],
            "failures": ["paper_body_screenshot", "wrong_large_crop", "unreadable_microtext"],
            "notes": "用户确认的 Direct CC 正文截图失败锚点。",
            "source": "user_confirmed",
        }
        for item in items
        if item["system_key"] == "direct_cc_deepseek_v4_pro"
        and item["case"] in KNOWN_SEVERE_CASES
    }


def _blind_review_html(manifest: dict[str, Any]) -> str:
    payload = json.dumps(manifest, ensure_ascii=False).replace("</", "<\\/")
    template = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>海报评分器盲评</title>
  <style>
    :root { --bg:#f4f5f6; --surface:#fff; --surface2:#eef0f2; --ink:#17191c; --muted:#626a73; --line:#d9dde1; --accent:#176b5b; --danger:#a63c2f; --warn:#986500; --focus:#185fa5; }
    @media (prefers-color-scheme:dark) { :root { --bg:#151719; --surface:#202327; --surface2:#2a2e33; --ink:#f4f5f6; --muted:#aab0b7; --line:#3b4148; --accent:#62c7ad; --danger:#ff9b8f; --warn:#f0c36b; --focus:#78b8f3; } }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.45 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }
    button,input,select,textarea { font:inherit; }
    button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible { outline:2px solid var(--focus); outline-offset:2px; }
    .topbar { position:sticky; top:0; z-index:20; display:flex; flex-wrap:wrap; gap:10px; align-items:center; min-height:54px; padding:9px 18px; border-bottom:1px solid var(--line); background:color-mix(in srgb,var(--surface) 94%,transparent); backdrop-filter:blur(14px); }
    .title { margin-right:auto; font-size:16px; font-weight:700; }
    .progress { min-width:120px; color:var(--muted); font-variant-numeric:tabular-nums; }
    .toolbar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
    .toolbar button,.toolbar select,.file-label { min-height:34px; padding:6px 10px; border:1px solid var(--line); border-radius:6px; background:var(--surface); color:var(--ink); cursor:pointer; }
    .file-label input { display:none; }
    main { width:min(1800px,calc(100% - 28px)); margin:18px auto 80px; }
    .group { margin:0 0 24px; padding:0 0 24px; border-bottom:1px solid var(--line); }
    .group-head { display:grid; grid-template-columns:88px minmax(0,1fr) auto; gap:12px; align-items:start; margin:0 0 10px; }
    .group-code { color:var(--accent); font-weight:800; }
    .case { min-width:0; font-size:15px; font-weight:700; overflow-wrap:anywhere; }
    .group-meta { color:var(--muted); font-size:12px; text-align:right; }
    .grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }
    .poster { overflow:hidden; border:1px solid var(--line); border-radius:8px; background:var(--surface); }
    .poster img { display:block; width:100%; aspect-ratio:2/1; object-fit:contain; background:#fff; border-bottom:1px solid var(--line); }
    .poster-body { padding:10px; }
    .poster-head { display:flex; align-items:center; gap:8px; min-height:30px; }
    .blind-code { font-weight:800; }
    .hidden-meta { display:none; margin-left:auto; color:var(--muted); font-size:12px; text-align:right; }
    body.reveal .hidden-meta { display:block; }
    .tier { display:grid; grid-template-columns:repeat(4,1fr); margin:8px 0; border:1px solid var(--line); border-radius:6px; overflow:hidden; }
    .tier button { min-height:34px; border:0; border-right:1px solid var(--line); background:var(--surface2); color:var(--ink); cursor:pointer; }
    .tier button:last-child { border-right:0; }
    .tier button.active { background:var(--accent); color:var(--surface); font-weight:700; }
    .score-row { display:grid; grid-template-columns:92px 1fr; gap:8px; align-items:start; }
    .score-row input { width:100%; min-height:34px; padding:6px 8px; border:1px solid var(--line); border-radius:5px; background:var(--surface); color:var(--ink); }
    .failures { display:flex; flex-wrap:wrap; gap:5px 10px; padding:6px 0; }
    .failures label { display:flex; gap:5px; align-items:center; color:var(--muted); font-size:12px; }
    textarea { width:100%; min-height:52px; resize:vertical; padding:7px 8px; border:1px solid var(--line); border-radius:5px; background:var(--surface); color:var(--ink); }
    .group.complete .group-code::after { content:" 已完成"; color:var(--accent); font-size:11px; font-weight:600; }
    .empty { padding:64px 20px; color:var(--muted); text-align:center; }
    @media (max-width:900px) { .grid { grid-template-columns:1fr; } .group-head { grid-template-columns:68px minmax(0,1fr); } .group-meta { grid-column:2; text-align:left; } }
    @media (max-width:560px) { main { width:100%; margin-top:10px; } .group { padding:0 10px 20px; } .topbar { padding:8px 10px; } .score-row { grid-template-columns:76px 1fr; } }
  </style>
</head>
<body>
  <header class="topbar">
    <div class="title">海报评分器盲评</div>
    <div class="progress" id="progress"></div>
    <div class="toolbar">
      <select id="cohortFilter" aria-label="样本类型"><option value="all">全部样本</option><option value="base_sample">分层样本</option><option value="targeted">定向样本</option></select>
      <select id="statusFilter" aria-label="标注状态"><option value="all">全部状态</option><option value="pending">待标注</option><option value="complete">已完成</option></select>
      <button type="button" id="revealBtn">显示模型与算法分数</button>
      <label class="file-label">导入标注<input id="importInput" type="file" accept="application/json"></label>
      <button type="button" id="exportBtn">导出 JSON</button>
    </div>
  </header>
  <main id="app"></main>
  <script>
    const manifest = __MANIFEST__;
    const storageKey = `poster-eval-labels:${manifest.manifest_id}`;
    const defaults = { severe:25, fail:43, revise:60, pass:78 };
    const tiers = manifest.label_schema.tiers;
    const failureTaxonomy = manifest.label_schema.failure_taxonomy;
    const itemsById = Object.fromEntries(manifest.items.map(item => [item.id,item]));
    let state = loadState();

    function tierForScore(value) {
      const score=Number(value);
      if(!Number.isFinite(score)) return '';
      const match=tiers.find(tier => score>=tier.score_range[0] && score<=tier.score_range[1]);
      return match ? match.id : '';
    }
    function normalizeState(raw) {
      const next=raw || {version:1,manifest_id:manifest.manifest_id,labels:{}};
      next.labels=next.labels || {};
      Object.values(next.labels).forEach(label => {
        const derived=tierForScore(label.human_score);
        if(derived) label.tier=derived;
      });
      return next;
    }
    function loadState() {
      try { return normalizeState(JSON.parse(localStorage.getItem(storageKey))); }
      catch (_) { return normalizeState(null); }
    }
    function saveState() { localStorage.setItem(storageKey,JSON.stringify(state)); updateProgress(); }
    function esc(value) { return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])); }
    function labelFor(id) { return state.labels[id] || {tier:'',human_score:'',failures:[],notes:''}; }
    function isComplete(id) { const label=labelFor(id); return label.human_score !== '' && Number.isFinite(Number(label.human_score)); }
    function groupComplete(group) { return group.item_ids.every(isComplete); }
    function updateLabel(id, patch) {
      const current=labelFor(id);
      state.labels[id]={...current,...patch,updated_at:new Date().toISOString()};
      saveState();
    }
    function render() {
      const cohort=document.getElementById('cohortFilter').value;
      const status=document.getElementById('statusFilter').value;
      const visible=manifest.groups.filter(group => (cohort==='all'||group.cohort===cohort) && (status==='all'||(status==='complete')===groupComplete(group)));
      document.getElementById('app').innerHTML=visible.length ? visible.map(groupHtml).join('') : '<div class="empty">没有符合筛选条件的论文组</div>';
      bindEvents(); updateProgress();
    }
    function groupHtml(group) {
      const members=group.item_ids.map(id => itemsById[id]);
      return `<section class="group ${groupComplete(group)?'complete':''}" data-group="${esc(group.group_code)}">
        <div class="group-head"><div class="group-code">${esc(group.group_code)}</div><div class="case">${esc(group.case)}</div><div class="group-meta">${esc(group.discipline_label)} · ${group.cohort==='base_sample'?'分层样本':'定向样本'} · ${members.length} 张</div></div>
        <div class="grid">${members.map(itemHtml).join('')}</div></section>`;
    }
    function itemHtml(item) {
      const label=labelFor(item.id);
      const tierButtons=tiers.map(tier => `<button type="button" data-tier="${tier.id}" class="${label.tier===tier.id?'active':''}">${esc(tier.label)}</button>`).join('');
      const checks=failureTaxonomy.map(failure => `<label><input type="checkbox" data-failure="${failure.id}" ${label.failures.includes(failure.id)?'checked':''}>${esc(failure.label)}</label>`).join('');
      return `<article class="poster" data-item="${item.id}"><img src="${esc(item.artifact_uri)}" alt="${esc(item.blind_code)} 海报" loading="lazy"><div class="poster-body">
        <div class="poster-head"><span class="blind-code">${esc(item.blind_code)}</span><span class="hidden-meta">${esc(item.system_label)} · ${item.algorithm_score ?? '暂无'}</span></div>
        <div class="tier">${tierButtons}</div>
        <div class="score-row"><input data-score type="number" min="0" max="100" step="1" value="${esc(label.human_score)}" aria-label="人工分数"><div class="failures">${checks}</div></div>
        <textarea data-notes placeholder="备注" aria-label="备注">${esc(label.notes)}</textarea>
      </div></article>`;
    }
    function bindEvents() {
      document.querySelectorAll('[data-item]').forEach(card => {
        const id=card.dataset.item;
        card.querySelectorAll('[data-tier]').forEach(button => button.addEventListener('click',() => {
          const tier=button.dataset.tier;
          updateLabel(id,{tier,human_score:defaults[tier]}); render();
        }));
        card.querySelector('[data-score]').addEventListener('change',event => {
          const human_score=event.target.value===''?'':Number(event.target.value);
          updateLabel(id,{human_score,tier:tierForScore(human_score)}); render();
        });
        card.querySelectorAll('[data-failure]').forEach(input => input.addEventListener('change',() => {
          const failures=[...card.querySelectorAll('[data-failure]:checked')].map(node => node.dataset.failure);
          updateLabel(id,{failures});
        }));
        card.querySelector('[data-notes]').addEventListener('change',event => updateLabel(id,{notes:event.target.value}));
      });
    }
    function updateProgress() {
      const done=manifest.items.filter(item => isComplete(item.id)).length;
      document.getElementById('progress').textContent=`${done} / ${manifest.items.length}`;
    }
    document.getElementById('cohortFilter').addEventListener('change',render);
    document.getElementById('statusFilter').addEventListener('change',render);
    document.getElementById('revealBtn').addEventListener('click',() => document.body.classList.toggle('reveal'));
    document.getElementById('exportBtn').addEventListener('click',() => {
      const blob=new Blob([JSON.stringify(state,null,2)],{type:'application/json'});
      const link=document.createElement('a'); link.href=URL.createObjectURL(blob); link.download=`${manifest.manifest_id}-labels.json`; link.click(); URL.revokeObjectURL(link.href);
    });
    document.getElementById('importInput').addEventListener('change',async event => {
      const file=event.target.files[0]; if(!file) return;
      const incoming=JSON.parse(await file.text());
      if(incoming.manifest_id!==manifest.manifest_id) { alert('标注文件与当前清单不匹配'); return; }
      state=normalizeState(incoming); saveState(); render();
    });
    render();
  </script>
</body>
</html>'''
    return template.replace("__MANIFEST__", payload)


def _analyze_labels(manifest: dict[str, Any], labels_payload: dict[str, Any]) -> dict[str, Any]:
    if labels_payload.get("manifest_id") != manifest["manifest_id"]:
        raise ValueError("label manifest_id does not match calibration manifest")
    labels = labels_payload.get("labels") or {}
    group_by_item = {
        item_id: group["group_code"]
        for group in manifest["groups"]
        for item_id in group["item_ids"]
    }
    tier_ranges = {
        str(tier["id"]): (float(tier["score_range"][0]), float(tier["score_range"][1]))
        for tier in manifest["label_schema"]["tiers"]
    }
    rows: list[dict[str, Any]] = []
    missing_saved_tiers = 0
    tier_score_mismatches = 0
    for item in manifest["items"]:
        label = labels.get(item["id"])
        if not isinstance(label, dict):
            continue
        saved_human_tier = str(label.get("tier") or "")
        if saved_human_tier and saved_human_tier not in tier_ranges:
            raise ValueError(f"unknown human tier {saved_human_tier!r} for item {item['id']}")
        human_score = _float(label.get("human_score"))
        if human_score is None:
            if not saved_human_tier:
                continue
            human_score = float(TIER_DEFAULT_SCORE[saved_human_tier])
        human_tier = _tier_for_score(human_score, tier_ranges)
        if not saved_human_tier:
            missing_saved_tiers += 1
        elif saved_human_tier != human_tier:
            tier_score_mismatches += 1
        rows.append({
            **{key: item[key] for key in (
                "id", "group_code", "blind_code", "cohort", "split", "system_key", "system_label",
                "discipline", "discipline_label", "case", "algorithm_score", "algorithm_verdict",
                "artifact", "paper", "source_dataset",
            )},
            "group_code": group_by_item[item["id"]],
            "human_tier": human_tier,
            "saved_human_tier": saved_human_tier,
            "human_score": human_score,
            "failures": list(label.get("failures") or []),
            "notes": str(label.get("notes") or ""),
        })

    calibration_metrics = _calibration_metrics(rows)
    per_system = _segment_analysis(rows, "system_key")
    per_discipline = _segment_analysis(rows, "discipline")
    per_split = [
        {
            "split": split,
            "n": len(segment),
            **_calibration_metrics(segment),
        }
        for split, segment in sorted(_group_rows(rows, "split").items())
    ]
    failure_counts = Counter(failure for row in rows for failure in row["failures"])
    return {
        "version": 1,
        "manifest_id": manifest["manifest_id"],
        "labeled_items": len(rows),
        "total_items": len(manifest["items"]),
        "completion_rate": _rate(len(rows), len(manifest["items"])),
        "label_quality": {
            "missing_saved_tiers": missing_saved_tiers,
            "tier_score_mismatches": tier_score_mismatches,
            "tier_source": "human_score",
        },
        **calibration_metrics,
        "human_tiers": dict(Counter(row["human_tier"] for row in rows)),
        "failure_counts": dict(failure_counts),
        "per_system": per_system,
        "per_discipline": per_discipline,
        "per_split": per_split,
        "worst_inversions": sorted(
            (
                {**row, "signed_error": round(float(row["algorithm_score"]) - float(row["human_score"]), 2)}
                for row in rows if row["algorithm_score"] is not None
            ),
            key=lambda row: abs(row["signed_error"]),
            reverse=True,
        )[:30],
        "rows": rows,
    }


def _tier_for_score(
    score: float,
    tier_ranges: dict[str, tuple[float, float]],
) -> str:
    for tier, (score_min, score_max) in tier_ranges.items():
        if score_min <= score <= score_max:
            return tier
    raise ValueError(f"human score {score} is outside the supported 0-100 range")


def _calibration_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    comparable = [row for row in rows if row["algorithm_score"] is not None]
    algorithm = [float(row["algorithm_score"]) for row in comparable]
    human = [float(row["human_score"]) for row in comparable]
    severe_rows = [row for row in comparable if row["human_tier"] == "severe"]
    bad_rows = [row for row in comparable if row["human_tier"] in {"severe", "fail"}]
    pass_rows = [row for row in comparable if row["human_tier"] == "pass"]
    pairs_total = pairs_correct = 0
    ordered = sorted(comparable, key=lambda row: row["group_code"])
    for _, group_rows in itertools.groupby(ordered, key=lambda row: row["group_code"]):
        group = list(group_rows)
        for left, right in itertools.combinations(group, 2):
            human_delta = float(left["human_score"]) - float(right["human_score"])
            if abs(human_delta) < 5.0:
                continue
            algorithm_delta = float(left["algorithm_score"]) - float(right["algorithm_score"])
            pairs_total += 1
            pairs_correct += int(human_delta * algorithm_delta > 0)
    return {
        "comparable_items": len(comparable),
        "spearman": _spearman(algorithm, human),
        "mae": round(statistics.fmean(abs(a - h) for a, h in zip(algorithm, human)), 3) if algorithm else None,
        "pairwise_ordering_accuracy": _rate(pairs_correct, pairs_total),
        "pairwise_pairs": pairs_total,
        "severe_cap_recall": _rate(
            sum(float(row["algorithm_score"]) <= 30 for row in severe_rows),
            len(severe_rows),
        ),
        "bad_false_pass_rate": _rate(
            sum(float(row["algorithm_score"]) >= 70 for row in bad_rows),
            len(bad_rows),
        ),
        "pass_false_fail_rate": _rate(
            sum(float(row["algorithm_score"]) < 50 for row in pass_rows),
            len(pass_rows),
        ),
    }


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _group_rows(rows: list[dict[str, Any]], field: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row[field])].append(row)
    return grouped


def _segment_analysis(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for value, segment in sorted(_group_rows(rows, field).items()):
        comparable = [row for row in segment if row["algorithm_score"] is not None]
        out.append({
            field: value,
            "n": len(segment),
            "spearman": _spearman(
                [float(row["algorithm_score"]) for row in comparable],
                [float(row["human_score"]) for row in comparable],
            ),
            "mean_algorithm_score": round(statistics.fmean(float(row["algorithm_score"]) for row in comparable), 2) if comparable else None,
            "mean_human_score": round(statistics.fmean(float(row["human_score"]) for row in comparable), 2) if comparable else None,
            "bad_false_pass_count": sum(
                row["human_tier"] in {"severe", "fail"} and float(row["algorithm_score"] or 0) >= 70
                for row in segment
            ),
        })
    return out


def _spearman(left: list[float], right: list[float]) -> float | None:
    if len(left) < 3 or len(left) != len(right):
        return None
    return _pearson(_ranks(left), _ranks(right))


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        rank = (start + 1 + end) / 2.0
        for index in order[start:end]:
            ranks[index] = rank
        start = end
    return ranks


def _pearson(left: list[float], right: list[float]) -> float | None:
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_sum = sum((a - left_mean) ** 2 for a in left)
    right_sum = sum((b - right_mean) ** 2 for b in right)
    denominator = math.sqrt(left_sum * right_sum)
    return round(numerator / denominator, 4) if denominator else None


def _legacy_label_count(path: Path | None) -> int | None:
    if not path or not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    sets = payload.get("label_sets") if isinstance(payload, dict) else None
    if not isinstance(sets, dict):
        return None
    return max((len((value or {}).get("case_labels") or {}) for value in sets.values()), default=0)


def _write_report(
    *,
    out_dir: Path,
    manifest: dict[str, Any],
    analysis: dict[str, Any] | None,
    body_scan: dict[str, Any] | None,
    legacy_label_count: int | None,
    shell_path: Path | None,
    embed_helper: Path | None,
) -> None:
    shell_path = shell_path or _latest_plugin_file("assets/html-report-shell.html")
    embed_helper = embed_helper or _latest_plugin_file(
        "skills/build-report/scripts/embed_html_report_runtime.py"
    )
    shell_css = ""
    if shell_path and shell_path.exists():
        match = re.search(r"<style>(.*?)</style>", shell_path.read_text(encoding="utf-8"), re.S)
        shell_css = match.group(1) if match else ""
    if not shell_css:
        shell_css = "body{font-family:system-ui;margin:0;background:#f7f7f7;color:#171717}main{max-width:1000px;margin:auto;padding:32px}table{border-collapse:collapse;width:100%}th,td{padding:8px;border-bottom:1px solid #ddd;text-align:left}"

    source_name = "AutoDesign 本地基准产物"
    source_file = "benchmark_600_records.csv"

    def sourced(value: Any, suffix: str, file_name: str = source_file) -> str:
        display_value = "暂无" if value is None else str(value)
        return (
            f'<span class="source-tooltip" tabindex="0" aria-describedby="src-{suffix}">{html.escape(display_value)}'
            f'<span class="source-tooltip-content" id="src-{suffix}" role="tooltip">'
            f"来源：{html.escape(source_name)}<br>文件：{html.escape(file_name)}</span></span>"
        )

    base_items = [item for item in manifest["items"] if item["cohort"] == "base_sample"]
    verdict_rows: list[dict[str, Any]] = []
    table_rows: list[str] = []
    for system_key in sorted({item["system_key"] for item in base_items}):
        segment = [item for item in base_items if item["system_key"] == system_key]
        system_label = segment[0]["system_label"]
        counts = Counter(item["algorithm_verdict"] or "unknown" for item in segment)
        verdict_labels = {"pass": "通过", "revise": "需修改", "fail": "失败"}
        for verdict in ("pass", "revise", "fail"):
            verdict_rows.append({"system": system_label, "verdict": verdict_labels[verdict], "count": counts[verdict]})
        source_suffix = _slug(system_key)
        table_rows.append(
            f"<tr><td>{html.escape(system_label)}</td>"
            f"<td>{sourced(len(segment), source_suffix + '-n')}</td>"
            f"<td>{sourced(counts['pass'], source_suffix + '-pass')}</td>"
            f"<td>{sourced(counts['revise'], source_suffix + '-revise')}</td>"
            f"<td>{sourced(counts['fail'], source_suffix + '-fail')}</td></tr>"
        )

    coverage = manifest["coverage"]
    quality = manifest["data_quality"]
    labeled = analysis["labeled_items"] if analysis else 0
    anchor_count = coverage["known_severe_anchors"]
    scan_sentence = ""
    scan_finding = ""
    if body_scan:
        scan_counts = body_scan.get("counts") or {}
        regression = body_scan.get("known_regression") or {}
        scan_sentence = (
            f" 检测器已扫描 {sourced(scan_counts.get('items'), 'scan-items', 'paper_body_scan_summary.json')} 张海报，"
            f"其中 {sourced(scan_counts.get('severe'), 'scan-severe', 'paper_body_scan_summary.json')} 张为严重命中，"
            f"{sourced(scan_counts.get('moderate'), 'scan-moderate', 'paper_body_scan_summary.json')} 张为中度命中。"
        )
        scan_finding = (
            f"<p>已知案例回归共命中 {sourced(regression.get('matched'), 'regression-matched', 'paper_body_scan_summary.json')} / "
            f"{sourced(regression.get('total'), 'regression-total', 'paper_body_scan_summary.json')} 项。"
            "这能验证预设案例，但新增命中的海报仍需人工复核后才能估计精确率。</p>"
        )
    summary = (
        f"基准样本清单已经完整，但评分器的有效性尚未由人工标签确认。"
        f"盲评集包含 {sourced(coverage['review_items'], 'review-items', 'calibration_manifest.json')} 张海报，"
        f"分布在 {sourced(coverage['review_groups'], 'review-groups', 'calibration_manifest.json')} 个成对论文组中。"
        f"其中保留了 {sourced(anchor_count, 'anchor-items', 'known_anchor_labels.json')} 个用户确认的严重失败锚点；"
        f"更广泛的盲评海报目前已标注 {sourced(labeled, 'labeled-items', 'calibration_analysis.json' if analysis else 'labels_template.json')} 张，"
        "因此当前模型排名仍应视为暂定结果。"
        f"{scan_sentence}"
    )

    analysis_block = ""
    if analysis:
        split_rows = "".join(
            "<tr>"
            f"<td>{html.escape(SPLIT_LABELS.get(str(row['split']), str(row['split'])))}</td>"
            f"<td>{sourced(row['n'], 'split-' + _slug(str(row['split'])) + '-n', 'calibration_analysis.json')}</td>"
            f"<td>{sourced(row['spearman'], 'split-' + _slug(str(row['split'])) + '-rho', 'calibration_analysis.json')}</td>"
            f"<td>{sourced(row['pairwise_ordering_accuracy'], 'split-' + _slug(str(row['split'])) + '-pairwise', 'calibration_analysis.json')}</td>"
            f"<td>{sourced(row['severe_cap_recall'], 'split-' + _slug(str(row['split'])) + '-severe', 'calibration_analysis.json')}</td>"
            f"<td>{sourced(row['bad_false_pass_rate'], 'split-' + _slug(str(row['split'])) + '-false-pass', 'calibration_analysis.json')}</td>"
            "</tr>"
            for row in analysis["per_split"]
        )
        analysis_block = (
            f"<p><strong>已观察到的校准结果：</strong>Spearman 相关系数为 {sourced(analysis['spearman'], 'rho', 'calibration_analysis.json')}，"
            f"成对排序准确率为 {sourced(analysis['pairwise_ordering_accuracy'], 'pairwise', 'calibration_analysis.json')}，"
            f"严重失败封顶召回率为 {sourced(analysis['severe_cap_recall'], 'severe-recall', 'calibration_analysis.json')}。</p>"
            "<div class='table-scroll'><table class='compact-table'><thead><tr><th>数据划分</th><th>数量</th><th>Spearman</th>"
            "<th>成对排序准确率</th><th>严重失败封顶召回率</th><th>坏海报误通过率</th></tr></thead>"
            f"<tbody>{split_rows}</tbody></table></div>"
        )
    else:
        analysis_block = (
            "<p><strong>目前尚无完整的人工结果指标。</strong>下图只展示评分器自身的判定分布，并不代表真实海报质量，"
            "不能据此证明某个系统优于另一个系统。</p>"
        )

    technical_html = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark"><title>海报评分器校准准备报告</title>
<style>{shell_css}
.mark{{background:var(--blue-dark)}} .reading ul{{padding-left:20px}} .compact-table th,.compact-table td{{text-align:left}} .compact-table th:not(:first-child),.compact-table td:not(:first-child){{text-align:right}} .status-bad{{color:var(--warning);font-weight:700}}
</style></head><body><div class="shell"><header class="topbar"><div class="brand"><span class="mark" aria-hidden="true"></span>AutoDesign 评测</div><div class="meta">校准快照 · 2026 年 7 月 10 日</div></header>
<main data-report-audience="technical">
<article class="reading"><div class="kicker">技术验证</div><header data-contract-section="title"><h1>海报评分器校准准备报告</h1></header>
<section class="summary" data-contract-section="technical-summary"><div class="summary-label">技术摘要</div><div class="summary-body"><p>{summary}</p>{analysis_block}</div></section>
<section class="narrative" data-contract-section="key-findings"><h2>分数表已经完整，但质量排序尚未验证</h2><p>源数据包含 {sourced(quality['row_count'], 'rows')} 条评分记录，覆盖 {sourced(quality['system_count'], 'systems')} 个系统、{sourced(quality['paper_group_count'], 'papers')} 篇论文和 {sourced(coverage['base_population_disciplines'], 'disciplines')} 个学科。所有论文组均完整，但旧校准集只有 {sourced(legacy_label_count if legacy_label_count is not None else '未知', 'legacy-labels', 'poster_quality_labels.json')} 个案例级标签，无法测量同一论文内不同系统的排序颠倒。</p>{scan_finding}</section></article>
<div class="wide"><figure class="card source-figure"><div class="card-head"><h3>当前评分器判定分布</h3><p>分层盲评样本；数量来自算法输出，不是人工真实标签。</p></div><div class="chart-wrap"><div data-recharts-chart="verdict-distribution"><div class="chart-fallback" data-recharts-fallback><div class="table-scroll"><table class="compact-table"><thead><tr><th>系统</th><th>数量</th><th>通过</th><th>需修改</th><th>失败</th></tr></thead><tbody>{''.join(table_rows)}</tbody></table></div></div><div data-recharts-live aria-hidden="true"></div></div></div><figcaption class="chart-note">每篇入选论文都保留全部基础系统的成对海报，避免论文难度混入系统比较。</figcaption><button type="button" class="source-tooltip" aria-describedby="chart-source">来源<span class="source-tooltip-content" id="chart-source" role="tooltip">来源：{source_name}<br>文件：{source_file}</span></button></figure></div>
<article class="reading"><section class="narrative" data-contract-section="scope-data-and-metric-definitions"><h2>评审集控制论文难度，并补充定向失败样本</h2><p>主要样本从基础总体中选择 {sourced(coverage['selected_base_papers'], 'selected-papers', 'calibration_manifest.json')} 篇论文，每篇都包含 {sourced(coverage['base_population_systems'], 'base-systems', 'calibration_manifest.json')} 个系统的输出，共形成 {sourced(coverage['base_sample_items'], 'base-items', 'calibration_manifest.json')} 张配对海报。在论文级别，{sourced(coverage['development_groups'], 'development-groups', 'calibration_manifest.json')} 组用于开发调参，{sourced(coverage['holdout_groups'], 'holdout-groups', 'calibration_manifest.json')} 组冻结用于最终验证；已知案例以及 {sourced(coverage['targeted_items'], 'targeted-items', 'calibration_manifest.json')} 张 Direct CC 和消融实验海报只作为诊断集。人工等级对应：严重失败 {sourced('0-30', 'tier-severe', 'calibration_manifest.json')} 分、失败 {sourced('31-49', 'tier-fail', 'calibration_manifest.json')} 分、需修改 {sourced('50-69', 'tier-revise', 'calibration_manifest.json')} 分、通过 {sourced('70-100', 'tier-pass', 'calibration_manifest.json')} 分。</p>
<p class="status-bad">数据质量提示：{sourced(quality['system_label_collision_rows'], 'label-collision')} 行记录复用了多个系统共享的显示名称。清单使用 system 和 system_key 作为规范身份，并从缓存的确定性快照中恢复了 {sourced(quality['recovered_artifact_count'], 'recovered-artifacts')} 条失效的输入链接。</p></section>
<section class="narrative" data-contract-section="methodology"><h2>校准关注排序错误，而不只是平均分偏移</h2><ul><li>只有点击“显示模型与算法分数”后，评审者才会看到模型身份和评分器分数。</li><li>核心指标包括严重失败封顶召回率、坏海报误通过率、同论文内成对排序准确率、Spearman 秩相关和平均绝对误差。</li><li>失败标签覆盖论文正文截图、错误裁切、微小不可读文字、布局破损、内容稀疏、层级薄弱、来源不匹配和泛化装饰。</li><li>阈值只在开发集上调整；保留集在最终验证前不得用于调参。</li></ul></section>
<section class="narrative" data-contract-section="limitations-uncertainty-and-robustness-checks"><h2>当前主要缺口是人工标签</h2><p>盲评集完成标注前，本报告只能验证覆盖率和数据完整性，不能估计评分器的召回率、精确率或排序一致性。诊断集刻意过采样了已知失败模式，不能用于估计总体发生率。对于审美类细微判断，还需要第二位评审者标注一小批重叠样本，才能判断标签是否稳定。</p></section>
<section class="narrative" data-contract-section="recommended-next-steps"><h2>完成标注后再调整全局权重</h2><ol><li>先标注诊断集，再标注开发集。</li><li>运行分析模式，按失败类型检查最大的分数颠倒案例。</li><li>只在开发集上调整确定性上限与阈值。</li><li>最后一次性检查保留集，通过后再重新生成基准排名。</li></ol></section>
<section class="narrative" data-contract-section="further-questions"><h2>首轮标注后仍需决定的问题</h2><ul><li>所有严重视觉失败是否统一封顶 30 分，还是按失败类型设置不同上限。</li><li>文字密集的理论类海报是否需要按学科设置阈值。</li><li>对于专业审美与结构性失败，允许多大的评审者分歧。</li></ul></section></article>
</main></div><!-- DATA_ANALYTICS_HTML_REPORT_RUNTIME --></body></html>'''

    payload = {
        "charts": [{
            "id": "verdict-distribution",
            "height": 360,
            "type": "bar",
            "dataset": {
                "id": "verdict-distribution",
                "title": "当前评分器判定分布",
                "data": verdict_rows,
                "chart_spec": {
                    "id": "verdict-distribution",
                    "dataset": "verdict-distribution",
                    "title": "当前评分器判定分布",
                    "type": "bar",
                    "encodings": {
                        "x": {"field": "system", "type": "nominal"},
                        "y": {"field": "count", "label": "海报数", "type": "quantitative"},
                        "color": {"field": "verdict", "type": "nominal"},
                    },
                    "xAxisTitle": "",
                    "yAxisTitle": "海报数",
                    "valueFormat": "number",
                    "settings": {"groupMode": "stacked"},
                },
            },
        }],
    }
    shell_out = out_dir / "calibration_readiness_report_shell.html"
    payload_out = out_dir / "calibration_readiness_report_payload.json"
    report_out = out_dir / "calibration_readiness_report.html"
    shell_out.write_text(technical_html, encoding="utf-8")
    _write_json(payload_out, payload)
    if embed_helper and embed_helper.exists():
        subprocess.run([
            sys.executable,
            str(embed_helper),
            "--input", str(shell_out),
            "--payload", str(payload_out),
            "--output", str(report_out),
        ], check=True)
    else:
        report_out.write_text(
            technical_html.replace("<!-- DATA_ANALYTICS_HTML_REPORT_RUNTIME -->", ""),
            encoding="utf-8",
        )
    (out_dir / "calibration_readiness_report_zh.html").write_text(
        report_out.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    _write_json(out_dir / "calibration_report_source_notes.json", {
        "audience": "技术人员",
        "delivery_mode": "HTML",
        "sources": manifest["source_paths"],
        "body_scan_summary": "paper_body_scan_summary.json" if body_scan else None,
        "chart_map": [{
            "section": "key-findings",
            "question": "当前评分器在配对样本中对不同系统给出了怎样的判定分布？",
            "family": "比较",
            "type": "堆叠柱状图",
            "fields": ["system", "verdict", "count"],
            "caveat": "仅为算法输出，不是人工质量标签。",
        }],
        "omissions": [
            "在获得人工标签前，不展示相关性或召回率图表。"
            if not analysis else "没有省略已请求的指标。"
        ],
    })


def _latest_plugin_file(relative: str) -> Path | None:
    root = Path.home() / ".codex" / "plugins" / "cache" / "openai-curated-remote" / "data-analytics"
    matches = sorted(root.glob(f"*/{relative}"))
    return matches[-1] if matches else None


def _write_manifest_csv(path: Path, items: list[dict[str, Any]]) -> None:
    fields = [
        "id", "group_code", "blind_code", "cohort", "split", "system_key", "system_label",
        "discipline", "discipline_label", "case", "algorithm_score", "algorithm_verdict",
        "artifact", "paper", "source_dataset",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: item.get(field) for field in fields} for item in items)


def _write_analysis_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = [key for key in rows[0] if key not in {"failures"}]
    fields.append("failures")
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "failures": ";".join(row.get("failures") or [])})


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _float(value: Any) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


if __name__ == "__main__":
    raise SystemExit(main())
