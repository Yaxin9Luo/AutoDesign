"""Panel-level polish helpers for final authored paper posters.

The polish loop is intentionally conservative: acceptance is based on small
panel-scoped diffs plus hard render/DOM non-regression checks.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import io
import json
import re
from pathlib import Path
from typing import Any

from ..run_control import CancellationToken

from bs4 import BeautifulSoup, Tag

from .io import atomic_write_json


PANEL_SELECTOR = ".slot,.panel,[data-panel-role],[data-slot-id]"


@dataclass
class PanelPolishTarget:
    panel_id: str
    label: str
    role: str = ""
    bbox: dict[str, float] = field(default_factory=dict)
    word_count: int = 0
    image_count: int = 0
    table_count: int = 0
    source_visuals: list[dict[str, Any]] = field(default_factory=list)
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    crop_path: str | None = None
    visual_triage: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        payload = {
            "panel_id": self.panel_id,
            "label": self.label,
            "role": self.role,
            "bbox": self.bbox,
            "word_count": self.word_count,
            "image_count": self.image_count,
            "table_count": self.table_count,
            "source_visuals": list(self.source_visuals),
            "score": round(float(self.score), 3),
            "reasons": list(self.reasons),
            "crop_path": self.crop_path,
        }
        if self.visual_triage:
            payload["visual_triage"] = dict(self.visual_triage)
        return payload


@dataclass
class _VisionAttachment:
    label: str
    image_b64: str
    media_type: str


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def build_panel_polish_context(
    *,
    run_dir: Path,
    final_dir: Path,
    work_dir: Path,
    max_panels: int = 2,
    allow_fallback: bool = False,
    selector: str = "dom",
    settings: Any | None = None,
    exclude_panel_ids: set[str] | None = None,
    patch_mode: str = "auto",
    cancellation_token: CancellationToken | None = None,
) -> dict[str, Any]:
    """Create panel crops and a compact issue manifest for Codex."""
    _raise_if_cancelled(cancellation_token, "panel_polish.context.start")
    final_dir = final_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    html_path = final_dir / "poster.html"
    preview_path = final_dir / "preview.png"
    dom_audit_path = final_dir / "paper_poster_dom_audit.json"
    dom_audit = load_json(dom_audit_path)
    selector = (selector or "dom").strip().lower()
    if selector not in {"dom", "vlm"}:
        selector = "dom"
    patch_mode = (patch_mode or "auto").strip().lower().replace("_", "-")
    if patch_mode not in {"auto", "text-only"}:
        patch_mode = "auto"
    visual_triage: dict[str, Any] = {}
    contact_sheet_path: str | None = None
    max_selected = max(1, int(max_panels or 1))
    excluded_ids = {_norm_id(str(item)) for item in (exclude_panel_ids or set()) if str(item).strip()}
    if selector == "vlm":
        candidate_panels = _exclude_panels(
            _top_level_panel_records(_panel_records_from_dom_layers(dom_audit)),
            excluded_ids,
        )
        _annotate_panel_issue_scores(candidate_panels, dom_audit)
        _write_panel_crops(
            preview_path=preview_path,
            dom_audit=dom_audit,
            panels=candidate_panels,
            out_dir=work_dir / "panel_crops",
        )
        contact = _write_panel_contact_sheet(
            candidate_panels,
            out_path=work_dir / "panel_contact_sheet.png",
        )
        contact_sheet_path = str(contact) if contact is not None else None
        visual_triage = run_panel_visual_triage(
            settings=settings,
            preview_path=preview_path,
            contact_sheet_path=contact,
            panels=candidate_panels,
            max_panels=max_selected,
            work_dir=work_dir,
            cancellation_token=cancellation_token,
        )
        panels = _select_vlm_triaged_panels(
            candidate_panels,
            visual_triage=visual_triage,
            max_panels=max_selected,
        )
        if not panels and allow_fallback:
            panels = _exclude_panels(
                select_panel_polish_targets(
                    dom_audit,
                    max_panels=max_selected,
                    allow_fallback=True,
                ),
                excluded_ids,
            )
            _write_panel_crops(
                preview_path=preview_path,
                dom_audit=dom_audit,
                panels=panels,
                out_dir=work_dir / "panel_crops",
            )
    else:
        panels = _exclude_panels(
            select_panel_polish_targets(
                dom_audit,
                max_panels=max_selected,
                allow_fallback=allow_fallback,
            ),
            excluded_ids,
        )
        _write_panel_crops(
            preview_path=preview_path,
            dom_audit=dom_audit,
            panels=panels,
            out_dir=work_dir / "panel_crops",
        )
    context = {
        "kind": "paper_poster_panel_polish_context",
        "run_dir": str(run_dir),
        "final_dir": str(final_dir),
        "source_html": str(html_path),
        "source_preview": str(preview_path),
        "source_dom_audit": str(dom_audit_path),
        "selector": selector,
        "patch_mode": patch_mode,
        "excluded_panel_ids": sorted(excluded_ids),
        "panel_contact_sheet": contact_sheet_path,
        "visual_triage": visual_triage,
        "selected_panels": [panel.to_json() for panel in panels],
        "global_metrics": _audit_metrics(dom_audit),
        "hard_rules": [
            "Codex may inspect the whole poster and all files.",
            "Default accepted edits should touch only selected panel internals plus small shared CSS.",
            "Do not change canvas size, paper-poster root contract, or rewrite the whole poster.",
            "All added paper facts must be grounded in paper memory/dossier/source evidence.",
            "If a global re-layout is required, write needs_global_refine=true in panel_polish_done.json.",
        ],
    }
    atomic_write_json(work_dir / "panel_polish_context.json", context)
    (work_dir / "panel_polish_prompt.md").write_text(
        build_panel_polish_prompt(context),
        encoding="utf-8",
    )
    return context


def select_panel_polish_targets(
    dom_audit: dict[str, Any],
    *,
    max_panels: int = 2,
    allow_fallback: bool = False,
) -> list[PanelPolishTarget]:
    panels = _panel_records_from_dom_layers(dom_audit)
    _annotate_panel_issue_scores(panels, dom_audit)
    ranked = [panel for panel in panels if panel.score > 0]
    if not ranked and allow_fallback:
        ranked = sorted(
            panels,
            key=lambda item: (_area(item.bbox), -item.word_count),
            reverse=True,
        )[:max_panels]
        for panel in ranked:
            panel.reasons = ["largest_panel_fallback"]
            panel.score = max(panel.score, 1.0)
    if not ranked:
        return []
    ranked.sort(key=lambda item: (-item.score, item.label))
    return ranked[:max(1, max_panels)]


def _annotate_panel_issue_scores(
    panels: list[PanelPolishTarget],
    dom_audit: dict[str, Any],
) -> None:
    issue_index = _issue_index(dom_audit)
    for panel in panels:
        key = _norm(panel.label + " " + panel.panel_id + " " + panel.role)
        score = 0.0
        reasons: list[str] = []
        for issue_key, issue_reasons in issue_index.items():
            if issue_key and (issue_key in key or key in issue_key):
                for reason in issue_reasons:
                    if reason not in reasons:
                        reasons.append(reason)
                    score += _reason_weight(reason)
        area = _area(panel.bbox)
        if area > 0 and panel.word_count < _min_words_for_panel(panel):
            reasons.append("panel_text_density_low")
            score += 40.0
        if panel.image_count > 0 and panel.word_count < 45:
            reasons.append("image_backed_panel_needs_more_explanation")
            score += 35.0
        if panel.score:
            score += panel.score
        panel.score = score
        panel.reasons = _dedupe(reasons)


def build_panel_polish_prompt(context: dict[str, Any]) -> str:
    panels = context.get("selected_panels") or []
    panel_lines = []
    for panel in panels:
        if not isinstance(panel, dict):
            continue
        visual = panel.get("visual_triage") if isinstance(panel.get("visual_triage"), dict) else {}
        visual_goal = str(visual.get("patch_goal") or visual.get("expected_visible_gain") or "").strip()
        visual_issue = str(visual.get("visual_issue") or "").strip()
        panel_lines.append(
            "- {panel_id} | {role} | words={word_count} images={image_count} "
            "tables={table_count} | reasons={reasons} | visual_issue={visual_issue} "
            "| patch_goal={visual_goal} | crop={crop}".format(
                panel_id=panel.get("panel_id") or panel.get("label"),
                role=panel.get("role") or "",
                word_count=panel.get("word_count", 0),
                image_count=panel.get("image_count", 0),
                table_count=panel.get("table_count", 0),
                reasons=", ".join(panel.get("reasons") or []),
                visual_issue=visual_issue,
                visual_goal=visual_goal,
                crop=panel.get("crop_path") or "",
            )
        )
        source_visuals = panel.get("source_visuals") if isinstance(panel.get("source_visuals"), list) else []
        if source_visuals:
            locked = [
                "{block_id}:{source_id}:{area}".format(
                    block_id=item.get("block_id") or item.get("label") or "?",
                    source_id=item.get("source_id") or "?",
                    area=round(float(item.get("area") or 0), 1),
                )
                for item in source_visuals[:6]
                if isinstance(item, dict)
            ]
            panel_lines.append(f"  locked source visuals: {', '.join(locked)}")
    selector = str(context.get("selector") or "dom")
    patch_mode = str(context.get("patch_mode") or "auto")
    visual_triage = context.get("visual_triage") if isinstance(context.get("visual_triage"), dict) else {}
    triage_summary = str(visual_triage.get("summary") or visual_triage.get("reason") or "").strip()
    return f"""You are running the Panel Polish Loop for an authored academic paper poster.

Goal:
- Improve final small details while preserving the current successful global poster.
- Focus on panel whitespace, panel text density, factual accuracy, local typography/color polish, and source visual placement.
- This iteration is selector={selector}. If selector=vlm, treat visual_triage as the primary reason the panels were selected; DOM metrics are only guardrails.
- Patch mode is {patch_mode}.

Files:
- Edit ./poster.html only.
- Full poster preview: ./preview_before.png
- Panel context: ./panel_polish_context.json
- Source DOM audit: ./paper_poster_dom_audit.json
- Paper memory/dossier files may exist in this directory.

Selected panels for this iteration:
{chr(10).join(panel_lines) if panel_lines else "- No panel selected; make no edits and report skipped."}

Visual triage summary:
{triage_summary or "- none"}

Operating policy:
- You may inspect the whole poster and all context files.
- Prefer local edits inside the selected panel roots. Small shared CSS additions are allowed when they preserve global style.
- Do not change canvas size, .paper-poster root identity, or rewrite the entire poster.
- Do not introduce unsupported paper facts. Use paper_memory_dossier, paper_memory, paper_evidence_packs, source visuals, or existing poster text.
- If the correct fix requires broad global re-layout, do not perform it here. Set "needs_global_refine": true in panel_polish_done.json.
- Preserve source visual ids and use object-fit: contain for paper screenshots unless there is a clear reason not to.
- For selected panels with locked source visuals, keep the existing paper screenshots/tables as anchors: do not remove, replace, duplicate, shrink, crop, or swap their source_id/src. If a visual already looks wrong, prefer adding/opening explanatory native text in the adjacent lane; only touch the visual if the change makes it larger or more contain-fitted.
- Remove visible paper-order prefixes such as "Figure 7" or "Table 2" from poster-facing captions unless the number itself is scientifically meaningful.
- Do not solve whitespace by wrapping every text unit in rectangles. Prefer open typography, alignment, rules, native tables, and selective emphasis; boxes/cards should be sparse and purposeful.
- If patch mode is text-only, do not add/remove/reorder block elements or introduce new data-block-id/data-slot-id nodes. Rewrite existing caption/body/readout text, adjust local typography sparingly, and keep every image/table/source visual in its current lane.
- Do not run Playwright, browser sessions, local HTTP servers, screenshots, or long validation commands. The outer loop renders and audits after your patch.

Before finishing:
- Write the marker immediately after the HTML edit. Do not do extra visual self-checks.
- Write ./panel_polish_done.json with:
  {{
    "status": "done" | "skipped",
    "needs_global_refine": false,
    "changed_panels": ["..."],
    "summary": "...",
    "evidence_refs": [{{"chunk_id": "...", "source_id": "...", "quote": "..."}}]
  }}
"""


def compare_panel_scope(
    before_html: str,
    after_html: str,
    *,
    selected_panel_ids: set[str],
) -> dict[str, Any]:
    before = _panel_html_map(before_html)
    after = _panel_html_map(after_html)
    changed = []
    for panel_id in sorted(set(before) | set(after)):
        if _normalize_html(before.get(panel_id, "")) != _normalize_html(after.get(panel_id, "")):
            changed.append(panel_id)
    selected = _expand_selected_scope_ids(before, selected_panel_ids)
    out_of_scope = [
        panel_id for panel_id in changed
        if panel_id not in selected
    ]
    root_before = _paper_poster_root_contract(before_html)
    root_after = _paper_poster_root_contract(after_html)
    return {
        "changed_panels": changed,
        "out_of_scope_changed_panels": out_of_scope,
        "changed_panel_count": len(changed),
        "out_of_scope_changed_panel_count": len(out_of_scope),
        "root_contract_before": root_before,
        "root_contract_after": root_after,
        "root_contract_changed": root_before != root_after,
    }


def panel_polish_acceptance(
    *,
    before_audit: dict[str, Any],
    after_dom_check: dict[str, Any],
    scope_report: dict[str, Any],
    allow_global: bool = False,
) -> dict[str, Any]:
    before = _quality_counts_from_audit(before_audit)
    after = _quality_counts_from_dom_check(after_dom_check)
    rejected: list[str] = []
    if after["missing_images"] > before["missing_images"]:
        rejected.append("missing_images_regressed")
    if after["hard_p0"] > before["hard_p0"]:
        rejected.append("dom_p0_regressed")
    if after["visual_overlaps"] > before["visual_overlaps"]:
        rejected.append("visual_overlap_regressed")
    if after["content_outside_panel"] > before["content_outside_panel"]:
        rejected.append("content_outside_panel_regressed")
    if after["panel_blank_bands"] > before["panel_blank_bands"]:
        rejected.append("panel_blank_band_regressed")
    if after["panel_text_thin"] > before["panel_text_thin"]:
        rejected.append("panel_text_thin_regressed")
    if after["image_crop_issues"] > before["image_crop_issues"]:
        rejected.append("image_crop_issue_regressed")
    if after["duplicate_image_groups"] > before["duplicate_image_groups"]:
        rejected.append("duplicate_image_group_regressed")
    if after["word_count"] + 15 < before["word_count"]:
        rejected.append("word_count_regressed")
    if bool(scope_report.get("root_contract_changed")):
        rejected.append("paper_poster_root_contract_changed")
    if not allow_global and int(scope_report.get("out_of_scope_changed_panel_count") or 0) > 0:
        rejected.append("out_of_scope_panel_changes")

    improved = (
        after["panel_blank_bands"] < before["panel_blank_bands"]
        or after["panel_text_thin"] < before["panel_text_thin"]
        or after["image_crop_issues"] < before["image_crop_issues"]
        or after["duplicate_image_groups"] < before["duplicate_image_groups"]
        or after["word_count"] > before["word_count"]
    )
    accepted = not rejected and (improved or int(scope_report.get("changed_panel_count") or 0) > 0)
    return {
        "accepted": bool(accepted),
        "rejected_reasons": rejected,
        "improved": bool(improved),
        "before": before,
        "after": after,
        "scope": scope_report,
    }


def run_panel_visual_triage(
    *,
    settings: Any | None,
    preview_path: Path,
    contact_sheet_path: Path | None,
    panels: list[PanelPolishTarget],
    max_panels: int,
    work_dir: Path,
    cancellation_token: CancellationToken | None = None,
) -> dict[str, Any]:
    """Ask the configured VLM to choose the panel-local edits with the best
    expected visual payoff.

    This is intentionally advisory. DOM audits remain the non-regression gate,
    but they no longer choose the work when selector=vlm.
    """
    _raise_if_cancelled(cancellation_token, "panel_polish.triage.start")
    result_path = work_dir / "panel_visual_triage.json"
    base = {
        "kind": "paper_poster_panel_visual_triage",
        "version": 1,
        "status": "skipped",
        "selected_panels": [],
    }
    if settings is None:
        base["reason"] = "settings_missing"
        atomic_write_json(result_path, base)
        return base
    if not panels:
        base["reason"] = "no_candidate_panels"
        atomic_write_json(result_path, base)
        return base
    if not preview_path.exists() or contact_sheet_path is None or not contact_sheet_path.exists():
        base["reason"] = "visual_inputs_missing"
        atomic_write_json(result_path, base)
        return base

    try:
        from ..llm_backend import make_backend

        backend = make_backend(settings, settings.critic_model, role="critic")
        max_selected = max(1, int(max_panels or 1))
        candidates = [_triage_candidate_json(idx, panel) for idx, panel in enumerate(panels, start=1)]
        prompt = (
            "You are the visual target selector for an academic paper poster panel-polish loop.\n"
            "Use the full poster preview and the labeled panel contact sheet to choose the "
            f"{max_selected} panel(s) where a local HTML/CSS/text patch is most likely to make "
            "a visible improvement. Do not mechanically follow DOM metrics; judge the rendered "
            "poster. Good targets have large blank areas, weak information density, awkward local "
            "typography/color, caption text that still sounds like source-paper ordering, or source "
            "visuals that look cropped, duplicated, misplaced, or too detached from explanation.\n\n"
            "Also treat over-boxed panels as polish targets: if a panel has become a rigid grid of "
            "small cards, nested rectangles, or boxed prose, prefer a de-boxing/local typography goal "
            "instead of adding still more boxes.\n\n"
            "Only choose panels that can be improved locally without changing the global canvas or "
            "poster grid. Prefer no selection over a low-value edit.\n\n"
            "Return strict JSON only with this shape:\n"
            "{\n"
            '  "kind": "paper_poster_panel_visual_triage",\n'
            '  "version": 1,\n'
            '  "summary": "...",\n'
            '  "selected_panels": [\n'
            "    {\n"
            '      "panel_id": "exact id from candidates",\n'
            '      "severity": "high|medium|low",\n'
            '      "confidence": 0.0,\n'
            '      "visual_issue": "what looks weak now",\n'
            '      "patch_goal": "what Codex should change inside this panel",\n'
            '      "expected_visible_gain": "what should visibly improve"\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            "Candidate panel manifest:\n"
            "```json\n"
            f"{json.dumps(candidates, ensure_ascii=False, indent=2)}\n"
            "```"
        )
        attachments = [
            _image_attachment("full poster preview", preview_path, _visual_max_edge(settings)),
            _image_attachment("labeled panel contact sheet", contact_sheet_path, _visual_max_edge(settings)),
        ]
        messages = _vision_messages(backend, prompt, attachments)
        thinking_budget = _visual_thinking_budget(settings)
        request_kwargs: dict[str, Any] = {
            "system": (
                "You are a precise VLM critic for academic poster panel polish. "
                "You choose local, visible, high-leverage panel fixes and return only valid JSON."
            ),
            "messages": messages,
            "tools": [],
            "thinking_budget": thinking_budget,
            "max_tokens": max(4096, thinking_budget + 2048) if thinking_budget > 0 else 4096,
        }
        if (
            cancellation_token is not None
            and getattr(cancellation_token, "can_cancel", True)
        ):
            request_kwargs["cancellation_token"] = cancellation_token
        response = backend.create_turn(**request_kwargs)
        _raise_if_cancelled(cancellation_token, "panel_polish.triage.after_model")
        parsed = parse_panel_visual_triage(
            response.text,
            valid_panel_ids={panel.panel_id for panel in panels},
            max_panels=max_selected,
        )
        parsed["status"] = "ok"
    except Exception as exc:  # noqa: BLE001
        parsed = {
            **base,
            "status": "error",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    _raise_if_cancelled(cancellation_token, "panel_polish.triage.before_write")
    atomic_write_json(result_path, parsed)
    return parsed


def parse_panel_visual_triage(
    text: str,
    *,
    valid_panel_ids: set[str],
    max_panels: int,
) -> dict[str, Any]:
    raw = _extract_json_object(text)
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw.get("selected_panels") or []:
        if not isinstance(item, dict):
            continue
        panel_id = _norm_id(str(item.get("panel_id") or ""))
        if not panel_id or panel_id not in valid_panel_ids or panel_id in seen:
            continue
        seen.add(panel_id)
        severity = str(item.get("severity") or "medium").lower()
        if severity not in {"high", "medium", "low"}:
            severity = "medium"
        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.5
        selected.append({
            "panel_id": panel_id,
            "severity": severity,
            "confidence": max(0.0, min(1.0, confidence)),
            "visual_issue": str(item.get("visual_issue") or "").strip(),
            "patch_goal": str(item.get("patch_goal") or "").strip(),
            "expected_visible_gain": str(item.get("expected_visible_gain") or "").strip(),
        })
        if len(selected) >= max(1, int(max_panels or 1)):
            break
    return {
        "kind": "paper_poster_panel_visual_triage",
        "version": 1,
        "summary": str(raw.get("summary") or "").strip(),
        "selected_panels": selected,
    }


def write_panel_visual_comparison(
    *,
    context: dict[str, Any],
    after_preview_path: Path,
    after_dom_check: dict[str, Any],
    work_dir: Path,
) -> str | None:
    panels = _panels_from_context(context)
    if not panels or not after_preview_path.exists():
        return None
    after_panels = [
        PanelPolishTarget(
            panel_id=panel.panel_id,
            label=panel.label,
            role=panel.role,
            bbox=dict(panel.bbox),
            word_count=panel.word_count,
            image_count=panel.image_count,
            table_count=panel.table_count,
            score=panel.score,
            reasons=list(panel.reasons),
            visual_triage=dict(panel.visual_triage),
        )
        for panel in panels
    ]
    _write_panel_crops(
        preview_path=after_preview_path,
        dom_audit=after_dom_check,
        panels=after_panels,
        out_dir=work_dir / "panel_crops_after",
    )
    out = _write_panel_comparison_sheet(
        before_panels=panels,
        after_panels=after_panels,
        out_path=work_dir / "panel_visual_comparison.png",
    )
    return str(out) if out is not None else None


def run_panel_visual_judge(
    *,
    settings: Any | None,
    context: dict[str, Any],
    comparison_sheet_path: str | None,
    acceptance: dict[str, Any],
    work_dir: Path,
    cancellation_token: CancellationToken | None = None,
) -> dict[str, Any]:
    _raise_if_cancelled(cancellation_token, "panel_polish.judge.start")
    result_path = work_dir / "panel_visual_judge.json"
    base = {
        "kind": "paper_poster_panel_visual_judge",
        "version": 1,
        "status": "skipped",
        "accepted": False,
        "verdict": "neutral",
        "issues": [],
    }
    if settings is None:
        base["reason"] = "settings_missing"
        atomic_write_json(result_path, base)
        return base
    path = Path(str(comparison_sheet_path or ""))
    if not path.exists():
        base["reason"] = "comparison_sheet_missing"
        atomic_write_json(result_path, base)
        return base

    try:
        from ..llm_backend import make_backend

        backend = make_backend(settings, settings.critic_model, role="critic")
        selected = context.get("selected_panels") or []
        prompt = (
            "You are the visual acceptance judge for one panel-polish patch on an academic paper poster.\n"
            "The image is a labeled before/after comparison sheet for the selected panel(s). "
            "Judge visible quality, not just DOM counters. Accept only if the after side is clearly "
            "better in the selected panels and does not introduce obvious visual regressions such as "
            "more empty space, weaker typography, bad screenshot crop, duplicate source visuals, "
            "overlap, broken images, or unsupported-looking caption clutter. Neutral/no visible gain "
            "must be rejected so the loop does not promote churn. Also reject edits that make a panel "
            "more rigidly boxed/card-heavy, unless the boxes are native data tables or compact comparison rows that "
            "are clearly necessary.\n\n"
            "Return strict JSON only with this shape:\n"
            "{\n"
            '  "kind": "paper_poster_panel_visual_judge",\n'
            '  "version": 1,\n'
            '  "verdict": "improved|neutral|worse",\n'
            '  "accepted": true,\n'
            '  "summary": "...",\n'
            '  "issues": ["..."]\n'
            "}\n\n"
            "Selected panel context:\n"
            "```json\n"
            f"{json.dumps(selected, ensure_ascii=False, indent=2)[:12000]}\n"
            "```\n\n"
            "DOM safety-gate snapshot, for awareness only:\n"
            "```json\n"
            f"{json.dumps({k: acceptance.get(k) for k in ('rejected_reasons', 'improved', 'before', 'after')}, ensure_ascii=False, indent=2)}\n"
            "```"
        )
        attachments = [_image_attachment("before-after panel comparison sheet", path, _visual_max_edge(settings))]
        messages = _vision_messages(backend, prompt, attachments)
        thinking_budget = _visual_thinking_budget(settings)
        request_kwargs: dict[str, Any] = {
            "system": (
                "You are a strict visual judge for local academic-poster panel edits. "
                "Return only valid JSON."
            ),
            "messages": messages,
            "tools": [],
            "thinking_budget": thinking_budget,
            "max_tokens": max(3072, thinking_budget + 1536) if thinking_budget > 0 else 3072,
        }
        if (
            cancellation_token is not None
            and getattr(cancellation_token, "can_cancel", True)
        ):
            request_kwargs["cancellation_token"] = cancellation_token
        response = backend.create_turn(**request_kwargs)
        _raise_if_cancelled(cancellation_token, "panel_polish.judge.after_model")
        parsed = parse_panel_visual_judge(response.text)
        parsed["status"] = "ok"
    except Exception as exc:  # noqa: BLE001
        parsed = {
            **base,
            "status": "error",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    _raise_if_cancelled(cancellation_token, "panel_polish.judge.before_write")
    atomic_write_json(result_path, parsed)
    return parsed


def _raise_if_cancelled(
    cancellation_token: CancellationToken | None,
    phase: str,
) -> None:
    if cancellation_token is not None:
        cancellation_token.raise_if_cancelled(phase)


def parse_panel_visual_judge(text: str) -> dict[str, Any]:
    raw = _extract_json_object(text)
    verdict = str(raw.get("verdict") or "neutral").lower()
    if verdict not in {"improved", "neutral", "worse"}:
        verdict = "neutral"
    accepted = bool(raw.get("accepted")) and verdict == "improved"
    issues = raw.get("issues") if isinstance(raw.get("issues"), list) else []
    return {
        "kind": "paper_poster_panel_visual_judge",
        "version": 1,
        "verdict": verdict,
        "accepted": accepted,
        "summary": str(raw.get("summary") or "").strip(),
        "issues": [str(item) for item in issues if str(item).strip()],
    }


def source_visual_lock_report(
    *,
    before_audit: dict[str, Any],
    after_dom_check: dict[str, Any],
    selected_panel_ids: set[str],
    max_area_shrink_ratio: float = 0.12,
) -> dict[str, Any]:
    """Detect source-visual regressions inside selected panels.

    The VLM judge remains the quality authority, but source-backed screenshots
    and tables are expensive to recover once a local patch shrinks or swaps
    them. This report is intentionally narrow: it only protects visuals already
    present inside the selected panel roots.
    """
    selected = {_norm_id(item) for item in selected_panel_ids if str(item).strip()}
    before_layers = before_audit.get("dom_layers") or before_audit.get("domLayers") or []
    after_layers = after_dom_check.get("domLayers") or after_dom_check.get("dom_layers") or []
    before_panels = _top_level_panel_records(_panel_records_from_dom_layers(before_audit))
    after_panels = _top_level_panel_records(_panel_records_from_dom_layers({"dom_layers": after_layers, "metrics": _audit_metrics(before_audit)}))
    before_by_id = {panel.panel_id: panel for panel in before_panels}
    after_by_id = {panel.panel_id: panel for panel in after_panels}
    violations: list[dict[str, Any]] = []
    checked: list[dict[str, Any]] = []
    for panel_id in sorted(selected):
        before_panel = before_by_id.get(panel_id)
        if before_panel is None:
            continue
        after_panel = after_by_id.get(panel_id)
        before_visuals = _source_visuals_in_panel(before_layers, before_panel.bbox)
        if not before_visuals:
            continue
        after_visuals = _source_visuals_in_panel(after_layers, after_panel.bbox if after_panel else before_panel.bbox)
        after_by_key = {
            _source_visual_key(item): item
            for item in after_visuals
            if _source_visual_key(item)
        }
        for before_visual in before_visuals:
            key = _source_visual_key(before_visual)
            if not key:
                continue
            after_visual = after_by_key.get(key)
            checked.append({
                "panel_id": panel_id,
                "block_id": before_visual.get("block_id"),
                "source_id": before_visual.get("source_id"),
                "before_area": before_visual.get("area"),
                "after_area": after_visual.get("area") if after_visual else None,
            })
            if after_visual is None:
                violations.append({
                    "panel_id": panel_id,
                    "block_id": before_visual.get("block_id"),
                    "source_id": before_visual.get("source_id"),
                    "reason": "source_visual_missing_or_source_changed",
                })
                continue
            before_area = float(before_visual.get("area") or 0)
            after_area = float(after_visual.get("area") or 0)
            if before_area > 0 and after_area < before_area * (1.0 - max_area_shrink_ratio):
                violations.append({
                    "panel_id": panel_id,
                    "block_id": before_visual.get("block_id"),
                    "source_id": before_visual.get("source_id"),
                    "reason": "source_visual_area_shrank",
                    "before_area": round(before_area, 2),
                    "after_area": round(after_area, 2),
                })
    return {
        "kind": "paper_poster_source_visual_lock_report",
        "version": 1,
        "selected_panel_ids": sorted(selected),
        "checked": checked,
        "violations": violations,
        "violation_count": len(violations),
    }


def panel_text_only_structure_report(
    *,
    before_html: str,
    after_html: str,
    selected_panel_ids: set[str],
) -> dict[str, Any]:
    before_panels = _panel_html_map(before_html)
    after_panels = _panel_html_map(after_html)
    selected = _expand_selected_scope_ids(before_panels, selected_panel_ids)
    violations: list[dict[str, Any]] = []
    checked: list[dict[str, Any]] = []
    for panel_id in sorted(selected):
        before_ids = _panel_node_ids(before_panels.get(panel_id, ""))
        after_ids = _panel_node_ids(after_panels.get(panel_id, ""))
        if not before_ids and not after_ids:
            continue
        added = sorted(after_ids - before_ids)
        removed = sorted(before_ids - after_ids)
        checked.append({
            "panel_id": panel_id,
            "before_node_count": len(before_ids),
            "after_node_count": len(after_ids),
            "added": added,
            "removed": removed,
        })
        if added:
            violations.append({
                "panel_id": panel_id,
                "reason": "text_only_added_nodes",
                "node_ids": added[:20],
            })
        if removed:
            violations.append({
                "panel_id": panel_id,
                "reason": "text_only_removed_nodes",
                "node_ids": removed[:20],
            })
    return {
        "kind": "paper_poster_text_only_structure_report",
        "version": 1,
        "selected_panel_ids": sorted(_norm_id(item) for item in selected_panel_ids if str(item).strip()),
        "checked": checked,
        "violations": violations,
        "violation_count": len(violations),
    }


def _panel_records_from_dom_layers(dom_audit: dict[str, Any]) -> list[PanelPolishTarget]:
    layers = dom_audit.get("dom_layers") or dom_audit.get("domLayers") or []
    metrics = _audit_metrics(dom_audit)
    root_area = max(1.0, float(metrics.get("root_w_px") or 0) * float(metrics.get("root_h_px") or 0))
    out: list[PanelPolishTarget] = []
    seen: set[str] = set()
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        bbox = _bbox(layer.get("bbox") or layer.get("rect"))
        if not bbox:
            continue
        area_ratio = _area(bbox) / root_area
        if area_ratio < 0.012 or area_ratio > 0.58:
            continue
        label = str(layer.get("name") or layer.get("label") or layer.get("block_id") or layer.get("layer_id") or "").strip()
        role = str(layer.get("role") or layer.get("data_role") or "").strip()
        kind = str(layer.get("kind") or "").lower()
        class_name = str(layer.get("class_name") or layer.get("className") or "")
        hay = " ".join([label, role, class_name]).lower()
        class_tokens = set(re.split(r"\s+", class_name.lower().strip()))
        source_visual_wrapper = any(
            token in hay
            for token in ("source-fig", "source_visual", "source visual", "table-crop", "local_evidence")
        )
        if source_visual_wrapper:
            continue
        is_panel = (
            kind in {"group", "shape"}
            and (
                "panel" in class_tokens
                or "slot" in class_tokens
                or any(token in hay for token in ("method", "result", "analysis", "limitation", "takeaway", "benchmark"))
            )
        )
        if not is_panel:
            continue
        if _is_chrome_panel(hay, bbox, metrics):
            continue
        panel_id = _norm_id(str(layer.get("block_id") or layer.get("layer_id") or role or label))
        if not panel_id or panel_id in seen:
            continue
        seen.add(panel_id)
        text = str(layer.get("text") or "")
        out.append(PanelPolishTarget(
            panel_id=panel_id,
            label=label or panel_id,
            role=role,
            bbox=bbox,
            word_count=_word_count(text),
            image_count=0,
            table_count=0,
        ))
    _attach_panel_child_counts(out, layers)
    return out


def _top_level_panel_records(panels: list[PanelPolishTarget]) -> list[PanelPolishTarget]:
    top_level: list[PanelPolishTarget] = []
    for panel in panels:
        panel_area = _area(panel.bbox)
        nested = False
        for other in panels:
            if other.panel_id == panel.panel_id:
                continue
            other_area = _area(other.bbox)
            if other_area <= panel_area * 1.25:
                continue
            if _center_inside(panel.bbox, other.bbox):
                nested = True
                break
        if not nested:
            top_level.append(panel)
    return top_level or panels


def _attach_panel_child_counts(panels: list[PanelPolishTarget], layers: list[Any]) -> None:
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        kind = str(layer.get("kind") or "").lower()
        if kind not in {"image", "table"}:
            continue
        bbox = _bbox(layer.get("bbox") or layer.get("rect"))
        if not bbox:
            continue
        for panel in panels:
            if _center_inside(bbox, panel.bbox):
                if kind == "image":
                    panel.image_count += 1
                    source_visual = _source_visual_record(layer)
                    if source_visual:
                        panel.source_visuals.append(source_visual)
                else:
                    panel.table_count += 1
                break


def _source_visuals_in_panel(layers: list[Any], panel_bbox: dict[str, float]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        if str(layer.get("kind") or "").lower() != "image":
            continue
        bbox = _bbox(layer.get("bbox") or layer.get("rect"))
        if not bbox or not _center_inside(bbox, panel_bbox):
            continue
        record = _source_visual_record(layer)
        if record:
            out.append(record)
    return out


def _source_visual_record(layer: dict[str, Any]) -> dict[str, Any] | None:
    source_id = str(layer.get("source_id") or layer.get("dataSourceId") or layer.get("data_source_id") or "").strip()
    source = str(layer.get("source") or layer.get("src") or "").strip()
    block_id = str(layer.get("block_id") or layer.get("label") or layer.get("layer_id") or "").strip()
    hay = " ".join([source_id, source, block_id]).lower()
    if not source_id and "ingest_" not in hay and "assets/" not in hay:
        return None
    bbox = _bbox(layer.get("bbox") or layer.get("rect"))
    return {
        "block_id": block_id,
        "source_id": source_id,
        "source": source,
        "bbox": bbox,
        "area": _area(bbox),
    }


def _source_visual_key(record: dict[str, Any]) -> str:
    source_id = _norm_id(str(record.get("source_id") or ""))
    block_id = _norm_id(str(record.get("block_id") or ""))
    return source_id or block_id


def _issue_index(dom_audit: dict[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}

    def add(label: str, reason: str) -> None:
        key = _norm(label)
        if not key:
            return
        out.setdefault(key, [])
        if reason not in out[key]:
            out[key].append(reason)

    for item in dom_audit.get("dom_panel_internal_blank_band_issues") or []:
        if isinstance(item, dict):
            add(str(item.get("label") or item.get("panel") or ""), "panel_internal_blank_band")
    for item in dom_audit.get("panelInteriorBlankBandIssues") or []:
        if isinstance(item, dict):
            add(str(item.get("label") or item.get("panel") or ""), "panel_internal_blank_band")
    for item in dom_audit.get("panelTextThinIssues") or []:
        if isinstance(item, dict):
            add(str(item.get("label") or item.get("panel") or ""), "panel_text_thin")
    for item in dom_audit.get("dom_image_crop_issues") or dom_audit.get("imageCropIssues") or []:
        if isinstance(item, dict):
            add(str(item.get("panel") or item.get("label") or ""), "image_crop_or_slot_issue")
    for item in dom_audit.get("dom_images") or dom_audit.get("missingImages") or []:
        if isinstance(item, dict):
            add(str(item.get("panel") or item.get("label") or ""), "missing_image")
    for item in dom_audit.get("imageDuplicateIssues") or []:
        if isinstance(item, dict):
            for panel in item.get("panels") or []:
                add(str(panel), "duplicate_source_visual")
    for finding in dom_audit.get("paper_poster_dom_findings") or []:
        if not isinstance(finding, dict):
            continue
        target = str(finding.get("target") or "")
        finding_id = str(finding.get("id") or "")
        if "blank" in finding_id:
            add(target, "panel_internal_blank_band")
        elif "thin" in finding_id:
            add(target, "panel_text_thin")
        elif "image" in finding_id or "crop" in finding_id:
            add(target, "image_crop_or_slot_issue")
        elif "overlap" in finding_id:
            add(target, "panel_overlap")
    return out


def _write_panel_crops(
    *,
    preview_path: Path,
    dom_audit: dict[str, Any],
    panels: list[PanelPolishTarget],
    out_dir: Path,
) -> None:
    if not preview_path.exists() or not panels:
        return
    try:
        from PIL import Image
    except Exception:
        return
    metrics = _audit_metrics(dom_audit)
    root_w = float(metrics.get("root_w_px") or metrics.get("root_w") or 0)
    root_h = float(metrics.get("root_h_px") or metrics.get("root_h") or 0)
    if root_w <= 0 or root_h <= 0:
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(preview_path) as image:
            sx = image.width / root_w
            sy = image.height / root_h
            for panel in panels:
                bbox = panel.bbox
                pad = 8
                x0 = max(0, int(round(float(bbox.get("x", 0)) * sx)) - pad)
                y0 = max(0, int(round(float(bbox.get("y", 0)) * sy)) - pad)
                x1 = min(image.width, int(round(float(bbox.get("right", 0)) * sx)) + pad)
                y1 = min(image.height, int(round(float(bbox.get("bottom", 0)) * sy)) + pad)
                if x1 <= x0 or y1 <= y0:
                    continue
                crop = image.crop((x0, y0, x1, y1))
                crop_path = out_dir / f"{panel.panel_id}.png"
                crop.save(crop_path)
                panel.crop_path = str(crop_path)
    except Exception:
        return


def _write_panel_contact_sheet(
    panels: list[PanelPolishTarget],
    *,
    out_path: Path,
) -> Path | None:
    panels = [panel for panel in panels if panel.crop_path and Path(panel.crop_path).exists()]
    if not panels:
        return None
    try:
        from PIL import Image, ImageDraw, ImageOps
    except Exception:
        return None
    tile_w = 720
    tile_h = 500
    header_h = 56
    cols = 2
    rows = (len(panels) + cols - 1) // cols
    sheet = Image.new("RGB", (tile_w * cols, tile_h * rows), (248, 246, 240))
    draw = ImageDraw.Draw(sheet)
    for idx, panel in enumerate(panels):
        row = idx // cols
        col = idx % cols
        x = col * tile_w
        y = row * tile_h
        draw.rectangle((x + 8, y + 8, x + tile_w - 8, y + tile_h - 8), outline=(194, 184, 170), width=2)
        label = (
            f"{idx + 1}. {panel.panel_id} | {panel.role or panel.label} | "
            f"words {panel.word_count} img {panel.image_count} table {panel.table_count}"
        )
        draw.text((x + 18, y + 18), label[:112], fill=(20, 28, 35))
        hints = ", ".join(panel.reasons[:3])
        if hints:
            draw.text((x + 18, y + 36), f"hints: {hints}"[:112], fill=(88, 75, 65))
        try:
            with Image.open(str(panel.crop_path)) as raw:
                fitted = ImageOps.contain(raw.convert("RGB"), (tile_w - 36, tile_h - header_h - 32))
                px = x + (tile_w - fitted.width) // 2
                py = y + header_h + (tile_h - header_h - fitted.height) // 2
                sheet.paste(fitted, (px, py))
        except Exception:
            continue
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path)
    return out_path


def _write_panel_comparison_sheet(
    *,
    before_panels: list[PanelPolishTarget],
    after_panels: list[PanelPolishTarget],
    out_path: Path,
) -> Path | None:
    pairs: list[tuple[PanelPolishTarget, PanelPolishTarget]] = []
    after_by_id = {panel.panel_id: panel for panel in after_panels}
    for before in before_panels:
        after = after_by_id.get(before.panel_id)
        if before.crop_path and Path(before.crop_path).exists() and after and after.crop_path and Path(after.crop_path).exists():
            pairs.append((before, after))
    if not pairs:
        return None
    try:
        from PIL import Image, ImageDraw, ImageOps
    except Exception:
        return None
    card_w = 1560
    card_h = 620
    header_h = 72
    half_w = (card_w - 54) // 2
    sheet = Image.new("RGB", (card_w, card_h * len(pairs)), (248, 246, 240))
    draw = ImageDraw.Draw(sheet)
    for row, (before, after) in enumerate(pairs):
        y = row * card_h
        draw.rectangle((8, y + 8, card_w - 8, y + card_h - 8), outline=(194, 184, 170), width=2)
        title = f"{before.panel_id} | {before.role or before.label}"
        triage = before.visual_triage or {}
        goal = str(triage.get("patch_goal") or triage.get("expected_visible_gain") or "").strip()
        draw.text((18, y + 16), title[:130], fill=(20, 28, 35))
        if goal:
            draw.text((18, y + 38), f"goal: {goal}"[:170], fill=(88, 75, 65))
        draw.text((18, y + header_h - 22), "BEFORE", fill=(128, 33, 21))
        draw.text((half_w + 36, y + header_h - 22), "AFTER", fill=(19, 94, 82))
        for path, x0 in [(before.crop_path, 18), (after.crop_path, half_w + 36)]:
            try:
                with Image.open(str(path)) as raw:
                    fitted = ImageOps.contain(raw.convert("RGB"), (half_w, card_h - header_h - 28))
                    px = x0 + (half_w - fitted.width) // 2
                    py = y + header_h + (card_h - header_h - fitted.height) // 2
                    sheet.paste(fitted, (px, py))
            except Exception:
                continue
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path)
    return out_path


def _select_vlm_triaged_panels(
    candidate_panels: list[PanelPolishTarget],
    *,
    visual_triage: dict[str, Any],
    max_panels: int,
) -> list[PanelPolishTarget]:
    by_id = {panel.panel_id: panel for panel in candidate_panels}
    selected: list[PanelPolishTarget] = []
    for item in visual_triage.get("selected_panels") or []:
        if not isinstance(item, dict):
            continue
        panel = by_id.get(_norm_id(str(item.get("panel_id") or "")))
        if panel is None:
            continue
        panel.visual_triage = dict(item)
        panel.reasons = _dedupe(["vlm_visual_triage"] + panel.reasons)
        panel.score = max(panel.score, 100.0 - len(selected))
        selected.append(panel)
        if len(selected) >= max(1, int(max_panels or 1)):
            break
    return selected


def _exclude_panels(
    panels: list[PanelPolishTarget],
    excluded_ids: set[str],
) -> list[PanelPolishTarget]:
    if not excluded_ids:
        return panels
    return [panel for panel in panels if _norm_id(panel.panel_id) not in excluded_ids]


def _triage_candidate_json(idx: int, panel: PanelPolishTarget) -> dict[str, Any]:
    return {
        "sheet_index": idx,
        "panel_id": panel.panel_id,
        "label": panel.label,
        "role": panel.role,
        "bbox": panel.bbox,
        "word_count": panel.word_count,
        "image_count": panel.image_count,
        "table_count": panel.table_count,
        "dom_hints": list(panel.reasons),
        "dom_score_hint": round(float(panel.score), 3),
    }


def _panels_from_context(context: dict[str, Any]) -> list[PanelPolishTarget]:
    out: list[PanelPolishTarget] = []
    for raw in context.get("selected_panels") or []:
        if not isinstance(raw, dict):
            continue
        panel_id = _norm_id(str(raw.get("panel_id") or ""))
        if not panel_id:
            continue
        out.append(PanelPolishTarget(
            panel_id=panel_id,
            label=str(raw.get("label") or panel_id),
            role=str(raw.get("role") or ""),
            bbox=_bbox(raw.get("bbox")),
            word_count=int(raw.get("word_count") or 0),
            image_count=int(raw.get("image_count") or 0),
            table_count=int(raw.get("table_count") or 0),
            source_visuals=[item for item in raw.get("source_visuals") or [] if isinstance(item, dict)],
            score=float(raw.get("score") or 0),
            reasons=[str(item) for item in raw.get("reasons") or []],
            crop_path=str(raw.get("crop_path") or "") or None,
            visual_triage=dict(raw.get("visual_triage") or {}),
        ))
    return out


def _vision_messages(backend: Any, prompt: str, attachments: list[_VisionAttachment]) -> list[Any]:
    if not attachments:
        return [{"role": "user", "content": prompt}]
    head = backend.vision_user_message(
        image_b64=attachments[0].image_b64,
        media_type=attachments[0].media_type,
        text=f"{prompt}\n\n[image: {attachments[0].label}]",
    )
    if not isinstance(head, dict) or not isinstance(head.get("content"), list):
        messages = [head]
        for attachment in attachments[1:]:
            messages.append(backend.vision_user_message(
                image_b64=attachment.image_b64,
                media_type=attachment.media_type,
                text=f"[image: {attachment.label}]",
            ))
        return messages
    for attachment in attachments[1:]:
        sibling = backend.vision_user_message(
            image_b64=attachment.image_b64,
            media_type=attachment.media_type,
            text=f"[image: {attachment.label}]",
        )
        sibling_content = sibling.get("content") if isinstance(sibling, dict) else None
        if isinstance(sibling_content, list):
            head["content"].extend(sibling_content)
    return [head]


def _image_attachment(label: str, path: Path, max_edge: int) -> _VisionAttachment:
    image_b64, media_type = _downscale_image_b64(path, max_edge=max_edge)
    return _VisionAttachment(label=label, image_b64=image_b64, media_type=media_type)


def _downscale_image_b64(path: Path, *, max_edge: int) -> tuple[str, str]:
    from PIL import Image

    image = Image.open(path)
    if image.mode not in {"RGB", "RGBA"}:
        image = image.convert("RGB")
    if image.mode == "RGBA":
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.getchannel("A"))
        image = background
    longest = max(image.size)
    if longest > max_edge:
        scale = max_edge / float(longest)
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.Resampling.LANCZOS,
        )
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=92, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"


def _visual_max_edge(settings: Any) -> int:
    raw = int(getattr(settings, "critic_preview_max_edge", 1024) or 1024)
    return min(2600, max(2200, raw))


def _visual_thinking_budget(settings: Any) -> int:
    raw = int(getattr(settings, "critic_thinking_budget", 0) or 0)
    return max(0, min(raw, 4000))


def _extract_json_object(text: str) -> dict[str, Any]:
    body = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", body, flags=re.S | re.I)
    if fenced:
        body = fenced.group(1).strip()
    decoder = json.JSONDecoder()
    starts = [0] if body.startswith("{") else []
    starts.extend(match.start() for match in re.finditer(r"\{", body))
    for start in starts:
        try:
            value, _end = decoder.raw_decode(body[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _panel_html_map(html: str) -> dict[str, str]:
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return {}
    root = soup.select_one(".paper-poster") or soup
    out: dict[str, str] = {}
    accepted: list[Tag] = []
    for tag in root.select(PANEL_SELECTOR):
        if not _tag_looks_like_panel_root(tag):
            continue
        if any(parent in accepted for parent in tag.parents):
            continue
        panel_id = _panel_id_from_tag(tag)
        if not panel_id or panel_id in out:
            continue
        out[panel_id] = str(tag)
        accepted.append(tag)
    return out


def _expand_selected_scope_ids(
    panel_html_by_id: dict[str, str],
    selected_panel_ids: set[str],
) -> set[str]:
    selected = {_norm_id(item) for item in selected_panel_ids if item}
    expanded = set(selected)
    for panel_id, html in panel_html_by_id.items():
        if panel_id in selected or _panel_html_contains_selected_id(html, selected):
            expanded.add(panel_id)
    return expanded


def _panel_html_contains_selected_id(html: str, selected_ids: set[str]) -> bool:
    if not html or not selected_ids:
        return False
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return False
    for tag in soup.find_all(True):
        for key in ("data-block-id", "data-slot-id", "data-panel-role", "id"):
            if _norm_id(str(tag.get(key) or "")) in selected_ids:
                return True
    return False


def _panel_node_ids(html: str) -> set[str]:
    if not html:
        return set()
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return set()
    out: set[str] = set()
    for tag in soup.find_all(True):
        for key in ("data-block-id", "data-slot-id"):
            value = _norm_id(str(tag.get(key) or ""))
            if value:
                out.add(f"{key}:{value}")
    return out


def _tag_looks_like_panel_root(tag: Tag) -> bool:
    class_tokens = {str(item).lower() for item in (tag.get("class") or [])}
    role = str(tag.get("data-panel-role") or tag.get("role") or "").lower()
    attrs = " ".join(
        str(tag.get(key) or "")
        for key in ("data-block-id", "data-slot-id", "data-panel-role", "id")
    ).lower()
    hay = " ".join([" ".join(class_tokens), role, attrs])
    if any(token in hay for token in ("source-fig", "source_visual", "source visual", "table-crop", "local_evidence")):
        return False
    if "panel" in class_tokens or "slot" in class_tokens:
        return True
    if role and role not in {"image", "figure", "table", "caption"}:
        return True
    return False


def _panel_id_from_tag(tag: Tag) -> str:
    raw = (
        tag.get("data-block-id")
        or tag.get("data-slot-id")
        or tag.get("data-panel-role")
        or tag.get("id")
        or " ".join(tag.get("class") or [])
    )
    return _norm_id(str(raw or ""))


def _paper_poster_root_contract(html: str) -> dict[str, str]:
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return {}
    root = soup.select_one(".paper-poster")
    if root is None:
        return {}
    return {
        "tag": str(root.name or ""),
        "class": " ".join(root.get("class") or []),
        "data-render-mode": str(root.get("data-render-mode") or ""),
        "data-block-id": str(root.get("data-block-id") or ""),
        "style_width_height": _style_width_height(str(root.get("style") or "")),
    }


def _quality_counts_from_audit(dom_audit: dict[str, Any]) -> dict[str, int]:
    metrics = _audit_metrics(dom_audit)
    findings = dom_audit.get("paper_poster_dom_findings") or dom_audit.get("findings") or []
    return {
        "hard_p0": int(dom_audit.get("paper_poster_dom_p0_count") or sum(
            1 for finding in findings if isinstance(finding, dict) and finding.get("severity") == "P0"
        )),
        "missing_images": len(dom_audit.get("dom_images") or dom_audit.get("missingImages") or []),
        "visual_overlaps": sum(
            1 for finding in findings
            if isinstance(finding, dict) and "overlap" in str(finding.get("id") or "")
        ),
        "content_outside_panel": sum(
            1 for finding in findings
            if isinstance(finding, dict) and "outside-panel" in str(finding.get("id") or "")
        ),
        "panel_blank_bands": int(metrics.get("panel_internal_blank_band_count") or len(
            dom_audit.get("dom_panel_internal_blank_band_issues") or dom_audit.get("panelInteriorBlankBandIssues") or []
        )),
        "panel_text_thin": int(metrics.get("panel_text_thin_count") or len(dom_audit.get("panelTextThinIssues") or [])),
        "image_crop_issues": int(metrics.get("image_crop_issue_count") or len(
            dom_audit.get("dom_image_crop_issues") or dom_audit.get("imageCropIssues") or []
        )),
        "duplicate_image_groups": int(metrics.get("image_duplicate_group_count") or len(dom_audit.get("imageDuplicateIssues") or [])),
        "word_count": int(metrics.get("leaf_visible_word_count") or metrics.get("visible_text_word_count") or metrics.get("word_count") or 0),
    }


def _quality_counts_from_dom_check(dom_check: dict[str, Any]) -> dict[str, int]:
    metrics = dom_check.get("metrics") if isinstance(dom_check.get("metrics"), dict) else {}
    return {
        "hard_p0": 0,
        "missing_images": len(dom_check.get("missingImages") or []),
        "visual_overlaps": len(dom_check.get("visualOverlaps") or []),
        "content_outside_panel": len(dom_check.get("contentOutsidePanel") or []),
        "panel_blank_bands": int(metrics.get("panel_internal_blank_band_count") or len(dom_check.get("panelInteriorBlankBandIssues") or [])),
        "panel_text_thin": int(metrics.get("panel_text_thin_count") or len(dom_check.get("panelTextThinIssues") or [])),
        "image_crop_issues": int(metrics.get("image_crop_issue_count") or len(dom_check.get("imageCropIssues") or [])),
        "duplicate_image_groups": int(metrics.get("image_duplicate_group_count") or len(dom_check.get("imageDuplicateIssues") or [])),
        "word_count": int(metrics.get("leaf_visible_word_count") or metrics.get("visible_text_word_count") or metrics.get("word_count") or 0),
    }


def _audit_metrics(dom_audit: dict[str, Any]) -> dict[str, Any]:
    metrics = dom_audit.get("metrics")
    if not isinstance(metrics, dict):
        metrics = dom_audit.get("paper_poster_dom_metrics")
    return dict(metrics) if isinstance(metrics, dict) else {}


def _bbox(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    x = _to_float(raw.get("x"))
    y = _to_float(raw.get("y"))
    w = _to_float(raw.get("w"))
    h = _to_float(raw.get("h"))
    if w <= 0 or h <= 0:
        return {}
    return {
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "right": _to_float(raw.get("right"), x + w),
        "bottom": _to_float(raw.get("bottom"), y + h),
    }


def _area(bbox: dict[str, Any]) -> float:
    return max(0.0, _to_float(bbox.get("w")) * _to_float(bbox.get("h")))


def _center_inside(child: dict[str, float], parent: dict[str, float]) -> bool:
    cx = child["x"] + child["w"] / 2.0
    cy = child["y"] + child["h"] / 2.0
    return parent["x"] <= cx <= parent["right"] and parent["y"] <= cy <= parent["bottom"]


def _is_chrome_panel(hay: str, bbox: dict[str, float], metrics: dict[str, Any]) -> bool:
    root_h = _to_float(metrics.get("root_h_px"))
    if root_h <= 0:
        return False
    if any(token in hay for token in ("header", "footer", "identity", "meta", "title")):
        return bbox.get("y", 0) < root_h * 0.16 or bbox.get("bottom", 0) > root_h * 0.90
    return False


def _min_words_for_panel(panel: PanelPolishTarget) -> int:
    area = _area(panel.bbox)
    if panel.image_count or panel.table_count:
        return 42 if area < 450_000 else 58
    return 52 if area < 450_000 else 76


def _reason_weight(reason: str) -> float:
    if reason in {"missing_image", "image_crop_or_slot_issue", "duplicate_source_visual"}:
        return 90.0
    if reason == "panel_internal_blank_band":
        return 80.0
    if reason == "panel_text_thin":
        return 70.0
    if reason == "panel_overlap":
        return 60.0
    return 25.0


def _word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9+\-_/'.:%]*", text or ""))


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _norm_id(value: str) -> str:
    out = re.sub(r"[^a-zA-Z0-9_-]+", "_", (value or "").strip())
    return out.strip("_")[:120]


def _normalize_html(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _style_width_height(style: str) -> str:
    parts = []
    for key in ("width", "height"):
        match = re.search(rf"(?:^|;)\s*{key}\s*:\s*([^;]+)", style or "", re.I)
        if match:
            parts.append(f"{key}:{match.group(1).strip()}")
    return ";".join(parts)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
