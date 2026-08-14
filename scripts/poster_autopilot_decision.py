"""Turn poster harness metrics into a concrete next engineering patch target.

The poster quality loop is intentionally report-only. This script is the next
layer above it: it reads one iteration's metrics, compares the failures against
the gold-reference contract, and emits a small decision record for the next
Codex patch. It still does not edit the repository.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from autodesign.util.io import atomic_write_json, ensure_dirs


GOLD_SPEC_PATH = (
    _REPO_ROOT
    / "autodesign"
    / "evaluator"
    / "assets"
    / "poster_gold_reference_specs.json"
)
DEFAULT_CASES = ("arxiv2026_longcat_next", "vit", "iclr2024_lcbm")

OWNER_PRIORITY = {
    "harness_reliability": 0,
    "designer_contract": 1,
    "layout_storyboard": 2,
    "content_strategy": 3,
    "renderer_export": 4,
    "visual_curation": 5,
    "typography_system": 6,
    "eval_calibration": 7,
}
SEVERITY_WEIGHT = {
    "blocker": 5.0,
    "p0": 5.0,
    "high": 3.0,
    "medium": 1.5,
    "low": 0.5,
}
TARGET_FILES = {
    "harness_reliability": [
        "autodesign/tools/propose_paper_poster_html.py",
        "autodesign/util/design_feedback.py",
        "scripts/poster_quality_eval.py",
        "scripts/poster_quality_loop.py",
    ],
    "designer_contract": [
        "prompts/designer.md",
        "skills/poster/visual_recipe/SKILL.md",
        "autodesign/runner.py",
        "autodesign/tools/propose_paper_poster_html.py",
    ],
    "layout_storyboard": [
        "autodesign/util/poster_plan_contract.py",
        "autodesign/tools/propose_paper_poster_html.py",
        "prompts/designer.md",
        "skills/poster/visual_recipe/SKILL.md",
    ],
    "content_strategy": [
        "autodesign/tools/ingest_document.py",
        "autodesign/util/poster_plan_contract.py",
        "skills/poster/visual_recipe/assets/dense_synthesis_targets.json",
        "prompts/designer.md",
    ],
    "renderer_export": [
        "autodesign/tools/propose_paper_poster_html.py",
        "autodesign/tools/html_artifact_renderer.py",
        "autodesign/tools/composite.py",
    ],
    "visual_curation": [
        "autodesign/tools/ingest_document.py",
        "autodesign/util/poster_plan_contract.py",
        "scripts/poster_quality_eval.py",
    ],
    "typography_system": [
        "autodesign/tools/propose_paper_poster_html.py",
        "prompts/designer.md",
        "skills/poster/visual_recipe/SKILL.md",
    ],
    "eval_calibration": [
        "scripts/poster_quality_eval.py",
        "eval/poster_quality_sets.json",
        "autodesign/evaluator/assets/poster_gold_reference_specs.json",
    ],
}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    metrics_path = _resolve_metrics_path(args)
    metrics = _read_json(metrics_path)
    if not isinstance(metrics, dict):
        raise SystemExit(f"metrics file is missing or invalid: {metrics_path}")
    gold_spec = load_gold_reference_spec(Path(args.gold_spec).expanduser())
    decision = build_autopilot_decision(
        metrics,
        gold_spec,
        iteration_dir=Path(args.iteration_dir).expanduser().resolve() if args.iteration_dir else None,
        selected_cases=tuple(args.case or ()),
    )
    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else metrics_path.parent
    ensure_dirs(out_dir)
    prefix = args.write_prefix
    atomic_write_json(out_dir / f"{prefix}.json", decision)
    (out_dir / f"{prefix}.md").write_text(render_autopilot_decision(decision), encoding="utf-8")
    print(f"autopilot decision: {out_dir / f'{prefix}.json'}")
    print(f"primary target: {decision.get('primary_target')}")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics", default=None, help="Path to poster_quality_loop/eval metrics.json.")
    parser.add_argument("--iteration-dir", default=None, help="Iteration directory containing metrics.json.")
    parser.add_argument("--out-dir", default=None, help="Where to write autopilot_decision.{json,md}.")
    parser.add_argument("--gold-spec", default=str(GOLD_SPEC_PATH))
    parser.add_argument("--write-prefix", default="autopilot_decision")
    parser.add_argument("--case", action="append", default=[], help="Restrict decision to these case slugs.")
    return parser.parse_args(argv)


def _resolve_metrics_path(args: argparse.Namespace) -> Path:
    if args.metrics:
        return Path(args.metrics).expanduser().resolve()
    if args.iteration_dir:
        return (Path(args.iteration_dir).expanduser().resolve() / "metrics.json")
    raise SystemExit("provide --metrics or --iteration-dir")


def load_gold_reference_spec(path: Path = GOLD_SPEC_PATH) -> dict[str, Any]:
    payload = _read_json(path)
    if isinstance(payload, dict) and payload:
        return payload
    return {
        "version": 0,
        "primary_gold_policy": {"human_references_are_primary": True},
        "primary_gold_references": {},
        "secondary_visual_anchors": {},
        "shared_generation_contract": {},
    }


def build_autopilot_decision(
    metrics: dict[str, Any],
    gold_spec: dict[str, Any],
    *,
    iteration_dir: Path | None = None,
    selected_cases: tuple[str, ...] = (),
) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    owner_scores: dict[str, float] = defaultdict(float)
    all_issue_ids: list[str] = []
    selected = {case for case in selected_cases if case}

    for result in metrics.get("results") or []:
        if not isinstance(result, dict):
            continue
        slug = _case_slug(result)
        if selected and slug not in selected:
            continue
        diag = diagnose_case(result, gold_spec)
        cases.append(diag)
        for issue_id in diag.get("issue_ids") or []:
            all_issue_ids.append(str(issue_id))
        for root in diag.get("root_causes") or []:
            owner = str(root.get("owner") or "")
            if not owner:
                continue
            weight = float(root.get("weight") or 1.0)
            owner_scores[owner] += weight

    if not cases:
        owner_scores["harness_reliability"] += 5.0
        cases.append({
            "case_slug": "none",
            "status": "no_cases",
            "root_causes": [{
                "owner": "harness_reliability",
                "reason": "No metrics results were available for the requested cases.",
                "weight": 5.0,
            }],
            "issue_ids": [],
        })

    mode_collapse = _mode_collapse_signal(cases)
    if mode_collapse:
        owner_scores["layout_storyboard"] += 3.0
        owner_scores["designer_contract"] += 2.0

    primary_target = _choose_primary_target(owner_scores)
    secondary_targets = [
        owner for owner, _score in sorted(
            owner_scores.items(),
            key=lambda item: (-item[1], OWNER_PRIORITY.get(item[0], 99), item[0]),
        )
        if owner != primary_target and owner in TARGET_FILES
    ][:3]
    blockers = [
        cause
        for case in cases
        for cause in (case.get("root_causes") or [])
        if float(cause.get("weight") or 0.0) >= 3.0
    ]
    accepted = not blockers and not any(case.get("status") != "pass" for case in cases)
    acceptance_reason = "passes primary gold density and contract floors" if accepted else _acceptance_reason(cases)
    decision = {
        "decision_kind": "poster_autopilot_next_patch",
        "version": 1,
        "iteration_dir": str(iteration_dir) if iteration_dir else None,
        "primary_target": primary_target,
        "secondary_targets": secondary_targets,
        "owner_scores": dict(sorted(owner_scores.items(), key=lambda item: (-item[1], item[0]))),
        "acceptance": {
            "accept": accepted,
            "reason": acceptance_reason,
        },
        "mode_collapse_signal": mode_collapse,
        "gold_policy": {
            "primary_gold_set": gold_spec.get("primary_gold_set"),
            "human_refs_primary": bool(
                (gold_spec.get("primary_gold_policy") or {}).get("human_references_are_primary")
            ),
            "iteration51_role": "secondary_visual_anchor_only",
        },
        "cases": cases,
        "next_actions": _next_actions(primary_target, secondary_targets),
        "target_files": TARGET_FILES.get(primary_target, []),
        "all_issue_ids": sorted(set(all_issue_ids)),
    }
    return decision


def diagnose_case(result: dict[str, Any], gold_spec: dict[str, Any]) -> dict[str, Any]:
    slug = _case_slug(result)
    comparison = result.get("comparison") if isinstance(result.get("comparison"), dict) else {}
    candidate = result.get("candidate") if isinstance(result.get("candidate"), dict) else {}
    html = candidate.get("html") if isinstance(candidate.get("html"), dict) else {}
    image = candidate.get("image") if isinstance(candidate.get("image"), dict) else {}
    paper = candidate.get("paper_poster") if isinstance(candidate.get("paper_poster"), dict) else {}
    generation = candidate.get("generation") if isinstance(candidate.get("generation"), dict) else {}
    issues = [item for item in (comparison.get("issues") or []) if isinstance(item, dict)]
    root_causes: list[dict[str, Any]] = []
    issue_ids: list[str] = []

    for issue in issues:
        issue_id = str(issue.get("id") or "")
        issue_ids.append(issue_id)
        classified = _classify_issue(issue)
        if classified:
            owner, reason, base_weight = classified
            severity = str(issue.get("severity") or "").lower()
            root_causes.append({
                "owner": owner,
                "reason": reason,
                "issue_id": issue_id,
                "weight": max(base_weight, SEVERITY_WEIGHT.get(severity, 1.0)),
            })

    terminal = str(generation.get("terminal_status") or candidate.get("terminal_status") or "").lower()
    preview_path = str(
        candidate.get("preview_path")
        or paper.get("final_preview_path")
        or candidate.get("path")
        or ""
    )
    usable_authored = _candidate_has_usable_authored_poster(candidate, paper)
    if (
        not candidate
        or (
            terminal in {"failed", "error", "max_turns", "timeout"}
            and not usable_authored
        )
        or (terminal and not preview_path and not usable_authored)
    ):
        root_causes.append({
            "owner": "harness_reliability",
            "reason": f"generation did not produce a usable authored poster ({terminal or 'missing candidate'})",
            "weight": 5.0,
        })
    if _deterministic_recovery_detected(candidate, comparison):
        root_causes.append({
            "owner": "harness_reliability",
            "reason": "deterministic recovery/fallback content appeared in a dogfood run",
            "weight": 5.0,
        })

    root_causes.extend(_paper_density_root_causes(slug, html, image, paper, gold_spec))
    root_causes.extend(_style_root_causes(html, image, paper))
    status = "pass" if not root_causes else _case_status(root_causes)
    return {
        "case_slug": slug,
        "status": status,
        "terminal_status": terminal or None,
        "usable_authored_poster": usable_authored,
        "preview_path": preview_path or None,
        "metrics": _compact_metrics(html, image, paper),
        "root_causes": _dedupe_root_causes(root_causes),
        "issue_ids": sorted(set(issue_ids)),
    }


def _candidate_has_usable_authored_poster(
    candidate: dict[str, Any],
    paper: dict[str, Any],
) -> bool:
    """Return true when a timeout/error still left a valid authored poster artifact."""
    if not isinstance(candidate, dict) or not candidate:
        return False
    html = candidate.get("html") if isinstance(candidate.get("html"), dict) else {}
    final_is_authored = bool(
        paper.get("final_is_authored_html")
        or paper.get("authored_html")
        or html.get("authored_html")
    )
    if not final_is_authored:
        return False
    has_preview = bool(
        candidate.get("preview_path")
        or paper.get("final_preview_path")
        or candidate.get("path")
    )
    has_html = bool(candidate.get("html_path") or paper.get("final_html_path"))
    if not has_preview or not has_html:
        return False
    if paper.get("artifact_consistency_ok") is False:
        return False
    if int(_num(paper.get("image_not_loaded_count"))) > 0:
        return False
    return True


def _paper_density_root_causes(
    slug: str,
    html: dict[str, Any],
    image: dict[str, Any],
    paper: dict[str, Any],
    gold_spec: dict[str, Any],
) -> list[dict[str, Any]]:
    causes: list[dict[str, Any]] = []
    gold_floor = _gold_floor_for_case(slug, gold_spec)
    gold_visual_fail = int(_num(paper.get("gold_visual_density_regression_count")))
    gold_quality_fail = int(_num(paper.get("gold_quality_floor_regression_count")))
    gold_composite_fail = int(_num(paper.get("gold_composite_fail_count")))
    if gold_visual_fail > 0 or gold_quality_fail > 0 or gold_composite_fail > 0:
        causes.append({
            "owner": "layout_storyboard",
            "reason": (
                "candidate regressed below the primary gold density/quality floor; "
                "P0 reductions do not count as progress if visual density drops"
            ),
            "weight": 4.5,
        })

    leaf_words = int(_num(
        paper.get("leaf_visible_word_count")
        if paper.get("leaf_visible_word_count") is not None
        else html.get("leaf_visible_word_count", html.get("authored_leaf_visible_word_count"))
    ))
    min_words = int(_num(gold_floor.get("min_leaf_visible_words"))) or 950
    if leaf_words and leaf_words < min_words:
        causes.append({
            "owner": "content_strategy",
            "reason": f"leaf visible words are too low ({leaf_words} < {min_words})",
            "weight": 3.5,
        })
    elif not leaf_words:
        causes.append({
            "owner": "content_strategy",
            "reason": "leaf visible word count is missing or zero, so panel fill cannot be trusted",
            "weight": 3.0,
        })

    native_units = int(_num(
        paper.get("native_information_unit_count")
        if paper.get("native_information_unit_count") is not None
        else html.get("authored_native_information_unit_count", html.get("native_information_unit_count"))
    ))
    min_units = int(_num(gold_floor.get("min_native_information_units"))) or 9
    if native_units and native_units < min_units:
        causes.append({
            "owner": "content_strategy",
            "reason": f"native information units are too low ({native_units} < {min_units})",
            "weight": 3.0,
        })

    nonwhite = _num(image.get("nonwhite_pixel_ratio", image.get("ink_ratio")))
    min_nonwhite = _num(gold_floor.get("min_nonwhite_pixel_ratio")) or 0.23
    if nonwhite and nonwhite < min_nonwhite:
        causes.append({
            "owner": "layout_storyboard",
            "reason": f"non-white pixel ratio is below the gold floor ({nonwhite:.3f} < {min_nonwhite:.3f})",
            "weight": 4.0,
        })
    blank_run = _num(
        image.get("longest_blank_vertical_run_ratio")
        if image.get("longest_blank_vertical_run_ratio") is not None
        else paper.get("longest_blank_vertical_run_ratio")
    )
    max_blank = _num(gold_floor.get("max_longest_blank_vertical_run_ratio")) or 0.08
    if blank_run and blank_run > max_blank:
        causes.append({
            "owner": "layout_storyboard",
            "reason": f"poster has a long blank vertical band ({blank_run:.3f} > {max_blank:.3f})",
            "weight": 4.0,
        })

    figure_area = _num(
        paper.get("figure_area_ratio")
        if paper.get("figure_area_ratio") is not None
        else html.get("visual_area_ratio")
    )
    min_figure = _num(gold_floor.get("min_source_figure_area_ratio")) or 0.08
    if figure_area and figure_area < min_figure and native_units < max(min_units, 12):
        causes.append({
            "owner": "visual_curation",
            "reason": (
                f"source visual area is low ({figure_area:.3f} < {min_figure:.3f}) "
                "without enough native reconstruction to compensate"
            ),
            "weight": 3.0,
        })

    authored_autofill = int(_num(html.get("contract_autofill_block_count")))
    if authored_autofill:
        causes.append({
            "owner": "designer_contract",
            "reason": (
                "contract supplement blocks are present; dogfood may render them "
                "for diagnosis, but they do not count as designer-authored density"
            ),
            "weight": 4.0,
        })

    dom_p0 = int(_num(paper.get("dom_p0_count")))
    if dom_p0:
        causes.append({
            "owner": "renderer_export",
            "reason": (
                "DOM P0s remain. Dense local overflow should be repaired locally, "
                "not by making the whole poster sparse"
            ),
            "weight": 3.0,
        })
    return causes


def _style_root_causes(html: dict[str, Any], image: dict[str, Any], paper: dict[str, Any]) -> list[dict[str, Any]]:
    causes: list[dict[str, Any]] = []
    palette_complexity = int(_num(image.get("palette_complexity")))
    if palette_complexity > 0 and palette_complexity > 220:
        causes.append({
            "owner": "typography_system",
            "reason": f"palette complexity is high ({palette_complexity}); enforce the academic token palette",
            "weight": 2.0,
        })
    palette_bad = int(_num(html.get("off_palette_color_count", paper.get("off_palette_color_count"))))
    if palette_bad:
        causes.append({
            "owner": "typography_system",
            "reason": f"off-palette colors found ({palette_bad}); remove per-panel colorful styling",
            "weight": 3.0,
        })
    times_ratio = _num(html.get("times_new_roman_ratio", paper.get("times_new_roman_ratio")))
    if times_ratio and times_ratio < 0.95:
        causes.append({
            "owner": "typography_system",
            "reason": f"Times New Roman usage is incomplete ({times_ratio:.2f})",
            "weight": 3.0,
        })
    return causes


def _classify_issue(issue: dict[str, Any]) -> tuple[str, str, float] | None:
    issue_id = str(issue.get("id") or "").lower()
    message = str(issue.get("message") or "").lower()
    owner = str(issue.get("owner") or "").strip()
    text = f"{issue_id} {message}"
    if owner in TARGET_FILES:
        return owner, str(issue.get("message") or issue_id or owner), 1.0
    if "contract_autofill" in text or "contract autofill" in text or "contract supplement" in text:
        return "designer_contract", "designer relied on diagnostic contract supplement instead of authored slot content", 4.0
    if any(token in text for token in ("deterministic", "fallback", "recovery")):
        return "harness_reliability", "deterministic fallback/recovery leaked into the output", 5.0
    if any(token in text for token in ("missing_authored_slot", "designer_contract", "data-slot-id")):
        return "designer_contract", "designer failed the authored HTML slot contract", 4.0
    if any(token in text for token in ("spatial_fill", "blank_run", "grid_coverage", "density", "empty", "underfilled")):
        return "layout_storyboard", "panel occupancy or visual density is below the gold target", 3.5
    if any(token in text for token in ("leaf_visible_words", "native", "information", "words_low")):
        return "content_strategy", "poster content is not filling slots with enough native information", 3.0
    if any(token in text for token in ("source_visual", "source figure", "visual_missing", "caption crop", "body text crop")):
        return "visual_curation", "source evidence selection or binding is wrong", 3.0
    if any(token in text for token in ("overflow", "overlap", "bbox", "clip", "text-fit")):
        return "renderer_export", "compiled text geometry does not realize authored lanes cleanly", 3.0
    if any(token in text for token in ("palette", "times", "font", "colorful", "color")):
        return "typography_system", "academic typography or palette contract failed", 2.5
    return None


def _deterministic_recovery_detected(candidate: dict[str, Any], comparison: dict[str, Any]) -> bool:
    html = candidate.get("html") if isinstance(candidate.get("html"), dict) else {}
    generation = candidate.get("generation") if isinstance(candidate.get("generation"), dict) else {}
    generation_blob = json.dumps(generation, sort_keys=True).lower()
    comparison_blob = json.dumps(comparison, sort_keys=True).lower()
    return any(token in generation_blob or token in comparison_blob for token in (
        "deterministic recovery",
        "deterministic_recovery",
        "dense_recovery",
        "spec_recovery",
    ))


def _gold_floor_for_case(slug: str, gold_spec: dict[str, Any]) -> dict[str, Any]:
    refs = gold_spec.get("primary_gold_references") if isinstance(gold_spec.get("primary_gold_references"), dict) else {}
    if slug in refs and isinstance(refs[slug], dict):
        floor = refs[slug].get("floor")
        return floor if isinstance(floor, dict) else {}
    floors = [
        ref.get("floor")
        for ref in refs.values()
        if isinstance(ref, dict) and isinstance(ref.get("floor"), dict)
    ]
    if not floors:
        return {}
    return {
        "min_nonwhite_pixel_ratio": min(_num(f.get("min_nonwhite_pixel_ratio")) for f in floors),
        "max_longest_blank_vertical_run_ratio": max(_num(f.get("max_longest_blank_vertical_run_ratio")) for f in floors),
        "min_leaf_visible_words": min(_num(f.get("min_leaf_visible_words")) for f in floors),
        "min_native_information_units": min(_num(f.get("min_native_information_units")) for f in floors),
        "min_source_figure_area_ratio": min(_num(f.get("min_source_figure_area_ratio")) for f in floors),
    }


def _case_status(root_causes: list[dict[str, Any]]) -> str:
    if not any(float(cause.get("weight") or 0.0) >= 3.0 for cause in root_causes):
        return "pass"
    owners = {str(cause.get("owner") or "") for cause in root_causes}
    if "harness_reliability" in owners:
        return "generation_contract_failed"
    if "layout_storyboard" in owners or "content_strategy" in owners:
        return "density_regression"
    if "renderer_export" in owners:
        return "local_dom_repair"
    return "needs_repair"


def _mode_collapse_signal(cases: list[dict[str, Any]]) -> dict[str, Any] | None:
    statuses = [str(case.get("status") or "") for case in cases]
    if len(cases) < 2:
        return None
    density_regressions = sum(1 for status in statuses if status == "density_regression")
    deterministic = sum(1 for status in statuses if status == "generation_contract_failed")
    if density_regressions >= 2:
        return {
            "kind": "sparse_layout_convergence",
            "case_count": len(cases),
            "affected_count": density_regressions,
            "message": "multiple cases converged to underfilled panels; patch positive dense skeleton/lane constraints",
        }
    if deterministic >= 2:
        return {
            "kind": "recovery_convergence",
            "case_count": len(cases),
            "affected_count": deterministic,
            "message": "multiple cases hit recovery/contract failure; patch planner contract reliability before styling",
        }
    return None


def _choose_primary_target(owner_scores: dict[str, float]) -> str:
    if not owner_scores:
        return "layout_storyboard"
    return sorted(
        owner_scores.items(),
        key=lambda item: (-item[1], OWNER_PRIORITY.get(item[0], 99), item[0]),
    )[0][0]


def _acceptance_reason(cases: list[dict[str, Any]]) -> str:
    failing = [case for case in cases if case.get("status") != "pass"]
    if not failing:
        return "not accepted due to residual blockers"
    first = failing[0]
    cause = (first.get("root_causes") or [{}])[0]
    return f"{first.get('case_slug')} failed: {cause.get('reason') or first.get('status')}"


def _next_actions(primary: str, secondary: list[str]) -> list[str]:
    actions = {
        "harness_reliability": [
            "Keep deterministic recovery disabled; allow only capped diagnostic supplements and surface their word share as planner contract debt.",
            "Add an explicit no-recovery check to the next generated iteration before judging visual quality.",
        ],
        "designer_contract": [
            "Strengthen authored HTML contract examples: every slot root plus block-local child lanes with absolute geometry.",
            "Reject hollow panels before render so the planner has to fill text/native units/source bindings itself.",
        ],
        "layout_storyboard": [
            "Compile the handmade references into a positive dense skeleton: high band occupancy, filled panels, narrow gutters, and no blank lower halves.",
            "Treat iteration51 only as a dense silhouette anchor and repair local overflow without deleting density.",
        ],
        "content_strategy": [
            "Raise sourced leaf words and native information units per panel before touching cosmetic spacing.",
            "Convert paper claims into tables, model cards, method pipelines, short result discussion, formulas, and ablation or limitation notes.",
        ],
        "renderer_export": [
            "Repair local text fitting and compiled bbox realization while preserving density floors.",
            "Prefer local shrink/split lanes over global whitespace expansion.",
        ],
        "visual_curation": [
            "Use source visuals only when they are real figures/tables and bind them to nearby explanatory text.",
            "Reject page/body-text crops as figure panels; reconstruct them as native tables or text units.",
        ],
        "typography_system": [
            "Normalize all poster text to Times New Roman with a role-based size/weight ladder.",
            "Clamp colors to the academic token palette and remove per-panel colorful backgrounds.",
        ],
        "eval_calibration": [
            "Adjust harness thresholds only when visual inspection shows the metric is rewarding the wrong behavior.",
            "Keep human references primary and iteration51 secondary in every scoring path.",
        ],
    }
    out = list(actions.get(primary, []))
    for owner in secondary:
        if owner in actions and actions[owner]:
            out.append(actions[owner][0])
    return out[:5]


def render_autopilot_decision(decision: dict[str, Any]) -> str:
    lines = [
        "# Poster Autopilot Decision",
        "",
        f"- Acceptance: {decision.get('acceptance', {}).get('accept')} - {decision.get('acceptance', {}).get('reason')}",
        f"- Primary target: `{decision.get('primary_target')}`",
        f"- Secondary targets: {', '.join(f'`{item}`' for item in decision.get('secondary_targets') or []) or 'none'}",
        "",
        "## Case Findings",
    ]
    for case in decision.get("cases") or []:
        lines.append(f"- `{case.get('case_slug')}`: {case.get('status')}")
        for cause in (case.get("root_causes") or [])[:4]:
            lines.append(f"  - {cause.get('owner')}: {cause.get('reason')}")
    lines.extend(["", "## Next Actions"])
    for action in decision.get("next_actions") or []:
        lines.append(f"- {action}")
    lines.extend(["", "## Target Files"])
    for path in decision.get("target_files") or []:
        lines.append(f"- `{path}`")
    lines.append("")
    return "\n".join(lines)


def _compact_metrics(html: dict[str, Any], image: dict[str, Any], paper: dict[str, Any]) -> dict[str, Any]:
    return {
        "leaf_visible_words": paper.get("leaf_visible_word_count", html.get("leaf_visible_word_count")),
        "native_information_units": paper.get(
            "native_information_unit_count",
            html.get("authored_native_information_unit_count"),
        ),
        "nonwhite_pixel_ratio": image.get("nonwhite_pixel_ratio", image.get("ink_ratio")),
        "longest_blank_vertical_run_ratio": image.get(
            "longest_blank_vertical_run_ratio",
            paper.get("longest_blank_vertical_run_ratio"),
        ),
        "source_figure_area_ratio": paper.get("figure_area_ratio", html.get("visual_area_ratio")),
        "dom_p0_count": paper.get("dom_p0_count"),
        "gold_visual_density_regression_count": paper.get("gold_visual_density_regression_count"),
        "gold_quality_floor_regression_count": paper.get("gold_quality_floor_regression_count"),
        "gold_composite_fail_count": paper.get("gold_composite_fail_count"),
    }


def _dedupe_root_causes(causes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for cause in sorted(
        causes,
        key=lambda item: (-float(item.get("weight") or 0.0), OWNER_PRIORITY.get(str(item.get("owner") or ""), 99)),
    ):
        key = (str(cause.get("owner") or ""), str(cause.get("reason") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(cause)
    return out[:8]


def _case_slug(result: dict[str, Any]) -> str:
    case = result.get("case") if isinstance(result.get("case"), dict) else {}
    return str(case.get("slug") or result.get("case_slug") or "unknown")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"could not parse {path}: {exc}") from exc


def _num(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return 0.0
    try:
        return float(match.group(0))
    except ValueError:
        return 0.0


if __name__ == "__main__":
    raise SystemExit(main())
