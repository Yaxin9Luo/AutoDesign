"""Deterministic gate audit for authored academic poster HTML.

This audit is intentionally AutoDesign-native. It measures the rendered
``.paper-poster`` contract and emits repair-oriented findings without relying
on external poster skill code or templates.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .browser_render import _launch_chromium
from .math_typesetting import wait_for_autodesign_math


_ROOT_TOLERANCE_PX = 2.0
_CLIP_TOLERANCE_PX = 4.0
_SEVERE_TEXT_OVERFLOW_RATIO = 0.22
_SEVERE_TEXT_HEIGHT_GAP_PX = 24.0
_TRAILING_CONTENT_MIN_RATIO = 0.80
_NATURAL_RENDERED_MIN_RATIO = 1.5
_TABLE_MIN_WIDTH_FILL_RATIO = 0.86
_TABLE_MIN_DEDICATED_HEIGHT_FILL_RATIO = 0.42
_TABLE_MIN_FONT_PX = 13.0
_SOURCE_FLOW_LIST_MIN_PADDING_PX = 18.0


def audit_authored_poster_gate(
    html_path: Path,
    *,
    canvas: dict[str, Any],
    poster_size: dict[str, Any] | None = None,
    dom_audit: dict[str, Any] | None = None,
    timeout_ms: int = 15_000,
) -> dict[str, Any]:
    """Return a machine-readable gate audit for an authored paper poster."""
    cw = int(canvas.get("w_px") or (poster_size or {}).get("canvas_w_px") or 0)
    ch = int(canvas.get("h_px") or (poster_size or {}).get("canvas_h_px") or 0)
    metrics: dict[str, Any] = {
        "canvas_w_px": cw,
        "canvas_h_px": ch,
    }
    if dom_audit:
        metrics["input_dom_layer_count"] = len(dom_audit.get("dom_layers") or [])
        metrics["input_dom_image_count"] = len(dom_audit.get("dom_images") or [])

    if cw <= 0 or ch <= 0 or not html_path.exists():
        return _payload(
            backend="unavailable",
            metrics=metrics,
            findings=[],
            warnings=["poster_gate_skipped_missing_canvas_or_html"],
        )

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return _payload(
            backend="unavailable",
            metrics=metrics,
            findings=[],
            warnings=[f"playwright_unavailable: {type(e).__name__}: {e}"],
        )

    try:
        with sync_playwright() as p:
            browser = _launch_chromium(p)
            page = browser.new_page(
                viewport={"width": max(1, cw), "height": max(1, ch)},
                device_scale_factor=1,
            )
            page.set_default_timeout(timeout_ms)
            page.set_default_navigation_timeout(timeout_ms)
            page.goto(html_path.resolve().as_uri(), wait_until="load", timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=min(2000, timeout_ms))
            except Exception:
                pass
            wait_for_autodesign_math(page, timeout_ms=min(3000, timeout_ms))
            screen_data = page.evaluate(_SNAPSHOT_JS)
            try:
                page.emulate_media(media="print")
                wait_for_autodesign_math(page, timeout_ms=min(3000, timeout_ms))
                print_data = page.evaluate(_ROOT_SNAPSHOT_JS)
            except Exception as e:
                print_data = {"warning": f"print_media_snapshot_failed: {type(e).__name__}: {e}"}
            browser.close()
    except Exception as e:
        return _payload(
            backend="unavailable",
            metrics=metrics,
            findings=[],
            warnings=[f"poster_gate_playwright_failed: {type(e).__name__}: {e}"],
        )

    warnings: list[str] = []
    if isinstance(print_data, dict) and print_data.get("warning"):
        warnings.append(str(print_data.get("warning")))
    if not isinstance(screen_data, dict) or screen_data.get("missingRoot"):
        findings = [_finding(
            "poster-gate-root-missing",
            "P0",
            "Rendered authored poster has no .paper-poster root.",
            "revise_authored_html",
        )]
        return _payload(backend="playwright", metrics=metrics, findings=findings, warnings=warnings)

    root = _as_dict(screen_data.get("root"))
    print_root = _as_dict(print_data.get("root") if isinstance(print_data, dict) else None)
    elements = [_as_dict(item) for item in screen_data.get("elements") or [] if isinstance(item, dict)]
    images = [_as_dict(item) for item in screen_data.get("images") or [] if isinstance(item, dict)]
    tables = [_as_dict(item) for item in screen_data.get("tables") or [] if isinstance(item, dict)]
    lists = [_as_dict(item) for item in screen_data.get("lists") or [] if isinstance(item, dict)]
    findings: list[dict[str, Any]] = []

    metrics.update(_root_metrics(root, prefix="screen"))
    if print_root:
        metrics.update(_root_metrics(print_root, prefix="print"))
    metrics["poster_gate_element_count"] = len(elements)
    metrics["poster_gate_image_count"] = len(images)
    metrics["poster_gate_table_count"] = len(tables)
    metrics["poster_gate_list_count"] = len(lists)

    _audit_root_fit(findings, root, print_root, cw=cw, ch=ch, poster_size=poster_size or {})
    _audit_hidden_clipping(findings, root, elements, cw=cw, ch=ch)
    _audit_trailing_panel_whitespace(findings, elements, cw=cw, ch=ch)
    _audit_figures(findings, images, cw=cw, ch=ch)
    _audit_tables(findings, tables, elements, cw=cw, ch=ch)
    _audit_source_flow_list_gutters(findings, lists)

    return _payload(
        backend="playwright",
        metrics=metrics,
        findings=findings,
        warnings=warnings,
    )


def _audit_root_fit(
    findings: list[dict[str, Any]],
    root: dict[str, Any],
    print_root: dict[str, Any],
    *,
    cw: int,
    ch: int,
    poster_size: dict[str, Any],
) -> None:
    _audit_root_dimensions(
        findings,
        root,
        media="screen",
        expected_w=cw,
        expected_h=ch,
        message="screen .paper-poster size does not match DesignSpec.canvas.",
        expected_label="canvas",
    )
    if not print_root:
        return
    _audit_print_root_dimensions(findings, print_root, cw=cw, ch=ch, poster_size=poster_size)


def _audit_root_dimensions(
    findings: list[dict[str, Any]],
    data: dict[str, Any],
    *,
    media: str,
    expected_w: float,
    expected_h: float,
    message: str,
    expected_label: str,
) -> None:
    if not data:
        return
    width_gap = abs(_float(data.get("w")) - expected_w)
    height_gap = abs(_float(data.get("h")) - expected_h)
    if width_gap <= _ROOT_TOLERANCE_PX and height_gap <= _ROOT_TOLERANCE_PX:
        return
    findings.append(_finding(
        "poster-gate-root-canvas-mismatch",
        "P0",
        message,
        "revise_authored_html",
        evidence={
            "media": media,
            "root_w_px": round(_float(data.get("w")), 2),
            "root_h_px": round(_float(data.get("h")), 2),
            f"expected_{expected_label}_w_px": round(float(expected_w), 2),
            f"expected_{expected_label}_h_px": round(float(expected_h), 2),
            "width_gap_px": round(width_gap, 2),
            "height_gap_px": round(height_gap, 2),
        },
    ))


def _audit_print_root_dimensions(
    findings: list[dict[str, Any]],
    data: dict[str, Any],
    *,
    cw: int,
    ch: int,
    poster_size: dict[str, Any],
) -> None:
    candidates: list[tuple[str, float, float]] = [("canvas", float(cw), float(ch))]
    print_w, print_h = _expected_print_css_px(poster_size)
    if print_w > 0 and print_h > 0:
        candidates.append(("print_page_css_px", print_w, print_h))
    root_w = _float(data.get("w"))
    root_h = _float(data.get("h"))
    for _, expected_w, expected_h in candidates:
        if abs(root_w - expected_w) <= _ROOT_TOLERANCE_PX and abs(root_h - expected_h) <= _ROOT_TOLERANCE_PX:
            return
    evidence: dict[str, Any] = {
        "media": "print",
        "root_w_px": round(root_w, 2),
        "root_h_px": round(root_h, 2),
    }
    for label, expected_w, expected_h in candidates:
        evidence[f"expected_{label}_w_px"] = round(float(expected_w), 2)
        evidence[f"expected_{label}_h_px"] = round(float(expected_h), 2)
        evidence[f"{label}_width_gap_px"] = round(abs(root_w - expected_w), 2)
        evidence[f"{label}_height_gap_px"] = round(abs(root_h - expected_h), 2)
    findings.append(_finding(
        "poster-gate-root-canvas-mismatch",
        "P0",
        "print .paper-poster size matches neither DesignSpec.canvas nor resolved print page size.",
        "revise_authored_html",
        evidence=evidence,
    ))


def _expected_print_css_px(poster_size: dict[str, Any]) -> tuple[float, float]:
    width_mm = _float(poster_size.get("width_mm"))
    height_mm = _float(poster_size.get("height_mm"))
    if width_mm <= 0 or height_mm <= 0:
        return 0.0, 0.0
    return width_mm / 25.4 * 96.0, height_mm / 25.4 * 96.0


def _audit_hidden_clipping(
    findings: list[dict[str, Any]],
    root: dict[str, Any],
    elements: list[dict[str, Any]],
    *,
    cw: int,
    ch: int,
) -> None:
    root_candidate = {
        "block_id": "paper-poster-root",
        "tag": "main",
        "role": "poster",
        "text": "",
        "rect": {"x": 0, "y": 0, "w": _float(root.get("w")), "h": _float(root.get("h"))},
        "scrollWidth": root.get("scrollWidth"),
        "scrollHeight": root.get("scrollHeight"),
        "clientWidth": root.get("clientWidth"),
        "clientHeight": root.get("clientHeight"),
        "overflowX": root.get("overflowX") or "hidden",
        "overflowY": root.get("overflowY") or "hidden",
    }
    candidates = [root_candidate] + elements
    for el in candidates:
        overflow = _element_overflow(el)
        if overflow["max_gap_px"] <= _CLIP_TOLERANCE_PX:
            continue
        if not _has_non_visible_overflow(el):
            continue
        text_like = _is_text_like(el)
        severe = (
            overflow["overflow_ratio"] >= _SEVERE_TEXT_OVERFLOW_RATIO
            or overflow["height_gap_px"] >= _SEVERE_TEXT_HEIGHT_GAP_PX
            or (not text_like and overflow["max_gap_px"] >= max(24.0, min(cw, ch) * 0.035))
        )
        block_id = _element_id(el)
        findings.append(_finding(
            "poster-gate-hidden-clipping" if not text_like else "poster-gate-text-clipping",
            "P0" if severe else "P1",
            (
                f"Text block '{block_id}' has hidden scroll overflow."
                if text_like else f"Container '{block_id}' clips rendered content."
            ),
            "shrink_text" if text_like else "revise_authored_html",
            block_id=block_id if block_id != "paper-poster-root" else None,
            evidence=overflow,
        ))


def _audit_trailing_panel_whitespace(
    findings: list[dict[str, Any]],
    elements: list[dict[str, Any]],
    *,
    cw: int,
    ch: int,
) -> None:
    panels = [el for el in elements if _is_panel_like(el)]
    meaningful = [el for el in elements if _is_meaningful_content(el)]
    for panel in panels:
        panel_rect = _rect(panel.get("rect"))
        if panel_rect["w"] <= 0 or panel_rect["h"] <= 0:
            continue
        area_ratio = (panel_rect["w"] * panel_rect["h"]) / float(max(1, cw * ch))
        if area_ratio < 0.035 or panel_rect["h"] < max(110.0, ch * 0.12):
            continue
        role_text = " ".join(str(panel.get(k) or "") for k in ("role", "panel_role", "class_name")).lower()
        if any(token in role_text for token in ("header", "footer", "title", "identity", "logo", "qr")):
            continue
        contained = [
            el for el in meaningful
            if el is not panel and _rect_center_inside(_rect(el.get("rect")), panel_rect)
        ]
        if not contained:
            continue
        content_bottom = max(_rect(el.get("rect"))["bottom"] for el in contained)
        bottom_ratio = (content_bottom - panel_rect["y"]) / max(1.0, panel_rect["h"])
        if bottom_ratio >= _TRAILING_CONTENT_MIN_RATIO:
            continue
        block_id = _element_id(panel)
        findings.append(_finding(
            "poster-gate-panel-trailing-whitespace",
            "P1",
            f"Panel '{block_id}' leaves a large unused trailing region below meaningful content.",
            "revise_authored_html",
            block_id=block_id,
            evidence={
                "content_bottom_ratio": round(bottom_ratio, 3),
                "threshold": _TRAILING_CONTENT_MIN_RATIO,
                "panel_area_ratio": round(area_ratio, 4),
                "panel_rect": _round_rect(panel_rect),
                "contained_content_count": len(contained),
            },
        ))


def _audit_figures(
    findings: list[dict[str, Any]],
    images: list[dict[str, Any]],
    *,
    cw: int,
    ch: int,
) -> None:
    for img in images:
        block_id = _element_id(img)
        rect = _rect(img.get("rect"))
        loaded = bool(img.get("complete")) and int(img.get("naturalWidth") or 0) > 0 and int(img.get("naturalHeight") or 0) > 0
        if not loaded:
            findings.append(_finding(
                "poster-gate-image-not-loaded",
                "P0",
                f"Image '{block_id}' did not load.",
                "swap_visual",
                block_id=block_id,
                evidence={
                    "src": str(img.get("src") or "")[:240],
                    "current_src": str(img.get("currentSrc") or "")[:240],
                    "complete": bool(img.get("complete")),
                    "natural_w_px": int(img.get("naturalWidth") or 0),
                    "natural_h_px": int(img.get("naturalHeight") or 0),
                    "rect": _round_rect(rect),
                },
            ))
            continue
        if not _is_source_figure_image(img):
            continue

        rendered_w = max(0.0, rect["w"])
        rendered_h = max(0.0, rect["h"])
        rendered_area_ratio = (rendered_w * rendered_h) / float(max(1, cw * ch))
        if rendered_w <= 0 or rendered_h <= 0:
            findings.append(_finding(
                "poster-gate-figure-zero-area",
                "P1",
                f"Source figure '{block_id}' renders with no visible area.",
                "resize_visual",
                block_id=block_id,
                evidence={"rect": _round_rect(rect)},
                fix="Resize the source figure box from its intrinsic aspect ratio inside the panel max constraints.",
            ))
            continue
        if rendered_area_ratio < 0.01 and max(rendered_w, rendered_h) < max(120.0, min(cw, ch) * 0.18):
            findings.append(_finding(
                "poster-gate-figure-undersized",
                "P1",
                f"Source figure '{block_id}' is too small to carry evidence at poster scale.",
                "resize_visual",
                block_id=block_id,
                evidence={
                    "rendered_area_ratio": round(rendered_area_ratio, 4),
                    "rect": _round_rect(rect),
                },
                fix="Increase the intrinsic-ratio figure box only until the source figure is readable without exceeding panel constraints.",
            ))

        natural_w = _float(img.get("naturalWidth"))
        natural_h = _float(img.get("naturalHeight"))
        width_ratio = natural_w / max(1.0, rendered_w)
        height_ratio = natural_h / max(1.0, rendered_h)
        if width_ratio < _NATURAL_RENDERED_MIN_RATIO or height_ratio < _NATURAL_RENDERED_MIN_RATIO:
            findings.append(_finding(
                "poster-gate-figure-low-resolution",
                "P1",
                f"Source figure '{block_id}' has low natural-pixel resolution for its rendered size.",
                "resize_visual",
                block_id=block_id,
                evidence={
                    "natural_w_px": int(natural_w),
                    "natural_h_px": int(natural_h),
                    "rendered_w_px": round(rendered_w, 2),
                    "rendered_h_px": round(rendered_h, 2),
                    "natural_to_rendered_w": round(width_ratio, 3),
                    "natural_to_rendered_h": round(height_ratio, 3),
                    "minimum_ratio": _NATURAL_RENDERED_MIN_RATIO,
                },
                fix="Reduce the rendered intrinsic-ratio figure box or choose a higher-resolution source for the same visual binding.",
            ))

        natural_aspect = natural_w / max(1.0, natural_h)
        rendered_aspect = rendered_w / max(1.0, rendered_h)
        aspect_gap = abs(natural_aspect - rendered_aspect) / max(0.01, natural_aspect)
        object_fit = str(img.get("objectFit") or "").strip().lower()
        if aspect_gap > 0.22:
            findings.append(_finding(
                "poster-gate-figure-aspect-mismatch",
                "P1",
                f"Source figure '{block_id}' is rendered in a box that does not follow the original figure aspect ratio.",
                "resize_visual",
                block_id=block_id,
                evidence={
                    "natural_aspect": round(natural_aspect, 4),
                    "rendered_aspect": round(rendered_aspect, 4),
                    "aspect_gap_ratio": round(aspect_gap, 3),
                    "object_fit": object_fit or "initial",
                    "repair_hint": "resize the figure box within the panel instead of cropping or stretching the source image",
                },
                fix="Recompute the figure box from the source image aspect ratio and fit it inside the panel/lane max box; do not stretch or cover-crop the source image.",
            ))


def _audit_tables(
    findings: list[dict[str, Any]],
    tables: list[dict[str, Any]],
    elements: list[dict[str, Any]],
    *,
    cw: int,
    ch: int,
) -> None:
    containers = [el for el in elements if _is_panel_like(el)]
    for table in tables:
        block_id = _element_id(table)
        rect = _rect(table.get("rect"))
        if rect["w"] <= 2 or rect["h"] <= 2:
            findings.append(_finding(
                "poster-gate-table-zero-area",
                "P1",
                f"Native table '{block_id}' renders with no visible area.",
                "resize_table",
                block_id=block_id,
                evidence={"rect": _round_rect(rect)},
                fix="Resize the native table block so it fills the intended card or panel lane.",
            ))
            continue

        container = _smallest_container_for_rect(rect, containers)
        if container is not None:
            container_rect = _rect(container.get("rect"))
            outside = _outside_gap(rect, container_rect)
            if outside["max_gap_px"] > max(8.0, min(container_rect["w"], container_rect["h"]) * 0.03):
                findings.append(_finding(
                    "poster-gate-table-outside-card",
                    "P1",
                    f"Native table '{block_id}' extends outside its card/panel.",
                    "resize_table",
                    block_id=block_id,
                    evidence={
                        **outside,
                        "table_rect": _round_rect(rect),
                        "container_rect": _round_rect(container_rect),
                        "container_id": _element_id(container),
                    },
                    fix="Constrain the table wrapper to the card/panel and let the table fill that wrapper instead of bleeding across neighboring content.",
                ))

            width_ratio = rect["w"] / max(1.0, container_rect["w"])
            height_ratio = rect["h"] / max(1.0, container_rect["h"])
            dedicated = _is_table_like(table) or _is_table_like(container)
            if width_ratio < _TABLE_MIN_WIDTH_FILL_RATIO or (
                dedicated and height_ratio < _TABLE_MIN_DEDICATED_HEIGHT_FILL_RATIO
            ):
                findings.append(_finding(
                    "poster-gate-table-underfilled-card",
                    "P1",
                    f"Native table '{block_id}' does not fill its table card/panel.",
                    "resize_table",
                    block_id=block_id,
                    evidence={
                        "width_fill_ratio": round(width_ratio, 3),
                        "min_width_fill_ratio": _TABLE_MIN_WIDTH_FILL_RATIO,
                        "height_fill_ratio": round(height_ratio, 3),
                        "min_dedicated_height_fill_ratio": _TABLE_MIN_DEDICATED_HEIGHT_FILL_RATIO if dedicated else None,
                        "dedicated_table_container": dedicated,
                        "row_count": int(table.get("row_count") or 0),
                        "column_count": int(table.get("column_count") or 0),
                        "table_rect": _round_rect(rect),
                        "container_rect": _round_rect(container_rect),
                        "container_id": _element_id(container),
                    },
                    fix=(
                        "Treat native tables like figures: reserve a table lane/card, set the wrapper and table "
                        "to fill its width and height, and tune rows, font, padding, or split wide tables instead "
                        "of leaving blank card area."
                    ),
                ))

        overflow = _element_overflow(table)
        if overflow["max_gap_px"] > _CLIP_TOLERANCE_PX and _has_non_visible_overflow(table):
            findings.append(_finding(
                "poster-gate-table-clipping",
                "P1",
                f"Native table '{block_id}' has clipped scroll overflow.",
                "resize_table",
                block_id=block_id,
                evidence=overflow,
                fix="Increase the table lane, reduce row/cell padding or font size, or split the table; do not hide clipped cells.",
            ))

        font_px = _float(table.get("font_px"))
        if font_px > 0 and font_px < _TABLE_MIN_FONT_PX:
            findings.append(_finding(
                "poster-gate-table-text-small",
                "P1",
                f"Native table '{block_id}' uses text that is too small for poster reading.",
                "resize_table",
                block_id=block_id,
                evidence={
                    "font_px": round(font_px, 2),
                    "minimum_font_px": _TABLE_MIN_FONT_PX,
                    "row_count": int(table.get("row_count") or 0),
                    "column_count": int(table.get("column_count") or 0),
                },
                fix="Use a larger table lane, fewer columns/rows, or a compact comparison table instead of shrinking table typography below the readability floor.",
            ))


def _audit_source_flow_list_gutters(
    findings: list[dict[str, Any]],
    lists: list[dict[str, Any]],
) -> None:
    risky: list[dict[str, Any]] = []
    for item in lists:
        if not bool(item.get("has_source_flow_ancestor")):
            continue
        if not bool(item.get("is_direct_source_flow_child")):
            continue
        if not bool(item.get("has_floated_source_sibling")):
            continue
        if int(item.get("item_count") or 0) <= 0:
            continue
        padding = max(_float(item.get("paddingInlineStartPx")), _float(item.get("paddingLeftPx")))
        text_indent = _float(item.get("textIndentPx"))
        if padding >= _SOURCE_FLOW_LIST_MIN_PADDING_PX and text_indent >= -1.0:
            continue
        risky.append({
            "element_id": str(item.get("element_id") or ""),
            "block_id": str(item.get("block_id") or ""),
            "tag": str(item.get("tag") or ""),
            "class_name": str(item.get("class_name") or ""),
            "source_flow_id": str(item.get("source_flow_id") or ""),
            "source_flow_class_name": str(item.get("source_flow_class_name") or ""),
            "padding_inline_start_px": round(padding, 2),
            "min_padding_inline_start_px": _SOURCE_FLOW_LIST_MIN_PADDING_PX,
            "text_indent_px": round(text_indent, 2),
            "display": str(item.get("display") or ""),
            "list_style_position": str(item.get("listStylePosition") or ""),
            "floated_source_sibling_count": int(item.get("floated_source_sibling_count") or 0),
            "item_count": int(item.get("item_count") or 0),
            "rect": _round_rect(_rect(item.get("rect"))),
        })
    if not risky:
        return
    findings.append(_finding(
        "paper_poster_source_flow_list_marker_gutter_low",
        "P1",
        "A floated source-flow bullet list does not reserve enough marker gutter.",
        "revise_authored_html",
        block_id=str(risky[0].get("block_id") or risky[0].get("element_id") or ""),
        evidence={"issues": risky[:8]},
        fix=(
            "Give source-flow sibling lists scoped CSS such as display:flow-root, "
            "padding-inline-start:1.25em, list-style-position:outside, and small li padding, "
            "or stack the source asset above the readout when the side lane is too narrow."
        ),
    ))


def _payload(
    *,
    backend: str,
    metrics: dict[str, Any],
    findings: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    p0_count = sum(1 for finding in findings if str(finding.get("severity") or "").upper() == "P0")
    p1_count = sum(1 for finding in findings if str(finding.get("severity") or "").upper() == "P1")
    warning_count = sum(1 for finding in findings if str(finding.get("severity") or "").lower() == "warning")
    return {
        "kind": "poster_gate_audit",
        "version": 1,
        "backend": backend,
        "metrics": metrics,
        "findings": findings,
        "warnings": warnings,
        "p0_count": p0_count,
        "p1_count": p1_count,
        "warning_count": warning_count,
    }


def _finding(
    finding_id: str,
    severity: str,
    message: str,
    repair_route: str,
    *,
    block_id: str | None = None,
    evidence: dict[str, Any] | None = None,
    fix: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": finding_id,
        "severity": severity,
        "message": message,
        "repair_route": repair_route,
        "stage": "rendering_export",
        "repairable": True,
    }
    if block_id:
        payload["block_id"] = block_id
    if evidence:
        payload["evidence"] = evidence
    if fix:
        payload["fix"] = fix
    return payload


def _root_metrics(root: dict[str, Any], *, prefix: str) -> dict[str, Any]:
    return {
        f"{prefix}_root_w_px": round(_float(root.get("w")), 2),
        f"{prefix}_root_h_px": round(_float(root.get("h")), 2),
        f"{prefix}_root_scroll_w_px": int(_float(root.get("scrollWidth"))),
        f"{prefix}_root_scroll_h_px": int(_float(root.get("scrollHeight"))),
        f"{prefix}_root_client_w_px": int(_float(root.get("clientWidth"))),
        f"{prefix}_root_client_h_px": int(_float(root.get("clientHeight"))),
    }


def _element_overflow(el: dict[str, Any]) -> dict[str, Any]:
    scroll_w = _float(el.get("scrollWidth"))
    scroll_h = _float(el.get("scrollHeight"))
    client_w = _float(el.get("clientWidth"))
    client_h = _float(el.get("clientHeight"))
    rect = _rect(el.get("rect"))
    width_gap = max(0.0, scroll_w - max(client_w, rect["w"]))
    height_gap = max(0.0, scroll_h - max(client_h, rect["h"]))
    overflow_area = max(0.0, scroll_w * scroll_h - max(client_w, rect["w"]) * max(client_h, rect["h"]))
    visible_area = max(1.0, max(client_w, rect["w"]) * max(client_h, rect["h"]))
    return {
        "block_id": _element_id(el),
        "overflow_x": str(el.get("overflowX") or ""),
        "overflow_y": str(el.get("overflowY") or ""),
        "scroll_width_px": round(scroll_w, 2),
        "scroll_height_px": round(scroll_h, 2),
        "client_width_px": round(client_w, 2),
        "client_height_px": round(client_h, 2),
        "width_gap_px": round(width_gap, 2),
        "height_gap_px": round(height_gap, 2),
        "max_gap_px": round(max(width_gap, height_gap), 2),
        "overflow_ratio": round(max(width_gap / max(1.0, rect["w"]), height_gap / max(1.0, rect["h"]), overflow_area / visible_area), 4),
        "rect": _round_rect(rect),
    }


def _has_non_visible_overflow(el: dict[str, Any]) -> bool:
    overflow_x = str(el.get("overflowX") or "").strip().lower()
    overflow_y = str(el.get("overflowY") or "").strip().lower()
    return overflow_x in {"hidden", "clip", "auto", "scroll"} or overflow_y in {"hidden", "clip", "auto", "scroll"}


def _is_text_like(el: dict[str, Any]) -> bool:
    tag = str(el.get("tag") or "").lower()
    role = " ".join(str(el.get(k) or "") for k in ("role", "data_kind", "class_name")).lower()
    text = str(el.get("text") or "").strip()
    if len(text.split()) >= 3:
        return True
    return tag in {"p", "h1", "h2", "h3", "h4", "li", "blockquote", "figcaption", "td", "th"} or any(
        token in role for token in ("text", "title", "caption", "body", "quote", "metric")
    )


def _is_panel_like(el: dict[str, Any]) -> bool:
    text = " ".join(str(el.get(k) or "") for k in ("role", "panel_role", "data_kind", "class_name", "tag")).lower()
    return any(token in text for token in ("panel", "card", "section", "lane"))


def _is_table_like(el: dict[str, Any]) -> bool:
    text = " ".join(
        str(el.get(k) or "")
        for k in ("role", "panel_role", "data_kind", "class_name", "tag", "block_id", "source_id", "layer_id")
    ).lower()
    return any(token in text for token in ("table", "result", "benchmark", "ablation", "evidence"))


def _is_meaningful_content(el: dict[str, Any]) -> bool:
    rect = _rect(el.get("rect"))
    if rect["w"] <= 2 or rect["h"] <= 2:
        return False
    tag = str(el.get("tag") or "").lower()
    if tag in {"img", "table", "svg", "canvas", "figure", "figcaption"}:
        return True
    text = str(el.get("text") or "").strip()
    if len(text) >= 8:
        return True
    kind = str(el.get("data_kind") or "").lower()
    return kind in {"image", "table", "chart", "caption", "text", "metric", "quote"}


def _is_source_figure_image(img: dict[str, Any]) -> bool:
    text = " ".join(
        str(img.get(k) or "")
        for k in (
            "role", "class_name", "alt", "source_id", "layer_id",
            "parent_role", "parent_class_name", "parent_source_id",
        )
    ).lower()
    if any(token in text for token in ("logo", "qr", "icon", "avatar", "identity")):
        return False
    return bool(img.get("source_id") or img.get("layer_id") or img.get("parent_source_id")) or any(
        token in text for token in ("figure", "fig", "source", "visual", "evidence", "chart", "table", "plot")
    )


def _rect(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict):
        raw = {}
    x = _float(raw.get("x"))
    y = _float(raw.get("y"))
    w = _float(raw.get("w"))
    h = _float(raw.get("h"))
    return {
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "right": _float(raw.get("right"), x + w),
        "bottom": _float(raw.get("bottom"), y + h),
    }


def _round_rect(rect: dict[str, float]) -> dict[str, float]:
    return {key: round(float(value), 2) for key, value in rect.items()}


def _smallest_container_for_rect(
    rect: dict[str, float],
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    contained: list[tuple[float, dict[str, Any]]] = []
    for candidate in candidates:
        candidate_rect = _rect(candidate.get("rect"))
        if _same_rectish(candidate_rect, rect):
            continue
        if not _rect_center_inside(rect, candidate_rect):
            continue
        if candidate_rect["w"] < rect["w"] * 0.85 or candidate_rect["h"] < rect["h"] * 0.85:
            continue
        contained.append((candidate_rect["w"] * candidate_rect["h"], candidate))
    if not contained:
        return None
    contained.sort(key=lambda item: item[0])
    return contained[0][1]


def _same_rectish(left: dict[str, float], right: dict[str, float]) -> bool:
    return (
        abs(left["x"] - right["x"]) <= 2
        and abs(left["y"] - right["y"]) <= 2
        and abs(left["w"] - right["w"]) <= 3
        and abs(left["h"] - right["h"]) <= 3
    )


def _outside_gap(child: dict[str, float], parent: dict[str, float]) -> dict[str, float]:
    left = max(0.0, parent["x"] - child["x"])
    top = max(0.0, parent["y"] - child["y"])
    right = max(0.0, child["right"] - parent["right"])
    bottom = max(0.0, child["bottom"] - parent["bottom"])
    return {
        "left_gap_px": round(left, 2),
        "top_gap_px": round(top, 2),
        "right_gap_px": round(right, 2),
        "bottom_gap_px": round(bottom, 2),
        "max_gap_px": round(max(left, top, right, bottom), 2),
    }


def _rect_center_inside(child: dict[str, float], parent: dict[str, float]) -> bool:
    cx = child["x"] + child["w"] / 2.0
    cy = child["y"] + child["h"] / 2.0
    return parent["x"] <= cx <= parent["right"] and parent["y"] <= cy <= parent["bottom"]


def _element_id(el: dict[str, Any]) -> str:
    for key in ("block_id", "slot_id", "panel_role", "lane", "element_id"):
        value = str(el.get(key) or "").strip()
        if value:
            return value
    return str(el.get("tag") or "element")


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


_ROOT_SNAPSHOT_JS = """() => {
  const root = document.querySelector('.paper-poster');
  if (!root) return {missingRoot: true};
  const rr = root.getBoundingClientRect();
  const cs = window.getComputedStyle(root);
  return {
    missingRoot: false,
    root: {
      x: rr.x, y: rr.y, w: rr.width, h: rr.height, right: rr.right, bottom: rr.bottom,
      scrollWidth: root.scrollWidth || 0,
      scrollHeight: root.scrollHeight || 0,
      clientWidth: root.clientWidth || 0,
      clientHeight: root.clientHeight || 0,
      overflowX: cs.overflowX || '',
      overflowY: cs.overflowY || ''
    }
  };
}"""


_SNAPSHOT_JS = """() => {
  const root = document.querySelector('.paper-poster');
  if (!root) return {missingRoot: true};
  const rr = root.getBoundingClientRect();
  const rectObj = r => ({
    x: r.x - rr.x,
    y: r.y - rr.y,
    w: r.width,
    h: r.height,
    right: r.right - rr.x,
    bottom: r.bottom - rr.y
  });
  const cleanText = value => String(value || '').replace(/\\s+/g, ' ').trim().slice(0, 500);
  const parentWith = (el, attr) => {
    let parent = el.parentElement;
    while (parent && parent !== root) {
      const value = parent.getAttribute(attr) || '';
      if (value) return value;
      parent = parent.parentElement;
    }
    return '';
  };
  const visible = el => {
    const cs = window.getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity || '1') === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const sourceFlowUnitFor = el => el.closest('.source-flow-unit,.figure-flow-unit');
  const floatedSourceChildrenFor = unit => {
    if (!unit) return [];
    return Array.from(unit.children || []).filter(child => {
      const tag = child.tagName ? child.tagName.toLowerCase() : '';
      const className = typeof child.className === 'string' ? child.className.toLowerCase() : '';
      const hasSourceMarker =
        tag === 'figure' ||
        tag === 'img' ||
        className.includes('flow-asset') ||
        className.includes('flow-figure') ||
        className.includes('source-asset') ||
        className.includes('source-table') ||
        child.hasAttribute('data-source-id') ||
        child.hasAttribute('data-layer-id') ||
        child.getAttribute('data-block-kind') === 'table' ||
        !!child.querySelector(':scope > img');
      if (!hasSourceMarker) return false;
      const cs = window.getComputedStyle(child);
      return cs.float === 'left' || cs.float === 'right' || className.includes('float-left') || className.includes('float-right');
    });
  };
  const rootStyle = window.getComputedStyle(root);
  const selectors = [
    '[data-block-id]',
    '[data-panel-role]',
    '[data-slot-id]',
    '[data-lane]',
    'figure',
    'section',
    'article',
    '.panel',
    '.card'
  ].join(',');
  const seen = new Set();
  const elements = Array.from(root.querySelectorAll(selectors)).filter(el => {
    if (el === root || seen.has(el) || !visible(el)) return false;
    seen.add(el);
    return true;
  }).map((el, idx) => {
    const r = el.getBoundingClientRect();
    const cs = window.getComputedStyle(el);
    return {
      element_id: `el_${idx}`,
      block_id: el.getAttribute('data-block-id') || '',
      panel_role: el.getAttribute('data-panel-role') || '',
      slot_id: el.getAttribute('data-slot-id') || '',
      lane: el.getAttribute('data-lane') || '',
      source_id: el.getAttribute('data-source-id') || '',
      layer_id: el.getAttribute('data-layer-id') || '',
      role: el.getAttribute('data-role') || el.getAttribute('role') || '',
      data_kind: el.getAttribute('data-block-kind') || '',
      tag: el.tagName.toLowerCase(),
      class_name: typeof el.className === 'string' ? el.className : '',
      text: cleanText(el.innerText || el.textContent || el.getAttribute('alt') || ''),
      rect: rectObj(r),
      scrollWidth: el.scrollWidth || 0,
      scrollHeight: el.scrollHeight || 0,
      clientWidth: el.clientWidth || 0,
      clientHeight: el.clientHeight || 0,
      overflowX: cs.overflowX || '',
      overflowY: cs.overflowY || ''
    };
  });
  const images = Array.from(root.querySelectorAll('img')).filter(visible).map((img, idx) => {
    const r = img.getBoundingClientRect();
    const cs = window.getComputedStyle(img);
    const parent = img.parentElement;
    return {
      element_id: `img_${idx}`,
      block_id: img.getAttribute('data-block-id') || parentWith(img, 'data-block-id'),
      source_id: img.getAttribute('data-source-id') || '',
      layer_id: img.getAttribute('data-layer-id') || '',
      role: img.getAttribute('data-role') || img.getAttribute('role') || '',
      class_name: typeof img.className === 'string' ? img.className : '',
      alt: img.getAttribute('alt') || '',
      src: img.getAttribute('src') || '',
      currentSrc: img.currentSrc || '',
      complete: img.complete,
      naturalWidth: img.naturalWidth || 0,
      naturalHeight: img.naturalHeight || 0,
      objectFit: cs.objectFit || '',
      objectPosition: cs.objectPosition || '',
      tag: 'img',
      rect: rectObj(r),
      parent_role: parent ? (parent.getAttribute('data-role') || parent.getAttribute('role') || '') : '',
      parent_class_name: parent && typeof parent.className === 'string' ? parent.className : '',
      parent_source_id: parentWith(img, 'data-source-id')
    };
  });
  const tables = Array.from(root.querySelectorAll('table')).filter(visible).map((table, idx) => {
    const r = table.getBoundingClientRect();
    const cs = window.getComputedStyle(table);
    const rows = Array.from(table.querySelectorAll('tr'));
    let columnCount = 0;
    rows.forEach(row => {
      const cells = Array.from(row.querySelectorAll('th,td'));
      columnCount = Math.max(columnCount, cells.length);
    });
    return {
      element_id: `table_${idx}`,
      block_id: table.getAttribute('data-block-id') || parentWith(table, 'data-block-id'),
      source_id: table.getAttribute('data-source-id') || parentWith(table, 'data-source-id'),
      layer_id: table.getAttribute('data-layer-id') || parentWith(table, 'data-layer-id'),
      role: table.getAttribute('data-role') || table.getAttribute('role') || '',
      data_kind: table.getAttribute('data-block-kind') || parentWith(table, 'data-block-kind') || 'table',
      tag: 'table',
      class_name: typeof table.className === 'string' ? table.className : '',
      text: cleanText(table.innerText || table.textContent || ''),
      rect: rectObj(r),
      scrollWidth: table.scrollWidth || 0,
      scrollHeight: table.scrollHeight || 0,
      clientWidth: table.clientWidth || 0,
      clientHeight: table.clientHeight || 0,
      overflowX: cs.overflowX || '',
      overflowY: cs.overflowY || '',
      font_px: parseFloat(cs.fontSize || '0') || 0,
      row_count: rows.length,
      column_count: columnCount
    };
  });
  const lists = Array.from(root.querySelectorAll('ul,ol')).filter(visible).map((list, idx) => {
    const r = list.getBoundingClientRect();
    const cs = window.getComputedStyle(list);
    const unit = sourceFlowUnitFor(list);
    const floatedSourceChildren = unit && list.parentElement === unit ? floatedSourceChildrenFor(unit) : [];
    const sourceFlowRect = unit ? unit.getBoundingClientRect() : null;
    return {
      element_id: `list_${idx}`,
      block_id: list.getAttribute('data-block-id') || parentWith(list, 'data-block-id'),
      panel_id: parentWith(list, 'data-panel-role') || parentWith(list, 'data-slot-id'),
      tag: list.tagName.toLowerCase(),
      class_name: typeof list.className === 'string' ? list.className : '',
      text: cleanText(list.innerText || list.textContent || ''),
      item_count: list.querySelectorAll(':scope > li').length,
      rect: rectObj(r),
      display: cs.display || '',
      paddingInlineStart: cs.paddingInlineStart || '',
      paddingInlineStartPx: parseFloat(cs.paddingInlineStart || cs.paddingLeft || '0') || 0,
      paddingLeftPx: parseFloat(cs.paddingLeft || '0') || 0,
      marginInlineStart: cs.marginInlineStart || '',
      marginLeftPx: parseFloat(cs.marginLeft || '0') || 0,
      listStylePosition: cs.listStylePosition || '',
      textIndentPx: parseFloat(cs.textIndent || '0') || 0,
      source_flow_id: unit ? (unit.getAttribute('data-block-id') || unit.getAttribute('data-source-id') || unit.getAttribute('data-layer-id') || '') : '',
      source_flow_class_name: unit && typeof unit.className === 'string' ? unit.className : '',
      source_flow_rect: sourceFlowRect ? rectObj(sourceFlowRect) : null,
      has_source_flow_ancestor: !!unit,
      is_direct_source_flow_child: !!unit && list.parentElement === unit,
      has_floated_source_sibling: floatedSourceChildren.length > 0,
      floated_source_sibling_count: floatedSourceChildren.length
    };
  });
  return {
    missingRoot: false,
    root: {
      x: rr.x, y: rr.y, w: rr.width, h: rr.height, right: rr.right, bottom: rr.bottom,
      scrollWidth: root.scrollWidth || 0,
      scrollHeight: root.scrollHeight || 0,
      clientWidth: root.clientWidth || 0,
      clientHeight: root.clientHeight || 0,
      overflowX: rootStyle.overflowX || '',
      overflowY: rootStyle.overflowY || ''
    },
    body: {
      scrollWidth: document.documentElement.scrollWidth || 0,
      scrollHeight: document.documentElement.scrollHeight || 0
    },
    elements,
    images,
    tables,
    lists
  };
}"""
