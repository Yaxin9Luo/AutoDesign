"""Build an offline HTML report for poster repair feedback packets.

This is diagnostic-only: it reads existing run artifacts and does not invoke
the designer, browser measurement, or poster generation.
"""

from __future__ import annotations

import argparse
import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_ATTEMPTS = ("attempt_02", "attempt_06", "attempt_07", "attempt_08")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _issue_id(data: dict[str, Any]) -> str:
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    return str(summary.get("issue_id") or payload.get("issue_id") or "")


def _issues(data: dict[str, Any]) -> list[dict[str, Any]]:
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    raw = summary.get("issues") or payload.get("issues") or []
    return [item for item in raw if isinstance(item, dict)]


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _clip(value: Any, limit: int = 360) -> str:
    text = str(value if value is not None else "")
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else f"{text[:limit - 1]}..."


def _file_link(attempt_dir: Path, rel_path: Any) -> str:
    rel = str(rel_path or "")
    if not rel:
        return ""
    path = attempt_dir / rel
    if path.exists():
        return f"<a href='{path.resolve().as_uri()}'><code>{_esc(rel)}</code></a>"
    return f"<code>{_esc(rel)}</code>"


def _img(path: Path | None, label: str) -> str:
    if not path or not path.exists():
        return f"<div class='missing'>{_esc(label)} missing</div>"
    return f"<figure><img src='{path.resolve().as_uri()}' alt='{_esc(label)}'><figcaption>{_esc(label)}</figcaption></figure>"


def _heading_downgrade_note(measurement: dict[str, Any]) -> str:
    bboxes = measurement.get("bboxes") if isinstance(measurement.get("bboxes"), dict) else {}
    header = bboxes.get("header-band") if isinstance(bboxes.get("header-band"), dict) else {}
    heading = bboxes.get("heading_01") if isinstance(bboxes.get("heading_01"), dict) else {}
    if not header or not heading:
        return ""
    header_metrics = header.get("_layout_metrics") if isinstance(header.get("_layout_metrics"), dict) else {}
    heading_metrics = heading.get("_layout_metrics") if isinstance(heading.get("_layout_metrics"), dict) else {}
    header_delta = float(header_metrics.get("scroll_height_px") or 0) - float(header_metrics.get("client_height_px") or 0)
    heading_delta = float(heading_metrics.get("scroll_height_px") or 0) - float(heading_metrics.get("client_height_px") or 0)
    if header_delta <= 0 and 0 < heading_delta <= 12:
        return (
            "<div class='ok'><b>Heading diagnostic after patch:</b> h1 has "
            f"{heading_delta:.1f}px internal font-metric overhang, but header-band itself has no scroll overflow. "
            "This should be diagnostic, not a hard heading_flow_overflow repair target.</div>"
        )
    return ""


def _source_visual_rows(data: dict[str, Any], packet: dict[str, Any]) -> str:
    rows: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    source_issues = list(_issues(data))
    for item in packet.get("image_issue_map") or []:
        if not isinstance(item, dict) or item.get("issue_id") != "paper_poster_html_source_visual_too_small":
            continue
        target = item.get("target") if isinstance(item.get("target"), dict) else {}
        geometry = item.get("diagnostic_geometry") if isinstance(item.get("diagnostic_geometry"), dict) else {}
        required = item.get("required_targets") if isinstance(item.get("required_targets"), dict) else {}
        source_id = target.get("source_id")
        if not source_id:
            continue
        source_issues.append({
            "source_id": source_id,
            "failure_kind": item.get("failure_kind"),
            "target_problem": required.get("target_problem"),
            "severity": required.get("severity") or geometry.get("severity"),
            "soft_finalizable": required.get("soft_finalizable") or geometry.get("soft_finalizable"),
            "recommended_first_action": required.get("recommended_first_action"),
            "source_panel_width_ratio": geometry.get("source_panel_width_ratio"),
            "required_panel_width_ratio": required.get("required_panel_width_ratio"),
            "object_fit_width_fill_ratio": geometry.get("object_fit_width_fill_ratio"),
            "required_source_height_px": required.get("required_source_height_px") or required.get("height_px_reference"),
            "acceptance_mode": required.get("acceptance_mode") or required.get("height_px_interpretation"),
        })
    for issue in source_issues:
        if not str(issue.get("source_id") or "").startswith(("ingest_fig_", "ingest_table_", "ingest_img_")):
            continue
        key = (
            str(issue.get("source_id") or ""),
            str(issue.get("failure_kind") or ""),
            str(issue.get("target_problem") or _derive_target_problem(issue)),
        )
        if key in seen:
            continue
        seen.add(key)
        target_problem = issue.get("target_problem") or _derive_target_problem(issue)
        action = issue.get("recommended_first_action") or _derive_action(issue)
        rows.append(
            "<tr>"
            f"<td>{_esc(issue.get('source_id'))}</td>"
            f"<td>{_esc(issue.get('failure_kind'))}</td>"
            f"<td>{_esc(target_problem)}</td>"
            f"<td>{_esc(issue.get('severity'))}</td>"
            f"<td>{_esc(issue.get('soft_finalizable'))}</td>"
            f"<td>{_esc(issue.get('acceptance_mode'))}</td>"
            f"<td>{_esc(issue.get('source_panel_width_ratio'))} / {_esc(issue.get('required_panel_width_ratio'))}</td>"
            f"<td>{_esc(issue.get('object_fit_width_fill_ratio'))}</td>"
            f"<td>{_esc(action)}</td>"
            "</tr>"
        )
    if not rows:
        return "<p class='muted'>No source visual issue rows in this feedback.</p>"
    return (
        "<table><thead><tr><th>Source</th><th>Failure</th><th>Target problem</th>"
        "<th>Severity</th><th>Soft-finalizable</th><th>Acceptance</th>"
        "<th>Width ratio</th><th>Object-fit fill</th><th>Recommended first action</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _derive_target_problem(issue: dict[str, Any]) -> str:
    if issue.get("target_problem"):
        return str(issue.get("target_problem"))
    failure = str(issue.get("failure_kind") or "")
    reasons = {str(item) for item in issue.get("reasons") or []}
    if (
        failure == "contain_wrapper_underfilled"
        and str(issue.get("severity") or "") == "near_miss"
        and issue.get("soft_finalizable") is True
    ):
        return "readable_visual_wrapper_polish"
    if failure == "contain_wrapper_underfilled" or "contain_wrapper_underfilled" in reasons:
        return "blank_wrapper_shell"
    if failure == "source_visual_sidecar_underfilled" or reasons & {"side_readout_too_thin", "side_text_coverage_low"}:
        return "blank_sidecar_lane"
    try:
        gap = float(issue.get("required_panel_width_ratio") or 0) - float(issue.get("source_panel_width_ratio") or 0)
    except (TypeError, ValueError):
        gap = 0
    return "minor_geometry_gap" if 0 < gap <= 0.03 else "true_too_small_visual"


def _derive_action(issue: dict[str, Any]) -> str:
    problem = _derive_target_problem(issue)
    if problem == "readable_visual_wrapper_polish":
        return "Near-miss wrapper polish: preserve the readable visual; only make scoped wrapper cleanup if safe."
    if problem == "blank_wrapper_shell":
        return "Repair wrapper aspect or fill the same flow unit with local source-backed readout; do not only nudge width."
    if problem == "blank_sidecar_lane":
        return "Fill the sidecar with source-backed readout/native rows or stack the source visual with readout below."
    if problem == "minor_geometry_gap":
        return "Prefer one local composition repair over tiny CSS width-only changes."
    return "Restore required visual ratios while preserving source aspect."


def _blank_fill_rows(data: dict[str, Any], packet: dict[str, Any]) -> str:
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    plans: list[tuple[str, dict[str, Any]]] = []
    for source_name, source in (("summary", summary), ("payload", payload), ("visual_packet", packet)):
        plan = source.get("blank_fill_plan")
        if _has_blank_fill_targets(plan):
            plans.append((source_name, plan))
        advisory_plan = source.get("advisory_blank_fill_plan")
        if _has_blank_fill_targets(advisory_plan):
            plans.append((f"{source_name}.advisory_blank_fill_plan", advisory_plan))
        for key in ("required_co_repair", "post_overflow_required_followup"):
            container = source.get(key)
            if isinstance(container, dict) and isinstance(container.get("blank_fill"), dict):
                plans.append((f"{source_name}.{key}", container["blank_fill"]))
    rows: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for source_name, plan in plans:
        for bucket, target in _blank_fill_plan_targets(plan):
            if not isinstance(target, dict):
                continue
            key = (
                str(target.get("target_kind") or ""),
                str(target.get("flow_unit_id") or target.get("section_id") or target.get("column_id") or ""),
                str(target.get("source_id") or target.get("asset_block_id") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                "<tr>"
                f"<td>{_esc(source_name)}</td>"
                f"<td>{_esc(target.get('promotion') or ('required' if target.get('required_co_repair_eligible', True) else 'advisory'))}</td>"
                f"<td>{_esc(target.get('target_kind'))}</td>"
                f"<td>{_esc(target.get('flow_unit_id') or target.get('section_id') or target.get('column_id'))}</td>"
                f"<td>{_esc(target.get('source_id'))}</td>"
                f"<td>{_esc(target.get('words_to_add_min'))}-{_esc(target.get('words_to_add_max'))}</td>"
                f"<td>{_esc(target.get('visual_salience_score'))}</td>"
                f"<td>{_esc(target.get('blank_fill_severity') or target.get('promotion'))}</td>"
                f"<td>{_esc(target.get('over_readout_budget'))}</td>"
                f"<td>{_esc(target.get('required_repair_mode'))}</td>"
                f"<td>{_esc(target.get('compact_rebalance_required'))}</td>"
                f"<td>{_esc(target.get('prose_fill_required'))}</td>"
                f"<td>{_esc(_blank_fill_soft_accept_effect(target, bucket=bucket, plan_required=bool(plan.get('blank_fill_required'))))}</td>"
                f"<td>{_esc(target.get('tail_gap_confidence'))}</td>"
                f"<td>{_esc(target.get('content_bottom_source'))}</td>"
                f"<td>{_esc(target.get('primary_repair_action'))}</td>"
                f"<td><code>{_esc(target.get('insert_selector'))}</code></td>"
                "</tr>"
            )
    if not rows:
        return "<p class='muted'>No blank-fill plan targets in this feedback.</p>"
    return (
        "<table><thead><tr><th>Source</th><th>Promotion</th><th>Kind</th><th>Target</th>"
        "<th>Source id</th><th>Words</th><th>Salience</th><th>Severity</th><th>Over budget</th>"
        "<th>Repair mode</th><th>Compact/rebalance</th><th>Prose required</th><th>Soft-accept effect</th>"
        "<th>Confidence</th><th>BBox source</th>"
        "<th>Action</th><th>Insert selector</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _has_blank_fill_targets(plan: Any) -> bool:
    if not isinstance(plan, dict):
        return False
    for key in ("targets", "required_targets", "advisory_targets", "suppressed_targets"):
        if any(isinstance(item, dict) for item in (plan.get(key) or [])):
            return True
    return False


def _blank_fill_plan_targets(plan: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    targets: list[tuple[str, dict[str, Any]]] = []
    for bucket in ("targets", "required_targets", "advisory_targets", "suppressed_targets"):
        for target in plan.get(bucket) or []:
            if isinstance(target, dict):
                targets.append((bucket, target))
    return targets


def _blank_fill_soft_accept_effect(
    target: dict[str, Any],
    *,
    bucket: str = "targets",
    plan_required: bool = False,
) -> str:
    if target.get("required_co_repair_eligible") is False:
        reason = target.get("required_demoted_reason") or "required_co_repair_eligible=false"
        return f"does not block: advisory/suppressed ({reason})"
    promotion = str(target.get("promotion") or target.get("blank_fill_severity") or "").lower()
    if bucket in {"advisory_targets", "suppressed_targets"} or promotion == "advisory":
        reason = target.get("required_demoted_reason") or target.get("safe_primary_repair_action") or "advisory target"
        return f"does not block: {reason}"
    compact = bool(target.get("compact_rebalance_required"))
    repair_mode = str(target.get("required_repair_mode") or "")
    if any(marker in repair_mode for marker in ("compact", "rebalance", "stack", "reduce")):
        compact = True
    if compact:
        reason = target.get("required_repair_reason") or "required compact/rebalance blank-fill target"
        return f"blocks: {reason}; do not prose-stuff"
    if target.get("prose_fill_required") is True:
        return "blocks: required source-backed prose/native fill target"
    if promotion == "required" or bucket == "required_targets" or (plan_required and bucket == "targets"):
        return "blocks: required local blank-fill target"
    return "does not block: no required blank-fill marker"


def _density_conservation_rows(data: dict[str, Any]) -> str:
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    density = payload.get("density_conservation")
    if not isinstance(density, dict):
        density = summary.get("density_conservation") if isinstance(summary.get("density_conservation"), dict) else {}
    if not density:
        return "<p class='muted'>No density conservation payload in this feedback.</p>"
    rows: list[str] = []
    for label, items in (
        ("hard-required", density.get("required_blank_fill_issues")),
        ("suppressed-advisory", density.get("suppressed_advisory_issues") or density.get("suppressed_editorial_blank_diagnostics")),
    ):
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            target = item.get("blank_fill_target") if isinstance(item.get("blank_fill_target"), dict) else item
            rows.append(
                "<tr>"
                f"<td>{_esc(label)}</td>"
                f"<td>{_esc(item.get('id') or item.get('original_issue_id'))}</td>"
                f"<td>{_esc(item.get('suppressed_reason') or item.get('suppression_reason'))}</td>"
                f"<td>{_esc(target.get('target_kind'))}</td>"
                f"<td>{_esc(target.get('flow_unit_id') or target.get('section_id') or target.get('column_id'))}</td>"
                f"<td>{_esc(target.get('promotion') or target.get('blank_fill_severity'))}</td>"
                f"<td>{_esc(target.get('visual_salience_score'))}</td>"
                f"<td>{_esc(target.get('over_readout_budget'))}</td>"
                f"<td>{_esc(target.get('required_repair_mode'))}</td>"
                f"<td>{_esc(target.get('compact_rebalance_required'))}</td>"
                f"<td>{_esc(target.get('prose_fill_required'))}</td>"
                f"<td>{_esc(_blank_fill_soft_accept_effect(target, bucket='targets'))}</td>"
                "</tr>"
            )
    if not rows:
        return "<p class='muted'>Density conservation has no required or suppressed blank-fill issues.</p>"
    return (
        "<table><thead><tr><th>Class</th><th>Issue</th><th>Reason</th><th>Target kind</th>"
        "<th>Target</th><th>Promotion</th><th>Salience</th><th>Over budget</th><th>Repair mode</th>"
        "<th>Compact/rebalance</th><th>Prose required</th><th>Soft-accept effect</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _typography_rows(data: dict[str, Any], attempt_dir: Path) -> str:
    repair = _read_json(attempt_dir / "auto_repair_typography_line_height.json")
    rows: list[str] = []
    if repair:
        for target in repair.get("targets") or []:
            if not isinstance(target, dict):
                continue
            rows.append(
                "<tr>"
                "<td>auto_micro_repair</td>"
                f"<td>{_esc(target.get('block_id'))}</td>"
                f"<td>{_esc(target.get('actual_line_height'))}</td>"
                f"<td>{_esc(target.get('target_line_height'))}</td>"
                "<td>scoped CSS then revalidate</td>"
                "</tr>"
            )
    for issue in _issues(data):
        if not isinstance(issue, dict) or str(issue.get("failure_kind") or "") != "body_line_height_unsafe":
            continue
        ratio = issue.get("actual_line_height")
        micro = ""
        try:
            value = float(ratio)
            if 0.98 <= value < 1.04 or 1.35 < value <= 1.45:
                micro = "eligible if attempt >= 5 and no hard secondary/required blank-fill"
        except (TypeError, ValueError):
            pass
        rows.append(
            "<tr>"
            "<td>feedback</td>"
            f"<td>{_esc(issue.get('block_id'))}</td>"
            f"<td>{_esc(ratio)}</td>"
            f"<td>{_esc(issue.get('severity'))}</td>"
            f"<td>{_esc(micro)}</td>"
            "</tr>"
        )
    if not rows:
        return "<p class='muted'>No typography line-height issue or auto repair record.</p>"
    return (
        "<table><thead><tr><th>Source</th><th>Block</th><th>Actual</th><th>Target / Severity</th>"
        "<th>Micro repair</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _local_overflow_rows(data: dict[str, Any]) -> str:
    rows: list[str] = []
    for issue in _issues(data):
        if not isinstance(issue, dict):
            continue
        if not (
            "overflow" in str(issue.get("failure_kind") or "").lower()
            or issue.get("bottom_overflow_px") is not None
            or issue.get("scroll_overflow_px") is not None
        ):
            continue
        rows.append(
            "<tr>"
            f"<td>{_esc(issue.get('container_kind') or issue.get('role'))}</td>"
            f"<td>{_esc(issue.get('section_id') or issue.get('container_id') or issue.get('block_id'))}</td>"
            f"<td>{_esc(issue.get('bottom_overflow_px'))}</td>"
            f"<td>{_esc(issue.get('severity'))}</td>"
            f"<td>{_esc(issue.get('soft_finalizable'))}</td>"
            f"<td>{_esc(issue.get('visible_overflow'))}</td>"
            f"<td>{_esc(issue.get('near_miss_threshold_px'))}</td>"
            f"<td>{_esc(issue.get('sample_text') or issue.get('text'))}</td>"
            "</tr>"
        )
    if not rows:
        return "<p class='muted'>No local overflow issue rows in this feedback.</p>"
    return (
        "<table><thead><tr><th>Container</th><th>Target</th><th>Bottom overflow</th>"
        "<th>Severity</th><th>Soft-finalizable</th><th>Visible overflow</th>"
        "<th>Near-miss threshold</th><th>Sample</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _identity_header_rows(data: dict[str, Any]) -> str:
    rows: list[str] = []
    issue_id = _issue_id(data)
    for issue in _issues(data):
        if not isinstance(issue, dict):
            continue
        row_id = str(issue.get("id") or issue.get("failure_kind") or "")
        if issue_id != "paper_poster_html_identity_header_only_failed" and not row_id.startswith("identity_header"):
            continue
        rows.append(
            "<tr>"
            f"<td>{_esc(row_id)}</td>"
            f"<td>{_esc(issue.get('header_id'))}</td>"
            f"<td>{_esc(issue.get('block_id'))}</td>"
            f"<td>{_esc(issue.get('role'))}</td>"
            f"<td>{_esc(issue.get('text') or issue.get('source_id'))}</td>"
            f"<td>{_esc(issue.get('repair'))}</td>"
            "</tr>"
        )
    if not rows:
        return "<p class='muted'>No identity-header contract issue rows in this feedback.</p>"
    return (
        "<table><thead><tr><th>Issue</th><th>Header</th><th>Block</th>"
        "<th>Role</th><th>Text/source</th><th>Repair</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _logo_resolver_rows(manifest: dict[str, Any], attempt_dir: Path) -> str:
    resolution = _read_json(attempt_dir / "designer_author_logo_candidate_resolution.json")
    identity = _read_json(attempt_dir / "academic_identity_assets.json")
    rows: list[str] = []
    assets_by_id = {
        str(asset.get("asset_id") or ""): asset
        for asset in (identity.get("assets") or [])
        if isinstance(asset, dict)
    }

    def add_row(
        source: str,
        entity: Any,
        role: Any,
        status: Any,
        asset_id: Any,
        local_path: Any,
        final_html_src: Any,
        data_source_id: Any,
        data_layer_id: Any,
        source_url_exposed: Any,
        source_url: Any,
        note: Any = "",
    ) -> None:
        local = str(local_path or "")
        local_only = _local_asset_status(local, attempt_dir)
        rows.append(
            "<tr>"
            f"<td>{_esc(source)}</td>"
            f"<td>{_esc(entity)}</td>"
            f"<td>{_esc(role)}</td>"
            f"<td>{_esc(status)}</td>"
            f"<td>{_esc(asset_id)}</td>"
            f"<td>{_file_link(attempt_dir, local)}</td>"
            f"<td>{_esc(local_only)}</td>"
            f"<td>{_esc(final_html_src)}</td>"
            f"<td>{_esc(data_source_id)}</td>"
            f"<td>{_esc(data_layer_id)}</td>"
            f"<td>{_esc(source_url_exposed)}</td>"
            f"<td>{_esc(source_url)}</td>"
            f"<td>{_esc(_clip(note, 260))}</td>"
            "</tr>"
        )

    for result in resolution.get("results") or []:
        if not isinstance(result, dict):
            continue
        asset_id = str(result.get("resolved_asset_id") or result.get("asset_id") or "")
        asset = assets_by_id.get(asset_id, {})
        add_row(
            "resolver",
            result.get("entity_name"),
            result.get("role"),
            result.get("status"),
            asset_id,
            asset.get("local_asset_path"),
            asset.get("final_html_src") or asset.get("local_asset_path"),
            asset.get("data_source_id") or asset.get("rendered_layer_id") or asset_id,
            asset.get("data_layer_id") or asset.get("rendered_layer_id") or asset_id,
            asset.get("source_url_exposed_to_author"),
            result.get("source_url"),
            result.get("reason") or result.get("message"),
        )

    heading = manifest.get("heading_identity_assets") if isinstance(manifest.get("heading_identity_assets"), dict) else {}
    for label, key in (
        ("manifest.required", "must_use_local_logo_assets"),
        ("manifest.optional", "optional_identity_assets"),
        ("manifest.fallback", "fallback_text_badges"),
    ):
        for item in heading.get(key) or []:
            if not isinstance(item, dict):
                continue
            add_row(
                label,
                item.get("entity_name") or item.get("label"),
                item.get("role"),
                "required" if item.get("required_to_place") else "context",
                item.get("asset_id"),
                item.get("local_asset_path"),
                item.get("final_html_src") or item.get("local_asset_path"),
                item.get("data_source_id"),
                item.get("data_layer_id"),
                item.get("source_url_exposed_to_author"),
                item.get("source_url"),
                item.get("placement_intent") or item.get("identity_group"),
            )

    if not rows:
        return "<p class='muted'>No designer logo resolver or heading identity diagnostics staged for this attempt.</p>"
    return (
        "<table><thead><tr><th>Source</th><th>Entity</th><th>Role</th><th>Status</th>"
        "<th>Asset id</th><th>Local path</th><th>Local-only check</th><th>Final HTML src</th>"
        "<th>data-source-id</th><th>data-layer-id</th><th>Remote exposed</th><th>Source URL</th><th>Note</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _poster_image_ref_rows(attempt_dir: Path) -> str:
    poster = _read_text(attempt_dir / "poster.html")
    if not poster:
        return "<p class='muted'>No poster.html staged for this attempt.</p>"
    refs: list[tuple[str, str]] = []
    for match in re.finditer(r"""<img\b[^>]*\bsrc\s*=\s*["']([^"']+)["']""", poster, flags=re.IGNORECASE):
        refs.append(("img", match.group(1)))
    for match in re.finditer(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)""", poster, flags=re.IGNORECASE):
        refs.append(("css-url", match.group(1)))
    rows = []
    for kind, src in refs[:40]:
        rows.append(
            "<tr>"
            f"<td>{_esc(kind)}</td>"
            f"<td><code>{_esc(src)}</code></td>"
            f"<td>{_esc(_image_ref_contract_status(src, attempt_dir))}</td>"
            "</tr>"
        )
    if not rows:
        return "<p class='muted'>No image references found in poster.html.</p>"
    return (
        "<table><thead><tr><th>Ref kind</th><th>src/url()</th><th>Local-logo contract status</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _local_asset_status(local_path: str, attempt_dir: Path) -> str:
    if not local_path:
        return ""
    lowered = local_path.lower()
    if lowered.startswith(("http://", "https://", "//", "data:", "file:", "javascript:", "blob:")):
        return "unsafe-nonlocal"
    return "local-present" if (attempt_dir / local_path).exists() else "local-path-missing"


def _image_ref_contract_status(src: str, attempt_dir: Path) -> str:
    lowered = src.strip().lower()
    if not lowered:
        return ""
    if lowered.startswith(("http://", "https://", "//", "data:", "file:", "javascript:", "blob:")):
        return "unsafe-nonlocal-final-ref"
    if src.startswith("/") or re.match(r"^[a-zA-Z]:[\\/]", src):
        return "unsafe-absolute-final-ref"
    if "{{layer:" in src:
        return "unresolved-layer-template"
    return "local-present" if (attempt_dir / src).exists() else "local-path-missing"


def _designer_context_overview(context: dict[str, Any]) -> str:
    if not context:
        return "<p class='muted'>No repair_context.json staged for this attempt.</p>"
    packet = context.get("visual_repair_packet") if isinstance(context.get("visual_repair_packet"), dict) else {}
    rows = [
        ("classification", context.get("classification")),
        ("primary_blocking_issue_id", context.get("primary_blocking_issue_id") or packet.get("primary_blocking_issue_id")),
        ("issue_id", context.get("issue_id")),
        ("repair_route", context.get("repair_route")),
        ("blank_fill_required", context.get("blank_fill_required") or packet.get("blank_fill_required")),
        ("secondary_diagnostics_are_advisory", packet.get("secondary_diagnostics_are_advisory")),
    ]
    scope = context.get("repair_scope") if isinstance(context.get("repair_scope"), dict) else {}
    if scope:
        rows.extend([
            ("repair_scope.mode", scope.get("mode")),
            ("repair_scope.target_block_ids", ", ".join(map(str, scope.get("target_block_ids") or []))),
            ("repair_scope.allowed_selectors", ", ".join(map(str, scope.get("allowed_selectors") or []))),
            ("repair_scope.forbidden_selectors", ", ".join(map(str, scope.get("forbidden_selectors") or []))),
        ])
    return (
        "<table><thead><tr><th>Context field</th><th>Value sent to designer</th></tr></thead><tbody>"
        + "".join(f"<tr><td><code>{_esc(key)}</code></td><td>{_esc(_clip(value, 520))}</td></tr>" for key, value in rows if value not in (None, "", []))
        + "</tbody></table>"
    )


def _collect_context_targets(context: dict[str, Any]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []

    def add_plan_targets(source: str, plan: Any) -> None:
        if not isinstance(plan, dict):
            return
        for bucket, target in _blank_fill_plan_targets(plan):
            if not isinstance(target, dict):
                continue
            target_source = source if bucket == "targets" else f"{source}.{bucket}"
            targets.append({
                "source": target_source,
                "kind": target.get("target_kind") or target.get("target_problem") or target.get("failure_kind"),
                "target": target.get("flow_unit_id") or target.get("section_id") or target.get("panel_id") or target.get("block_id") or target.get("column_id"),
                "source_id": target.get("source_id"),
                "words": _word_range(target),
                "severity": target.get("blank_fill_severity") or target.get("promotion") or target.get("severity"),
                "salience": target.get("visual_salience_score"),
                "repair_mode": target.get("required_repair_mode"),
                "compact_rebalance": target.get("compact_rebalance_required"),
                "prose_required": target.get("prose_fill_required"),
                "soft_accept": _blank_fill_soft_accept_effect(target),
                "selector": target.get("insert_selector") or _first_item(target.get("allowed_selectors")),
                "action": target.get("primary_repair_action") or target.get("recommended_first_action") or target.get("repair"),
                "forbidden": ", ".join(map(str, target.get("forbidden_selectors") or [])),
            })

    def add_issue_targets(source: str, issues: Any) -> None:
        for issue in issues or []:
            if not isinstance(issue, dict):
                continue
            if isinstance(issue.get("blank_fill_target"), dict):
                add_plan_targets(f"{source}.blank_fill_target", {"targets": [issue["blank_fill_target"]]})
            target_kind = issue.get("target_problem") or issue.get("failure_kind") or issue.get("id")
            if target_kind and (
                issue.get("source_id")
                or issue.get("flow_unit_id")
                or issue.get("section_id")
                or issue.get("panel_id")
                or issue.get("block_id")
            ):
                targets.append({
                    "source": source,
                    "kind": target_kind,
                    "target": issue.get("flow_unit_id") or issue.get("section_id") or issue.get("panel_id") or issue.get("block_id"),
                    "source_id": issue.get("source_id"),
                    "words": _word_range(issue),
                    "severity": issue.get("blank_fill_severity") or issue.get("promotion") or issue.get("severity"),
                    "salience": issue.get("visual_salience_score"),
                    "repair_mode": issue.get("required_repair_mode"),
                    "compact_rebalance": issue.get("compact_rebalance_required"),
                    "prose_required": issue.get("prose_fill_required"),
                    "soft_accept": _blank_fill_soft_accept_effect(issue),
                    "selector": issue.get("insert_selector") or _first_item(issue.get("allowed_selectors")),
                    "action": issue.get("primary_repair_action") or issue.get("recommended_first_action") or issue.get("repair"),
                    "forbidden": ", ".join(map(str, issue.get("forbidden_selectors") or [])),
                })

    add_plan_targets("repair_context.blank_fill", context.get("blank_fill"))
    for key in ("required_co_repair", "post_overflow_required_followup"):
        container = context.get(key)
        if isinstance(container, dict):
            add_plan_targets(f"repair_context.{key}.blank_fill", container.get("blank_fill"))
    packet = context.get("visual_repair_packet") if isinstance(context.get("visual_repair_packet"), dict) else {}
    add_plan_targets("visual_packet.blank_fill_plan", packet.get("blank_fill_plan"))
    add_plan_targets("visual_packet.advisory_blank_fill_plan", packet.get("advisory_blank_fill_plan"))
    if isinstance(context.get("source_visual_sizing"), dict):
        add_plan_targets("repair_context.source_visual_sizing", context["source_visual_sizing"])
    if isinstance(context.get("source_wrap"), dict):
        add_plan_targets("repair_context.source_wrap", context["source_wrap"])
    add_issue_targets("repair_context.issues", context.get("issues"))
    for diag in context.get("secondary_gate_issues") or []:
        if isinstance(diag, dict):
            add_issue_targets(f"secondary.{diag.get('stage')}", diag.get("issues"))

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for target in targets:
        key = (
            str(target.get("source") or ""),
            str(target.get("kind") or ""),
            str(target.get("target") or ""),
            str(target.get("source_id") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(target)
    return deduped


def _word_range(target: dict[str, Any]) -> str:
    lo = target.get("words_to_add_min")
    hi = target.get("words_to_add_max")
    if lo is None and hi is None:
        return ""
    return f"{lo}-{hi}"


def _first_item(value: Any) -> Any:
    if isinstance(value, list) and value:
        return value[0]
    return ""


def _designer_target_rows(context: dict[str, Any]) -> str:
    targets = _collect_context_targets(context)
    if not targets:
        return "<p class='muted'>No structured local repair targets found in repair_context.json.</p>"
    rows = []
    for target in targets[:18]:
        rows.append(
            "<tr>"
            f"<td>{_esc(target.get('source'))}</td>"
            f"<td>{_esc(target.get('kind'))}</td>"
            f"<td>{_esc(target.get('target'))}</td>"
            f"<td>{_esc(target.get('source_id'))}</td>"
            f"<td>{_esc(target.get('words'))}</td>"
            f"<td>{_esc(target.get('severity'))}</td>"
            f"<td>{_esc(target.get('salience'))}</td>"
            f"<td>{_esc(target.get('repair_mode'))}</td>"
            f"<td>{_esc(target.get('compact_rebalance'))}</td>"
            f"<td>{_esc(target.get('prose_required'))}</td>"
            f"<td>{_esc(target.get('soft_accept'))}</td>"
            f"<td><code>{_esc(target.get('selector'))}</code></td>"
            f"<td>{_esc(_clip(target.get('action'), 420))}</td>"
            f"<td><code>{_esc(_clip(target.get('forbidden'), 260))}</code></td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Context source</th><th>Kind</th><th>Target id</th>"
        "<th>Source id</th><th>Words</th><th>Severity</th><th>Salience</th><th>Repair mode</th>"
        "<th>Compact/rebalance</th><th>Prose required</th><th>Soft-accept effect</th><th>Selector</th>"
        "<th>Designer action</th><th>Forbidden scope</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _manifest_context(manifest: dict[str, Any], packet: dict[str, Any], attempt_dir: Path) -> str:
    if not manifest and not packet:
        return "<p class='muted'>No author_input_manifest.json or visual_repair_packet.json staged for this attempt.</p>"
    must_read = manifest.get("must_read_first") if isinstance(manifest.get("must_read_first"), list) else []
    repair_inputs = manifest.get("repair_inputs") if isinstance(manifest.get("repair_inputs"), list) else []
    sections = [
        "<h4>Must-read files</h4>",
        _chip_list(must_read[:18]),
        "<h4>Repair input files</h4>",
        _chip_list(repair_inputs[:24]),
        "<h4>Required images</h4>",
        _image_contract_rows(manifest.get("must_read_first_images") or packet.get("must_read_images") or [], attempt_dir),
        "<h4>Advisory images</h4>",
        _image_contract_rows(packet.get("advisory_images") or [], attempt_dir),
        "<h4>Reference images</h4>",
        _image_contract_rows(packet.get("reference_images") or [], attempt_dir),
    ]
    return "".join(sections)


def _chip_list(items: list[Any]) -> str:
    if not items:
        return "<p class='muted'>None.</p>"
    return "<div class='chips'>" + "".join(f"<code>{_esc(item)}</code>" for item in items) + "</div>"


def _image_contract_rows(items: Any, attempt_dir: Path) -> str:
    if not isinstance(items, list) or not items:
        return "<p class='muted'>None.</p>"
    rows = []
    for item in items[:16]:
        if not isinstance(item, dict):
            continue
        rows.append(
            "<tr>"
            f"<td>{_file_link(attempt_dir, item.get('path'))}</td>"
            f"<td>{_esc(item.get('role'))}</td>"
            f"<td>{_esc(item.get('issue_id'))}</td>"
            f"<td>{_esc(item.get('stage'))}</td>"
            f"<td>{_esc(item.get('diagnostic_only'))}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Path</th><th>Role</th><th>Issue</th><th>Stage</th><th>Diagnostic only</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _prompt_context_snippets(prompt_path: Path) -> str:
    text = _read_text(prompt_path)
    if not text:
        return "<p class='muted'>No designer_author_prompt.md staged for this attempt.</p>"
    anchors = (
        "Read repair_context.json",
        "Fix primary_blocking_issue_id",
        "Visual repair packet:",
        "must_read_images",
        "This is a ",
        "blank-fill",
        "Blank-fill",
        "source-flow",
        "direct sibling",
        "Header/title band",
        "do not change .poster-columns",
        "Do not solve",
        "secondary diagnostics",
        "designer_author_logo_candidates.json",
    )
    rows = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if any(anchor in stripped for anchor in anchors):
            rows.append(
                "<tr>"
                f"<td>{line_no}</td>"
                f"<td>{_esc(_clip(stripped, 760))}</td>"
                "</tr>"
            )
        if len(rows) >= 42:
            break
    if not rows:
        return "<p class='muted'>No matching repair-context prompt snippets found.</p>"
    return (
        "<table><thead><tr><th>Line</th><th>Prompt text sent to designer</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _packet_groups(packet: dict[str, Any], attempt_dir: Path) -> str:
    groups = _normalized_packet_image_groups(packet)
    sections: list[str] = []
    for title, key in (
        ("Primary repair images", "primary"),
        ("Advisory images", "advisory_images"),
        ("Reference images", "reference_images"),
    ):
        items = groups.get(key) or []
        figs = []
        for item in items[:6]:
            if not isinstance(item, dict):
                continue
            rel = str(item.get("path") or "")
            figs.append(_img(attempt_dir / rel, f"{item.get('role')} / {item.get('issue_id')}"))
        if figs:
            sections.append(f"<h4>{_esc(title)}</h4><div class='gallery'>{''.join(figs)}</div>")
    return "".join(sections) or "<p class='muted'>No staged visual packet image groups.</p>"


def _normalized_packet_image_groups(packet: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    image_map = packet.get("image_issue_map") if isinstance(packet.get("image_issue_map"), list) else []
    if not image_map:
        return {
            "primary": packet.get("must_read_images") if isinstance(packet.get("must_read_images"), list) else [],
            "advisory_images": packet.get("advisory_images") if isinstance(packet.get("advisory_images"), list) else [],
            "reference_images": packet.get("reference_images") if isinstance(packet.get("reference_images"), list) else [],
        }
    groups: dict[str, list[dict[str, Any]]] = {"primary": [], "advisory_images": [], "reference_images": []}
    for item in image_map:
        if not isinstance(item, dict) or not item.get("path"):
            continue
        role = str(item.get("role") or "")
        path = str(item.get("path") or "")
        candidate = str(item.get("candidate") or "")
        diagnostic = bool(item.get("diagnostic_only")) or role.startswith("secondary") or "/secondary_" in path
        if candidate == "locked_base_candidate" or path.startswith("locked_base") or "/locked_base_" in path:
            bucket = "reference_images"
        elif diagnostic:
            bucket = "advisory_images"
        else:
            bucket = "primary"
        groups[bucket].append(item)
    return groups


def build_report(run_dir: Path, attempts: tuple[str, ...], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    cards: list[str] = []
    for attempt in attempts:
        attempt_dir = run_dir / "designer_author" / attempt
        feedback = _read_json(attempt_dir / "validation_feedback.json")
        measurement = _read_json(attempt_dir / "candidate_measurement.json")
        packet = _read_json(attempt_dir / "visual_repair_packet.json")
        repair_context = _read_json(attempt_dir / "repair_context.json")
        manifest = _read_json(attempt_dir / "author_input_manifest.json")
        issue = _issue_id(feedback)
        cards.append(
            "<section class='card'>"
            f"<h2>{_esc(attempt)} <span>{_esc(issue or 'no validation feedback')}</span></h2>"
            f"{_heading_downgrade_note(measurement)}"
            "<h3>Visual Packet Groups</h3>"
            f"{_packet_groups(packet, attempt_dir)}"
            "<h3>Local Overflow Severity</h3>"
            f"{_local_overflow_rows(feedback)}"
            "<h3>Identity Header Contract</h3>"
            f"{_identity_header_rows(feedback)}"
            "<h3>Logo Resolver Diagnostics</h3>"
            f"{_logo_resolver_rows(manifest, attempt_dir)}"
            "<h3>Final Poster Image Ref Contract</h3>"
            f"{_poster_image_ref_rows(attempt_dir)}"
            "<h3>Source Visual Repair Semantics</h3>"
            f"{_source_visual_rows(feedback, packet)}"
            "<h3>Typography Micro Repair</h3>"
            f"{_typography_rows(feedback, attempt_dir)}"
            "<h3>Blank-Fill Targets</h3>"
            f"{_blank_fill_rows(feedback, packet)}"
            "<h3>Density Conservation</h3>"
            f"{_density_conservation_rows(feedback)}"
            "<h3>Designer Context Contract</h3>"
            f"{_designer_context_overview(repair_context)}"
            "<h3>Designer Structured Targets</h3>"
            f"{_designer_target_rows(repair_context)}"
            "<h3>Designer Input Manifest & Images</h3>"
            f"{_manifest_context(manifest, packet, attempt_dir)}"
            "<h3>Repair Prompt Excerpts</h3>"
            f"{_prompt_context_snippets(attempt_dir / 'designer_author_prompt.md')}"
            "</section>"
        )
    html_text = (
        "<!doctype html><meta charset='utf-8'><title>Poster Repair Feedback Visualization</title>"
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:24px;background:#f8fafc;color:#111827}"
        "h1{font-size:24px}h2{font-size:20px;margin:0 0 12px}h2 span{font-size:13px;color:#475569;font-weight:500}"
        "h3{margin-top:24px;border-top:1px solid #e5e7eb;padding-top:14px}h4{margin:14px 0 8px;color:#334155}"
        ".card{background:white;border:1px solid #d8dee8;border-radius:8px;padding:18px;margin:16px 0}"
        ".gallery{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}"
        "figure{margin:0;border:1px solid #e5e7eb;background:#fff;padding:8px}img{max-width:100%;display:block}figcaption{font-size:12px;color:#64748b;margin-top:6px}"
        "table{border-collapse:collapse;width:100%;font-size:13px}th,td{border:1px solid #e5e7eb;padding:6px;text-align:left;vertical-align:top}"
        "th{background:#f1f5f9}code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;background:#f8fafc;border:1px solid #e5e7eb;border-radius:4px;padding:1px 3px}"
        ".chips{display:flex;flex-wrap:wrap;gap:6px}.chips code{display:inline-block}"
        ".ok{background:#ecfdf5;border-left:4px solid #10b981;padding:10px;margin:8px 0}.muted,.missing{color:#64748b}</style>"
        f"<h1>Poster Repair Feedback Visualization</h1><p>Run: <code>{_esc(run_dir)}</code></p>{''.join(cards)}"
    )
    out = output_dir / "index.html"
    out.write_text(html_text, encoding="utf-8")
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", default="out/runs/20260618-134322-21c7f55f")
    parser.add_argument("--attempt", action="append", dest="attempts")
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    run_dir = Path(args.run_dir)
    attempts = tuple(args.attempts or DEFAULT_ATTEMPTS)
    output_dir = Path(args.output_dir or f"out/diagnostics/poster_repair_feedback_loop_{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    out = build_report(run_dir, attempts, output_dir)
    print(out)


if __name__ == "__main__":
    main()
