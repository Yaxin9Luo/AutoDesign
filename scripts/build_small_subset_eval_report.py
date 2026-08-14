#!/usr/bin/env python3
"""Build a combined Chinese report for a multi-system poster small subset."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime
import html
import json
import math
import re
from pathlib import Path
import statistics
import sys
from typing import Any


_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from autodesign.eval_protocol import EVAL_PROTOCOL  # noqa: E402
from autodesign.evaluator.poster_rubric import DIMENSIONS as RUBRIC_DIMENSIONS  # noqa: E402


DIMENSIONS = [
    ("source_faithfulness", "来源忠实度"),
    ("paper_coverage", "论文覆盖"),
    ("information_density_and_synthesis", "信息密度与综合"),
    ("visual_evidence_use", "视觉证据"),
    ("basic_layout_integrity", "基础布局"),
    ("layout_readability", "布局可读性"),
    ("professional_aesthetics", "专业美感"),
]

DIMENSION_WEIGHTS = {dimension.id: dimension.weight for dimension in RUBRIC_DIMENSIONS}

_COMPATIBLE_LEGACY_VLM_PROVENANCE = {
    "legacy-rubric:0.1.19",
    "legacy-rubric:0.1.20",
}

_BATCH_ADJUSTMENTS = {0.0, -0.5, -1.0, -1.5}
_VERDICT_RANK = {"fail": 0, "revise": 1, "pass": 2}

CALIBRATION_SYSTEM_MAP = {
    "autodesign-cc-deepseek": "ablation_deepseek-v4-pro-tencent_company",
    "autodesign-cc-longcat": "ablation_longcat-2-0_official",
    "autodesign-cc-seed": "ablation_seed-2-1-pro_ark-official",
    "autodesign-cc-glm": "ablation_glm-5-2_company",
    "direct-cc-deepseek": "direct_cc_deepseek_v4_pro",
}

VERDICT_LABELS = {"pass": "通过", "revise": "需修改", "fail": "失败"}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    suite_dir = Path(args.suite_dir).expanduser().resolve()
    manifest = _read_json(suite_dir / "input_manifest.json")
    records = _load_records(suite_dir, manifest)
    summaries = _summarize(records, manifest["systems"])
    calibration = None
    if args.labels and args.calibration_manifest:
        calibration = _calibrate(
            records,
            _read_json(Path(args.calibration_manifest).expanduser().resolve()),
            _read_json(Path(args.labels).expanduser().resolve()),
        )

    _write_combined_csv(suite_dir / "combined_scores.csv", records)
    _write_summary_csv(suite_dir / "system_summary.csv", summaries)
    if calibration is not None:
        _write_json(suite_dir / "calibration_validation.json", calibration)
    rendered_html = _render_html(manifest, records, summaries, calibration)
    report_path = suite_dir / f"small_subset_{len(records)}_eval_table_zh.html"
    canonical_report_path = suite_dir / "small_subset_eval_table_zh.html"
    for path in (report_path, canonical_report_path):
        path.write_text(rendered_html, encoding="utf-8")
    print(json.dumps({
        "report": str(report_path),
        "canonical_report": str(canonical_report_path),
        "records": len(records),
        "systems": len(summaries),
        "eval_protocols": sorted({record["eval_protocol"] for record in records}),
        "evaluator_fingerprints": sorted({record["evaluator_fingerprint"] for record in records}),
    }, ensure_ascii=False, indent=2))
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-dir", required=True)
    parser.add_argument("--labels", help="Optional exported human labels JSON.")
    parser.add_argument("--calibration-manifest", help="Manifest paired with --labels.")
    return parser.parse_args(argv)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_records(suite_dir: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    input_items = {
        (item["system_key"], item["case"]): item
        for item in manifest["items"]
    }
    records: list[dict[str, Any]] = []
    protocols: set[str] = set()
    fingerprints: set[str] = set()
    vlm_fingerprints: set[str] = set()
    batch_style_fingerprints: set[str] = set()
    for system in manifest["systems"]:
        key = system["key"]
        summary_path = suite_dir / "systems" / key / "benchmark_summary.json"
        if not summary_path.exists():
            raise FileNotFoundError(f"missing completed benchmark summary: {summary_path}")
        benchmark = _read_json(summary_path)
        summary_protocol = str(benchmark.get("eval_protocol") or "")
        summary_fingerprint = str(benchmark.get("evaluator_fingerprint") or "")
        if summary_protocol != EVAL_PROTOCOL:
            raise ValueError(f"unsupported eval protocol in benchmark summary: {summary_path}")
        if not _is_sha256_fingerprint(summary_fingerprint):
            raise ValueError(f"invalid evaluator fingerprint in benchmark summary: {summary_path}")
        batch_style_fingerprint = str(benchmark.get("batch_style_fingerprint") or "")
        if not _is_sha256_fingerprint(batch_style_fingerprint):
            raise ValueError(f"invalid batch-style fingerprint in benchmark summary: {summary_path}")
        batch_style_fingerprints.add(batch_style_fingerprint)
        summary_judge_models = {
            str(value) for value in benchmark.get("judge_models", []) if value
        }
        summary_vlm_fingerprints = {
            str(value)
            for value in benchmark.get("vlm_prompt_fingerprints", [])
            if value
        }
        if benchmark.get("vlm_prompt_fingerprint"):
            summary_vlm_fingerprints.add(str(benchmark["vlm_prompt_fingerprint"]))
        if not summary_judge_models or not summary_vlm_fingerprints:
            raise ValueError(f"missing judge provenance in benchmark summary: {summary_path}")
        summary_source_fingerprints = {
            str(value)
            for value in benchmark.get("source_evaluator_fingerprints", [])
            if value
        }
        summary_batch_results = benchmark.get("batch_style_homogeneity")
        if not isinstance(summary_batch_results, dict) or not summary_batch_results:
            raise ValueError(f"missing batch-style result in benchmark summary: {summary_path}")
        scored = [
            row
            for row in benchmark.get("records", [])
            if row.get("status") in {"scored", "cached", "reaggregated"}
            and row.get("officially_eligible") is True
            and row.get("overall") is not None
        ]
        expected = int(system.get("items") or 0)
        if len(scored) != expected:
            raise ValueError(f"{key} has {len(scored)} publishable posters; expected {expected}")
        for row in scored:
            case = row["case"]
            input_item = input_items.get((key, case))
            if input_item is None:
                raise ValueError(f"score has no staged input manifest entry: {key}/{case}")
            report_path = Path(row["report_path"])
            if not report_path.exists():
                raise FileNotFoundError(f"missing poster quality report: {report_path}")
            report = _read_json(report_path)
            if report.get("reaggregation_status") == "degraded":
                raise ValueError(f"degraded reaggregation is not publishable: {report_path}")
            judge_model = str(report.get("judge_model") or "").strip()
            if not judge_model:
                raise ValueError(f"missing judge_model in scored report: {report_path}")
            if summary_judge_models and judge_model not in summary_judge_models:
                raise ValueError(f"report judge model disagrees with benchmark summary: {report_path}")
            protocol = str(report.get("eval_protocol") or "")
            fingerprint = str(report.get("evaluator_fingerprint") or "")
            if protocol != EVAL_PROTOCOL:
                raise ValueError(f"unsupported eval protocol in scored report: {report_path}")
            if not _is_sha256_fingerprint(fingerprint):
                raise ValueError(f"missing evaluator fingerprint in scored report: {report_path}")
            if summary_fingerprint and fingerprint != summary_fingerprint:
                raise ValueError(f"report fingerprint disagrees with benchmark summary: {report_path}")
            artifact_sha256 = str(report.get("artifact_sha256") or "")
            if not artifact_sha256 or artifact_sha256 != str(input_item["sha256"]):
                raise ValueError(f"report artifact hash disagrees with input manifest: {report_path}")
            source_fingerprint = str(report.get("source_evaluator_fingerprint") or "")
            legacy_source = str(report.get("legacy_source_rubric_version") or "")
            if source_fingerprint and not _is_sha256_fingerprint(source_fingerprint):
                raise ValueError(f"invalid source evaluator fingerprint: {report_path}")
            if (
                not source_fingerprint
                and legacy_source not in {"0.1.19", "0.1.20"}
            ):
                raise ValueError(f"missing reaggregation source provenance: {report_path}")
            if (
                source_fingerprint
                and summary_source_fingerprints
                and source_fingerprint not in summary_source_fingerprints
            ):
                raise ValueError(f"report source provenance disagrees with benchmark summary: {report_path}")
            vlm_fingerprint = str(report.get("vlm_prompt_fingerprint") or "")
            if vlm_fingerprint:
                if not (
                    _is_sha256_fingerprint(vlm_fingerprint)
                    or vlm_fingerprint.startswith("legacy-rubric:")
                ):
                    raise ValueError(f"invalid VLM prompt fingerprint: {report_path}")
                vlm_fingerprints.add(vlm_fingerprint)
            if summary_vlm_fingerprints and vlm_fingerprint not in summary_vlm_fingerprints:
                raise ValueError(f"report VLM provenance disagrees with benchmark summary: {report_path}")
            if str(row.get("batch_style_fingerprint") or "") != batch_style_fingerprint:
                raise ValueError(f"record batch-style provenance disagrees with benchmark summary: {report_path}")
            batch_result = _batch_result_for_row(row, summary_batch_results, report_path)
            _validate_batch_provenance(
                row,
                batch_result,
                batch_style_fingerprint=batch_style_fingerprint,
                report_path=report_path,
            )
            _validate_score_row(row, report, report_path)
            protocols.add(protocol)
            fingerprints.add(fingerprint)
            finding_ids = [str(item.get("id")) for item in report.get("findings", [])]
            body_severity = "none"
            if "paper-body-screenshot-catastrophic" in finding_ids:
                body_severity = "catastrophic"
            elif "paper-body-screenshot-severe" in finding_ids:
                body_severity = "severe"
            elif "paper-body-screenshot-copying" in finding_ids:
                body_severity = "moderate"
            record = {
                **row,
                "system_key": key,
                "system_label": system["label"],
                "system_type": str(
                    system.get("type")
                    or ("AutoDesign" if key.startswith("autodesign-") else "Direct CC")
                ),
                "judge_model": judge_model,
                "artifact": input_item["staged"],
                "artifact_sha256": input_item["sha256"],
                "artifact_uri": Path(input_item["staged"]).as_uri(),
                "report_uri": report_path.as_uri(),
                "eval_protocol": protocol,
                "evaluator_fingerprint": fingerprint,
                "source_evaluator_fingerprint": source_fingerprint or None,
                "legacy_source_rubric_version": legacy_source or None,
                "vlm_prompt_fingerprint": vlm_fingerprint or None,
                "batch_style_fingerprint": batch_style_fingerprint or None,
                "finding_ids": finding_ids,
                "body_screenshot_severity": body_severity,
                "gate_ceiling": report.get("gate_ceiling"),
                "gate_triggered": bool(report.get("gate_triggered")),
            }
            records.append(record)
    expected_total = sum(int(system.get("items") or 0) for system in manifest["systems"])
    if len(records) != expected_total:
        raise ValueError(f"loaded {len(records)} scored posters; expected {expected_total}")
    if len(protocols) != 1 or len(fingerprints) != 1:
        raise ValueError("mixed evaluator protocols or fingerprints are not comparable")
    if not _compatible_vlm_provenance(vlm_fingerprints):
        raise ValueError(f"mixed VLM prompt fingerprints are not comparable: {sorted(vlm_fingerprints)}")
    if len(batch_style_fingerprints) > 1:
        raise ValueError("mixed batch-style fingerprints are not comparable")
    judge_models = sorted({record["judge_model"] for record in records})
    if len(judge_models) != 1:
        raise ValueError(f"mixed judge models are not comparable: {judge_models}")
    return records


def _is_sha256_fingerprint(value: str) -> bool:
    return re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None


def _compatible_vlm_provenance(values: set[str]) -> bool:
    """Allow one current prompt plus only explicitly equivalent legacy prompts."""
    current = {value for value in values if _is_sha256_fingerprint(value)}
    legacy = values - current
    return len(current) <= 1 and legacy <= _COMPATIBLE_LEGACY_VLM_PROVENANCE


def _finite_score(value: Any, *, maximum: float, label: str, report_path: Path) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {label} in scored report: {report_path}") from exc
    if not math.isfinite(score) or not 0.0 <= score <= maximum:
        raise ValueError(f"invalid {label} in scored report: {report_path}")
    return score


def _finite_adjustment(value: Any, *, report_path: Path) -> float:
    try:
        adjustment = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid batch-style adjustment in scored report: {report_path}") from exc
    if not math.isfinite(adjustment) or adjustment not in _BATCH_ADJUSTMENTS:
        raise ValueError(f"invalid batch-style adjustment in scored report: {report_path}")
    return adjustment


def _batch_result_for_row(
    row: dict[str, Any],
    results: dict[str, Any],
    report_path: Path,
) -> dict[str, Any]:
    system = str(row.get("system") or "")
    result = results.get(system) if system else None
    if result is None and len(results) == 1:
        result = next(iter(results.values()))
    if not isinstance(result, dict):
        raise ValueError(f"missing record batch-style result: {report_path}")
    return result


def _validate_batch_provenance(
    row: dict[str, Any],
    result: dict[str, Any],
    *,
    batch_style_fingerprint: str,
    report_path: Path,
) -> None:
    expected_status = str(result.get("status") or "degraded")
    if expected_status not in {"ok", "not_applicable"}:
        raise ValueError(f"non-publishable batch-style result: {report_path}")
    expected = {
        "batch_style_status": expected_status,
        "batch_style_fingerprint": batch_style_fingerprint,
        "batch_style_judge_model": result.get("judge_model"),
        "batch_style_cache_status": result.get("cache_status"),
        "batch_style_source": result.get("source"),
    }
    if result.get("batch_style_fingerprint") != batch_style_fingerprint:
        raise ValueError(f"batch-style result fingerprint disagrees with summary: {report_path}")
    for field, value in expected.items():
        if row.get(field) != value:
            raise ValueError(f"record {field} disagrees with batch-style result: {report_path}")
    row_adjustment = _finite_adjustment(
        row.get("homogeneity_adjustment", 0.0),
        report_path=report_path,
    )
    result_adjustment = _finite_adjustment(
        result.get("adjustment_points", 0.0),
        report_path=report_path,
    )
    if row_adjustment != result_adjustment:
        raise ValueError(f"record batch-style adjustment disagrees with result: {report_path}")


def _validate_score_row(
    row: dict[str, Any],
    report: dict[str, Any],
    report_path: Path,
) -> None:
    """Verify benchmark-summary scores are derived from the referenced report."""
    report_dimensions = {
        str(item.get("id")): _finite_score(
            item.get("score_0_10"),
            maximum=10.0,
            label=f"dimension {item.get('id')}",
            report_path=report_path,
        )
        for item in report.get("dimensions", [])
        if isinstance(item, dict) and item.get("id") in DIMENSION_WEIGHTS
    }
    if set(report_dimensions) != set(DIMENSION_WEIGHTS):
        raise ValueError(f"missing dimensions in scored report: {report_path}")
    row_dimensions = row.get("dimensions")
    if not isinstance(row_dimensions, dict):
        raise ValueError(f"missing summary dimensions for scored report: {report_path}")

    batch_status = str(row.get("batch_style_status") or "not_applicable")
    if batch_status not in {"ok", "not_applicable"}:
        raise ValueError(f"non-publishable batch-style status for scored report: {report_path}")
    adjustment = 0.0
    if batch_status == "ok":
        adjustment = _finite_adjustment(
            row.get("homogeneity_adjustment", 0.0),
            report_path=report_path,
        )

    expected_dimensions = dict(report_dimensions)
    expected_dimensions["professional_aesthetics"] = max(
        0.0,
        min(10.0, expected_dimensions["professional_aesthetics"] + adjustment),
    )
    for dimension, expected in expected_dimensions.items():
        actual = _finite_score(
            row_dimensions.get(dimension),
            maximum=10.0,
            label=f"summary dimension {dimension}",
            report_path=report_path,
        )
        if not math.isclose(actual, expected, abs_tol=0.011):
            raise ValueError(f"summary dimension score disagrees with report: {report_path}")

    report_overall = _finite_score(
        report.get("overall_score_0_100"),
        maximum=100.0,
        label="overall score",
        report_path=report_path,
    )
    weighted_overall = sum(
        expected_dimensions[dimension] * weight / 10.0
        for dimension, weight in DIMENSION_WEIGHTS.items()
    )
    expected_overall = min(report_overall, weighted_overall)
    row_overall = _finite_score(
        row.get("overall"),
        maximum=100.0,
        label="summary overall score",
        report_path=report_path,
    )
    if not math.isclose(row_overall, expected_overall, abs_tol=0.051):
        raise ValueError(f"summary score disagrees with report: {report_path}")
    computed_verdict = (
        "pass"
        if expected_overall >= 70.0 and not bool(report.get("gate_triggered"))
        else "revise"
        if expected_overall >= 50.0
        else "fail"
    )
    report_verdict = str(report.get("verdict") or "").lower()
    expected_verdict = computed_verdict
    if report_verdict in _VERDICT_RANK:
        expected_verdict = min(
            (report_verdict, computed_verdict),
            key=_VERDICT_RANK.__getitem__,
        )
    if str(row.get("verdict") or "").lower() != expected_verdict:
        raise ValueError(f"summary verdict disagrees with report: {report_path}")


def _summarize(
    records: list[dict[str, Any]],
    systems: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_system: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_system[record["system_key"]].append(record)
    summaries: list[dict[str, Any]] = []
    for system in systems:
        rows = by_system[system["key"]]
        scores = [float(row["overall"]) for row in rows]
        verdicts = Counter(row["verdict"] for row in rows)
        body = Counter(row["body_screenshot_severity"] for row in rows)
        summary = {
            "system_key": system["key"],
            "system_label": system["label"],
            "system_type": rows[0]["system_type"],
            "n": len(rows),
            "mean": round(statistics.mean(scores), 2),
            "median": round(statistics.median(scores), 2),
            "minimum": round(min(scores), 2),
            "maximum": round(max(scores), 2),
            "pass": verdicts["pass"],
            "revise": verdicts["revise"],
            "fail": verdicts["fail"],
            "body_moderate": body["moderate"],
            "body_severe": body["severe"],
            "body_catastrophic": body["catastrophic"],
            "hard_gate": sum(bool(row["gate_triggered"]) for row in rows),
        }
        for dimension, _ in DIMENSIONS:
            values = [float(row["dimensions"][dimension]) for row in rows]
            summary[dimension] = round(statistics.mean(values), 2)
        summaries.append(summary)
    return summaries


def _calibrate(
    records: list[dict[str, Any]],
    manifest: dict[str, Any],
    label_export: dict[str, Any],
) -> dict[str, Any]:
    labels = label_export.get("labels", {})
    human: dict[tuple[str, str], dict[str, Any]] = {}
    for item in manifest.get("items", []):
        label = labels.get(item["id"])
        if not label or label.get("human_score") is None or item.get("blind_code") == "G001-B":
            continue
        human[(item["system_key"], item["case"])] = {
            "human_score": float(label["human_score"]),
            "blind_code": item.get("blind_code"),
        }

    rows: list[dict[str, Any]] = []
    for record in records:
        calibration_key = CALIBRATION_SYSTEM_MAP.get(record["system_key"])
        label = human.get((calibration_key, record["case"])) if calibration_key else None
        if not label:
            continue
        algorithm_score = float(record["overall"])
        human_score = label["human_score"]
        rows.append({
            "system_key": record["system_key"],
            "system_label": record["system_label"],
            "case": record["case"],
            "blind_code": label["blind_code"],
            "algorithm_score": algorithm_score,
            "human_score": human_score,
            "error": round(algorithm_score - human_score, 2),
            "absolute_error": round(abs(algorithm_score - human_score), 2),
        })
    if not rows:
        raise ValueError("the supplied labels did not match any scored records")

    def stats(items: list[dict[str, Any]]) -> dict[str, Any]:
        algorithm = [row["algorithm_score"] for row in items]
        human_scores = [row["human_score"] for row in items]
        return {
            "n": len(items),
            "algorithm_mean": round(statistics.mean(algorithm), 2),
            "human_mean": round(statistics.mean(human_scores), 2),
            "mae": round(statistics.mean(row["absolute_error"] for row in items), 2),
            "bias": round(statistics.mean(row["error"] for row in items), 2),
            "spearman": round(_correlation(_rank(algorithm), _rank(human_scores)), 3),
            "tier_accuracy": round(statistics.mean(
                _tier(row["algorithm_score"]) == _tier(row["human_score"])
                for row in items
            ), 3),
        }

    by_system: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_system[row["system_key"]].append(row)
    return {
        "excluded_known_mislabels": ["G001-B"],
        "overall": stats(rows),
        "systems": {
            key: {"system_label": items[0]["system_label"], **stats(items)}
            for key, items in by_system.items()
        },
        "largest_errors": sorted(rows, key=lambda row: row["absolute_error"], reverse=True)[:10],
        "rows": rows,
    }


def _rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        end = index + 1
        while end < len(order) and values[order[end]] == values[order[index]]:
            end += 1
        average_rank = (index + end - 1) / 2.0 + 1.0
        for position in order[index:end]:
            ranks[position] = average_rank
        index = end
    return ranks


def _correlation(left: list[float], right: list[float]) -> float:
    left_mean = statistics.mean(left)
    right_mean = statistics.mean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    denominator = math.sqrt(
        sum((x - left_mean) ** 2 for x in left)
        * sum((y - right_mean) ** 2 for y in right)
    )
    return numerator / denominator if denominator else 0.0


def _tier(score: float) -> str:
    if score < 50:
        return "fail"
    if score < 70:
        return "revise"
    return "pass"


def _write_combined_csv(path: Path, records: list[dict[str, Any]]) -> None:
    fields = [
        "system_key", "system_label", "system_type", "discipline", "discipline_label",
        "case", "overall", "diagnostic_overall", "officially_eligible", "verdict",
        "body_screenshot_severity", "gate_triggered", "gate_ceiling", "eval_protocol",
        "evaluator_fingerprint", "source_evaluator_fingerprint",
        "legacy_source_rubric_version", "vlm_prompt_fingerprint",
        "batch_style_fingerprint", "batch_style_status", "batch_style_judge_model",
        "batch_style_cache_status", "batch_style_source", "homogeneity_adjustment",
        "reaggregation_status", "judge_model", "artifact", "artifact_sha256",
        "paper_sha256", "report_path",
        *[dimension for dimension, _ in DIMENSIONS], "finding_ids",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow({
                **{field: record.get(field) for field in fields},
                **record["dimensions"],
                "finding_ids": "|".join(record["finding_ids"]),
            })


def _write_summary_csv(path: Path, summaries: list[dict[str, Any]]) -> None:
    fields = list(summaries[0])
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(summaries)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _render_html(
    manifest: dict[str, Any],
    records: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    calibration: dict[str, Any] | None,
) -> str:
    system_order = [system["key"] for system in manifest["systems"]]
    record_count = len(records)
    system_count = len(system_order)
    system_labels = {system["key"]: system["label"] for system in manifest["systems"]}
    by_case: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    case_meta: dict[str, dict[str, str]] = {}
    for record in records:
        by_case[record["case"]][record["system_key"]] = record
        case_meta[record["case"]] = {
            "discipline": record["discipline_label"],
            "case": record["case"],
        }
    cases = []
    for item in manifest["items"]:
        if item["system_key"] == system_order[0] and item["case"] not in cases:
            cases.append(item["case"])

    group_rows = []
    group_order = list(dict.fromkeys(record["system_type"] for record in records))
    for group in group_order:
        selected = [record for record in records if record["system_type"] == group]
        scores = [float(record["overall"]) for record in selected]
        group_rows.append({
            "label": group,
            "n": len(selected),
            "mean": statistics.mean(scores),
            "median": statistics.median(scores),
            "pass": sum(record["verdict"] == "pass" for record in selected),
            "revise": sum(record["verdict"] == "revise" for record in selected),
            "fail": sum(record["verdict"] == "fail" for record in selected),
        })

    summary_rows = "".join(
        f"""<tr>
          <td><strong>{html.escape(row['system_label'])}</strong><span class="sub">{row['system_type']}</span></td>
          <td class="score { _score_class(row['mean']) }">{row['mean']:.2f}</td>
          <td>{row['median']:.2f}</td><td>{row['minimum']:.2f}</td><td>{row['maximum']:.2f}</td>
          <td><span class="ok">{row['pass']}</span> / <span class="warn">{row['revise']}</span> / <span class="bad">{row['fail']}</span></td>
          <td>{row['body_moderate']} / {row['body_severe']} / {row['body_catastrophic']}</td><td>{row['hard_gate']}</td>
        </tr>"""
        for row in summaries
    )
    dimension_summary_head = "".join(f"<th>{label}</th>" for _, label in DIMENSIONS)
    dimension_summary_rows = "".join(
        "<tr><td><strong>" + html.escape(row["system_label"]) + "</strong></td>"
        + "".join(f"<td>{float(row[dimension]):.2f}</td>" for dimension, _ in DIMENSIONS)
        + "</tr>"
        for row in summaries
    )
    matrix_head = "".join(f"<th>{html.escape(system_labels[key])}</th>" for key in system_order)
    matrix_rows = []
    for case in cases:
        cells = []
        for key in system_order:
            record = by_case[case][key]
            row_id = _row_id(key, case)
            cells.append(
                f'<td><a class="matrix-score {_score_class(record["overall"])}" '
                f'href="#{row_id}">{float(record["overall"]):.2f}</a></td>'
            )
        matrix_rows.append(
            f"<tr><td><strong>{html.escape(case_meta[case]['discipline'])}</strong>"
            f"<span class=\"sub case\">{html.escape(case)}</span></td>{''.join(cells)}</tr>"
        )

    detail_rows = []
    for record in records:
        dimension_cells = "".join(
            f"<td>{float(record['dimensions'][dimension]):.2f}</td>"
            for dimension, _ in DIMENSIONS
        )
        finding_badges = _finding_badges(record)
        detail_rows.append(f"""<tr id="{_row_id(record['system_key'], record['case'])}"
            data-system="{html.escape(record['system_key'])}" data-verdict="{record['verdict']}"
            data-body="{record['body_screenshot_severity']}"
            data-search="{html.escape((record['system_label'] + ' ' + record['case']).lower())}">
          <td><a href="{record['artifact_uri']}"><img loading="lazy" src="{record['artifact_uri']}" alt="海报缩略图"></a></td>
          <td><strong>{html.escape(record['system_label'])}</strong><span class="sub">{html.escape(record['discipline_label'])}</span></td>
          <td><span class="case-name">{html.escape(record['case'])}</span>{finding_badges}</td>
          <td class="score {_score_class(record['overall'])}">{float(record['overall']):.2f}</td>
          <td>{VERDICT_LABELS.get(record['verdict'], record['verdict'])}</td>
          {dimension_cells}
          <td><a href="{record['report_uri']}">JSON</a></td>
        </tr>""")

    calibration_html = ""
    if calibration:
        overall = calibration["overall"]
        per_system = "".join(
            f"<tr><td>{html.escape(value['system_label'])}</td><td>{value['n']}</td>"
            f"<td>{value['mae']:.2f}</td><td>{value['bias']:+.2f}</td>"
            f"<td>{value['spearman']:.3f}</td><td>{value['tier_accuracy']:.1%}</td></tr>"
            for value in calibration["systems"].values()
        )
        calibration_html = f"""
        <section>
          <h2>人工标签校准验证</h2>
          <p class="note">使用 {overall['n']} 张已有人工标签验证；已按用户确认排除误标 G001-B。没有对应人工标签的 Direct 系统不参与校准统计。</p>
          <div class="metrics">
            <div><span>样本</span><strong>{overall['n']}</strong></div>
            <div><span>MAE</span><strong>{overall['mae']:.2f}</strong></div>
            <div><span>平均偏差</span><strong>{overall['bias']:+.2f}</strong></div>
            <div><span>Spearman</span><strong>{overall['spearman']:.3f}</strong></div>
          </div>
          <div class="table-wrap"><table><thead><tr><th>系统</th><th>N</th><th>MAE</th><th>偏差</th><th>Spearman</th><th>档位一致率</th></tr></thead><tbody>{per_system}</tbody></table></div>
        </section>"""

    dimension_headers = "".join(f"<th title=\"0-10 分\">{label}</th>" for _, label in DIMENSIONS)
    options = "".join(
        f'<option value="{html.escape(key)}">{html.escape(system_labels[key])}</option>'
        for key in system_order
    )
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    judge_model = records[0]["judge_model"]
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Small Subset {system_count} 系统海报评测</title>
<style>
:root{{--ink:#182126;--muted:#65727a;--line:#d9e0e3;--soft:#f5f7f7;--blue:#176b87;--green:#257a4b;--amber:#a66509;--red:#ae3030;--deep-red:#771d1d}}
*{{box-sizing:border-box}} body{{margin:0;background:#fff;color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;letter-spacing:0}}
header{{border-bottom:1px solid var(--line);padding:30px max(24px,calc((100vw - 1520px)/2)) 24px;background:#f8faf9}}
main{{max-width:1520px;margin:auto;padding:0 24px 64px}} h1{{font-size:30px;margin:0 0 6px}} h2{{font-size:20px;margin:0 0 14px}} h3{{font-size:15px;margin:22px 0 10px}} p{{margin:4px 0;color:var(--muted)}} section{{padding:28px 0;border-bottom:1px solid var(--line)}}
.metrics{{display:grid;grid-template-columns:repeat(4,minmax(130px,1fr));gap:1px;background:var(--line);border:1px solid var(--line);margin:16px 0}}
.metrics div{{background:#fff;padding:14px 16px}} .metrics span,.sub{{display:block;color:var(--muted);font-size:12px}} .metrics strong{{display:block;font-size:25px;margin-top:2px}}
.table-wrap{{overflow:auto;border:1px solid var(--line)}} table{{width:100%;border-collapse:collapse;min-width:920px}} th,td{{padding:10px 11px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}} th{{background:var(--soft);font-size:12px;position:sticky;top:0;z-index:1}} tr:last-child td{{border-bottom:0}} tbody tr:hover{{background:#f8fbfb}}
.score,.matrix-score{{font-weight:750;font-variant-numeric:tabular-nums}} .pass{{color:var(--green)}} .revise{{color:var(--amber)}} .fail{{color:var(--red)}} .catastrophic{{color:var(--deep-red)}}
.ok{{color:var(--green)}} .warn{{color:var(--amber)}} .bad{{color:var(--red)}} .matrix-score{{display:inline-block;min-width:54px;text-align:center;text-decoration:none;border-bottom:2px solid currentColor}}
.case{{max-width:390px;overflow-wrap:anywhere}} .case-name{{display:block;min-width:260px;max-width:390px;overflow-wrap:anywhere}} .note{{max-width:900px}}
.filters{{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0}} input,select{{height:36px;border:1px solid #bcc7cb;background:#fff;padding:0 10px;border-radius:4px;color:var(--ink)}} input{{min-width:260px}}
.badge{{display:inline-block;margin:6px 5px 0 0;padding:2px 5px;border:1px solid var(--line);border-radius:3px;font-size:11px;color:var(--muted)}} .badge.severe{{border-color:#d7a13c;color:#875708}} .badge.cat{{border-color:#c65a5a;color:#8c2020}}
.details img{{display:block;width:220px;height:110px;object-fit:contain;background:#eef1f1}} .details td{{white-space:nowrap}} .details td:nth-child(3){{white-space:normal}} a{{color:var(--blue)}}
.group-grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:16px}} .group-band{{border-left:4px solid var(--blue);padding:8px 14px;background:var(--soft)}} .group-band strong{{font-size:20px}}
@media(max-width:760px){{main{{padding:0 12px 40px}}header{{padding:22px 16px}}.metrics{{grid-template-columns:1fr 1fr}}.group-grid{{grid-template-columns:1fr}}.details img{{width:170px;height:85px}}}}
</style></head><body>
<header><h1>Small Subset {system_count} 系统海报评测</h1><p>{record_count} 张海报 · 10 篇论文 · PosterBench Final Eval · judge {html.escape(judge_model)}</p><p>生成时间：{generated}</p></header>
<main>
<section><h2>系统总览</h2><div class="group-grid">{''.join(f'<div class="group-band"><span>{row["label"]} · {row["n"]} 张</span><strong>{row["mean"]:.2f}</strong><p>中位数 {row["median"]:.2f} · 通过/修改/失败 {row["pass"]}/{row["revise"]}/{row["fail"]}</p></div>' for row in group_rows)}</div>
<div class="table-wrap" style="margin-top:16px"><table><thead><tr><th>系统</th><th>均分</th><th>中位数</th><th>最低</th><th>最高</th><th>通过 / 修改 / 失败</th><th>正文复制 中度 / 严重 / 灾难</th><th>硬门控</th></tr></thead><tbody>{summary_rows}</tbody></table></div>
<h3>七维均分（0–10）</h3><div class="table-wrap"><table><thead><tr><th>系统</th>{dimension_summary_head}</tr></thead><tbody>{dimension_summary_rows}</tbody></table></div></section>
{calibration_html}
<section><h2>同论文横向对照</h2><p class="note">点击分数可跳到对应海报明细。绿色 ≥70，黄色 50–69.99，红色 &lt;50。</p><div class="table-wrap"><table><thead><tr><th>论文</th>{matrix_head}</tr></thead><tbody>{''.join(matrix_rows)}</tbody></table></div></section>
<section><h2>{record_count} 张逐项明细</h2><div class="filters"><input id="search" type="search" placeholder="搜索系统或论文"><select id="system"><option value="">全部系统</option>{options}</select><select id="verdict"><option value="">全部结论</option><option value="pass">通过</option><option value="revise">需修改</option><option value="fail">失败</option></select><select id="body"><option value="">全部正文截图状态</option><option value="moderate">中度</option><option value="severe">严重</option><option value="catastrophic">灾难</option><option value="none">未命中</option></select><span id="visible" class="sub"></span></div>
<div class="table-wrap details"><table><thead><tr><th>海报</th><th>系统 / 学科</th><th>论文 / 关键门控</th><th>总分</th><th>结论</th>{dimension_headers}<th>报告</th></tr></thead><tbody id="rows">{''.join(detail_rows)}</tbody></table></div></section>
</main><script>
const controls=["search","system","verdict","body"].map(id=>document.getElementById(id));
function applyFilters(){{const q=controls[0].value.trim().toLowerCase(),system=controls[1].value,verdict=controls[2].value,body=controls[3].value;let count=0;document.querySelectorAll("#rows tr").forEach(row=>{{const show=(!q||row.dataset.search.includes(q))&&(!system||row.dataset.system===system)&&(!verdict||row.dataset.verdict===verdict)&&(!body||row.dataset.body===body);row.hidden=!show;if(show)count++;}});document.getElementById("visible").textContent=`显示 ${{count}} / {record_count}`;}}
controls.forEach(control=>control.addEventListener("input",applyFilters));applyFilters();
</script></body></html>"""


def _finding_badges(record: dict[str, Any]) -> str:
    badges: list[str] = []
    body = record["body_screenshot_severity"]
    if body == "catastrophic":
        badges.append('<span class="badge cat">正文截图：灾难</span>')
    elif body == "severe":
        badges.append('<span class="badge severe">正文截图：严重</span>')
    elif body == "moderate":
        badges.append('<span class="badge severe">正文复制：中度</span>')
    if "judge-confirmed-major-visual-failure" in record["finding_ids"]:
        badges.append('<span class="badge severe">多信号视觉失败</span>')
    elif "judge-confirmed-serious-visual-defect" in record["finding_ids"]:
        badges.append('<span class="badge">严重视觉缺陷</span>')
    if record["gate_triggered"]:
        ceiling = record.get("gate_ceiling")
        label = f"硬门控 ≤{float(ceiling):g}" if ceiling is not None else "硬门控"
        badges.append(f'<span class="badge cat">{label}</span>')
    return "".join(badges)


def _score_class(score: Any) -> str:
    value = float(score)
    if value <= 0:
        return "catastrophic"
    if value < 50:
        return "fail"
    if value < 70:
        return "revise"
    return "pass"


def _row_id(system_key: str, case: str) -> str:
    return "poster-" + re.sub(r"[^a-z0-9-]+", "-", f"{system_key}-{case}".lower()).strip("-")


if __name__ == "__main__":
    raise SystemExit(main())
