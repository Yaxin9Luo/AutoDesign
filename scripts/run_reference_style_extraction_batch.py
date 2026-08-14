#!/usr/bin/env python3
"""Run Reference Style Agent extraction only, without paper ingest or Designer."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from autodesign.agents.reference_style_agent import prepare_reference_style_contract
from autodesign.util.io import atomic_write_json, sha256_file
from autodesign.util.reference_style_audit import semantic_reference_style_issues


class _ExtractionContext:
    def __init__(self, *, settings: Any, run_dir: Path, layers_dir: Path, run_id: str):
        self.settings = settings
        self.run_dir = run_dir
        self.layers_dir = layers_dir
        self.run_id = run_id
        self.state: dict[str, Any] = {"artifact_type": "poster", "rendered_layers": {}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract and audit reusable poster style blueprints only."
    )
    parser.add_argument("--reference", action="append", default=[], metavar="PATH")
    parser.add_argument("--manifest", type=Path, help="JSONL cases: case_id, source, page (1-based).")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--command", required=True, help="Coding-harness command; prompt is sent on stdin.")
    parser.add_argument("--harness", default="codex")
    parser.add_argument("--model-hint", default="")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--jobs", type=int, default=1)
    args = parser.parse_args(argv)

    cases = _load_cases(args.reference, args.manifest)
    if not cases:
        parser.error("provide at least one --reference or --manifest case")
    out_dir = args.out.expanduser().resolve()
    cases_dir = out_dir / "cases"
    cases_dir.mkdir(parents=True, exist_ok=True)
    normalized = [_normalize_case(case) for case in cases]
    case_ids = [str(case["case_id"]) for case in normalized]
    duplicates = sorted({case_id for case_id in case_ids if case_ids.count(case_id) > 1})
    if duplicates:
        parser.error(f"duplicate case_id values: {duplicates}")
    atomic_write_json(out_dir / "normalized_manifest.json", {"schema_version": 1, "cases": normalized})

    common = {
        "command": args.command,
        "harness": args.harness,
        "model_hint": args.model_hint,
        "timeout_s": max(1, args.timeout),
        "cases_dir": cases_dir,
    }
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(_run_case, case, **common): case for case in normalized}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:  # each case must remain visible in the batch report
                case = futures[future]
                results.append({
                    "case_id": case["case_id"],
                    "source": case["source"],
                    "page": case["page"],
                    "status": "failed",
                    "outcome_class": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                })
    results.sort(key=lambda item: str(item.get("case_id") or ""))
    summary = {
        "schema_version": 1,
        "out_dir": str(out_dir),
        "total": len(results),
        "passed": sum(item.get("status") == "passed" for item in results),
        "failed": sum(item.get("status") != "passed" for item in results),
        "outcome_counts": {
            outcome: sum(item.get("outcome_class") == outcome for item in results)
            for outcome in ("first_attempt_passed", "repaired_passed", "failed")
        },
        "cases": results,
    }
    atomic_write_json(out_dir / "batch_summary.json", summary)
    _write_index(out_dir, summary)
    return 0 if summary["failed"] == 0 else 1


def _load_cases(references: list[str], manifest_path: Path | None) -> list[dict[str, Any]]:
    cases = [{"source": path, "page": 1} for path in references]
    if manifest_path:
        for line_number, line in enumerate(
            manifest_path.expanduser().read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_number}: {exc}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"manifest line {line_number} must be an object")
            cases.append(item)
    return cases


def _normalize_case(case: dict[str, Any]) -> dict[str, Any]:
    source = Path(str(case.get("source") or "")).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"reference poster not found: {source}")
    page = max(1, int(case.get("page") or 1))
    source_sha = sha256_file(source)
    slug = re.sub(r"[^a-z0-9]+", "-", source.stem.lower()).strip("-") or "reference"
    case_id = str(case.get("case_id") or f"{slug}-p{page}-{source_sha[:8]}")
    case_id = re.sub(r"[^A-Za-z0-9._-]+", "-", case_id).strip("-")
    if not case_id or case_id in {".", ".."}:
        raise ValueError(f"invalid case id for {source}")
    normalized = {
        "case_id": case_id,
        "source": str(source),
        "source_sha256": source_sha,
        "page": page,
    }
    if isinstance(case.get("expect"), dict):
        normalized["expect"] = case["expect"]
    return normalized


def _run_case(
    case: dict[str, Any],
    *,
    command: str,
    harness: str,
    model_hint: str,
    timeout_s: int,
    cases_dir: Path,
) -> dict[str, Any]:
    run_dir = cases_dir / str(case["case_id"])
    run_dir.mkdir(parents=True, exist_ok=True)
    layers_dir = run_dir / "layers"
    layers_dir.mkdir(exist_ok=True)
    atomic_write_json(run_dir / "case.json", case)
    settings = SimpleNamespace(
        harness_api_key=(
            os.getenv("AUTODESIGN_HARNESS_API_KEY", "").strip()
            or os.getenv("DESIGN_ANYTHING_HARNESS_API_KEY", "").strip()
            or os.getenv("HARNESS_API_KEY", "").strip()
            or None
        )
    )
    ctx = _ExtractionContext(
        settings=settings,
        run_dir=run_dir,
        layers_dir=layers_dir,
        run_id=str(case["case_id"]),
    )
    try:
        contract = prepare_reference_style_contract(
            ctx,
            Path(str(case["source"])),
            command=command,
            harness=harness,
            model_hint=model_hint,
            timeout_s=timeout_s,
            page_index=int(case["page"]) - 1,
            semantic_expectations=(case.get("expect") if isinstance(case.get("expect"), dict) else None),
            enforce_extraction_only_artifacts=True,
        )
        audit = json.loads((run_dir / "reference_style_audit.json").read_text(encoding="utf-8"))
        expectation_issues = semantic_reference_style_issues(contract, case.get("expect"))
        result = {
            **case,
            "status": (
                "passed"
                if audit.get("status") == "pass" and not expectation_issues
                else "failed"
            ),
            "style_reference_id": contract.get("style_reference_id"),
            "extraction_attempt_count": contract.get("extraction_attempt_count"),
            "region_count": audit.get("region_count"),
            "region_roles": audit.get("region_roles"),
            "audit_status": audit.get("status"),
            "audit_issues": audit.get("issues") or [],
            "expectation_issues": expectation_issues,
            "preview": "reference_style_blueprint_preview.png",
            "contract": "reference_style_contract.json",
            "audit": "reference_style_audit.json",
        }
        result["outcome_class"] = (
            "first_attempt_passed"
            if result["status"] == "passed" and int(contract.get("extraction_attempt_count") or 0) == 1
            else "repaired_passed" if result["status"] == "passed" else "failed"
        )
    except Exception as exc:
        result = {
            **case,
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "outcome_class": "failed",
        }
    atomic_write_json(run_dir / "result.json", result)
    return result


def _write_index(out_dir: Path, summary: dict[str, Any]) -> None:
    rows: list[str] = []
    for item in summary["cases"]:
        case_id = str(item.get("case_id") or "")
        rel = f"cases/{case_id}"
        preview = (
            f'<a href="{rel}/reference_style_blueprint.html"><img src="{rel}/reference_style_blueprint_preview.png" alt="{html.escape(case_id)}"></a>'
            if item.get("status") == "passed"
            else ""
        )
        details = html.escape(
            str(
                item.get("error")
                or ", ".join(issue.get("check", "") for issue in item.get("audit_issues") or [])
                or ", ".join(item.get("expectation_issues") or [])
            )
        )
        rows.append(
            "<tr>"
            f"<td>{preview}</td><td><strong>{html.escape(case_id)}</strong><br>{html.escape(str(item.get('source') or ''))}</td>"
            f"<td>{html.escape(str(item.get('outcome_class') or item.get('status') or ''))}</td>"
            f"<td>{html.escape(str(item.get('region_count') or ''))}</td>"
            f"<td>{html.escape(', '.join(item.get('region_roles') or []))}</td>"
            f"<td>{details}<br><a href=\"{rel}/reference_style_audit.json\">audit</a> · <a href=\"{rel}/reference_style_contract.json\">contract</a></td>"
            "</tr>"
        )
    document = f"""<!doctype html><html><head><meta charset="utf-8"><title>Reference Style Extraction</title>
<style>body{{font:14px Arial,sans-serif;margin:24px;color:#171717}}table{{border-collapse:collapse;width:100%}}th,td{{border-bottom:1px solid #ddd;padding:10px;text-align:left;vertical-align:top}}img{{width:360px;height:180px;object-fit:contain;background:#f5f5f5}}.summary{{margin-bottom:18px}}</style></head><body>
<h1>Reference Style Extraction</h1><p class="summary">total={summary['total']} passed={summary['passed']} failed={summary['failed']}</p>
<table><thead><tr><th>Preview</th><th>Case</th><th>Outcome</th><th>Regions</th><th>Roles</th><th>Audit</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
</body></html>"""
    temp = out_dir / f".index.{os.getpid()}.{hashlib.sha256(document.encode()).hexdigest()[:8]}.tmp"
    temp.write_text(document, encoding="utf-8")
    os.replace(temp, out_dir / "index.html")


if __name__ == "__main__":
    raise SystemExit(main())
