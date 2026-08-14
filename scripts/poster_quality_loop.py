"""Report-only system improvement loop for paper poster quality.

This script runs the fixed poster eval harness, aggregates failures by system
owner, and emits a patch brief for an outer engineering agent. It never edits
the repository.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from autodesign.util.io import atomic_write_json, ensure_dirs
from autodesign.util.layer_parse import parse_html_layers


OWNER_TAXONOMY = [
    "designer_contract",
    "content_strategy",
    "visual_curation",
    "layout_storyboard",
    "typography_system",
    "deterministic_env_feedback",
    "critic_rubric",
    "renderer_export",
    "model_routing",
    "harness_reliability",
    "eval_calibration",
]
SEVERITY_RANK = {"blocker": 0, "high": 1, "medium": 2, "low": 3}
OWNER_ACCEPTANCE_FAMILIES = {
    "designer_contract": "generation_quality",
    "content_strategy": "curation",
    "visual_curation": "curation",
    "layout_storyboard": "layout",
    "typography_system": "layout",
    "deterministic_env_feedback": "reliability",
    "critic_rubric": "evaluator",
    "renderer_export": "reliability",
    "model_routing": "reliability",
    "harness_reliability": "reliability",
    "eval_calibration": "evaluator",
}
REVIEW_POSTER_ARTIFACTS = (
    "poster.html",
    "preview.png",
    "poster.pdf",
    "paper_poster_render_manifest.json",
    "paper_poster_dom_audit.json",
    "poster_plan_contract_audit.json",
    "poster_content_brief.json",
    "poster_plan_contract.json",
    "design_spec.json",
    "design_feedback.json",
    "provenance_report.json",
)
ARTIFACT_ROLE_COUNT_KEYS = (
    "final_candidate_count",
    "diagnostic_partial_count",
    "failed_no_visual_count",
)
LIKELY_FILES = {
    "designer_contract": [
        "autodesign/tools/ingest_document.py",
        "autodesign/util/poster_plan_contract.py",
        "prompts/designer.md",
    ],
    "visual_curation": [
        "autodesign/tools/ingest_document.py",
        "autodesign/util/poster_plan_contract.py",
        "skills/poster/visual_recipe/SKILL.md",
    ],
    "content_strategy": [
        "autodesign/tools/ingest_document.py",
        "autodesign/util/poster_plan_contract.py",
        "autodesign/tools/propose_design_spec.py",
        "scripts/poster_quality_eval.py",
        "prompts/designer.md",
        "skills/poster/visual_recipe/SKILL.md",
    ],
    "layout_storyboard": [
        "autodesign/tools/propose_design_spec.py",
        "prompts/designer.md",
        "skills/poster/visual_recipe/SKILL.md",
        "autodesign/util/html_artifact.py",
        "autodesign/tools/apply_design_ops.py",
    ],
    "typography_system": [
        "prompts/designer.md",
        "skills/poster/visual_recipe/SKILL.md",
        "autodesign/tools/composite.py",
    ],
    "deterministic_env_feedback": [
        "autodesign/util/design_feedback.py",
        "autodesign/tools/composite.py",
        "autodesign/quality_assets.py",
    ],
    "critic_rubric": [
        "prompts/critic_vision_poster.md",
        "autodesign/agents/critic_agent.py",
    ],
    "renderer_export": [
        "autodesign/tools/composite.py",
        "autodesign/tools/html_renderer.py",
        "autodesign/tools/html_artifact_renderer.py",
    ],
    "model_routing": [
        "autodesign/config.py",
        "autodesign/llm_backend.py",
    ],
    "harness_reliability": [
        "scripts/poster_quality_loop.py",
        "scripts/poster_quality_eval.py",
        "autodesign/tools/ingest_document.py",
    ],
    "eval_calibration": [
        "scripts/poster_quality_eval.py",
        "eval/poster_quality_sets.json",
    ],
}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    mode = _harness_mode(args.harness_mode)
    if args.max_system_iterations > 3 and not args.raphael_loop:
        raise SystemExit("--max-system-iterations is capped at 3 before human review")

    out_dir = Path(args.out_dir).expanduser().resolve()
    ensure_dirs(out_dir)

    progress_path = out_dir / "progress.json"
    progress = _read_progress(progress_path)
    start_iteration = len(progress.get("iterations") or []) + 1
    baseline_metrics = _read_json(Path(args.baseline_metrics).expanduser()) if args.baseline_metrics else None
    previous_metrics = baseline_metrics or _previous_progress_metrics(progress)
    last_iter_dir: Path | None = None

    for iteration in range(start_iteration, start_iteration + max(1, args.max_system_iterations)):
        iter_dir = out_dir / f"iteration_{iteration:02d}"
        ensure_dirs(iter_dir)
        eval_rc = _run_eval(args, iter_dir=iter_dir, harness_mode=mode)
        metrics = _read_json(iter_dir / "metrics.json") or {}
        ingest_curation = build_ingest_curation_report(metrics, iter_dir=iter_dir)
        generation_authored = build_generation_authored_report(metrics, iter_dir=iter_dir)
        metrics["ingest_curation_summary"] = ingest_curation.get("summary") or {}
        metrics["ingest_curation_report_path"] = str(iter_dir / "ingest_curation_report.json")
        metrics["ingest_curation_blocker_count"] = (ingest_curation.get("summary") or {}).get("blocker_count", 0)
        metrics["generation_authored_summary"] = generation_authored.get("summary") or {}
        metrics["generation_authored_cases"] = generation_authored.get("cases") or []
        metrics["generation_authored_report_path"] = str(iter_dir / "generation_authored_report.json")
        artifact_role_counts = _result_artifact_role_counts(metrics)
        metrics["artifact_role_counts"] = artifact_role_counts
        metrics.update(artifact_role_counts)
        generated_posters_review = _write_generated_posters_review(iter_dir, metrics)
        metrics["generated_posters_review"] = generated_posters_review
        metrics["generated_posters_review_dir"] = generated_posters_review.get("review_dir")
        metrics["generated_posters_artifact_role_counts"] = generated_posters_review.get("artifact_role_counts") or {}
        atomic_write_json(iter_dir / "metrics.json", metrics)
        rollup = build_issue_rollup(metrics, ingest_curation=ingest_curation)
        diagnosis = build_owner_diagnosis(rollup)
        previous_rollup = (
            build_issue_rollup(previous_metrics)
            if isinstance(previous_metrics, dict) and previous_metrics else None
        )
        before_after = build_before_after(
            previous_metrics,
            metrics,
            before_rollup=previous_rollup,
            after_rollup=rollup,
        )
        patch_brief = build_system_patch_brief(
            rollup,
            diagnosis,
            before_after,
            iter_dir=iter_dir,
            args=args,
            harness_mode=mode,
            eval_rc=eval_rc,
        )

        atomic_write_json(iter_dir / "issue_rollup.json", rollup)
        atomic_write_json(iter_dir / "owner_diagnosis.json", diagnosis)
        atomic_write_json(iter_dir / "ingest_curation_report.json", ingest_curation)
        atomic_write_json(iter_dir / "generation_authored_report.json", generation_authored)
        atomic_write_json(iter_dir / "before_after.json", before_after)
        (iter_dir / "before_after.md").write_text(render_before_after(before_after), encoding="utf-8")
        (iter_dir / "system_patch_brief.md").write_text(patch_brief, encoding="utf-8")
        _copy_contact_sheet(iter_dir, out_dir / f"contact_sheet_iteration_{iteration:02d}.png")
        acceptance_signal = _append_progress_entry(
            progress_path,
            progress,
            iteration=iteration,
            args=args,
            eval_rc=eval_rc,
            rollup=rollup,
            diagnosis=diagnosis,
            before_after=before_after,
            iter_dir=iter_dir,
        )
        atomic_write_json(iter_dir / "acceptance_signal.json", acceptance_signal)

        previous_metrics = metrics
        last_iter_dir = iter_dir
        # v1 is report-only: without an outer Codex patch between iterations,
        # rerunning would mostly measure sampling noise. Stop after one eval
        # unless the caller explicitly provided --repeat-without-patch. In
        # Raphael mode Codex is still the outer patch agent; this script only
        # records one evaluated loop step per invocation.
        if not args.repeat_without_patch:
            break

    summary = {
        "out_dir": str(out_dir),
        "last_iteration_dir": str(last_iter_dir) if last_iter_dir else None,
        "eval_set": args.eval_set,
        "label_set": args.label_set,
        "harness_mode": mode,
        "max_system_iterations_requested": args.max_system_iterations,
        "raphael_loop": bool(args.raphael_loop),
        "report_only": True,
        "progress_path": str(progress_path),
        "generated_posters_review_dir": str(last_iter_dir / "generated_posters") if last_iter_dir else None,
    }
    atomic_write_json(out_dir / "loop_summary.json", summary)
    print(f"loop dir:     {out_dir}")
    if last_iter_dir:
        print(f"patch brief:  {last_iter_dir / 'system_patch_brief.md'}")
        print(f"issue rollup: {last_iter_dir / 'issue_rollup.json'}")
        print(f"before/after: {last_iter_dir / 'before_after.md'}")
        print(f"review posters: {last_iter_dir / 'generated_posters'}")
        print(f"progress:     {progress_path}")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--set", dest="eval_set", default=None)
    parser.add_argument(
        "--label-set",
        default=None,
        help="Named labeled evaluator calibration set to forward to poster_quality_eval.py.",
    )
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--candidate", action="append", default=[], metavar="CASE=PATH")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--template", default=None)
    parser.add_argument("--brief", default=None)
    parser.add_argument("--skip-enhancer", action="store_true")
    parser.add_argument("--no-claim-graph", action="store_true")
    parser.add_argument(
        "--generate-workers",
        type=int,
        default=None,
        help="Forward concurrent generation worker count to poster_quality_eval.py.",
    )
    parser.add_argument(
        "--generate",
        dest="generate",
        action="store_true",
        default=None,
        help="Generate candidates before evaluating. Default for eval sets; off for label-set-only runs.",
    )
    parser.add_argument(
        "--no-generate",
        dest="generate",
        action="store_false",
        help="Evaluate references and any --candidate paths without API generation.",
    )
    parser.add_argument(
        "--allow-large-generate",
        action="store_true",
        help="Forward the high-cost batch opt-in to poster_quality_eval.py.",
    )
    parser.add_argument("--baseline-metrics", default=None)
    parser.add_argument(
        "--attempted-owner",
        default=None,
        choices=OWNER_TAXONOMY,
        help="Owner this outer Codex patch attempted to improve.",
    )
    parser.add_argument(
        "--patch-summary",
        default="No patch summary provided",
        help="Short human-readable summary of the repo patch being evaluated.",
    )
    parser.add_argument("--max-system-iterations", type=int, default=1)
    parser.add_argument(
        "--harness-mode",
        default=None,
        choices=["cheap", "standard", "quality", "dogfood"],
        help="Overrides POSTER_HARNESS_MODE for this loop run.",
    )
    parser.add_argument(
        "--repeat-without-patch",
        action="store_true",
        help="Repeat eval iterations without repo patches. Mainly for noise checks.",
    )
    parser.add_argument(
        "--raphael-loop",
        action="store_true",
        help=(
            "Mark this run as part of the Codex-driven continuous loop. This "
            "removes the human-review cap but still keeps the script report-only."
        ),
    )
    args = parser.parse_args(argv)
    if not args.eval_set and not args.label_set:
        parser.error("--set or --label-set is required")
    if args.generate is None:
        args.generate = False if args.label_set and not args.eval_set else True
    if args.out_dir is None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        run_name = args.eval_set or args.label_set or "ad-hoc"
        args.out_dir = f"out/poster_quality_loop/{run_name}/{stamp}"
    return args


def _harness_mode(override: str | None) -> str:
    return (override or os.getenv("POSTER_HARNESS_MODE") or "dogfood").strip().lower()


def _run_eval(args: argparse.Namespace, *, iter_dir: Path, harness_mode: str) -> int:
    cmd = [
        sys.executable,
        "scripts/poster_quality_eval.py",
        "--data-dir",
        args.data_dir,
        "--out-dir",
        str(iter_dir),
    ]
    if args.eval_set:
        cmd.extend(["--set", args.eval_set])
    if args.label_set:
        cmd.extend(["--label-set", args.label_set])
    for case in args.case:
        cmd.extend(["--case", case])
    for candidate in args.candidate:
        cmd.extend(["--candidate", candidate])
    if args.limit is not None:
        cmd.extend(["--limit", str(args.limit)])
    if args.template:
        cmd.extend(["--template", args.template])
    if args.brief:
        cmd.extend(["--brief", args.brief])
    if args.skip_enhancer:
        cmd.append("--skip-enhancer")
    if args.no_claim_graph:
        cmd.append("--no-claim-graph")
    if args.generate_workers is not None:
        cmd.extend(["--generate-workers", str(args.generate_workers)])
    if args.allow_large_generate:
        cmd.append("--allow-large-generate")
    if args.generate:
        cmd.append("--generate")

    env = os.environ.copy()
    env["POSTER_HARNESS_MODE"] = harness_mode
    log_path = iter_dir / "loop_eval.log"
    with log_path.open("w", encoding="utf-8") as log:
        log.write("$ " + " ".join(_shell_quote(part) for part in cmd) + "\n")
        log.write(f"# POSTER_HARNESS_MODE={harness_mode}\n\n")
        log.flush()
        proc = subprocess.run(
            cmd,
            cwd=str(_REPO_ROOT),
            env=env,
            text=True,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        log.write(f"\nexit_code={proc.returncode}\n")
    return int(proc.returncode)


def build_ingest_curation_report(metrics: dict[str, Any], *, iter_dir: Path) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    for result in metrics.get("results") or []:
        if not isinstance(result.get("candidate"), dict) or not result.get("candidate"):
            continue
        case = (result.get("case") or {}).get("slug") or "unknown"
        candidate = result.get("candidate") or {}
        generation = candidate.get("generation") or {}
        run_dir = _candidate_run_dir(candidate)
        html_path = Path(str(candidate.get("html_path") or "")) if candidate.get("html_path") else None
        log_path = Path(str(generation.get("log_path") or "")) if generation.get("log_path") else None
        case_report = _build_case_ingest_curation(
            case=case,
            run_dir=run_dir,
            html_path=html_path,
            log_path=log_path,
        )
        cases.append(case_report)

    scored = [case for case in cases if isinstance(case.get("metrics"), dict)]
    summary = _summarize_ingest_curation(scored)
    report = {
        "kind": "paper_poster_ingest_curation_report",
        "version": 1,
        "eval_set": (metrics.get("eval_set") or {}).get("id"),
        "harness_mode": metrics.get("harness_mode"),
        "summary": summary,
        "cases": cases,
    }
    report["path"] = str(iter_dir / "ingest_curation_report.json")
    return report


def _build_case_ingest_curation(
    *,
    case: str,
    run_dir: Path | None,
    html_path: Path | None,
    log_path: Path | None,
) -> dict[str, Any]:
    artifact_paths: dict[str, str] = {}
    if run_dir:
        artifact_paths["run_dir"] = str(run_dir)
    if html_path:
        artifact_paths["html"] = str(html_path)
    if log_path:
        artifact_paths["log"] = str(log_path)

    if not run_dir or not run_dir.exists():
        return {
            "case": case,
            "status": "missing_run_dir",
            "metrics": _empty_curation_metrics(),
            "findings": [{
                "id": "ingest_curation_missing_run_dir",
                "owner": "harness_reliability",
                "severity": "blocker",
                "message": "No generated run directory was available for ingest curation audit.",
            }],
            "artifact_paths": artifact_paths,
        }

    brief = _read_json(run_dir / "poster_content_brief.json")
    contract = _read_json(run_dir / "poster_plan_contract.json")
    preflight = _read_json(run_dir / "poster_contract_preflight.json")
    provenance = _read_json(run_dir / "paper_visual_provenance.json")
    storyboard = _read_json(run_dir / "paper_visual_storyboard.json")
    log_events = _read_json_log_events(log_path) if log_path else []

    sections = list((brief or {}).get("sections") or []) if isinstance(brief, dict) else []
    selected_visuals = list((contract or {}).get("selected_visuals") or []) if isinstance(contract, dict) else []
    required_roles = list((contract or {}).get("required_visual_roles") or []) if isinstance(contract, dict) else []
    selected_ids = [
        str(item.get("layer_id"))
        for item in selected_visuals
        if isinstance(item, dict) and item.get("layer_id")
    ]
    storyboard_selected = [
        str(item.get("asset_id"))
        for item in ((storyboard or {}).get("selected_assets") or [])
        if isinstance(item, dict) and item.get("asset_id")
    ]
    placed_ids = _placed_selected_visual_ids(selected_ids, html_path)
    placed_storyboard_ids = _placed_selected_visual_ids(storyboard_selected, html_path)
    final_dom_source_metrics = _final_dom_source_asset_metrics(run_dir)
    final_selected_count = _safe_int(final_dom_source_metrics.get("selected_source_asset_count"), 0)
    final_placed_count = _safe_int(final_dom_source_metrics.get("selected_source_asset_dom_placed_count"), 0)
    final_missing_count = _safe_int(final_dom_source_metrics.get("selected_source_asset_dom_missing_count"), 0)
    selected_visual_count = len(selected_ids)
    selected_visuals_placed_ratio = round(len(placed_ids) / max(1, selected_visual_count), 3)
    if final_selected_count > 0:
        selected_visual_count = final_selected_count
        selected_visuals_placed_ratio = round(final_placed_count / max(1, final_selected_count), 3)
    role_coverage = _visual_role_coverage(required_roles, selected_ids)
    brief_coverage = _brief_section_coverage(sections)
    abstract_risk = _abstract_dump_risk(sections)
    budget = _candidate_budget_metrics(log_events)
    body_window = _candidate_body_window_metrics(log_events)
    spec_recovery = _candidate_spec_recovery_metrics(log_events)
    provenance_assets = list((provenance or {}).get("assets") or []) if isinstance(provenance, dict) else []
    provenance_asset_ids = [
        str(item.get("asset_id"))
        for item in provenance_assets
        if isinstance(item, dict) and item.get("asset_id")
    ]
    metrics_payload = {
        "ingest_score": 1.0,
        "selected_visual_count": selected_visual_count,
        "selected_visuals_placed_ratio": selected_visuals_placed_ratio,
        "final_dom_selected_source_asset_count": final_selected_count,
        "final_dom_selected_source_asset_placed_count": final_placed_count,
        "final_dom_selected_source_asset_missing_count": final_missing_count,
        "provenance_asset_count": len(provenance_asset_ids),
        "provenance_missing_count": max(0, len(selected_ids) - len(set(selected_ids) & set(provenance_asset_ids))),
        "storyboard_selected_asset_count": len(storyboard_selected),
        "storyboard_selected_asset_placed_ratio": round(len(placed_storyboard_ids) / max(1, len(storyboard_selected)), 3),
        "storyboard_missing_asset_count": max(0, len(storyboard_selected) - len(placed_storyboard_ids)),
        "visual_role_coverage": role_coverage,
        "brief_section_coverage": brief_coverage,
        "abstract_dump_risk": abstract_risk,
        "candidate_budget_used": budget,
        "candidate_body_window": body_window,
        "candidate_spec_recovery": spec_recovery,
        "preflight_status": (preflight or {}).get("status") if isinstance(preflight, dict) else None,
    }

    findings = _ingest_curation_findings(
        brief=brief if isinstance(brief, dict) else None,
        contract=contract if isinstance(contract, dict) else None,
        preflight=preflight if isinstance(preflight, dict) else None,
        metrics=metrics_payload,
        required_roles=required_roles,
        sections=sections,
    )
    metrics_payload["ingest_score"] = _ingest_score(findings, metrics_payload)
    status = "pass" if not findings else "revise"
    if any(_severity(f.get("severity")) == "blocker" for f in findings):
        status = "fail"
    return {
        "case": case,
        "status": status,
        "artifact_paths": artifact_paths,
        "metrics": metrics_payload,
        "selected_visual_ids": selected_ids,
        "placed_selected_visual_ids": placed_ids,
        "storyboard_selected_asset_ids": storyboard_selected,
        "placed_storyboard_asset_ids": placed_storyboard_ids,
        "findings": findings,
    }


def _empty_curation_metrics() -> dict[str, Any]:
    return {
        "ingest_score": 0.0,
        "selected_visual_count": 0,
        "selected_visuals_placed_ratio": 0.0,
        "provenance_asset_count": 0,
        "provenance_missing_count": 0,
        "storyboard_selected_asset_count": 0,
        "storyboard_selected_asset_placed_ratio": 0.0,
        "storyboard_missing_asset_count": 0,
        "visual_role_coverage": 0.0,
        "brief_section_coverage": 0.0,
        "abstract_dump_risk": 0.0,
        "candidate_budget_used": {},
        "candidate_body_window": {},
        "candidate_spec_recovery": {},
    }


def _placed_selected_visual_ids(selected_ids: list[str], html_path: Path | None) -> list[str]:
    if not selected_ids or not html_path or not html_path.exists():
        return []
    try:
        raw = html_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        raw = ""
    layer_ids = {str(layer.get("layer_id") or "") for layer in parse_html_layers(html_path)}
    placed: list[str] = []
    for layer_id in selected_ids:
        if layer_id in layer_ids or layer_id in raw or f"img_{layer_id}" in raw:
            placed.append(layer_id)
    return placed


def _final_dom_source_asset_metrics(run_dir: Path | None) -> dict[str, int]:
    if not run_dir:
        return {}
    audit = _read_json(run_dir / "final" / "paper_poster_dom_audit.json")
    metrics = audit.get("paper_poster_dom_metrics") if isinstance(audit, dict) else None
    if not isinstance(metrics, dict):
        return {}
    selected = _safe_int(metrics.get("selected_source_asset_count"), 0)
    placed = _safe_int(metrics.get("selected_source_asset_dom_placed_count"), 0)
    missing = _safe_int(metrics.get("selected_source_asset_dom_missing_count"), max(0, selected - placed))
    source_backed = _safe_int(metrics.get("source_backed_dom_image_count"), 0)
    return {
        "selected_source_asset_count": selected,
        "selected_source_asset_dom_placed_count": placed,
        "selected_source_asset_dom_missing_count": missing,
        "source_backed_dom_image_count": source_backed,
    }


def _visual_role_coverage(required_roles: list[Any], selected_ids: list[str]) -> float:
    roles = [role for role in required_roles if isinstance(role, dict)]
    if not roles:
        return 1.0 if selected_ids else 0.0
    covered = 0
    selected = set(selected_ids)
    for role in roles:
        ids = {str(layer_id) for layer_id in (role.get("visual_ids") or []) if layer_id}
        min_count = max(1, _safe_int(role.get("min_count"), 1))
        if len(ids & selected) >= min_count:
            covered += 1
    return round(covered / max(1, len(roles)), 3)


def _brief_section_coverage(sections: list[Any]) -> float:
    required = {"problem", "method", "key_contribution", "main_evidence", "takeaway", "limitation_future"}
    present = 0
    for section in sections:
        if not isinstance(section, dict):
            continue
        if str(section.get("section_id") or "") not in required:
            continue
        bullets = [b for b in (section.get("bullets") or []) if isinstance(b, dict) and str(b.get("text") or "").strip()]
        if bullets:
            present += 1
    return round(present / len(required), 3)


def _abstract_dump_risk(sections: list[Any]) -> float:
    bullets: list[dict[str, Any]] = []
    for section in sections:
        if isinstance(section, dict):
            bullets.extend([b for b in (section.get("bullets") or []) if isinstance(b, dict)])
    if not bullets:
        return 0.0
    abstract_like = 0
    long_like = 0
    for bullet in bullets:
        source = str(bullet.get("source") or "").lower()
        text = str(bullet.get("text") or "")
        if "abstract" in source:
            abstract_like += 1
        if len(text.split()) > 45 or len(text) > 260:
            long_like += 1
    return round((abstract_like + long_like * 0.5) / max(1, len(bullets)), 3)


def _candidate_budget_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for event in events:
        name = str(event.get("event") or "")
        if name == "ingest.pdf.caption_match.budget":
            out["caption_match"] = _budget_event(event)
        elif name == "ingest.pdf.table_parse.budget":
            out["table_parse"] = _budget_event(event)
        elif name == "ingest.pdf.caption_match.start" and "caption_match" not in out:
            out["caption_match"] = {
                "original": event.get("n_candidates"),
                "kept": event.get("n_candidates"),
                "dropped": 0,
                "parallelism": event.get("parallelism"),
                "budgeted": False,
            }
    return out


def _candidate_body_window_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in events:
        if str(event.get("event") or "") != "ingest.pdf.body_window":
            continue
        total_pages = _safe_int(event.get("total_pages"), 0)
        body_pages = _safe_int(event.get("body_pages"), total_pages)
        ignored = _safe_int(event.get("ignored_reference_pages"), max(0, total_pages - body_pages))
        return {
            "total_pages": total_pages,
            "body_pages": body_pages,
            "references_start_page": event.get("references_start_page"),
            "ignored_reference_pages": ignored,
            "body_only": ignored > 0,
        }
    return {}


def _candidate_spec_recovery_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    reasons: list[str] = []
    auto_recovery = False
    for event in events:
        name = str(event.get("event") or "")
        if name == "spec.recovered":
            reason = str(event.get("reason") or "").strip()
            reasons.append(reason or "unknown")
        elif name == "run.auto_spec_recovery.done":
            auto_recovery = True
    if not reasons and not auto_recovery:
        return {}
    return {
        "count": len(reasons),
        "latest_reason": reasons[-1] if reasons else None,
        "reasons": reasons,
        "auto_spec_recovery": auto_recovery,
        "deterministic_recovery": bool(reasons),
    }


def _budget_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "original": event.get("original"),
        "kept": event.get("kept"),
        "dropped": event.get("dropped"),
        "max_candidates": event.get("max_candidates"),
        "parallelism": event.get("parallelism"),
        "budgeted": True,
    }


def _ingest_curation_findings(
    *,
    brief: dict[str, Any] | None,
    contract: dict[str, Any] | None,
    preflight: dict[str, Any] | None,
    metrics: dict[str, Any],
    required_roles: list[Any],
    sections: list[Any],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if not brief:
        findings.append(_curation_finding(
            "ingest_curation_missing_content_brief",
            "content_strategy",
            "blocker",
            "poster_content_brief.json is missing, so the planner cannot consume a compact paper poster brief.",
        ))
    if not contract:
        findings.append(_curation_finding(
            "ingest_curation_missing_plan_contract",
            "designer_contract",
            "blocker",
            "poster_plan_contract.json is missing, so selected visuals and required sections are not binding.",
        ))
    if preflight and str(preflight.get("status") or "") == "fail":
        findings.append(_curation_finding(
            "ingest_curation_contract_preflight_failed",
            "visual_curation",
            "blocker",
            "poster_contract_preflight failed before DesignSpec generation.",
        ))
    if metrics.get("selected_visual_count", 0) <= 0:
        findings.append(_curation_finding(
            "ingest_curation_no_selected_visuals",
            "visual_curation",
            "blocker",
            "No selected source visuals were available for the poster contract.",
        ))
    if metrics.get("selected_visual_count", 0) > 0 and metrics.get("provenance_asset_count", 0) <= 0:
        findings.append(_curation_finding(
            "ingest_curation_missing_visual_provenance",
            "visual_curation",
            "high",
            "Selected source visuals exist, but paper_visual_provenance.json is missing or empty.",
        ))
    if metrics.get("provenance_missing_count", 0) > 0:
        findings.append(_curation_finding(
            "ingest_curation_selected_visuals_missing_provenance",
            "visual_curation",
            "high",
            "Some selected source visuals are not backed by paper_visual_provenance assets.",
        ))
    if metrics.get("storyboard_selected_asset_count", 0) > 0 and float(metrics.get("storyboard_selected_asset_placed_ratio") or 0.0) < 0.4:
        findings.append(_curation_finding(
            "ingest_curation_storyboard_assets_not_placed",
            "layout_storyboard",
            "high",
            "Few storyboard-selected source visuals appear traceable in the final HTML candidate.",
        ))
    if float(metrics.get("visual_role_coverage") or 0.0) < 1.0:
        missing = [
            str(role.get("role") or "unknown")
            for role in required_roles
            if isinstance(role, dict)
            and not set(str(v) for v in (role.get("visual_ids") or []))  # malformed role
        ]
        msg = "Required method/evidence visual roles were not fully covered by selected visuals."
        if missing:
            msg += f" Missing role buckets: {', '.join(sorted(set(missing)))}."
        findings.append(_curation_finding(
            "ingest_curation_visual_role_coverage_low",
            "visual_curation",
            "high",
            msg,
        ))
    if float(metrics.get("brief_section_coverage") or 0.0) < 0.84:
        findings.append(_curation_finding(
            "ingest_curation_brief_section_coverage_low",
            "content_strategy",
            "high",
            "poster_content_brief is missing bullet coverage for one or more required paper poster sections.",
        ))
    if float(metrics.get("abstract_dump_risk") or 0.0) >= 0.35:
        findings.append(_curation_finding(
            "ingest_curation_abstract_dump_risk",
            "content_strategy",
            "medium",
            "poster_content_brief relies heavily on abstract-like or long bullets instead of posterized claims.",
        ))
    if metrics.get("selected_visual_count", 0) > 0 and float(metrics.get("selected_visuals_placed_ratio") or 0.0) < 0.4:
        findings.append(_curation_finding(
            "ingest_curation_selected_visuals_not_placed",
            "layout_storyboard",
            "high",
            "Few selected source visuals appear traceable in the final HTML candidate.",
        ))
    return findings


def _curation_finding(issue_id: str, owner: str, severity: str, message: str) -> dict[str, Any]:
    return {
        "id": issue_id,
        "owner": owner,
        "severity": severity,
        "message": message,
        "source": "ingest_curation",
    }


def _ingest_score(findings: list[dict[str, Any]], metrics: dict[str, Any]) -> float:
    score = 1.0
    for finding in findings:
        severity = _severity(finding.get("severity"))
        if severity == "blocker":
            score -= 0.35
        elif severity == "high":
            score -= 0.18
        elif severity == "medium":
            score -= 0.08
        else:
            score -= 0.03
    score -= max(0.0, 1.0 - float(metrics.get("visual_role_coverage") or 0.0)) * 0.12
    score -= max(0.0, 1.0 - float(metrics.get("brief_section_coverage") or 0.0)) * 0.12
    return round(max(0.0, min(1.0, score)), 3)


def _summarize_ingest_curation(cases: list[dict[str, Any]]) -> dict[str, Any]:
    if not cases:
        return {
            "case_count": 0,
            "ingest_score": None,
            "visual_role_coverage": None,
            "brief_section_coverage": None,
            "selected_visuals_placed_ratio": None,
            "provenance_asset_count": None,
            "provenance_missing_count": None,
            "storyboard_selected_asset_count": None,
            "storyboard_selected_asset_placed_ratio": None,
            "storyboard_missing_asset_count": None,
            "candidate_budget_used": {},
            "worst_case": None,
        }
    metric_rows = [case.get("metrics") or {} for case in cases]
    worst = min(cases, key=lambda case: float((case.get("metrics") or {}).get("ingest_score") or 0.0))
    budgeted = [
        ((row.get("candidate_budget_used") or {}).get("caption_match") or {})
        for row in metric_rows
        if ((row.get("candidate_budget_used") or {}).get("caption_match") or {}).get("budgeted")
    ]
    body_windows = [
        row.get("candidate_body_window") or {}
        for row in metric_rows
        if isinstance(row.get("candidate_body_window"), dict) and row.get("candidate_body_window")
    ]
    recovered = [
        row.get("candidate_spec_recovery") or {}
        for row in metric_rows
        if isinstance(row.get("candidate_spec_recovery"), dict) and row.get("candidate_spec_recovery")
    ]
    return {
        "case_count": len(cases),
        "blocker_count": sum(
            1
            for case in cases
            for finding in (case.get("findings") or [])
            if _severity(finding.get("severity")) == "blocker"
        ),
        "high_count": sum(
            1
            for case in cases
            for finding in (case.get("findings") or [])
            if _severity(finding.get("severity")) == "high"
        ),
        "ingest_score": _avg_metric(metric_rows, "ingest_score"),
        "visual_role_coverage": _avg_metric(metric_rows, "visual_role_coverage"),
        "brief_section_coverage": _avg_metric(metric_rows, "brief_section_coverage"),
        "selected_visuals_placed_ratio": _avg_metric(metric_rows, "selected_visuals_placed_ratio"),
        "provenance_asset_count": _avg_metric(metric_rows, "provenance_asset_count"),
        "provenance_missing_count": _avg_metric(metric_rows, "provenance_missing_count"),
        "storyboard_selected_asset_count": _avg_metric(metric_rows, "storyboard_selected_asset_count"),
        "storyboard_selected_asset_placed_ratio": _avg_metric(metric_rows, "storyboard_selected_asset_placed_ratio"),
        "storyboard_missing_asset_count": _avg_metric(metric_rows, "storyboard_missing_asset_count"),
        "abstract_dump_risk": _avg_metric(metric_rows, "abstract_dump_risk"),
        "candidate_budget_used": {
            "budgeted_case_count": len(budgeted),
            "max_original_candidates": max([int(b.get("original") or 0) for b in budgeted] or [0]),
            "avg_kept_candidates": _avg_raw([b.get("kept") for b in budgeted]),
        },
        "candidate_body_window": {
            "case_count": len(body_windows),
            "body_only_case_count": sum(1 for row in body_windows if row.get("body_only")),
            "avg_body_pages": _avg_raw([row.get("body_pages") for row in body_windows]),
            "avg_ignored_reference_pages": _avg_raw([row.get("ignored_reference_pages") for row in body_windows]),
        },
        "candidate_spec_recovery": {
            "case_count": len(recovered),
            "total_recovery_count": sum(_safe_int(row.get("count"), 0) for row in recovered),
            "reasons": sorted({
                str(reason)
                for row in recovered
                for reason in (row.get("reasons") or [])
                if str(reason).strip()
            }),
        },
        "worst_case": worst.get("case"),
    }


def _avg_metric(rows: list[dict[str, Any]], key: str) -> float | None:
    return _avg_raw([row.get(key) for row in rows])


def _avg_raw(values: list[Any]) -> float | None:
    nums: list[float] = []
    for value in values:
        if isinstance(value, (int, float)):
            nums.append(float(value))
    if not nums:
        return None
    return round(sum(nums) / len(nums), 3)


def _read_json_log_events(path: Path | None) -> list[dict[str, Any]]:
    if not path or not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _latest_poster_contract_audit(run_dir: Path) -> dict[str, Any] | None:
    path = _latest_poster_contract_audit_path(run_dir)
    return _read_json(path) if path else None


def _latest_poster_contract_audit_path(run_dir: Path) -> Path | None:
    candidates = sorted((run_dir / "composites").glob("iter_*/poster_plan_contract_audit.json"))
    for path in reversed(candidates):
        if path.exists():
            return path
    fallback = run_dir / "poster_plan_contract_audit.json"
    return fallback if fallback.exists() else None


def build_generation_authored_report(metrics: dict[str, Any], *, iter_dir: Path) -> dict[str, Any]:
    """Summarize authored-HTML paper poster render/DOM audit metrics."""
    cases: list[dict[str, Any]] = []
    for result in metrics.get("results") or []:
        if not isinstance(result, dict):
            continue
        case = (result.get("case") or {}).get("slug") or "unknown"
        candidate = result.get("candidate") if isinstance(result.get("candidate"), dict) else {}
        run_dir = _candidate_run_dir(candidate or {})
        if not run_dir:
            continue
        paper_poster = candidate.get("paper_poster") if isinstance(candidate.get("paper_poster"), dict) else {}
        manifest_path = Path(str(paper_poster.get("manifest_path"))).expanduser() if paper_poster.get("manifest_path") else _latest_paper_poster_render_manifest_path(run_dir)
        dom_audit_path = Path(str(paper_poster.get("dom_audit_path"))).expanduser() if paper_poster.get("dom_audit_path") else _latest_paper_poster_dom_audit_path(run_dir)
        contract_audit_path = Path(str(paper_poster.get("contract_audit_path"))).expanduser() if paper_poster.get("contract_audit_path") else _latest_poster_contract_audit_path(run_dir)
        if paper_poster:
            findings = [
                finding for finding in (
                    list(paper_poster.get("consistency_findings") or [])
                    + list(paper_poster.get("dom_findings") or [])
                )
                if isinstance(finding, dict)
            ]
            metrics_row = _compact_generation_authored_metrics_from_paper(paper_poster)
        else:
            manifest = _read_json(manifest_path) if manifest_path else None
            dom_audit = _read_json(dom_audit_path) if dom_audit_path else None
            if not isinstance(manifest, dict) and not isinstance(dom_audit, dict):
                continue
            manifest = manifest if isinstance(manifest, dict) else {}
            dom_audit = dom_audit if isinstance(dom_audit, dict) else {}
            findings = [
                finding for finding in (dom_audit.get("paper_poster_dom_findings") or [])
                if isinstance(finding, dict)
            ]
            metrics_row = _compact_generation_authored_metrics(manifest, dom_audit, findings)
        artifact_paths = _artifact_paths(result)
        cases.append({
            "case": case,
            "run_dir": str(run_dir),
            "manifest_path": str(manifest_path or ""),
            "dom_audit_path": str(dom_audit_path or ""),
            "contract_audit_path": str(contract_audit_path or ""),
            "preview_path": artifact_paths.get("preview"),
            "html_path": artifact_paths.get("html"),
            "final_html_path": paper_poster.get("final_html_path") or artifact_paths.get("html"),
            "final_preview_path": paper_poster.get("final_preview_path") or artifact_paths.get("preview"),
            "final_is_authored_html": bool(paper_poster.get("final_is_authored_html")),
            "artifact_consistency_ok": bool(paper_poster.get("artifact_consistency_ok")),
            "manifest_matches_final": bool(paper_poster.get("manifest_matches_final")),
            "preview_matches_final": bool(paper_poster.get("preview_matches_final")),
            "dom_audit_matches_final": bool(paper_poster.get("dom_audit_matches_final")),
            "stale_authored_audit_present": bool(paper_poster.get("stale_authored_audit_present")),
            "metrics": metrics_row,
            "findings": findings,
        })
    report = {
        "kind": "paper_poster_generation_authored_report",
        "version": 1,
        "eval_set": (metrics.get("eval_set") or {}).get("id"),
        "harness_mode": metrics.get("harness_mode"),
        "summary": _summarize_generation_authored_cases(cases),
        "cases": cases,
    }
    report["path"] = str(iter_dir / "generation_authored_report.json")
    return report


def _latest_paper_poster_render_manifest_path(run_dir: Path) -> Path | None:
    return _latest_composite_artifact_path(run_dir, "paper_poster_render_manifest.json")


def _latest_paper_poster_dom_audit_path(run_dir: Path) -> Path | None:
    return _latest_composite_artifact_path(run_dir, "paper_poster_dom_audit.json")


def _latest_composite_artifact_path(run_dir: Path, filename: str) -> Path | None:
    final_path = run_dir / "final" / filename
    if final_path.exists():
        return final_path
    candidates = sorted((run_dir / "composites").glob(f"iter_*/{filename}"))
    for path in reversed(candidates):
        if path.exists():
            return path
    fallback = run_dir / filename
    return fallback if fallback.exists() else None


def _write_generated_posters_review(iter_dir: Path, metrics: dict[str, Any]) -> dict[str, Any]:
    """Create a stable human-review tree for generated posters in one loop iteration."""
    review_dir = iter_dir / "generated_posters"
    if review_dir.is_symlink() or review_dir.is_file():
        review_dir.unlink()
    elif review_dir.exists():
        shutil.rmtree(review_dir)
    ensure_dirs(review_dir)

    cases: list[dict[str, Any]] = []
    used_slugs: set[str] = set()
    for result in metrics.get("results") or []:
        if not isinstance(result, dict):
            continue
        case = result.get("case") if isinstance(result.get("case"), dict) else {}
        candidate = result.get("candidate") if isinstance(result.get("candidate"), dict) else {}
        run_dir = _candidate_run_dir(candidate)
        if not run_dir:
            continue
        title = _review_poster_title(result, run_dir)
        folder_slug = _unique_review_slug(_review_title_slug(title), used_slugs)
        case_dir = review_dir / folder_slug
        ensure_dirs(case_dir)

        generation = candidate.get("generation") if isinstance(candidate.get("generation"), dict) else {}
        paper_poster = candidate.get("paper_poster") if isinstance(candidate.get("paper_poster"), dict) else {}
        comparison = result.get("comparison") if isinstance(result.get("comparison"), dict) else {}
        issues = comparison.get("issues") if isinstance(comparison.get("issues"), list) else []
        blocker_count = sum(
            1 for issue in issues
            if isinstance(issue, dict) and str(issue.get("severity") or "") == "blocker"
        )
        high_count = sum(
            1 for issue in issues
            if isinstance(issue, dict) and str(issue.get("severity") or "") == "high"
        )
        dom_p0_count = _safe_int(paper_poster.get("dom_p0_count"))
        runtime_terminal = generation.get("terminal_status")
        runtime_finalized = generation.get("finalized")
        eval_status = "fail" if blocker_count or dom_p0_count else "pass"

        copied: dict[str, str] = {}
        for filename in REVIEW_POSTER_ARTIFACTS:
            src = _latest_composite_artifact_path(run_dir, filename)
            if src and _copy_review_artifact(src, case_dir / filename):
                copied[filename] = str(case_dir / filename)

        artifact_paths = _artifact_paths(result)
        _copy_named_review_artifact(artifact_paths.get("html"), case_dir / "poster.html", copied)
        _copy_named_review_artifact(artifact_paths.get("preview"), case_dir / "preview.png", copied)
        artifact_role = _review_artifact_role(
            eval_status=eval_status,
            runtime_terminal=runtime_terminal,
            runtime_finalized=runtime_finalized,
            copied=copied,
        )
        diagnostic_only = artifact_role != "final_candidate"
        if artifact_role == "diagnostic_partial":
            _copy_alias_review_artifact(copied.get("poster.html"), case_dir / "diagnostic_poster.html", copied)
            _copy_alias_review_artifact(copied.get("preview.png"), case_dir / "diagnostic_preview.png", copied)
        _copy_named_review_artifact(generation.get("log_path"), case_dir / "generation.log", copied)
        human_review_artifacts = _human_review_artifacts(artifact_role, copied)

        original_html = (run_dir / "final" / "poster.html")
        if original_html.exists():
            _write_original_html_redirect(case_dir / "open_original.html", original_html)
            copied["open_original.html"] = str(case_dir / "open_original.html")

        metadata = {
            "case_slug": case.get("slug"),
            "case_group": case.get("group"),
            "paper_title": title,
            "folder_slug": folder_slug,
            "source_run_dir": str(run_dir),
            "source_run_id": run_dir.name,
            "template": (result.get("template") or case.get("template")),
            "status": result.get("status"),
            "score": result.get("score"),
            "runtime_terminal_status": runtime_terminal,
            "runtime_finalized": runtime_finalized,
            "eval_status": eval_status,
            "artifact_role": artifact_role,
            "valid_final": artifact_role == "final_candidate",
            "diagnostic_only": diagnostic_only,
            "dom_p0_count": dom_p0_count,
            "blocker_count": blocker_count,
            "high_count": high_count,
            "proxy_score": comparison.get("proxy_score"),
            "label_prediction": result.get("label_prediction"),
            "top_blocking_reasons": _top_blocking_reasons(
                generation=generation,
                paper_poster=paper_poster,
                issues=issues,
                artifact_role=artifact_role,
                dom_p0_count=dom_p0_count,
            ),
            "human_review_artifacts": human_review_artifacts,
            "copied_artifacts": copied,
        }
        atomic_write_json(case_dir / "review_metadata.json", metadata)
        (case_dir / "source_run_dir.txt").write_text(f"{run_dir}\n", encoding="utf-8")
        cases.append(metadata)

    artifact_role_counts = _artifact_role_counts_from_review_cases(cases)
    index = {
        "kind": "generated_posters_review",
        "version": 1,
        "iteration_dir": str(iter_dir),
        "review_dir": str(review_dir),
        "case_count": len(cases),
        "artifact_role_counts": artifact_role_counts,
        **artifact_role_counts,
        "cases": cases,
    }
    atomic_write_json(iter_dir / "generated_posters_index.json", index)
    (iter_dir / "generated_posters_index.md").write_text(
        _render_generated_posters_index(index),
        encoding="utf-8",
    )
    return index


def _review_poster_title(result: dict[str, Any], run_dir: Path) -> str:
    for filename in ("poster_content_brief.json", "poster_plan_contract.json"):
        data = _read_json(run_dir / filename)
        if isinstance(data, dict):
            title = data.get("title") or data.get("paper_title") or data.get("display_title")
            if isinstance(title, str) and title.strip():
                return title.strip()
    case = result.get("case") if isinstance(result.get("case"), dict) else {}
    title = case.get("title") or case.get("slug") or run_dir.name
    return str(title).strip() or run_dir.name


def _review_title_slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(title).lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:120].strip("-") or "untitled-paper"


def _unique_review_slug(slug: str, used: set[str]) -> str:
    candidate = slug
    counter = 2
    while candidate in used:
        candidate = f"{slug}-{counter}"
        counter += 1
    used.add(candidate)
    return candidate


def _copy_named_review_artifact(raw_path: Any, dest: Path, copied: dict[str, str]) -> None:
    if not raw_path:
        return
    src = Path(str(raw_path)).expanduser()
    if _copy_review_artifact(src, dest):
        copied[dest.name] = str(dest)


def _copy_alias_review_artifact(raw_path: Any, dest: Path, copied: dict[str, str]) -> None:
    if not raw_path:
        return
    if _copy_review_artifact(Path(str(raw_path)), dest):
        copied[dest.name] = str(dest)


def _review_artifact_role(
    *,
    eval_status: str,
    runtime_terminal: Any,
    runtime_finalized: Any,
    copied: dict[str, str],
) -> str:
    has_visual = bool(copied.get("poster.html") or copied.get("preview.png"))
    return _artifact_role_from_signals(
        eval_status=eval_status,
        runtime_terminal=runtime_terminal,
        runtime_finalized=runtime_finalized,
        has_visual=has_visual,
    )


def _artifact_role_from_signals(
    *,
    eval_status: str,
    runtime_terminal: Any,
    runtime_finalized: Any,
    has_visual: bool,
) -> str:
    if (
        eval_status == "pass"
        and runtime_terminal in {"pass", None, ""}
        and runtime_finalized is True
    ):
        return "final_candidate"
    return "diagnostic_partial" if has_visual else "failed_no_visual"


def _result_artifact_role(result: dict[str, Any]) -> str:
    candidate = result.get("candidate") if isinstance(result.get("candidate"), dict) else {}
    if not candidate:
        return ""
    generation = candidate.get("generation") if isinstance(candidate.get("generation"), dict) else {}
    paper_poster = candidate.get("paper_poster") if isinstance(candidate.get("paper_poster"), dict) else {}
    comparison = result.get("comparison") if isinstance(result.get("comparison"), dict) else {}
    issues = comparison.get("issues") if isinstance(comparison.get("issues"), list) else []
    blocker_count = sum(
        1 for issue in issues
        if isinstance(issue, dict) and _severity(issue.get("severity")) == "blocker"
    )
    eval_status = "fail" if blocker_count or _safe_int(paper_poster.get("dom_p0_count")) else "pass"
    has_visual = bool(
        candidate.get("image")
        or candidate.get("html")
        or candidate.get("preview_path")
        or candidate.get("html_path")
    )
    return _artifact_role_from_signals(
        eval_status=eval_status,
        runtime_terminal=generation.get("terminal_status"),
        runtime_finalized=generation.get("finalized"),
        has_visual=has_visual,
    )


def _empty_artifact_role_counts() -> dict[str, int]:
    return {key: 0 for key in ARTIFACT_ROLE_COUNT_KEYS}


def _artifact_role_count_key(role: str) -> str | None:
    if role == "final_candidate":
        return "final_candidate_count"
    if role == "diagnostic_partial":
        return "diagnostic_partial_count"
    if role == "failed_no_visual":
        return "failed_no_visual_count"
    return None


def _result_artifact_role_counts(metrics: dict[str, Any]) -> dict[str, int]:
    counts = _empty_artifact_role_counts()
    saw_role = False
    for result in metrics.get("results") or []:
        if not isinstance(result, dict):
            continue
        key = _artifact_role_count_key(_result_artifact_role(result))
        if key:
            counts[key] += 1
            saw_role = True
    if saw_role:
        return counts
    existing = metrics.get("artifact_role_counts") if isinstance(metrics.get("artifact_role_counts"), dict) else {}
    for key in ARTIFACT_ROLE_COUNT_KEYS:
        if key in existing or key in metrics:
            counts[key] = _safe_int(existing.get(key, metrics.get(key)))
    return counts


def _artifact_role_counts_from_review_cases(cases: list[dict[str, Any]]) -> dict[str, int]:
    counts = _empty_artifact_role_counts()
    for case in cases:
        if not isinstance(case, dict):
            continue
        key = _artifact_role_count_key(str(case.get("artifact_role") or ""))
        if key:
            counts[key] += 1
    return counts


def _human_review_artifacts(artifact_role: str, copied: dict[str, str]) -> dict[str, str]:
    html_name = "diagnostic_poster.html" if artifact_role == "diagnostic_partial" else "poster.html"
    preview_name = "diagnostic_preview.png" if artifact_role == "diagnostic_partial" else "preview.png"
    artifacts = {
        "preferred_html": html_name if copied.get(html_name) else "",
        "preferred_preview": preview_name if copied.get(preview_name) else "",
        "tooling_html_alias": "poster.html" if copied.get("poster.html") else "",
        "tooling_preview_alias": "preview.png" if copied.get("preview.png") else "",
    }
    return {key: value for key, value in artifacts.items() if value}


def _top_blocking_reasons(
    *,
    generation: dict[str, Any],
    paper_poster: dict[str, Any],
    issues: list[Any],
    artifact_role: str,
    dom_p0_count: int,
) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []

    def add(category: str, evidence: str, *, count: int = 1, severity: str = "blocker") -> None:
        if not evidence:
            return
        reasons.append({
            "category": category,
            "severity": severity,
            "count": count,
            "evidence": evidence,
        })

    terminal = str(generation.get("terminal_status") or "")
    if terminal == "timeout" or bool(generation.get("generation_timeout")):
        seconds = generation.get("generation_timeout_seconds")
        detail = f" after {seconds}s" if seconds is not None else ""
        add("timeout", f"generation terminal_status=timeout{detail}")
    if generation.get("finalized") is False:
        add("not_finalized", f"generation finalized=false, terminal_status={terminal or 'unknown'}")
    issue_dom_p0_count = _issue_match_count(issues, ("dom_p0", "dom p0", "paper_poster_dom", "dom audit"))
    if dom_p0_count > 0 or issue_dom_p0_count > 0:
        count = dom_p0_count or issue_dom_p0_count
        add("dom_p0", f"paper_poster_dom_audit reported {count} P0 issue(s)", count=count)

    gold_count = sum(
        _safe_int(paper_poster.get(key))
        for key in (
            "gold_visual_density_regression_count",
            "gold_quality_floor_regression_count",
            "gold_composite_fail_count",
        )
    )
    if gold_count <= 0:
        gold_count = _issue_match_count(issues, ("gold_floor", "gold floor", "gold_visual", "gold_quality"))
    if gold_count > 0:
        add("gold_floor", f"gold floor/composite regression count={gold_count}", count=gold_count)

    asset_not_loaded_count = _asset_not_loaded_count(paper_poster, issues)
    if asset_not_loaded_count > 0:
        add("asset_not_loaded", f"source/image asset load failure count={asset_not_loaded_count}", count=asset_not_loaded_count)
    if artifact_role == "failed_no_visual":
        add("failed_no_visual", "no copied poster.html or preview.png visual artifact was available")
    return reasons[:8]


def _issue_match_count(issues: list[Any], needles: tuple[str, ...]) -> int:
    count = 0
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        haystack = " ".join(
            str(issue.get(key) or "")
            for key in ("id", "message", "category")
        ).lower()
        if any(needle in haystack for needle in needles):
            count += 1
    return count


def _asset_not_loaded_count(paper_poster: dict[str, Any], issues: list[Any]) -> int:
    count = _safe_int(paper_poster.get("image_not_loaded_count"))
    findings = []
    for key in ("dom_findings", "consistency_findings"):
        values = paper_poster.get(key)
        if isinstance(values, list):
            findings.extend(values)
    findings.extend(issue for issue in issues if isinstance(issue, dict))
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        haystack = " ".join(
            str(finding.get(key) or "")
            for key in ("id", "message", "category")
        ).lower()
        if "not-loaded" in haystack or "not loaded" in haystack or "asset_not_loaded" in haystack:
            count += 1
    return count


def _copy_review_artifact(src: Path, dest: Path) -> bool:
    try:
        source = src.resolve() if src.is_symlink() else src
    except OSError:
        source = src
    if not source.exists() or not source.is_file():
        return False
    ensure_dirs(dest.parent)
    shutil.copy2(source, dest)
    return True


def _write_original_html_redirect(path: Path, original_html: Path) -> None:
    try:
        target = original_html.resolve().as_uri()
    except ValueError:
        return
    path.write_text(
        (
            "<!doctype html>\n"
            "<meta charset=\"utf-8\">\n"
            f"<meta http-equiv=\"refresh\" content=\"0; url={target}\">\n"
            f"<p><a href=\"{target}\">Open original poster HTML</a></p>\n"
        ),
        encoding="utf-8",
    )


def _render_generated_posters_index(index: dict[str, Any]) -> str:
    lines = [
        "# Generated Posters Review",
        "",
        f"- Iteration dir: `{index.get('iteration_dir')}`",
        f"- Review dir: `{index.get('review_dir')}`",
        f"- Cases: `{index.get('case_count')}`",
        f"- Final candidates: `{index.get('final_candidate_count') or 0}`",
        f"- Diagnostic partials: `{index.get('diagnostic_partial_count') or 0}`",
        f"- Failed/no visual: `{index.get('failed_no_visual_count') or 0}`",
        "- Diagnostic partial artifacts do not count as final pass; inspect "
        "`diagnostic_poster.html` / `diagnostic_preview.png` first when present.",
        "",
        "| Case | Eval | Runtime | Valid Final | Artifact Role | Review Artifact | DOM P0 | Blockers | Paper Title | Review Folder | Source Run |",
        "|---|---|---|---|---|---|---:|---:|---|---|---|",
    ]
    for case in index.get("cases") or []:
        if not isinstance(case, dict):
            continue
        folder = (Path(str(index.get("review_dir") or "")) / str(case.get("folder_slug") or "")).resolve()
        review_artifact = _index_review_artifact_label(case)
        lines.append(
            "| "
            f"`{case.get('case_slug') or ''}` | "
            f"`{case.get('eval_status') or ''}` | "
            f"`{case.get('runtime_terminal_status') or ''}` | "
            f"{'yes' if case.get('valid_final') else 'no'} | "
            f"`{case.get('artifact_role') or ''}` | "
            f"{review_artifact} | "
            f"{case.get('dom_p0_count') or 0} | "
            f"{case.get('blocker_count') or 0} | "
            f"{case.get('paper_title') or ''} | "
            f"`{folder}` | "
            f"`{case.get('source_run_id') or ''}` |"
        )
    lines.append("")
    lines.append(
        "Each review folder contains copied review artifacts plus `source_run_dir.txt`; "
        "`final_candidate` means the runtime finalized and passed local eval, while "
        "`diagnostic_partial` artifacts are visual/debug evidence only. Tooling aliases "
        "`poster.html` and `preview.png` are still copied when available."
    )
    return "\n".join(lines)


def _index_review_artifact_label(case: dict[str, Any]) -> str:
    artifacts = case.get("human_review_artifacts") if isinstance(case.get("human_review_artifacts"), dict) else {}
    preferred = [
        str(artifacts.get(key) or "")
        for key in ("preferred_html", "preferred_preview")
        if artifacts.get(key)
    ]
    if preferred:
        suffix = " (diagnostic only)" if case.get("artifact_role") == "diagnostic_partial" else ""
        return "`" + "` / `".join(preferred) + "`" + suffix
    if case.get("artifact_role") == "failed_no_visual":
        return "`generation.log`"
    return ""


def _compact_generation_authored_metrics(
    manifest: dict[str, Any],
    dom_audit: dict[str, Any],
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    counts = _finding_id_counts(findings)
    dom_metrics = (
        dom_audit.get("paper_poster_dom_metrics")
        if isinstance(dom_audit.get("paper_poster_dom_metrics"), dict)
        else {}
    )
    render_mode = str(manifest.get("render_mode") or "")
    dom_p0_count = int(dom_audit.get("paper_poster_dom_p0_count") or 0)
    return {
        "authored_html": render_mode == "authored_html",
        "render_mode": render_mode,
        "dom_backend": dom_audit.get("paper_poster_dom_backend"),
        "dom_backend_playwright": dom_audit.get("paper_poster_dom_backend") == "playwright",
        "dom_audit_p0_count": dom_p0_count,
        "unprotected_dom_audit_p0_count": dom_p0_count,
        "preview_fallback_count": 1 if bool(manifest.get("preview_fallback_used")) else 0,
        "image_not_loaded_count": counts.get("paper-poster-image-not-loaded", 0),
        "block_out_of_bounds_count": counts.get("paper-poster-block-out-of-bounds", 0),
        "root_overflow_count": counts.get("paper-poster-overflow", 0),
        "caption_overlap_count": counts.get("paper-poster-caption-overlap", 0),
        "footer_overlap_count": counts.get("paper-poster-footer-overlap", 0),
        "panel_underfilled_count": counts.get("paper-poster-panel-underfilled", 0),
        "page_like_source_figure_count": counts.get("paper-poster-page-screenshot-source-figure", 0),
        "leaf_visible_word_count": _dom_leaf_visible_word_count(dom_audit),
        "figure_area_ratio": dom_metrics.get("figure_area_ratio"),
        "native_information_unit_count": dom_metrics.get("native_information_unit_count"),
        "dom_native_information_unit_count": dom_metrics.get("dom_native_information_unit_count"),
        "image_count": dom_metrics.get("image_count"),
        "source_provenance_asset_count": dom_metrics.get("source_provenance_asset_count"),
        "source_backed_dom_image_count": dom_metrics.get("source_backed_dom_image_count"),
        "unbacked_source_image_count": int(dom_metrics.get("unbacked_source_image_count") or 0),
        "page_like_source_dom_image_count": int(dom_metrics.get("page_like_source_dom_image_count") or 0),
        "panel_internal_underfilled_count": int(dom_metrics.get("panel_internal_underfilled_count") or 0),
        "panel_internal_underfilled_p0_count": int(dom_metrics.get("panel_internal_underfilled_p0_count") or 0),
        "panel_internal_min_coverage": dom_metrics.get("panel_internal_min_coverage"),
        "panel_internal_max_blank_run_ratio": dom_metrics.get("panel_internal_max_blank_run_ratio"),
        "panel_visual_underfilled_count": 0,
        "panel_visual_underfilled_p0_count": 0,
        "panel_visual_min_ink_ratio": None,
        "panel_visual_min_grid_coverage": None,
        "panel_visual_max_blank_run_ratio": None,
        "selected_source_asset_count": dom_metrics.get("selected_source_asset_count"),
        "selected_source_asset_dom_missing_count": int(dom_metrics.get("selected_source_asset_dom_missing_count") or 0),
        "dom_warning_count": len(dom_audit.get("paper_poster_dom_warnings") or []),
    }


def _compact_generation_authored_metrics_from_paper(paper_poster: dict[str, Any]) -> dict[str, Any]:
    dom_p0_count = int(paper_poster.get("dom_p0_count") or 0)
    dense_local_repair_only = bool(paper_poster.get("dense_gold_local_repair_only"))
    proxy = paper_poster.get("html_proxy_metrics") if isinstance(paper_poster.get("html_proxy_metrics"), dict) else {}
    leaf_visible_words = paper_poster.get("leaf_visible_word_count")
    if leaf_visible_words is None:
        leaf_visible_words = (
            proxy.get("authored_leaf_visible_word_count")
            if proxy.get("authored_leaf_visible_word_count") is not None
            else proxy.get("leaf_visible_word_count")
        )
    native_units = (
        paper_poster.get("native_information_unit_count")
        if paper_poster.get("native_information_unit_count") is not None
        else proxy.get("authored_native_information_unit_count")
        if proxy.get("authored_native_information_unit_count") is not None
        else proxy.get("native_information_unit_count")
    )
    dom_native_units = (
        paper_poster.get("dom_native_information_unit_count")
        if paper_poster.get("dom_native_information_unit_count") is not None
        else proxy.get("dom_native_information_unit_count")
    )
    return {
        "authored_html": bool(paper_poster.get("final_is_authored_html")),
        "render_mode": str(paper_poster.get("render_mode") or ""),
        "final_is_authored_html": bool(paper_poster.get("final_is_authored_html")),
        "artifact_consistency_ok": bool(paper_poster.get("artifact_consistency_ok")),
        "manifest_matches_final": bool(paper_poster.get("manifest_matches_final")),
        "preview_matches_final": bool(paper_poster.get("preview_matches_final")),
        "dom_audit_matches_final": bool(paper_poster.get("dom_audit_matches_final")),
        "stale_authored_audit_present": bool(paper_poster.get("stale_authored_audit_present")),
        "dom_backend": paper_poster.get("dom_backend"),
        "dom_backend_playwright": paper_poster.get("dom_backend") == "playwright",
        "dom_audit_p0_count": dom_p0_count,
        "unprotected_dom_audit_p0_count": 0 if dense_local_repair_only else dom_p0_count,
        "dense_gold_local_repair_only": dense_local_repair_only,
        "gold_visual_density_pass": bool(paper_poster.get("gold_visual_density_pass")),
        "gold_visual_density_regression_count": int(paper_poster.get("gold_visual_density_regression_count") or 0),
        "gold_quality_floor_pass": bool(paper_poster.get("gold_quality_floor_pass")),
        "gold_quality_floor_regression_count": int(paper_poster.get("gold_quality_floor_regression_count") or 0),
        "gold_composite_pass": bool(paper_poster.get("gold_composite_pass")),
        "gold_composite_score": paper_poster.get("gold_composite_score"),
        "gold_composite_fail_count": int(paper_poster.get("gold_composite_fail_count") or 0),
        "leaf_visible_word_count": leaf_visible_words,
        "preview_fallback_count": 1 if bool(paper_poster.get("preview_fallback_used")) else 0,
        "image_not_loaded_count": int(paper_poster.get("image_not_loaded_count") or 0),
        "block_out_of_bounds_count": int(paper_poster.get("block_out_of_bounds_count") or 0),
        "root_overflow_count": int(paper_poster.get("root_overflow_count") or 0),
        "caption_overlap_count": int(paper_poster.get("caption_overlap_count") or 0),
        "footer_overlap_count": int(paper_poster.get("footer_overlap_count") or 0),
        "panel_underfilled_count": int(paper_poster.get("panel_underfilled_count") or 0),
        "page_like_source_figure_count": int(paper_poster.get("page_like_source_figure_count") or 0),
        "figure_area_ratio": paper_poster.get("figure_area_ratio"),
        "native_information_unit_count": native_units,
        "dom_native_information_unit_count": dom_native_units,
        "image_count": (paper_poster.get("dom_metrics") or {}).get("image_count") if isinstance(paper_poster.get("dom_metrics"), dict) else None,
        "source_provenance_asset_count": paper_poster.get("source_provenance_asset_count"),
        "source_backed_dom_image_count": paper_poster.get("source_backed_dom_image_count"),
        "unbacked_source_image_count": int(paper_poster.get("unbacked_source_image_count") or 0),
        "page_like_source_dom_image_count": int(paper_poster.get("page_like_source_dom_image_count") or 0),
        "panel_internal_underfilled_count": int(paper_poster.get("panel_internal_underfilled_count") or 0),
        "panel_internal_underfilled_p0_count": int(paper_poster.get("panel_internal_underfilled_p0_count") or 0),
        "panel_internal_min_coverage": paper_poster.get("panel_internal_min_coverage"),
        "panel_internal_max_blank_run_ratio": paper_poster.get("panel_internal_max_blank_run_ratio"),
        "panel_visual_underfilled_count": int(paper_poster.get("panel_visual_underfilled_count") or 0),
        "panel_visual_underfilled_p0_count": int(paper_poster.get("panel_visual_underfilled_p0_count") or 0),
        "panel_visual_min_ink_ratio": paper_poster.get("panel_visual_min_ink_ratio"),
        "panel_visual_min_grid_coverage": paper_poster.get("panel_visual_min_grid_coverage"),
        "panel_visual_max_blank_run_ratio": paper_poster.get("panel_visual_max_blank_run_ratio"),
        "selected_source_asset_count": paper_poster.get("selected_source_asset_count"),
        "selected_source_asset_dom_missing_count": int(paper_poster.get("selected_source_asset_dom_missing_count") or 0),
        "dom_warning_count": int(paper_poster.get("dom_warning_count") or 0),
    }


def _dom_leaf_visible_word_count(dom_audit: dict[str, Any]) -> int:
    layers = [
        layer for layer in (dom_audit.get("dom_layers") or [])
        if isinstance(layer, dict)
        and str(layer.get("kind") or "") in {"text", "caption", "metric", "quote", "table"}
    ]
    return sum(
        len(re.findall(r"[A-Za-z0-9][A-Za-z0-9_+./:%-]*", str(layer.get("text") or "")))
        for layer in layers
    )


def _summarize_generation_authored_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [case.get("metrics") or {} for case in cases if isinstance(case.get("metrics"), dict)]
    if not rows:
        return {}

    def sum_metric(key: str) -> int:
        total = 0
        for row in rows:
            value = row.get(key)
            if isinstance(value, bool):
                total += int(value)
            elif isinstance(value, (int, float)):
                total += int(value)
        return total

    def avg_metric(key: str) -> float | None:
        values = [float(row[key]) for row in rows if isinstance(row.get(key), (int, float))]
        return round(sum(values) / len(values), 4) if values else None

    return {
        "authored_html_case_count": len(rows),
        "authored_render_mode_ratio": round(sum_metric("authored_html") / max(1, len(rows)), 4),
        "artifact_consistency_ok_ratio": round(sum_metric("artifact_consistency_ok") / max(1, len(rows)), 4),
        "manifest_matches_final_ratio": round(sum_metric("manifest_matches_final") / max(1, len(rows)), 4),
        "preview_matches_final_ratio": round(sum_metric("preview_matches_final") / max(1, len(rows)), 4),
        "dom_audit_matches_final_ratio": round(sum_metric("dom_audit_matches_final") / max(1, len(rows)), 4),
        "final_not_authored_count": len(rows) - sum_metric("final_is_authored_html"),
        "authored_artifact_mismatch_count": sum(
            1 for row in rows
            if bool(row.get("final_is_authored_html")) and not bool(row.get("artifact_consistency_ok"))
        ),
        "stale_authored_audit_count": sum_metric("stale_authored_audit_present"),
        "dom_audit_p0_count": sum_metric("dom_audit_p0_count"),
        "unprotected_dom_audit_p0_count": sum_metric("unprotected_dom_audit_p0_count"),
        "dense_gold_local_repair_only_count": sum_metric("dense_gold_local_repair_only"),
        "gold_visual_density_pass_count": sum_metric("gold_visual_density_pass"),
        "gold_visual_density_regression_count": sum_metric("gold_visual_density_regression_count"),
        "gold_quality_floor_pass_count": sum_metric("gold_quality_floor_pass"),
        "gold_quality_floor_regression_count": sum_metric("gold_quality_floor_regression_count"),
        "gold_composite_pass_count": sum_metric("gold_composite_pass"),
        "gold_composite_score": avg_metric("gold_composite_score"),
        "gold_composite_fail_count": sum_metric("gold_composite_fail_count"),
        "dom_backend_playwright_ratio": round(sum_metric("dom_backend_playwright") / max(1, len(rows)), 4),
        "preview_fallback_count": sum_metric("preview_fallback_count"),
        "image_not_loaded_count": sum_metric("image_not_loaded_count"),
        "block_out_of_bounds_count": sum_metric("block_out_of_bounds_count"),
        "root_overflow_count": sum_metric("root_overflow_count"),
        "caption_overlap_count": sum_metric("caption_overlap_count"),
        "footer_overlap_count": sum_metric("footer_overlap_count"),
        "panel_underfilled_count": sum_metric("panel_underfilled_count"),
        "page_like_source_figure_count": sum_metric("page_like_source_figure_count"),
        "leaf_visible_word_count": avg_metric("leaf_visible_word_count"),
        "figure_area_ratio": avg_metric("figure_area_ratio"),
        "native_information_unit_count": avg_metric("native_information_unit_count"),
        "dom_native_information_unit_count": avg_metric("dom_native_information_unit_count"),
        "source_provenance_asset_count": avg_metric("source_provenance_asset_count"),
        "source_backed_dom_image_count": avg_metric("source_backed_dom_image_count"),
        "unbacked_source_image_count": sum_metric("unbacked_source_image_count"),
        "page_like_source_dom_image_count": sum_metric("page_like_source_dom_image_count"),
        "panel_internal_underfilled_count": sum_metric("panel_internal_underfilled_count"),
        "panel_internal_underfilled_p0_count": sum_metric("panel_internal_underfilled_p0_count"),
        "panel_internal_min_coverage": avg_metric("panel_internal_min_coverage"),
        "panel_internal_max_blank_run_ratio": avg_metric("panel_internal_max_blank_run_ratio"),
        "panel_visual_underfilled_count": sum_metric("panel_visual_underfilled_count"),
        "panel_visual_underfilled_p0_count": sum_metric("panel_visual_underfilled_p0_count"),
        "panel_visual_min_ink_ratio": avg_metric("panel_visual_min_ink_ratio"),
        "panel_visual_min_grid_coverage": avg_metric("panel_visual_min_grid_coverage"),
        "panel_visual_max_blank_run_ratio": avg_metric("panel_visual_max_blank_run_ratio"),
        "selected_source_asset_count": avg_metric("selected_source_asset_count"),
        "selected_source_asset_dom_missing_count": sum_metric("selected_source_asset_dom_missing_count"),
        "dom_warning_count": sum_metric("dom_warning_count"),
    }


def _finding_id_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        issue_id = str(finding.get("id") or "")
        if issue_id:
            counts[issue_id] = counts.get(issue_id, 0) + 1
    return counts


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def build_issue_rollup(metrics: dict[str, Any], ingest_curation: dict[str, Any] | None = None) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    score_values: list[float] = []
    scorecards: list[dict[str, Any]] = []

    issues.extend(_label_calibration_issues(metrics))

    for result in metrics.get("results") or []:
        case = (result.get("case") or {}).get("slug") or "unknown"
        comparison = result.get("comparison") or {}
        if comparison.get("proxy_score") is not None:
            score_values.append(float(comparison.get("proxy_score")))
        for issue in comparison.get("issues") or []:
            issues.append({
                "case": case,
                "id": issue.get("id"),
                "owner": _owner(issue.get("owner")),
                "severity": _severity(issue.get("severity")),
                "message": issue.get("message"),
                "source": "proxy_eval",
                "artifact_paths": _artifact_paths(result),
            })
        run_dir = ((result.get("candidate") or {}).get("generation") or {}).get("run_dir")
        if run_dir:
            run_signals = _read_run_signals(Path(run_dir))
            scorecards.extend(run_signals.get("scorecards") or [])
            for finding in run_signals.get("findings") or []:
                issues.append({
                    "case": case,
                    "id": finding.get("id"),
                    "owner": _owner_for_feedback(finding),
                    "severity": _severity(finding.get("severity")),
                    "stage": finding.get("stage"),
                    "repair_route": finding.get("repair_route"),
                    "message": finding.get("message"),
                    "source": finding.get("source") or "design_feedback",
                    "artifact_paths": _artifact_paths(result) | {"run_dir": str(run_dir)},
                })
    if ingest_curation:
        for case_report in ingest_curation.get("cases") or []:
            case = case_report.get("case") or "unknown"
            artifact_paths = dict(case_report.get("artifact_paths") or {})
            artifact_paths["ingest_curation_report"] = str(ingest_curation.get("path") or "")
            for finding in case_report.get("findings") or []:
                if not isinstance(finding, dict):
                    continue
                issues.append({
                    "case": case,
                    "id": finding.get("id"),
                    "owner": _owner(finding.get("owner")),
                    "severity": _severity(finding.get("severity")),
                    "message": finding.get("message"),
                    "source": "ingest_curation",
                    "artifact_paths": {key: value for key, value in artifact_paths.items() if value},
                })
    for case_report in metrics.get("generation_authored_cases") or []:
        if not isinstance(case_report, dict):
            continue
        case = str(case_report.get("case") or "unknown")
        authored_metrics = case_report.get("metrics") if isinstance(case_report.get("metrics"), dict) else {}
        dense_local_repair_only = bool(authored_metrics.get("dense_gold_local_repair_only"))
        artifact_paths = {
            "run_dir": case_report.get("run_dir"),
            "paper_poster_dom_audit": case_report.get("dom_audit_path"),
            "paper_poster_render_manifest": case_report.get("manifest_path"),
            "poster_contract_audit": case_report.get("contract_audit_path"),
            "preview": case_report.get("preview_path"),
            "html": case_report.get("html_path"),
            "final_preview": case_report.get("final_preview_path"),
            "final_html": case_report.get("final_html_path"),
            "generation_authored_report": metrics.get("generation_authored_report_path"),
        }
        for finding in case_report.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            issue_id = finding.get("id")
            if any(issue.get("case") == case and issue.get("id") == issue_id for issue in issues):
                continue
            severity = _severity(finding.get("severity"))
            if dense_local_repair_only and severity == "blocker" and _is_local_dom_repair_finding(finding):
                severity = "high"
            issues.append({
                "case": case,
                "id": issue_id,
                "owner": _owner_for_dom_finding(finding),
                "severity": severity,
                "message": finding.get("message"),
                "source": "paper_poster_dom_audit",
                "repair_route": finding.get("repair_route"),
                "artifact_paths": {key: str(value) for key, value in artifact_paths.items() if value},
            })

    owner_counts = {owner: {"total": 0, "blocker": 0, "high": 0, "medium": 0, "low": 0} for owner in OWNER_TAXONOMY}
    for issue in issues:
        owner = _owner(issue.get("owner"))
        severity = _severity(issue.get("severity"))
        owner_counts.setdefault(owner, {"total": 0, "blocker": 0, "high": 0, "medium": 0, "low": 0})
        owner_counts[owner]["total"] += 1
        owner_counts[owner][severity] += 1

    avg_score = round(sum(score_values) / len(score_values), 3) if score_values else None
    label_summary = _label_calibration_summary(metrics)
    blocker_count = sum(1 for issue in issues if _severity(issue.get("severity")) == "blocker")
    high_count = sum(1 for issue in issues if _severity(issue.get("severity")) == "high")
    medium_count = sum(1 for issue in issues if _severity(issue.get("severity")) == "medium")
    low_count = sum(1 for issue in issues if _severity(issue.get("severity")) == "low")
    low_confidence = bool(metrics.get("low_confidence")) or len(score_values) == 1
    dimension_summary = _candidate_dimension_summary(metrics)
    ingest_summary = (ingest_curation or {}).get("summary") if ingest_curation else metrics.get("ingest_curation_summary")
    authored_summary = metrics.get("generation_authored_summary")
    artifact_role_counts = _result_artifact_role_counts(metrics)
    scorecard_avg = _average_scorecards(scorecards)
    scorecard_source = "critic" if scorecard_avg else None
    if not scorecard_avg:
        scorecard_avg = _deterministic_scorecard_fallback(
            avg_score=avg_score,
            dimension_summary=dimension_summary,
            generation_authored_summary=authored_summary if isinstance(authored_summary, dict) else {},
            ingest_curation_summary=ingest_summary if isinstance(ingest_summary, dict) else {},
            issues=issues,
        )
        if scorecard_avg:
            scorecard_source = "deterministic_fallback"
    manual_overall = dimension_summary.get("manual_work_proxy_overall")
    if scorecard_avg and isinstance(manual_overall, (int, float)) and scorecard_avg.get("human_effort_saved") is None:
        scorecard_avg["human_effort_saved"] = round(float(manual_overall), 3)
    confidence = _confidence_label(
        candidate_count=len(score_values),
        low_confidence=low_confidence,
    )
    success = {
        "average_score_ge_0_80": avg_score is not None and avg_score >= 0.80,
        "zero_blockers": blocker_count == 0,
        "evidence_use_ge_0_75": (
            scorecard_avg.get("evidence_use") is not None
            and float(scorecard_avg["evidence_use"]) >= 0.75
        ),
        "information_architecture_ge_0_75": (
            scorecard_avg.get("information_architecture") is not None
            and float(scorecard_avg["information_architecture"]) >= 0.75
        ),
    }
    return {
        "eval_set": (metrics.get("eval_set") or {}).get("id"),
        "harness_mode": metrics.get("harness_mode"),
        "low_confidence": low_confidence,
        "average_proxy_score": avg_score,
        "blocker_count": blocker_count,
        "scorecard_average": scorecard_avg,
        "scorecard_source": scorecard_source,
        "quality_scorecard": {
            "hard_blockers": blocker_count,
            "major_issues": high_count,
            "minor_issues": medium_count + low_count,
            "medium_issues": medium_count,
            "low_issues": low_count,
            "dimensions": dimension_summary,
            "confidence": confidence,
            "useful_partial_improvement": None,
        },
        "ingest_curation_summary": ingest_summary,
        "ingest_curation_report_path": (ingest_curation or {}).get("path") if ingest_curation else metrics.get("ingest_curation_report_path"),
        "generation_authored_summary": authored_summary,
        "generation_authored_report_path": metrics.get("generation_authored_report_path"),
        "artifact_role_counts": artifact_role_counts,
        **artifact_role_counts,
        "label_calibration_summary": label_summary,
        "success_targets": success,
        "acceptance_passed": all(success.values()) if scorecard_avg else False,
        "issues": sorted(issues, key=lambda item: (SEVERITY_RANK.get(_severity(item.get("severity")), 9), str(item.get("owner") or ""), str(item.get("id") or ""))),
        "owner_counts": owner_counts,
    }


def build_owner_diagnosis(rollup: dict[str, Any]) -> dict[str, Any]:
    issues = list(rollup.get("issues") or [])
    owner_counts = rollup.get("owner_counts", {}) if isinstance(rollup.get("owner_counts"), dict) else {}
    ranked = sorted(
        owner_counts.items(),
        key=lambda item: (
            item[1].get("blocker", 0),
            item[1].get("high", 0),
            item[1].get("total", 0),
        ),
        reverse=True,
    )
    primary_owner = ranked[0][0] if ranked and ranked[0][1].get("total", 0) else None
    primary_reason = "ranked_by_blocker_high_total"
    ingest_priority = _ingest_priority_owner(issues)
    if ingest_priority and not _layout_storyboard_dominates(owner_counts, ingest_priority):
        primary_owner = ingest_priority
        primary_reason = "ingest_priority"
    elif ingest_priority:
        primary_owner = "layout_storyboard"
        primary_reason = "layout_storyboard_dominates_ingest_priority"
    return {
        "primary_owner": primary_owner,
        "primary_owner_reason": primary_reason,
        "ranked_owners": [
            {"owner": owner, **counts, "likely_files": LIKELY_FILES.get(owner, [])}
            for owner, counts in ranked
            if counts.get("total", 0)
        ],
        "owner_taxonomy": OWNER_TAXONOMY,
    }


def _ingest_priority_owner(issues: list[dict[str, Any]]) -> str | None:
    if any(
        _owner(issue.get("owner")) == "model_routing"
        and _severity(issue.get("severity")) in {"blocker", "high"}
        for issue in issues
    ):
        return "model_routing"
    owner_order = ["visual_curation", "content_strategy", "layout_storyboard"]
    for severity in ("blocker", "high"):
        for owner in owner_order:
            if any(
                issue.get("source") == "ingest_curation"
                and _severity(issue.get("severity")) == severity
                and _owner(issue.get("owner")) == owner
                for issue in issues
            ):
                return owner
    return None


def _layout_storyboard_dominates(owner_counts: dict[str, Any], competing_owner: str | None) -> bool:
    layout = owner_counts.get("layout_storyboard") if isinstance(owner_counts.get("layout_storyboard"), dict) else {}
    if not layout:
        return False
    competing = owner_counts.get(str(competing_owner or "")) if competing_owner else {}
    if not isinstance(competing, dict):
        competing = {}
    layout_blockers = int(layout.get("blocker") or 0)
    layout_high = int(layout.get("high") or 0)
    layout_total = int(layout.get("total") or 0)
    competing_total = int(competing.get("total") or 0)
    if layout_blockers >= 3 and layout_total >= max(24, competing_total + 10):
        return True
    if layout_blockers >= 1 and layout_high >= 20 and layout_total >= max(35, competing_total * 2):
        return True
    return False


def build_before_after(
    before: dict[str, Any] | None,
    after: dict[str, Any],
    *,
    before_rollup: dict[str, Any] | None = None,
    after_rollup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    before_summary = _metrics_summary(before or {}, rollup=before_rollup)
    after_summary = _metrics_summary(after or {}, rollup=after_rollup)
    deltas = {
        key: _delta(before_summary.get(key), after_summary.get(key))
        for key in sorted(set(before_summary) | set(after_summary))
    }
    owner_deltas = _owner_count_deltas(
        (before_rollup or {}).get("owner_counts") or {},
        (after_rollup or {}).get("owner_counts") or {},
    )
    return {
        "before": before_summary,
        "after": after_summary,
        "deltas": deltas,
        "owner_deltas": owner_deltas,
        "has_before": bool(before),
        "regression_flags": _regression_flags(deltas, after=after_summary),
    }


def build_system_patch_brief(
    rollup: dict[str, Any],
    diagnosis: dict[str, Any],
    before_after: dict[str, Any],
    *,
    iter_dir: Path,
    args: argparse.Namespace,
    harness_mode: str,
    eval_rc: int,
) -> str:
    issues = rollup.get("issues") or []
    top_initial = issues[0] if issues else {}
    owner = diagnosis.get("primary_owner") or top_initial.get("owner") or "none"
    top = next(
        (issue for issue in issues if _owner(issue.get("owner")) == owner),
        top_initial,
    )
    likely_files = LIKELY_FILES.get(str(owner), [])
    observed = top.get("message") or "No candidate issues exceeded the current proxy thresholds."
    evidence_paths = top.get("artifact_paths") or {}
    lines = [
        "# System Patch Brief",
        "",
        f"- Eval set: `{args.eval_set or 'none'}`",
        f"- Label set: `{args.label_set or 'none'}`",
        f"- Eval route override: `{harness_mode}`",
        f"- Eval exit code: `{eval_rc}`",
        f"- Low confidence: `{rollup.get('low_confidence')}`",
        f"- Average proxy score: `{rollup.get('average_proxy_score')}`",
        f"- Blockers: `{rollup.get('blocker_count')}`",
        f"- Final candidates: `{rollup.get('final_candidate_count')}`",
        f"- Diagnostic partials: `{rollup.get('diagnostic_partial_count')}`",
        f"- Failed/no visual: `{rollup.get('failed_no_visual_count')}`",
    ]
    quality_scorecard = rollup.get("quality_scorecard") or {}
    if isinstance(quality_scorecard, dict) and quality_scorecard:
        dimensions = quality_scorecard.get("dimensions") if isinstance(quality_scorecard.get("dimensions"), dict) else {}
        lines.extend([
            f"- Confidence: `{quality_scorecard.get('confidence')}`",
            f"- Major issues: `{quality_scorecard.get('major_issues')}`",
            f"- Minor issues: `{quality_scorecard.get('minor_issues')}`",
        ])
        if dimensions:
            lines.extend([
                f"- Average visual area: `{dimensions.get('average_visual_area_ratio')}`",
                f"- Average top-half visual area: `{dimensions.get('average_top_half_visual_area_ratio')}`",
                f"- Average caption-like text count: `{dimensions.get('average_caption_like_text_count')}`",
                f"- Mixed-panel binding: `{dimensions.get('mixed_panel_binding_score')}`",
                f"- Panel rule score: `{dimensions.get('panel_rule_score')}`",
                f"- Image-panel text score: `{dimensions.get('image_panel_text_score')}`",
                f"- Underfilled panels avg: `{dimensions.get('average_underfilled_panel_count')}`",
                f"- Title/content mismatches avg: `{dimensions.get('average_section_title_content_mismatch_count')}`",
                f"- Manual-work proxy: `{dimensions.get('manual_work_proxy_overall')}`",
                f"- Semantic synthesis: `{dimensions.get('semantic_synthesis_score')}`",
                f"- Text density quality: `{dimensions.get('text_density_quality')}`",
                f"- Native reconstruction: `{dimensions.get('native_reconstruction_score')}`",
            ])
    curation_summary = rollup.get("ingest_curation_summary") or {}
    if isinstance(curation_summary, dict) and curation_summary:
        lines.extend([
            f"- Ingest curation score: `{curation_summary.get('ingest_score')}`",
            f"- Visual role coverage: `{curation_summary.get('visual_role_coverage')}`",
            f"- Selected visual placement: `{curation_summary.get('selected_visuals_placed_ratio')}`",
            f"- Provenance assets: `{curation_summary.get('provenance_asset_count')}`",
            f"- Provenance missing: `{curation_summary.get('provenance_missing_count')}`",
            f"- Storyboard selected assets: `{curation_summary.get('storyboard_selected_asset_count')}`",
            f"- Storyboard asset placement: `{curation_summary.get('storyboard_selected_asset_placed_ratio')}`",
        ])
        if curation_summary.get("worst_case"):
            lines.append(f"- Worst ingest case: `{curation_summary.get('worst_case')}`")
        if rollup.get("ingest_curation_report_path"):
            lines.append(f"- Ingest curation report: `{rollup.get('ingest_curation_report_path')}`")
    label_summary = rollup.get("label_calibration_summary") or {}
    if isinstance(label_summary, dict) and label_summary:
        lines.extend([
            f"- Label calibration accuracy: `{label_summary.get('label_accuracy')}`",
            f"- Label mismatches: `{label_summary.get('label_mismatch_count')}`",
            f"- Label missed axes: `{label_summary.get('label_missed_axis_count')}`",
        ])
    identity_summary = (before_after.get("after") or {})
    if any(key in identity_summary for key in ("authored_render_mode_ratio", "dom_audit_p0_count")):
        lines.extend([
            f"- Authored HTML render ratio: `{identity_summary.get('authored_render_mode_ratio')}`",
            f"- DOM audit P0 count: `{identity_summary.get('dom_audit_p0_count')}`",
            f"- Unprotected DOM P0 count: `{identity_summary.get('unprotected_dom_audit_p0_count')}`",
            f"- Gold density regressions: `{identity_summary.get('gold_visual_density_regression_count')}`",
            f"- Gold quality floor regressions: `{identity_summary.get('gold_quality_floor_regression_count')}`",
            f"- Gold composite score: `{identity_summary.get('gold_composite_score')}`",
            f"- Gold composite fail count: `{identity_summary.get('gold_composite_fail_count')}`",
            f"- Dense local-repair-only cases: `{identity_summary.get('dense_gold_local_repair_only_count')}`",
            f"- DOM Playwright ratio: `{identity_summary.get('dom_backend_playwright_ratio')}`",
            f"- Preview fallback count: `{identity_summary.get('preview_fallback_count')}`",
            f"- Image-not-loaded count: `{identity_summary.get('image_not_loaded_count')}`",
            f"- Source provenance assets: `{identity_summary.get('source_provenance_asset_count')}`",
            f"- Source-backed DOM images: `{identity_summary.get('source_backed_dom_image_count')}`",
            f"- Unbacked source images: `{identity_summary.get('unbacked_source_image_count')}`",
            f"- Page-like source figures: `{identity_summary.get('page_like_source_dom_image_count')}`",
            f"- Missing selected source assets: `{identity_summary.get('selected_source_asset_dom_missing_count')}`",
            f"- Underfilled panel interiors: `{identity_summary.get('panel_internal_underfilled_count')}`",
            f"- Panel internal min coverage: `{identity_summary.get('panel_internal_min_coverage')}`",
            f"- Panel internal max blank run: `{identity_summary.get('panel_internal_max_blank_run_ratio')}`",
            f"- Visually underfilled panels: `{identity_summary.get('panel_visual_underfilled_p0_count')}`",
            f"- Panel visual min ink: `{identity_summary.get('panel_visual_min_ink_ratio')}`",
            f"- Panel visual max blank run: `{identity_summary.get('panel_visual_max_blank_run_ratio')}`",
            f"- Root overflow count: `{identity_summary.get('root_overflow_count')}`",
            f"- Caption overlap count: `{identity_summary.get('caption_overlap_count')}`",
            f"- Figure area ratio: `{identity_summary.get('figure_area_ratio')}`",
            f"- Leaf visible words: `{identity_summary.get('leaf_visible_word_count')}`",
        ])
    lines.extend([
        "",
        "## Observed Failure",
        "",
        observed,
        "",
        "## Evidence Artifact Paths",
        "",
    ])
    if evidence_paths:
        lines.extend(f"- `{key}`: `{value}`" for key, value in sorted(evidence_paths.items()))
    else:
        lines.append(f"- Eval report: `{iter_dir / 'report.md'}`")
    lines.extend([
        "",
        "## Suspected Owner",
        "",
        f"`{owner}`",
        "",
        "## Desired System Behavior",
        "",
        _desired_behavior(str(owner)),
        "",
        "## Likely Files",
        "",
    ])
    lines.extend(f"- `{path}`" for path in likely_files) if likely_files else lines.append("- None; no patch recommended.")
    lines.extend([
        "",
        "## Non-Goals",
        "",
        "- Do not mutate the evaluator and generator in the same iteration when claiming generation improvement.",
        "- Do not loosen acceptance thresholds to hide a candidate failure.",
        "- Do not add automatic repo mutation to `poster_quality_loop.py`.",
        "- Do not increase model cost for explicit lower-cost eval overrides.",
        "",
        "## Acceptance Tests",
        "",
        "- `git diff --check`",
        "- `python3 -m py_compile scripts/poster_quality_eval.py scripts/poster_quality_loop.py`",
        "- `uv --cache-dir .uv-cache run python -m autodesign.smoke`",
        "- Rerun the same eval set and inspect `acceptance_signal.json`.",
        "- Reliability/model/harness patches may be accepted by target-owner issue reduction or pass/finalize-rate improvement, without requiring proxy-score gain.",
        "- Layout/content/curation patches must improve owner-specific dimensions such as human-effort proxy, semantic synthesis, native reconstruction, archetype-conditioned visual fill, caption/placement coverage, or scorecard dimensions without hard regressions.",
        "- Generation-quality patches may accept smaller proxy gains (`>=0.04`) when blockers/pass/finalize counts do not regress.",
        "",
        "## Next Raphael Action",
        "",
        f"- Patch owner: `{owner}`",
        f"- Loop action: `{_next_raphael_loop_action(rollup, before_after, eval_rc=eval_rc)}`",
        "- Exact rerun command:",
        "",
        "```bash",
        _rerun_command(args, harness_mode),
        "```",
        "",
        "## Rerun Command",
        "",
        "```bash",
        _rerun_command(args, harness_mode),
        "```",
        "",
        "## Rollback Criteria",
        "",
        "- Revert the patch if blocker count increases.",
        "- Revert if pass/finalized count regresses.",
        "- Revert if average proxy score drops beyond the low-confidence noise band without a deliberate evaluator-only calibration.",
        "- Revert if the attempted owner severity score worsens.",
        "- Revert if explicit lower-cost eval overrides start using a more expensive model path.",
        "",
        "## Top Issues",
        "",
    ])
    if not issues:
        lines.append("- No issues found by current proxy and environment signals.")
    else:
        for issue in issues[:8]:
            lines.append(
                f"- `{issue.get('severity')}` `{issue.get('owner')}` `{issue.get('id')}`: {issue.get('message')}"
            )
    lines.extend([
        "",
        "## Before/After",
        "",
        render_before_after(before_after),
    ])
    return "\n".join(lines)


def _rerun_command(args: argparse.Namespace, harness_mode: str) -> str:
    parts = [
        f"POSTER_HARNESS_MODE={harness_mode} uv run python scripts/poster_quality_loop.py",
        f"  --data-dir {_shell_quote(args.data_dir)}",
    ]
    if args.eval_set:
        parts.append(f"  --set {_shell_quote(args.eval_set)}")
    if args.label_set:
        parts.append(f"  --label-set {_shell_quote(args.label_set)}")
    for case in args.case:
        parts.append(f"  --case {_shell_quote(case)}")
    for candidate in args.candidate:
        parts.append(f"  --candidate {_shell_quote(candidate)}")
    if not args.generate:
        parts.append("  --no-generate")
    if args.template:
        parts.append(f"  --template {_shell_quote(args.template)}")
    if args.brief:
        parts.append(f"  --brief {_shell_quote(args.brief)}")
    if args.skip_enhancer:
        parts.append("  --skip-enhancer")
    if args.no_claim_graph:
        parts.append("  --no-claim-graph")
    if args.generate_workers is not None:
        parts.append(f"  --generate-workers {int(args.generate_workers)}")
    if args.allow_large_generate:
        parts.append("  --allow-large-generate")
    if args.attempted_owner:
        parts.append(f"  --attempted-owner {_shell_quote(args.attempted_owner)}")
    if args.patch_summary:
        parts.append(f"  --patch-summary {_shell_quote(args.patch_summary)}")
    if args.raphael_loop:
        parts.append("  --raphael-loop")
    parts.append("  --max-system-iterations 1")
    return " \\\n".join(parts)


def _next_raphael_loop_action(
    rollup: dict[str, Any],
    before_after: dict[str, Any],
    *,
    eval_rc: int,
) -> str:
    if eval_rc != 0:
        return "keep_iterating: fix eval or generation failure"
    after = before_after.get("after") or {}
    if int(after.get("blocker_count") or 0) > 0 or int(after.get("high_count") or 0) > 0:
        return "keep_iterating: patch the current primary owner"
    if int(after.get("label_mismatch_count") or 0) > 0:
        return "keep_iterating: repair labeled evaluator calibration"
    if float(after.get("authored_render_mode_ratio") or 1.0) < 1.0:
        return "keep_iterating: route academic posters through authored HTML"
    if int(after.get("final_not_authored_count") or 0) > 0:
        return "keep_iterating: keep final academic posters on authored HTML"
    if int(after.get("authored_artifact_mismatch_count") or 0) > 0:
        return "keep_iterating: repair authored final artifact manifest/audit consistency"
    if int(after.get("stale_authored_audit_count") or 0) > 0:
        return "keep_iterating: remove stale authored audit artifacts from final metrics"
    if int(after.get("gold_visual_density_regression_count") or 0) > 0:
        return "keep_iterating: restore iteration-51 gold visual density floors"
    if int(after.get("gold_quality_floor_regression_count") or 0) > 0:
        return "keep_iterating: restore handmade gold quality floors"
    if int(after.get("gold_composite_fail_count") or 0) > 0:
        return "keep_iterating: beat handmade gold composite signals"
    unprotected_dom_p0 = after.get("unprotected_dom_audit_p0_count")
    if unprotected_dom_p0 is None:
        unprotected_dom_p0 = after.get("dom_audit_p0_count")
    if int(unprotected_dom_p0 or 0) > 0:
        return "keep_iterating: repair unprotected authored DOM audit P0 findings"
    if int(after.get("preview_fallback_count") or 0) > 0:
        return "keep_iterating: remove authored preview fallback"
    hard_flags = before_after.get("regression_flags") or []
    if hard_flags:
        return "keep_iterating: remove hard regressions"
    candidate_count = int(after.get("candidate_count") or 0)
    if candidate_count < 2:
        return "expand_eval: run one or two gold generation cases"
    if rollup.get("low_confidence"):
        return "expand_eval: rerun fixed cases to reduce noise"
    return "stop_for_human_review: no blocker/high issue remains in this report"


def render_before_after(payload: dict[str, Any]) -> str:
    lines = [
        "| Metric | Before | After | Delta |",
        "|---|---:|---:|---:|",
    ]
    before = payload.get("before") or {}
    after = payload.get("after") or {}
    deltas = payload.get("deltas") or {}
    for key in sorted(set(before) | set(after)):
        lines.append(f"| `{key}` | `{before.get(key)}` | `{after.get(key)}` | `{deltas.get(key)}` |")
    flags = payload.get("regression_flags") or []
    if flags:
        lines.extend(["", "Regression flags:"])
        lines.extend(f"- {flag}" for flag in flags)
    owner_deltas = {
        owner: delta for owner, delta in (payload.get("owner_deltas") or {}).items()
        if any(value not in (None, 0, 0.0) for value in (delta or {}).values())
    }
    if owner_deltas:
        lines.extend(["", "Owner deltas:"])
        for owner, delta in sorted(owner_deltas.items()):
            lines.append(
                f"- `{owner}` total `{delta.get('total')}`, blocker `{delta.get('blocker')}`, "
                f"high `{delta.get('high')}`, medium `{delta.get('medium')}`, low `{delta.get('low')}`"
            )
    if not payload.get("has_before"):
        lines.extend(["", "No baseline metrics were provided; this is an after-only report."])
    return "\n".join(lines)


def _append_progress_entry(
    progress_path: Path,
    progress: dict[str, Any],
    *,
    iteration: int,
    args: argparse.Namespace,
    eval_rc: int,
    rollup: dict[str, Any],
    diagnosis: dict[str, Any],
    before_after: dict[str, Any],
    iter_dir: Path,
) -> dict[str, Any]:
    attempted_owner = _attempted_owner(args, diagnosis, progress)
    acceptance_signal = _acceptance_signal(
        before_after,
        rollup=rollup,
        attempted_owner=attempted_owner,
        eval_rc=eval_rc,
    )
    accepted = acceptance_signal.get("accepted")
    reason = str(acceptance_signal.get("reason") or "")
    next_owner = diagnosis.get("primary_owner") or "none"
    curation_summary = rollup.get("ingest_curation_summary") or {}
    after_summary = before_after.get("after") or {}
    artifact_role_counts = {
        key: _safe_int(after_summary.get(key))
        for key in ARTIFACT_ROLE_COUNT_KEYS
    }
    entry = {
        "iteration": iteration,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "attempted_owner": attempted_owner,
        "patch_summary": args.patch_summary or "No patch summary provided",
        "eval_exit_code": eval_rc,
        "before": before_after.get("before") or {},
        "after": after_summary,
        "artifact_role_counts": artifact_role_counts,
        **artifact_role_counts,
        "ingest_score": curation_summary.get("ingest_score"),
        "visual_role_coverage": curation_summary.get("visual_role_coverage"),
        "brief_section_coverage": curation_summary.get("brief_section_coverage"),
        "candidate_budget_used": curation_summary.get("candidate_budget_used") or {},
        "selected_visuals_placed_ratio": curation_summary.get("selected_visuals_placed_ratio"),
        "provenance_asset_count": curation_summary.get("provenance_asset_count"),
        "provenance_missing_count": curation_summary.get("provenance_missing_count"),
        "storyboard_selected_asset_count": curation_summary.get("storyboard_selected_asset_count"),
        "storyboard_selected_asset_placed_ratio": curation_summary.get("storyboard_selected_asset_placed_ratio"),
        "accepted": accepted,
        "acceptance_reason": reason,
        "acceptance_signal": acceptance_signal,
        "next_recommended_owner": next_owner,
        "metrics_path": str(iter_dir / "metrics.json"),
        "issue_rollup_path": str(iter_dir / "issue_rollup.json"),
        "ingest_curation_report_path": str(iter_dir / "ingest_curation_report.json"),
        "system_patch_brief_path": str(iter_dir / "system_patch_brief.md"),
        "git_sha": _git_output(["rev-parse", "HEAD"]),
        "dirty_files": _git_output(["status", "--short"]).splitlines(),
    }
    progress["eval_set"] = args.eval_set
    progress["label_set"] = args.label_set
    progress["out_dir"] = str(progress_path.parent)
    progress["iterations"] = list(progress.get("iterations") or []) + [entry]
    progress["convergence_summary"] = _build_convergence_summary(
        progress["iterations"],
        rollup=rollup,
        current_primary_owner=next_owner,
        current_acceptance_signal=acceptance_signal,
    )
    atomic_write_json(progress_path, progress)
    return acceptance_signal


def _build_convergence_summary(
    iterations: list[dict[str, Any]],
    *,
    rollup: dict[str, Any],
    current_primary_owner: str,
    current_acceptance_signal: dict[str, Any],
) -> dict[str, Any]:
    accepted_iterations = [
        int(entry.get("iteration") or 0)
        for entry in iterations
        if entry.get("accepted") is True
    ]
    rejected_count = sum(1 for entry in iterations if entry.get("accepted") is False)
    no_improvement_streak = 0
    for entry in reversed(iterations):
        if entry.get("accepted") is True:
            break
        no_improvement_streak += 1
    repeated_owner_streak = 0
    for entry in reversed(iterations):
        if str(entry.get("next_recommended_owner") or "none") != str(current_primary_owner or "none"):
            break
        repeated_owner_streak += 1
    current = iterations[-1] if iterations else {}
    after = current.get("after") if isinstance(current.get("after"), dict) else {}
    label_ok = (
        int(after.get("label_case_count") or 0) == 12
        and int(after.get("label_matched_count") or 0) == 12
        and int(after.get("label_mismatch_count") or 0) == 0
    )
    no_blocker_or_high = (
        int(after.get("blocker_count") or 0) == 0
        and int(after.get("high_count") or 0) == 0
    )
    no_hard_regressions = not bool(current_acceptance_signal.get("hard_regressions"))
    converged = (
        no_improvement_streak >= 5
        and no_blocker_or_high
        and label_ok
        and no_hard_regressions
    )
    return {
        "accepted_count": len(accepted_iterations),
        "rejected_count": rejected_count,
        "last_accepted_iteration": max(accepted_iterations) if accepted_iterations else None,
        "no_improvement_streak": no_improvement_streak,
        "repeated_owner_streak": repeated_owner_streak,
        "current_primary_owner": current_primary_owner,
        "no_blocker_or_high": no_blocker_or_high,
        "label_calibration_12_of_12": label_ok,
        "generation_eval_no_hard_regressions": no_hard_regressions,
        "converged": converged,
        "current_blocker_count": rollup.get("blocker_count"),
        "current_high_count": after.get("high_count"),
        "current_final_candidate_count": after.get("final_candidate_count"),
        "current_diagnostic_partial_count": after.get("diagnostic_partial_count"),
        "current_failed_no_visual_count": after.get("failed_no_visual_count"),
    }


def _read_progress(path: Path) -> dict[str, Any]:
    payload = _read_json(path)
    if isinstance(payload, dict):
        payload["iterations"] = list(payload.get("iterations") or [])
        return payload
    return {"version": 1, "iterations": []}


def _previous_progress_metrics(progress: dict[str, Any]) -> dict[str, Any] | None:
    for entry in reversed(list(progress.get("iterations") or [])):
        if not isinstance(entry, dict) or not entry.get("metrics_path"):
            continue
        metrics = _read_json(Path(str(entry.get("metrics_path"))).expanduser())
        if isinstance(metrics, dict):
            return metrics
    return None


def _attempted_owner(args: argparse.Namespace, diagnosis: dict[str, Any], progress: dict[str, Any]) -> str:
    if args.attempted_owner:
        return str(args.attempted_owner)
    iterations = list(progress.get("iterations") or [])
    if iterations:
        prior = iterations[-1].get("next_recommended_owner")
        if prior:
            return str(prior)
    return str(diagnosis.get("primary_owner") or "none")


def _acceptance_decision(before_after: dict[str, Any], *, eval_rc: int = 0) -> tuple[bool | None, str]:
    signal = _acceptance_signal(
        before_after,
        rollup={},
        attempted_owner="none",
        eval_rc=eval_rc,
    )
    return signal.get("accepted"), str(signal.get("reason") or "")


def _acceptance_signal(
    before_after: dict[str, Any],
    *,
    rollup: dict[str, Any],
    attempted_owner: str,
    eval_rc: int = 0,
) -> dict[str, Any]:
    after = before_after.get("after") or {}
    candidate_count = after.get("candidate_count")
    owner = _owner(attempted_owner)
    owner_family = OWNER_ACCEPTANCE_FAMILIES.get(owner, "generation_quality")
    signal: dict[str, Any] = {
        "accepted": None,
        "reason": "",
        "attempted_owner": owner,
        "owner_family": owner_family,
        "confidence": ((rollup.get("quality_scorecard") or {}).get("confidence") or _confidence_label(
            candidate_count=int(candidate_count or 0),
            low_confidence=bool(after.get("low_confidence")),
        )),
        "hard_regressions": [],
        "improved_dimensions": [],
        "regressed_dimensions": [],
        "owner_delta": {},
        "useful_partial_improvement": False,
        "recommendation": "reject",
    }
    if eval_rc != 0:
        signal.update(accepted=False, reason=f"eval failed (exit code {eval_rc}).")
        return signal
    has_candidates = isinstance(candidate_count, (int, float)) and candidate_count > 0
    has_label_calibration = (
        isinstance(after.get("label_case_count"), (int, float))
        and int(after.get("label_case_count") or 0) > 0
    )
    if not has_candidates and not has_label_calibration:
        signal.update(accepted=False, reason="generation failed / no candidate metrics.")
        return signal
    if not before_after.get("has_before"):
        signal.update(
            accepted=None,
            reason="No baseline or previous metrics were available.",
            recommendation="establish_baseline",
        )
        return signal

    deltas = before_after.get("deltas") or {}
    owner_delta = (before_after.get("owner_deltas") or {}).get(owner) or {}
    signal["owner_delta"] = owner_delta
    improvements = _dimension_improvements(deltas)
    regressions = _dimension_regressions(deltas)
    signal["improved_dimensions"] = improvements
    signal["regressed_dimensions"] = regressions
    target_owner_delta_score = _owner_delta_score(owner_delta)
    signal["target_owner_delta_score"] = target_owner_delta_score

    hard_regressions = _hard_acceptance_regressions(
        deltas,
        after=after,
        owner_delta=owner_delta,
        low_confidence=bool(after.get("low_confidence")),
    )
    if hard_regressions:
        signal.update(
            accepted=False,
            reason="; ".join(hard_regressions),
            hard_regressions=hard_regressions,
            useful_partial_improvement=bool(improvements),
        )
        return signal

    blocker_delta = deltas.get("blocker_count")
    if isinstance(blocker_delta, (int, float)) and blocker_delta < 0:
        signal.update(
            accepted=True,
            reason=f"Blocker count decreased by {abs(blocker_delta):g}.",
            recommendation="accept",
            useful_partial_improvement=True,
        )
        return signal

    label_improvement = _label_calibration_improvement_reason(deltas)
    if owner_family == "evaluator" and label_improvement:
        signal.update(
            accepted=True,
            reason=label_improvement,
            recommendation="accept",
            useful_partial_improvement=True,
        )
        return signal

    if target_owner_delta_score < 0:
        signal.update(
            accepted=True,
            reason=f"Target owner `{owner}` severity decreased.",
            recommendation="accept",
            useful_partial_improvement=True,
        )
        return signal

    authored_improvement = _authored_improvement_reason(deltas, owner=owner)
    if authored_improvement:
        signal.update(
            accepted=True,
            reason=authored_improvement,
            recommendation="accept",
            useful_partial_improvement=True,
        )
        return signal

    pass_delta = deltas.get("pass_count")
    finalized_delta = deltas.get("finalized_count")
    if owner_family == "reliability" and (
        _positive_delta(pass_delta) or _positive_delta(finalized_delta)
    ):
        signal.update(
            accepted=True,
            reason="Reliability patch improved pass/finalized count without hard regressions.",
            recommendation="accept",
            useful_partial_improvement=True,
        )
        return signal

    score_delta = deltas.get("average_proxy_score")
    if isinstance(score_delta, (int, float)) and score_delta >= 0.04:
        signal.update(
            accepted=True,
            reason=f"Average proxy score improved by {score_delta:.3f}.",
            recommendation="accept",
            useful_partial_improvement=True,
        )
        return signal

    if owner_family == "evaluator" and _scorecard_dimension_improved(deltas):
        signal.update(
            accepted=True,
            reason="Evaluator/critic scorecard dimension improved without hard regressions.",
            recommendation="accept",
            useful_partial_improvement=True,
        )
        return signal

    content_delta = _content_strategy_improvement_reason(deltas)
    if owner == "content_strategy" and content_delta:
        signal.update(
            accepted=True,
            reason=content_delta,
            recommendation="accept",
            useful_partial_improvement=True,
        )
        return signal

    ingest_delta = deltas.get("ingest_score")
    if owner_family == "curation" and isinstance(ingest_delta, (int, float)) and ingest_delta >= 0.03:
        signal.update(
            accepted=True,
            reason=f"Ingest curation score improved by {ingest_delta:.3f}.",
            recommendation="accept",
            useful_partial_improvement=True,
        )
        return signal

    placed_delta = deltas.get("selected_visuals_placed_ratio")
    if owner_family in {"curation", "layout", "generation_quality"} and isinstance(placed_delta, (int, float)) and placed_delta >= 0.03:
        signal.update(
            accepted=True,
            reason=f"Selected-visual placement ratio improved by {placed_delta:.3f}.",
            recommendation="accept",
            useful_partial_improvement=True,
        )
        return signal

    if owner_family == "layout" and _layout_dimension_improved(deltas):
        signal.update(
            accepted=True,
            reason="Layout-specific dimensions improved without hard regressions.",
            recommendation="accept",
            useful_partial_improvement=True,
        )
        return signal

    if owner_family == "reliability" and not regressions:
        signal.update(
            accepted=True,
            reason="Reliability patch completed the eval without hard or dimensional regressions.",
            recommendation="accept",
        )
        return signal

    if improvements:
        signal.update(
            accepted=False,
            reason="Useful partial improvement detected but no owner-specific acceptance rule fired.",
            useful_partial_improvement=True,
            recommendation="rerun_or_expand_eval",
        )
        return signal

    signal.update(
        accepted=False,
        reason="No blocker, owner-specific, scorecard, or dimension improvement.",
    )
    return signal


def _read_run_signals(run_dir: Path) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    scorecards: list[dict[str, Any]] = []
    design_feedback = _read_latest_json(run_dir / "composites", "iter_*/design_feedback.json")
    if isinstance(design_feedback, dict):
        findings.extend(design_feedback.get("findings") or [])
    critique_paths = sorted(
        run_dir.glob("critique_*.json"),
        key=lambda path: _numeric_path_suffix(path, "critique_"),
    )
    for critique_path in critique_paths[-1:]:
        critique = _read_json(critique_path)
        if not isinstance(critique, dict):
            continue
        if isinstance(critique.get("dimension_scores"), dict):
            scorecards.append(critique.get("dimension_scores") or {})
        for issue in critique.get("issues") or []:
            if isinstance(issue, dict):
                findings.append({
                    "id": issue.get("issue_id") or issue.get("category"),
                    "source": "critic",
                    "severity": issue.get("severity"),
                    "stage": issue.get("stage"),
                    "repair_route": issue.get("repair_route"),
                    "message": issue.get("description"),
                    "category": issue.get("category"),
                    "critique_path": str(critique_path),
                })
    return {"findings": findings, "scorecards": scorecards}


def _numeric_path_suffix(path: Path, prefix: str) -> int:
    match = re.search(rf"{re.escape(prefix)}(\d+)", path.stem)
    if not match:
        return -1
    try:
        return int(match.group(1))
    except ValueError:
        return -1


def _read_latest_json(root: Path, pattern: str) -> dict[str, Any] | None:
    paths = sorted(root.glob(pattern))
    for path in reversed(paths):
        payload = _read_json(path)
        if isinstance(payload, dict):
            return payload
    return None


def _metrics_summary(metrics: dict[str, Any], *, rollup: dict[str, Any] | None = None) -> dict[str, Any]:
    scores: list[float] = []
    blockers = 0
    pass_count = 0
    finalized_count = 0
    for result in metrics.get("results") or []:
        comparison = result.get("comparison") or {}
        if comparison.get("proxy_score") is not None:
            scores.append(float(comparison.get("proxy_score")))
        for issue in comparison.get("issues") or []:
            if _severity(issue.get("severity")) == "blocker":
                blockers += 1
        generation = ((result.get("candidate") or {}).get("generation") or {})
        if generation.get("terminal_status") == "pass":
            pass_count += 1
        if generation.get("finalized") is True:
            finalized_count += 1
    blockers += _safe_int(metrics.get("ingest_curation_blocker_count"), 0)
    issue_counts = _rollup_issue_counts(rollup)
    summary = {
        "average_proxy_score": (
            rollup.get("average_proxy_score")
            if isinstance(rollup, dict) and rollup.get("average_proxy_score") is not None
            else round(sum(scores) / len(scores), 3) if scores else None
        ),
        "candidate_count": len(scores),
        "pass_count": pass_count,
        "finalized_count": finalized_count,
        "blocker_count": issue_counts.get("blocker", blockers),
        "high_count": issue_counts.get("high", 0),
        "medium_count": issue_counts.get("medium", 0),
        "low_count": issue_counts.get("low", 0),
        "major_issue_count": issue_counts.get("blocker", blockers) + issue_counts.get("high", 0),
        "minor_issue_count": issue_counts.get("medium", 0) + issue_counts.get("low", 0),
        "low_confidence": (
            bool(rollup.get("low_confidence"))
            if isinstance(rollup, dict) and rollup.get("low_confidence") is not None
            else bool(metrics.get("low_confidence"))
        ),
    }
    role_counts = (
        rollup.get("artifact_role_counts")
        if isinstance(rollup, dict) and isinstance(rollup.get("artifact_role_counts"), dict)
        else _result_artifact_role_counts(metrics)
    )
    for key in ARTIFACT_ROLE_COUNT_KEYS:
        summary[key] = _safe_int((role_counts or {}).get(key))
    summary.update(_candidate_dimension_summary(metrics))
    scorecard_average = rollup.get("scorecard_average") if isinstance(rollup, dict) else None
    if isinstance(scorecard_average, dict):
        for key, value in scorecard_average.items():
            summary[f"scorecard_{key}"] = value
    curation = metrics.get("ingest_curation_summary") if isinstance(metrics.get("ingest_curation_summary"), dict) else {}
    for key in (
        "ingest_score",
        "visual_role_coverage",
        "brief_section_coverage",
        "selected_visuals_placed_ratio",
        "provenance_asset_count",
        "provenance_missing_count",
        "storyboard_selected_asset_count",
        "storyboard_selected_asset_placed_ratio",
        "storyboard_missing_asset_count",
    ):
        if curation.get(key) is not None:
            summary[key] = curation.get(key)
    summary.update(_label_calibration_summary(metrics))
    authored_summary = (
        metrics.get("generation_authored_summary")
        if isinstance(metrics.get("generation_authored_summary"), dict)
        else {}
    )
    for key in (
        "authored_html_case_count",
        "authored_render_mode_ratio",
        "artifact_consistency_ok_ratio",
        "manifest_matches_final_ratio",
        "preview_matches_final_ratio",
        "dom_audit_matches_final_ratio",
        "final_not_authored_count",
        "authored_artifact_mismatch_count",
        "stale_authored_audit_count",
        "dom_audit_p0_count",
        "unprotected_dom_audit_p0_count",
        "dense_gold_local_repair_only_count",
        "gold_visual_density_pass_count",
        "gold_visual_density_regression_count",
        "gold_quality_floor_pass_count",
        "gold_quality_floor_regression_count",
        "gold_composite_pass_count",
        "gold_composite_score",
        "gold_composite_fail_count",
        "dom_backend_playwright_ratio",
        "preview_fallback_count",
        "image_not_loaded_count",
        "block_out_of_bounds_count",
        "root_overflow_count",
        "caption_overlap_count",
        "footer_overlap_count",
        "leaf_visible_word_count",
        "figure_area_ratio",
        "dom_warning_count",
    ):
        if authored_summary.get(key) is not None:
            summary[key] = authored_summary.get(key)
    return summary


def _candidate_dimension_summary(metrics: dict[str, Any]) -> dict[str, Any]:
    html_values: dict[str, list[float]] = {
        "average_visual_area_ratio": [],
        "average_top_half_visual_area_ratio": [],
        "average_text_area_ratio": [],
        "average_visible_word_count": [],
        "average_leaf_visible_word_count": [],
        "average_caption_like_text_count": [],
        "mixed_panel_binding_score": [],
        "text_density_quality": [],
        "average_section_label_like_count": [],
        "average_image_layer_count": [],
        "manual_work_proxy_overall": [],
        "semantic_synthesis_score": [],
        "information_architecture_proxy_score": [],
        "native_reconstruction_score": [],
        "paper_faithfulness_proxy": [],
        "narrative_coherence_proxy": [],
        "source_visual_use": [],
        "panel_rule_score": [],
        "image_panel_text_score": [],
        "panel_fill_score": [],
        "section_title_alignment_score": [],
        "average_image_backed_panel_text_low_count": [],
        "average_underfilled_panel_count": [],
        "average_section_title_content_mismatch_count": [],
        "average_terse_figure_number_caption_count": [],
        "mechanical_screenshot_discount": [],
        "estimated_human_minutes_saved": [],
    }
    image_values: dict[str, list[float]] = {
        "average_white_space_ratio": [],
        "average_nonwhite_pixel_ratio": [],
        "average_longest_blank_vertical_run_ratio": [],
        "average_vertical_band_nonwhite_min": [],
        "average_edge_density": [],
        "average_empty_cell_ratio": [],
        "average_palette_complexity": [],
    }
    for result in metrics.get("results") or []:
        candidate = result.get("candidate") or {}
        html = candidate.get("html") or {}
        image = (candidate.get("image") or {})
        content_profile = candidate.get("content_value_profile") if isinstance(candidate.get("content_value_profile"), dict) else {}
        if not content_profile:
            comparison = result.get("comparison") if isinstance(result.get("comparison"), dict) else {}
            content_profile = comparison.get("content_value_profile") if isinstance(comparison.get("content_value_profile"), dict) else {}
        manual_work = content_profile.get("manual_work_proxy") if isinstance(content_profile.get("manual_work_proxy"), dict) else {}
        _append_float(html_values["average_visual_area_ratio"], html.get("visual_area_ratio"))
        _append_float(html_values["average_top_half_visual_area_ratio"], html.get("top_half_visual_area_ratio"))
        _append_float(html_values["average_text_area_ratio"], html.get("text_area_ratio"))
        _append_float(html_values["average_visible_word_count"], html.get("visible_text_word_count"))
        _append_float(html_values["average_leaf_visible_word_count"], html.get("leaf_visible_word_count"))
        _append_float(html_values["average_caption_like_text_count"], html.get("caption_like_text_count"))
        _append_float(html_values["mixed_panel_binding_score"], html.get("mixed_panel_binding_score"))
        _append_float(html_values["average_image_backed_panel_text_low_count"], html.get("image_backed_panel_text_low_count"))
        _append_float(html_values["average_underfilled_panel_count"], html.get("underfilled_panel_count"))
        _append_float(html_values["average_section_title_content_mismatch_count"], html.get("section_title_content_mismatch_count"))
        _append_float(html_values["average_terse_figure_number_caption_count"], html.get("terse_figure_number_caption_count"))
        _append_float(html_values["average_section_label_like_count"], html.get("section_label_like_count"))
        _append_float(html_values["average_image_layer_count"], html.get("image_layer_count"))
        _append_float(html_values["manual_work_proxy_overall"], manual_work.get("overall"))
        _append_float(html_values["semantic_synthesis_score"], manual_work.get("semantic_synthesis_score"))
        _append_float(html_values["information_architecture_proxy_score"], manual_work.get("information_architecture_score"))
        _append_float(html_values["native_reconstruction_score"], manual_work.get("native_reconstruction_score"))
        _append_float(html_values["text_density_quality"], manual_work.get("text_density_quality"))
        _append_float(html_values["paper_faithfulness_proxy"], manual_work.get("paper_faithfulness_proxy"))
        _append_float(html_values["narrative_coherence_proxy"], manual_work.get("narrative_coherence_proxy"))
        _append_float(html_values["source_visual_use"], manual_work.get("source_visual_use"))
        _append_float(html_values["panel_rule_score"], manual_work.get("panel_rule_score"))
        _append_float(html_values["image_panel_text_score"], manual_work.get("image_panel_text_score"))
        _append_float(html_values["panel_fill_score"], manual_work.get("panel_fill_score"))
        _append_float(html_values["section_title_alignment_score"], manual_work.get("section_title_alignment_score"))
        _append_float(html_values["mechanical_screenshot_discount"], manual_work.get("mechanical_screenshot_discount"))
        _append_float(html_values["estimated_human_minutes_saved"], manual_work.get("estimated_human_minutes_saved"))
        _append_float(image_values["average_white_space_ratio"], image.get("white_space_ratio"))
        _append_float(image_values["average_nonwhite_pixel_ratio"], image.get("nonwhite_pixel_ratio"))
        _append_float(image_values["average_longest_blank_vertical_run_ratio"], image.get("longest_blank_vertical_run_ratio"))
        _append_float(image_values["average_vertical_band_nonwhite_min"], image.get("vertical_band_nonwhite_min"))
        _append_float(image_values["average_edge_density"], image.get("edge_density"))
        _append_float(image_values["average_empty_cell_ratio"], image.get("empty_cell_ratio"))
        _append_float(image_values["average_palette_complexity"], image.get("palette_complexity"))
    summary: dict[str, Any] = {}
    for key, values in {**html_values, **image_values}.items():
        if values:
            summary[key] = round(sum(values) / len(values), 3)
    return summary


def _label_calibration_summary(metrics: dict[str, Any]) -> dict[str, Any]:
    calibration = metrics.get("label_calibration")
    if not isinstance(calibration, dict):
        return {}
    rows = [
        row for row in (calibration.get("rows") or [])
        if isinstance(row, dict)
    ]

    def row_count(label: str, predicate: Any) -> int:
        return sum(
            1 for row in rows
            if row.get("label") == label and predicate(row)
        )

    accuracy = calibration.get("accuracy")
    try:
        label_accuracy = round(float(accuracy), 3)
    except Exception:
        label_accuracy = None
    missed_axis_count = calibration.get("missed_axis_count")
    if missed_axis_count is None:
        missed_axis_count = sum(
            len(row.get("missed_expected_issue_axes") or [])
            for row in rows
        )
    summary: dict[str, Any] = {
        "label_case_count": _safe_int(calibration.get("case_count"), len(rows)),
        "label_matched_count": _safe_int(calibration.get("matched_count"), 0),
        "label_mismatch_count": _safe_int(calibration.get("mismatch_count"), 0),
        "label_missed_axis_count": _safe_int(missed_axis_count, 0),
        "label_positive_mismatch_count": _safe_int(
            calibration.get("positive_mismatch_count"),
            row_count("positive", lambda row: not row.get("match")),
        ),
        "label_near_miss_pass_count": _safe_int(
            calibration.get("near_miss_pass_count"),
            row_count("near_miss", lambda row: row.get("observed_verdict") == "pass"),
        ),
        "label_negative_pass_count": _safe_int(
            calibration.get("negative_pass_count"),
            row_count("negative", lambda row: row.get("observed_verdict") == "pass"),
        ),
        "label_negative_mismatch_count": _safe_int(
            calibration.get("negative_mismatch_count"),
            row_count("negative", lambda row: not row.get("match")),
        ),
    }
    if label_accuracy is not None:
        summary["label_accuracy"] = label_accuracy
    return summary


def _label_calibration_issues(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    calibration = metrics.get("label_calibration")
    if not isinstance(calibration, dict):
        return []
    issues: list[dict[str, Any]] = []
    for row in calibration.get("rows") or []:
        if not isinstance(row, dict) or row.get("match"):
            continue
        axes = ", ".join(str(axis) for axis in (row.get("missed_expected_issue_axes") or []))
        detail = f"; missed axes: {axes}" if axes else ""
        issues.append({
            "case": row.get("case") or "unknown",
            "id": "label_calibration_mismatch",
            "owner": "eval_calibration",
            "severity": "high",
            "message": (
                f"Label calibration expected {row.get('expected_verdict')} for "
                f"{row.get('label')} reference but observed {row.get('observed_verdict')}{detail}"
            ),
            "source": "label_calibration",
            "artifact_paths": {},
        })
    return issues


def _append_float(values: list[float], value: Any) -> None:
    try:
        values.append(float(value))
    except Exception:
        return


def _rollup_issue_counts(rollup: dict[str, Any] | None) -> dict[str, int]:
    counts = {"blocker": 0, "high": 0, "medium": 0, "low": 0}
    if not isinstance(rollup, dict):
        return counts
    issues = rollup.get("issues")
    if isinstance(issues, list):
        for issue in issues:
            if isinstance(issue, dict):
                severity = _severity(issue.get("severity"))
                counts[severity] += 1
        return counts
    if rollup.get("blocker_count") is not None:
        counts["blocker"] = _safe_int(rollup.get("blocker_count"), 0)
    return counts


def _average_scorecards(scorecards: list[dict[str, Any]]) -> dict[str, float]:
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for card in scorecards:
        for key, value in card.items():
            try:
                val = float(value)
            except Exception:
                continue
            sums[key] = sums.get(key, 0.0) + val
            counts[key] = counts.get(key, 0) + 1
    return {key: round(sums[key] / counts[key], 3) for key in sorted(sums) if counts.get(key)}


def _deterministic_scorecard_fallback(
    *,
    avg_score: float | None,
    dimension_summary: dict[str, Any],
    generation_authored_summary: dict[str, Any],
    ingest_curation_summary: dict[str, Any],
    issues: list[dict[str, Any]],
) -> dict[str, float]:
    """Conservative scorecard for clean deterministic pass runs without a critic JSON.

    The real critic scorecard remains authoritative. This fallback only prevents
    no-critic dogfood passes from being rejected solely because a rubric JSON was
    never emitted.
    """
    if avg_score is None or avg_score < 0.80:
        return {}
    if any(_severity(issue.get("severity")) in {"blocker", "high"} for issue in issues):
        return {}
    if _safe_float(generation_authored_summary.get("authored_html_case_count"), 0.0) <= 0:
        return {}
    if _safe_float(generation_authored_summary.get("authored_render_mode_ratio"), 0.0) < 0.99:
        return {}
    unprotected_dom_p0 = (
        generation_authored_summary.get("unprotected_dom_audit_p0_count")
        if generation_authored_summary.get("unprotected_dom_audit_p0_count") is not None
        else generation_authored_summary.get("dom_audit_p0_count")
    )
    if _safe_float(unprotected_dom_p0, 0.0) > 0:
        return {}
    if _safe_float(generation_authored_summary.get("gold_visual_density_regression_count"), 0.0) > 0:
        return {}
    if _safe_float(generation_authored_summary.get("gold_quality_floor_regression_count"), 0.0) > 0:
        return {}
    if _safe_float(generation_authored_summary.get("gold_composite_fail_count"), 0.0) > 0:
        return {}
    if _safe_float(generation_authored_summary.get("preview_fallback_count"), 0.0) > 0:
        return {}

    issue_ids = {str(issue.get("id") or "").lower() for issue in issues}
    issue_text = " ".join(issue_ids)
    medium_count = sum(1 for issue in issues if _severity(issue.get("severity")) == "medium")

    selected = _safe_float(generation_authored_summary.get("selected_source_asset_count"), 0.0)
    placed = _safe_float(generation_authored_summary.get("source_backed_dom_image_count"), 0.0)
    missing = _safe_float(generation_authored_summary.get("selected_source_asset_dom_missing_count"), 0.0)
    unbacked = _safe_float(generation_authored_summary.get("unbacked_source_image_count"), 0.0)
    provenance = _safe_float(generation_authored_summary.get("source_provenance_asset_count"), 0.0)
    ingest_score = _safe_float(ingest_curation_summary.get("ingest_score"), 0.0)
    selected_ratio = max(
        _safe_float(ingest_curation_summary.get("selected_visuals_placed_ratio"), 0.0),
        _safe_float(ingest_curation_summary.get("storyboard_selected_asset_placed_ratio"), 0.0),
    )
    evidence = 0.54
    if selected > 0 and missing == 0:
        evidence += 0.14
    if unbacked == 0:
        evidence += 0.10
    if provenance > 0:
        evidence += 0.04
    if selected > 0 and placed >= selected:
        evidence += 0.10
    elif placed > 0:
        evidence += 0.06
    evidence += min(0.08, max(0.0, ingest_score) * 0.08)
    evidence += min(0.06, max(0.0, selected_ratio) * 0.06)
    if "unbacked" in issue_text or ("source" in issue_text and "missing" in issue_text):
        evidence -= 0.08
    evidence -= min(0.06, medium_count * 0.015)

    words = _safe_float(dimension_summary.get("average_visible_word_count"), 0.0)
    captions = _safe_float(dimension_summary.get("average_caption_like_text_count"), 0.0)
    sections = _safe_float(dimension_summary.get("average_section_label_like_count"), 0.0)
    image_layers = _safe_float(dimension_summary.get("average_image_layer_count"), 0.0)
    visual_area = max(
        _safe_float(dimension_summary.get("average_visual_area_ratio"), 0.0),
        _safe_float(generation_authored_summary.get("figure_area_ratio"), 0.0),
    )
    information = 0.52
    if words >= 260:
        information += 0.14
    elif words >= 180:
        information += 0.08
    if captions >= 6:
        information += 0.10
    elif captions >= 3:
        information += 0.06
    if sections >= 6:
        information += 0.08
    elif sections >= 4:
        information += 0.04
    if image_layers >= 6:
        information += 0.06
    elif image_layers >= 3:
        information += 0.03
    mixed_binding = _safe_float(dimension_summary.get("mixed_panel_binding_score"), 0.0)
    text_density = _safe_float(dimension_summary.get("text_density_quality"), 0.0)
    if mixed_binding >= 0.70:
        information += 0.07
    elif mixed_binding >= 0.45:
        information += 0.04
    if text_density >= 0.70:
        information += 0.05
    elif text_density >= 0.50:
        information += 0.03
    if visual_area >= 0.45 and mixed_binding >= 0.45:
        information += 0.03
    if any(token in issue_text for token in ("paper-visible-text-low", "caption-generic", "caption")):
        information -= 0.08
    information -= min(0.05, medium_count * 0.01)

    editability = 0.68
    if _safe_float(generation_authored_summary.get("artifact_consistency_ok_ratio"), 0.0) >= 0.99:
        editability += 0.08
    if _safe_float(generation_authored_summary.get("manifest_matches_final_ratio"), 0.0) >= 0.99:
        editability += 0.05
    if _safe_float(generation_authored_summary.get("preview_matches_final_ratio"), 0.0) >= 0.99:
        editability += 0.05
    if _safe_float(generation_authored_summary.get("dom_audit_matches_final_ratio"), 0.0) >= 0.99:
        editability += 0.05
    if _safe_float(generation_authored_summary.get("caption_overlap_count"), 0.0) == 0:
        editability += 0.03

    layout = 0.72
    if visual_area >= 0.32 and mixed_binding >= 0.45:
        layout += 0.04
    if _safe_float(generation_authored_summary.get("root_overflow_count"), 0.0) == 0:
        layout += 0.04
    if _safe_float(generation_authored_summary.get("block_out_of_bounds_count"), 0.0) == 0:
        layout += 0.04
    if medium_count:
        layout -= min(0.05, medium_count * 0.01)

    manual_effort = _safe_float(dimension_summary.get("manual_work_proxy_overall"), 0.0)
    if manual_effort <= 0:
        manual_effort = (
            0.38 * information
            + 0.32 * layout
            + 0.30 * evidence
        )

    return {
        "editability_export": _clamp_score(editability, hi=0.88),
        "evidence_use": _clamp_score(evidence, hi=0.88),
        "information_architecture": _clamp_score(information, hi=0.88),
        "human_effort_saved": _clamp_score(manual_effort, hi=0.88),
        "poster_impact": _clamp_score(layout, lo=0.70, hi=0.84),
        "typography_craft": _clamp_score(layout, lo=0.70, hi=0.84),
    }


def _clamp_score(value: float, *, lo: float = 0.0, hi: float = 1.0) -> float:
    return round(max(lo, min(hi, value)), 3)


def _confidence_label(*, candidate_count: int, low_confidence: bool) -> str:
    if candidate_count <= 0:
        return "none"
    if candidate_count == 1:
        return "very_low"
    if low_confidence:
        return "low"
    if candidate_count < 4:
        return "medium"
    return "high"


def _owner_count_deltas(
    before_counts: dict[str, Any],
    after_counts: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    owners = sorted(set(before_counts) | set(after_counts) | set(OWNER_TAXONOMY))
    deltas: dict[str, dict[str, Any]] = {}
    for owner in owners:
        before = before_counts.get(owner) if isinstance(before_counts.get(owner), dict) else {}
        after = after_counts.get(owner) if isinstance(after_counts.get(owner), dict) else {}
        owner_delta: dict[str, Any] = {}
        for key in ("total", "blocker", "high", "medium", "low"):
            owner_delta[key] = _delta(before.get(key, 0), after.get(key, 0))
        deltas[owner] = owner_delta
    return deltas


def _owner_delta_score(owner_delta: dict[str, Any]) -> float:
    return (
        _numeric(owner_delta.get("blocker")) * 100.0
        + _numeric(owner_delta.get("high")) * 20.0
        + _numeric(owner_delta.get("medium")) * 4.0
        + _numeric(owner_delta.get("low"))
    )


def _hard_acceptance_regressions(
    deltas: dict[str, Any],
    *,
    after: dict[str, Any],
    owner_delta: dict[str, Any],
    low_confidence: bool,
) -> list[str]:
    regressions: list[str] = []
    if int(after.get("final_not_authored_count") or 0) > 0:
        regressions.append(f"final academic poster is not authored HTML for {int(after.get('final_not_authored_count') or 0)} case(s)")
    if int(after.get("authored_artifact_mismatch_count") or 0) > 0:
        regressions.append(f"authored final artifact manifest/audit mismatch in {int(after.get('authored_artifact_mismatch_count') or 0)} case(s)")
    if int(after.get("stale_authored_audit_count") or 0) > 0:
        regressions.append(f"stale authored audit artifact present in {int(after.get('stale_authored_audit_count') or 0)} case(s)")
    if int(after.get("gold_visual_density_regression_count") or 0) > 0:
        regressions.append(
            f"gold visual density regression count is {int(after.get('gold_visual_density_regression_count') or 0)}"
        )
    if int(after.get("gold_quality_floor_regression_count") or 0) > 0:
        regressions.append(
            f"gold quality floor regression count is {int(after.get('gold_quality_floor_regression_count') or 0)}"
        )
    if int(after.get("gold_composite_fail_count") or 0) > 0:
        regressions.append(f"gold composite fail count is {int(after.get('gold_composite_fail_count') or 0)}")
    unprotected_value = (
        after.get("unprotected_dom_audit_p0_count")
        if after.get("unprotected_dom_audit_p0_count") is not None
        else after.get("dom_audit_p0_count")
    )
    unprotected_dom_p0 = int(unprotected_value or 0)
    if unprotected_dom_p0 > 0:
        regressions.append(f"unprotected DOM audit P0 count is {unprotected_dom_p0}")
    if int(after.get("preview_fallback_count") or 0) > 0:
        regressions.append(f"preview fallback count is {int(after.get('preview_fallback_count') or 0)}")
    if int(after.get("unbacked_source_image_count") or 0) > 0:
        regressions.append(f"unbacked source image count is {int(after.get('unbacked_source_image_count') or 0)}")
    if int(after.get("page_like_source_dom_image_count") or after.get("page_like_source_figure_count") or 0) > 0:
        regressions.append(
            f"page-like source figure count is {int(after.get('page_like_source_dom_image_count') or after.get('page_like_source_figure_count') or 0)}"
        )
    if int(after.get("panel_internal_underfilled_p0_count") or after.get("panel_underfilled_count") or 0) > 0:
        regressions.append(
            f"underfilled panel interior count is {int(after.get('panel_internal_underfilled_p0_count') or after.get('panel_underfilled_count') or 0)}"
        )
    if int(after.get("panel_visual_underfilled_p0_count") or 0) > 0:
        regressions.append(
            f"visually underfilled panel count is {int(after.get('panel_visual_underfilled_p0_count') or 0)}"
        )
    if int(after.get("selected_source_asset_dom_missing_count") or 0) > 0:
        regressions.append(f"selected source asset DOM missing count is {int(after.get('selected_source_asset_dom_missing_count') or 0)}")
    blocker_delta = deltas.get("blocker_count")
    if isinstance(blocker_delta, (int, float)) and blocker_delta > 0:
        regressions.append(f"blocker_count increased by {blocker_delta}")
    pass_delta = deltas.get("pass_count")
    if isinstance(pass_delta, (int, float)) and pass_delta < 0:
        regressions.append(f"pass_count regressed by {pass_delta}")
    finalized_delta = deltas.get("finalized_count")
    if isinstance(finalized_delta, (int, float)) and finalized_delta < 0:
        regressions.append(f"finalized_count regressed by {finalized_delta}")
    score_delta = deltas.get("average_proxy_score")
    proxy_floor = -0.08 if low_confidence else -0.05
    if isinstance(score_delta, (int, float)) and score_delta < proxy_floor:
        regressions.append(f"average_proxy_score regressed by {score_delta:.3f}")
    ingest_delta = deltas.get("ingest_score")
    if isinstance(ingest_delta, (int, float)) and ingest_delta < -0.08:
        regressions.append(f"ingest_score regressed by {ingest_delta:.3f}")
    positive_label_delta = deltas.get("label_positive_mismatch_count")
    if isinstance(positive_label_delta, (int, float)) and positive_label_delta > 0:
        regressions.append(f"positive label mismatch count increased by {positive_label_delta:g}")
    negative_label_delta = deltas.get("label_negative_mismatch_count")
    if isinstance(negative_label_delta, (int, float)) and negative_label_delta > 0:
        regressions.append(f"negative label mismatch count increased by {negative_label_delta:g}")
    authored_delta = deltas.get("authored_render_mode_ratio")
    if isinstance(authored_delta, (int, float)) and authored_delta < 0:
        regressions.append(f"authored HTML render ratio regressed by {authored_delta:.3f}")
    artifact_consistency_delta = deltas.get("artifact_consistency_ok_ratio")
    if isinstance(artifact_consistency_delta, (int, float)) and artifact_consistency_delta < 0:
        regressions.append(f"authored artifact consistency ratio regressed by {artifact_consistency_delta:.3f}")
    manifest_match_delta = deltas.get("manifest_matches_final_ratio")
    if isinstance(manifest_match_delta, (int, float)) and manifest_match_delta < 0:
        regressions.append(f"manifest/final match ratio regressed by {manifest_match_delta:.3f}")
    preview_match_delta = deltas.get("preview_matches_final_ratio")
    if isinstance(preview_match_delta, (int, float)) and preview_match_delta < 0:
        regressions.append(f"preview/final match ratio regressed by {preview_match_delta:.3f}")
    dom_match_delta = deltas.get("dom_audit_matches_final_ratio")
    if isinstance(dom_match_delta, (int, float)) and dom_match_delta < 0:
        regressions.append(f"DOM audit/final match ratio regressed by {dom_match_delta:.3f}")
    playwright_delta = deltas.get("dom_backend_playwright_ratio")
    if isinstance(playwright_delta, (int, float)) and playwright_delta < 0:
        regressions.append(f"DOM Playwright audit ratio regressed by {playwright_delta:.3f}")
    dom_p0_delta = deltas.get("unprotected_dom_audit_p0_count")
    if isinstance(dom_p0_delta, (int, float)) and dom_p0_delta > 0:
        regressions.append(f"unprotected DOM audit P0 count increased by {dom_p0_delta:g}")
    gold_density_delta = deltas.get("gold_visual_density_regression_count")
    if isinstance(gold_density_delta, (int, float)) and gold_density_delta > 0:
        regressions.append(f"gold visual density regression count increased by {gold_density_delta:g}")
    gold_quality_delta = deltas.get("gold_quality_floor_regression_count")
    if isinstance(gold_quality_delta, (int, float)) and gold_quality_delta > 0:
        regressions.append(f"gold quality floor regression count increased by {gold_quality_delta:g}")
    gold_composite_delta = deltas.get("gold_composite_fail_count")
    if isinstance(gold_composite_delta, (int, float)) and gold_composite_delta > 0:
        regressions.append(f"gold composite fail count increased by {gold_composite_delta:g}")
    preview_fallback_delta = deltas.get("preview_fallback_count")
    if isinstance(preview_fallback_delta, (int, float)) and preview_fallback_delta > 0:
        regressions.append(f"preview fallback count increased by {preview_fallback_delta:g}")
    image_not_loaded_delta = deltas.get("image_not_loaded_count")
    if isinstance(image_not_loaded_delta, (int, float)) and image_not_loaded_delta > 0:
        regressions.append(f"image-not-loaded count increased by {image_not_loaded_delta:g}")
    root_overflow_delta = deltas.get("root_overflow_count")
    if isinstance(root_overflow_delta, (int, float)) and root_overflow_delta > 0:
        regressions.append(f"root overflow count increased by {root_overflow_delta:g}")
    target_owner_score = _owner_delta_score(owner_delta)
    if target_owner_score > 0:
        regressions.append("target owner severity increased")
    return regressions


def _dimension_improvements(deltas: dict[str, Any]) -> list[dict[str, Any]]:
    improvements: list[dict[str, Any]] = []
    thresholds = {
        "average_proxy_score": 0.04,
        "ingest_score": 0.03,
        "visual_role_coverage": 0.03,
        "brief_section_coverage": 0.03,
        "selected_visuals_placed_ratio": 0.03,
        "average_visual_area_ratio": 0.06,
        "average_top_half_visual_area_ratio": 0.08,
        "average_nonwhite_pixel_ratio": 0.03,
        "average_vertical_band_nonwhite_min": 0.03,
        "average_leaf_visible_word_count": 100.0,
        "leaf_visible_word_count": 100.0,
        "gold_composite_score": 0.04,
        "average_caption_like_text_count": 0.5,
        "average_image_layer_count": 0.5,
        "mixed_panel_binding_score": 0.08,
        "panel_rule_score": 0.05,
        "image_panel_text_score": 0.06,
        "panel_fill_score": 0.05,
        "section_title_alignment_score": 0.04,
        "text_density_quality": 0.05,
        "manual_work_proxy_overall": 0.04,
        "semantic_synthesis_score": 0.05,
        "native_reconstruction_score": 0.05,
        "paper_faithfulness_proxy": 0.05,
        "narrative_coherence_proxy": 0.05,
        "source_visual_use": 0.05,
        "estimated_human_minutes_saved": 8.0,
        "scorecard_evidence_use": 0.05,
        "scorecard_information_architecture": 0.05,
        "scorecard_human_effort_saved": 0.05,
        "scorecard_visual_hierarchy": 0.05,
        "label_accuracy": 0.08,
        "authored_render_mode_ratio": 0.25,
        "artifact_consistency_ok_ratio": 0.25,
        "manifest_matches_final_ratio": 0.25,
        "preview_matches_final_ratio": 0.25,
        "dom_audit_matches_final_ratio": 0.25,
        "dom_backend_playwright_ratio": 0.25,
        "figure_area_ratio": 0.03,
    }
    for key, threshold in thresholds.items():
        delta = deltas.get(key)
        if isinstance(delta, (int, float)) and delta >= threshold:
            improvements.append({"metric": key, "delta": delta})
    for key in (
        "blocker_count",
        "high_count",
        "major_issue_count",
        "label_mismatch_count",
        "label_missed_axis_count",
        "label_near_miss_pass_count",
        "label_negative_pass_count",
        "final_not_authored_count",
        "authored_artifact_mismatch_count",
        "stale_authored_audit_count",
        "unprotected_dom_audit_p0_count",
        "gold_visual_density_regression_count",
        "gold_quality_floor_regression_count",
        "gold_composite_fail_count",
        "average_longest_blank_vertical_run_ratio",
        "preview_fallback_count",
        "image_not_loaded_count",
        "block_out_of_bounds_count",
        "root_overflow_count",
        "caption_overlap_count",
        "footer_overlap_count",
        "average_image_backed_panel_text_low_count",
        "average_underfilled_panel_count",
        "average_section_title_content_mismatch_count",
        "average_terse_figure_number_caption_count",
    ):
        delta = deltas.get(key)
        if isinstance(delta, (int, float)) and delta < 0:
            improvements.append({"metric": key, "delta": delta})
    return improvements


def _dimension_regressions(deltas: dict[str, Any]) -> list[dict[str, Any]]:
    regressions: list[dict[str, Any]] = []
    thresholds = {
        "average_proxy_score": -0.05,
        "ingest_score": -0.08,
        "selected_visuals_placed_ratio": -0.05,
        "average_visual_area_ratio": -0.08,
        "average_top_half_visual_area_ratio": -0.10,
        "average_nonwhite_pixel_ratio": -0.03,
        "average_vertical_band_nonwhite_min": -0.03,
        "average_leaf_visible_word_count": -120.0,
        "leaf_visible_word_count": -120.0,
        "gold_composite_score": -0.04,
        "average_caption_like_text_count": -1.0,
        "average_image_layer_count": -1.0,
        "mixed_panel_binding_score": -0.10,
        "panel_rule_score": -0.06,
        "image_panel_text_score": -0.08,
        "panel_fill_score": -0.06,
        "section_title_alignment_score": -0.05,
        "text_density_quality": -0.06,
        "manual_work_proxy_overall": -0.05,
        "semantic_synthesis_score": -0.05,
        "native_reconstruction_score": -0.05,
        "paper_faithfulness_proxy": -0.06,
        "narrative_coherence_proxy": -0.06,
        "source_visual_use": -0.06,
        "estimated_human_minutes_saved": -10.0,
        "scorecard_evidence_use": -0.05,
        "scorecard_information_architecture": -0.05,
        "scorecard_human_effort_saved": -0.05,
        "scorecard_visual_hierarchy": -0.05,
        "label_accuracy": -0.08,
        "authored_render_mode_ratio": -0.25,
        "artifact_consistency_ok_ratio": -0.25,
        "manifest_matches_final_ratio": -0.25,
        "preview_matches_final_ratio": -0.25,
        "dom_audit_matches_final_ratio": -0.25,
        "dom_backend_playwright_ratio": -0.25,
        "figure_area_ratio": -0.04,
    }
    for key, threshold in thresholds.items():
        delta = deltas.get(key)
        if isinstance(delta, (int, float)) and delta <= threshold:
            regressions.append({"metric": key, "delta": delta})
    for key in (
        "blocker_count",
        "high_count",
        "major_issue_count",
        "label_mismatch_count",
        "label_missed_axis_count",
        "label_near_miss_pass_count",
        "label_negative_pass_count",
        "label_positive_mismatch_count",
        "label_negative_mismatch_count",
        "final_not_authored_count",
        "authored_artifact_mismatch_count",
        "stale_authored_audit_count",
        "unprotected_dom_audit_p0_count",
        "gold_visual_density_regression_count",
        "gold_quality_floor_regression_count",
        "gold_composite_fail_count",
        "preview_fallback_count",
        "image_not_loaded_count",
        "block_out_of_bounds_count",
        "root_overflow_count",
        "caption_overlap_count",
        "footer_overlap_count",
        "average_image_backed_panel_text_low_count",
        "average_underfilled_panel_count",
        "average_section_title_content_mismatch_count",
        "average_terse_figure_number_caption_count",
        "average_longest_blank_vertical_run_ratio",
    ):
        delta = deltas.get(key)
        if isinstance(delta, (int, float)) and delta > 0:
            regressions.append({"metric": key, "delta": delta})
    return regressions


def _scorecard_dimension_improved(deltas: dict[str, Any]) -> bool:
    return any(
        isinstance(deltas.get(key), (int, float)) and float(deltas[key]) >= 0.05
        for key in (
            "scorecard_evidence_use",
            "scorecard_information_architecture",
            "scorecard_human_effort_saved",
            "scorecard_visual_hierarchy",
            "scorecard_editability_export",
        )
    )


def _label_calibration_improvement_reason(deltas: dict[str, Any]) -> str | None:
    mismatch_delta = deltas.get("label_mismatch_count")
    if isinstance(mismatch_delta, (int, float)) and mismatch_delta <= -1:
        return f"Label calibration mismatch count decreased by {abs(mismatch_delta):g}."
    missed_axis_delta = deltas.get("label_missed_axis_count")
    if isinstance(missed_axis_delta, (int, float)) and missed_axis_delta <= -1:
        return f"Label calibration missed-axis count decreased by {abs(missed_axis_delta):g}."
    accuracy_delta = deltas.get("label_accuracy")
    if isinstance(accuracy_delta, (int, float)) and accuracy_delta >= 0.08:
        return f"Label calibration accuracy improved by {accuracy_delta:.3f}."
    return None


def _authored_improvement_reason(deltas: dict[str, Any], *, owner: str) -> str | None:
    if owner in {"harness_reliability", "renderer_export"}:
        for key, label in (
            ("final_not_authored_count", "Non-authored final"),
            ("authored_artifact_mismatch_count", "Authored artifact mismatch"),
            ("stale_authored_audit_count", "Stale authored audit"),
        ):
            delta = deltas.get(key)
            if isinstance(delta, (int, float)) and delta < 0:
                return f"{label} count decreased by {abs(delta):g}."
        for key, label in (
            ("artifact_consistency_ok_ratio", "Artifact consistency"),
            ("manifest_matches_final_ratio", "Manifest/final match"),
            ("preview_matches_final_ratio", "Preview/final match"),
            ("dom_audit_matches_final_ratio", "DOM audit/final match"),
        ):
            delta = deltas.get(key)
            if isinstance(delta, (int, float)) and delta >= 0.25:
                return f"{label} ratio improved by {delta:.3f}."
        authored_delta = deltas.get("authored_render_mode_ratio")
        if isinstance(authored_delta, (int, float)) and authored_delta >= 0.25:
            return f"Authored HTML render ratio improved by {authored_delta:.3f}."
    if owner == "renderer_export":
        dom_p0_delta = deltas.get("unprotected_dom_audit_p0_count")
        if isinstance(dom_p0_delta, (int, float)) and dom_p0_delta < 0:
            return f"Unprotected DOM audit P0 count decreased by {abs(dom_p0_delta):g}."
        fallback_delta = deltas.get("preview_fallback_count")
        if isinstance(fallback_delta, (int, float)) and fallback_delta < 0:
            return f"Preview fallback count decreased by {abs(fallback_delta):g}."
        playwright_delta = deltas.get("dom_backend_playwright_ratio")
        if isinstance(playwright_delta, (int, float)) and playwright_delta >= 0.25:
            return f"DOM Playwright audit ratio improved by {playwright_delta:.3f}."
    if owner == "layout_storyboard":
        for key, label in (
            ("root_overflow_count", "Root overflow"),
            ("block_out_of_bounds_count", "Out-of-bounds block"),
            ("caption_overlap_count", "Caption overlap"),
            ("footer_overlap_count", "Footer overlap"),
            ("unprotected_dom_audit_p0_count", "Unprotected DOM audit P0"),
            ("gold_visual_density_regression_count", "Gold visual density regression"),
        ):
            delta = deltas.get(key)
            if isinstance(delta, (int, float)) and delta < 0:
                return f"{label} count decreased by {abs(delta):g}."
        density_delta = deltas.get("average_nonwhite_pixel_ratio")
        if isinstance(density_delta, (int, float)) and density_delta >= 0.03:
            return f"Non-white pixel density improved by {density_delta:.3f}."
        blank_delta = deltas.get("average_longest_blank_vertical_run_ratio")
        if isinstance(blank_delta, (int, float)) and blank_delta <= -0.03:
            return f"Longest blank vertical band decreased by {abs(blank_delta):.3f}."
        figure_delta = deltas.get("figure_area_ratio")
        if isinstance(figure_delta, (int, float)) and figure_delta >= 0.03:
            return f"DOM figure area ratio improved by {figure_delta:.3f}."
    if owner == "visual_curation":
        image_delta = deltas.get("image_not_loaded_count")
        if isinstance(image_delta, (int, float)) and image_delta < 0:
            return f"Image-not-loaded count decreased by {abs(image_delta):g}."
        figure_delta = deltas.get("figure_area_ratio")
        if isinstance(figure_delta, (int, float)) and figure_delta >= 0.03:
            return f"DOM figure area ratio improved by {figure_delta:.3f}."
    return None


def _content_strategy_improvement_reason(deltas: dict[str, Any]) -> str | None:
    for key, label, threshold in (
        ("manual_work_proxy_overall", "Manual-work proxy", 0.04),
        ("semantic_synthesis_score", "Semantic synthesis", 0.05),
        ("text_density_quality", "Text density quality", 0.05),
        ("paper_faithfulness_proxy", "Paper faithfulness", 0.05),
        ("narrative_coherence_proxy", "Narrative coherence", 0.05),
        ("panel_fill_score", "Panel fill", 0.05),
        ("section_title_alignment_score", "Section title/content alignment", 0.04),
        ("native_reconstruction_score", "Native reconstruction", 0.05),
        ("scorecard_human_effort_saved", "Human-effort scorecard", 0.05),
    ):
        delta = deltas.get(key)
        if isinstance(delta, (int, float)) and delta >= threshold:
            return f"{label} improved by {delta:.3f}."
    minutes_delta = deltas.get("estimated_human_minutes_saved")
    if isinstance(minutes_delta, (int, float)) and minutes_delta >= 8.0:
        return f"Estimated human minutes saved improved by {minutes_delta:.1f}."
    return None


def _layout_dimension_improved(deltas: dict[str, Any]) -> bool:
    return any(
        isinstance(deltas.get(key), (int, float)) and float(deltas[key]) >= threshold
        for key, threshold in {
            "mixed_panel_binding_score": 0.08,
            "panel_rule_score": 0.05,
            "image_panel_text_score": 0.06,
            "panel_fill_score": 0.05,
            "section_title_alignment_score": 0.04,
            "figure_area_ratio": 0.03,
            "average_caption_like_text_count": 0.5,
            "average_image_layer_count": 0.5,
        }.items()
    )


def _positive_delta(value: Any) -> bool:
    return isinstance(value, (int, float)) and value > 0


def _numeric(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _regression_flags(deltas: dict[str, Any], *, after: dict[str, Any] | None = None) -> list[str]:
    flags: list[str] = []
    after = after or {}
    if int(after.get("final_not_authored_count") or 0) > 0:
        flags.append(f"final_not_authored_count is {int(after.get('final_not_authored_count') or 0)}")
    if int(after.get("authored_artifact_mismatch_count") or 0) > 0:
        flags.append(f"authored_artifact_mismatch_count is {int(after.get('authored_artifact_mismatch_count') or 0)}")
    if int(after.get("stale_authored_audit_count") or 0) > 0:
        flags.append(f"stale_authored_audit_count is {int(after.get('stale_authored_audit_count') or 0)}")
    if int(after.get("gold_visual_density_regression_count") or 0) > 0:
        flags.append(f"gold_visual_density_regression_count is {int(after.get('gold_visual_density_regression_count') or 0)}")
    if int(after.get("gold_quality_floor_regression_count") or 0) > 0:
        flags.append(f"gold_quality_floor_regression_count is {int(after.get('gold_quality_floor_regression_count') or 0)}")
    if int(after.get("gold_composite_fail_count") or 0) > 0:
        flags.append(f"gold_composite_fail_count is {int(after.get('gold_composite_fail_count') or 0)}")
    unprotected_value = (
        after.get("unprotected_dom_audit_p0_count")
        if after.get("unprotected_dom_audit_p0_count") is not None
        else after.get("dom_audit_p0_count")
    )
    if int(unprotected_value or 0) > 0:
        flags.append(f"unprotected_dom_audit_p0_count is {int(unprotected_value or 0)}")
    if int(after.get("preview_fallback_count") or 0) > 0:
        flags.append(f"preview_fallback_count is {int(after.get('preview_fallback_count') or 0)}")
    if int(after.get("unbacked_source_image_count") or 0) > 0:
        flags.append(f"unbacked_source_image_count is {int(after.get('unbacked_source_image_count') or 0)}")
    if int(after.get("page_like_source_dom_image_count") or after.get("page_like_source_figure_count") or 0) > 0:
        flags.append(
            f"page_like_source_figure_count is {int(after.get('page_like_source_dom_image_count') or after.get('page_like_source_figure_count') or 0)}"
        )
    if int(after.get("panel_internal_underfilled_p0_count") or after.get("panel_underfilled_count") or 0) > 0:
        flags.append(
            f"panel_internal_underfilled_count is {int(after.get('panel_internal_underfilled_p0_count') or after.get('panel_underfilled_count') or 0)}"
        )
    if int(after.get("panel_visual_underfilled_p0_count") or 0) > 0:
        flags.append(
            f"panel_visual_underfilled_p0_count is {int(after.get('panel_visual_underfilled_p0_count') or 0)}"
        )
    if int(after.get("selected_source_asset_dom_missing_count") or 0) > 0:
        flags.append(f"selected_source_asset_dom_missing_count is {int(after.get('selected_source_asset_dom_missing_count') or 0)}")
    score_delta = deltas.get("average_proxy_score")
    if isinstance(score_delta, (int, float)) and score_delta < -0.05:
        flags.append(f"average_proxy_score regressed by {score_delta:.3f}")
    blocker_delta = deltas.get("blocker_count")
    if isinstance(blocker_delta, (int, float)) and blocker_delta > 0:
        flags.append(f"blocker_count increased by {blocker_delta}")
    ingest_delta = deltas.get("ingest_score")
    if isinstance(ingest_delta, (int, float)) and ingest_delta < -0.08:
        flags.append(f"ingest_score regressed by {ingest_delta:.3f}")
    label_accuracy_delta = deltas.get("label_accuracy")
    if isinstance(label_accuracy_delta, (int, float)) and label_accuracy_delta < -0.08:
        flags.append(f"label_accuracy regressed by {label_accuracy_delta:.3f}")
    label_mismatch_delta = deltas.get("label_mismatch_count")
    if isinstance(label_mismatch_delta, (int, float)) and label_mismatch_delta > 0:
        flags.append(f"label_mismatch_count increased by {label_mismatch_delta:g}")
    authored_delta = deltas.get("authored_render_mode_ratio")
    if isinstance(authored_delta, (int, float)) and authored_delta < 0:
        flags.append(f"authored_render_mode_ratio regressed by {authored_delta:.3f}")
    artifact_consistency_delta = deltas.get("artifact_consistency_ok_ratio")
    if isinstance(artifact_consistency_delta, (int, float)) and artifact_consistency_delta < 0:
        flags.append(f"artifact_consistency_ok_ratio regressed by {artifact_consistency_delta:.3f}")
    manifest_match_delta = deltas.get("manifest_matches_final_ratio")
    if isinstance(manifest_match_delta, (int, float)) and manifest_match_delta < 0:
        flags.append(f"manifest_matches_final_ratio regressed by {manifest_match_delta:.3f}")
    preview_match_delta = deltas.get("preview_matches_final_ratio")
    if isinstance(preview_match_delta, (int, float)) and preview_match_delta < 0:
        flags.append(f"preview_matches_final_ratio regressed by {preview_match_delta:.3f}")
    dom_match_delta = deltas.get("dom_audit_matches_final_ratio")
    if isinstance(dom_match_delta, (int, float)) and dom_match_delta < 0:
        flags.append(f"dom_audit_matches_final_ratio regressed by {dom_match_delta:.3f}")
    dom_p0_delta = deltas.get("unprotected_dom_audit_p0_count")
    if isinstance(dom_p0_delta, (int, float)) and dom_p0_delta > 0:
        flags.append(f"unprotected_dom_audit_p0_count increased by {dom_p0_delta:g}")
    gold_density_delta = deltas.get("gold_visual_density_regression_count")
    if isinstance(gold_density_delta, (int, float)) and gold_density_delta > 0:
        flags.append(f"gold_visual_density_regression_count increased by {gold_density_delta:g}")
    gold_quality_delta = deltas.get("gold_quality_floor_regression_count")
    if isinstance(gold_quality_delta, (int, float)) and gold_quality_delta > 0:
        flags.append(f"gold_quality_floor_regression_count increased by {gold_quality_delta:g}")
    gold_composite_delta = deltas.get("gold_composite_fail_count")
    if isinstance(gold_composite_delta, (int, float)) and gold_composite_delta > 0:
        flags.append(f"gold_composite_fail_count increased by {gold_composite_delta:g}")
    preview_fallback_delta = deltas.get("preview_fallback_count")
    if isinstance(preview_fallback_delta, (int, float)) and preview_fallback_delta > 0:
        flags.append(f"preview_fallback_count increased by {preview_fallback_delta:g}")
    image_not_loaded_delta = deltas.get("image_not_loaded_count")
    if isinstance(image_not_loaded_delta, (int, float)) and image_not_loaded_delta > 0:
        flags.append(f"image_not_loaded_count increased by {image_not_loaded_delta:g}")
    root_overflow_delta = deltas.get("root_overflow_count")
    if isinstance(root_overflow_delta, (int, float)) and root_overflow_delta > 0:
        flags.append(f"root_overflow_count increased by {root_overflow_delta:g}")
    return flags


def _delta(before: Any, after: Any) -> Any:
    if isinstance(before, bool) or isinstance(after, bool):
        return None
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        return round(float(after) - float(before), 3)
    return None


def _artifact_paths(result: dict[str, Any]) -> dict[str, str]:
    candidate = result.get("candidate") or {}
    generation = candidate.get("generation") or {}
    run_dir = _candidate_run_dir(candidate)
    out = {
        "preview": candidate.get("preview_path"),
        "html": candidate.get("html_path"),
        "run_dir": str(run_dir) if run_dir else generation.get("run_dir"),
        "log": generation.get("log_path"),
    }
    return {key: str(value) for key, value in out.items() if value}


def _candidate_run_dir(candidate: dict[str, Any]) -> Path | None:
    generation = candidate.get("generation") if isinstance(candidate, dict) else None
    if isinstance(generation, dict) and generation.get("run_dir"):
        return Path(str(generation.get("run_dir"))).expanduser()
    candidates: list[Path] = []
    for key in ("path", "html_path", "preview_path"):
        raw = candidate.get(key) if isinstance(candidate, dict) else None
        if not raw:
            continue
        path = Path(str(raw)).expanduser()
        candidates.extend(_run_dir_candidates_from_path(path))
    scored = [
        (_run_dir_marker_score(path), path)
        for path in candidates
        if path and path.exists()
    ]
    scored = [(score, path) for score, path in scored if score > 0]
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], -len(item[1].parts)), reverse=True)
    return scored[0][1]


def _run_dir_candidates_from_path(path: Path) -> list[Path]:
    base = path.parent if path.suffix else path
    candidates = [base]
    if base.name == "final":
        candidates.append(base.parent)
    if base.parent.name == "final":
        candidates.append(base.parent.parent)
    if re.match(r"iter_\d+$", base.name) and base.parent.name == "composites":
        candidates.append(base.parent.parent)
    if base.name == "composites":
        candidates.append(base.parent)
    candidates.append(base.parent)
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return unique


def _run_dir_marker_score(path: Path) -> int:
    score = 0
    for name in (
        "poster_content_brief.json",
        "poster_plan_contract.json",
        "poster_contract_preflight.json",
        "design_spec.json",
    ):
        if (path / name).exists():
            score += 3
    if (path / "final").is_dir():
        score += 2
    if (path / "composites").is_dir():
        score += 2
    if (path / "layers").is_dir():
        score += 1
    return score


def _owner_for_feedback(finding: dict[str, Any]) -> str:
    source = str(finding.get("source") or "")
    stage = str(finding.get("stage") or "")
    category = str(finding.get("category") or "")
    if source == "paper_poster_dom":
        raw = finding.get("raw") if isinstance(finding.get("raw"), dict) else finding
        return _owner_for_dom_finding(raw)
    if source == "poster_contract":
        if stage == "visual_curation":
            return "visual_curation"
        if stage == "typography_system":
            return "typography_system"
        if stage == "content_strategy":
            return "content_strategy"
        return "layout_storyboard"
    if source in {"paper_density", "frame_layout", "layout_grounding"}:
        return "layout_storyboard"
    if source in {"paper_information", "html_artifact_contract"}:
        return "designer_contract"
    if source in {"quality_lint", "text_overlap", "visual_reference"}:
        return "deterministic_env_feedback"
    if source == "critic":
        if category == "typography":
            return "typography_system"
        return "critic_rubric"
    return "deterministic_env_feedback"


def _owner_for_dom_finding(finding: dict[str, Any]) -> str:
    issue_id = str(finding.get("id") or "").lower()
    repair_route = str(finding.get("repair_route") or "").lower()
    if issue_id in {"candidate_final_not_authored_html", "candidate_stale_authored_audit_present"}:
        return "harness_reliability"
    if issue_id in {"candidate_authored_artifact_mismatch"}:
        return "renderer_export"
    if "image-not-loaded" in issue_id or "undeclared-asset" in issue_id:
        return "visual_curation"
    if any(token in issue_id for token in ("overflow", "out-of-bounds", "caption-overlap", "footer-overlap", "figure-area-low", "size-mismatch")):
        return "layout_storyboard"
    if (
        issue_id.startswith("authored-html-")
        or "sanitizer" in issue_id
        or "remote-url" in issue_id
        or "unsafe" in issue_id
        or "unknown-block" in issue_id
        or "missing-block" in issue_id
        or repair_route in {"revise_authored_html", "shrink_text"}
    ):
        return "renderer_export"
    return "renderer_export"


def _is_local_dom_repair_finding(finding: dict[str, Any]) -> bool:
    issue_id = str(finding.get("id") or "").lower()
    repair_route = str(finding.get("repair_route") or "").lower()
    if any(token in issue_id for token in ("text-overflow", "text-overlap", "caption-overlap", "footer-overlap")):
        return True
    if repair_route not in {"shrink_text", "revise_authored_html"}:
        return False
    return not any(
        token in issue_id
        for token in ("image-not-loaded", "out-of-bounds", "root", "overflow-root", "size-mismatch")
    )


def _desired_behavior(owner: str) -> str:
    if owner == "visual_curation":
        return "Select and place method/evidence/table/qualitative source visuals from ingest buckets before using fallback figures."
    if owner == "content_strategy":
        return "Compress the paper into posterized sections, rebuild native tables/cards/formulas/pipelines when valuable, and preserve section continuity instead of optimizing only for more screenshots."
    if owner == "layout_storyboard":
        return "Generate diverse panel topology whose slots match the paper contract, using archetype-conditioned visual fill plus human-effort value rather than one global figure-area target."
    if owner == "typography_system":
        return "Keep poster text in short editable labels, captions, and bullets within contract budgets."
    if owner == "critic_rubric":
        return "Make the critic scorecard penalize visible poster failures with stage and repair_route metadata."
    if owner == "deterministic_env_feedback":
        return "Surface measurable failures as design_feedback blockers/high findings before finalize."
    if owner == "renderer_export":
        return "Preserve source visuals, native text, captions, and HTML/PDF/PSD export fidelity."
    if owner == "model_routing":
        return "Keep explicit lower-cost eval overrides stable while choosing role models that follow poster contracts."
    if owner == "harness_reliability":
        return "Keep the loop ledger honest on failed generation, missing candidates, API failures, and partial runs so Codex can continue safely."
    if owner == "eval_calibration":
        return "Adjust proxy metrics only in a separate calibration iteration, with generator behavior held constant."
    return "Make the planner consume poster_content_brief and poster_plan_contract as binding inputs."


def _copy_contact_sheet(iter_dir: Path, dest: Path) -> None:
    src = iter_dir / "contact_sheet.png"
    if src.exists():
        shutil.copyfile(src, dest)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _git_output(args: list[str]) -> str:
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=str(_REPO_ROOT),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""


def _owner(value: Any) -> str:
    raw = str(value or "").strip()
    return raw if raw in OWNER_TAXONOMY else "deterministic_env_feedback"


def _severity(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"blocker", "high", "medium", "low"}:
        return raw
    if raw == "p0":
        return "blocker"
    if raw == "p1":
        return "high"
    if raw == "p2":
        return "medium"
    return "medium"


def _shell_quote(value: str) -> str:
    if not value:
        return "''"
    if all(ch.isalnum() or ch in "._/-:=+" for ch in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


if __name__ == "__main__":
    raise SystemExit(main())
