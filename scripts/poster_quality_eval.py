"""Poster quality benchmark harness for local paper/poster references.

The default path is offline and cheap: discover paper cases, measure author
poster PNGs, and write a report plus contact sheet. Use ``--generate`` only
when you intentionally want to spend API calls on AutoDesign runs.

Examples:
    uv run python scripts/poster_quality_eval.py \
        --case icml2023_ds1000

    uv run python scripts/poster_quality_eval.py \
        --data-dir /path/to/paper-data --case icml2023_ds1000 --generate

    uv run python scripts/poster_quality_eval.py \
        --candidate icml2023_ds1000=out/runs/20260515-120000-abcd1234/final

    uv run python scripts/poster_quality_eval.py \
        --label-set paper-poster-evaluator-calibration-v1
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median, pstdev
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import fitz  # pymupdf
from bs4 import BeautifulSoup, Tag
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from autodesign.util.io import atomic_write_json, ensure_dirs
from autodesign.util.layer_parse import parse_html_layers


DEFAULT_BRIEF = (
    "Create a production-quality academic conference poster from the attached "
    "paper. Keep the source figures and tables as the primary visual mass, "
    "use concise editable text for claims/captions, preserve provenance, and "
    "target a dense but readable NeurIPS/ICML/ICLR-style poster."
)
DEFAULT_TEMPLATE = "academic-landscape-1.414"
SET_CONFIG_PATH = _REPO_ROOT / "eval" / "poster_quality_sets.json"
LABEL_CONFIG_PATH = _REPO_ROOT / "eval" / "poster_quality_labels.json"
DENSE_SYNTHESIS_TARGETS_PATH = (
    _REPO_ROOT
    / "skills"
    / "poster"
    / "visual_recipe"
    / "assets"
    / "dense_synthesis_targets.json"
)
_DENSE_SYNTHESIS_TARGETS_CACHE: dict[str, Any] | None = None
HARNESS_MODES = {"cheap", "standard", "quality", "dogfood"}
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

ARCHETYPE_TARGET_PROFILES: dict[str, dict[str, Any]] = {
    "gui_video_benchmark": {
        "visual_area_min": 0.40,
        "human_effort_min": 0.50,
        "summary": "GUI/video benchmark posters should spend visual mass on process bands, screenshot strips, and result tables.",
    },
    "world_model_filmstrip": {
        "visual_area_min": 0.36,
        "human_effort_min": 0.56,
        "summary": "World-model/video-prediction posters should expose sequence filmstrips, prediction grids, architecture spines, and compact comparison tables.",
    },
    "multi_view_matrix_graph": {
        "visual_area_min": 0.32,
        "human_effort_min": 0.58,
        "summary": "Multi-view posters should use matrix/graph/table walls and a method/result split.",
    },
    "table_first_benchmark": {
        "visual_area_min": 0.14,
        "human_effort_min": 0.64,
        "summary": "Benchmark posters can be lower on screenshot area when native leaderboard, ablation, and result structures are reconstructed.",
    },
    "theory_text_board": {
        "visual_area_min": 0.16,
        "human_effort_min": 0.72,
        "summary": "Theory/text-heavy posters should prioritize theorem/proof intuition, assumptions/results, dense native hierarchy, and minimal source figures.",
    },
    "research_synthesis_dense": {
        "visual_area_min": 0.16,
        "human_effort_min": 0.72,
        "summary": "Dense synthesis posters should reward native model cards, tables, pipelines, limitations, and coherent section continuity over screenshot count.",
    },
    "visual_evidence_wall": {
        "visual_area_min": 0.16,
        "human_effort_min": 0.58,
        "summary": "Visual evidence posters should bind readable source figures/tables to nearby claims; text density and editorial synthesis matter more than screenshot area.",
    },
    "default": {
        "visual_area_min": 0.32,
        "human_effort_min": 0.58,
        "summary": "Default academic posters balance source visuals with compact native research synthesis.",
    },
}

HUMAN_EFFORT_RULES = [
    {
        "id": "semantic_synthesis_over_screenshot_count",
        "owner": "content_strategy",
        "severity": "high",
        "expectation": (
            "Treat dense, paper-faithful text synthesis inside each panel as the "
            "primary human-effort signal; screenshot count and visual area are "
            "supporting evidence, not substitutes for filled explanatory boxes."
        ),
    },
    {
        "id": "native_reconstruction_value",
        "owner": "content_strategy",
        "severity": "high",
        "expectation": (
            "Reward native editable tables, model cards, formulas, theorem boards, "
            "pipelines, and limitations/future-work panels because they represent "
            "high manual editorial labor."
        ),
    },
    {
        "id": "dense_text_requires_hierarchy",
        "owner": "typography_system",
        "severity": "high",
        "expectation": (
            "Dense text is acceptable only when posterized into section hierarchy, "
            "emphasis, role coverage, and short scannable blocks."
        ),
    },
    {
        "id": "body_pages_before_references_only",
        "owner": "visual_curation",
        "severity": "high",
        "expectation": (
            "Ingest and visual selection should use paper body pages before "
            "References/Bibliography; citation pages rarely carry poster-worthy evidence."
        ),
    },
    {
        "id": "image_panels_require_explanatory_text",
        "owner": "layout_storyboard",
        "severity": "high",
        "expectation": (
            "Every panel containing a source figure/table must include nearby dense "
            "editable explanation of what that evidence contributes to the paper story."
        ),
    },
    {
        "id": "section_titles_match_panel_content",
        "owner": "designer_contract",
        "severity": "high",
        "expectation": (
            "Panel titles must name the actual content inside the box; a method "
            "section should not contain only benchmark/result evidence."
        ),
    },
]

MECHANICAL_VISUAL_DISCOUNT_POLICY = {
    "id": "mechanical_screenshot_discount",
    "owner": "layout_storyboard",
    "expectation": (
        "High visual area receives less credit when it is mostly a screenshot wall "
        "without semantic section synthesis or native reconstruction."
    ),
    "discount_when": {
        "image_layer_count_gte": 6,
        "visual_area_ratio_gte": 0.40,
        "semantic_synthesis_score_lt": 0.58,
        "native_reconstruction_score_lt": 0.42,
    },
}


@dataclass(frozen=True)
class PosterCase:
    slug: str
    group: str
    case_dir: str
    paper_path: str
    reference_poster_path: str | None
    reference_html_path: str | None
    reference_metadata_path: str | None
    slides_path: str | None
    title: str
    page_count: int | None


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    data_dir = _resolve_data_dir(args.data_dir)
    out_dir = Path(args.out_dir).expanduser().resolve()
    ensure_dirs(out_dir, out_dir / "logs")

    eval_set = load_eval_set(args.eval_set)
    label_set = load_label_set(args.label_set or eval_set.get("label_set"))
    harness_mode = _harness_mode()
    selected_cases = (
        args.case
        or list(eval_set.get("cases") or [])
        or _label_set_cases(label_set)
    )
    default_template = str(eval_set.get("default_template") or DEFAULT_TEMPLATE)
    template = args.template or default_template
    brief = args.brief or str(eval_set.get("brief") or DEFAULT_BRIEF)
    needs_cases = bool(
        args.generate
        or args.candidate
        or selected_cases
        or eval_set
        or label_set
    )
    all_cases = discover_cases(data_dir) if needs_cases else []
    cases = _select_cases(all_cases, selected_cases, args.limit) if needs_cases else []
    if needs_cases and not cases:
        print(f"No paper cases found in {data_dir}", file=sys.stderr)
        return 2

    if args.generate and len(cases) > 2 and not args.allow_large_generate:
        print(
            "Refusing to generate more than 2 poster cases in one run. "
            "Use repeated --case, --limit 2, or pass --allow-large-generate "
            "when you intentionally want a high-cost batch.",
            file=sys.stderr,
        )
        return 2

    candidate_map = _parse_candidates(args.candidate)
    generate_workers = _generate_worker_count(
        args.generate_workers,
        harness_mode=harness_mode,
        n_cases=len(cases),
    )
    reference_by_case = {
        case.slug: _reference_metrics_for_case(case)
        for case in cases
    }
    reference_metrics = [
        ref["image"]
        for ref in reference_by_case.values()
        if isinstance(ref.get("image"), dict)
    ]
    native_reference_metrics = [
        ref["html"]
        for ref in reference_by_case.values()
        if isinstance(ref.get("html"), dict)
    ]
    reference_metadata = [
        ref["metadata"]
        for ref in reference_by_case.values()
        if isinstance(ref.get("metadata"), dict)
    ]
    targets = summarize_reference_targets(
        reference_metrics,
        native_reference_metrics,
        reference_metadata,
    )
    generation_brief = _brief_with_native_reference_prior(brief, targets, template=template)

    generated_runs: dict[str, Path] = {}
    generated_status: dict[str, dict[str, Any]] = {}
    if args.generate:
        def run_case(case: PosterCase) -> tuple[PosterCase, dict[str, Any] | None]:
            return case, _generate_case(
                case,
                out_dir=out_dir,
                template=_template_for_case(
                    case,
                    eval_set=eval_set,
                    override_template=args.template,
                    default_template=default_template,
                ),
                brief=generation_brief,
                skip_enhancer=args.skip_enhancer,
                no_claim_graph=args.no_claim_graph,
                harness_mode=harness_mode,
                reference_profile=_case_reference_profile_for_generation(
                    case,
                    eval_set=eval_set,
                    reference_payload=reference_by_case.get(case.slug),
                ),
            )

        if generate_workers > 1:
            print(f"generating {len(cases)} case(s) with {generate_workers} workers")
            with ThreadPoolExecutor(max_workers=generate_workers) as pool:
                futures = {pool.submit(run_case, case): case for case in cases}
                for future in as_completed(futures):
                    case = futures[future]
                    try:
                        _, generated = future.result()
                    except Exception as exc:
                        print(
                            f"generation failed for {case.slug}; worker raised {type(exc).__name__}: {exc}",
                            file=sys.stderr,
                        )
                        generated = None
                    if generated is not None:
                        if _generated_candidate_should_be_evaluated(generated, harness_mode=harness_mode) and generated.get("run_dir"):
                            generated_runs[case.slug] = Path(str(generated["run_dir"]))
                        generated_status[case.slug] = generated
        else:
            for case in cases:
                _, generated = run_case(case)
                if generated is not None:
                    if _generated_candidate_should_be_evaluated(generated, harness_mode=harness_mode) and generated.get("run_dir"):
                        generated_runs[case.slug] = Path(str(generated["run_dir"]))
                    generated_status[case.slug] = generated

    results: list[dict[str, Any]] = []
    for case in cases:
        reference_payload = reference_by_case.get(case.slug) or {}
        ref = reference_payload.get("image")
        ref_html = reference_payload.get("html")
        ref_metadata = reference_payload.get("metadata")

        candidate_path = generated_runs.get(case.slug) or candidate_map.get(case.slug)
        candidate = _candidate_metrics(candidate_path) if candidate_path else None
        if candidate is not None and case.slug in generated_status:
            candidate["generation"] = generated_status[case.slug]
        elif candidate is None and case.slug in generated_status:
            candidate = {
                "path": str(candidate_path) if candidate_path else None,
                "error": "generation failed before a candidate artifact could be resolved",
                "generation": generated_status[case.slug],
            }
        results.append({
            "case": asdict(case),
            "reference": ref,
            "reference_html": ref_html,
            "reference_metadata": ref_metadata,
            "reference_metadata_warnings": reference_payload.get("metadata_warnings") or [],
            "candidate": candidate,
            "comparison": compare_candidate(
                ref,
                candidate,
                case_slug=case.slug,
                reference_html=ref_html,
                reference_metadata=ref_metadata if isinstance(ref_metadata, dict) else None,
            ) if candidate else None,
        })

    artifact_role_counts = _candidate_artifact_role_counts(results)
    batch_layout_diversity = apply_batch_layout_diversity(results)
    rubric = build_reference_rubric(
        eval_set=eval_set,
        targets=targets,
        results=results,
        template=template,
    )
    label_calibration = build_label_calibration(
        label_set=label_set,
        results=results,
        all_cases=all_cases,
    )
    low_confidence_cases = _low_confidence_cases(results)
    low_confidence = bool(low_confidence_cases)
    report = build_report(
        data_dir=data_dir,
        out_dir=out_dir,
        cases=cases,
        eval_set=eval_set,
        targets=targets,
        rubric=rubric,
        results=results,
        label_calibration=label_calibration,
        generated=bool(generated_runs or generated_status),
        template=template,
        harness_mode=harness_mode,
        low_confidence=low_confidence,
        low_confidence_cases=low_confidence_cases,
        batch_layout_diversity=batch_layout_diversity,
    )
    reproducibility = build_reproducibility_metadata(
        eval_set=eval_set,
        label_set=label_set,
        harness_mode=harness_mode,
    )
    atomic_write_json(out_dir / "cases.json", [asdict(c) for c in cases])
    atomic_write_json(out_dir / "metrics.json", {
        "data_dir": str(data_dir),
        "generated_at": int(time.time()),
        "harness_mode": harness_mode,
        "low_confidence": low_confidence,
        "low_confidence_cases": low_confidence_cases,
        "reproducibility": reproducibility,
        "eval_set": eval_set or None,
        "label_set": label_set or None,
        "label_calibration": label_calibration or None,
        "template": template,
        "generation_brief": generation_brief,
        "generate_workers": generate_workers if args.generate else 0,
        "case_templates": eval_set.get("case_templates") or {},
        "artifact_role_counts": artifact_role_counts,
        **artifact_role_counts,
        "batch_layout_diversity": batch_layout_diversity,
        "reference_targets": targets,
        "reference_rubric": rubric,
        "results": results,
    })
    atomic_write_json(out_dir / "reference_rubric.json", rubric)
    if label_calibration:
        atomic_write_json(out_dir / "label_calibration.json", label_calibration)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    write_contact_sheet(results, out_dir / "contact_sheet.png")
    write_run_commands(
        cases,
        data_dir,
        out_dir,
        template,
        generation_brief,
        eval_set,
        override_template=args.template,
    )

    print(f"cases:        {len(cases)}")
    print(f"data dir:     {data_dir}")
    print(f"report:       {out_dir / 'report.md'}")
    print(f"metrics:      {out_dir / 'metrics.json'}")
    print(f"rubric:       {out_dir / 'reference_rubric.json'}")
    if label_calibration:
        print(f"labels:       {out_dir / 'label_calibration.json'}")
    print(f"contact:      {out_dir / 'contact_sheet.png'}")
    print(f"run commands: {out_dir / 'run_commands.sh'}")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Directory containing paper cases. Defaults to the repository data/ directory.",
    )
    parser.add_argument(
        "--out-dir",
        default=_REPO_ROOT / "out" / "poster_quality_eval",
        help="Output directory for metrics, report, contact sheet, and logs.",
    )
    parser.add_argument(
        "--set",
        dest="eval_set",
        default=None,
        help=f"Named eval set from {SET_CONFIG_PATH.relative_to(_REPO_ROOT)}.",
    )
    parser.add_argument(
        "--label-set",
        default=None,
        help=(
            f"Named labeled evaluator calibration set from "
            f"{LABEL_CONFIG_PATH.relative_to(_REPO_ROOT)}. When no --set or "
            "--case is provided, the label set controls case selection."
        ),
    )
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="Case slug to include. Repeatable. Default: all discovered cases.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of cases after --case filtering.",
    )
    parser.add_argument(
        "--candidate",
        action="append",
        default=[],
        metavar="CASE=PATH",
        help="Evaluate an existing candidate preview/html/final dir for a case.",
    )
    parser.add_argument(
        "--generate",
        action="store_true",
        help="Run AutoDesign for each selected case. This can be slow and uses API credits.",
    )
    parser.add_argument(
        "--allow-large-generate",
        action="store_true",
        help="Allow --generate with more than 2 selected cases. Defaults to refusing high-cost batches.",
    )
    parser.add_argument(
        "--generate-workers",
        type=int,
        default=None,
        help=(
            "Number of concurrent AutoDesign generation subprocesses. Defaults "
            "to POSTER_EVAL_GENERATE_WORKERS, then 3 for dogfood, 2 for cheap multi-case "
            "runs, otherwise 1."
        ),
    )
    parser.add_argument(
        "--template",
        default=None,
        help=(
            "Poster template passed to autodesign.cli run when --generate is set. "
            "Defaults to the eval set template, then academic-landscape-1.414."
        ),
    )
    parser.add_argument(
        "--brief",
        default=None,
        help="Brief passed to AutoDesign when --generate is set. Defaults to the eval set brief.",
    )
    parser.add_argument(
        "--skip-enhancer",
        action="store_true",
        help="Forward --skip-enhancer to the AutoDesign CLI when generating.",
    )
    parser.add_argument(
        "--no-claim-graph",
        action="store_true",
        help="Forward --no-claim-graph to the AutoDesign CLI when generating.",
    )
    return parser.parse_args(argv)


def _harness_mode() -> str:
    raw = os.getenv("POSTER_HARNESS_MODE", "dogfood").strip().lower() or "dogfood"
    if raw not in HARNESS_MODES:
        raise SystemExit(
            f"unknown POSTER_HARNESS_MODE={raw!r}; expected one of {sorted(HARNESS_MODES)}"
        )
    return raw


def _generate_worker_count(raw: int | None, *, harness_mode: str, n_cases: int) -> int:
    if n_cases <= 1:
        return 1
    value = raw
    if value is None:
        env_raw = os.getenv("POSTER_EVAL_GENERATE_WORKERS")
        if env_raw:
            try:
                value = int(env_raw)
            except ValueError:
                raise SystemExit(
                    f"POSTER_EVAL_GENERATE_WORKERS must be an integer, got {env_raw!r}"
                )
    if value is None:
        value = 3 if harness_mode == "dogfood" else 2 if harness_mode == "cheap" else 1
    return max(1, min(int(value), n_cases))


def _resolve_data_dir(raw: str | None) -> Path:
    if raw:
        return Path(raw).expanduser().resolve()
    return (_REPO_ROOT / "data").resolve()


def load_eval_set(name: str | None) -> dict[str, Any]:
    if not name:
        return {}
    if not SET_CONFIG_PATH.exists():
        raise SystemExit(f"eval set config not found: {SET_CONFIG_PATH}")
    try:
        payload = json.loads(SET_CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"could not parse {SET_CONFIG_PATH}: {exc}") from exc
    eval_set = (payload.get("sets") or {}).get(name)
    if not isinstance(eval_set, dict):
        available = ", ".join(sorted((payload.get("sets") or {}).keys()))
        raise SystemExit(f"unknown eval set {name!r}. Available: {available}")
    return {"id": name, **eval_set}


def load_label_set(name: str | None) -> dict[str, Any]:
    if not name:
        return {}
    if not LABEL_CONFIG_PATH.exists():
        raise SystemExit(f"label set config not found: {LABEL_CONFIG_PATH}")
    try:
        payload = json.loads(LABEL_CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"could not parse {LABEL_CONFIG_PATH}: {exc}") from exc
    label_set = (payload.get("label_sets") or {}).get(name)
    if not isinstance(label_set, dict):
        available = ", ".join(sorted((payload.get("label_sets") or {}).keys()))
        raise SystemExit(f"unknown label set {name!r}. Available: {available}")
    return {"id": name, **label_set}


def _label_set_cases(label_set: dict[str, Any]) -> list[str]:
    cases = label_set.get("cases")
    if isinstance(cases, list) and cases:
        return [str(case) for case in cases if str(case or "").strip()]
    labels = label_set.get("case_labels")
    if isinstance(labels, dict):
        return [str(case) for case in labels.keys()]
    return []


def build_reproducibility_metadata(
    *,
    eval_set: dict[str, Any],
    label_set: dict[str, Any],
    harness_mode: str,
) -> dict[str, Any]:
    env_keys = [
        "POSTER_HARNESS_MODE",
        "DESIGNER_MODEL",
        "CRITIC_MODEL",
        "INGEST_MODEL",
        "MAX_CRITIQUE_ITERS",
        "MAX_ENV_REPAIR_ATTEMPTS",
        "POSTER_QUALITY_TRACE",
        "POSTER_EVAL_GENERATE_WORKERS",
        "INGEST_VLM_PARALLELISM",
        "INGEST_CAPTION_PARALLELISM",
        "OPENAI_COMPAT_BASE_URL",
    ]
    return {
        "git_sha": _git_output(["rev-parse", "HEAD"]),
        "git_branch": _git_output(["branch", "--show-current"]),
        "dirty_files": _git_output(["status", "--short"]).splitlines(),
        "harness_mode": harness_mode,
        "eval_set_id": eval_set.get("id"),
        "label_set_id": label_set.get("id"),
        "env": {key: os.getenv(key) for key in env_keys if os.getenv(key) is not None},
        "file_hashes": _file_hashes([
            SET_CONFIG_PATH,
            LABEL_CONFIG_PATH,
            _REPO_ROOT / "prompts" / "designer.md",
            _REPO_ROOT / "prompts" / "critic_vision_poster.md",
            _REPO_ROOT / "skills" / "poster" / "visual_recipe" / "SKILL.md",
            _REPO_ROOT / "scripts" / "poster_quality_eval.py",
        ]),
    }


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


def _file_hashes(paths: list[Path]) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in paths:
        if not path.exists():
            continue
        try:
            out[str(path.relative_to(_REPO_ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
        except Exception:
            continue
    return out


def _template_for_case(
    case: PosterCase,
    *,
    eval_set: dict[str, Any],
    override_template: str | None,
    default_template: str,
) -> str:
    if override_template:
        return override_template
    case_templates = eval_set.get("case_templates") or {}
    if isinstance(case_templates, dict):
        value = case_templates.get(case.slug)
        if value:
            return str(value)
    inferred = _template_from_reference_poster(case.reference_poster_path)
    if inferred:
        return inferred
    return default_template


def _reference_metrics_for_case(case: PosterCase) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    metadata, warnings = _reference_metadata_for_case(case)
    if metadata:
        payload["metadata"] = metadata
    if warnings:
        payload["metadata_warnings"] = warnings
    if case.reference_poster_path:
        payload["image"] = image_metrics(Path(case.reference_poster_path))
    if case.reference_html_path:
        html_metrics = reference_html_metrics(Path(case.reference_html_path))
        payload["html"] = _merge_reference_html_metadata(html_metrics, metadata)
    return payload


def _reference_metadata_for_case(case: PosterCase) -> tuple[dict[str, Any], list[str]]:
    path = Path(case.reference_metadata_path) if case.reference_metadata_path else Path(case.case_dir) / "metadata.json"
    if not path.exists():
        return {}, []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {}, [f"metadata_json_invalid: {path}: {exc}"]
    except Exception as exc:
        return {}, [f"metadata_json_unreadable: {path}: {type(exc).__name__}: {exc}"]
    if not isinstance(payload, dict):
        return {}, [f"metadata_json_not_object: {path}"]
    payload = dict(payload)
    payload.setdefault("path", str(path))
    return payload, []


def _merge_reference_html_metadata(
    html_metrics: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(html_metrics, dict):
        html_metrics = {}
    out = dict(html_metrics)
    if not isinstance(metadata, dict) or not metadata:
        return out
    profile = str(metadata.get("reference_profile") or metadata.get("profile") or "").strip()
    if profile:
        out["inferred_reference_profile"] = out.get("reference_profile")
        out["reference_profile"] = profile
        out["reference_profile_source"] = "metadata"
    metrics_hint = metadata.get("reference_metrics_hint")
    if isinstance(metrics_hint, dict):
        out["metadata_reference_metrics_hint"] = metrics_hint
    text_targets = metadata.get("text_synthesis_targets")
    if isinstance(text_targets, dict):
        out["metadata_text_synthesis_targets"] = text_targets
    required_units = metadata.get("required_units")
    if isinstance(required_units, list):
        out["metadata_required_units"] = [
            str(item) for item in required_units if str(item or "").strip()
        ]
    return out


def _template_from_reference_poster(reference_poster_path: str | None) -> str | None:
    if not reference_poster_path:
        return None
    try:
        with Image.open(reference_poster_path) as im:
            width, height = im.size
    except Exception:
        return None
    if width <= 0 or height <= 0:
        return None
    aspect = width / float(height)
    if aspect >= 1.75:
        return "academic-wide-2x1"
    if 1.25 <= aspect < 1.75:
        return "academic-landscape-1.414"
    if 0.62 <= aspect < 0.90:
        return "conference-poster-portrait"
    return None


def discover_cases(data_dir: Path) -> list[PosterCase]:
    cases: list[PosterCase] = []
    for paper in sorted(data_dir.glob("**/paper.pdf")):
        case_dir = paper.parent
        title, page_count = _pdf_title_and_pages(paper)
        poster = case_dir / "poster.png"
        reference_html = _reference_html_path(case_dir)
        metadata = case_dir / "metadata.json"
        slides = case_dir / "slides.pdf"
        cases.append(PosterCase(
            slug=case_dir.name,
            group=case_dir.parent.name,
            case_dir=str(case_dir),
            paper_path=str(paper),
            reference_poster_path=str(poster) if poster.exists() else None,
            reference_html_path=str(reference_html) if reference_html else None,
            reference_metadata_path=str(metadata) if metadata.exists() else None,
            slides_path=str(slides) if slides.exists() else None,
            title=title or _title_from_slug(case_dir.name),
            page_count=page_count,
        ))
    return cases


def _reference_html_path(case_dir: Path) -> Path | None:
    for name in ("poster.html", "reference.html"):
        candidate = case_dir / name
        if candidate.exists():
            return candidate
    candidates = sorted(case_dir.glob("*[Pp]oster*.html")) + sorted(case_dir.glob("*[Rr]eference*.html"))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _select_cases(cases: list[PosterCase], selected: list[str], limit: int | None) -> list[PosterCase]:
    if selected:
        by_slug = {case.slug: case for case in cases}
        ordered: list[PosterCase] = []
        missing: list[str] = []
        for slug in selected:
            case = by_slug.get(slug)
            if case is None:
                missing.append(slug)
            else:
                ordered.append(case)
        cases = ordered
        if missing:
            print(f"warning: missing requested cases: {', '.join(missing)}", file=sys.stderr)
    if limit is not None:
        cases = cases[:max(0, limit)]
    return cases


def _pdf_title_and_pages(path: Path) -> tuple[str, int | None]:
    try:
        doc = fitz.open(path)
    except Exception:
        return "", None
    try:
        page_count = len(doc)
        text = doc[0].get_text("text") if page_count else ""
        return _guess_title(text), page_count
    except Exception:
        return "", len(doc) if doc else None
    finally:
        doc.close()


def _guess_title(text: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    bad_prefixes = (
        "abstract", "introduction", "proceedings", "published", "arxiv",
        "conference", "workshop", "copyright",
    )
    for line in lines[:40]:
        if len(line) < 12 or len(line) > 180:
            continue
        if any(line.lower().startswith(prefix) for prefix in bad_prefixes):
            continue
        if re.fullmatch(r"\d+", line):
            continue
        return line
    return ""


def _title_from_slug(slug: str) -> str:
    parts = slug.split("_")
    if parts and re.match(r"^(icml|iclr|nips|neurips)\d{4}$", parts[0]):
        parts = parts[1:]
    return " ".join(part.capitalize() for part in parts)


def image_metrics(path: Path) -> dict[str, Any]:
    with Image.open(path) as src:
        src = ImageOps.exif_transpose(src)
        width, height = src.size
        img = src.convert("RGB")

    small = img.copy()
    small.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
    pixels = _pixels(small)
    small_w, small_h = small.size
    n = max(1, len(pixels))
    white = 0
    dark = 0
    saturated = 0
    quant_bins: set[tuple[int, int, int]] = set()
    row_nonwhite_counts = [0 for _ in range(max(1, small_h))]
    for idx, (r, g, b) in enumerate(pixels):
        luma = _luma(r, g, b)
        sat = _saturation(r, g, b)
        is_near_white = luma > 245 and sat < 0.08
        if is_near_white:
            white += 1
        else:
            row_nonwhite_counts[min(len(row_nonwhite_counts) - 1, idx // max(1, small_w))] += 1
        if luma < 80:
            dark += 1
        if sat > 0.35:
            saturated += 1
        quant_bins.add((r // 32, g // 32, b // 32))

    gray = small.convert("L")
    edges = gray.filter(ImageFilter.FIND_EDGES)
    edge_density = sum(1 for v in _pixels(edges) if v > 28) / n
    grid = _grid_mass(small, cols=4 if width >= height else 3, rows=3 if width >= height else 4)
    masses = [cell["mass_ratio"] for cell in grid]
    avg_mass = sum(masses) / max(1, len(masses))
    mass_cv = pstdev(masses) / avg_mass if avg_mass > 0 else 0.0
    nonwhite_ratio = max(0.0, min(1.0, 1.0 - white / n))
    row_nonwhite_ratios = [
        count / max(1, small_w)
        for count in row_nonwhite_counts
    ]
    blank_row_threshold = 0.018
    longest_blank_run = 0
    current_blank_run = 0
    for ratio in row_nonwhite_ratios:
        if ratio <= blank_row_threshold:
            current_blank_run += 1
            longest_blank_run = max(longest_blank_run, current_blank_run)
        else:
            current_blank_run = 0
    band_count = 10
    vertical_band_nonwhite: list[float] = []
    for band in range(band_count):
        start = int(round(band * len(row_nonwhite_ratios) / band_count))
        end = int(round((band + 1) * len(row_nonwhite_ratios) / band_count))
        segment = row_nonwhite_ratios[start:max(start + 1, end)]
        vertical_band_nonwhite.append(round(sum(segment) / max(1, len(segment)), 4))

    return {
        "path": str(path),
        "width": width,
        "height": height,
        "aspect_ratio": round(width / max(1, height), 4),
        "orientation": "landscape" if width > height else "portrait" if height > width else "square",
        "white_space_ratio": round(white / n, 4),
        "nonwhite_pixel_ratio": round(nonwhite_ratio, 4),
        "longest_blank_vertical_run_ratio": round(longest_blank_run / max(1, len(row_nonwhite_ratios)), 4),
        "vertical_band_nonwhite_ratios": vertical_band_nonwhite,
        "vertical_band_nonwhite_min": round(min(vertical_band_nonwhite or [0.0]), 4),
        "blank_row_threshold": blank_row_threshold,
        "dark_ink_ratio": round(dark / n, 4),
        "saturated_pixel_ratio": round(saturated / n, 4),
        "edge_density": round(edge_density, 4),
        "palette_complexity": len(quant_bins),
        "grid_mass_cv": round(mass_cv, 4),
        "empty_cell_ratio": round(sum(1 for value in masses if value < 0.035) / max(1, len(masses)), 4),
        "grid": grid,
    }


def _panel_visual_fill_metrics(
    preview_path: Path,
    dom_audit: dict[str, Any],
    dom_metrics: dict[str, Any],
) -> dict[str, Any]:
    layers = [
        layer for layer in (dom_audit.get("dom_layers") or [])
        if isinstance(layer, dict)
    ]
    if not preview_path.exists() or not layers:
        return {}
    try:
        with Image.open(preview_path) as src:
            img = ImageOps.exif_transpose(src).convert("RGB")
    except Exception:
        return {}
    img_w, img_h = img.size
    canvas = _paper_poster_dom_canvas(layers, dom_metrics)
    cw = max(1, int(canvas.get("w") or img_w))
    ch = max(1, int(canvas.get("h") or img_h))
    scale_x = img_w / float(max(1, cw))
    scale_y = img_h / float(max(1, ch))
    canvas_area = max(1.0, float(cw * ch))
    panels = [
        layer for layer in layers
        if _is_panel_visual_fill_candidate(layer, canvas_area=canvas_area)
    ]
    samples: list[dict[str, Any]] = []
    ink_ratios: list[float] = []
    coverages: list[float] = []
    blank_runs: list[float] = []
    for panel in panels:
        bbox = panel.get("bbox") if isinstance(panel.get("bbox"), dict) else {}
        x = int(round(_num(bbox.get("x")) * scale_x))
        y = int(round(_num(bbox.get("y")) * scale_y))
        w = int(round(_num(bbox.get("w")) * scale_x))
        h = int(round(_num(bbox.get("h")) * scale_y))
        if w < 24 or h < 24:
            continue
        x1 = max(0, min(img_w - 1, x))
        y1 = max(0, min(img_h - 1, y))
        x2 = max(x1 + 1, min(img_w, x + w))
        y2 = max(y1 + 1, min(img_h, y + h))
        crop = img.crop((x1, y1, x2, y2))
        stats = _visual_ink_grid_stats(crop)
        ink_ratios.append(stats["ink_ratio"])
        coverages.append(stats["grid_coverage"])
        blank_runs.append(stats["longest_blank_row_run_ratio"])
        panel_area_ratio = (_num(bbox.get("w")) * _num(bbox.get("h"))) / canvas_area
        large_panel = panel_area_ratio >= (0.045 if ch > cw else 0.065)
        min_ink = 0.16 if large_panel else 0.095
        min_coverage = 0.42 if large_panel else 0.26
        max_blank = 0.42 if large_panel else 0.55
        reasons: list[str] = []
        if stats["ink_ratio"] < min_ink:
            reasons.append("low_visual_ink")
        if stats["grid_coverage"] < min_coverage:
            reasons.append("low_visual_grid_coverage")
        if stats["longest_blank_row_run_ratio"] > max_blank:
            reasons.append("long_visual_blank_band")
        if reasons:
            severity = "P0" if large_panel and (
                stats["ink_ratio"] < min_ink * 0.88
                or stats["grid_coverage"] < min_coverage * 0.82
                or stats["longest_blank_row_run_ratio"] > max_blank
            ) else "P1"
            samples.append({
                "block_id": panel.get("layer_id"),
                "role": panel.get("role"),
                "class_name": panel.get("class_name"),
                "bbox": bbox,
                "panel_area_ratio": round(panel_area_ratio, 4),
                "visual_ink_ratio": stats["ink_ratio"],
                "visual_grid_coverage": stats["grid_coverage"],
                "longest_visual_blank_row_run_ratio": stats["longest_blank_row_run_ratio"],
                "min_visual_ink_ratio": min_ink,
                "min_visual_grid_coverage": min_coverage,
                "max_visual_blank_row_run_ratio": max_blank,
                "reasons": reasons,
                "severity": severity,
            })
    p0_count = sum(1 for sample in samples if sample.get("severity") == "P0")
    return {
        "panel_visual_audited_count": len(panels),
        "panel_visual_underfilled_count": len(samples),
        "panel_visual_underfilled_p0_count": p0_count,
        "panel_visual_min_ink_ratio": round(min(ink_ratios or [1.0]), 4),
        "panel_visual_avg_ink_ratio": round(sum(ink_ratios) / max(1, len(ink_ratios)), 4),
        "panel_visual_min_grid_coverage": round(min(coverages or [1.0]), 4),
        "panel_visual_max_blank_run_ratio": round(max(blank_runs or [0.0]), 4),
        "panel_visual_underfilled_samples": samples[:8],
    }


def _is_panel_visual_fill_candidate(layer: dict[str, Any], *, canvas_area: float) -> bool:
    bbox = layer.get("bbox") if isinstance(layer.get("bbox"), dict) else {}
    area = _num(bbox.get("w")) * _num(bbox.get("h"))
    if area < canvas_area * 0.018:
        return False
    kind = str(layer.get("kind") or "").lower()
    if kind in {"text", "caption", "metric", "quote", "table", "image", "chart", "embed"}:
        return False
    role = str(layer.get("role") or "").lower()
    class_name = str(layer.get("class_name") or "").lower()
    layer_id = str(layer.get("layer_id") or "").lower()
    haystack = " ".join((kind, role, class_name, layer_id))
    if any(token in haystack for token in (
        "panel-head", "section-no", "badge", "metric-chip", "logo",
        "header", "title", "thesis", "footer", "citation", "provenance",
    )):
        return False
    return any(token in haystack for token in (
        "panel", "hero", "evidence", "lower-grid", "method", "analysis",
        "results", "representation", "synthesis", "qual", "grid",
    ))


def _visual_ink_grid_stats(img: Image.Image) -> dict[str, float]:
    small = img.copy()
    small.thumbnail((360, 360), Image.Resampling.LANCZOS)
    w, h = small.size
    pixels = _pixels(small)
    if not pixels:
        return {"ink_ratio": 0.0, "grid_coverage": 0.0, "longest_blank_row_run_ratio": 1.0}
    ink_mask: list[bool] = []
    for r, g, b in pixels:
        luma = _luma(r, g, b)
        sat = _saturation(r, g, b)
        ink_mask.append(luma < 236 or sat > 0.14)
    ink_ratio = sum(1 for value in ink_mask if value) / max(1, len(ink_mask))
    rows = 12
    cols = 12
    marked = 0
    longest_blank = 0
    current_blank = 0
    for row in range(rows):
        row_marked = False
        y0 = int(round(row * h / rows))
        y1 = int(round((row + 1) * h / rows))
        for col in range(cols):
            x0 = int(round(col * w / cols))
            x1 = int(round((col + 1) * w / cols))
            total = 0
            ink = 0
            for yy in range(y0, max(y0 + 1, y1)):
                base = yy * w
                for xx in range(x0, max(x0 + 1, x1)):
                    idx = min(len(ink_mask) - 1, base + min(w - 1, xx))
                    total += 1
                    ink += 1 if ink_mask[idx] else 0
            if total and ink / total >= 0.025:
                marked += 1
                row_marked = True
        if row_marked:
            current_blank = 0
        else:
            current_blank += 1
            longest_blank = max(longest_blank, current_blank)
    return {
        "ink_ratio": round(ink_ratio, 4),
        "grid_coverage": round(marked / float(max(1, rows * cols)), 4),
        "longest_blank_row_run_ratio": round(longest_blank / float(max(1, rows)), 4),
    }


def reference_html_metrics(path: Path) -> dict[str, Any]:
    """Extract static native-information metrics from a reference HTML poster."""
    try:
        doc = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    except Exception as exc:
        return {
            "path": str(path),
            "error": f"could not parse reference HTML: {type(exc).__name__}",
        }
    root = doc.select_one(".paper-poster, .poster, main, body")
    full_text = doc.get_text(" ", strip=True)
    text_layers = [
        {
            "kind": "text",
            "layer_id": str(node.get("class") or node.name),
            "role": node.name,
            "text": node.get_text(" ", strip=True),
        }
        for node in doc.select("h1, h2, h3, h4, p, li, th, td, .panel-head, .contrib-title, .result-band-title, .stat-val, .stat-label, .tag, .formula, .flow-box")
        if node.get_text(" ", strip=True)
    ]
    content = _content_feature_metrics_from_layers(text_layers, doc=doc)
    panel_nodes = doc.select(".panel, article, section")
    if not panel_nodes and root is not None:
        panel_nodes = [root]
    panels: list[dict[str, Any]] = []
    for index, panel in enumerate(panel_nodes, start=1):
        head = panel.select_one(".panel-head, h2, h3, header")
        panel_text = panel.get_text(" ", strip=True)
        panels.append({
            "index": index,
            "head": head.get_text(" ", strip=True) if head else "",
            "word_count": _word_count(panel_text),
            "table_count": len(panel.select("table")),
            "table_row_count": len(panel.select("tr")),
            "flow_box_count": len(panel.select(".flow-box")),
            "formula_count": len(panel.select(".formula")),
            "stat_card_count": len(panel.select(".stat, .metric-card, .kpi, .stat-card")),
            "contribution_card_count": len(panel.select(".contrib, .contribution-card")),
            "result_band_count": len(panel.select(".result-band, .callout, .insight-card")),
            "heading_count": len(panel.select("h1, h2, h3, h4")),
            "paragraph_count": len(panel.select("p")),
            "list_item_count": len(panel.select("li")),
        })
    grid_rows = [
        node for node in doc.select(".body > .cols, .poster > .cols, main > .cols, .paper-poster > .cols")
    ]
    if not grid_rows:
        grid_rows = [node for node in doc.select(".cols, .row, .grid") if node.select(".panel, article, section")]
    panel_words = [int(panel.get("word_count") or 0) for panel in panels]
    table_count = len(doc.select("table"))
    chart_count = int(content.get("native_chart_like_count") or 0)
    table_rows = len(doc.select("tr"))
    table_cells = len(doc.select("td, th"))
    flow_box_count = len(doc.select(".flow-box"))
    formula_dom_count = len(doc.select(".formula"))
    stat_card_count = len(doc.select(".stat, .metric-card, .kpi, .stat-card"))
    contribution_card_count = len(doc.select(".contrib, .contribution-card"))
    result_band_count = len(doc.select(".result-band, .callout, .insight-card"))
    highlight_count = len(doc.select("strong, b, em, mark, .hl, .hl-blue, .hl-green, .best, .accent, .highlight, .emphasis, [data-emphasis]"))
    headings = doc.select("h1, h2, h3, h4, .panel-head")
    native_units = int(content.get("native_information_unit_count") or 0)
    synthesis_units = (
        native_units
        + min(4, table_count)
        + min(4, chart_count)
        + min(3, formula_dom_count)
        + min(4, flow_box_count // 3)
        + min(3, stat_card_count)
        + min(4, contribution_card_count)
        + min(3, result_band_count)
    )
    profile = _reference_html_profile(
        table_count=table_count,
        formula_count=max(formula_dom_count, int(content.get("formula_like_text_count") or 0)),
        flow_box_count=flow_box_count,
        model_card_count=int(content.get("model_card_like_count") or 0),
        synthesis_units=synthesis_units,
    )
    return {
        "path": str(path),
        "reference_kind": "html",
        "reference_profile": profile,
        "canvas": _reference_html_canvas(doc),
        "visible_text_word_count": _word_count(full_text),
        "panel_count": len(panels),
        "grid_row_count": len(grid_rows),
        "grid_panel_counts": [
            len(row.select(":scope > .panel, :scope > article, :scope > section"))
            for row in grid_rows
        ],
        "section_heading_count": len(headings),
        "h3_count": len(doc.select("h3")),
        "paragraph_count": len(doc.select("p")),
        "list_item_count": len(doc.select("li")),
        "table_count": table_count,
        "chart_like_count": chart_count,
        "table_row_count": table_rows,
        "table_cell_count": table_cells,
        "flow_box_count": flow_box_count,
        "formula_dom_count": formula_dom_count,
        "stat_card_count": stat_card_count,
        "contribution_card_count": contribution_card_count,
        "result_band_count": result_band_count,
        "tag_count": len(doc.select(".tag, .badge, .pill")),
        "highlight_emphasis_count": highlight_count,
        "panel_word_avg": round(sum(panel_words) / max(1, len(panel_words)), 2),
        "panel_word_max": max(panel_words or [0]),
        "panel_word_min": min(panel_words or [0]),
        "panels": panels,
        **content,
        "native_information_unit_count": synthesis_units,
        "base_native_information_unit_count": native_units,
    }


def _reference_html_canvas(doc: BeautifulSoup) -> dict[str, Any]:
    style_text = "\n".join(str(style.string or "") for style in doc.select("style"))
    root = doc.select_one(".paper-poster, .poster, main")
    width = _css_px(style_text, r"(?:\.paper-poster|\.poster|html,\s*body|body)\s*\{[^}]*\bwidth\s*:\s*([0-9.]+)px")
    height = _css_px(style_text, r"(?:\.paper-poster|\.poster|html,\s*body|body)\s*\{[^}]*\b(?:height|min-height)\s*:\s*([0-9.]+)px")
    if root is not None:
        data_w = root.get("data-w") or root.get("data-width")
        data_h = root.get("data-h") or root.get("data-height")
        width = int(float(data_w)) if data_w and str(data_w).replace(".", "", 1).isdigit() else width
        height = int(float(data_h)) if data_h and str(data_h).replace(".", "", 1).isdigit() else height
    return {
        "w": width,
        "h": height,
        "aspect_ratio": round(width / height, 4) if width and height else None,
        "orientation": "landscape" if width and height and width > height else "portrait" if width and height and height > width else None,
    }


def _css_px(style_text: str, pattern: str) -> int | None:
    match = re.search(pattern, style_text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    try:
        return int(round(float(match.group(1))))
    except Exception:
        return None


def _reference_html_profile(
    *,
    table_count: int,
    formula_count: int,
    flow_box_count: int,
    model_card_count: int,
    synthesis_units: int,
) -> str:
    if synthesis_units >= 12 and (table_count >= 1 or flow_box_count >= 4 or model_card_count >= 1):
        return "research_synthesis_dense"
    if table_count >= 1:
        return "table_first_benchmark"
    if formula_count >= 2:
        return "theory_text_board"
    return "default"


def _luma(r: int, g: int, b: int) -> float:
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _pixels(img: Image.Image) -> list[Any]:
    reader = getattr(img, "get_flattened_data", None)
    if callable(reader):
        return list(reader())
    return list(img.getdata())


def _saturation(r: int, g: int, b: int) -> float:
    high = max(r, g, b)
    low = min(r, g, b)
    return 0.0 if high == 0 else (high - low) / high


def _grid_mass(img: Image.Image, *, cols: int, rows: int) -> list[dict[str, Any]]:
    rgb = img.convert("RGB")
    w, h = rgb.size
    cells: list[dict[str, Any]] = []
    for row in range(rows):
        for col in range(cols):
            x0 = int(col * w / cols)
            x1 = int((col + 1) * w / cols)
            y0 = int(row * h / rows)
            y1 = int((row + 1) * h / rows)
            crop = rgb.crop((x0, y0, x1, y1))
            data = _pixels(crop)
            if not data:
                mass = 0.0
            else:
                marked = 0
                for r, g, b in data:
                    if _luma(r, g, b) < 245 or _saturation(r, g, b) >= 0.08:
                        marked += 1
                mass = marked / len(data)
            cells.append({
                "row": row,
                "col": col,
                "mass_ratio": round(mass, 4),
            })
    return cells


def _parse_candidates(raw: list[str]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for item in raw:
        if "=" not in item:
            raise SystemExit(f"--candidate must be CASE=PATH, got: {item}")
        case, path = item.split("=", 1)
        out[case.strip()] = Path(path).expanduser().resolve()
    return out


def _candidate_metrics(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    target = _resolve_candidate_path(path)
    if target is None:
        return {
            "path": str(path),
            "error": "candidate preview/html/final dir not found",
        }
    image_path, html_path = target
    payload: dict[str, Any] = {
        "path": str(path),
        "preview_path": str(image_path) if image_path else None,
        "html_path": str(html_path) if html_path else None,
    }
    paper_poster: dict[str, Any] = {}
    run_dir = _candidate_run_dir(path, image_path=image_path, html_path=html_path)
    if run_dir:
        payload["run_dir"] = str(run_dir)
        paper_poster = _paper_poster_artifact_metrics(run_dir)
        if paper_poster:
            payload["paper_poster"] = paper_poster
        layout_signature = _candidate_layout_signature(run_dir)
        if layout_signature:
            payload["layout_signature"] = layout_signature
        content_features = _candidate_design_content_features(run_dir)
        if content_features:
            payload["content_features"] = content_features
    if image_path:
        payload["image"] = image_metrics(image_path)
    if html_path:
        payload["html"] = _merge_authored_paper_poster_html_metrics(
            html_layer_metrics(html_path),
            paper_poster,
        )
    elif paper_poster.get("html_proxy_metrics"):
        payload["html"] = _merge_authored_paper_poster_html_metrics({}, paper_poster)
    payload["content_value_profile"] = _candidate_content_value_profile(payload)
    return payload


def _candidate_layout_signature(run_dir: Path) -> dict[str, Any]:
    spec_path = _latest_design_spec_path(run_dir)
    spec_payload = _read_json(spec_path) if spec_path else None
    spec = _unwrap_design_spec(spec_payload)
    if not isinstance(spec, dict):
        return {}
    artifact = spec.get("html_artifact") if isinstance(spec.get("html_artifact"), dict) else {}
    frames = [frame for frame in (artifact.get("frames") or []) if isinstance(frame, dict)]
    if not frames:
        return {}
    frame = frames[0]
    layout_plan = frame.get("layout_plan") if isinstance(frame.get("layout_plan"), dict) else {}
    slots = [slot for slot in (layout_plan.get("slots") or []) if isinstance(slot, dict)]
    blocks = [block for block in (frame.get("blocks") or []) if isinstance(block, dict)]
    canvas = spec.get("canvas") if isinstance(spec.get("canvas"), dict) else {}
    cw = int(_num(canvas.get("w_px") or canvas.get("w") or 0))
    ch = int(_num(canvas.get("h_px") or canvas.get("h") or 0))
    if cw <= 0 or ch <= 0:
        cw, ch = _layout_signature_canvas_from_slots(slots, blocks)
    quantized_slots = [
        {
            "slot_id": str(slot.get("slot_id") or ""),
            "role": _layout_role_family(slot.get("role") or slot.get("panel_job") or ""),
            "bbox": _quantized_bbox(slot.get("bbox"), cw, ch),
        }
        for slot in slots
        if isinstance(slot.get("bbox"), dict)
    ]
    visual_blocks = [
        block for block in blocks
        if str(block.get("kind") or "").lower() in {"image", "table"}
        and not _layout_signature_is_identity(block)
        and isinstance(block.get("bbox"), dict)
    ]
    quantized_visuals = [
        {
            "role": _layout_role_family(block.get("role") or block.get("caption") or ""),
            "bbox": _quantized_bbox(block.get("bbox"), cw, ch),
        }
        for block in visual_blocks
    ]
    row_count = len({item["bbox"][1] for item in quantized_slots if item.get("bbox")})
    col_count = len({item["bbox"][0] for item in quantized_slots if item.get("bbox")})
    signature_core = {
        "archetype": str(layout_plan.get("archetype") or frame.get("layout") or ""),
        "slot_roles": [item["role"] for item in quantized_slots],
        "slot_bboxes": [item["bbox"] for item in quantized_slots],
        "visual_bboxes": [item["bbox"] for item in quantized_visuals],
        "row_count": row_count,
        "col_count": col_count,
    }
    return {
        **signature_core,
        "source": str(spec_path) if spec_path else None,
        "slot_count": len(quantized_slots),
        "visual_count": len(quantized_visuals),
        "panel_count": len(quantized_slots),
        "slot_ids": [item["slot_id"] for item in quantized_slots],
        "visual_roles": [item["role"] for item in quantized_visuals],
        "topology_hash": hashlib.sha1(
            json.dumps(signature_core, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16],
    }


def _candidate_design_content_features(run_dir: Path) -> dict[str, Any]:
    spec_path = _latest_design_spec_path(run_dir)
    spec_payload = _read_json(spec_path) if spec_path else None
    spec = _unwrap_design_spec(spec_payload)
    if not isinstance(spec, dict):
        return {}
    artifact = spec.get("html_artifact") if isinstance(spec.get("html_artifact"), dict) else {}
    frames = [frame for frame in (artifact.get("frames") or []) if isinstance(frame, dict)]
    if not frames:
        return {}
    frame = frames[0]
    layout_plan = frame.get("layout_plan") if isinstance(frame.get("layout_plan"), dict) else {}
    blocks = _flatten_html_blocks(frame.get("blocks") or [])
    text_layers = [
        block for block in blocks
        if str(block.get("kind") or "").lower() in {"text", "caption", "metric", "table"}
    ]
    image_layers = [
        block for block in blocks
        if str(block.get("kind") or "").lower() in {"image", "table"}
        and not _layout_signature_is_identity(block)
    ]
    text_metrics = _content_feature_metrics_from_layers(text_layers, doc=None)
    role_haystack = " ".join(
        str(item.get(key) or "")
        for item in [layout_plan, *blocks]
        for key in ("archetype", "slot_id", "role", "panel_job", "block_id", "kind", "text", "title", "caption")
    )
    role_hits = sorted(set(text_metrics.get("section_role_hits") or []) | set(_section_role_hits(role_haystack)))
    layout_value_profile = layout_plan.get("value_profile") if isinstance(layout_plan.get("value_profile"), dict) else {}
    return {
        **text_metrics,
        "source": str(spec_path) if spec_path else None,
        "archetype": str(layout_plan.get("archetype") or frame.get("layout") or ""),
        "layout_value_profile": layout_value_profile,
        "slot_count": len([slot for slot in (layout_plan.get("slots") or []) if isinstance(slot, dict)]),
        "block_count": len(blocks),
        "text_block_count": len(text_layers),
        "image_block_count": len(image_layers),
        "section_role_hits": role_hits,
        "section_role_coverage": round(len(role_hits) / 6.0, 3),
    }


def _flatten_html_blocks(raw_blocks: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def walk(item: Any) -> None:
        if not isinstance(item, dict):
            return
        out.append(item)
        for child in item.get("children") or []:
            walk(child)

    for block in raw_blocks or []:
        walk(block)
    return out


def _latest_design_spec_path(run_dir: Path) -> Path | None:
    direct = run_dir / "design_spec.json"
    if direct.exists():
        return direct
    candidates = sorted((run_dir / "specs").glob("design_spec_*.json"))
    for candidate in reversed(candidates):
        if candidate.exists():
            return candidate
    return None


def _unwrap_design_spec(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    nested = payload.get("design_spec")
    if isinstance(nested, dict):
        return nested
    if isinstance(payload.get("html_artifact"), dict):
        return payload
    return None


def _layout_signature_canvas_from_slots(
    slots: list[dict[str, Any]],
    blocks: list[dict[str, Any]],
) -> tuple[int, int]:
    max_x = 0.0
    max_y = 0.0
    for item in [*slots, *blocks]:
        bbox = item.get("bbox") if isinstance(item.get("bbox"), dict) else {}
        max_x = max(max_x, _num(bbox.get("x")) + _num(bbox.get("w")))
        max_y = max(max_y, _num(bbox.get("y")) + _num(bbox.get("h")))
    return max(1, int(round(max_x))), max(1, int(round(max_y)))


def _layout_signature_is_identity(block: dict[str, Any]) -> bool:
    if block.get("is_identity_asset") or block.get("identity_asset_id"):
        return True
    haystack = " ".join(
        str(block.get(key) or "")
        for key in ("role", "source", "source_id", "asset_type", "block_id")
    ).lower()
    return "identity" in haystack or "academic_identity_search" in haystack


def _quantized_bbox(raw: Any, cw: int, ch: int) -> tuple[int, int, int, int]:
    bbox = raw if isinstance(raw, dict) else {}
    return (
        int(round(_num(bbox.get("x")) / max(1, cw) * 24)),
        int(round(_num(bbox.get("y")) / max(1, ch) * 24)),
        int(round(_num(bbox.get("w")) / max(1, cw) * 24)),
        int(round(_num(bbox.get("h")) / max(1, ch) * 24)),
    )


def _layout_role_family(value: Any) -> str:
    text = re.sub(r"[^a-z0-9_ ]+", " ", str(value or "").lower())
    families = [
        "title", "thesis", "workflow", "screenshot", "sequence",
        "architecture", "matrix", "graph", "method", "leaderboard",
        "table", "benchmark", "ablation", "qualitative", "results",
        "evidence", "theorem", "proof", "takeaway", "footer",
    ]
    hits = [family for family in families if family in text]
    return "+".join(hits[:3]) if hits else (text.split() or ["unknown"])[0]


def apply_batch_layout_diversity(results: list[dict[str, Any]]) -> dict[str, Any]:
    indexed: list[tuple[int, str, dict[str, Any]]] = []
    for idx, result in enumerate(results):
        candidate = result.get("candidate") if isinstance(result, dict) else None
        signature = candidate.get("layout_signature") if isinstance(candidate, dict) else None
        if not isinstance(signature, dict) or not signature:
            continue
        case = (result.get("case") or {}).get("slug") or f"case_{idx + 1}"
        indexed.append((idx, str(case), signature))
    if len(indexed) < 3:
        return {
            "candidate_count": len(indexed),
            "status": "not_enough_candidates",
            "repeated_components": [],
        }

    similarities: list[dict[str, Any]] = []
    graph: dict[int, set[int]] = {idx: set() for idx, _, _ in indexed}
    for left_pos in range(len(indexed)):
        left_idx, left_case, left_sig = indexed[left_pos]
        for right_idx, right_case, right_sig in indexed[left_pos + 1:]:
            score = _layout_signature_similarity(left_sig, right_sig)
            similarities.append({
                "left": left_case,
                "right": right_case,
                "similarity": round(score, 3),
                "left_archetype": left_sig.get("archetype"),
                "right_archetype": right_sig.get("archetype"),
            })
            if score >= 0.72:
                graph[left_idx].add(right_idx)
                graph[right_idx].add(left_idx)

    components = _layout_similarity_components(graph)
    repeated = [component for component in components if len(component) >= 3]
    has_blocker_repetition = False
    repeated_details: list[dict[str, Any]] = []
    for component in repeated:
        component_cases = [
            (result.get("case") or {}).get("slug") or f"case_{idx + 1}"
            for idx, result in enumerate(results)
            if idx in component
        ]
        archetypes = sorted({
            str(((results[idx].get("candidate") or {}).get("layout_signature") or {}).get("archetype") or "")
            for idx in component
        })
        all_same_batch = len(component) == len(indexed) and len(archetypes) == 1
        severity = "blocker" if all_same_batch else "high"
        penalty = 0.16 if all_same_batch else 0.10
        has_blocker_repetition = has_blocker_repetition or all_same_batch
        repeated_details.append({
            "cases": component_cases,
            "size": len(component),
            "severity": severity,
            "score_delta": round(-penalty, 3),
            "archetypes": archetypes,
        })
        message = (
            f"batch layout topology repeated across {len(component)} candidates "
            f"({', '.join(component_cases)}); archetypes={', '.join(archetypes) or 'unknown'}"
        )
        for idx in component:
            _append_batch_layout_issue(results[idx], severity=severity, penalty=penalty, message=message)

    return {
        "candidate_count": len(indexed),
        "status": (
            "blocker_template_repetition"
            if has_blocker_repetition
            else "repeated_template"
            if repeated
            else "diverse"
        ),
        "threshold": 0.72,
        "repeated_components": repeated_details,
        "pairwise_similarity": similarities,
        "archetypes": {
            case: signature.get("archetype")
            for _, case, signature in indexed
        },
        "topology_hashes": {
            case: signature.get("topology_hash")
            for _, case, signature in indexed
        },
    }


def _layout_similarity_components(graph: dict[int, set[int]]) -> list[set[int]]:
    seen: set[int] = set()
    components: list[set[int]] = []
    for node in graph:
        if node in seen:
            continue
        stack = [node]
        component: set[int] = set()
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            component.add(current)
            stack.extend(sorted(graph.get(current, set()) - seen))
        components.append(component)
    return components


def _append_batch_layout_issue(
    result: dict[str, Any],
    *,
    severity: str,
    penalty: float,
    message: str,
) -> None:
    comparison = result.get("comparison")
    if not isinstance(comparison, dict):
        return
    issues = comparison.setdefault("issues", [])
    if any(isinstance(issue, dict) and issue.get("id") == "candidate_batch_template_repetition" for issue in issues):
        return
    issues.append({
        "id": "candidate_batch_template_repetition",
        "owner": "layout_storyboard",
        "severity": severity,
        "score_delta": round(-penalty, 3),
        "message": message,
    })
    notes = comparison.setdefault("notes", [])
    notes.append(message)
    score = float(comparison.get("proxy_score") or 0.0)
    comparison["proxy_score"] = round(max(0.0, score - penalty), 3)


def _layout_signature_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    archetype_score = 1.0 if str(left.get("archetype") or "") == str(right.get("archetype") or "") else 0.0
    slot_count_score = 1.0 - min(
        1.0,
        abs(int(left.get("slot_count") or 0) - int(right.get("slot_count") or 0)) / 8.0,
    )
    role_score = _jaccard(left.get("slot_roles") or [], right.get("slot_roles") or [])
    slot_bbox_score = _bbox_sequence_similarity(left.get("slot_bboxes") or [], right.get("slot_bboxes") or [])
    visual_bbox_score = _bbox_sequence_similarity(left.get("visual_bboxes") or [], right.get("visual_bboxes") or [])
    rhythm_score = 0.5 * (
        1.0 - min(1.0, abs(int(left.get("row_count") or 0) - int(right.get("row_count") or 0)) / 6.0)
    ) + 0.5 * (
        1.0 - min(1.0, abs(int(left.get("col_count") or 0) - int(right.get("col_count") or 0)) / 6.0)
    )
    return (
        0.22 * archetype_score
        + 0.12 * slot_count_score
        + 0.18 * role_score
        + 0.28 * slot_bbox_score
        + 0.12 * visual_bbox_score
        + 0.08 * rhythm_score
    )


def _bbox_sequence_similarity(left: list[Any], right: list[Any]) -> float:
    if not left or not right:
        return 0.0
    count = min(len(left), len(right))
    scores = [
        _quantized_bbox_similarity(left[idx], right[idx])
        for idx in range(count)
    ]
    length_penalty = count / max(len(left), len(right))
    return (sum(scores) / max(1, len(scores))) * length_penalty


def _quantized_bbox_similarity(left: Any, right: Any) -> float:
    if not isinstance(left, (list, tuple)) or not isinstance(right, (list, tuple)):
        return 0.0
    if len(left) != 4 or len(right) != 4:
        return 0.0
    lx, ly, lw, lh = [float(item) for item in left]
    rx, ry, rw, rh = [float(item) for item in right]
    l_area = max(0.0, lw) * max(0.0, lh)
    r_area = max(0.0, rw) * max(0.0, rh)
    if l_area <= 0 or r_area <= 0:
        return 0.0
    ix0 = max(lx, rx)
    iy0 = max(ly, ry)
    ix1 = min(lx + lw, rx + rw)
    iy1 = min(ly + lh, ry + rh)
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    union = l_area + r_area - inter
    return inter / union if union > 0 else 0.0


def _jaccard(left: list[Any], right: list[Any]) -> float:
    left_set = {str(item) for item in left if str(item).strip()}
    right_set = {str(item) for item in right if str(item).strip()}
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / max(1, len(left_set | right_set))


def _low_confidence_cases(results: list[dict[str, Any]]) -> list[str]:
    cases: list[str] = []
    for result in results:
        candidate = result.get("candidate") if isinstance(result, dict) else None
        generation = candidate.get("generation") if isinstance(candidate, dict) else None
        if not isinstance(generation, dict):
            continue
        # The v1 harness produces one generated candidate per case. Marking
        # that explicitly keeps reports from treating a two-case run as a
        # high-confidence sample just because two total candidates exist.
        count = int(generation.get("candidate_count") or 1)
        if count <= 1:
            case = result.get("case") if isinstance(result.get("case"), dict) else {}
            cases.append(str(case.get("slug") or "unknown"))
    return cases


def _resolve_candidate_path(path: Path) -> tuple[Path | None, Path | None] | None:
    if path.is_dir():
        final_dir = path / "final" if (path / "final").is_dir() else path
        image = _first_existing(final_dir / name for name in ("preview.png", "poster.png"))
        html = _first_existing(final_dir / name for name in ("poster.html", "index.html"))
        if image or html:
            return image, html
        latest_composite = _latest_composite_candidate(path)
        if latest_composite is not None:
            return latest_composite
        return image, html
    if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
        html = _first_existing(path.parent / name for name in ("poster.html", "index.html"))
        return path, html
    if path.is_file() and path.suffix.lower() == ".html":
        image = _first_existing(path.parent / name for name in ("preview.png", "poster.png"))
        return image, path
    return None


def _latest_composite_candidate(run_dir: Path) -> tuple[Path | None, Path | None] | None:
    composite_root = run_dir / "composites"
    if not composite_root.is_dir():
        return None
    iter_dirs = [path for path in composite_root.glob("iter_*") if path.is_dir()]
    for iter_dir in sorted(iter_dirs, key=_iteration_dir_sort_key, reverse=True):
        image = _first_existing(iter_dir / name for name in ("preview.png", "poster.png"))
        html = _first_existing(iter_dir / name for name in ("poster.html", "index.html"))
        if image or html:
            return image, html
    return None


def _candidate_run_dir(path: Path, *, image_path: Path | None, html_path: Path | None) -> Path | None:
    candidates: list[Path] = []
    if path.is_dir():
        candidates.extend(_run_dir_candidates(path))
    for artifact in (image_path, html_path):
        if artifact:
            candidates.extend(_run_dir_candidates(artifact.parent))
    scored = [
        (_run_dir_marker_score(candidate), candidate)
        for candidate in candidates
        if candidate.exists()
    ]
    scored = [(score, candidate) for score, candidate in scored if score > 0]
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], -len(item[1].parts)), reverse=True)
    return scored[0][1]


def _run_dir_candidates(path: Path) -> list[Path]:
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


def _paper_poster_artifact_metrics(run_dir: Path) -> dict[str, Any]:
    final_html_path = run_dir / "final" / "poster.html"
    if not final_html_path.exists() and (run_dir / "poster.html").exists():
        final_html_path = run_dir / "poster.html"
    final_preview_path = run_dir / "final" / "preview.png"
    if not final_preview_path.exists() and (run_dir / "preview.png").exists():
        final_preview_path = run_dir / "preview.png"
    final_html_sha = _path_sha256(final_html_path)
    final_preview_sha = _path_sha256(final_preview_path)
    final_is_authored = _is_authored_paper_poster_html(final_html_path)
    final_manifest_path = _final_json_path(run_dir, "paper_poster_render_manifest.json") or (
        run_dir / "paper_poster_render_manifest.json"
        if (run_dir / "paper_poster_render_manifest.json").exists()
        else None
    )
    final_dom_audit_path = _final_json_path(run_dir, "paper_poster_dom_audit.json") or (
        run_dir / "paper_poster_dom_audit.json"
        if (run_dir / "paper_poster_dom_audit.json").exists()
        else None
    )
    latest_manifest_path = _latest_composite_json_path(run_dir, "paper_poster_render_manifest.json")
    latest_dom_audit_path = _latest_composite_json_path(run_dir, "paper_poster_dom_audit.json")
    manifest_path = final_manifest_path or latest_manifest_path
    dom_audit_path = final_dom_audit_path or latest_dom_audit_path
    contract_audit_path = _final_json_path(run_dir, "poster_plan_contract_audit.json") or _latest_composite_json_path(run_dir, "poster_plan_contract_audit.json")
    manifest = _read_json(manifest_path) if manifest_path else None
    dom_audit = _read_json(dom_audit_path) if dom_audit_path else None
    contract_audit = _read_json(contract_audit_path) if contract_audit_path else None
    if not any(isinstance(item, dict) for item in (manifest, dom_audit, contract_audit)) and not final_html_path.exists():
        return {}
    manifest = manifest if isinstance(manifest, dict) else {}
    dom_audit = dom_audit if isinstance(dom_audit, dict) else {}
    contract_audit = contract_audit if isinstance(contract_audit, dict) else {}
    manifest_matches_final = (
        final_is_authored
        and final_manifest_path is not None
        and _hash_matches(final_html_sha, manifest.get("html_sha256"))
    )
    preview_matches_final = (
        final_is_authored
        and final_preview_path.exists()
        and final_manifest_path is not None
        and _hash_matches(final_preview_sha, manifest.get("preview_sha256"))
    )
    dom_audit_matches_final = (
        final_is_authored
        and final_dom_audit_path is not None
        and (
            _hash_matches(final_html_sha, dom_audit.get("html_sha256"))
            or _hash_matches(final_html_sha, dom_audit.get("dom_audit_html_sha256"))
        )
    )
    artifact_consistency_ok = (
        final_is_authored
        and manifest_matches_final
        and preview_matches_final
        and dom_audit_matches_final
    )
    stale_authored_audit_present = (
        not final_is_authored
        and (
            bool(latest_manifest_path and latest_manifest_path.exists())
            or bool(latest_dom_audit_path and latest_dom_audit_path.exists())
        )
    )
    consistency_findings = _paper_poster_consistency_findings(
        final_html_path=final_html_path,
        final_is_authored=final_is_authored,
        final_manifest_path=final_manifest_path,
        final_dom_audit_path=final_dom_audit_path,
        manifest_matches_final=manifest_matches_final,
        preview_matches_final=preview_matches_final,
        dom_audit_matches_final=dom_audit_matches_final,
        stale_authored_audit_present=stale_authored_audit_present,
    )
    findings = [
        finding for finding in (dom_audit.get("paper_poster_dom_findings") or [])
        if isinstance(finding, dict)
    ] if artifact_consistency_ok else []
    finding_counts = _finding_id_counts(findings)
    dom_metrics = (
        dom_audit.get("paper_poster_dom_metrics")
        if artifact_consistency_ok and isinstance(dom_audit.get("paper_poster_dom_metrics"), dict)
        else {}
    )
    layout_quality_findings = [
        finding for finding in (dom_audit.get("paper_poster_layout_quality_findings") or [])
        if artifact_consistency_ok and isinstance(finding, dict)
    ]
    preview_quality_findings = [
        finding for finding in (dom_audit.get("paper_poster_preview_quality_findings") or [])
        if artifact_consistency_ok and isinstance(finding, dict)
    ]
    render_mode = "authored_html" if final_is_authored else "non_authored_final" if final_html_path.exists() else str(manifest.get("render_mode") or "")
    html_proxy_metrics = (
        _paper_poster_html_proxy_metrics(dom_audit, dom_metrics)
        if artifact_consistency_ok
        else {}
    )
    panel_visual_metrics = (
        _panel_visual_fill_metrics(final_preview_path, dom_audit, dom_metrics)
        if artifact_consistency_ok and final_preview_path.exists()
        else {}
    )
    return {
        "render_mode": render_mode,
        "authored_html": final_is_authored,
        "final_is_authored_html": final_is_authored,
        "artifact_consistency_ok": artifact_consistency_ok,
        "manifest_matches_final": manifest_matches_final,
        "preview_matches_final": preview_matches_final,
        "dom_audit_matches_final": dom_audit_matches_final,
        "stale_authored_audit_present": stale_authored_audit_present,
        "final_html_path": str(final_html_path) if final_html_path.exists() else None,
        "final_preview_path": str(final_preview_path) if final_preview_path.exists() else None,
        "final_html_sha256": final_html_sha,
        "final_preview_sha256": final_preview_sha,
        "manifest_path": str(manifest_path) if manifest_path else None,
        "dom_audit_path": str(dom_audit_path) if dom_audit_path else None,
        "contract_audit_path": str(contract_audit_path) if contract_audit_path else None,
        "final_manifest_path": str(final_manifest_path) if final_manifest_path else None,
        "final_dom_audit_path": str(final_dom_audit_path) if final_dom_audit_path else None,
        "dom_backend": dom_audit.get("paper_poster_dom_backend"),
        "dom_p0_count": int(dom_audit.get("paper_poster_dom_p0_count") or 0) if artifact_consistency_ok else 0,
        "dom_warning_count": len(dom_audit.get("paper_poster_dom_warnings") or []) if artifact_consistency_ok else 0,
        "layout_quality_unresolved_count": len(layout_quality_findings),
        "preview_quality_unresolved_count": len(preview_quality_findings),
        "quality_unresolved_count": len(layout_quality_findings) + len(preview_quality_findings),
        "layout_quality_findings": layout_quality_findings[:16],
        "preview_quality_findings": preview_quality_findings[:16],
        "preview_backend": manifest.get("preview_backend"),
        "preview_fallback_used": bool(manifest.get("preview_fallback_used")) if manifest_matches_final else False,
        "image_not_loaded_count": finding_counts.get("paper-poster-image-not-loaded", 0),
        "block_out_of_bounds_count": finding_counts.get("paper-poster-block-out-of-bounds", 0),
        "root_overflow_count": finding_counts.get("paper-poster-overflow", 0),
        "caption_overlap_count": finding_counts.get("paper-poster-caption-overlap", 0),
        "footer_overlap_count": finding_counts.get("paper-poster-footer-overlap", 0),
        "panel_underfilled_count": finding_counts.get("paper-poster-panel-underfilled", 0),
        "page_like_source_figure_count": finding_counts.get("paper-poster-page-screenshot-source-figure", 0),
        "leaf_visible_word_count": _proxy_metric(
            html_proxy_metrics,
            "authored_leaf_visible_word_count",
            "leaf_visible_word_count",
            "leaf_visible_text_word_count",
        ),
        "native_information_unit_count": _proxy_metric(
            html_proxy_metrics,
            "authored_native_information_unit_count",
            "native_information_unit_count",
            "dom_native_information_unit_count",
        ),
        "dom_native_information_unit_count": _proxy_metric(
            html_proxy_metrics,
            "dom_native_information_unit_count",
            "native_information_unit_count",
        ),
        "figure_area_ratio": dom_metrics.get("figure_area_ratio"),
        "source_provenance_asset_count": dom_metrics.get("source_provenance_asset_count"),
        "source_backed_dom_image_count": dom_metrics.get("source_backed_dom_image_count"),
        "unbacked_source_image_count": dom_metrics.get("unbacked_source_image_count"),
        "page_like_source_asset_count": dom_metrics.get("page_like_source_asset_count"),
        "page_like_source_dom_image_count": dom_metrics.get("page_like_source_dom_image_count"),
        "panel_internal_underfilled_count": dom_metrics.get("panel_internal_underfilled_count"),
        "panel_internal_underfilled_p0_count": dom_metrics.get("panel_internal_underfilled_p0_count"),
        "panel_internal_word_budget_fail_count": dom_metrics.get("panel_internal_word_budget_fail_count"),
        "panel_internal_native_unit_fail_count": dom_metrics.get("panel_internal_native_unit_fail_count"),
        "panel_internal_min_coverage": dom_metrics.get("panel_internal_min_coverage"),
        "panel_internal_avg_coverage": dom_metrics.get("panel_internal_avg_coverage"),
        "panel_internal_max_blank_run_ratio": dom_metrics.get("panel_internal_max_blank_run_ratio"),
        "panel_visual_underfilled_count": panel_visual_metrics.get("panel_visual_underfilled_count"),
        "panel_visual_underfilled_p0_count": panel_visual_metrics.get("panel_visual_underfilled_p0_count"),
        "panel_visual_min_ink_ratio": panel_visual_metrics.get("panel_visual_min_ink_ratio"),
        "panel_visual_avg_ink_ratio": panel_visual_metrics.get("panel_visual_avg_ink_ratio"),
        "panel_visual_min_grid_coverage": panel_visual_metrics.get("panel_visual_min_grid_coverage"),
        "panel_visual_max_blank_run_ratio": panel_visual_metrics.get("panel_visual_max_blank_run_ratio"),
        "selected_source_asset_count": dom_metrics.get("selected_source_asset_count"),
        "selected_source_asset_dom_placed_count": dom_metrics.get("selected_source_asset_dom_placed_count"),
        "selected_source_asset_dom_missing_count": dom_metrics.get("selected_source_asset_dom_missing_count"),
        "source_asset_manifest_sha256": dom_metrics.get("source_asset_manifest_sha256") or manifest.get("source_asset_manifest_sha256"),
        "dom_metrics": dom_metrics,
        "panel_visual_metrics": panel_visual_metrics,
        "html_proxy_metrics": html_proxy_metrics,
        "dom_findings": findings,
        "consistency_findings": consistency_findings,
        "contract_metrics": (
            contract_audit.get("poster_contract_metrics")
            if isinstance(contract_audit.get("poster_contract_metrics"), dict)
            else {}
        ),
    }


def _merge_authored_paper_poster_html_metrics(
    html_metrics: dict[str, Any],
    paper_poster: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(html_metrics or {})
    proxy = paper_poster.get("html_proxy_metrics") if isinstance(paper_poster, dict) else None
    if not isinstance(proxy, dict) or not proxy:
        return merged
    raw_keys = (
        "layer_count",
        "text_layer_count",
        "image_layer_count",
        "visible_text_word_count",
        "leaf_visible_text_word_count",
        "leaf_visible_word_count",
        "group_visible_word_count",
        "max_text_layer_words",
        "avg_text_layer_words",
        "caption_like_text_count",
        "section_label_like_count",
        "visual_area_ratio",
        "top_half_visual_area_ratio",
        "text_area_ratio",
    )
    raw = {
        key: merged.get(key)
        for key in raw_keys
        if key in merged
    }
    max_merge_keys = {
        "native_information_unit_count",
        "dom_native_information_unit_count",
        "native_table_like_count",
        "native_chart_like_count",
        "formula_like_text_count",
        "model_card_like_count",
        "pipeline_like_count",
        "emphasis_signal_count",
        "dom_panel_count",
        "image_backed_panel_count",
        "caption_like_text_count",
        "section_label_like_count",
    }
    prefer_proxy_when_raw_empty_keys = {
        "layer_count",
        "text_layer_count",
        "image_layer_count",
        "visible_text_word_count",
        "leaf_visible_text_word_count",
        "leaf_visible_word_count",
        "authored_visible_text_word_count",
        "authored_leaf_visible_word_count",
        "visual_area_ratio",
        "top_half_visual_area_ratio",
        "authored_visual_area_ratio",
    }
    for key, value in proxy.items():
        if value is not None:
            if key in max_merge_keys:
                merged[key] = max(_safe_float_any(merged.get(key), 0.0), _safe_float_any(value, 0.0))
            elif key in prefer_proxy_when_raw_empty_keys:
                current = _safe_float_any(merged.get(key), 0.0)
                incoming = _safe_float_any(value, 0.0)
                if current <= 0 < incoming or key not in merged:
                    merged[key] = value
            else:
                merged[key] = value
    merged["html_metrics_source"] = "paper_poster_dom_audit"
    if raw:
        merged["raw_html_layer_metrics"] = raw
    return merged


def _proxy_metric(metrics: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = metrics.get(key)
        if value is not None:
            return value
    return None


def _paper_poster_manual_dom_check_payload(dom_audit: dict[str, Any]) -> dict[str, Any]:
    payload = dom_audit.get("manual_dom_check")
    if isinstance(payload, dict):
        return payload
    path_value = dom_audit.get("manual_dom_check_path")
    if not path_value:
        return {}
    try:
        payload = _read_json(Path(str(path_value)))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _paper_poster_dom_root_rect(dom_audit: dict[str, Any]) -> dict[str, Any]:
    for key in ("dom_root_rect", "rootRect"):
        value = dom_audit.get(key)
        if isinstance(value, dict):
            return value
    manual = _paper_poster_manual_dom_check_payload(dom_audit)
    value = manual.get("rootRect")
    return value if isinstance(value, dict) else {}


def _paper_poster_dom_image_records(dom_audit: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("dom_image_ink_records", "imageInkRecords"):
        records = dom_audit.get(key)
        if isinstance(records, list):
            return [record for record in records if isinstance(record, dict)]
    manual = _paper_poster_manual_dom_check_payload(dom_audit)
    records = manual.get("imageInkRecords")
    if isinstance(records, list):
        return [record for record in records if isinstance(record, dict)]
    return []


def _paper_poster_image_record_bbox(record: dict[str, Any]) -> dict[str, Any]:
    for key in ("rect", "bbox"):
        value = record.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _paper_poster_image_record_is_identity(record: dict[str, Any]) -> bool:
    if bool(record.get("likelyIdentity") or record.get("likely_identity") or record.get("is_identity_asset")):
        return True
    haystack = " ".join(
        str(record.get(key) or "")
        for key in (
            "label",
            "panel",
            "role",
            "sourceId",
            "source_id",
            "layerId",
            "layer_id",
            "blockId",
            "block_id",
            "className",
            "class_name",
        )
    ).lower()
    return any(token in haystack for token in ("logo", "identity", "header", "footer", "affiliation"))


def _paper_poster_html_proxy_metrics_from_dom_metrics(
    dom_audit: dict[str, Any],
    dom_metrics: dict[str, Any],
) -> dict[str, Any]:
    word_count = int(_safe_float_any(
        _proxy_metric(dom_metrics, "visible_text_word_count", "word_count"),
        0.0,
    ))
    leaf_word_count = int(_safe_float_any(
        _proxy_metric(dom_metrics, "leaf_visible_word_count", "leaf_visible_text_word_count"),
        0.0,
    )) or word_count
    text_unit_count = int(_safe_float_any(
        _proxy_metric(dom_metrics, "dom_text_layer_count", "text_unit_count"),
        0.0,
    ))
    image_records = _paper_poster_dom_image_records(dom_audit)
    content_image_records = [
        record for record in image_records
        if not _paper_poster_image_record_is_identity(record)
    ]
    raw_image_count = int(_safe_float_any(
        _proxy_metric(dom_metrics, "dom_image_layer_count", "image_count"),
        0.0,
    ))
    image_layer_count = len(content_image_records) if content_image_records else raw_image_count
    if word_count <= 0 and image_layer_count <= 0:
        return {}
    root = _paper_poster_dom_root_rect(dom_audit)
    cw = int(round(_safe_float_any(
        _proxy_metric(dom_metrics, "root_w_px", "canvas_w_px") or root.get("w"),
        0.0,
    )))
    ch = int(round(_safe_float_any(
        _proxy_metric(dom_metrics, "root_h_px", "canvas_h_px") or root.get("h"),
        0.0,
    )))
    canvas_area = max(1, cw * ch)
    visual_area_ratio = _safe_float_any(
        _proxy_metric(dom_metrics, "authored_visual_area_ratio", "visual_area_ratio", "figure_area_ratio"),
        0.0,
    )
    top_half_visual_area_ratio = _safe_float_any(dom_metrics.get("top_half_visual_area_ratio"), 0.0)
    if cw > 0 and ch > 0 and content_image_records:
        visual_area = sum(
            _bbox_area(_paper_poster_image_record_bbox(record), cw, ch)
            for record in content_image_records
        )
        top_half_visual_area = sum(
            _bbox_area_in_region(
                _paper_poster_image_record_bbox(record),
                cw,
                ch,
                {"x": 0, "y": 0, "w": cw, "h": ch / 2},
            )
            for record in content_image_records
        )
        visual_area_ratio = max(visual_area_ratio, round(visual_area / canvas_area, 4))
        top_half_visual_area_ratio = max(
            top_half_visual_area_ratio,
            round(top_half_visual_area / max(1, cw * ch / 2), 4),
        )
    table_count = int(_safe_float_any(dom_metrics.get("table_count"), 0.0))
    panel_count = int(_safe_float_any(dom_metrics.get("panel_count"), 0.0))
    layer_count = int(_safe_float_any(dom_metrics.get("dom_layer_count"), 0.0)) or int(_safe_float_any(dom_metrics.get("element_count"), 0.0))
    return {
        "canvas": {"w": cw, "h": ch} if cw > 0 and ch > 0 else {},
        "layer_count": layer_count,
        "text_layer_count": text_unit_count,
        "image_layer_count": image_layer_count,
        "identity_image_layer_count": max(0, len(image_records) - len(content_image_records)) if image_records else 0,
        "shape_layer_count": 0,
        "visible_text_word_count": word_count,
        "leaf_visible_text_word_count": leaf_word_count,
        "leaf_visible_word_count": leaf_word_count,
        "authored_visible_text_word_count": word_count,
        "authored_leaf_visible_word_count": leaf_word_count,
        "authored_text_layer_count": text_unit_count,
        "visual_area_ratio": round(visual_area_ratio, 4),
        "authored_visual_area_ratio": round(visual_area_ratio, 4),
        "top_half_visual_area_ratio": round(top_half_visual_area_ratio, 4),
        "dom_panel_count": panel_count,
        "native_table_like_count": table_count,
        "dom_metrics_fallback_used": True,
    }


def _paper_poster_html_proxy_metrics(
    dom_audit: dict[str, Any],
    dom_metrics: dict[str, Any],
) -> dict[str, Any]:
    layers = [
        layer for layer in (dom_audit.get("dom_layers") or [])
        if isinstance(layer, dict)
    ]
    if not layers:
        return _paper_poster_html_proxy_metrics_from_dom_metrics(dom_audit, dom_metrics)
    canvas = _paper_poster_dom_canvas(layers, dom_metrics)
    cw = int(canvas.get("w") or 0)
    ch = int(canvas.get("h") or 0)
    canvas_area = max(1, cw * ch)
    text_layers = _paper_poster_dom_text_layers(layers)
    image_layers = [layer for layer in layers if layer.get("kind") == "image"]
    content_image_layers = [
        layer for layer in image_layers
        if not _is_identity_dom_layer(layer)
    ]
    contract_autofill_layers = [layer for layer in layers if _is_contract_autofill_layer(layer)]
    repair_generated_layers = [layer for layer in layers if _is_repair_generated_layer(layer)]
    authored_text_layers = [
        layer for layer in text_layers
        if not _is_repair_generated_layer(layer)
    ]
    authored_content_image_layers = [
        layer for layer in content_image_layers
        if not _is_repair_generated_layer(layer)
    ]
    shape_layers = [layer for layer in layers if layer.get("kind") == "shape"]
    leaf_text_layers = [
        layer for layer in layers
        if str(layer.get("kind") or "") in {"text", "caption", "metric", "quote", "table"}
        and _word_count(str(layer.get("text") or "")) > 0
    ]
    authored_leaf_text_layers = [
        layer for layer in leaf_text_layers
        if not _is_repair_generated_layer(layer)
    ]
    authored_leaf_non_table_text_layers = [
        layer for layer in authored_leaf_text_layers
        if str(layer.get("kind") or "").lower() != "table"
        and str(layer.get("tag") or "").lower() != "table"
    ]
    authored_leaf_table_layers = [
        layer for layer in authored_leaf_text_layers
        if str(layer.get("kind") or "").lower() == "table"
        or str(layer.get("tag") or "").lower() == "table"
    ]
    group_text_layers = [
        layer for layer in layers
        if str(layer.get("kind") or "") == "group"
        and _word_count(str(layer.get("text") or "")) > 0
    ]
    leaf_word_counts = [_word_count(str(layer.get("text") or "")) for layer in leaf_text_layers]
    word_counts = [_word_count(str(layer.get("text") or "")) for layer in text_layers]
    authored_leaf_word_counts = [_word_count(str(layer.get("text") or "")) for layer in authored_leaf_text_layers]
    authored_leaf_non_table_word_counts = [
        _word_count(str(layer.get("text") or ""))
        for layer in authored_leaf_non_table_text_layers
    ]
    authored_leaf_table_word_counts = [
        _word_count(str(layer.get("text") or ""))
        for layer in authored_leaf_table_layers
    ]
    quality_word_counts = authored_leaf_non_table_word_counts or authored_leaf_word_counts or leaf_word_counts or word_counts
    word_count = sum(word_counts)
    leaf_word_count = sum(leaf_word_counts)
    authored_word_count = sum(_layer_word_count(layer) for layer in authored_text_layers)
    authored_leaf_word_count = sum(_layer_word_count(layer) for layer in authored_leaf_text_layers)
    contract_autofill_word_count = sum(
        _layer_word_count(layer)
        for layer in text_layers
        if _is_contract_autofill_layer(layer)
    )
    max_words = max(quality_word_counts or [0])
    visual_area = sum(
        _bbox_area(layer.get("bbox") or {}, cw, ch)
        for layer in content_image_layers
    )
    authored_visual_area = sum(
        _bbox_area(layer.get("bbox") or {}, cw, ch)
        for layer in authored_content_image_layers
    )
    repair_visual_area = sum(
        _bbox_area(layer.get("bbox") or {}, cw, ch)
        for layer in content_image_layers
        if _is_repair_generated_layer(layer)
    )
    text_area = sum(
        _bbox_area(layer.get("bbox") or {}, cw, ch)
        for layer in text_layers
    )
    top_half_visual_area = sum(
        _bbox_area_in_region(layer.get("bbox") or {}, cw, ch, {"x": 0, "y": 0, "w": cw, "h": ch / 2})
        for layer in content_image_layers
    )
    font_sizes = [
        int(layer.get("font_size_px") or 0)
        for layer in text_layers
        if int(layer.get("font_size_px") or 0) > 0
    ]
    content_features = _content_feature_metrics_from_layers(text_layers, doc=None)
    layer_native_units = _dom_layer_native_information_unit_count(layers)
    authored_native_units = _dom_layer_native_information_unit_count(
        layers,
        exclude_repair_generated=True,
    )
    contract_autofill_native_units = _dom_layer_native_information_unit_count(
        contract_autofill_layers,
        exclude_repair_generated=False,
    )
    if layer_native_units:
        content_features["dom_native_information_unit_count"] = max(
            int(_safe_float_any(content_features.get("dom_native_information_unit_count"), 0.0)),
            layer_native_units,
        )
        content_features["native_information_unit_count"] = max(
            int(_safe_float_any(content_features.get("native_information_unit_count"), 0.0)),
            layer_native_units,
        )
    content_features["authored_native_information_unit_count"] = authored_native_units
    mixed_panel_metrics = _mixed_panel_binding_metrics(
        content_image_layers,
        text_layers,
        cw=cw,
        ch=ch,
    )
    authored_mixed_panel_metrics = _mixed_panel_binding_metrics(
        authored_content_image_layers,
        authored_text_layers,
        cw=cw,
        ch=ch,
    )
    typography_metrics = _poster_typography_contract_metrics(authored_leaf_text_layers)
    palette_metrics = _poster_palette_contract_metrics(layers)
    return {
        "canvas": canvas,
        "layer_count": len(layers),
        "text_layer_count": len(text_layers),
        "image_layer_count": len(content_image_layers),
        "identity_image_layer_count": len(image_layers) - len(content_image_layers),
        "shape_layer_count": len(shape_layers),
        "visible_text_word_count": word_count,
        "leaf_visible_text_word_count": leaf_word_count,
        "leaf_visible_word_count": leaf_word_count,
        "group_visible_word_count": sum(_word_count(str(layer.get("text") or "")) for layer in group_text_layers),
        "max_text_layer_words": max_words,
        "max_table_layer_words": max(authored_leaf_table_word_counts or [0]),
        "avg_text_layer_words": round(sum(quality_word_counts) / max(1, len(quality_word_counts)), 2),
        "caption_like_text_count": sum(1 for layer in text_layers if _is_caption_like(layer)),
        "section_label_like_count": sum(1 for layer in text_layers if _is_section_label_like(layer)),
        "visual_area_ratio": round(visual_area / canvas_area, 4),
        "top_half_visual_area_ratio": round(top_half_visual_area / max(1, cw * ch / 2), 4),
        "text_area_ratio": round(text_area / canvas_area, 4),
        "font_size_min": min(font_sizes) if font_sizes else None,
        "font_size_max": max(font_sizes) if font_sizes else None,
        "authored_visible_text_word_count": authored_word_count,
        "authored_leaf_visible_word_count": authored_leaf_word_count,
        "authored_text_layer_count": len(authored_text_layers),
        "authored_native_information_unit_count": authored_native_units,
        "authored_visual_area_ratio": round(authored_visual_area / canvas_area, 4),
        "authored_mixed_panel_visual_count": authored_mixed_panel_metrics.get("mixed_panel_visual_count"),
        "authored_mixed_panel_binding_score": authored_mixed_panel_metrics.get("mixed_panel_binding_score"),
        "contract_autofill_block_count": len(contract_autofill_layers),
        "contract_autofill_word_count": contract_autofill_word_count,
        "contract_autofill_native_unit_count": contract_autofill_native_units,
        "repair_generated_block_count": len(repair_generated_layers),
        "repair_generated_visual_area_ratio": round(repair_visual_area / canvas_area, 4),
        "auto_source_block_count": sum(
            1
            for layer in repair_generated_layers
            if "auto_source" in _layer_identity_blob(layer)
            or "contract-auto-source" in _layer_identity_blob(layer)
        ),
        **mixed_panel_metrics,
        **typography_metrics,
        **palette_metrics,
        **content_features,
    }


def _paper_poster_dom_text_layers(layers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    explicit = [
        layer for layer in layers
        if str(layer.get("kind") or "") in {"text", "caption", "metric", "quote"}
        and _word_count(str(layer.get("text") or "")) > 0
    ]
    tables = [
        layer for layer in layers
        if str(layer.get("kind") or "") == "table"
        and _word_count(str(layer.get("text") or "")) > 0
    ]
    groups = [
        layer for layer in layers
        if str(layer.get("kind") or "") == "group"
        and _word_count(str(layer.get("text") or "")) >= 4
    ]
    explicit_words = sum(_word_count(str(layer.get("text") or "")) for layer in explicit + tables)
    group_words = sum(_word_count(str(layer.get("text") or "")) for layer in groups)
    if groups and (explicit_words == 0 or group_words > max(120, int(explicit_words * 1.5))):
        return groups
    return explicit + tables


def _dom_layer_native_information_unit_count(
    layers: list[dict[str, Any]],
    *,
    exclude_repair_generated: bool = False,
) -> int:
    count = 0
    seen: set[str] = set()
    semantic_tokens = (
        "native-info-unit",
        "metric-card",
        "stat-card",
        "result-band",
        "flow-box",
        "formula",
        "callout",
        "insight-card",
        "contribution-card",
        "benchmark-table",
        "promoted-contract-unit",
    )
    for layer in layers:
        if exclude_repair_generated and _is_repair_generated_layer(layer):
            continue
        layer_id = str(layer.get("layer_id") or layer.get("block_id") or "")
        if layer_id and layer_id in seen:
            continue
        kind = str(layer.get("kind") or "").lower()
        blob = " ".join(str(layer.get(key) or "") for key in ("role", "name", "class_name", "layer_id", "block_id")).lower()
        if kind not in {"text", "caption", "metric", "quote", "table", "group", "shape"}:
            continue
        if "grid" in blob and not any(token in blob for token in ("info-unit", "metric-card", "result-band", "flow-box")):
            continue
        if kind != "table" and not any(token in blob for token in semantic_tokens):
            continue
        if kind != "table" and _word_count(str(layer.get("text") or "")) < 3:
            continue
        if layer_id:
            seen.add(layer_id)
        count += 1
    return min(count, 48)


def _paper_poster_dom_canvas(
    layers: list[dict[str, Any]],
    dom_metrics: dict[str, Any],
) -> dict[str, int]:
    cw = int(round(_num(dom_metrics.get("root_w_px"))))
    ch = int(round(_num(dom_metrics.get("root_h_px"))))
    if cw > 0 and ch > 0:
        return {"w": cw, "h": ch}
    max_right = 0.0
    max_bottom = 0.0
    for layer in layers:
        bbox = layer.get("bbox") if isinstance(layer.get("bbox"), dict) else {}
        max_right = max(max_right, _num(bbox.get("x")) + _num(bbox.get("w")))
        max_bottom = max(max_bottom, _num(bbox.get("y")) + _num(bbox.get("h")))
    return {"w": int(round(max_right)), "h": int(round(max_bottom))}


def _mixed_panel_binding_metrics(
    image_layers: list[dict[str, Any]],
    text_layers: list[dict[str, Any]],
    *,
    cw: int,
    ch: int,
) -> dict[str, Any]:
    if not image_layers:
        return {
            "mixed_panel_visual_count": 0,
            "mixed_panel_binding_score": 0.0,
            "mixed_panel_local_text_words": 0,
        }
    bound_visuals = 0
    local_words_total = 0
    for image in image_layers:
        local_words = 0
        for text_layer in text_layers:
            text = str(text_layer.get("text") or "")
            words = _word_count(text)
            if words <= 0:
                continue
            if _text_layer_binds_visual(image, text_layer, cw=cw, ch=ch):
                local_words += min(40, words)
        if local_words >= 8:
            bound_visuals += 1
            local_words_total += local_words
    return {
        "mixed_panel_visual_count": bound_visuals,
        "mixed_panel_binding_score": round(bound_visuals / max(1, len(image_layers)), 3),
        "mixed_panel_local_text_words": int(local_words_total),
    }


def _poster_panel_rule_metrics(doc: BeautifulSoup | None) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "dom_panel_count": 0,
        "image_backed_panel_count": 0,
        "image_backed_panel_text_low_count": 0,
        "underfilled_panel_count": 0,
        "section_title_content_mismatch_count": 0,
        "figure_number_caption_count": 0,
        "terse_figure_number_caption_count": 0,
        "image_panel_min_words": 0,
        "image_panel_avg_words": 0.0,
        "panel_rule_samples": [],
    }
    if doc is None:
        return defaults

    panels = _poster_panel_nodes(doc)
    if not panels:
        return defaults

    image_backed = 0
    image_low_text = 0
    underfilled = 0
    title_mismatches = 0
    fig_caption_count = 0
    terse_caption_count = 0
    image_panel_words: list[int] = []
    samples: list[dict[str, Any]] = []

    for index, panel in enumerate(panels, start=1):
        head = _poster_panel_heading(panel)
        head_text = _clean_inline_text(head.get_text(" ", strip=True) if head else "")
        full_text = _clean_inline_text(panel.get_text(" ", strip=True))
        body_text = full_text
        if head_text and body_text.lower().startswith(head_text.lower()):
            body_text = body_text[len(head_text):].strip(" :-—–|")
        words = _word_count(body_text)
        image_count = _panel_image_count(panel)
        native_structure_count = len(panel.select("table, tr, .stat, .metric-card, .kpi, .stat-card, .flow-box, .formula, .result-band, .callout, .insight-card"))
        caption_texts = _panel_caption_texts(panel)
        panel_fig_caption_count = sum(1 for text in caption_texts if _starts_with_figure_or_table_number(text))
        panel_terse_caption_count = sum(1 for text in caption_texts if _is_terse_figure_number_caption(text))
        fig_caption_count += panel_fig_caption_count
        terse_caption_count += panel_terse_caption_count

        if image_count:
            image_backed += 1
            image_panel_words.append(words)
            min_words = 18 + 8 * max(0, image_count - 1)
            if words < min_words:
                image_low_text += 1
                _append_panel_rule_sample(
                    samples,
                    "image_panel_text_low",
                    index=index,
                    head=head_text,
                    detail=f"{words} words for {image_count} visual(s); target >= {min_words}",
                )
            if words < min_words and native_structure_count == 0:
                underfilled += 1
        elif words < 18 and native_structure_count == 0:
            underfilled += 1
            _append_panel_rule_sample(
                samples,
                "underfilled_text_panel",
                index=index,
                head=head_text,
                detail=f"{words} words and no native table/card/pipeline structure",
            )

        expected_role = _section_title_expected_role(head_text)
        if expected_role and not _panel_content_matches_role(expected_role, body_text):
            title_mismatches += 1
            _append_panel_rule_sample(
                samples,
                "section_title_content_mismatch",
                index=index,
                head=head_text,
                detail=f"title suggests {expected_role}, but panel text reads as {_dominant_panel_role(body_text) or 'unclear'}",
            )

    return {
        "dom_panel_count": len(panels),
        "image_backed_panel_count": image_backed,
        "image_backed_panel_text_low_count": image_low_text,
        "underfilled_panel_count": underfilled,
        "section_title_content_mismatch_count": title_mismatches,
        "figure_number_caption_count": fig_caption_count,
        "terse_figure_number_caption_count": terse_caption_count,
        "image_panel_min_words": min(image_panel_words or [0]),
        "image_panel_avg_words": round(sum(image_panel_words) / max(1, len(image_panel_words)), 2),
        "panel_rule_samples": samples,
    }


def _poster_panel_nodes(doc: BeautifulSoup) -> list[Any]:
    root = doc.select_one(".paper-poster, .poster, main, body")
    raw = list(doc.select(".panel, .slot, article, section, [data-panel-role], [data-slot-id]"))
    panels: list[Any] = []
    seen: set[int] = set()
    for node in raw:
        if id(node) in seen:
            continue
        seen.add(id(node))
        classes = {str(cls) for cls in (node.get("class") or [])}
        if root is not None and node is root:
            continue
        if classes & {"paper-poster", "poster", "canvas", "od-artifact", "deck-slide"}:
            continue
        text = node.get_text(" ", strip=True)
        if not text and not node.select("img, [data-kind='image'], table"):
            continue
        if (
            not _poster_panel_heading(node)
            and not classes.intersection({"panel", "card", "section", "slot"})
            and not node.get("data-panel-role")
            and not node.get("data-slot-id")
        ):
            continue
        panels.append(node)
    if not panels and root is not None:
        panels = [root]
    return panels


def _poster_panel_heading(panel: Any) -> Any | None:
    for selector in (
        ".panel-head",
        ".section-title",
        ".result-band-title",
        ".panel-bar",
        ".panel-label",
        "[data-role='panel-bar']",
        "[data-role='panel-label']",
        "[data-role='section_heading']",
        "h2",
        "h3",
        "header",
    ):
        node = panel.select_one(selector)
        if node is not None and node.get_text(" ", strip=True):
            return node
    return None


def _panel_image_count(panel: Any) -> int:
    image_nodes = panel.select("img, [data-kind='image']")
    return len({id(node) for node in image_nodes})


def _panel_caption_texts(panel: Any) -> list[str]:
    nodes = panel.select("figcaption, .caption, [data-kind='caption'], .layer[data-kind='text'], .od-layer[data-kind='text'], p, li")
    texts: list[str] = []
    seen: set[str] = set()
    for node in nodes:
        text = _clean_inline_text(node.get_text(" ", strip=True))
        if not text or text in seen:
            continue
        seen.add(text)
        if _starts_with_figure_or_table_number(text) or "caption" in " ".join(str(cls) for cls in (node.get("class") or [])).lower():
            texts.append(text)
    return texts


def _append_panel_rule_sample(
    samples: list[dict[str, Any]],
    issue_id: str,
    *,
    index: int,
    head: str,
    detail: str,
    limit: int = 6,
) -> None:
    if len(samples) >= limit:
        return
    samples.append({
        "id": issue_id,
        "panel_index": index,
        "head": head,
        "detail": detail,
    })


def _clean_inline_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _starts_with_figure_or_table_number(text: str) -> bool:
    return bool(re.match(r"^\s*(?:fig(?:ure)?|table)\.?\s*\d+[a-z]?\b", text, flags=re.IGNORECASE))


def _is_terse_figure_number_caption(text: str) -> bool:
    if not _starts_with_figure_or_table_number(text):
        return False
    words = _word_count(text)
    if words <= 18:
        return True
    explanatory_terms = (
        "shows", "demonstrates", "supports", "explains", "reveals", "indicates",
        "because", "therefore", "compared", "improves", "reduces", "tradeoff",
        "evidence", "takeaway", "suggests",
    )
    lower = text.lower()
    return words <= 28 and not any(term in lower for term in explanatory_terms)


_SECTION_ROLE_TERMS: dict[str, tuple[str, ...]] = {
    "context": (
        "problem", "motivation", "context", "challenge", "why", "gap",
        "limitation", "background", "need",
    ),
    "method": (
        "method", "mechanism", "model", "architecture", "pipeline", "algorithm",
        "objective", "training", "recipe", "encoder", "decoder", "token",
        "perturbation", "forward", "update", "optimizer", "gradient", "loss",
        "backbone", "module",
    ),
    "results": (
        "result", "benchmark", "accuracy", "performance", "metric", "baseline",
        "comparison", "ablation", "table", "score", "improves", "beats",
        "outperforms", "memory", "compute", "evaluation", "curve", "plot",
        "roberta", "sst", "snli", "mnli", "f1",
    ),
    "analysis": (
        "analysis", "behavior", "interpretation", "why it matters", "takeaway",
        "insight", "supports", "evidence", "suggests", "explains",
    ),
    "limitations": (
        "limitation", "future", "open question", "risk", "remaining", "fails",
        "caveat", "not yet", "next",
    ),
}


def _section_title_expected_role(title: str) -> str | None:
    lower = _strip_section_number(title).lower()
    if not lower:
        return None
    if any(term in lower for term in ("limitation", "future", "open")):
        return "limitations"
    if any(term in lower for term in ("method", "mechanism", "model", "architecture", "pipeline", "recipe", "objective")):
        return "method"
    if any(term in lower for term in ("result", "benchmark", "metric", "evidence", "ablation", "scale", "comparison", "slice")):
        return "results"
    if any(term in lower for term in ("motivation", "problem")):
        return "context"
    return None


def _strip_section_number(text: str) -> str:
    return re.sub(r"^\s*\d+\s*[·.\-:]\s*", "", text or "").strip()


def _panel_role_scores(text: str) -> dict[str, int]:
    lower = text.lower()
    scores: dict[str, int] = {}
    for role, terms in _SECTION_ROLE_TERMS.items():
        score = 0
        for term in terms:
            if term in lower:
                score += 1
        scores[role] = score
    return scores


def _dominant_panel_role(text: str) -> str | None:
    scores = _panel_role_scores(text)
    if not scores:
        return None
    role, score = max(scores.items(), key=lambda item: item[1])
    return role if score > 0 else None


def _panel_content_matches_role(expected_role: str, text: str) -> bool:
    scores = _panel_role_scores(text)
    expected = int(scores.get(expected_role) or 0)
    dominant = _dominant_panel_role(text)
    if expected >= 2:
        return True
    if expected >= 1 and dominant in {expected_role, None}:
        return True
    # Allow analysis/result overlap because result panels often include
    # interpretive takeaways, but do not let a method title cover pure results.
    if expected_role == "analysis" and int(scores.get("results") or 0) >= 2:
        return True
    if expected_role == "results" and int(scores.get("analysis") or 0) >= 2:
        return True
    return False


def _text_layer_binds_visual(
    image_layer: dict[str, Any],
    text_layer: dict[str, Any],
    *,
    cw: int,
    ch: int,
) -> bool:
    image_box = image_layer.get("bbox") if isinstance(image_layer.get("bbox"), dict) else {}
    text_box = text_layer.get("bbox") if isinstance(text_layer.get("bbox"), dict) else {}
    if not image_box or not text_box:
        return False
    text = str(text_layer.get("text") or "")
    role_blob = " ".join(str(text_layer.get(key) or "") for key in ("layer_id", "name", "role")).lower()
    caption_like = "caption" in role_blob or bool(re.search(r"\b(?:fig(?:ure)?|table)\.?\s*\d+", text.lower()))

    ix = _num(image_box.get("x"))
    iy = _num(image_box.get("y"))
    iw = _num(image_box.get("w"))
    ih = _num(image_box.get("h"))
    tx = _num(text_box.get("x"))
    ty = _num(text_box.get("y"))
    tw = _num(text_box.get("w"))
    th = _num(text_box.get("h"))
    if min(iw, ih, tw, th, cw, ch) <= 0:
        return False

    x_overlap = max(0.0, min(ix + iw, tx + tw) - max(ix, tx))
    y_overlap = max(0.0, min(iy + ih, ty + th) - max(iy, ty))
    horizontal_match = x_overlap >= min(iw, tw) * 0.30
    vertical_match = y_overlap >= min(ih, th) * 0.12
    vertical_gap = min(abs(ty - (iy + ih)), abs(iy - (ty + th)))
    horizontal_gap = min(abs(tx - (ix + iw)), abs(ix - (tx + tw)))
    near_below_or_above = horizontal_match and vertical_gap <= 190
    near_side = vertical_match and horizontal_gap <= 220
    expanded_overlap = _bbox_area_in_region(
        text_box,
        cw,
        ch,
        {
            "x": ix - 180,
            "y": iy - 180,
            "w": iw + 360,
            "h": ih + 360,
        },
    ) > 0
    return bool((caption_like and (near_below_or_above or near_side)) or expanded_overlap)


def _is_identity_dom_layer(layer: dict[str, Any]) -> bool:
    if bool(layer.get("is_identity_asset") or layer.get("identity_asset_id")):
        return True
    haystack = " ".join(
        str(layer.get(key) or "")
        for key in ("layer_id", "role", "source", "source_id", "asset_type")
    ).lower()
    return "identity" in haystack or "academic_identity_search" in haystack


def _final_json_path(run_dir: Path, filename: str) -> Path | None:
    path = run_dir / "final" / filename
    return path if path.exists() else None


def _latest_composite_json_path(run_dir: Path, filename: str) -> Path | None:
    final_path = run_dir / "final" / filename
    if final_path.exists():
        return final_path
    candidates = sorted((run_dir / "composites").glob(f"iter_*/{filename}"))
    for candidate in reversed(candidates):
        if candidate.exists():
            return candidate
    fallback = run_dir / filename
    return fallback if fallback.exists() else None


def _is_authored_paper_poster_html(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        doc = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    except Exception:
        return False
    root = doc.select_one(".paper-poster[data-render-mode='authored_html']")
    if root is not None:
        return True
    root = doc.select_one(".paper-poster")
    return bool(root and str(root.get("data-render-mode") or "") == "authored_html")


def _path_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _hash_matches(expected: str | None, observed: Any) -> bool:
    return bool(expected and observed and str(observed) == expected)


def _paper_poster_consistency_findings(
    *,
    final_html_path: Path,
    final_is_authored: bool,
    final_manifest_path: Path | None,
    final_dom_audit_path: Path | None,
    manifest_matches_final: bool,
    preview_matches_final: bool,
    dom_audit_matches_final: bool,
    stale_authored_audit_present: bool,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if final_html_path.exists() and not final_is_authored:
        findings.append({
            "severity": "P0",
            "id": "candidate_final_not_authored_html",
            "message": "Academic paper poster final/poster.html is not authored HTML.",
            "fix": "Keep the final academic poster on the authored_html renderer path.",
            "stage": "harness_reliability",
            "repair_route": "repair_final_artifact_routing",
        })
    if stale_authored_audit_present:
        findings.append({
            "severity": "P0",
            "id": "candidate_stale_authored_audit_present",
            "message": "Run contains authored DOM audit artifacts that do not correspond to final/poster.html.",
            "fix": "Remove stale authored-only final artifacts or ignore them when final HTML is not authored.",
            "stage": "harness_reliability",
            "repair_route": "repair_final_artifact_routing",
        })
    if final_is_authored and (
        final_manifest_path is None
        or final_dom_audit_path is None
        or not manifest_matches_final
        or not preview_matches_final
        or not dom_audit_matches_final
    ):
        findings.append({
            "severity": "P0",
            "id": "candidate_authored_artifact_mismatch",
            "message": "Authored final HTML is missing matching render manifest or DOM audit artifacts.",
            "fix": "Write final-linked manifest/audit artifacts with hashes matching final/poster.html.",
            "stage": "renderer_export",
            "repair_route": "repair_final_artifact_routing",
            "evidence": {
                "has_final_manifest": final_manifest_path is not None,
                "has_final_dom_audit": final_dom_audit_path is not None,
                "manifest_matches_final": manifest_matches_final,
                "preview_matches_final": preview_matches_final,
                "dom_audit_matches_final": dom_audit_matches_final,
            },
        })
    return findings


def _finding_id_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        issue_id = str(finding.get("id") or "")
        if issue_id:
            counts[issue_id] = counts.get(issue_id, 0) + 1
    return counts


def _read_json(path: Path | None) -> Any:
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _iteration_dir_sort_key(path: Path) -> tuple[int, str]:
    match = re.search(r"iter_(\d+)$", path.name)
    if not match:
        return (-1, path.name)
    return (int(match.group(1)), path.name)


def _first_existing(paths: Any) -> Path | None:
    for path in paths:
        if path.exists():
            return path
    return None


def html_layer_metrics(html_path: Path) -> dict[str, Any]:
    layers = parse_html_layers(html_path)
    canvas = _canvas_size(html_path)
    cw = canvas["w"]
    ch = canvas["h"]
    canvas_area = max(1, cw * ch)
    text_layers = [layer for layer in layers if layer.get("kind") == "text"]
    image_layers = [layer for layer in layers if layer.get("kind") == "image"]
    shape_layers = [layer for layer in layers if layer.get("kind") == "shape"]
    word_counts = [_word_count(str(layer.get("text") or "")) for layer in text_layers]
    word_count = sum(word_counts)
    max_words = max(word_counts or [0])
    font_sizes = [
        int(layer.get("font_size_px") or 0)
        for layer in text_layers
        if int(layer.get("font_size_px") or 0) > 0
    ]
    visual_area = sum(
        _bbox_area(layer.get("bbox") or {}, cw, ch)
        for layer in image_layers
    )
    text_area = sum(
        _bbox_area(layer.get("bbox") or {}, cw, ch)
        for layer in text_layers
    )
    top_half_visual_area = sum(
        _bbox_area_in_region(layer.get("bbox") or {}, cw, ch, {"x": 0, "y": 0, "w": cw, "h": ch / 2})
        for layer in image_layers
    )
    caption_like_count = sum(1 for layer in text_layers if _is_caption_like(layer))
    section_label_count = sum(1 for layer in text_layers if _is_section_label_like(layer))
    try:
        doc = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    except Exception:
        doc = None
    content_features = _content_feature_metrics_from_layers(text_layers, doc=doc)
    panel_rule_metrics = _poster_panel_rule_metrics(doc)
    mixed_panel_metrics = _mixed_panel_binding_metrics(
        [layer for layer in image_layers if not _is_identity_dom_layer(layer)],
        text_layers,
        cw=cw,
        ch=ch,
    )
    typography_metrics = _poster_typography_contract_metrics(text_layers)
    palette_metrics = _poster_palette_contract_metrics(layers)
    return {
        "canvas": canvas,
        "layer_count": len(layers),
        "text_layer_count": len(text_layers),
        "image_layer_count": len(image_layers),
        "shape_layer_count": len(shape_layers),
        "visible_text_word_count": word_count,
        "max_text_layer_words": max_words,
        "avg_text_layer_words": round(word_count / max(1, len(text_layers)), 2),
        "caption_like_text_count": caption_like_count,
        "section_label_like_count": section_label_count,
        "visual_area_ratio": round(visual_area / canvas_area, 4),
        "top_half_visual_area_ratio": round(top_half_visual_area / max(1, cw * ch / 2), 4),
        "text_area_ratio": round(text_area / canvas_area, 4),
        "font_size_min": min(font_sizes) if font_sizes else None,
        "font_size_max": max(font_sizes) if font_sizes else None,
        "authored_visible_text_word_count": word_count,
        "authored_leaf_visible_word_count": word_count,
        "authored_native_information_unit_count": content_features.get("native_information_unit_count"),
        "authored_visual_area_ratio": round(visual_area / canvas_area, 4),
        "contract_autofill_block_count": 0,
        "contract_autofill_word_count": 0,
        "contract_autofill_native_unit_count": 0,
        "repair_generated_block_count": 0,
        **panel_rule_metrics,
        **mixed_panel_metrics,
        **typography_metrics,
        **palette_metrics,
        **content_features,
    }


def _canvas_size(html_path: Path) -> dict[str, int]:
    try:
        doc = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    except Exception:
        return {"w": 0, "h": 0}
    node = doc.select_one(".canvas[data-w][data-h]")
    if node is None:
        node = doc.select_one(".od-frame[data-w][data-h], main[data-w][data-h]")
    return {
        "w": _int_attr(node, "data-w", 0) if node else 0,
        "h": _int_attr(node, "data-h", 0) if node else 0,
    }


def _int_attr(node: Any, key: str, default: int) -> int:
    try:
        return int(float(str(node.get(key) or default)))
    except Exception:
        return default


def _word_count(text: str) -> int:
    return len(re.findall(r"[^\W_]+", text, flags=re.UNICODE))


def _layer_word_count(layer: dict[str, Any]) -> int:
    return _word_count(str(layer.get("text") or ""))


def _layer_identity_blob(layer: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "layer_id",
        "block_id",
        "name",
        "role",
        "class_name",
        "tag",
        "parent_block_id",
        "generated_by",
    ):
        parts.append(str(layer.get(key) or ""))
    parts.extend(str(item) for item in (layer.get("ancestor_block_ids") or []))
    return " ".join(parts).lower()


def _is_contract_autofill_layer(layer: dict[str, Any]) -> bool:
    blob = _layer_identity_blob(layer)
    return any(token in blob for token in (
        "contract-autofill",
        "contract_autofill",
        "contract-slot-autofill",
        "_auto_unit_",
        "contract-info-unit",
    ))


def _is_repair_generated_layer(layer: dict[str, Any]) -> bool:
    blob = _layer_identity_blob(layer)
    return any(token in blob for token in (
        "contract-autofill",
        "contract_autofill",
        "contract-slot-autofill",
        "_auto_unit_",
        "contract-info-unit",
        "contract-auto-source",
        "auto_source",
        "contract-slot-shell",
        "contract-created-slot",
        "_auto_title",
        "auto_contract_fill",
    ))


def _css_color_to_rgba(value: Any) -> tuple[int, int, int, float] | None:
    raw = str(value or "").strip().lower()
    if not raw or raw in {"transparent", "none", "inherit", "initial", "unset"}:
        return None
    if raw.startswith("#"):
        hex_value = raw[1:]
        if len(hex_value) == 3:
            hex_value = "".join(ch * 2 for ch in hex_value)
        if len(hex_value) == 6 and re.fullmatch(r"[0-9a-f]{6}", hex_value):
            return (
                int(hex_value[0:2], 16),
                int(hex_value[2:4], 16),
                int(hex_value[4:6], 16),
                1.0,
            )
    match = re.match(r"rgba?\(([^)]+)\)", raw)
    if match:
        parts = [part.strip() for part in match.group(1).split(",")]
        if len(parts) >= 3:
            try:
                rgb = [max(0, min(255, int(round(float(part.rstrip('%')))))) for part in parts[:3]]
                alpha = float(parts[3]) if len(parts) >= 4 else 1.0
            except ValueError:
                return None
            return rgb[0], rgb[1], rgb[2], max(0.0, min(1.0, alpha))
    named = {
        "black": (0, 0, 0, 1.0),
        "white": (255, 255, 255, 1.0),
        "gray": (128, 128, 128, 1.0),
        "grey": (128, 128, 128, 1.0),
        "red": (255, 0, 0, 1.0),
        "blue": (0, 0, 255, 1.0),
        "green": (0, 128, 0, 1.0),
    }
    return named.get(raw)


def _hex_color(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def _color_luma(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _color_saturation(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    hi = max(r, g, b)
    lo = min(r, g, b)
    if hi <= 0:
        return 0.0
    return (hi - lo) / float(hi)


def _is_neutral_or_paper_color(rgb: tuple[int, int, int]) -> bool:
    luma = _color_luma(rgb)
    spread = max(rgb) - min(rgb)
    if luma >= 220 or luma <= 55:
        return True
    return spread <= 32


def _hue_bucket(rgb: tuple[int, int, int]) -> str:
    r, g, b = rgb
    hi = max(rgb)
    lo = min(rgb)
    if hi == lo:
        return "neutral"
    if hi == r and g >= b:
        return "red_orange"
    if hi == r:
        return "red_purple"
    if hi == g and r >= b:
        return "yellow_green"
    if hi == g:
        return "green_cyan"
    if r >= g:
        return "purple_blue"
    return "blue_cyan"


def _poster_palette_contract_metrics(layers: list[dict[str, Any]]) -> dict[str, Any]:
    colors: set[str] = set()
    accent_colors: set[str] = set()
    saturated_colors: set[str] = set()
    hue_buckets: set[str] = set()
    for layer in layers:
        values: list[Any] = [
            layer.get("fill_color"),
            layer.get("background_color"),
        ]
        values.extend(layer.get("border_colors") or [])
        effects = layer.get("effects") if isinstance(layer.get("effects"), dict) else {}
        values.append(effects.get("fill"))
        for value in values:
            rgba = _css_color_to_rgba(value)
            if rgba is None:
                continue
            r, g, b, alpha = rgba
            if alpha < 0.10:
                continue
            rgb = (r, g, b)
            color = _hex_color(rgb)
            colors.add(color)
            if _is_neutral_or_paper_color(rgb):
                continue
            accent_colors.add(color)
            hue_buckets.add(_hue_bucket(rgb))
            if _color_saturation(rgb) >= 0.34:
                saturated_colors.add(color)
    accent_family_count = len(hue_buckets)
    accent_count = len(accent_colors)
    return {
        "palette_css_color_count": len(colors),
        "palette_accent_color_count": accent_count,
        "palette_accent_family_count": accent_family_count,
        "palette_saturated_color_count": len(saturated_colors),
        "palette_accent_colors": sorted(accent_colors)[:12],
        "palette_accent_family_names": sorted(hue_buckets),
        "palette_contract_pass": accent_count <= 6 and accent_family_count <= 3,
    }


def _font_weight_number(value: Any) -> int:
    raw = str(value or "").strip().lower()
    if raw == "bold":
        return 700
    if raw == "normal":
        return 400
    try:
        return int(round(float(raw)))
    except ValueError:
        return 0


def _is_times_new_roman_family(value: Any) -> bool:
    raw = str(value or "").strip().lower().replace("'", "").replace('"', "")
    return "times new roman" in raw


def _is_heading_or_title_layer(layer: dict[str, Any]) -> bool:
    blob = _layer_identity_blob(layer)
    return any(token in blob for token in (
        "title",
        "heading",
        "panel-head",
        "section_heading",
        "section-title",
        "poster-title",
        "author",
        "affiliation",
        "venue",
    ))


def _poster_typography_contract_metrics(text_layers: list[dict[str, Any]]) -> dict[str, Any]:
    measured = [layer for layer in text_layers if _layer_word_count(layer) > 0]
    if not measured:
        return {
            "typography_text_layer_count": 0,
            "typography_times_new_roman_ratio": 0.0,
            "typography_font_family_violation_count": 0,
            "typography_font_size_level_count": 0,
            "typography_font_size_gradient_ok": False,
            "typography_weight_contract_ok": False,
            "typography_contract_pass": False,
        }
    times_ok = sum(1 for layer in measured if _is_times_new_roman_family(layer.get("font_family")))
    font_sizes = [
        int(round(_safe_float_any(layer.get("font_size_px"), 0.0)))
        for layer in measured
        if _safe_float_any(layer.get("font_size_px"), 0.0) > 0
    ]
    size_levels = sorted({max(1, int(round(size / 2.0) * 2)) for size in font_sizes})
    heading_weights = [
        _font_weight_number(layer.get("font_weight"))
        for layer in measured
        if _is_heading_or_title_layer(layer) and _font_weight_number(layer.get("font_weight")) > 0
    ]
    body_weights = [
        _font_weight_number(layer.get("font_weight"))
        for layer in measured
        if not _is_heading_or_title_layer(layer) and _font_weight_number(layer.get("font_weight")) > 0
    ]
    gradient_ok = 3 <= len(size_levels) <= 9
    if font_sizes:
        gradient_ok = gradient_ok and max(font_sizes) >= min(font_sizes) + 8
    heading_ok = not heading_weights or median(heading_weights) >= 650
    body_ok = not body_weights or median(body_weights) <= 620
    ratio = times_ok / max(1, len(measured))
    return {
        "typography_text_layer_count": len(measured),
        "typography_times_new_roman_ratio": round(ratio, 4),
        "typography_font_family_violation_count": len(measured) - times_ok,
        "typography_font_families": sorted({
            str(layer.get("font_family") or "").strip()
            for layer in measured
            if str(layer.get("font_family") or "").strip()
        })[:8],
        "typography_font_size_level_count": len(size_levels),
        "typography_font_size_levels": size_levels[:12],
        "typography_font_size_gradient_ok": gradient_ok,
        "typography_heading_weight_median": int(median(heading_weights)) if heading_weights else None,
        "typography_body_weight_median": int(median(body_weights)) if body_weights else None,
        "typography_weight_contract_ok": heading_ok and body_ok,
        "typography_contract_pass": ratio >= 0.98 and gradient_ok and heading_ok and body_ok,
    }


def _content_feature_metrics_from_layers(
    layers: list[dict[str, Any]],
    *,
    doc: BeautifulSoup | None,
) -> dict[str, Any]:
    texts = [str(layer.get("text") or "") for layer in layers]
    haystack_parts = [
        str(layer.get(key) or "")
        for layer in layers
        for key in ("layer_id", "name", "role", "kind", "text")
    ]
    haystack = re.sub(r"\s+", " ", " ".join([*haystack_parts, *texts])).strip()
    lower = haystack.lower()
    role_hits = _section_role_hits(haystack)
    template_hits = _template_instruction_text_hits(texts)
    text_quality = _poster_text_quality_metrics(texts, template_hits=template_hits)
    table_count = _native_table_like_count(lower, doc)
    chart_count = _native_chart_like_count(lower, doc)
    formula_count = _formula_like_count(layers, lower)
    dom_native_unit_count = _dom_native_information_unit_count(doc)
    model_card_count = _keyword_family_count(lower, (
        "model card", "parameters", "architecture", "modalities", "modality",
        "training tokens", "license", "backbone", "tokenizer",
    ))
    pipeline_count = _keyword_family_count(lower, (
        "pipeline", "framework", "workflow", "process", "stage", "encoder",
        "decoder", "tokenizer", "rvq", "arrow", "->", "→",
    ))
    emphasis_count = _emphasis_count(doc)
    native_units = max(
        dom_native_unit_count,
        table_count + chart_count + formula_count + model_card_count + pipeline_count,
    )
    return {
        "section_role_hits": role_hits,
        "section_role_coverage": round(len(role_hits) / 6.0, 3),
        "native_table_like_count": table_count,
        "native_chart_like_count": chart_count,
        "formula_like_text_count": formula_count,
        "dom_native_information_unit_count": dom_native_unit_count,
        "model_card_like_count": model_card_count,
        "pipeline_like_count": pipeline_count,
        "emphasis_signal_count": emphasis_count,
        "native_information_unit_count": native_units,
        "template_instruction_text_count": len(template_hits),
        "template_instruction_text_samples": template_hits[:6],
        **text_quality,
    }


def _template_instruction_text_hits(texts: list[str]) -> list[str]:
    patterns = (
        r"\b(?:problem|method|results?|analysis|training|limitations?|conclusion|footer|provenance|contribution|takeaway|benchmark|ablation|model-card|future-work)\s+(?:section|panel)\s*:",
        r"\b(?:fill with|reserve a|use editable|should be split|should carry|source text should|tie each comparison|explain deltas|recover data recipe|distinguish data source|close the story|connect motivation|keep source ids|provenance band)\b",
        r"\b(?:caption must explicitly|primary caption references|secondary inline note references)\b",
        r"\b(?:native_grid|model-card with fields|result-band tying|mechanism checklist|source visual shows supports)\b",
        r"\b(?:\d+\s+|one\s+|two\s+|three\s+|four\s+|five\s+)?(?:tension cards?|numbered contribution cards?|flow-?box stages?|formula cards?|callout cards?|metric bands?|result bands?|mechanism cards?|implication cards?|objective cards?)\b",
    )
    hits: list[str] = []
    seen: set[str] = set()
    for raw in texts:
        text = _clean_inline_text(raw)
        if not text:
            continue
        lower = text.lower()
        if not any(re.search(pattern, lower, flags=re.IGNORECASE) for pattern in patterns):
            continue
        sample = text[:180]
        key = re.sub(r"[^a-z0-9]+", " ", sample.lower()).strip()[:120]
        if key and key not in seen:
            seen.add(key)
            hits.append(sample)
    return hits


def _poster_text_quality_metrics(
    texts: list[str],
    *,
    template_hits: list[str] | None = None,
) -> dict[str, Any]:
    clean_texts = [_clean_inline_text(text) for text in texts]
    candidate_texts = [
        text for text in clean_texts
        if _word_count(text) >= 5 and len(_poster_text_quality_tokens(text)) >= 5
        and not _looks_like_panel_aggregate_text(text)
    ]
    normalized = [_poster_text_quality_key(text) for text in candidate_texts]
    block_counts = Counter(key for key in normalized if key)
    duplicate_groups = [
        (key, count) for key, count in block_counts.items()
        if count >= 2
    ]
    duplicate_groups.sort(key=lambda item: (-item[1], item[0]))
    duplicate_instances = sum(count - 1 for _key, count in duplicate_groups)
    sample_by_key = {
        _poster_text_quality_key(text): text[:180]
        for text in candidate_texts
        if _poster_text_quality_key(text)
    }

    repeated_ngrams = _poster_repeated_ngram_groups(candidate_texts, n=8, min_blocks=3)
    if not repeated_ngrams:
        repeated_ngrams = _poster_repeated_ngram_groups(candidate_texts, n=6, min_blocks=4)
    repeated_instances = sum(count - 1 for _gram, count in repeated_ngrams)
    template_count = len(template_hits or [])
    debt = min(
        1.0,
        (duplicate_instances / 14.0)
        + (len(repeated_ngrams) / 16.0)
        + (template_count / 8.0),
    )
    return {
        "duplicate_text_block_group_count": len(duplicate_groups),
        "duplicate_text_block_count": duplicate_instances,
        "duplicate_text_block_samples": [
            f"{sample_by_key.get(key, key)[:150]} x{count}"
            for key, count in duplicate_groups[:6]
        ],
        "repeated_ngram_group_count": len(repeated_ngrams),
        "repeated_ngram_instance_count": repeated_instances,
        "repeated_ngram_samples": [
            f"{gram} x{count}" for gram, count in repeated_ngrams[:6]
        ],
        "text_repetition_debt_score": round(debt, 4),
    }


def _poster_text_quality_key(text: str) -> str:
    tokens = _poster_text_quality_tokens(text)
    return " ".join(tokens[:42])


def _looks_like_panel_aggregate_text(text: str) -> bool:
    clean = _clean_inline_text(text)
    if _word_count(clean) < 18:
        return False
    if re.match(r"^\d{2}\s+[A-Z][A-Z0-9 &/+\-]{2,}\s+", clean):
        return True
    if clean.upper().startswith("DENSE SYNTHESIS ") and _word_count(clean) >= 20:
        return True
    return False


def _poster_text_quality_tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9][a-z0-9_+./:%-]*", str(text or "").lower())


def _poster_repeated_ngram_groups(
    texts: list[str],
    *,
    n: int,
    min_blocks: int,
) -> list[tuple[str, int]]:
    gram_blocks: dict[tuple[str, ...], set[int]] = {}
    for idx, text in enumerate(texts):
        tokens = _poster_text_quality_tokens(text)
        if len(tokens) < n:
            continue
        seen_in_block = {
            tuple(tokens[pos: pos + n])
            for pos in range(0, len(tokens) - n + 1)
        }
        for gram in seen_in_block:
            if not any(len(token) >= 4 or any(ch.isdigit() for ch in token) for token in gram):
                continue
            gram_blocks.setdefault(gram, set()).add(idx)
    repeated = [
        (" ".join(gram), len(blocks))
        for gram, blocks in gram_blocks.items()
        if len(blocks) >= min_blocks
    ]
    repeated.sort(key=lambda item: (-item[1], item[0]))
    return repeated[:24]


def _section_role_hits(text: Any) -> list[str]:
    lower = str(text or "").lower()
    role_tokens = {
        "motivation": (
            "motivation", "abstract", "background", "problem", "question",
            "challenge", "bottleneck", "why",
        ),
        "contribution": (
            "contribution", "principal", "key contribution", "core contribution",
            "takeaway", "finding", "insight",
        ),
        "method": (
            "method", "framework", "architecture", "model", "algorithm",
            "training", "tokenizer", "pipeline", "proof", "theorem",
        ),
        "results": (
            "result", "results", "benchmark", "leaderboard", "accuracy",
            "ablation", "evaluation", "metric", "experiment", "comparison",
        ),
        "analysis": (
            "analysis", "ablation", "limitation", "limitations", "future",
            "discussion", "assumption", "assumptions",
        ),
        "conclusion": (
            "conclusion", "summary", "takeaway", "future work", "outlook",
            "impact",
        ),
    }
    return sorted(role for role, tokens in role_tokens.items() if any(token in lower for token in tokens))


def _native_table_like_count(lower: str, doc: BeautifulSoup | None) -> int:
    count = 0
    if doc is not None:
        count += len(doc.select("table, [role='table'], .table, .leaderboard, .results-table, .benchmark-table"))
    if any(token in lower for token in ("leaderboard", "benchmark", "results table", "ablation", "accuracy", "map", "auc", "f1")):
        count += 1
    metric_tokens = len(re.findall(r"\b\d+(?:\.\d+)?\s*(?:%|acc|map|auc|f1|bleu|rouge)?\b", lower))
    if metric_tokens >= 12 and any(token in lower for token in ("ours", "baseline", "benchmark", "result", "model")):
        count += 1
    return min(count, 4)


def _native_chart_like_count(lower: str, doc: BeautifulSoup | None) -> int:
    count = 0
    if doc is not None:
        count += len(doc.select(
            ".chart, .curve, .plot, .chart-card, .curve-card, .plot-card, "
            "[data-chart], [data-block-kind='chart'], svg.chart, canvas.chart"
        ))
    chart_tokens = (
        "chart", "curve", "plot", "scale curve", "compute curve", "accuracy curve",
        "attention map", "attention rollout", "interpretability", "trend line",
        "phase diagram", "scaling law",
    )
    token_hits = sum(1 for token in chart_tokens if token in lower)
    if token_hits >= 5:
        count += 3
    elif token_hits >= 3:
        count += 2
    elif token_hits >= 1:
        count += 1
    return min(count, 4)


def _dom_native_information_unit_count(doc: BeautifulSoup | None) -> int:
    if doc is None:
        return 0
    selectors = (
        ".contract-info-unit, .info-unit, .native-info-unit, "
        "[data-role~='native-info-unit'], [data-role~='metric-card'], "
        "[data-role~='stat-card'], [data-role~='result-band'], [data-role~='flow-box'], "
        "[data-role~='model-card'], [data-role~='formula'], "
        ".metric-card, .stat-card, .result-band, .flow-box, .formula, .callout, "
        ".insight-card, .contribution-card, table, [role='table'], "
        ".chart, .curve, .plot, .chart-card, .curve-card, .plot-card, [data-chart], "
        "[data-block-kind='chart'], svg.chart, canvas.chart"
    )
    seen: set[int] = set()
    count = 0
    for node in doc.select(selectors):
        if _is_repair_generated_node(node):
            continue
        node_id = id(node)
        if node_id in seen:
            continue
        seen.add(node_id)
        name = str(getattr(node, "name", "") or "").lower()
        role_class = " ".join(
            str(part)
            for part in (
                node.get("class") if isinstance(node.get("class"), list) else [node.get("class") or ""],
                [node.get("data-role") or ""],
            )
            for part in (part if isinstance(part, list) else [part])
        ).lower()
        if "grid" in role_class and "info-unit" not in role_class and name not in {"table", "svg", "canvas"}:
            continue
        text = _clean_inline_text(node.get_text(" ", strip=True))
        if name not in {"table", "svg", "canvas"} and _word_count(text) < 3:
            continue
        count += 1
    return min(count, 48)


def _is_repair_generated_node(node: Any) -> bool:
    if not isinstance(node, Tag):
        return False
    parts = [
        str(node.get("data-block-id") or ""),
        str(node.get("data-role") or ""),
        str(node.get("role") or ""),
        str(node.get("class") or ""),
        str(node.get("data-generated-by") or ""),
    ]
    parent = node.parent if isinstance(node.parent, Tag) else None
    while isinstance(parent, Tag):
        parts.extend([
            str(parent.get("data-block-id") or ""),
            str(parent.get("data-role") or ""),
            str(parent.get("role") or ""),
            str(parent.get("class") or ""),
            str(parent.get("data-generated-by") or ""),
        ])
        parent = parent.parent if isinstance(parent.parent, Tag) else None
    blob = " ".join(parts).lower()
    return any(token in blob for token in (
        "contract-autofill",
        "contract_autofill",
        "contract-slot-autofill",
        "_auto_unit_",
        "contract-info-unit",
        "contract-auto-source",
        "auto_source",
        "contract-slot-shell",
        "contract-created-slot",
        "_auto_title",
        "auto_contract_fill",
        "promoted-contract-unit",
        "contract-promoted",
    ))


def _formula_like_count(layers: list[dict[str, Any]], lower: str) -> int:
    count = 0
    formula_patterns = (
        r"\\frac|\\sum|\\math|\\theta|\\lambda|\\mathbf",
        r"[∀∃∑∫≤≥≈]",
        r"\bo\([^)]*\)",
        r"\btheorem\b|\bproof\b|\blemma\b|\bbound\b|\bassumption\b",
    )
    for layer in layers:
        text = str(layer.get("text") or "")
        if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in formula_patterns):
            count += 1
    if count == 0 and any(token in lower for token in ("theorem", "proof", "lower bound", "upper bound", "regret", "sample complexity")):
        count = 1
    return min(count, 5)


def _keyword_family_count(lower: str, tokens: tuple[str, ...]) -> int:
    hits = sum(1 for token in tokens if token.lower() in lower)
    if hits >= 5:
        return 3
    if hits >= 3:
        return 2
    if hits >= 1:
        return 1
    return 0


def _emphasis_count(doc: BeautifulSoup | None) -> int:
    if doc is None:
        return 0
    return min(24, len(doc.select("strong, b, mark, em, .accent, .highlight, .emphasis, [data-emphasis]")))


def _bbox_area(bbox: dict[str, Any], cw: int, ch: int) -> float:
    return _bbox_area_in_region(bbox, cw, ch, {"x": 0, "y": 0, "w": cw, "h": ch})


def _bbox_area_in_region(
    bbox: dict[str, Any],
    cw: int,
    ch: int,
    region: dict[str, Any],
) -> float:
    x = _num(bbox.get("x"))
    y = _num(bbox.get("y"))
    w = _num(bbox.get("w"))
    h = _num(bbox.get("h"))
    rx = max(0.0, min(float(cw), _num(region.get("x"))))
    ry = max(0.0, min(float(ch), _num(region.get("y"))))
    rr = max(0.0, min(float(cw), rx + _num(region.get("w"))))
    rb = max(0.0, min(float(ch), ry + _num(region.get("h"))))
    left = max(rx, min(rr, x))
    top = max(ry, min(rb, y))
    right = max(rx, min(rr, x + w))
    bottom = max(ry, min(rb, y + h))
    return max(0.0, right - left) * max(0.0, bottom - top)


def _num(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _is_caption_like(layer: dict[str, Any]) -> bool:
    text = str(layer.get("text") or "")
    name = " ".join(str(layer.get(key) or "") for key in ("layer_id", "name"))
    haystack = f"{name} {text}".lower()
    return bool(
        re.search(r"\bfig(?:ure)?\.?\s*\d+|\btable\.?\s*\d+|caption|fig_|table_|图\s*\d+|表\s*\d+", haystack)
    )


def _is_section_label_like(layer: dict[str, Any]) -> bool:
    text = " ".join(str(layer.get("text") or "").split())
    structured_haystack = " ".join(
        str(layer.get(key) or "")
        for key in ("layer_id", "name", "role")
    ).lower()
    tokens = (
        "problem", "method", "contribution", "evidence", "result", "results",
        "takeaway", "limitation", "future", "overview", "benchmark",
        "问题", "方法", "贡献", "证据", "结果", "结论", "局限",
    )
    if any(token in structured_haystack for token in tokens):
        return True
    if not text or _word_count(text) > 12:
        return False
    return any(token in f"{structured_haystack} {text}".lower() for token in tokens)


def _generate_case(
    case: PosterCase,
    *,
    out_dir: Path,
    template: str,
    brief: str,
    skip_enhancer: bool,
    no_claim_graph: bool,
    harness_mode: str,
    reference_profile: str | None = None,
) -> dict[str, Any] | None:
    cmd = [
        sys.executable,
        "-m",
        "autodesign.cli",
        "run",
        "--from-file",
        case.paper_path,
        "--template",
        template,
    ]
    if skip_enhancer:
        cmd.append("--skip-enhancer")
    if no_claim_graph:
        cmd.append("--no-claim-graph")
    cmd.append(brief)

    log_path = out_dir / "logs" / f"{case.slug}.log"
    env = os.environ.copy()
    env["POSTER_HARNESS_MODE"] = harness_mode
    env["POSTER_CANVAS_TEMPLATE"] = template
    profile = str(reference_profile or "").strip()
    if profile:
        env["POSTER_REFERENCE_PROFILE"] = profile
    elif "dense-synthesis reference prior" in brief.lower() or "research_synthesis_dense" in brief.lower():
        env["POSTER_REFERENCE_PROFILE"] = "research_synthesis_dense"
    if case.reference_metadata_path:
        env["POSTER_REFERENCE_METADATA_PATH"] = case.reference_metadata_path
    env.update(_harness_env_overrides(harness_mode))
    with log_path.open("w", encoding="utf-8") as log:
        log.write("$ " + " ".join(_shell_quote(part) for part in cmd) + "\n\n")
        log.write(f"# POSTER_HARNESS_MODE={harness_mode}\n")
        for key, value in sorted(_harness_env_overrides(harness_mode).items()):
            log.write(f"# {key}={value}\n")
        log.write("\n")
        log.flush()
        timeout_s = _generation_case_timeout_seconds(harness_mode)
        popen_kwargs: dict[str, Any] = {
            "cwd": str(_REPO_ROOT),
            "env": env,
            "text": True,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
        }
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(cmd, **popen_kwargs)
        timed_out = False
        killed_process_group = False
        try:
            output, _ = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "posix":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                    killed_process_group = True
                except ProcessLookupError:
                    pass
                except Exception:
                    proc.kill()
            else:
                proc.kill()
            output, _ = proc.communicate()
        if output:
            log.write(output)
            log.flush()
        if timed_out:
            timeout_lines = [
                "",
                "terminal_status=timeout",
                f"generation_timeout_seconds={timeout_s}",
            ]
            if killed_process_group:
                timeout_lines.append("killed_process_group=true")
            timeout_lines.append(f"generation_timeout case={case.slug} timeout_s={timeout_s}")
            log.write("\n".join(timeout_lines) + "\n")
        log.write(f"\nexit_code={proc.returncode}\n")

    log_text = log_path.read_text(encoding="utf-8")
    run_dir = _parse_run_dir(log_text)
    status = _parse_generation_status(log_text)
    if "generation_timeout " in log_text:
        status["terminal_status"] = "timeout"
        status["finalized"] = False
        status["generation_timeout"] = True
        status["generation_timeout_seconds"] = timeout_s
        if killed_process_group:
            status["killed_process_group"] = True
    if proc.returncode != 0 or run_dir is None:
        print(f"generation failed for {case.slug}; see {log_path}", file=sys.stderr)
    if run_dir is None:
        return {
            "run_dir": None,
            "log_path": str(log_path),
            "exit_code": proc.returncode,
            "harness_mode": harness_mode,
            "error": "generation failed before reporting a run directory",
            **status,
        }
    if proc.returncode != 0:
        if bool(status.get("generation_timeout")):
            status["error"] = _generation_error_summary(log_text)
        else:
            status["terminal_status"] = "error"
            status["error"] = _generation_error_summary(log_text)
        status["finalized"] = False
        print(f"captured partial run for {case.slug}: {run_dir}", file=sys.stderr)
    return {
        "run_dir": str(run_dir),
        "log_path": str(log_path),
        "exit_code": proc.returncode,
        "harness_mode": harness_mode,
        **status,
    }


def _generation_case_timeout_seconds(harness_mode: str) -> int:
    raw = os.environ.get("POSTER_GENERATION_CASE_TIMEOUT_S", "").strip()
    if raw:
        try:
            base_timeout = max(60, int(float(raw)))
        except ValueError:
            base_timeout = _default_generation_case_timeout_seconds(harness_mode)
    else:
        base_timeout = _default_generation_case_timeout_seconds(harness_mode)
    return base_timeout


def _default_generation_case_timeout_seconds(harness_mode: str) -> int:
    if harness_mode == "dogfood":
        return 900
    if harness_mode == "quality":
        return 1200
    return 720


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _generated_candidate_should_be_evaluated(status: dict[str, Any], *, harness_mode: str) -> bool:
    if not isinstance(status, dict):
        return False
    if harness_mode == "dogfood" and (
        bool(status.get("auto_spec_recovery"))
        or bool(status.get("deterministic_spec_recovery"))
        or int(_safe_float_any(status.get("spec_recovery_count"), 0.0)) > 0
    ):
        status["candidate_excluded_from_eval"] = True
        status["candidate_excluded_reason"] = "dogfood_deterministic_spec_recovery"
        return False
    if bool(status.get("generation_timeout")):
        if _generation_timeout_has_usable_authored_candidate(status, harness_mode=harness_mode):
            status.pop("candidate_excluded_from_eval", None)
            status.pop("candidate_excluded_reason", None)
            status["partial_timeout_candidate_evaluated"] = True
            return True
        status["candidate_excluded_from_eval"] = True
        status["candidate_excluded_reason"] = "generation_case_timeout_no_candidate"
        return False
    return True


def _generation_timeout_has_usable_authored_candidate(
    status: dict[str, Any],
    *,
    harness_mode: str,
) -> bool:
    run_dir_raw = str(status.get("run_dir") or "").strip()
    if not run_dir_raw:
        return False
    run_dir = Path(run_dir_raw).expanduser()
    if not run_dir.exists():
        return False
    candidate = _latest_composite_candidate(run_dir)
    if candidate is None:
        return False
    preview_path, html_path = candidate
    if preview_path is None or html_path is None:
        return False
    if not preview_path.exists() or not html_path.exists():
        return False
    if harness_mode == "dogfood" and not _is_authored_paper_poster_html(html_path):
        return False
    return True


def _case_reference_profile_for_generation(
    case: PosterCase,
    *,
    eval_set: dict[str, Any],
    reference_payload: dict[str, Any] | None,
) -> str | None:
    payload = reference_payload if isinstance(reference_payload, dict) else {}
    for key in ("metadata", "html", "image"):
        row = payload.get(key)
        if not isinstance(row, dict):
            continue
        profile = str(row.get("reference_profile") or row.get("profile") or "").strip()
        if profile:
            return profile
    case_profiles = eval_set.get("case_reference_profiles")
    if isinstance(case_profiles, dict):
        profile = str(case_profiles.get(case.slug) or "").strip()
        if profile:
            return profile
    return None


def _harness_env_overrides(mode: str) -> dict[str, str]:
    if mode == "cheap":
        return {
            "MAX_CRITIQUE_ITERS": "0",
            "MAX_ENV_REPAIR_ATTEMPTS": "1",
            "POSTER_QUALITY_TRACE": "0",
        }
    if mode == "standard":
        return {
            "MAX_CRITIQUE_ITERS": "1",
            "MAX_ENV_REPAIR_ATTEMPTS": "1",
            "POSTER_QUALITY_TRACE": "0",
        }
    if mode == "quality":
        return {
            "MAX_CRITIQUE_ITERS": "2",
            "MAX_ENV_REPAIR_ATTEMPTS": "2",
            "POSTER_QUALITY_TRACE": "0",
        }
    if mode == "dogfood":
        overrides = {
            "MAX_CRITIQUE_ITERS": "2",
            "MAX_ENV_REPAIR_ATTEMPTS": "2",
            "POSTER_QUALITY_TRACE": "1",
            "INGEST_VLM_PARALLELISM": "2",
            "DOGFOOD_INLINE_BLOCKING_REPAIR": "1",
            "DOGFOOD_DISABLE_TIMEOUT_SPEC_RECOVERY": "1",
            "POSTER_ENABLE_DENSE_RECOVERY": "0",
        }
        return overrides
    return {}


def _shell_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:=+-]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _parse_run_dir(text: str) -> Path | None:
    match = re.search(r"Run dir:\s+(.+)", text)
    if not match:
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("event") != "run.start":
                continue
            run_id = str(event.get("run_id") or "").strip()
            if not run_id:
                continue
            candidate = (_REPO_ROOT / "out" / "runs" / run_id).resolve()
            if candidate.exists():
                return candidate
        run_matches = re.findall(r'(/[^\s"]*/out/runs/[^/\s"]+)', text)
        for raw in reversed(run_matches):
            candidate = Path(raw).expanduser().resolve()
            if candidate.exists():
                return candidate
        return None
    return Path(match.group(1).strip()).expanduser().resolve()


def _parse_generation_status(text: str) -> dict[str, Any]:
    terminal = None
    for match in re.finditer(r'"terminal_status":\s*"([^"]+)"', text):
        terminal = match.group(1)
    terminal_match = re.findall(r"Terminal:\s+([A-Za-z_]+)", text)
    if terminal_match:
        terminal = terminal_match[-1]
    finalized = terminal == "pass"
    if (
        '"event": "designer.finalized"' in text
        or '"event": "planner.finalized"' in text
        or re.search(
            r'"event":\s*"tool\.result".*?"tool":\s*"finalize".*?"status":\s*"ok"',
            text,
        )
    ):
        finalized = True
    status: dict[str, Any] = {
        "terminal_status": terminal or "unknown",
        "finalized": finalized,
    }
    for key in ("design_feedback_blockers", "remaining_blocking_findings"):
        values = re.findall(rf'"{key}":\s*(\d+)', text)
        if values:
            status[key] = int(values[-1])
    designer_model = None
    designer_api_errors: list[str] = []
    spec_recovery_reasons: list[str] = []
    dense_recovery_reasons: list[str] = []
    propose_design_spec_calls = 0
    propose_design_spec_missing_payload_calls = 0
    designer_contract_abort: dict[str, Any] | None = None
    auto_spec_recovery = False
    dogfood_report_only_after_blocking_composite = False
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_name = str(event.get("event") or "")
        if event_name in {"designer.start", "planner.start"} and event.get("model"):
            designer_model = str(event.get("model"))
        elif event_name in {"designer.api_error", "planner.api_error"}:
            designer_api_errors.append(_compact_generation_error(event.get("error")))
        elif event_name == "tool.call" and event.get("tool") == "propose_design_spec":
            propose_design_spec_calls += 1
            if event.get("has_design_spec") is False:
                propose_design_spec_missing_payload_calls += 1
        elif event_name in {"designer.contract_abort", "planner.contract_abort"}:
            designer_contract_abort = {
                "reason": str(event.get("reason") or "designer_contract_abort"),
                "repeat_count": int(_safe_float_any(event.get("repeat_count"), 0.0)),
                "owner": str(event.get("owner") or "designer_contract"),
                "severity": str(event.get("severity") or "blocker"),
            }
        elif event_name == "spec.recovered":
            reason = str(event.get("reason") or "").strip()
            spec_recovery_reasons.append(reason or "unknown")
        elif event_name == "paper_poster_html.dense_recovery.start":
            reason = str(event.get("reason") or "").strip()
            dense_recovery_reasons.append(reason or "unknown")
        elif event_name == "run.auto_spec_recovery.done":
            auto_spec_recovery = True
        elif (
            event.get("reason") == "dogfood_report_only_after_blocking_composite"
            or event_name in {"designer.blocking_composite_feedback", "planner.blocking_composite_feedback"}
            and event.get("reason") == "dogfood_report_only_after_blocking_composite"
        ):
            dogfood_report_only_after_blocking_composite = True
    if designer_model:
        status["designer_model"] = designer_model
    if designer_api_errors:
        last_error = designer_api_errors[-1]
        status["designer_api_error"] = last_error
        status["designer_api_error_count"] = len(designer_api_errors)
        status["planner_api_error"] = last_error
        status["planner_api_error_count"] = len(designer_api_errors)
        status["model_routing_error"] = _looks_like_model_routing_error(last_error)
    if propose_design_spec_calls:
        status["propose_design_spec_calls"] = propose_design_spec_calls
        status["propose_design_spec_missing_payload_calls"] = propose_design_spec_missing_payload_calls
    if designer_contract_abort:
        status["designer_contract_abort"] = True
        status["designer_contract_abort_reason"] = designer_contract_abort.get("reason")
        status["designer_contract_abort_repeat_count"] = designer_contract_abort.get("repeat_count")
        status["planner_contract_abort"] = True
        status["planner_contract_abort_reason"] = designer_contract_abort.get("reason")
        status["planner_contract_abort_repeat_count"] = designer_contract_abort.get("repeat_count")
    if spec_recovery_reasons:
        status["spec_recovery_count"] = len(spec_recovery_reasons)
        status["spec_recovery_reason"] = spec_recovery_reasons[-1]
        status["spec_recovery_reasons"] = spec_recovery_reasons
        status["deterministic_spec_recovery"] = True
    if dense_recovery_reasons:
        status["dense_recovery_count"] = len(dense_recovery_reasons)
        status["dense_recovery_reason"] = dense_recovery_reasons[-1]
        status["dense_recovery_reasons"] = dense_recovery_reasons
        status["deterministic_dense_recovery"] = True
    if auto_spec_recovery:
        status["auto_spec_recovery"] = True
    if dogfood_report_only_after_blocking_composite:
        status["dogfood_report_only_after_blocking_composite"] = True
        status["report_only_partial_candidate"] = True
    return status


def _generation_error_summary(text: str) -> str:
    if "openai.APIConnectionError" in text or "APIConnectionError" in text:
        return "api_connection_error"
    if "httpx.ConnectError" in text or "ConnectError" in text:
        return "connection_error"
    traceback_lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(traceback_lines):
        if line.startswith("openai.") or line.startswith("httpx.") or line.startswith("RuntimeError:"):
            return line[:240]
    return "generation exited nonzero"


def _compact_generation_error(raw: Any) -> str:
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    text = re.sub(r"('user_id':\s*)'[^']+'", r"\1'<redacted>'", text)
    text = re.sub(r'("user_id":\s*)"[^"]+"', r'\1"<redacted>"', text)
    return text[:500]


def _looks_like_model_routing_error(error: str) -> bool:
    lowered = error.lower()
    return any(
        marker in lowered
        for marker in (
            "not a valid model id",
            "model id",
            "model not found",
            "no endpoints found",
            "unsupported model",
            "model unavailable",
            "not available in your region",
        )
    )


def summarize_reference_targets(
    metrics: list[dict[str, Any]],
    native_metrics: list[dict[str, Any]] | None = None,
    reference_metadata: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    native_metrics = native_metrics or []
    reference_metadata = reference_metadata or []

    def med(key: str, rows: list[dict[str, Any]] | None = None) -> float | None:
        source = metrics if rows is None else rows
        values = [float(item[key]) for item in source if item.get(key) is not None]
        return round(median(values), 4) if values else None

    orientations: dict[str, int] = {}
    for item in metrics:
        orientation = str(item.get("orientation") or "unknown")
        orientations[orientation] = orientations.get(orientation, 0) + 1
    native_profiles: dict[str, int] = {}
    for item in native_metrics:
        profile = str(item.get("reference_profile") or "default")
        native_profiles[profile] = native_profiles.get(profile, 0) + 1

    return {
        "reference_count": len(metrics),
        "native_reference_count": len(native_metrics),
        "orientation_counts": orientations,
        "median_aspect_ratio": med("aspect_ratio"),
        "median_white_space_ratio": med("white_space_ratio"),
        "median_nonwhite_pixel_ratio": med("nonwhite_pixel_ratio"),
        "median_dark_ink_ratio": med("dark_ink_ratio"),
        "median_longest_blank_vertical_run_ratio": med("longest_blank_vertical_run_ratio"),
        "median_vertical_band_nonwhite_min": med("vertical_band_nonwhite_min"),
        "median_edge_density": med("edge_density"),
        "median_grid_mass_cv": med("grid_mass_cv"),
        "median_empty_cell_ratio": med("empty_cell_ratio"),
        "median_palette_complexity": med("palette_complexity"),
        "native_html": {
            "reference_count": len(native_metrics),
            "profile_counts": native_profiles,
            "median_visible_text_word_count": med("visible_text_word_count", native_metrics),
            "median_panel_count": med("panel_count", native_metrics),
            "median_grid_row_count": med("grid_row_count", native_metrics),
            "median_section_heading_count": med("section_heading_count", native_metrics),
            "median_table_count": med("table_count", native_metrics),
            "median_table_row_count": med("table_row_count", native_metrics),
            "median_table_cell_count": med("table_cell_count", native_metrics),
            "median_flow_box_count": med("flow_box_count", native_metrics),
            "median_formula_count": med("formula_dom_count", native_metrics),
            "median_model_card_like_count": med("model_card_like_count", native_metrics),
            "median_pipeline_like_count": med("pipeline_like_count", native_metrics),
            "median_native_information_unit_count": med("native_information_unit_count", native_metrics),
            "median_highlight_emphasis_count": med("highlight_emphasis_count", native_metrics),
            "panel_heads": _top_native_panel_heads(native_metrics),
        },
        "reference_metadata_summary": _reference_metadata_summary(reference_metadata),
    }


def _reference_metadata_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [row for row in rows if isinstance(row, dict)]
    if not valid:
        return {}

    def med_hint(key: str) -> float | None:
        values: list[float] = []
        for row in valid:
            hint = row.get("reference_metrics_hint") if isinstance(row.get("reference_metrics_hint"), dict) else {}
            value = hint.get(key)
            try:
                values.append(float(value))
            except Exception:
                continue
        return round(median(values), 4) if values else None

    profile_counts: dict[str, int] = {}
    template_counts: dict[str, int] = {}
    priorities: dict[str, int] = {}
    layout_archetypes: dict[str, int] = {}
    required_units: list[str] = []
    generation_prior: list[str] = []
    negative_guidance: list[str] = []
    acceptance_focus: list[str] = []
    text_targets: list[dict[str, Any]] = []
    for row in valid:
        profile = str(row.get("reference_profile") or row.get("profile") or "default").strip() or "default"
        profile_counts[profile] = profile_counts.get(profile, 0) + 1
        template = str(row.get("preferred_template") or "").strip()
        if template:
            template_counts[template] = template_counts.get(template, 0) + 1
        archetype = str(row.get("layout_archetype") or "").strip()
        if archetype:
            layout_archetypes[archetype] = layout_archetypes.get(archetype, 0) + 1
        text_target = row.get("text_synthesis_targets")
        if isinstance(text_target, dict):
            text_targets.append(text_target)
            priority = str(text_target.get("priority") or "").strip()
            if priority:
                priorities[priority] = priorities.get(priority, 0) + 1
        required_units.extend(_metadata_string_list(row.get("required_units")))
        generation_prior.extend(_metadata_string_list(row.get("generation_prior")))
        negative_guidance.extend(_metadata_string_list(row.get("negative_guidance")))
        acceptance_focus.extend(_metadata_string_list(row.get("acceptance_focus")))

    summary = {
        "reference_count": len(valid),
        "profile_counts": profile_counts,
        "preferred_template_counts": template_counts,
        "layout_archetype_counts": layout_archetypes,
        "text_synthesis_priority_counts": priorities,
        "median_target_panel_count": med_hint("target_panel_count"),
        "median_min_panel_count": med_hint("min_panel_count"),
        "median_target_native_information_units": med_hint("target_native_information_units"),
        "median_min_native_information_units": med_hint("min_native_information_units"),
        "median_target_visible_words": med_hint("target_visible_words"),
        "median_min_visible_words": med_hint("min_visible_words"),
        "median_target_table_count": med_hint("target_table_count"),
        "median_target_section_heading_count": med_hint("target_section_heading_count"),
        "required_units": _unique_preserve_order(required_units),
        "generation_prior": _unique_preserve_order(generation_prior)[:12],
        "negative_guidance": _unique_preserve_order(negative_guidance)[:12],
        "acceptance_focus": _unique_preserve_order(acceptance_focus)[:12],
        "text_synthesis_targets": _merge_text_synthesis_targets(text_targets),
    }
    return {key: value for key, value in summary.items() if value not in ({}, [], None)}


def _metadata_string_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item or "").strip()]


def _unique_preserve_order(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.lower()
        if key not in seen:
            out.append(value)
            seen.add(key)
    return out


def _merge_text_synthesis_targets(targets: list[dict[str, Any]]) -> dict[str, Any]:
    if not targets:
        return {}
    merged: dict[str, Any] = {"reference_count": len(targets)}
    priority_order = {"highest": 4, "high": 3, "medium": 2, "low": 1}
    priorities = [
        str(target.get("priority") or "").strip()
        for target in targets
        if str(target.get("priority") or "").strip()
    ]
    if priorities:
        merged["priority"] = max(priorities, key=lambda value: priority_order.get(value.lower(), 0))
    for key in ("density_goal", "correctness_goal", "coherence_goal", "condensation_goal"):
        values = _unique_preserve_order([
            str(target.get(key) or "").strip()
            for target in targets
            if str(target.get(key) or "").strip()
        ])
        if values:
            merged[key] = values[:3]
    for key in ("preferred_text_units", "avoid_text_failures"):
        values: list[str] = []
        for target in targets:
            values.extend(_metadata_string_list(target.get(key)))
        unique = _unique_preserve_order(values)
        if unique:
            merged[key] = unique[:12]
    return merged


def _top_native_panel_heads(native_metrics: list[dict[str, Any]], *, limit: int = 12) -> list[str]:
    heads: list[str] = []
    for item in native_metrics:
        for panel in item.get("panels") or []:
            if not isinstance(panel, dict):
                continue
            head = re.sub(r"\s+", " ", str(panel.get("head") or "")).strip()
            if head and head not in heads:
                heads.append(head)
            if len(heads) >= limit:
                return heads
    return heads


def build_reference_rubric(
    *,
    eval_set: dict[str, Any],
    targets: dict[str, Any],
    results: list[dict[str, Any]],
    template: str | None = None,
) -> dict[str, Any]:
    """Return the compact rubric the self-evolving loop should optimize."""
    cases = [str((result.get("case") or {}).get("slug") or "") for result in results]
    rules = list(eval_set.get("acceptance_rules") or [])
    if _landscape_dominates(targets):
        rules = [
            {
                "id": "computed_landscape_orientation",
                "owner": "layout_storyboard",
                "severity": "blocker",
                "expectation": (
                    "Reference set is landscape-dominant; generated candidates "
                    "should not use a portrait canvas unless the run explicitly opts out."
                ),
            },
            *rules,
        ]
    if float(targets.get("median_edge_density") or 0.0) >= 0.18:
        rules.append({
            "id": "computed_high_evidence_density",
            "owner": "layout_storyboard",
            "severity": "high",
            "expectation": (
                "Reference set has high edge density, which usually means many "
                "screenshots, diagrams, tables, formulas, or plots. Generated "
                "posters should preserve dense visual evidence instead of using "
                "large decorative whitespace."
            ),
        })
    native_targets = targets.get("native_html") if isinstance(targets.get("native_html"), dict) else {}
    native_profile_counts = native_targets.get("profile_counts") if isinstance(native_targets.get("profile_counts"), dict) else {}
    metadata_summary = (
        targets.get("reference_metadata_summary")
        if isinstance(targets.get("reference_metadata_summary"), dict)
        else {}
    )
    metadata_profile_counts = (
        metadata_summary.get("profile_counts")
        if isinstance(metadata_summary.get("profile_counts"), dict)
        else {}
    )
    dense_native_count = int(native_profile_counts.get("research_synthesis_dense") or 0)
    dense_metadata_count = int(metadata_profile_counts.get("research_synthesis_dense") or 0)
    if dense_native_count or dense_metadata_count:
        rules.append({
            "id": "computed_dense_synthesis_native_targets",
            "owner": "content_strategy",
            "severity": "high",
            "expectation": (
                "Reference set includes native HTML dense-synthesis posters. "
                "Generated candidates should use editable model cards, method "
                "pipelines, benchmark tables, formulas, ablation or limitation notes, "
                "limitations/future panels, and synthesis takeaways instead of "
                "treating low screenshot count as low quality."
            ),
        })
        if metadata_summary.get("text_synthesis_targets"):
            rules.append({
                "id": "computed_gold_v1_text_synthesis_targets",
                "owner": "content_strategy",
                "severity": "high",
                "expectation": (
                    "Human-effort gold metadata marks dense edited text, paper-faithful "
                    "claims, and coherent cross-panel condensation as first-class goals."
                ),
            })

    return {
        "eval_set_id": eval_set.get("id"),
        "description": eval_set.get("description"),
        "cases": cases,
        "default_template": eval_set.get("default_template") or DEFAULT_TEMPLATE,
        "case_templates": eval_set.get("case_templates") or {},
        "case_reference_profiles": eval_set.get("case_reference_profiles") or {},
        "reference_mode_guidance": eval_set.get("reference_mode_guidance") or [],
        "reference_traits": list(eval_set.get("reference_traits") or []),
        "proxy_targets": targets,
        "reference_metadata_summary": metadata_summary,
        "text_synthesis_targets": metadata_summary.get("text_synthesis_targets") or {},
        "required_units": metadata_summary.get("required_units") or [],
        "generation_prior": metadata_summary.get("generation_prior") or [],
        "negative_guidance": metadata_summary.get("negative_guidance") or [],
        "dense_synthesis_reference_targets": _dense_synthesis_reference_targets(
            targets,
            template=template,
        ),
        "archetype_target_profiles": ARCHETYPE_TARGET_PROFILES,
        "human_effort_rules": HUMAN_EFFORT_RULES,
        "mechanical_visual_discount_policy": MECHANICAL_VISUAL_DISCOUNT_POLICY,
        "acceptance_rules": rules,
        "owner_taxonomy": OWNER_TAXONOMY,
    }


def _dense_synthesis_reference_targets(
    targets: dict[str, Any],
    *,
    template: str | None = None,
) -> dict[str, Any]:
    native = targets.get("native_html") if isinstance(targets.get("native_html"), dict) else {}
    profile_counts = native.get("profile_counts") if isinstance(native.get("profile_counts"), dict) else {}
    metadata = (
        targets.get("reference_metadata_summary")
        if isinstance(targets.get("reference_metadata_summary"), dict)
        else {}
    )
    metadata_profile_counts = (
        metadata.get("profile_counts")
        if isinstance(metadata.get("profile_counts"), dict)
        else {}
    )
    if not (
        int(profile_counts.get("research_synthesis_dense") or 0)
        or int(metadata_profile_counts.get("research_synthesis_dense") or 0)
    ):
        return {}
    has_metadata_panel_target = metadata.get("median_target_panel_count") not in (None, "")
    has_metadata_min_panel = metadata.get("median_min_panel_count") not in (None, "")
    has_metadata_native_target = metadata.get("median_target_native_information_units") not in (None, "")
    has_metadata_min_native = metadata.get("median_min_native_information_units") not in (None, "")
    has_metadata_word_target = metadata.get("median_target_visible_words") not in (None, "")
    has_metadata_min_words = metadata.get("median_min_visible_words") not in (None, "")
    panel_count = int(round(float(
        metadata.get("median_target_panel_count")
        or native.get("median_panel_count")
        or 8
    )))
    min_panel_count = int(round(float(
        metadata.get("median_min_panel_count")
        or min(6, panel_count)
        or 6
    )))
    native_units = int(round(float(
        metadata.get("median_target_native_information_units")
        or native.get("median_native_information_unit_count")
        or 16
    )))
    min_native_units = int(round(float(
        metadata.get("median_min_native_information_units")
        or min(8, native_units)
        or 8
    )))
    words = int(round(float(
        metadata.get("median_target_visible_words")
        or native.get("median_visible_text_word_count")
        or 900
    )))
    min_words = int(round(float(metadata.get("median_min_visible_words") or 650)))
    required_units = list(metadata.get("required_units") or [])
    if not required_units:
        required_units = [
            "model_card",
            "method_pipeline",
            "benchmark_table",
            "ablation_analysis",
            "limitations_future",
            "synthesis_takeaway",
        ]
    generation_prior = list(metadata.get("generation_prior") or [])
    if not generation_prior:
        generation_prior = [
            "Use a 6-8 main-panel synthesis board when the paper is text/model/system-heavy.",
            "Include a compact three-row title/authors/organization identity band.",
            "Dedicate main panels to problem+contribution, model card, method pipeline, results/table, ablation-analysis, limitations, and takeaway; carry extra detail as internal rows, tables, and notes.",
            "Prefer editable HTML tables, compact comparison tables, formulas, flow boxes, source-grounded bullets, and highlighted claims over decorative screenshots.",
            "Keep dense text posterized: many short sections, strong panel heads, bold key terms, and compact bullets instead of raw abstract paragraphs.",
        ]
    if not has_metadata_min_native:
        min_native_units = min(22, max(18, min_native_units))
    if not has_metadata_native_target:
        native_units = min(34, max(30, native_units, min_native_units))
    else:
        native_units = max(native_units, min_native_units)
    if not has_metadata_min_words:
        min_words = max(1300, min_words)
    if not has_metadata_word_target:
        words = min(1900, max(1800, words, min_words))
    else:
        words = max(words, min_words)
    landscape = _template_requests_landscape(template)
    return {
        "profile": "research_synthesis_dense",
        "target_panel_count": panel_count if has_metadata_panel_target else min(8, max(7, panel_count)),
        "min_panel_count": min_panel_count if has_metadata_min_panel else (6 if landscape else min(8, max(6, min_panel_count))),
        "target_native_information_unit_count": native_units,
        "target_native_information_units": native_units,
        "min_native_information_units": min_native_units,
        "target_visible_text_word_count": words,
        "target_visible_words": words,
        "min_visible_words": min_words,
        "target_nonwhite_pixel_ratio": targets.get("median_nonwhite_pixel_ratio"),
        "target_dark_ink_ratio": targets.get("median_dark_ink_ratio"),
        "target_edge_density": targets.get("median_edge_density"),
        "max_longest_blank_vertical_run_ratio": max(
            0.08,
            float(targets.get("median_longest_blank_vertical_run_ratio") or 0.0) * 8.0,
        ),
        "target_vertical_band_nonwhite_min": targets.get("median_vertical_band_nonwhite_min"),
        "target_grid_row_count": int(round(float(native.get("median_grid_row_count") or 4))),
        "target_section_heading_count": int(round(float(native.get("median_section_heading_count") or 12))),
        "required_units": required_units,
        "text_synthesis_targets": metadata.get("text_synthesis_targets") or {},
        "negative_guidance": metadata.get("negative_guidance") or [],
        "acceptance_focus": metadata.get("acceptance_focus") or [],
        "generation_prior": generation_prior,
    }


def _brief_with_native_reference_prior(
    brief: str,
    targets: dict[str, Any],
    *,
    template: str | None = None,
) -> str:
    dense_targets = _dense_synthesis_reference_targets(targets, template=template)
    if not dense_targets:
        return brief
    text_targets = (
        dense_targets.get("text_synthesis_targets")
        if isinstance(dense_targets.get("text_synthesis_targets"), dict)
        else {}
    )
    preferred_text_units = ", ".join(list(text_targets.get("preferred_text_units") or [])[:6])
    avoid_text_failures = ", ".join(list(text_targets.get("avoid_text_failures") or [])[:5])
    generation_prior = [
        _normalize_generation_prior_for_template(str(item), template)
        for item in list(dense_targets.get("generation_prior") or [])[:4]
    ]
    brief_min_native_units = max(18, int(dense_targets["min_native_information_units"]))
    prior = [
        "",
        "Native HTML dense-synthesis reference prior:",
        (
            f"- Target a {dense_targets['target_panel_count']}-panel synthesis board "
            f"with at least {brief_min_native_units} native information units "
            f"(target {dense_targets['target_native_information_unit_count']}) and "
            f"at least {dense_targets['min_visible_words']} visible words "
            f"(target about {dense_targets['target_visible_text_word_count']})."
        ),
        (
            f"- Match handmade gold density signals: non-white pixel ratio near "
            f"{dense_targets.get('target_nonwhite_pixel_ratio')}, no vertical blank band above "
            f"{dense_targets.get('max_longest_blank_vertical_run_ratio')}, and vertical band occupancy "
            f"near {dense_targets.get('target_vertical_band_nonwhite_min')} minimum."
        ),
        (
            f"- Match reference ink structure too: dark ink ratio near "
            f"{dense_targets.get('target_dark_ink_ratio')} and edge density near "
            f"{dense_targets.get('target_edge_density')}; pale boxes and hairline text do not satisfy density."
        ),
        "- Compile the reference logic into a hard skeleton: compact identity header, 6-8 numbered main panels, narrow gutters, colored section bars, high effective content fill, local figure/table plus explanation groups, native tables/cards/pipelines/formulas, limitations, and synthesis takeaways.",
        "- Build editable model cards, method pipelines, benchmark tables, formulas, source-grounded bullets, ablation or limitation notes, limitations/future panels, and synthesis takeaways. Keep provenance in metadata unless the user explicitly asks for visible citation/contact text.",
        "- Do not optimize for screenshot area. A low screenshot count is acceptable when the poster visibly saves human editorial labor through dense text synthesis, filled panels, native structure, and coherent section continuity.",
        "- Avoid long abstract dumps: split content into panel heads, short claims, bullets, callouts, tags, and native tables.",
    ]
    if text_targets:
        prior.extend([
            "- Text synthesis is a first-class target: dense but edited, paper-faithful, and coherent across panels.",
            "- Preserve correctness: source-backed model facts, benchmark values, limitations, and conclusions must not be invented.",
        ])
    if preferred_text_units:
        prior.append(f"- Preferred text units: {preferred_text_units}.")
    if avoid_text_failures:
        prior.append(f"- Avoid text failures: {avoid_text_failures}.")
    for item in generation_prior:
        prior.append(f"- Gold prior: {item}")
    return brief.rstrip() + "\n\n" + "\n".join(prior)


def _normalize_generation_prior_for_template(item: str, template: str | None) -> str:
    if not _template_requests_landscape(template):
        return item
    text = str(item or "")
    text = re.sub(
        r"\bBuild a portrait dense synthesis board\b",
        "Build a landscape 6-8 main-panel dense synthesis board",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\bUse a portrait dense board\b",
        "Use a landscape dense board",
        text,
        flags=re.IGNORECASE,
    )
    return text


def _template_requests_landscape(template: str | None) -> bool:
    raw = str(template or "").strip().lower()
    if not raw:
        return False
    if "portrait" in raw:
        return False
    return any(token in raw for token in ("landscape", "wide", "2x1", "16:9"))


def _landscape_dominates(targets: dict[str, Any]) -> bool:
    counts = targets.get("orientation_counts")
    if not isinstance(counts, dict):
        return False
    landscape = int(counts.get("landscape") or 0)
    portrait = int(counts.get("portrait") or 0)
    return landscape > portrait and landscape > 0


def _candidate_content_value_profile(candidate: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        return {}
    html = candidate.get("html") if isinstance(candidate.get("html"), dict) else {}
    paper_poster = candidate.get("paper_poster") if isinstance(candidate.get("paper_poster"), dict) else {}
    content_features = candidate.get("content_features") if isinstance(candidate.get("content_features"), dict) else {}
    layout_signature = candidate.get("layout_signature") if isinstance(candidate.get("layout_signature"), dict) else {}
    profile_name = _candidate_archetype_profile(candidate)

    words = _safe_float_any(
        html.get("authored_visible_text_word_count")
        if "authored_visible_text_word_count" in html else html.get("visible_text_word_count"),
        0.0,
    )
    max_words = _safe_float_any(html.get("max_text_layer_words"), 0.0)
    avg_words = _safe_float_any(html.get("avg_text_layer_words"), 0.0)
    section_labels = _safe_float_any(html.get("section_label_like_count"), 0.0)
    captions = _safe_float_any(html.get("caption_like_text_count"), 0.0)
    image_layers = _safe_float_any(html.get("image_layer_count"), 0.0)
    visual_area = (
        _safe_float_any(html.get("authored_visual_area_ratio"), 0.0)
        if "authored_visual_area_ratio" in html
        else max(
            _safe_float_any(html.get("visual_area_ratio"), 0.0),
            _safe_float_any(paper_poster.get("figure_area_ratio"), 0.0),
        )
    )
    role_hits = sorted(set(html.get("section_role_hits") or []) | set(content_features.get("section_role_hits") or []))
    role_coverage = max(
        _safe_float_any(html.get("section_role_coverage"), 0.0),
        _safe_float_any(content_features.get("section_role_coverage"), 0.0),
        len(role_hits) / 6.0 if role_hits else 0.0,
    )
    native_table_count = max(
        _safe_float_any(html.get("native_table_like_count"), 0.0),
        _safe_float_any(content_features.get("native_table_like_count"), 0.0),
    )
    native_chart_count = max(
        _safe_float_any(html.get("native_chart_like_count"), 0.0),
        _safe_float_any(content_features.get("native_chart_like_count"), 0.0),
    )
    formula_count = max(
        _safe_float_any(html.get("formula_like_text_count"), 0.0),
        _safe_float_any(content_features.get("formula_like_text_count"), 0.0),
    )
    model_card_count = max(
        _safe_float_any(html.get("model_card_like_count"), 0.0),
        _safe_float_any(content_features.get("model_card_like_count"), 0.0),
    )
    pipeline_count = max(
        _safe_float_any(html.get("pipeline_like_count"), 0.0),
        _safe_float_any(content_features.get("pipeline_like_count"), 0.0),
    )
    emphasis_count = max(
        _safe_float_any(html.get("emphasis_signal_count"), 0.0),
        _safe_float_any(content_features.get("emphasis_signal_count"), 0.0),
    )
    authored_native_metric_available = (
        "authored_native_information_unit_count" in html
        or "authored_native_information_unit_count" in content_features
    )
    if authored_native_metric_available:
        reported_native_units = max(
            _safe_float_any(html.get("authored_native_information_unit_count"), 0.0),
            _safe_float_any(content_features.get("authored_native_information_unit_count"), 0.0),
        )
    else:
        reported_native_units = max(
            _safe_float_any(html.get("native_information_unit_count"), 0.0),
            _safe_float_any(content_features.get("native_information_unit_count"), 0.0),
            _safe_float_any(html.get("dom_native_information_unit_count"), 0.0),
            _safe_float_any(content_features.get("dom_native_information_unit_count"), 0.0),
        )
    native_units = (
        reported_native_units
        if authored_native_metric_available
        else max(
            reported_native_units,
            native_table_count + native_chart_count + formula_count + model_card_count + pipeline_count,
        )
    )
    layout_value = content_features.get("layout_value_profile") if isinstance(content_features.get("layout_value_profile"), dict) else {}

    # Extremely long average text layers are raw paragraphs, not editorial labor.
    paragraph_penalty = min(0.28, max(0.0, (avg_words - 38.0) / 95.0) + max(0.0, (max_words - 110.0) / 360.0))
    role_score = min(1.0, role_coverage)
    section_score = min(1.0, section_labels / 6.0)
    emphasis_score = min(1.0, emphasis_count / 10.0)
    caption_score = min(1.0, captions / max(2.0, image_layers if image_layers else 2.0))
    mixed_panel_score = _safe_float_any(html.get("mixed_panel_binding_score"), 0.0)
    local_visual_text_score = max(caption_score, mixed_panel_score)
    image_panel_count = _safe_float_any(html.get("image_backed_panel_count"), 0.0)
    image_panel_low = _safe_float_any(html.get("image_backed_panel_text_low_count"), 0.0)
    underfilled_panels = _safe_float_any(html.get("underfilled_panel_count"), 0.0)
    title_mismatches = _safe_float_any(html.get("section_title_content_mismatch_count"), 0.0)
    repetition_debt = _safe_float_any(html.get("text_repetition_debt_score"), 0.0)
    dom_panels = max(
        1.0,
        _safe_float_any(html.get("dom_panel_count"), 0.0),
        _safe_float_any(layout_signature.get("slot_count"), 0.0),
    )
    image_panel_text_score = 1.0 if image_panel_count <= 0 else _clamp01(1.0 - image_panel_low / max(1.0, image_panel_count))
    panel_fill_score = _clamp01(1.0 - underfilled_panels / max(1.0, dom_panels))
    heading_alignment_score = _clamp01(1.0 - title_mismatches / max(1.0, dom_panels))
    panel_rule_score = _clamp01(
        0.42 * image_panel_text_score
        + 0.34 * panel_fill_score
        + 0.24 * heading_alignment_score
    )
    word_density_floor = _profile_text_density_floor(profile_name)
    word_density_high = 1050 if profile_name in {
        "research_synthesis_dense",
        "table_first_benchmark",
        "theory_text_board",
    } else 760
    word_density_score = _bounded_score(words, low=max(180.0, word_density_floor * 0.45), high=word_density_high)
    semantic = _clamp01(
        0.48 * word_density_score
        + 0.18 * role_score
        + 0.11 * section_score
        + 0.08 * emphasis_score
        + 0.07 * local_visual_text_score
        + 0.08 * panel_rule_score
        - paragraph_penalty
        - min(0.24, 0.24 * repetition_debt)
    )
    information_architecture = _clamp01(
        0.28 * role_score
        + 0.17 * section_score
        + 0.16 * min(1.0, _safe_float_any(layout_signature.get("slot_count"), 0.0) / 7.0)
        + 0.10 * local_visual_text_score
        + 0.16 * panel_fill_score
        + 0.07 * image_panel_text_score
        + 0.04 * min(1.0, emphasis_count / 8.0)
        + 0.02 * min(1.0, words / max(1.0, word_density_floor))
        - min(0.18, paragraph_penalty * 0.7)
        - min(0.18, 0.18 * repetition_debt)
    )
    native_reconstruction = _clamp01(
        0.26 * min(1.0, native_table_count / 2.0)
        + 0.16 * min(1.0, native_chart_count / 2.0)
        + 0.20 * min(1.0, formula_count / 2.0)
        + 0.20 * min(1.0, model_card_count / 2.0)
        + 0.15 * min(1.0, pipeline_count / 2.0)
        + 0.03 * min(1.0, native_units / 5.0)
    )
    if profile_name in {"theory_text_board", "research_synthesis_dense"}:
        native_reconstruction = _clamp01(native_reconstruction + 0.12 * role_score + 0.08 * section_score)
    elif profile_name == "gui_video_benchmark":
        native_reconstruction = _clamp01(native_reconstruction + 0.08 * caption_score)

    source_backed = max(
        _safe_float_any(paper_poster.get("source_backed_dom_image_count"), 0.0),
        _safe_float_any(paper_poster.get("selected_source_asset_dom_placed_count"), 0.0),
        _safe_float_any(paper_poster.get("selected_source_asset_count"), 0.0),
    )
    selected_source_total = max(
        1.0,
        _safe_float_any(paper_poster.get("selected_source_asset_count"), 0.0),
        source_backed,
    )
    text_density_quality = _clamp01(
        0.52 * _bounded_score(words, low=max(220.0, word_density_floor * 0.55), high=word_density_high)
        + 0.18 * panel_fill_score
        + 0.14 * image_panel_text_score
        + 0.08 * role_score
        + 0.05 * section_score
        + 0.03 * emphasis_score
        - min(0.26, paragraph_penalty)
        - min(0.22, 0.22 * repetition_debt)
    )
    paper_faithfulness_proxy = _clamp01(
        0.30 * min(1.0, source_backed / selected_source_total)
        + 0.20 * local_visual_text_score
        + 0.16 * min(1.0, native_table_count / 1.0)
        + 0.06 * min(1.0, native_chart_count / 1.0)
        + 0.14 * role_score
        + 0.10 * min(1.0, native_units / 5.0)
        + 0.04 * heading_alignment_score
        - min(0.16, 0.16 * repetition_debt)
    )
    narrative_coherence_proxy = _clamp01(
        0.40 * role_score
        + 0.24 * section_score
        + 0.16 * min(1.0, _safe_float_any(layout_signature.get("slot_count"), 0.0) / 9.0)
        + 0.10 * local_visual_text_score
        + 0.08 * emphasis_score
        + 0.02 * heading_alignment_score
        - min(0.18, paragraph_penalty * 0.8)
        - min(0.20, 0.20 * repetition_debt)
    )
    source_visual_use = _clamp01(
        0.42 * min(1.0, source_backed / selected_source_total)
        + 0.32 * local_visual_text_score
        + 0.06 * min(1.0, visual_area / 0.28)
        + 0.12 * min(1.0, image_layers / 3.0)
    )
    anti_abstract_dump = _clamp01(1.0 - min(0.70, paragraph_penalty * 2.2))

    mechanical_discount = 0.0
    if image_layers >= 6 and visual_area >= 0.40:
        weak_editorial = max(0.0, 0.58 - semantic) + max(0.0, 0.42 - native_reconstruction)
        mechanical_discount = min(0.24, weak_editorial * 0.45)
    if profile_name in {"theory_text_board", "research_synthesis_dense", "table_first_benchmark"}:
        mechanical_discount *= 0.55

    value_hint = 0.0
    if layout_value:
        value_hint = (
            _safe_float_any(layout_value.get("editorial_synthesis_value"), 0.0)
            + _safe_float_any(layout_value.get("native_reconstruction_value"), 0.0)
        ) / 20.0
    overall = _clamp01(
        0.32 * semantic
        + 0.20 * information_architecture
        + 0.16 * native_reconstruction
        + 0.20 * text_density_quality
        + 0.04 * mixed_panel_score
        + 0.08 * panel_rule_score
        + min(0.06, value_hint)
        - mechanical_discount
    )
    minutes = round(
        8.0
        + 42.0 * semantic
        + 34.0 * information_architecture
        + 44.0 * native_reconstruction
        - 18.0 * mechanical_discount,
        1,
    )
    target = ARCHETYPE_TARGET_PROFILES.get(profile_name, ARCHETYPE_TARGET_PROFILES["default"])
    return {
        "profile": profile_name,
        "archetype": str(layout_signature.get("archetype") or content_features.get("archetype") or ""),
        "section_role_hits": role_hits,
        "section_role_coverage": round(role_coverage, 3),
        "native_table_like_count": int(native_table_count),
        "native_chart_like_count": int(native_chart_count),
        "formula_like_text_count": int(formula_count),
        "model_card_like_count": int(model_card_count),
        "pipeline_like_count": int(pipeline_count),
        "native_information_unit_count": int(native_units),
        "authored_native_information_unit_count": int(reported_native_units),
        "authored_visible_text_word_count": int(words),
        "emphasis_signal_count": int(emphasis_count),
        "visual_area_ratio": round(visual_area, 4),
        "image_layer_count": int(image_layers),
        "source_backed_dom_image_count": paper_poster.get("source_backed_dom_image_count"),
        "manual_work_proxy": {
            "semantic_synthesis_score": round(semantic, 3),
            "information_architecture_score": round(information_architecture, 3),
            "native_reconstruction_score": round(native_reconstruction, 3),
            "text_density_quality": round(text_density_quality, 3),
            "paper_faithfulness_proxy": round(paper_faithfulness_proxy, 3),
            "narrative_coherence_proxy": round(narrative_coherence_proxy, 3),
            "native_reconstruction_value": round(native_reconstruction, 3),
            "source_visual_use": round(source_visual_use, 3),
            "mixed_panel_binding_score": round(mixed_panel_score, 3),
            "panel_rule_score": round(panel_rule_score, 3),
            "image_panel_text_score": round(image_panel_text_score, 3),
            "panel_fill_score": round(panel_fill_score, 3),
            "section_title_alignment_score": round(heading_alignment_score, 3),
            "anti_abstract_dump": round(anti_abstract_dump, 3),
            "mechanical_screenshot_discount": round(mechanical_discount, 3),
            "estimated_human_minutes_saved": minutes,
            "overall": round(overall, 3),
        },
        "targets": {
            "visual_area_min": target.get("visual_area_min"),
            "human_effort_min": target.get("human_effort_min"),
            "text_density_floor": word_density_floor,
        },
    }


def _candidate_archetype_profile(candidate: dict[str, Any]) -> str:
    signature = candidate.get("layout_signature") if isinstance(candidate.get("layout_signature"), dict) else {}
    features = candidate.get("content_features") if isinstance(candidate.get("content_features"), dict) else {}
    html = candidate.get("html") if isinstance(candidate.get("html"), dict) else {}
    raw = " ".join(
        str(item or "")
        for item in (
            signature.get("archetype"),
            features.get("archetype"),
            " ".join(signature.get("slot_roles") or []),
            " ".join(html.get("section_role_hits") or []),
            " ".join(features.get("section_role_hits") or []),
        )
    ).lower()
    native_table_count = max(
        _safe_float_any(html.get("native_table_like_count"), 0.0),
        _safe_float_any(features.get("native_table_like_count"), 0.0),
    )
    if any(token in raw for token in ("gui", "screenshot", "interaction", "videogui")):
        return "gui_video_benchmark"
    if any(token in raw for token in ("world_model", "world model", "video_prediction", "filmstrip", "sequence")):
        return "world_model_filmstrip"
    if any(token in raw for token in ("research_synthesis_dense", "dense_synthesis", "synthesis_dense", "dense_board")):
        return "research_synthesis_dense"
    if any(token in raw for token in ("multi_view", "multi-view", "matrix", "graph", "clustering")):
        return "multi_view_matrix_graph"
    if native_table_count >= 1 and any(token in raw for token in ("result", "evidence", "benchmark", "wide_banded")):
        return "table_first_benchmark"
    if any(token in raw for token in ("table_first", "leaderboard", "benchmark_table", "table", "ablation")):
        return "table_first_benchmark"
    if any(token in raw for token in ("theory", "theorem", "proof", "halfspace", "bound")):
        return "theory_text_board"
    if any(token in raw for token in ("synthesis", "model_card", "dense")):
        return "research_synthesis_dense"
    return "default"


def _safe_float_any(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _dense_synthesis_reference_penalty(
    *,
    reference_html: dict[str, Any],
    candidate: dict[str, Any],
    content_profile: dict[str, Any],
    html: dict[str, Any],
    issue: Any,
) -> float:
    if not reference_html:
        return 0.0
    penalty = 0.0
    ref_units = int(_safe_float_any(reference_html.get("native_information_unit_count"), 0.0))
    ref_panels = int(_safe_float_any(reference_html.get("panel_count"), 0.0))
    ref_words = int(_safe_float_any(reference_html.get("visible_text_word_count"), 0.0))
    ref_headings = int(_safe_float_any(reference_html.get("section_heading_count"), 0.0))
    ref_tables = int(_safe_float_any(reference_html.get("table_count"), 0.0))
    ref_flow = int(_safe_float_any(reference_html.get("flow_box_count"), 0.0))
    ref_model = int(_safe_float_any(reference_html.get("model_card_like_count"), 0.0))

    candidate_units = _candidate_native_information_units(content_profile, html)
    candidate_words = int(_safe_float_any(
        html.get("authored_visible_text_word_count")
        if "authored_visible_text_word_count" in html else html.get("visible_text_word_count"),
        0.0,
    ))
    candidate_headings = int(_safe_float_any(html.get("section_label_like_count"), 0.0))
    candidate_tables = int(_safe_float_any(content_profile.get("native_table_like_count"), 0.0))
    candidate_flow_model = int(_safe_float_any(content_profile.get("pipeline_like_count"), 0.0)) + int(_safe_float_any(content_profile.get("model_card_like_count"), 0.0))
    signature = candidate.get("layout_signature") if isinstance(candidate.get("layout_signature"), dict) else {}
    candidate_panels = int(_safe_float_any(signature.get("panel_count") or signature.get("slot_count"), 0.0))

    unit_target = max(8, min(ref_units, int(round(ref_units * 0.45)))) if ref_units else 8
    panel_target = max(6, min(ref_panels, int(round(ref_panels * 0.65)))) if ref_panels else 6
    word_target = max(420, min(ref_words, int(round(ref_words * 0.40)))) if ref_words else 420
    heading_target = max(6, min(ref_headings, int(round(ref_headings * 0.45)))) if ref_headings else 6

    if candidate_units < unit_target:
        penalty += 0.14
        issue(
            "candidate_native_information_units_low",
            "content_strategy",
            "high",
            (
                "dense HTML reference expects editable native information design: "
                f"{candidate_units} native units < target {unit_target}"
            ),
        )
    if candidate_panels and candidate_panels < panel_target:
        penalty += 0.08
        issue(
            "candidate_dense_synthesis_panel_count_low",
            "layout_storyboard",
            "medium",
            f"dense synthesis reference uses a multi-panel board; candidate panels {candidate_panels} < target {panel_target}",
        )
    if candidate_words < word_target:
        penalty += 0.08
        issue(
            "candidate_dense_synthesis_word_density_low",
            "content_strategy",
            "medium",
            f"visible text density is low for dense synthesis: {candidate_words} words < target {word_target}",
        )
    if candidate_headings < heading_target:
        penalty += 0.06
        issue(
            "candidate_dense_synthesis_section_hierarchy_low",
            "typography_system",
            "medium",
            f"section hierarchy is thin for dense synthesis: {candidate_headings} headings < target {heading_target}",
        )
    if ref_tables >= 1 and candidate_tables < 1:
        penalty += 0.10
        issue(
            "candidate_dense_synthesis_table_missing",
            "content_strategy",
            "high",
            "dense HTML reference uses native benchmark/result tables; candidate lacks a native table-like structure",
        )
    if (ref_flow >= 4 or ref_model >= 1) and candidate_flow_model < 1:
        penalty += 0.10
        issue(
            "candidate_dense_synthesis_pipeline_or_model_card_missing",
            "content_strategy",
            "high",
            "dense HTML reference uses method pipelines/model cards; candidate lacks equivalent native structure",
        )
    return min(0.28, penalty)


def _metadata_reference_penalty(
    *,
    reference_metadata: dict[str, Any],
    candidate: dict[str, Any],
    content_profile: dict[str, Any],
    html: dict[str, Any],
    issue: Any,
) -> float:
    if not isinstance(reference_metadata, dict) or not reference_metadata:
        return 0.0
    profile = str(reference_metadata.get("reference_profile") or reference_metadata.get("profile") or "")
    if profile != "research_synthesis_dense":
        return 0.0
    hint = reference_metadata.get("reference_metrics_hint") if isinstance(reference_metadata.get("reference_metrics_hint"), dict) else {}
    text_targets = reference_metadata.get("text_synthesis_targets") if isinstance(reference_metadata.get("text_synthesis_targets"), dict) else {}
    manual_work = content_profile.get("manual_work_proxy") if isinstance(content_profile.get("manual_work_proxy"), dict) else {}
    signature = candidate.get("layout_signature") if isinstance(candidate.get("layout_signature"), dict) else {}

    candidate_words = int(_safe_float_any(
        html.get("authored_visible_text_word_count")
        if "authored_visible_text_word_count" in html else html.get("visible_text_word_count"),
        0.0,
    ))
    candidate_panels = int(_safe_float_any(signature.get("panel_count") or signature.get("slot_count"), 0.0))
    candidate_units = _candidate_native_information_units(content_profile, html)
    candidate_tables = int(_safe_float_any(content_profile.get("native_table_like_count"), 0.0))
    candidate_headings = int(_safe_float_any(html.get("section_label_like_count"), 0.0))
    max_words = int(_safe_float_any(html.get("max_text_layer_words"), 0.0))

    min_words = int(_safe_float_any(hint.get("min_visible_words"), 850.0))
    min_panels = int(_safe_float_any(hint.get("min_panel_count"), 6.0))
    min_units = int(_safe_float_any(hint.get("min_native_information_units"), 9.0))
    target_tables = int(_safe_float_any(hint.get("target_table_count"), 1.0))
    target_headings = int(_safe_float_any(hint.get("target_section_heading_count"), 18.0))

    penalty = 0.0
    if candidate_words < min_words:
        penalty += 0.10
        issue(
            "candidate_gold_v1_text_density_low",
            "content_strategy",
            "high",
            f"gold v1 expects dense edited text: {candidate_words} words < minimum {min_words}",
        )
    if candidate_panels and candidate_panels < min_panels:
        penalty += 0.08
        issue(
            "candidate_gold_v1_panel_count_low",
            "layout_storyboard",
            "high",
            f"gold v1 expects a multi-panel synthesis board: {candidate_panels} panels < minimum {min_panels}",
        )
    if candidate_units < min_units:
        penalty += 0.10
        issue(
            "candidate_gold_v1_native_reconstruction_low",
            "content_strategy",
            "high",
            f"gold v1 expects native reconstruction value: {candidate_units} units < minimum {min_units}",
        )
    if target_tables >= 1 and candidate_tables < 1:
        penalty += 0.08
        issue(
            "candidate_gold_v1_native_table_missing",
            "content_strategy",
            "high",
            "gold v1 references use native benchmark/result tables; candidate lacks one",
        )
    if candidate_headings < max(8, int(round(target_headings * 0.35))):
        penalty += 0.05
        issue(
            "candidate_gold_v1_section_hierarchy_low",
            "typography_system",
            "medium",
            f"gold v1 expects strong section hierarchy: {candidate_headings} section labels/headings",
        )

    score_thresholds = {
        "text_density_quality": ("candidate_gold_v1_text_density_quality_low", 0.52, "dense edited text quality is low"),
        "paper_faithfulness_proxy": ("candidate_gold_v1_paper_faithfulness_proxy_low", 0.45, "source-backed paper-faithfulness proxy is low"),
        "narrative_coherence_proxy": ("candidate_gold_v1_narrative_coherence_low", 0.52, "panel narrative coherence proxy is low"),
        "source_visual_use": ("candidate_gold_v1_source_visual_use_low", 0.34, "source visual/table use is weak for a paper poster"),
    }
    for metric, (issue_id, threshold, message) in score_thresholds.items():
        score = float(manual_work.get(metric) or 0.0)
        if score < threshold:
            penalty += 0.05
            issue(issue_id, "content_strategy", "medium", f"{message}: {score:.2f} < {threshold:.2f}")

    anti_dump = float(manual_work.get("anti_abstract_dump") or 0.0)
    if anti_dump < 0.66 or max_words > 140:
        penalty += 0.08
        avoid = ", ".join(list(text_targets.get("avoid_text_failures") or [])[:3])
        suffix = f"; avoid: {avoid}" if avoid else ""
        issue(
            "candidate_gold_v1_abstract_dump_risk",
            "content_strategy",
            "high",
            f"candidate risks abstract-dump prose or overlong text layers (max_words={max_words}){suffix}",
        )

    missing_units = _candidate_missing_metadata_required_units(reference_metadata, content_profile)
    if missing_units:
        penalty += min(0.12, 0.04 * len(missing_units))
        issue(
            "candidate_gold_v1_required_units_missing",
            "content_strategy",
            "high",
            "gold v1 required synthesis units are missing: " + ", ".join(missing_units[:5]),
        )
    return min(0.36, penalty)


def _candidate_missing_metadata_required_units(
    reference_metadata: dict[str, Any],
    content_profile: dict[str, Any],
) -> list[str]:
    required = [
        str(item)
        for item in (reference_metadata.get("required_units") or [])
        if str(item or "").strip()
    ]
    if not required:
        return []
    role_hits = {str(item) for item in (content_profile.get("section_role_hits") or [])}
    tables = int(_safe_float_any(content_profile.get("native_table_like_count"), 0.0))
    formulas = int(_safe_float_any(content_profile.get("formula_like_text_count"), 0.0))
    model_cards = int(_safe_float_any(content_profile.get("model_card_like_count"), 0.0))
    pipelines = int(_safe_float_any(content_profile.get("pipeline_like_count"), 0.0))
    missing: list[str] = []
    for unit in required:
        normalized = unit.lower()
        present = True
        if "identity_header" in normalized or "title_header" in normalized:
            present = True
        elif "headline_metric" in normalized or "metric_strip" in normalized:
            present = tables >= 1 or "results" in role_hits
        elif "motivation_problem" in normalized or ("motivation" in normalized and "problem" in normalized):
            present = "motivation" in role_hits
        elif "principal_contribution" in normalized or "principal_contributions" in normalized:
            present = "contribution" in role_hits
        elif "understanding_generation_unification" in normalized:
            present = bool({"contribution", "method", "analysis"} & role_hits) or model_cards >= 1 or pipelines >= 1
        elif "architecture_redraw" in normalized or ("architecture" in normalized and "redraw" in normalized):
            present = model_cards >= 1 or pipelines >= 1 or "method" in role_hits
        elif "scale_curve" in normalized or "compute_curve" in normalized:
            present = tables >= 1 or "results" in role_hits
        elif "data_requirements" in normalized or "data_requirement" in normalized:
            present = model_cards >= 1 or pipelines >= 1 or "method" in role_hits
        elif "motivation_panel" in normalized:
            present = "motivation" in role_hits
        elif "finding_card" in normalized or "finding_cards" in normalized:
            present = bool({"contribution", "results", "analysis"} & role_hits) or model_cards >= 1
        elif "core_idea" in normalized or "formula_panel" in normalized:
            present = formulas >= 1 or pipelines >= 1 or "method" in role_hits
        elif "model_card" in normalized:
            present = model_cards >= 1 or "method" in role_hits
        elif "pipeline" in normalized:
            present = pipelines >= 1 or "method" in role_hits
        elif "table" in normalized or "results" in normalized or "benchmark" in normalized:
            present = tables >= 1 or "results" in role_hits
        elif "ablation" in normalized or "analysis" in normalized:
            present = "analysis" in role_hits or "results" in role_hits
        elif "limitation" in normalized or "future" in normalized:
            present = "analysis" in role_hits or "conclusion" in role_hits
        elif "footer" in normalized or "provenance" in normalized:
            present = "conclusion" in role_hits
        else:
            token = normalized.replace("_", " ")
            present = any(token in hit.replace("_", " ") for hit in role_hits)
        if not present:
            missing.append(unit)
    return missing


def _bounded_score(value: float, *, low: float, high: float) -> float:
    if high <= low:
        return 0.0
    return _clamp01((value - low) / (high - low))


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _profile_text_density_floor(profile_name: str) -> int:
    if profile_name in {"research_synthesis_dense", "theory_text_board"}:
        return 800
    if profile_name == "table_first_benchmark":
        return 650
    if profile_name in {"visual_evidence_wall", "multi_view_matrix_graph"}:
        return 600
    return 620


def _load_dense_synthesis_targets_for_eval() -> dict[str, Any]:
    global _DENSE_SYNTHESIS_TARGETS_CACHE
    if _DENSE_SYNTHESIS_TARGETS_CACHE is not None:
        return _DENSE_SYNTHESIS_TARGETS_CACHE
    try:
        payload = json.loads(DENSE_SYNTHESIS_TARGETS_PATH.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    _DENSE_SYNTHESIS_TARGETS_CACHE = payload if isinstance(payload, dict) else {}
    return _DENSE_SYNTHESIS_TARGETS_CACHE


def _dense_gold_reference_for_case(
    case_slug: str | None,
    *,
    reference: dict[str, Any] | None = None,
    reference_html: dict[str, Any] | None = None,
    reference_metadata: dict[str, Any] | None,
    reference_profile: str,
) -> dict[str, Any]:
    targets = _load_dense_synthesis_targets_for_eval()
    references = targets.get("gold_regression_references")
    if not isinstance(references, dict):
        references = {}
    handmade_references = targets.get("handmade_gold_references")
    if not isinstance(handmade_references, dict):
        handmade_references = {}
    slug = str(case_slug or "").strip()
    metadata_slug = str((reference_metadata or {}).get("case_id") or (reference_metadata or {}).get("slug") or "").strip()
    lookup_slugs = [value for value in (slug, metadata_slug) if value]
    regression_anchor: dict[str, Any] = {}
    for lookup_slug in lookup_slugs:
        if isinstance(references.get(lookup_slug), dict):
            regression_anchor = dict(references[lookup_slug])
            break
    handmade: dict[str, Any] = {}
    for lookup_slug in lookup_slugs:
        if isinstance(handmade_references.get(lookup_slug), dict):
            handmade = dict(handmade_references[lookup_slug])
            break
    out: dict[str, Any] = {}
    if handmade:
        out = dict(handmade)
        out.setdefault("visual_density_floor", handmade.get("quality_floor") or {})
        out["handmade_gold_reference_id"] = handmade.get("id")
        out["quality_floor"] = handmade.get("quality_floor") or out.get("quality_floor") or {}
        out["gold_composite_baseline"] = handmade.get("gold_composite_baseline") or out.get("gold_composite_baseline") or {}
        if regression_anchor:
            out["secondary_visual_anchor_reference_id"] = regression_anchor.get("id")
            out["secondary_visual_anchor_role"] = regression_anchor.get("role") or "dense_skeleton_visual_anchor"
            out["secondary_visual_anchor_known_limitations"] = (
                regression_anchor.get("known_limitations")
                or ((regression_anchor.get("dense_skeleton") or {}).get("known_limitations") if isinstance(regression_anchor.get("dense_skeleton"), dict) else None)
                or []
            )
            if regression_anchor.get("dense_skeleton"):
                out["dense_skeleton"] = regression_anchor.get("dense_skeleton")
        elif handmade.get("dense_skeleton") and not out.get("dense_skeleton"):
            out["dense_skeleton"] = handmade.get("dense_skeleton")
    if out:
        derived = _reference_derived_gold_reference(
            slug or metadata_slug,
            reference=reference,
            reference_html=reference_html,
            reference_metadata=reference_metadata,
            reference_profile=reference_profile,
        )
        for section in ("visual_density_floor", "quality_floor"):
            derived_section = derived.get(section) if isinstance(derived.get(section), dict) else {}
            if not derived_section:
                continue
            out_section = out.setdefault(section, {})
            if isinstance(out_section, dict):
                for key in ("min_dark_ink_ratio", "min_edge_density"):
                    if key in derived_section and key not in out_section:
                        out_section[key] = derived_section[key]
        return out
    derived = _reference_derived_gold_reference(
        slug or metadata_slug,
        reference=reference,
        reference_html=reference_html,
        reference_metadata=reference_metadata,
        reference_profile=reference_profile,
    )
    if derived:
        return derived
    if regression_anchor:
        return dict(regression_anchor)
    profile_ref = references.get(f"profile:{reference_profile}")
    if reference_profile and isinstance(profile_ref, dict):
        return dict(profile_ref)
    return {}


def _reference_derived_gold_reference(
    case_slug: str,
    *,
    reference: dict[str, Any] | None,
    reference_html: dict[str, Any] | None,
    reference_metadata: dict[str, Any] | None,
    reference_profile: str,
) -> dict[str, Any]:
    if reference_profile != "research_synthesis_dense" and str((reference_html or {}).get("reference_profile") or "") != "research_synthesis_dense":
        return {}
    if not isinstance(reference, dict) or not isinstance(reference_html, dict):
        return {}
    ref_nonwhite = _safe_float_any(reference.get("nonwhite_pixel_ratio"), 0.0)
    ref_blank = _safe_float_any(reference.get("longest_blank_vertical_run_ratio"), 0.0)
    ref_dark_ink = _safe_float_any(reference.get("dark_ink_ratio"), 0.0)
    ref_edge_density = _safe_float_any(reference.get("edge_density"), 0.0)
    ref_bands = [
        _safe_float_any(value, 0.0)
        for value in (reference.get("vertical_band_nonwhite_ratios") or [])
        if isinstance(value, (int, float))
    ]
    ref_words = int(_safe_float_any(reference_html.get("visible_text_word_count"), 0.0))
    ref_units = int(_safe_float_any(reference_html.get("native_information_unit_count"), 0.0))
    hint = reference_metadata.get("reference_metrics_hint") if isinstance((reference_metadata or {}).get("reference_metrics_hint"), dict) else {}
    min_words = max(
        int(_safe_float_any(hint.get("min_visible_words"), 0.0)),
        int(round(ref_words * 0.62)),
    )
    min_units = max(
        int(_safe_float_any(hint.get("min_native_information_units"), 0.0)),
        int(round(ref_units * 0.55)),
    )
    band_floors = [
        round(min(0.22, max(0.08, value * 0.42)), 4)
        for value in ref_bands[:10]
    ]
    return {
        "id": f"derived_human_effort_gold_{case_slug or 'dense'}",
        "case_slug": case_slug,
        "quality_floor": {
            "min_nonwhite_pixel_ratio": round(max(0.18, ref_nonwhite * 0.55), 4),
            "min_dark_ink_ratio": round(max(0.035, ref_dark_ink * 0.62), 4),
            "min_edge_density": round(max(0.18, ref_edge_density * 0.78), 4),
            "max_longest_blank_vertical_run_ratio": max(0.08, round(ref_blank * 8.0, 4)),
            "vertical_band_count": 10,
            "min_vertical_band_nonwhite_ratios": band_floors,
            "max_vertical_band_floor_failures": 1,
            "min_leaf_visible_words": min_words,
            "min_source_figure_area_ratio": 0.08,
            "min_native_information_units": min_units,
        },
        "visual_density_floor": {
            "min_nonwhite_pixel_ratio": round(max(0.16, ref_nonwhite * 0.45), 4),
            "min_dark_ink_ratio": round(max(0.03, ref_dark_ink * 0.52), 4),
            "min_edge_density": round(max(0.17, ref_edge_density * 0.72), 4),
            "max_longest_blank_vertical_run_ratio": max(0.09, round(ref_blank * 10.0, 4)),
            "vertical_band_count": 10,
            "min_vertical_band_nonwhite_ratios": band_floors,
            "max_vertical_band_floor_failures": 1,
            "min_leaf_visible_words": max(850, int(round(ref_words * 0.55))),
            "min_source_figure_area_ratio": 0.08,
            "min_native_information_units": max(8, int(round(ref_units * 0.45))),
        },
        "gold_composite_baseline": {
            "min_composite_score": 0.98,
            "nonwhite_pixel_ratio": ref_nonwhite,
            "dark_ink_ratio": ref_dark_ink,
            "edge_density": ref_edge_density,
            "longest_blank_vertical_run_ratio": ref_blank,
            "vertical_band_nonwhite_min": _safe_float_any(reference.get("vertical_band_nonwhite_min"), min(ref_bands or [0.0])),
            "leaf_visible_words": ref_words,
            "source_figure_area_ratio": 0.08,
            "native_information_units": ref_units,
            "panel_count": int(_safe_float_any(reference_html.get("panel_count"), 0.0)),
            "section_heading_count": int(_safe_float_any(reference_html.get("section_heading_count"), 0.0)),
        },
    }


def _gold_thresholds(gold_reference: dict[str, Any]) -> dict[str, Any]:
    thresholds = gold_reference.get("visual_density_floor")
    return thresholds if isinstance(thresholds, dict) else {}


def _gold_quality_thresholds(gold_reference: dict[str, Any]) -> dict[str, Any]:
    thresholds = gold_reference.get("quality_floor")
    return thresholds if isinstance(thresholds, dict) else {}


def _candidate_leaf_visible_words(html: dict[str, Any]) -> int:
    if "authored_leaf_visible_word_count" in html:
        return max(0, int(_safe_float_any(html.get("authored_leaf_visible_word_count"), 0.0)))
    leaf_words = int(_safe_float_any(html.get("leaf_visible_word_count"), -1.0))
    if leaf_words < 0:
        leaf_words = int(_safe_float_any(html.get("leaf_visible_text_word_count"), -1.0))
    if leaf_words < 0:
        leaf_words = int(_safe_float_any(html.get("visible_text_word_count"), 0.0))
    return max(0, leaf_words)


def _candidate_native_information_units(content_profile: dict[str, Any], html: dict[str, Any]) -> int:
    if "authored_native_information_unit_count" in html or "authored_native_information_unit_count" in content_profile:
        return max(
            int(_safe_float_any(content_profile.get("authored_native_information_unit_count"), 0.0)),
            int(_safe_float_any(html.get("authored_native_information_unit_count"), 0.0)),
        )
    return max(
        int(_safe_float_any(content_profile.get("native_information_unit_count"), 0.0)),
        int(_safe_float_any(html.get("native_information_unit_count"), 0.0)),
    )


def _candidate_source_figure_area_ratio(paper_poster: dict[str, Any], html: dict[str, Any]) -> float:
    paper_source_area = _safe_float_any(paper_poster.get("figure_area_ratio"), 0.0)
    if paper_source_area > 0:
        return paper_source_area
    if "authored_visual_area_ratio" in html:
        return _safe_float_any(html.get("authored_visual_area_ratio"), 0.0)
    return _safe_float_any(html.get("visual_area_ratio"), 0.0)


def _candidate_contract_repair_debt(html: dict[str, Any]) -> dict[str, Any]:
    known = any(
        key in html
        for key in (
            "contract_autofill_block_count",
            "contract_autofill_word_count",
            "contract_autofill_native_unit_count",
            "repair_generated_block_count",
            "auto_source_block_count",
        )
    )
    raw_words = max(1, int(_safe_float_any(html.get("visible_text_word_count"), 0.0)))
    autofill_words = int(_safe_float_any(html.get("contract_autofill_word_count"), 0.0))
    return {
        "known": known,
        "contract_autofill_block_count": int(_safe_float_any(html.get("contract_autofill_block_count"), 0.0)),
        "contract_autofill_word_count": autofill_words,
        "contract_autofill_word_share": round(autofill_words / raw_words, 4),
        "contract_autofill_native_unit_count": int(_safe_float_any(html.get("contract_autofill_native_unit_count"), 0.0)),
        "repair_generated_block_count": int(_safe_float_any(html.get("repair_generated_block_count"), 0.0)),
        "auto_source_block_count": int(_safe_float_any(html.get("auto_source_block_count"), 0.0)),
    }


def _dense_gold_visual_density_status(
    *,
    gold_reference: dict[str, Any],
    image: dict[str, Any],
    html: dict[str, Any],
    paper_poster: dict[str, Any],
    content_profile: dict[str, Any],
) -> dict[str, Any]:
    thresholds = _gold_thresholds(gold_reference)
    if not thresholds:
        return {}
    nonwhite = _safe_float_any(
        image.get("nonwhite_pixel_ratio"),
        max(0.0, 1.0 - _safe_float_any(image.get("white_space_ratio"), 1.0)),
    )
    dark_ink = _safe_float_any(image.get("dark_ink_ratio"), 0.0)
    longest_blank = _safe_float_any(image.get("longest_blank_vertical_run_ratio"), 0.0)
    edge_density = _safe_float_any(image.get("edge_density"), 0.0)
    band_values = [
        _safe_float_any(value, 0.0)
        for value in (image.get("vertical_band_nonwhite_ratios") or [])
        if isinstance(value, (int, float))
    ]
    band_floors = [
        _safe_float_any(value, 0.0)
        for value in (thresholds.get("min_vertical_band_nonwhite_ratios") or [])
        if isinstance(value, (int, float))
    ]
    leaf_words = _candidate_leaf_visible_words(html)
    source_figure_area = _candidate_source_figure_area_ratio(paper_poster, html)
    native_units = _candidate_native_information_units(content_profile, html)
    repair_debt = _candidate_contract_repair_debt(html)
    panel_internal_p0 = int(_safe_float_any(paper_poster.get("panel_internal_underfilled_p0_count"), 0.0))
    panel_word_budget_fail = int(_safe_float_any(paper_poster.get("panel_internal_word_budget_fail_count"), 0.0))
    panel_native_unit_fail = int(_safe_float_any(paper_poster.get("panel_internal_native_unit_fail_count"), 0.0))
    panel_visual_p0 = int(_safe_float_any(paper_poster.get("panel_visual_underfilled_p0_count"), 0.0))
    panel_visual_min_ink = _safe_float_any(paper_poster.get("panel_visual_min_ink_ratio"), 1.0)
    panel_visual_min_grid = _safe_float_any(paper_poster.get("panel_visual_min_grid_coverage"), 1.0)

    failures: list[dict[str, Any]] = []
    min_nonwhite = _safe_float_any(thresholds.get("min_nonwhite_pixel_ratio"), 0.0)
    if min_nonwhite and nonwhite < min_nonwhite:
        failures.append({
            "id": "candidate_gold_visual_density_nonwhite_low",
            "owner": "layout_storyboard",
            "severity": "blocker",
            "message": (
                f"gold regression density failed: non-white pixel ratio {nonwhite:.3f} "
                f"< floor {min_nonwhite:.3f}; fewer DOM P0s cannot compensate for a sparse poster"
            ),
        })
    min_dark_ink = _safe_float_any(thresholds.get("min_dark_ink_ratio"), 0.0)
    if min_dark_ink and dark_ink < min_dark_ink:
        failures.append({
            "id": "candidate_gold_visual_density_dark_ink_low",
            "owner": "layout_storyboard",
            "severity": "blocker",
            "message": (
                f"gold regression density failed: dark ink ratio {dark_ink:.3f} "
                f"< floor {min_dark_ink:.3f}; pale panel backgrounds do not count as dense information"
            ),
        })
    min_edge_density = _safe_float_any(thresholds.get("min_edge_density"), 0.0)
    if min_edge_density and edge_density < min_edge_density:
        failures.append({
            "id": "candidate_gold_visual_density_edge_low",
            "owner": "layout_storyboard",
            "severity": "blocker",
            "message": (
                f"gold regression density failed: edge density {edge_density:.3f} "
                f"< floor {min_edge_density:.3f}; the poster lacks reference-like text/figure microstructure"
            ),
        })
    max_blank = _safe_float_any(thresholds.get("max_longest_blank_vertical_run_ratio"), 0.0)
    if max_blank and longest_blank > max_blank:
        failures.append({
            "id": "candidate_gold_visual_density_blank_band",
            "owner": "layout_storyboard",
            "severity": "blocker",
            "message": (
                f"gold regression density failed: longest vertical blank band {longest_blank:.3f} "
                f"> maximum {max_blank:.3f}"
            ),
        })
    band_failures: list[dict[str, Any]] = []
    for idx, floor in enumerate(band_floors):
        if idx < len(band_values) and floor > 0 and band_values[idx] < floor:
            band_failures.append({"band": idx, "value": round(band_values[idx], 4), "floor": round(floor, 4)})
    max_band_failures = int(_safe_float_any(thresholds.get("max_vertical_band_floor_failures"), 0.0))
    if band_failures and len(band_failures) > max_band_failures:
        sample = ", ".join(
            f"b{item['band']}={item['value']:.3f}<{item['floor']:.3f}"
            for item in band_failures[:5]
        )
        failures.append({
            "id": "candidate_gold_visual_density_band_occupancy_low",
            "owner": "layout_storyboard",
            "severity": "blocker",
            "message": f"gold regression density failed: vertical band occupancy below floor ({sample})",
        })
    min_leaf_words = int(_safe_float_any(thresholds.get("min_leaf_visible_words"), 0.0))
    if min_leaf_words and leaf_words < min_leaf_words:
        failures.append({
            "id": "candidate_gold_leaf_visible_words_low",
            "owner": "content_strategy",
            "severity": "blocker",
            "message": (
                f"gold regression density failed: leaf visible words {leaf_words} "
                f"< floor {min_leaf_words}; grouped/hidden text does not satisfy dense poster copy"
            ),
        })
    min_figure_area = _safe_float_any(thresholds.get("min_source_figure_area_ratio"), 0.0)
    if min_figure_area and source_figure_area < min_figure_area:
        failures.append({
            "id": "candidate_gold_source_figure_area_low",
            "owner": "visual_curation",
            "severity": "blocker",
            "message": (
                f"gold regression density failed: source figure area {source_figure_area:.3f} "
                f"< floor {min_figure_area:.3f}"
            ),
        })
    min_native_units = int(_safe_float_any(thresholds.get("min_native_information_units"), 0.0))
    if min_native_units and native_units < min_native_units:
        failures.append({
            "id": "candidate_gold_native_information_units_low",
            "owner": "content_strategy",
            "severity": "blocker",
            "message": (
                f"gold regression density failed: native information units {native_units} "
                f"< floor {min_native_units}"
            ),
        })
    if repair_debt.get("known") and (
        int(repair_debt.get("contract_autofill_block_count") or 0) > 2
        or float(repair_debt.get("contract_autofill_word_share") or 0.0) > 0.03
        or int(repair_debt.get("contract_autofill_native_unit_count") or 0) > 0
    ):
        failures.append({
            "id": "candidate_gold_contract_autofill_debt",
            "owner": "designer_contract",
            "severity": "blocker",
            "message": (
                "gold regression density failed: deterministic contract supplement is being counted as content "
                f"(blocks={repair_debt.get('contract_autofill_block_count')}, "
                f"words={repair_debt.get('contract_autofill_word_count')}, "
                f"word_share={repair_debt.get('contract_autofill_word_share')}); "
                "Designer-authored HTML must fill panels before local supplements"
            ),
        })
    if panel_internal_p0 > 0:
        failures.append({
            "id": "candidate_gold_panel_internal_underfilled",
            "owner": "layout_storyboard",
            "severity": "blocker",
            "message": (
                f"gold regression density failed: {panel_internal_p0} large panel(s) have empty interiors; "
                "global non-white pixels cannot compensate for blank boxed regions"
            ),
        })
    if panel_word_budget_fail > 0:
        failures.append({
            "id": "candidate_gold_panel_word_budget_low",
            "owner": "content_strategy",
            "severity": "blocker",
            "message": (
                f"gold regression density failed: {panel_word_budget_fail} panel(s) miss their local word budget; "
                "the harness is panel-local, not a global poster word average"
            ),
        })
    if panel_native_unit_fail > 0:
        failures.append({
            "id": "candidate_gold_panel_native_units_low",
            "owner": "content_strategy",
            "severity": "blocker",
            "message": (
                f"gold regression density failed: {panel_native_unit_fail} panel(s) miss native unit budgets; "
                "fill boxes with real cards/tables/pipeline rows rather than empty surfaces"
            ),
        })
    if panel_visual_p0 > 0:
        failures.append({
            "id": "candidate_gold_panel_visual_underfilled",
            "owner": "layout_storyboard",
            "severity": "blocker",
            "message": (
                f"gold regression density failed: {panel_visual_p0} large panel(s) are visually underfilled "
                f"(min ink {panel_visual_min_ink:.3f}, min grid {panel_visual_min_grid:.3f}); "
                "dense references require local information inside each panel"
            ),
        })
    return {
        "reference_id": gold_reference.get("id") or gold_reference.get("name"),
        "thresholds": thresholds,
        "metrics": {
            "nonwhite_pixel_ratio": round(nonwhite, 4),
            "dark_ink_ratio": round(dark_ink, 4),
            "edge_density": round(edge_density, 4),
            "longest_blank_vertical_run_ratio": round(longest_blank, 4),
            "vertical_band_nonwhite_ratios": band_values,
            "leaf_visible_word_count": leaf_words,
            "source_figure_area_ratio": round(source_figure_area, 4),
            "native_information_unit_count": native_units,
            **repair_debt,
            "panel_internal_underfilled_p0_count": panel_internal_p0,
            "panel_internal_word_budget_fail_count": panel_word_budget_fail,
            "panel_internal_native_unit_fail_count": panel_native_unit_fail,
            "panel_visual_underfilled_p0_count": panel_visual_p0,
            "panel_visual_min_ink_ratio": round(panel_visual_min_ink, 4),
            "panel_visual_min_grid_coverage": round(panel_visual_min_grid, 4),
        },
        "band_floor_failures": band_failures,
        "failures": failures,
        "passes_visual_density": not failures,
    }


def _dense_gold_quality_floor_status(
    *,
    gold_reference: dict[str, Any],
    image: dict[str, Any],
    html: dict[str, Any],
    paper_poster: dict[str, Any],
    content_profile: dict[str, Any],
) -> dict[str, Any]:
    thresholds = _gold_quality_thresholds(gold_reference)
    if not thresholds:
        return {}
    payload = _dense_gold_visual_density_status(
        gold_reference={
            "id": gold_reference.get("handmade_gold_reference_id") or gold_reference.get("id"),
            "visual_density_floor": thresholds,
        },
        image=image,
        html=html,
        paper_poster=paper_poster,
        content_profile=content_profile,
    )
    failures: list[dict[str, Any]] = []
    for finding in payload.get("failures") or []:
        if not isinstance(finding, dict):
            continue
        issue_id = str(finding.get("id") or "candidate_gold_quality_floor_failed")
        issue_id = issue_id.replace("candidate_gold_visual_density", "candidate_gold_quality_visual_density")
        issue_id = issue_id.replace("candidate_gold_leaf", "candidate_gold_quality_leaf")
        issue_id = issue_id.replace("candidate_gold_source", "candidate_gold_quality_source")
        issue_id = issue_id.replace("candidate_gold_native", "candidate_gold_quality_native")
        message = str(finding.get("message") or "")
        message = message.replace("gold regression density failed", "handmade gold quality floor failed")
        failures.append({**finding, "id": issue_id, "message": message})
    return {
        **payload,
        "failures": failures,
        "passes_quality_floor": not failures,
    }


def _dense_gold_composite_status(
    *,
    gold_reference: dict[str, Any],
    image: dict[str, Any],
    html: dict[str, Any],
    paper_poster: dict[str, Any],
    content_profile: dict[str, Any],
) -> dict[str, Any]:
    baseline = gold_reference.get("gold_composite_baseline")
    if not isinstance(baseline, dict) or not baseline:
        return {}
    nonwhite = _safe_float_any(
        image.get("nonwhite_pixel_ratio"),
        max(0.0, 1.0 - _safe_float_any(image.get("white_space_ratio"), 1.0)),
    )
    dark_ink = _safe_float_any(image.get("dark_ink_ratio"), 0.0)
    longest_blank = _safe_float_any(image.get("longest_blank_vertical_run_ratio"), 0.0)
    band_min = _safe_float_any(image.get("vertical_band_nonwhite_min"), 0.0)
    leaf_words = _candidate_leaf_visible_words(html)
    source_figure_area = _candidate_source_figure_area_ratio(paper_poster, html)
    native_units = _candidate_native_information_units(content_profile, html)
    repair_debt = _candidate_contract_repair_debt(html)
    panel_count = int(_safe_float_any(html.get("dom_panel_count"), 0.0))
    section_count = int(_safe_float_any(html.get("section_label_like_count"), 0.0))

    def pos_ratio(value: float, target: float, *, cap: float = 1.18) -> float:
        if target <= 0:
            return 1.0
        return min(cap, max(0.0, value / target))

    def inverse_blank_score(value: float, target: float) -> float:
        allowed = max(0.08, target * 10.0)
        if value <= allowed:
            return 1.0
        return max(0.0, min(1.0, allowed / max(value, 1e-6)))

    components = {
        "nonwhite": pos_ratio(nonwhite, _safe_float_any(baseline.get("nonwhite_pixel_ratio"), 0.0)),
        "dark_ink": pos_ratio(dark_ink, _safe_float_any(baseline.get("dark_ink_ratio"), 0.0), cap=1.12),
        "blank_band": inverse_blank_score(longest_blank, _safe_float_any(baseline.get("longest_blank_vertical_run_ratio"), 0.0)),
        "band_min": pos_ratio(band_min, _safe_float_any(baseline.get("vertical_band_nonwhite_min"), 0.0), cap=1.12),
        "leaf_words": pos_ratio(float(leaf_words), _safe_float_any(baseline.get("leaf_visible_words"), 0.0), cap=1.12),
        "source_area": pos_ratio(source_figure_area, _safe_float_any(baseline.get("source_figure_area_ratio"), 0.0), cap=1.12),
        "native_units": pos_ratio(float(native_units), _safe_float_any(baseline.get("native_information_units"), 0.0), cap=1.12),
        "panel_count": pos_ratio(float(panel_count), _safe_float_any(baseline.get("panel_count"), 0.0), cap=1.08),
        "section_hierarchy": pos_ratio(float(section_count), max(8.0, _safe_float_any(baseline.get("section_heading_count"), 0.0) * 0.45), cap=1.08),
    }
    weights = {
        "nonwhite": 0.10,
        "dark_ink": 0.10,
        "blank_band": 0.12,
        "band_min": 0.10,
        "leaf_words": 0.18,
        "source_area": 0.10,
        "native_units": 0.18,
        "panel_count": 0.07,
        "section_hierarchy": 0.05,
    }
    score = sum(components[key] * weights[key] for key in weights)
    if repair_debt.get("known"):
        score -= min(
            0.18,
            0.025 * int(repair_debt.get("contract_autofill_block_count") or 0)
            + 0.20 * float(repair_debt.get("contract_autofill_word_share") or 0.0)
            + 0.015 * int(repair_debt.get("contract_autofill_native_unit_count") or 0),
        )
    threshold = _safe_float_any(baseline.get("min_composite_score"), 0.98)
    failures = []
    if score < threshold:
        weakest = sorted(components.items(), key=lambda item: item[1])[:3]
        failures.append({
            "id": "candidate_gold_composite_score_low",
            "owner": "layout_storyboard",
            "severity": "blocker",
            "message": (
                f"handmade gold composite score {score:.3f} < target {threshold:.3f}; "
                "weakest components: "
                + ", ".join(f"{key}={value:.2f}" for key, value in weakest)
            ),
        })
    return {
        "reference_id": gold_reference.get("handmade_gold_reference_id") or gold_reference.get("id"),
        "score": round(score, 4),
        "threshold": threshold,
        "components": {key: round(value, 4) for key, value in components.items()},
        "metrics": {
            "nonwhite_pixel_ratio": round(nonwhite, 4),
            "dark_ink_ratio": round(dark_ink, 4),
            "longest_blank_vertical_run_ratio": round(longest_blank, 4),
            "vertical_band_nonwhite_min": round(band_min, 4),
            "leaf_visible_word_count": leaf_words,
            "source_figure_area_ratio": round(source_figure_area, 4),
            "native_information_unit_count": native_units,
            **repair_debt,
            "panel_count": panel_count,
            "section_label_like_count": section_count,
        },
        "failures": failures,
        "passes_composite": not failures,
    }


def _is_local_dom_repair_finding(finding: dict[str, Any]) -> bool:
    issue_id = str(finding.get("id") or "").lower()
    repair_route = str(finding.get("repair_route") or "").lower()
    if any(token in issue_id for token in ("text-overflow", "text-overlap", "caption-overlap", "footer-overlap")):
        return True
    return repair_route in {"shrink_text", "revise_authored_html"} and not any(
        token in issue_id
        for token in ("image-not-loaded", "out-of-bounds", "root", "overflow-root", "size-mismatch")
    )


def _dense_gold_local_repair_only(
    *,
    gold_status: dict[str, Any],
    paper_poster: dict[str, Any],
) -> bool:
    if not gold_status or not gold_status.get("passes_visual_density"):
        return False
    dom_p0_count = int(_safe_float_any(paper_poster.get("dom_p0_count"), 0.0))
    if dom_p0_count <= 0:
        return False
    findings = [
        finding for finding in (paper_poster.get("dom_findings") or [])
        if isinstance(finding, dict) and str(finding.get("severity") or "").strip().lower() in {"p0", "blocker"}
    ]
    if not findings:
        return False
    return all(_is_local_dom_repair_finding(finding) for finding in findings)


def _dom_finding_issue_sample(findings: Any, *, limit: int = 3) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    if not isinstance(findings, list):
        return samples
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        samples.append({
            "id": finding.get("id") or finding.get("finding_id") or finding.get("kind"),
            "block": (
                finding.get("block")
                or finding.get("block_id")
                or finding.get("layer_id")
                or finding.get("panel")
                or finding.get("selector")
            ),
            "fix": finding.get("fix") or finding.get("suggested_fix") or finding.get("repair") or finding.get("message"),
        })
        if len(samples) >= limit:
            break
    return samples


def _panel_visual_issue_evidence(paper_poster: dict[str, Any]) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    metrics = paper_poster.get("panel_visual_metrics")
    if isinstance(metrics, dict):
        raw_samples = metrics.get("panel_visual_underfilled_samples")
        if isinstance(raw_samples, list):
            samples = [sample for sample in raw_samples if isinstance(sample, dict)]
    return {
        "panel_visual_underfilled_count": int(_safe_float_any(paper_poster.get("panel_visual_underfilled_count"), 0.0)),
        "panel_visual_underfilled_p0_count": int(_safe_float_any(paper_poster.get("panel_visual_underfilled_p0_count"), 0.0)),
        "panel_visual_min_ink_ratio": round(_safe_float_any(paper_poster.get("panel_visual_min_ink_ratio"), 1.0), 4),
        "panel_visual_min_grid_coverage": round(_safe_float_any(paper_poster.get("panel_visual_min_grid_coverage"), 1.0), 4),
        "panel_visual_max_blank_run_ratio": round(_safe_float_any(paper_poster.get("panel_visual_max_blank_run_ratio"), 0.0), 4),
        "samples": samples[:3],
    }


def _bbox_filled_but_low_ink_evidence(paper_poster: dict[str, Any]) -> dict[str, Any] | None:
    panel_visual_count = int(_safe_float_any(paper_poster.get("panel_visual_underfilled_count"), 0.0))
    if panel_visual_count <= 0:
        return None
    panel_internal_min = _safe_float_any(paper_poster.get("panel_internal_min_coverage"), 0.0)
    panel_internal_avg = _safe_float_any(paper_poster.get("panel_internal_avg_coverage"), 0.0)
    visual_min_ink = _safe_float_any(paper_poster.get("panel_visual_min_ink_ratio"), 1.0)
    visual_min_grid = _safe_float_any(paper_poster.get("panel_visual_min_grid_coverage"), 1.0)
    visual_blank = _safe_float_any(paper_poster.get("panel_visual_max_blank_run_ratio"), 0.0)
    dom_coverage_high = panel_internal_min >= 0.58 or panel_internal_avg >= 0.70
    preview_ink_low = visual_min_ink < 0.16 or visual_min_grid < 0.42 or visual_blank > 0.42
    if not (dom_coverage_high and preview_ink_low):
        return None
    return {
        **_panel_visual_issue_evidence(paper_poster),
        "panel_internal_min_coverage": round(panel_internal_min, 4),
        "panel_internal_avg_coverage": round(panel_internal_avg, 4),
    }


def compare_candidate(
    reference: dict[str, Any] | None,
    candidate: dict[str, Any] | None,
    *,
    case_slug: str | None = None,
    reference_html: dict[str, Any] | None = None,
    reference_metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not candidate:
        return None
    image = candidate.get("image") or {}
    html = candidate.get("html") or {}
    paper_poster = candidate.get("paper_poster") or {}
    content_profile = (
        candidate.get("content_value_profile")
        if isinstance(candidate.get("content_value_profile"), dict)
        else _candidate_content_value_profile(candidate)
    )
    manual_work = content_profile.get("manual_work_proxy") if isinstance(content_profile.get("manual_work_proxy"), dict) else {}
    archetype_profile = str(content_profile.get("profile") or _candidate_archetype_profile(candidate))
    reference_html_profile = str((reference_html or {}).get("reference_profile") or "")
    reference_metadata_profile = (
        str((reference_metadata or {}).get("reference_profile") or (reference_metadata or {}).get("profile") or "")
        if isinstance(reference_metadata, dict) else ""
    )
    if reference_html_profile == "research_synthesis_dense" or reference_metadata_profile == "research_synthesis_dense":
        archetype_profile = "research_synthesis_dense"
        if content_profile.get("profile") != "research_synthesis_dense":
            content_profile = dict(content_profile)
            content_profile["profile"] = "research_synthesis_dense"
            if isinstance(content_profile.get("targets"), dict):
                content_profile["targets"] = dict(content_profile["targets"])
                dense_target = ARCHETYPE_TARGET_PROFILES["research_synthesis_dense"]
                content_profile["targets"]["visual_area_min"] = dense_target["visual_area_min"]
                content_profile["targets"]["human_effort_min"] = dense_target["human_effort_min"]
    score = 1.0
    notes: list[str] = []
    issues: list[dict[str, Any]] = []

    def issue(
        issue_id: str,
        owner: str,
        severity: str,
        message: str,
        *,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        notes.append(message)
        row = {
            "id": issue_id,
            "owner": owner,
            "severity": severity,
            "message": message,
        }
        if evidence:
            row["evidence"] = evidence
        issues.append(row)

    gold_reference = _dense_gold_reference_for_case(
        case_slug,
        reference=reference,
        reference_html=reference_html,
        reference_metadata=reference_metadata,
        reference_profile=reference_metadata_profile or reference_html_profile or archetype_profile,
    )
    gold_status = _dense_gold_visual_density_status(
        gold_reference=gold_reference,
        image=image,
        html=html,
        paper_poster=paper_poster,
        content_profile=content_profile,
    ) if gold_reference else {}
    gold_quality_status = _dense_gold_quality_floor_status(
        gold_reference=gold_reference,
        image=image,
        html=html,
        paper_poster=paper_poster,
        content_profile=content_profile,
    ) if gold_reference else {}
    gold_composite_status = _dense_gold_composite_status(
        gold_reference=gold_reference,
        image=image,
        html=html,
        paper_poster=paper_poster,
        content_profile=content_profile,
    ) if gold_reference else {}
    if isinstance(paper_poster, dict) and gold_status:
        paper_poster["gold_regression_reference_id"] = gold_status.get("reference_id")
        paper_poster["primary_gold_reference_id"] = (
            gold_reference.get("handmade_gold_reference_id")
            or gold_status.get("reference_id")
        )
        paper_poster["gold_reference_role"] = (
            "primary_human_gold"
            if gold_reference.get("handmade_gold_reference_id")
            else str(gold_reference.get("role") or "gold_reference")
        )
        if gold_reference.get("secondary_visual_anchor_reference_id"):
            paper_poster["secondary_visual_anchor_reference_id"] = gold_reference.get("secondary_visual_anchor_reference_id")
            paper_poster["secondary_visual_anchor_role"] = gold_reference.get("secondary_visual_anchor_role")
        paper_poster["gold_visual_density_pass"] = bool(gold_status.get("passes_visual_density"))
        paper_poster["gold_visual_density_regression_count"] = len(gold_status.get("failures") or [])
        paper_poster["gold_visual_density_metrics"] = gold_status.get("metrics") or {}
        paper_poster["gold_visual_density_band_floor_failures"] = gold_status.get("band_floor_failures") or []
        paper_poster["leaf_visible_word_count"] = (gold_status.get("metrics") or {}).get("leaf_visible_word_count")
        paper_poster["dense_gold_local_repair_only"] = _dense_gold_local_repair_only(
            gold_status=gold_status,
            paper_poster=paper_poster,
        )
    if isinstance(paper_poster, dict) and gold_quality_status:
        paper_poster["gold_quality_floor_pass"] = bool(gold_quality_status.get("passes_quality_floor"))
        paper_poster["gold_quality_floor_regression_count"] = len(gold_quality_status.get("failures") or [])
        paper_poster["gold_quality_floor_metrics"] = gold_quality_status.get("metrics") or {}
    if isinstance(paper_poster, dict) and gold_composite_status:
        paper_poster["gold_composite_pass"] = bool(gold_composite_status.get("passes_composite"))
        paper_poster["gold_composite_score"] = gold_composite_status.get("score")
        paper_poster["gold_composite_threshold"] = gold_composite_status.get("threshold")
        paper_poster["gold_composite_components"] = gold_composite_status.get("components") or {}
        paper_poster["gold_composite_fail_count"] = len(gold_composite_status.get("failures") or [])

    generation = candidate.get("generation") or {}
    designer_contract_abort = bool(
        generation.get("designer_contract_abort")
        or generation.get("planner_contract_abort")
    )
    if designer_contract_abort:
        repeat_count = int(_safe_float_any(
            generation.get("designer_contract_abort_repeat_count")
            or generation.get("planner_contract_abort_repeat_count"),
            0.0,
        ))
        reason = str(
            generation.get("designer_contract_abort_reason")
            or generation.get("planner_contract_abort_reason")
            or "designer_contract_abort"
        )
        score -= 0.60
        issue(
            "candidate_designer_contract_abort",
            "designer_contract",
            "blocker",
            (
                f"AutoDesign terminated after repeated invalid designer tool calls "
                f"({reason}, repeat_count={repeat_count}); designer must call "
                "propose_design_spec with a complete top-level design_spec payload"
            ),
        )
    designer_api_error = str(
        generation.get("designer_api_error")
        or generation.get("planner_api_error")
        or ""
    ).strip()
    if designer_api_error:
        designer_model = str(generation.get("designer_model") or "unknown")
        score -= 0.30
        issue(
            "candidate_designer_model_api_error",
            "model_routing",
            "blocker",
            f"Designer model `{designer_model}` failed before proposing a DesignSpec: {designer_api_error}",
        )
    dense_recovery_count = int(_safe_float_any(generation.get("dense_recovery_count"), 0.0))
    if dense_recovery_count:
        reasons = [
            str(item)
            for item in (generation.get("dense_recovery_reasons") or [])
            if str(item).strip()
        ]
        latest_reason = str(generation.get("dense_recovery_reason") or (reasons[-1] if reasons else "unknown"))
        score -= 0.60
        issue(
            "candidate_deterministic_dense_recovery_unacceptable",
            "harness_reliability",
            "blocker",
            (
                f"final candidate used deterministic dense HTML recovery "
                f"({latest_reason}, count={dense_recovery_count}); dogfood must "
                "evaluate the designer-authored HTML directly instead of a fallback template"
            ),
        )
    spec_recovery_count = int(_safe_float_any(generation.get("spec_recovery_count"), 0.0))
    if spec_recovery_count:
        reasons = [
            str(item)
            for item in (generation.get("spec_recovery_reasons") or [])
            if str(item).strip()
        ]
        latest_reason = str(generation.get("spec_recovery_reason") or (reasons[-1] if reasons else "unknown"))
        timeout_recovery = (
            "designer_timeout_after_poster_contract" in reasons
            or latest_reason == "designer_timeout_after_poster_contract"
            or "planner_timeout_after_poster_contract" in reasons
            or latest_reason == "planner_timeout_after_poster_contract"
        )
        missing_args_recovery = "designer_missing_design_spec_args" in reasons or latest_reason == "designer_missing_design_spec_args"
        if timeout_recovery:
            score -= 0.60
            issue(
                "candidate_timeout_spec_recovery_unacceptable",
                "harness_reliability",
                "blocker",
                (
                    "final candidate came from runner-owned timeout deterministic "
                    "DesignSpec recovery; keep it for diagnosis only and do not "
                    "accept it as a valid dogfood candidate"
                ),
            )
        elif missing_args_recovery:
            score -= 0.40
            issue(
                "candidate_missing_design_spec_recovered",
                "designer_contract",
                "blocker",
                (
                    "final candidate used deterministic recovery after planner "
                    "called propose_design_spec without design_spec; dogfood "
                    "runs must force the planner to author a real DesignSpec"
                ),
            )
        elif spec_recovery_count > 1:
            score -= 0.12
            issue(
                "candidate_multiple_spec_recoveries",
                "harness_reliability",
                "high",
                (
                    f"DesignSpec was deterministically recovered {spec_recovery_count} times "
                    f"({', '.join(reasons[:4])}); repeated empty/recovery paths should be treated as reliability debt"
                ),
            )
        else:
            score -= 0.04
            issue(
                "candidate_spec_recovered",
                "harness_reliability",
                "medium",
                (
                    f"final candidate used deterministic DesignSpec recovery: {latest_reason}; "
                    "review visually instead of treating the run as an authored planner success"
                ),
            )
    terminal_status = str(generation.get("terminal_status") or "")
    if terminal_status and terminal_status not in {"pass", "revise"}:
        finalized = generation.get("finalized") is True
        has_usable_partial_candidate = (
            bool(generation.get("generation_timeout"))
            and bool(generation.get("partial_timeout_candidate_evaluated"))
            and bool(candidate.get("image") or candidate.get("html"))
        )
        report_only_partial = (
            bool(generation.get("dogfood_report_only_after_blocking_composite"))
            and bool(candidate.get("image") or candidate.get("html"))
        ) or has_usable_partial_candidate
        score -= 0.04 if report_only_partial else (0.28 if finalized else 0.35)
        blocker_count = generation.get("remaining_blocking_findings")
        detail = (
            f"; remaining blockers: {blocker_count}"
            if blocker_count is not None else ""
        )
        if not designer_api_error:
            if report_only_partial:
                issue(
                    "candidate_dogfood_report_only_partial",
                    "deterministic_env_feedback",
                    "medium",
                    (
                        f"Dogfood stopped after a blocking composite with terminal_status={terminal_status}{detail}; "
                        "treat the artifact as a valid visual/layout diagnostic instead of a generation-failure blocker"
                    ),
                )
            elif finalized:
                issue(
                    "candidate_generation_terminal_fail",
                    "deterministic_env_feedback",
                    "blocker",
                    f"AutoDesign finalized but ended with terminal_status={terminal_status}{detail}",
                )
            else:
                issue(
                    "candidate_generation_not_finalized",
                    "deterministic_env_feedback",
                    "blocker",
                    f"AutoDesign run ended with terminal_status={terminal_status}{detail}",
                )
    if not candidate.get("image") and not candidate.get("html"):
        score -= 0.50
        issue(
            "candidate_artifact_missing",
            "harness_reliability",
            "blocker",
            "generated run did not expose a preview image or HTML candidate artifact",
        )

    if reference and image:
        aspect_delta = abs(float(image.get("aspect_ratio") or 0) - float(reference.get("aspect_ratio") or 0))
        white_delta = abs(float(image.get("white_space_ratio") or 0) - float(reference.get("white_space_ratio") or 0))
        edge_delta = abs(float(image.get("edge_density") or 0) - float(reference.get("edge_density") or 0))
        if aspect_delta > 0.12:
            score -= 0.04
            issue(
                "candidate_aspect_mismatch",
                "eval_calibration",
                "medium" if aspect_delta > 0.5 else "low",
                (
                    f"aspect differs from rendered reference by {aspect_delta:.2f}; "
                    "case template/orientation checks carry the hard aspect contract"
                ),
            )
        if white_delta > 0.18:
            score -= 0.14
            issue(
                "candidate_whitespace_mismatch",
                "layout_storyboard",
                "medium",
                f"white-space proxy differs from reference by {white_delta:.2f}",
            )
        if edge_delta > 0.08:
            score -= 0.10
            issue(
                "candidate_edge_density_mismatch",
                "eval_calibration",
                "medium",
                f"edge-density proxy differs from reference by {edge_delta:.2f}",
            )

    if gold_status:
        gold_failures = [
            finding for finding in (gold_status.get("failures") or [])
            if isinstance(finding, dict)
        ]
        for finding in gold_failures:
            score -= 0.12
            issue(
                str(finding.get("id") or "candidate_gold_visual_density_regression"),
                str(finding.get("owner") or "layout_storyboard"),
                str(finding.get("severity") or "blocker"),
                str(finding.get("message") or "candidate regressed below the iteration-51 LongCat gold density floor"),
            )
    if gold_quality_status:
        for finding in [
            item for item in (gold_quality_status.get("failures") or [])
            if isinstance(item, dict)
        ]:
            score -= 0.10
            issue(
                str(finding.get("id") or "candidate_gold_quality_floor_failed"),
                str(finding.get("owner") or "layout_storyboard"),
                str(finding.get("severity") or "blocker"),
                str(finding.get("message") or "candidate regressed below handmade gold quality floor"),
            )
    if gold_composite_status:
        for finding in [
            item for item in (gold_composite_status.get("failures") or [])
            if isinstance(item, dict)
        ]:
            score -= 0.16
            issue(
                str(finding.get("id") or "candidate_gold_composite_score_low"),
                str(finding.get("owner") or "layout_storyboard"),
                str(finding.get("severity") or "blocker"),
                str(finding.get("message") or "candidate did not beat handmade gold composite signal"),
            )

    if html:
        visual_ratio = (
            float(html.get("authored_visual_area_ratio") or 0.0)
            if "authored_visual_area_ratio" in html
            else float(html.get("visual_area_ratio") or 0.0)
        )
        top_half_visual_ratio = float(html.get("top_half_visual_area_ratio") or 0.0)
        words = (
            int(html.get("authored_visible_text_word_count") or 0)
            if "authored_visible_text_word_count" in html
            else int(html.get("visible_text_word_count") or 0)
        )
        max_words = int(html.get("max_text_layer_words") or 0)
        avg_words = float(html.get("avg_text_layer_words") or 0.0)
        image_layers = int(html.get("image_layer_count") or 0)
        caption_like = int(html.get("caption_like_text_count") or 0)
        section_labels = int(html.get("section_label_like_count") or 0)
        dom_panels = int(html.get("dom_panel_count") or 0)
        image_backed_panels = int(html.get("image_backed_panel_count") or 0)
        image_panel_text_low = int(html.get("image_backed_panel_text_low_count") or 0)
        underfilled_panels = int(html.get("underfilled_panel_count") or 0)
        title_mismatches = int(html.get("section_title_content_mismatch_count") or 0)
        terse_number_captions = int(html.get("terse_figure_number_caption_count") or 0)
        template_instruction_count = int(html.get("template_instruction_text_count") or 0)
        template_instruction_samples = [
            str(item)
            for item in (html.get("template_instruction_text_samples") or [])
            if str(item).strip()
        ][:3]
        duplicate_text_blocks = int(html.get("duplicate_text_block_count") or 0)
        duplicate_text_samples = [
            str(item)
            for item in (html.get("duplicate_text_block_samples") or [])
            if str(item).strip()
        ][:3]
        repeated_ngram_groups = int(html.get("repeated_ngram_group_count") or 0)
        repeated_ngram_samples = [
            str(item)
            for item in (html.get("repeated_ngram_samples") or [])
            if str(item).strip()
        ][:3]
        repetition_debt = float(html.get("text_repetition_debt_score") or 0.0)
        manual_overall = float(manual_work.get("overall") or 0.0)
        semantic_score = float(manual_work.get("semantic_synthesis_score") or 0.0)
        native_score = float(manual_work.get("native_reconstruction_score") or 0.0)
        mechanical_discount = float(manual_work.get("mechanical_screenshot_discount") or 0.0)
        image_panel_text_score = float(manual_work.get("image_panel_text_score") or 0.0)
        panel_fill_score = float(manual_work.get("panel_fill_score") or 0.0)
        section_title_alignment_score = float(manual_work.get("section_title_alignment_score") or 0.0)
        mixed_panel_score = max(
            float(manual_work.get("mixed_panel_binding_score") or 0.0),
            float(html.get("authored_mixed_panel_binding_score") or 0.0),
            float(html.get("mixed_panel_binding_score") or 0.0),
        )
        role_coverage = float(content_profile.get("section_role_coverage") or 0.0)
        target = ARCHETYPE_TARGET_PROFILES.get(archetype_profile, ARCHETYPE_TARGET_PROFILES["default"])
        profile_targets = content_profile.get("targets") if isinstance(content_profile.get("targets"), dict) else {}
        text_density_floor = int(profile_targets.get("text_density_floor") or _profile_text_density_floor(archetype_profile))
        visual_min = float(target.get("visual_area_min") or 0.32)
        repair_debt = _candidate_contract_repair_debt(html)
        if repair_debt.get("known") and (
            int(repair_debt.get("contract_autofill_block_count") or 0) > 2
            or float(repair_debt.get("contract_autofill_word_share") or 0.0) > 0.03
            or int(repair_debt.get("contract_autofill_native_unit_count") or 0) > 0
        ):
            score -= 0.22
            issue(
                "candidate_contract_autofill_debt",
                "designer_contract",
                "blocker",
                (
                    "candidate relies on deterministic contract supplement instead of designer-authored panel payloads: "
                    f"blocks={repair_debt.get('contract_autofill_block_count')}, "
                    f"words={repair_debt.get('contract_autofill_word_count')}, "
                    f"word_share={repair_debt.get('contract_autofill_word_share')}"
                ),
            )
        typography_count = int(_safe_float_any(html.get("typography_text_layer_count"), 0.0))
        if typography_count >= 4:
            times_ratio = _safe_float_any(html.get("typography_times_new_roman_ratio"), 0.0)
            if times_ratio < 0.98:
                score -= 0.16
                issue(
                    "candidate_academic_font_family_not_times_new_roman",
                    "typography_system",
                    "blocker",
                    (
                        f"academic poster typography contract failed: only {times_ratio:.2f} "
                        "of authored text layers use Times New Roman"
                    ),
                )
            if html.get("typography_font_size_gradient_ok") is False:
                level_count = int(_safe_float_any(html.get("typography_font_size_level_count"), 0.0))
                score -= 0.08
                issue(
                    "candidate_academic_font_size_gradient_weak",
                    "typography_system",
                    "high",
                    (
                        f"academic poster typography contract failed: {level_count} font-size levels; "
                        "expected a controlled title/section/body/caption gradient"
                    ),
                )
            if html.get("typography_weight_contract_ok") is False:
                score -= 0.06
                issue(
                    "candidate_academic_font_weight_contract_weak",
                    "typography_system",
                    "medium",
                    "academic poster typography contract failed: heading/body font weights are not consistently normalized",
                )
        if "palette_contract_pass" in html and not bool(html.get("palette_contract_pass")):
            accent_count = int(_safe_float_any(html.get("palette_accent_color_count"), 0.0))
            family_count = int(_safe_float_any(html.get("palette_accent_family_count"), 0.0))
            score -= 0.12
            issue(
                "candidate_palette_consistency_low",
                "layout_storyboard",
                "high",
                (
                    f"palette is too fragmented for a reference-like academic poster: "
                    f"{accent_count} accent colors across {family_count} hue families; "
                    "use one paper background plus one main accent and one auxiliary accent"
                ),
            )
        if visual_ratio < visual_min:
            deficit = visual_min - visual_ratio
            editorial_support_ok = (
                words >= 360
                and mixed_panel_score >= 0.55
                and manual_overall >= max(0.58, float(target.get("human_effort_min") or 0.58) - 0.08)
                and archetype_profile in {"table_first_benchmark", "visual_evidence_wall", "research_synthesis_dense"}
            )
            if editorial_support_ok and deficit < 0.12:
                score -= 0.04
                issue(
                    "candidate_visual_area_soft_low",
                    "eval_calibration",
                    "low",
                    (
                        f"visual area is below the profile target ({visual_ratio:.2f} < {visual_min:.2f}) "
                        "but dense text and local figure/text binding make this a soft signal"
                    ),
                )
            else:
                score -= 0.14 if deficit >= 0.10 else 0.08
                issue(
                    "candidate_visual_area_low",
                    "designer_contract",
                    "blocker" if deficit >= 0.18 and archetype_profile not in {"theory_text_board", "research_synthesis_dense", "table_first_benchmark"} else "high",
                    (
                        f"low generated visual area ratio for {archetype_profile}: "
                        f"{visual_ratio:.2f} < target {visual_min:.2f}"
                    ),
                )
        if words < 220:
            score -= 0.12
            issue(
                "candidate_visible_text_low",
                "designer_contract",
                "medium",
                f"low visible text word count for academic poster: {words}",
            )
        if words < text_density_floor:
            deficit = (text_density_floor - words) / max(1.0, float(text_density_floor))
            score -= min(0.22, 0.08 + 0.20 * deficit)
            issue(
                "candidate_text_density_below_human_effort_floor",
                "content_strategy",
                "blocker" if archetype_profile in {"research_synthesis_dense", "theory_text_board"} and deficit >= 0.20 else "high",
                (
                    f"visible text density is below the human-effort floor for {archetype_profile}: "
                    f"{words} < {text_density_floor}; prioritize writing dense, source-backed "
                    "panel copy before optimizing screenshot placement"
                ),
            )
        if template_instruction_count:
            score -= min(0.20, 0.07 * template_instruction_count)
            issue(
                "candidate_template_instruction_text_visible",
                "content_strategy",
                "blocker" if template_instruction_count >= 2 else "high",
                (
                    f"{template_instruction_count} visible text block(s) look like generator/template instructions "
                    f"instead of paper content"
                    + (f"; examples: {' | '.join(template_instruction_samples)}" if template_instruction_samples else "")
                ),
            )
        if duplicate_text_blocks >= 3:
            score -= min(0.22, 0.05 + 0.025 * duplicate_text_blocks)
            issue(
                "candidate_duplicate_text_blocks_visible",
                "content_strategy",
                "blocker" if duplicate_text_blocks >= 8 else "high",
                (
                    f"{duplicate_text_blocks} repeated visible text block instance(s) detected; "
                    "dense posters must add new paper facts, not reuse the same claim across panels"
                    + (f"; examples: {' | '.join(duplicate_text_samples)}" if duplicate_text_samples else "")
                ),
            )
        if repeated_ngram_groups >= 4:
            score -= min(0.24, 0.06 + 0.025 * repeated_ngram_groups + 0.12 * repetition_debt)
            issue(
                "candidate_repeated_text_ngrams_visible",
                "content_strategy",
                "blocker" if repeated_ngram_groups >= 10 or repetition_debt >= 0.45 else "high",
                (
                    f"{repeated_ngram_groups} repeated long phrase group(s) detected; "
                    "the planner is filling density with redundant wording instead of panel-specific synthesis"
                    + (f"; examples: {' | '.join(repeated_ngram_samples)}" if repeated_ngram_samples else "")
                ),
            )
        if dom_panels >= 8:
            words_per_panel = words / max(1, dom_panels)
            per_panel_floor = 64.0 if archetype_profile in {"research_synthesis_dense", "theory_text_board"} else 48.0
            if words_per_panel < per_panel_floor:
                score -= min(0.16, 0.04 + (per_panel_floor - words_per_panel) / per_panel_floor * 0.16)
                issue(
                    "candidate_panel_text_density_low",
                    "content_strategy",
                    "high",
                    (
                        f"average panel text density is too low: {words_per_panel:.1f} words/panel "
                        f"across {dom_panels} panels; empty boxes should be filled with paper-digested "
                        "claims, caveats, result interpretation, and figure explanations"
                    ),
                )
        if max_words > 120 or (max_words > 90 and manual_overall < 0.68):
            score -= 0.10
            issue(
                "candidate_long_text_layer",
                "typography_system",
                "medium",
                f"longest text layer has {max_words} words",
            )
        if (
            image_layers >= 4
            and caption_like < min(4, max(2, image_layers // 3))
            and mixed_panel_score < 0.55
            and image_panel_text_score < 0.70
        ):
            score -= 0.12
            issue(
                "candidate_caption_coverage_low",
                "deterministic_env_feedback",
                "high",
                f"only {caption_like} caption/reference-like layers and weak local explanatory text for {image_layers} visuals",
            )
        if (
            image_layers >= 4
            and top_half_visual_ratio < 0.16
            and mixed_panel_score < 0.55
            and archetype_profile not in {"theory_text_board", "research_synthesis_dense", "table_first_benchmark"}
        ):
            score -= 0.12
            issue(
                "candidate_top_half_visual_fill_low",
                "layout_storyboard",
                "high",
                f"top-half visual fill is low: {top_half_visual_ratio:.2f}",
            )
        if image_layers >= 4 and mixed_panel_score < 0.45 and caption_like < max(2, image_layers // 2):
            score -= 0.10
            issue(
                "candidate_visual_text_binding_low",
                "layout_storyboard",
                "high",
                (
                    f"only {mixed_panel_score:.2f} of visuals have nearby explanatory text; "
                    "avoid separating evidence walls from their claims"
                ),
            )
        if image_backed_panels and image_panel_text_low:
            score -= min(0.16, 0.04 * image_panel_text_low)
            issue(
                "candidate_image_panel_explanation_low",
                "layout_storyboard",
                "high",
                (
                    f"{image_panel_text_low}/{image_backed_panels} image-backed panels have too little "
                    "local explanatory text; every figure/table panel should interleave evidence with the claim it supports"
                ),
            )
        if dom_panels and underfilled_panels >= max(2, math.ceil(dom_panels * 0.18)):
            score -= min(0.18, 0.035 * underfilled_panels + (0.04 if panel_fill_score < 0.82 else 0.0))
            issue(
                "candidate_panel_fill_low",
                "content_strategy",
                "blocker" if panel_fill_score < 0.65 else "high",
                (
                    f"{underfilled_panels}/{dom_panels} panels look underfilled; empty boxes should be "
                    "shrunk/merged or filled with source-backed synthesis text"
                ),
            )
        if title_mismatches:
            score -= min(0.08, 0.04 * title_mismatches)
            issue(
                "candidate_section_title_content_mismatch",
                "designer_contract",
                "high" if section_title_alignment_score < 0.88 else "medium",
                (
                    f"{title_mismatches} panel title(s) do not match their contents; "
                    "section names must describe the actual evidence or synthesis inside the box"
                ),
            )
        if (
            terse_number_captions >= max(2, image_backed_panels // 2)
            and image_panel_text_score < 0.82
        ):
            score -= 0.06
            issue(
                "candidate_figure_number_caption_overuse",
                "content_strategy",
                "medium",
                (
                    f"{terse_number_captions} short Fig./Table-number captions detected; "
                    "remove bare figure labels and use dense local prose about what each source visual proves"
                ),
            )
        elif terse_number_captions >= max(3, image_backed_panels // 2):
            score -= 0.02
            issue(
                "candidate_figure_number_caption_overuse",
                "content_strategy",
                "low",
                (
                    f"{terse_number_captions} short Fig./Table-number captions detected; "
                    "the panel text is otherwise adequate, but those labels should become explanatory prose"
                ),
            )
        if section_labels < 4:
            score -= 0.08
            issue(
                "candidate_section_storyboard_weak",
                "designer_contract",
                "medium",
                f"only {section_labels} compact section-label-like layers detected",
            )
        if avg_words > 46 or (avg_words > 38 and semantic_score < 0.65):
            score -= 0.08
            issue(
                "candidate_panel_text_too_paragraphic",
                "typography_system",
                "medium",
                f"average text layer has {avg_words:.1f} words; panel copy may be too paragraph-like",
            )
        human_min = float(target.get("human_effort_min") or 0.58)
        if manual_overall < human_min:
            score -= 0.14 if manual_overall < human_min - 0.16 else 0.08
            issue(
                "candidate_editorial_labor_low",
                "content_strategy",
                "high" if manual_overall < human_min - 0.16 else "medium",
                (
                    f"manual-work proxy is low for {archetype_profile}: "
                    f"{manual_overall:.2f} < target {human_min:.2f}; "
                    "poster needs stronger synthesis, hierarchy, and section continuity"
                ),
            )
        needs_native = archetype_profile in {
            "table_first_benchmark",
            "theory_text_board",
            "research_synthesis_dense",
            "multi_view_matrix_graph",
        }
        if needs_native and native_score < 0.42 and role_coverage < 0.72:
            score -= 0.12
            issue(
                "candidate_native_reconstruction_low",
                "content_strategy",
                "high",
                (
                    f"native reconstruction is low for {archetype_profile}: "
                    f"{native_score:.2f}; expected editable tables/formulas/cards/pipeline structure"
                ),
            )
        if mechanical_discount >= 0.10 and semantic_score < 0.58:
            score -= 0.10
            issue(
                "candidate_mechanical_screenshot_wall",
                "layout_storyboard",
                "high",
                (
                    "visual area is high but the poster looks mechanically screenshot-heavy; "
                    f"semantic synthesis={semantic_score:.2f}, native reconstruction={native_score:.2f}"
                ),
            )
        if reference_html_profile == "research_synthesis_dense":
            dense_penalty = _dense_synthesis_reference_penalty(
                reference_html=reference_html or {},
                candidate=candidate,
                content_profile=content_profile,
                html=html,
                issue=issue,
            )
            score -= dense_penalty
        if reference_metadata:
            metadata_penalty = _metadata_reference_penalty(
                reference_metadata=reference_metadata,
                candidate=candidate,
                content_profile=content_profile,
                html=html,
                issue=issue,
            )
            score -= metadata_penalty

    if paper_poster:
        for finding in paper_poster.get("consistency_findings") or []:
            if not isinstance(finding, dict):
                continue
            issue_id = str(finding.get("id") or "candidate_authored_artifact_mismatch")
            owner = "harness_reliability" if "stale" in issue_id or "not_authored" in issue_id else "renderer_export"
            score -= 0.25 if issue_id == "candidate_final_not_authored_html" else 0.18
            issue(
                issue_id,
                owner,
                "blocker",
                str(finding.get("message") or "Authored paper-poster final artifact consistency failed."),
            )
        if paper_poster.get("final_is_authored_html") is False and not paper_poster.get("consistency_findings"):
            score -= 0.25
            issue(
                "candidate_final_not_authored_html",
                "harness_reliability",
                "blocker",
                "academic paper poster final/poster.html is not authored HTML",
            )
        dom_p0_count = int(paper_poster.get("dom_p0_count") or 0)
        if dom_p0_count:
            dom_finding_samples = _dom_finding_issue_sample(paper_poster.get("dom_findings"), limit=3)
            dom_finding_suffix = ""
            if dom_finding_samples:
                dom_finding_suffix = "; first findings: " + "; ".join(
                    "/".join(
                        str(part)
                        for part in (
                            sample.get("id") or "unknown",
                            sample.get("block") or "unknown_block",
                            sample.get("fix") or "no_fix",
                        )
                    )
                    for sample in dom_finding_samples
                )
            if bool(paper_poster.get("dense_gold_local_repair_only")):
                score -= 0.05
                issue(
                    "candidate_dom_local_repair_needed",
                    "renderer_export",
                    "high",
                    (
                        f"paper_poster_dom_audit reports {dom_p0_count} local text/layout P0 issue(s), "
                        "but dense gold density floors pass; repair these locally instead of "
                        "sparsifying the whole poster"
                        f"{dom_finding_suffix}"
                    ),
                    evidence={"dom_findings": dom_finding_samples} if dom_finding_samples else None,
                )
            else:
                score -= 0.20
                issue(
                    "candidate_dom_audit_p0",
                    "layout_storyboard",
                    "blocker",
                    f"paper_poster_dom_audit reports {dom_p0_count} P0 issue(s){dom_finding_suffix}",
                    evidence={"dom_findings": dom_finding_samples} if dom_finding_samples else None,
                )
        if paper_poster.get("preview_fallback_used"):
            score -= 0.18
            issue(
                "candidate_preview_fallback",
                "renderer_export",
                "blocker",
                "authored HTML preview used the fallback renderer path",
            )
        if int(paper_poster.get("image_not_loaded_count") or 0):
            score -= 0.18
            issue(
                "candidate_dom_image_not_loaded",
                "visual_curation",
                "blocker",
                "authored HTML DOM audit detected unloaded image assets",
            )
        if int(paper_poster.get("unbacked_source_image_count") or 0):
            score -= 0.10
            issue(
                "candidate_unbacked_source_image",
                "visual_curation",
                "high",
                "authored HTML includes paper images not bound to source provenance assets",
            )
        if int(paper_poster.get("page_like_source_figure_count") or paper_poster.get("page_like_source_dom_image_count") or 0):
            score -= 0.22
            issue(
                "candidate_page_like_source_figure",
                "visual_curation",
                "blocker",
                "source figure slot uses a full-page/body-text PDF crop instead of a clean chart, diagram, table, or sub-panel",
            )
        panel_underfilled = int(
            paper_poster.get("panel_underfilled_count")
            or paper_poster.get("panel_internal_underfilled_p0_count")
            or 0
        )
        if panel_underfilled:
            score -= 0.16
            issue(
                "candidate_panel_internal_underfilled",
                "layout_storyboard",
                "blocker",
                f"{panel_underfilled} large panel(s) have empty interiors compared with human gold references",
            )
        panel_visual_underfilled = int(paper_poster.get("panel_visual_underfilled_count") or 0)
        panel_visual_underfilled_p0 = int(paper_poster.get("panel_visual_underfilled_p0_count") or 0)
        if panel_visual_underfilled:
            panel_visual_severity = "blocker" if panel_visual_underfilled_p0 else "high"
            score -= 0.20 if panel_visual_underfilled_p0 else 0.12
            issue(
                "candidate_panel_visual_underfilled",
                "layout_storyboard",
                panel_visual_severity,
                (
                    f"{panel_visual_underfilled} large panel(s) have visually underfilled interiors "
                    f"({panel_visual_underfilled_p0} P0) despite passing coarse global density"
                ),
                evidence=_panel_visual_issue_evidence(paper_poster),
            )
        bbox_low_ink_evidence = _bbox_filled_but_low_ink_evidence(paper_poster)
        if bbox_low_ink_evidence:
            score -= 0.08
            issue(
                "candidate_bbox_filled_but_low_ink",
                "layout_storyboard",
                "high",
                (
                    "DOM/panel bbox coverage is high, but preview pixels show low ink or sparse grid coverage; "
                    "filled boxes must contain visible readable content, not pale/blank surfaces"
                ),
                evidence=bbox_low_ink_evidence,
            )
        if int(paper_poster.get("selected_source_asset_dom_missing_count") or 0):
            score -= 0.10
            issue(
                "candidate_selected_source_asset_missing",
                "visual_curation",
                "high",
                "storyboard-selected source assets are missing from the authored HTML DOM",
            )
        unresolved_quality = [
            finding for finding in [
                *(paper_poster.get("layout_quality_findings") or []),
                *(paper_poster.get("preview_quality_findings") or []),
            ]
            if isinstance(finding, dict)
        ]
        if unresolved_quality:
            score -= min(0.24, 0.08 + 0.035 * len(unresolved_quality))
            sample = "; ".join(
                str(item.get("id") or item.get("message") or "quality_finding")
                for item in unresolved_quality[:5]
            )
            issue(
                "candidate_unresolved_density_style_quality_debt",
                "layout_storyboard",
                "medium",
                (
                    f"{len(unresolved_quality)} paper-poster density/style "
                    f"finding(s) remain as quality debt after render QA: {sample}"
                ),
            )

    if image and float(image.get("empty_cell_ratio") or 0.0) > 0.35:
        score -= 0.18
        issue(
            "candidate_sparse_grid_cells",
            "layout_storyboard",
            "high",
            "visual mass has too many sparse grid cells; large blank areas count against human-effort density",
        )

    if html and not any(str(row.get("severity") or "") == "blocker" for row in issues):
        human_effort_floor = 0.0
        manual_overall = float(manual_work.get("overall") or 0.0)
        paper_faithfulness = float(manual_work.get("paper_faithfulness_proxy") or 0.0)
        narrative_coherence = float(manual_work.get("narrative_coherence_proxy") or 0.0)
        native_score = float(manual_work.get("native_reconstruction_score") or 0.0)
        anti_abstract_dump = float(manual_work.get("anti_abstract_dump") or 0.0)
        words = int(html.get("visible_text_word_count") or 0)
        profile_targets = content_profile.get("targets") if isinstance(content_profile.get("targets"), dict) else {}
        text_density_floor = int(profile_targets.get("text_density_floor") or _profile_text_density_floor(archetype_profile))
        panel_fill_score = float(manual_work.get("panel_fill_score") or 0.0)
        mixed_panel_score = max(
            float(manual_work.get("mixed_panel_binding_score") or 0.0),
            float(html.get("mixed_panel_binding_score") or 0.0),
        )
        if (
            archetype_profile == "research_synthesis_dense"
            and manual_overall >= 0.80
            and paper_faithfulness >= 0.90
            and narrative_coherence >= 0.78
            and native_score >= 0.74
            and anti_abstract_dump >= 0.80
            and words >= text_density_floor
            and panel_fill_score >= 0.86
        ):
            human_effort_floor = 0.64
        elif (
            archetype_profile == "table_first_benchmark"
            and manual_overall >= 0.70
            and paper_faithfulness >= 0.86
            and mixed_panel_score >= 0.70
            and anti_abstract_dump >= 0.80
            and words >= text_density_floor
            and panel_fill_score >= 0.82
        ):
            human_effort_floor = 0.64
        if human_effort_floor and score < human_effort_floor:
            score = human_effort_floor
            notes.append(
                (
                    "human-effort anchor floor applied: manual-work, source-faithfulness, "
                    "native reconstruction, and anti-abstract-dump signals prevent a "
                    "known-good mixed-panel poster from being scored as a failure"
                ),
            )

    score = max(0.0, min(1.0, score))
    return {
        "proxy_score": round(score, 3),
        "notes": notes,
        "issues": issues,
        "content_value_profile": content_profile,
    }


def build_label_calibration(
    *,
    label_set: dict[str, Any],
    results: list[dict[str, Any]],
    all_cases: list[PosterCase] | None = None,
) -> dict[str, Any]:
    """Evaluate labeled reference posters without generating candidates.

    This is an evaluator-calibration pass: it asks whether current offline
    proxy rules classify known good, near-miss, and bad reference posters in
    the direction a human expects. It deliberately does not compare a poster
    against itself with ``compare_candidate`` because that would make every
    reference look perfect.
    """
    if not label_set:
        return {}
    raw_labels = label_set.get("case_labels")
    if not isinstance(raw_labels, dict) or not raw_labels:
        return {}

    by_case = {
        str((result.get("case") or {}).get("slug") or ""): result
        for result in results
    }
    discovered_cases = {
        case.slug: case
        for case in (all_cases or [])
    }
    rows: list[dict[str, Any]] = []
    for slug in _label_set_cases(label_set):
        label = raw_labels.get(slug)
        if not isinstance(label, dict):
            rows.append({
                "case": slug,
                "status": "missing_label",
                "expected_verdict": None,
                "observed_verdict": "fail",
                "match": False,
                "issues": [{
                    "id": "label_missing",
                    "owner": "eval_calibration",
                    "severity": "blocker",
                    "message": "Case is listed in label set but has no label record.",
                }],
            })
            continue
        result = by_case.get(slug)
        reference = (result or {}).get("reference") if isinstance(result, dict) else None
        reference_html = (result or {}).get("reference_html") if isinstance(result, dict) else None
        if not isinstance(reference, dict) and not isinstance(reference_html, dict):
            reference_payload = _label_reference_metrics(slug, discovered_cases)
            reference = reference_payload.get("image") if isinstance(reference_payload, dict) else None
            reference_html = reference_payload.get("html") if isinstance(reference_payload, dict) else None
        observed = _reference_proxy_verdict(
            reference if isinstance(reference, dict) else None,
            reference_html=reference_html if isinstance(reference_html, dict) else None,
            label=label,
        )
        expected = str(label.get("expected_verdict") or "").strip().lower()
        allowed = [
            str(v).strip().lower()
            for v in (label.get("acceptable_verdicts") or [expected])
            if str(v).strip()
        ]
        match = observed["verdict"] in set(allowed)
        missed_axes = []
        if not match and observed["verdict"] == "pass":
            missed_axes = [
                str(axis)
                for axis in (label.get("expected_issue_axes") or [])
                if str(axis or "").strip()
            ]
        rows.append({
            "case": slug,
            "status": "pass" if match else "mismatch",
            "label": label.get("label"),
            "quality_tier": label.get("quality_tier"),
            "expected_verdict": expected,
            "acceptable_verdicts": allowed,
            "observed_verdict": observed["verdict"],
            "observed_score": observed["score"],
            "match": match,
            "missed_expected_issue_axes": missed_axes,
            "rationale": label.get("rationale"),
            "reference_metrics": _compact_reference_metrics(reference),
            "reference_html_metrics": _compact_reference_html_metrics(reference_html),
            "issues": observed["issues"],
        })

    matched = sum(1 for row in rows if row.get("match"))
    mismatches = [row for row in rows if not row.get("match")]
    positive_mismatches = [
        row for row in rows
        if row.get("label") == "positive" and not row.get("match")
    ]
    negative_mismatches = [
        row for row in rows
        if row.get("label") == "negative" and not row.get("match")
    ]
    near_miss_passes = [
        row for row in rows
        if row.get("label") == "near_miss" and row.get("observed_verdict") == "pass"
    ]
    negative_passes = [
        row for row in rows
        if row.get("label") == "negative" and row.get("observed_verdict") == "pass"
    ]
    missed_axis_count = sum(
        len(row.get("missed_expected_issue_axes") or [])
        for row in rows
    )
    return {
        "kind": "paper_poster_label_calibration",
        "version": 1,
        "label_set_id": label_set.get("id"),
        "description": label_set.get("description"),
        "case_count": len(rows),
        "matched_count": matched,
        "mismatch_count": len(mismatches),
        "accuracy": round(matched / max(1, len(rows)), 3),
        "missed_axis_count": missed_axis_count,
        "positive_mismatch_count": len(positive_mismatches),
        "near_miss_pass_count": len(near_miss_passes),
        "negative_pass_count": len(negative_passes),
        "negative_mismatch_count": len(negative_mismatches),
        "mismatches": [
            {
                "case": row.get("case"),
                "label": row.get("label"),
                "expected_verdict": row.get("expected_verdict"),
                "observed_verdict": row.get("observed_verdict"),
                "missed_expected_issue_axes": row.get("missed_expected_issue_axes"),
            }
            for row in mismatches
        ],
        "rows": rows,
    }


def _label_reference_metrics(
    slug: str,
    discovered_cases: dict[str, PosterCase],
) -> dict[str, Any]:
    case = discovered_cases.get(slug)
    if case is None:
        return {}
    return _reference_metrics_for_case(case)


def _reference_proxy_verdict(
    reference: dict[str, Any] | None,
    *,
    reference_html: dict[str, Any] | None = None,
    label: dict[str, Any] | None = None,
) -> dict[str, Any]:
    score = 1.0
    issues: list[dict[str, Any]] = []

    def issue(issue_id: str, owner: str, severity: str, message: str) -> None:
        nonlocal score
        score -= {
            "blocker": 0.55,
            "high": 0.24,
            "medium": 0.12,
            "low": 0.04,
        }.get(severity, 0.08)
        issues.append({
            "id": issue_id,
            "owner": owner,
            "severity": severity,
            "message": message,
        })

    if not reference and not reference_html:
        issue(
            "reference_poster_missing",
            "harness_reliability",
            "blocker",
            "No poster.png or poster.html/reference.html reference is available for labeled calibration.",
        )
    if reference:
        whitespace = float(reference.get("white_space_ratio") or 0.0)
        edge_density = float(reference.get("edge_density") or 0.0)
        palette = int(reference.get("palette_complexity") or 0)
        grid_mass_cv = float(reference.get("grid_mass_cv") or 0.0)
        saturated = float(reference.get("saturated_pixel_ratio") or 0.0)

        if whitespace >= 0.82:
            issue(
                "reference_whitespace_extreme",
                "layout_storyboard",
                "blocker",
                f"Reference poster is extremely sparse/page-like: whitespace={whitespace:.2f}.",
            )
        elif whitespace >= 0.78:
            issue(
                "reference_whitespace_high",
                "layout_storyboard",
                "high",
                f"Reference poster has high whitespace for dense paper-poster calibration: {whitespace:.2f}.",
            )
        elif whitespace >= 0.72:
            issue(
                "reference_whitespace_borderline",
                "layout_storyboard",
                "medium",
                f"Reference poster is somewhat sparse: whitespace={whitespace:.2f}.",
            )

        if edge_density < 0.15:
            issue(
                "reference_edge_density_very_low",
                "visual_curation",
                "blocker",
                f"Reference poster has very low edge/evidence density: {edge_density:.2f}.",
            )
        elif edge_density < 0.19:
            issue(
                "reference_edge_density_low",
                "visual_curation",
                "high",
                f"Reference poster has low edge/evidence density: {edge_density:.2f}.",
            )

        if palette and palette < 80:
            issue(
                "reference_palette_complexity_low",
                "layout_storyboard",
                "medium",
                f"Reference poster has very low palette complexity: {palette}.",
            )

        if whitespace < 0.52 and edge_density >= 0.23:
            issue(
                "reference_text_heavy_unrefined",
                "typography_system",
                "high",
                (
                    "Reference poster looks over-dense and under-posterized: "
                    f"whitespace={whitespace:.2f}, edge_density={edge_density:.2f}."
                ),
            )

        if edge_density >= 0.23 and saturated < 0.06 and palette <= 180:
            issue(
                "reference_theorem_text_heavy",
                "content_strategy",
                "high",
                (
                    "Reference poster has dense text-like marks without enough "
                    f"visual/color evidence: edge_density={edge_density:.2f}, "
                    f"saturated_pixel_ratio={saturated:.2f}, palette={palette}."
                ),
            )

        if grid_mass_cv >= 0.62 and saturated < 0.03:
            issue(
                "reference_layout_rhythm_weak",
                "layout_storyboard",
                "high",
                (
                    "Reference poster has weak visual rhythm and hierarchy: "
                    f"grid_mass_cv={grid_mass_cv:.2f}, saturated_pixel_ratio={saturated:.2f}."
                ),
            )

    if reference_html:
        profile = str((label or {}).get("reference_profile") or reference_html.get("reference_profile") or "")
        native_units = int(_safe_float_any(reference_html.get("native_information_unit_count"), 0.0))
        panels = int(_safe_float_any(reference_html.get("panel_count"), 0.0))
        headings = int(_safe_float_any(reference_html.get("section_heading_count"), 0.0))
        words = int(_safe_float_any(reference_html.get("visible_text_word_count"), 0.0))
        tables = int(_safe_float_any(reference_html.get("table_count"), 0.0))
        flow_boxes = int(_safe_float_any(reference_html.get("flow_box_count"), 0.0))
        model_cards = int(_safe_float_any(reference_html.get("model_card_like_count"), 0.0))
        if profile == "research_synthesis_dense":
            if native_units < 10:
                issue(
                    "reference_native_information_units_low",
                    "content_strategy",
                    "high",
                    f"Native dense-synthesis reference has too few structured information units: {native_units}.",
                )
            if panels < 6:
                issue(
                    "reference_native_panel_count_low",
                    "layout_storyboard",
                    "high",
                    f"Native dense-synthesis reference has too few panels: {panels}.",
                )
            if headings < 8:
                issue(
                    "reference_native_section_hierarchy_low",
                    "typography_system",
                    "medium",
                    f"Native dense-synthesis reference has weak section hierarchy: {headings} headings.",
                )
            if words < 650:
                issue(
                    "reference_native_word_density_low",
                    "content_strategy",
                    "medium",
                    f"Native dense-synthesis reference has low visible text density: {words} words.",
                )
            if tables < 1:
                issue(
                    "reference_native_table_missing",
                    "content_strategy",
                    "high",
                    "Native dense-synthesis reference lacks benchmark/result table structure.",
                )
            if flow_boxes < 4 and model_cards < 1:
                issue(
                    "reference_native_pipeline_or_model_card_missing",
                    "content_strategy",
                    "high",
                    "Native dense-synthesis reference lacks method pipeline or model-card structure.",
                )

    score = max(0.0, min(1.0, score))
    severities = {str(issue.get("severity") or "") for issue in issues}
    if "blocker" in severities or score < 0.45:
        verdict = "fail"
    elif "high" in severities or score < 0.78:
        verdict = "revise"
    else:
        verdict = "pass"
    return {
        "verdict": verdict,
        "score": round(score, 3),
        "issues": issues,
    }


def _compact_reference_metrics(reference: Any) -> dict[str, Any]:
    if not isinstance(reference, dict):
        return {}
    keys = (
        "width",
        "height",
        "aspect_ratio",
        "orientation",
        "white_space_ratio",
        "nonwhite_pixel_ratio",
        "longest_blank_vertical_run_ratio",
        "vertical_band_nonwhite_min",
        "vertical_band_nonwhite_ratios",
        "dark_ink_ratio",
        "saturated_pixel_ratio",
        "edge_density",
        "palette_complexity",
        "grid_mass_cv",
        "empty_cell_ratio",
    )
    return {key: reference.get(key) for key in keys if reference.get(key) is not None}


def _compact_reference_html_metrics(reference_html: Any) -> dict[str, Any]:
    if not isinstance(reference_html, dict):
        return {}
    keys = (
        "reference_kind",
        "reference_profile",
        "visible_text_word_count",
        "panel_count",
        "grid_row_count",
        "section_heading_count",
        "table_count",
        "table_row_count",
        "table_cell_count",
        "flow_box_count",
        "formula_dom_count",
        "model_card_like_count",
        "pipeline_like_count",
        "native_information_unit_count",
        "highlight_emphasis_count",
    )
    return {key: reference_html.get(key) for key in keys if reference_html.get(key) is not None}


def build_report(
    *,
    data_dir: Path,
    out_dir: Path,
    cases: list[PosterCase],
    eval_set: dict[str, Any],
    targets: dict[str, Any],
    rubric: dict[str, Any],
    results: list[dict[str, Any]],
    label_calibration: dict[str, Any],
    generated: bool,
    template: str,
    harness_mode: str,
    low_confidence: bool,
    low_confidence_cases: list[str],
    batch_layout_diversity: dict[str, Any] | None = None,
) -> str:
    lines: list[str] = [
        "# Poster Quality Eval",
        "",
        f"- Eval set: `{eval_set.get('id') or 'ad hoc'}`",
        f"- Data dir: `{data_dir}`",
        f"- Cases: {len(cases)}",
        f"- Reference posters: {targets.get('reference_count', 0)}",
        f"- Native HTML references: {targets.get('native_reference_count', 0)}",
        f"- Generation template: `{template}`",
        f"- Eval route override: `{harness_mode}`",
    ]
    case_templates = eval_set.get("case_templates") or {}
    if isinstance(case_templates, dict) and case_templates:
        rendered = ", ".join(f"{slug}={tpl}" for slug, tpl in sorted(case_templates.items()))
        lines.append(f"- Case templates: `{rendered}`")
    artifact_role_counts = _candidate_artifact_role_counts(results)
    lines.extend([
        f"- Generated in this run: {'yes' if generated else 'no'}",
        f"- Low confidence: {'yes' if low_confidence else 'no'}",
    ])
    if any(artifact_role_counts.values()):
        lines.extend([
            f"- Final candidates: `{artifact_role_counts['final_candidate_count']}`",
            f"- Diagnostic partials: `{artifact_role_counts['diagnostic_partial_count']}`",
            f"- Failed/no visual: `{artifact_role_counts['failed_no_visual_count']}`",
        ])
    if low_confidence_cases:
        lines.append(f"- Low-confidence cases: `{', '.join(low_confidence_cases)}`")
    lines.extend([
        "",
        "## Reference Targets",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ])
    for key in (
        "orientation_counts",
        "median_aspect_ratio",
        "median_white_space_ratio",
        "median_edge_density",
        "median_grid_mass_cv",
        "median_empty_cell_ratio",
        "median_palette_complexity",
        "native_html",
    ):
        lines.append(f"| {key} | `{json.dumps(targets.get(key), sort_keys=True)}` |")

    traits = list(rubric.get("reference_traits") or [])
    if traits:
        lines.extend(["", "## Reference Traits", ""])
        lines.extend(f"- {trait}" for trait in traits)

    mode_guidance = list(rubric.get("reference_mode_guidance") or [])
    if mode_guidance:
        lines.extend([
            "",
            "## Reference Mode Guidance",
            "",
            "| Mode | Profile | Cases | Expectation |",
            "|---|---|---|---|",
        ])
        for item in mode_guidance:
            if not isinstance(item, dict):
                continue
            mode_cases = ", ".join(str(case) for case in item.get("cases") or [])
            lines.append(
                f"| `{item.get('mode')}` | `{item.get('profile')}` | "
                f"`{mode_cases}` | {item.get('expectation')} |"
            )

    case_profiles = rubric.get("case_reference_profiles")
    if isinstance(case_profiles, dict) and case_profiles:
        lines.extend([
            "",
            "## Case Reference Profiles",
            "",
            "| Case | Profile | Template |",
            "|---|---|---|",
        ])
        rubric_case_templates = (
            rubric.get("case_templates")
            if isinstance(rubric.get("case_templates"), dict)
            else {}
        )
        for slug, profile in sorted(case_profiles.items()):
            lines.append(
                f"| `{slug}` | `{profile}` | `{rubric_case_templates.get(slug) or ''}` |"
            )

    profiles = rubric.get("archetype_target_profiles") if isinstance(rubric.get("archetype_target_profiles"), dict) else {}
    if profiles:
        lines.extend([
            "",
            "## Archetype Target Profiles",
            "",
            "| Profile | Min visual area | Min human effort | Summary |",
            "|---|---:|---:|---|",
        ])
        for profile_id, profile in sorted(profiles.items()):
            if not isinstance(profile, dict):
                continue
            lines.append(
                f"| `{profile_id}` | {profile.get('visual_area_min')} | "
                f"{profile.get('human_effort_min')} | {profile.get('summary')} |"
            )

    if batch_layout_diversity:
        lines.extend([
            "",
            "## Batch Layout Diversity",
            "",
            f"- Status: `{batch_layout_diversity.get('status')}`",
            f"- Candidate signatures: `{batch_layout_diversity.get('candidate_count')}`",
        ])
        archetypes = batch_layout_diversity.get("archetypes") or {}
        hashes = batch_layout_diversity.get("topology_hashes") or {}
        if archetypes:
            lines.extend([
                "",
                "| Case | Archetype | Topology hash |",
                "|---|---|---|",
            ])
            for case, archetype in sorted(archetypes.items()):
                lines.append(f"| `{case}` | `{archetype}` | `{hashes.get(case) or ''}` |")
        repeated = batch_layout_diversity.get("repeated_components") or []
        if repeated:
            lines.extend(["", "Repeated topology groups:"])
            for component in repeated:
                lines.append(f"- `{', '.join(component.get('cases') or [])}`")

    rules = list(rubric.get("acceptance_rules") or [])
    if rules:
        lines.extend([
            "",
            "## Acceptance Rules",
            "",
            "| Rule | Owner | Severity | Expectation |",
            "|---|---|---|---|",
        ])
        for rule in rules:
            lines.append(
                f"| `{rule.get('id')}` | {rule.get('owner')} | "
                f"{rule.get('severity')} | {rule.get('expectation')} |"
            )

    if label_calibration:
        lines.extend([
            "",
            "## Labeled Evaluator Calibration",
            "",
            f"- Label set: `{label_calibration.get('label_set_id')}`",
            f"- Cases: {label_calibration.get('case_count')}",
            f"- Accuracy: `{label_calibration.get('accuracy')}` "
            f"({label_calibration.get('matched_count')} matched / "
            f"{label_calibration.get('mismatch_count')} mismatched)",
            f"- Missed axes: `{label_calibration.get('missed_axis_count')}`",
            f"- Near-miss observed pass count: `{label_calibration.get('near_miss_pass_count')}`",
            "",
            "| Case | Label | Expected | Observed | Score | Match | Notes |",
            "|---|---|---|---|---:|---|---|",
        ])
        for row in label_calibration.get("rows") or []:
            axes = row.get("missed_expected_issue_axes") or []
            issue_ids = [
                str(issue.get("id") or "")
                for issue in (row.get("issues") or [])
                if isinstance(issue, dict) and issue.get("id")
            ]
            notes = (
                "missed axes: " + ", ".join(axes)
                if axes else
                ", ".join(issue_ids[:3])
            )
            lines.append(
                f"| `{row.get('case')}` | {row.get('label')} | "
                f"{row.get('expected_verdict')} | {row.get('observed_verdict')} | "
                f"{row.get('observed_score')} | {'yes' if row.get('match') else 'no'} | "
                f"{notes} |"
            )

    lines.extend([
        "",
        "## Cases",
        "",
        "| Case | Group | Pages | Reference | Candidate | Proxy score | Manual work | Profile |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ])
    for result in results:
        case = result["case"]
        ref = result.get("reference") or {}
        ref_html = result.get("reference_html") or {}
        candidate = result.get("candidate") or {}
        comparison = result.get("comparison") or {}
        content_profile = (
            candidate.get("content_value_profile")
            if isinstance(candidate.get("content_value_profile"), dict)
            else comparison.get("content_value_profile") if isinstance(comparison.get("content_value_profile"), dict)
            else {}
        )
        manual_work = content_profile.get("manual_work_proxy") if isinstance(content_profile.get("manual_work_proxy"), dict) else {}
        ref_cell = (
            f"{ref.get('width')}x{ref.get('height')}, ws {ref.get('white_space_ratio')}"
            if ref else "none"
        )
        if ref_html:
            ref_cell = (
                f"{ref_cell}; html {ref_html.get('panel_count')}p/"
                f"{ref_html.get('visible_text_word_count')}w/"
                f"{ref_html.get('native_information_unit_count')}u"
            )
        cand_img = candidate.get("image") or {}
        generation = candidate.get("generation") or {}
        status = str(generation.get("terminal_status") or "")
        cand_cell = (
            f"{cand_img.get('width')}x{cand_img.get('height')}, ws {cand_img.get('white_space_ratio')}"
            if cand_img else "none"
        )
        if status and status != "pass":
            cand_cell = f"{cand_cell} ({status})"
        role = _candidate_artifact_role(result)
        if role:
            cand_cell = f"{cand_cell} [{role}]"
        score = comparison.get("proxy_score", "")
        lines.append(
            f"| `{case['slug']}` | {case['group']} | {case.get('page_count') or ''} "
            f"| {ref_cell} | {cand_cell} | {score} | "
            f"{manual_work.get('overall', '')} | `{content_profile.get('profile') or ''}` |"
        )

    issue_rows: list[str] = []
    for result in results:
        case = result["case"]
        comparison = result.get("comparison") or {}
        for issue in comparison.get("issues") or []:
            issue_rows.append(
                f"| `{case['slug']}` | `{issue.get('id')}` | {issue.get('owner')} "
                f"| {issue.get('severity')} | {issue.get('message')} |"
            )
    if issue_rows:
        lines.extend([
            "",
            "## Candidate Issues",
            "",
            "| Case | Issue | Owner | Severity | Message |",
            "|---|---|---|---|---|",
            *issue_rows,
        ])

    lines.extend([
        "",
        "## Suggested Loop",
        "",
        "1. Run the offline report first to understand the reference dataset.",
        "2. Generate one or two cases with `--generate`; do not start with the full set.",
        "3. Inspect `contact_sheet.png`, `metrics.json`, and each AutoDesign run log.",
        "4. Classify failures by pipeline owner: designer_contract, visual_curation, layout_storyboard, typography_system, deterministic_env_feedback, critic_rubric, renderer_export, model_routing, or eval_calibration.",
        "5. Patch the highest-frequency owner, run smoke/build checks, then rerun the same cases.",
        "",
        "## Commands",
        "",
        f"Reference rubric JSON: `{out_dir / 'reference_rubric.json'}`",
        f"Generated command list: `{out_dir / 'run_commands.sh'}`",
        "",
    ])
    return "\n".join(lines)


def write_contact_sheet(results: list[dict[str, Any]], out_path: Path) -> None:
    thumb_w = 260
    thumb_h = 190
    label_h = 44
    gap = 18
    cols = 2
    rows = len(results)
    sheet_w = cols * thumb_w + (cols + 1) * gap
    sheet_h = rows * (thumb_h + label_h + gap) + gap
    sheet = Image.new("RGB", (sheet_w, max(sheet_h, 1)), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()

    for row, result in enumerate(results):
        y = gap + row * (thumb_h + label_h + gap)
        case = result["case"]
        for col, kind in enumerate(("reference", "candidate")):
            x = gap + col * (thumb_w + gap)
            img_path = _image_path_for_sheet(result, kind)
            if img_path and img_path.exists():
                with Image.open(img_path) as src:
                    src = ImageOps.exif_transpose(src.convert("RGB"))
                    thumb = ImageOps.contain(src, (thumb_w, thumb_h), Image.Resampling.LANCZOS)
                bx = x + (thumb_w - thumb.width) // 2
                by = y + (thumb_h - thumb.height) // 2
                sheet.paste(thumb, (bx, by))
            else:
                draw.rectangle((x, y, x + thumb_w, y + thumb_h), outline="#cccccc")
                draw.text((x + 10, y + 80), "missing", fill="#777777", font=font)
            label_kind = kind
            if kind == "candidate":
                role = _candidate_artifact_role(result)
                if role:
                    label_kind = f"{kind} {role}"
            label = f"{case['slug']} / {label_kind}"
            draw.text((x, y + thumb_h + 8), label[:42], fill="#111111", font=font)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path)


def _image_path_for_sheet(result: dict[str, Any], kind: str) -> Path | None:
    if kind == "reference":
        ref = result.get("reference") or {}
        return Path(ref["path"]) if ref.get("path") else None
    candidate = result.get("candidate") or {}
    image = candidate.get("image") or {}
    return Path(image["path"]) if image.get("path") else None


def _candidate_artifact_role(result: dict[str, Any]) -> str:
    candidate = result.get("candidate") if isinstance(result.get("candidate"), dict) else {}
    if not candidate:
        return ""
    comparison = result.get("comparison") if isinstance(result.get("comparison"), dict) else {}
    issues = comparison.get("issues") if isinstance(comparison.get("issues"), list) else []
    has_blocker = any(
        isinstance(issue, dict) and str(issue.get("severity") or "") == "blocker"
        for issue in issues
    )
    generation = candidate.get("generation") if isinstance(candidate.get("generation"), dict) else {}
    terminal_status = generation.get("terminal_status")
    has_visual = bool(candidate.get("image") or candidate.get("html"))
    if (
        generation.get("finalized") is True
        and terminal_status in {"pass", None, ""}
        and not has_blocker
    ):
        return "final_candidate"
    return "diagnostic_partial" if has_visual else "failed_no_visual"


def _candidate_artifact_role_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "final_candidate_count": 0,
        "diagnostic_partial_count": 0,
        "failed_no_visual_count": 0,
    }
    for result in results:
        role = _candidate_artifact_role(result)
        if role == "final_candidate":
            counts["final_candidate_count"] += 1
        elif role == "diagnostic_partial":
            counts["diagnostic_partial_count"] += 1
        elif role == "failed_no_visual":
            counts["failed_no_visual_count"] += 1
    return counts


def write_run_commands(
    cases: list[PosterCase],
    data_dir: Path,
    out_dir: Path,
    template: str,
    brief: str,
    eval_set: dict[str, Any],
    override_template: str | None = None,
) -> None:
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        "# Run one command at a time if you are watching API spend.",
        "",
    ]
    for case in cases:
        set_arg = ""
        if eval_set.get("id"):
            set_arg = f"--set {_shell_quote(str(eval_set['id']))} "
        case_template = _template_for_case(
            case,
            eval_set=eval_set,
            override_template=override_template,
            default_template=template,
        )
        lines.append(
            "uv run python scripts/poster_quality_eval.py "
            f"--data-dir {_shell_quote(str(data_dir))} "
            f"--out-dir {_shell_quote(str(out_dir / case.slug))} "
            f"{set_arg}"
            f"--case {_shell_quote(case.slug)} "
            "--generate "
            f"--template {_shell_quote(case_template)} "
            f"--brief {_shell_quote(brief)}"
        )
    path = out_dir / "run_commands.sh"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)


if __name__ == "__main__":
    raise SystemExit(main())
