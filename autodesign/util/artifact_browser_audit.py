"""Playwright-backed layout audits for external Landing and Slides HTML.

The external coding-agent artifacts are free-form HTML, so static parsing cannot
prove that counted text or evidence is actually painted.  This module keeps the
browser measurement generic while leaving artifact-specific acceptance policy in
the two public entry points below.  It intentionally has no Poster call sites.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

from .browser_render import _launch_chromium
from .math_typesetting import wait_for_autodesign_math


_LANDING_VIEWPORTS = (
    ("desktop", 1440, 900),
)
_SLIDE_WIDTH = 1920
_SLIDE_HEIGHT = 1080
_GEOMETRY_TOLERANCE_PX = 2.0


def audit_landing_html(
    html_path: Path,
    *,
    required_source_ids: Iterable[str] | None = None,
    timeout_ms: int = 15_000,
    block_network: bool = True,
) -> dict[str, Any]:
    """Audit a Landing artifact at the supported desktop viewport with and without page JS."""

    html_path = Path(html_path).resolve()
    required = _normalized_ids(required_source_ids)
    snapshots: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    blocked_requests: list[str] = []
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return _unavailable_report("landing", exc)

    try:
        with sync_playwright() as playwright:
            browser = _launch_chromium(playwright)
            try:
                for viewport_name, width, height in _LANDING_VIEWPORTS:
                    for javascript_enabled in (True, False):
                        mode = "js_enabled" if javascript_enabled else "js_disabled"
                        key = f"{viewport_name}_{mode}"
                        snapshot, blocked = _snapshot_page(
                            browser,
                            html_path,
                            viewport_width=width,
                            viewport_height=height,
                            javascript_enabled=javascript_enabled,
                            evaluator=_LANDING_SNAPSHOT_JS,
                            timeout_ms=timeout_ms,
                            block_network=block_network,
                            measure_landing_sources=True,
                        )
                        snapshots[key] = snapshot
                        blocked_requests.extend(blocked)
            finally:
                browser.close()
    except Exception as exc:
        return _unavailable_report("landing", exc)

    findings: list[dict[str, Any]] = []
    for viewport_name, _, _ in _LANDING_VIEWPORTS:
        enabled = snapshots[f"{viewport_name}_js_enabled"]
        disabled = snapshots[f"{viewport_name}_js_disabled"]
        findings.extend(_landing_snapshot_findings(viewport_name, enabled, required))
        findings.extend(_landing_snapshot_findings(viewport_name, disabled, required))
        findings.extend(_landing_js_disabled_findings(viewport_name, enabled, disabled, required))

    if blocked_requests:
        warnings.append(f"blocked_network_requests:{len(set(blocked_requests))}")
        findings.append(_finding(
            "landing_runtime_network_request",
            "landing page attempted runtime network requests",
            urls=sorted(set(blocked_requests))[:20],
        ))
    metrics = {
        "viewport_count": len(_LANDING_VIEWPORTS),
        "snapshot_count": len(snapshots),
        "required_source_ids": sorted(required),
        "blocked_request_count": len(set(blocked_requests)),
        "snapshots": snapshots,
    }
    findings = _dedupe_findings(findings)
    return {
        "kind": "artifact_browser_audit",
        "version": 1,
        "artifact_type": "landing",
        "backend": "playwright",
        "status": "error" if findings else "ok",
        "accepted": not findings,
        "findings": findings,
        "metrics": metrics,
        "warnings": warnings,
    }


def audit_slides_html(
    html_path: Path,
    *,
    required_source_ids: Iterable[str] | None = None,
    expected_slide_count: int | None = None,
    timeout_ms: int = 15_000,
    block_network: bool = True,
) -> dict[str, Any]:
    """Audit every 1920x1080 slide frame with page JS enabled and disabled."""

    html_path = Path(html_path).resolve()
    required = _normalized_ids(required_source_ids)
    snapshots: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    blocked_requests: list[str] = []
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return _unavailable_report("slides", exc)

    try:
        with sync_playwright() as playwright:
            browser = _launch_chromium(playwright)
            try:
                for javascript_enabled in (True, False):
                    mode = "js_enabled" if javascript_enabled else "js_disabled"
                    snapshot, blocked = _snapshot_page(
                        browser,
                        html_path,
                        viewport_width=_SLIDE_WIDTH,
                        viewport_height=_SLIDE_HEIGHT,
                        javascript_enabled=javascript_enabled,
                        evaluator=_SLIDES_SNAPSHOT_JS,
                        timeout_ms=timeout_ms,
                        block_network=block_network,
                    )
                    snapshots[mode] = snapshot
                    blocked_requests.extend(blocked)
            finally:
                browser.close()
    except Exception as exc:
        return _unavailable_report("slides", exc)

    enabled = snapshots["js_enabled"]
    disabled = snapshots["js_disabled"]
    findings = _slides_snapshot_findings(enabled, required, expected_slide_count)
    findings.extend(_slides_snapshot_findings(disabled, required, expected_slide_count))
    findings.extend(_slides_js_disabled_findings(enabled, disabled, required))
    if blocked_requests:
        warnings.append(f"blocked_network_requests:{len(set(blocked_requests))}")
        findings.append(_finding(
            "slides_runtime_network_request",
            "slides attempted runtime network requests",
            urls=sorted(set(blocked_requests))[:20],
        ))
    metrics = {
        "snapshot_count": len(snapshots),
        "expected_slide_count": expected_slide_count,
        "required_source_ids": sorted(required),
        "blocked_request_count": len(set(blocked_requests)),
        "snapshots": snapshots,
    }
    findings = _dedupe_findings(findings)
    return {
        "kind": "artifact_browser_audit",
        "version": 1,
        "artifact_type": "slides",
        "backend": "playwright",
        "status": "error" if findings else "ok",
        "accepted": not findings,
        "findings": findings,
        "metrics": metrics,
        "warnings": warnings,
    }


def _snapshot_page(
    browser: Any,
    html_path: Path,
    *,
    viewport_width: int,
    viewport_height: int,
    javascript_enabled: bool,
    evaluator: str,
    timeout_ms: int,
    block_network: bool,
    measure_landing_sources: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    blocked: list[str] = []
    context = browser.new_context(
        java_script_enabled=javascript_enabled,
        viewport={"width": viewport_width, "height": viewport_height},
        device_scale_factor=1,
        reduced_motion="reduce",
    )
    try:
        if block_network:
            _install_local_only_route(context, html_path.parent, blocked)
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        page.set_default_navigation_timeout(timeout_ms)
        page.goto(html_path.as_uri(), wait_until="load", timeout=timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=min(2_000, timeout_ms))
        except Exception:
            pass
        if javascript_enabled:
            wait_for_autodesign_math(page, timeout_ms=min(3_000, timeout_ms))
        _prime_local_media(page, timeout_ms=timeout_ms)
        snapshot = page.evaluate(evaluator)
        if measure_landing_sources:
            snapshot["sources"] = _measure_landing_sources(page, snapshot.get("sources") or [])
        snapshot["viewport"] = {"width": viewport_width, "height": viewport_height}
        snapshot["javascript_enabled"] = javascript_enabled
        return snapshot, blocked
    finally:
        context.close()


def _measure_landing_sources(page: Any, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    measured_sources: list[dict[str, Any]] = []
    original_scroll = page.evaluate("() => ({x: window.scrollX, y: window.scrollY})")
    try:
        for source_index, source in enumerate(sources):
            if not str(source.get("source_id") or ""):
                measured_sources.append(source)
                continue
            measured = page.evaluate(_LANDING_SOURCE_MEASUREMENT_JS, source_index)
            measured_sources.append(measured if isinstance(measured, dict) else source)
    finally:
        page.evaluate(
            "position => window.scrollTo(position.x, position.y)",
            original_scroll,
        )
    return measured_sources


def _prime_local_media(page: Any, *, timeout_ms: int) -> None:
    media = page.locator(
        "img[loading='lazy'], img[data-source-id], [data-source-id] img"
    )
    count = min(media.count(), 200)
    original_scroll = page.evaluate("() => ({x: window.scrollX, y: window.scrollY})")
    try:
        for index in range(count):
            try:
                target_y = media.nth(index).evaluate(
                    "el => Math.max(0, el.getBoundingClientRect().top + window.scrollY "
                    "- window.innerHeight / 2)"
                )
                page.evaluate(
                    "y => window.scrollTo(window.scrollX, y)",
                    target_y,
                )
            except Exception:
                continue
        if count:
            page.wait_for_timeout(min(100, timeout_ms))
    finally:
        page.evaluate(
            "position => window.scrollTo(position.x, position.y)",
            original_scroll,
        )


def _install_local_only_route(context: Any, allowed_root: Path, blocked: list[str]) -> None:
    allowed_root = allowed_root.resolve()

    def handle_route(route: Any) -> None:
        url = route.request.url
        parsed = urlparse(url)
        if parsed.scheme in {"about", "data"}:
            route.continue_()
            return
        if parsed.scheme == "file":
            candidate = Path(unquote(parsed.path)).resolve()
            if candidate == allowed_root or allowed_root in candidate.parents:
                route.continue_()
                return
        blocked.append(url)
        route.abort()

    context.route("**/*", handle_route)


def _landing_snapshot_findings(
    viewport_name: str,
    snapshot: dict[str, Any],
    required: set[str],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    overflow = float(snapshot.get("horizontal_overflow_px") or 0)
    if overflow > _GEOMETRY_TOLERANCE_PX:
        findings.append(_finding(
            "landing_document_horizontal_overflow",
            f"{viewport_name} document overflows horizontally by {overflow:.1f}px",
            viewport=viewport_name,
            overflow_px=round(overflow, 2),
        ))
    if not snapshot.get("title_visible"):
        findings.append(_finding(
            "landing_core_title_not_visible",
            f"{viewport_name} paper title is not visibly rendered",
            viewport=viewport_name,
        ))
    if int(snapshot.get("visible_section_count") or 0) < 3:
        findings.append(_finding(
            "landing_core_sections_not_visible",
            f"{viewport_name} renders fewer than three visible semantic sections",
            viewport=viewport_name,
            actual=int(snapshot.get("visible_section_count") or 0),
        ))
    clipped = [
        item for item in snapshot.get("clipped_content") or []
        if isinstance(item, dict)
    ]
    if clipped:
        findings.append(_finding(
            "landing_content_clipped",
            f"{viewport_name} contains clipped core text",
            viewport=viewport_name,
            elements=clipped,
        ))
    sources = _source_map(snapshot)
    expected = required or set(sources)
    for source_id in sorted(expected):
        source = sources.get(source_id)
        if source is None:
            findings.append(_finding(
                "landing_source_evidence_missing",
                f"{viewport_name} does not render required source evidence {source_id}",
                viewport=viewport_name,
                source_id=source_id,
            ))
            continue
        if not source.get("loaded"):
            findings.append(_finding(
                "landing_source_evidence_broken",
                f"{viewport_name} source evidence {source_id} did not load",
                viewport=viewport_name,
                source_id=source_id,
            ))
        if not source.get("effectively_visible"):
            findings.append(_finding(
                "landing_source_evidence_not_visible",
                f"{viewport_name} source evidence {source_id} has no meaningful painted area",
                viewport=viewport_name,
                source_id=source_id,
                visible_area_px=source.get("visible_area_px", 0),
                visible_ratio=source.get("visible_ratio", 0),
            ))
    return findings


def _landing_js_disabled_findings(
    viewport_name: str,
    enabled: dict[str, Any],
    disabled: dict[str, Any],
    required: set[str],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    enabled_words = int(enabled.get("visible_word_count") or 0)
    disabled_words = int(disabled.get("visible_word_count") or 0)
    enabled_sections = int(enabled.get("visible_section_count") or 0)
    disabled_sections = int(disabled.get("visible_section_count") or 0)
    lost_core = (
        bool(enabled.get("title_visible")) and not bool(disabled.get("title_visible"))
    ) or (
        enabled_sections >= 3 and disabled_sections < 3
    ) or (
        enabled_words >= 20 and disabled_words < max(10, math.floor(enabled_words * 0.8))
    )
    if lost_core:
        findings.append(_finding(
            "landing_core_content_js_dependent",
            f"{viewport_name} core content disappears when page JavaScript is disabled",
            viewport=viewport_name,
            enabled_visible_words=enabled_words,
            disabled_visible_words=disabled_words,
        ))
    enabled_sources = _source_map(enabled)
    disabled_sources = _source_map(disabled)
    expected = required or set(enabled_sources)
    lost_sources = [
        source_id
        for source_id in sorted(expected)
        if enabled_sources.get(source_id, {}).get("effectively_visible")
        and not disabled_sources.get(source_id, {}).get("effectively_visible")
    ]
    if lost_sources:
        findings.append(_finding(
            "landing_source_evidence_js_dependent",
            f"{viewport_name} source evidence depends on page JavaScript: {', '.join(lost_sources)}",
            viewport=viewport_name,
            source_ids=lost_sources,
        ))
    return findings


def _slides_snapshot_findings(
    snapshot: dict[str, Any],
    required: set[str],
    expected_slide_count: int | None,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    slides = [item for item in snapshot.get("slides") or [] if isinstance(item, dict)]
    if expected_slide_count is not None and len(slides) != expected_slide_count:
        findings.append(_finding(
            "slides_browser_slide_count_mismatch",
            f"browser rendered {len(slides)} slides; expected {expected_slide_count}",
            expected=expected_slide_count,
            actual=len(slides),
        ))
    navigation_controls = [
        item
        for item in snapshot.get("visible_navigation_controls") or []
        if isinstance(item, dict)
    ]
    if navigation_controls:
        findings.append(_finding(
            "slides_visible_navigation_controls",
            "deck exposes visible playback or slide navigation controls",
            elements=navigation_controls,
        ))
    all_sources: dict[str, dict[str, Any]] = {}
    for slide in slides:
        slide_id = str(slide.get("slide_id") or "")
        width = float(slide.get("width") or 0)
        height = float(slide.get("height") or 0)
        layout_width = float(slide.get("layout_width") or 0)
        layout_height = float(slide.get("layout_height") or 0)
        rendered_ratio = width / height if height > 0 else 0
        if (
            not slide.get("effectively_visible")
            or abs(layout_width - _SLIDE_WIDTH) > _GEOMETRY_TOLERANCE_PX
            or abs(layout_height - _SLIDE_HEIGHT) > _GEOMETRY_TOLERANCE_PX
            or width < _SLIDE_WIDTH * 0.5
            or height < _SLIDE_HEIGHT * 0.5
            or abs(rendered_ratio - (_SLIDE_WIDTH / _SLIDE_HEIGHT)) > 0.01
        ):
            findings.append(_finding(
                "slides_root_geometry_invalid",
                f"slide {slide_id or '<unnamed>'} is not a visible 1920x1080 logical frame",
                slide_id=slide_id,
                width=round(width, 2),
                height=round(height, 2),
                layout_width=round(layout_width, 2),
                layout_height=round(layout_height, 2),
            ))
        overflow_x = float(slide.get("scroll_overflow_x") or 0)
        overflow_y = float(slide.get("scroll_overflow_y") or 0)
        overflow_x_behavior = str(slide.get("overflow_x_behavior") or "")
        overflow_y_behavior = str(slide.get("overflow_y_behavior") or "")
        scrollable_overflow = (
            overflow_x > _GEOMETRY_TOLERANCE_PX
            and overflow_x_behavior in {"auto", "scroll"}
        ) or (
            overflow_y > _GEOMETRY_TOLERANCE_PX
            and overflow_y_behavior in {"auto", "scroll"}
        )
        if scrollable_overflow:
            findings.append(_finding(
                "slides_internal_overflow",
                f"slide {slide_id or '<unnamed>'} has internal scroll overflow",
                slide_id=slide_id,
                overflow_x_px=round(overflow_x, 2),
                overflow_y_px=round(overflow_y, 2),
            ))
        clipped = [
            item for item in slide.get("clipped_content") or []
            if isinstance(item, dict)
        ]
        if clipped:
            findings.append(_finding(
                "slides_content_clipped",
                f"slide {slide_id or '<unnamed>'} contains clipped or off-frame content",
                slide_id=slide_id,
                elements=clipped,
            ))
        for source in slide.get("sources") or []:
            if not isinstance(source, dict):
                continue
            source_id = str(source.get("source_id") or "")
            previous = all_sources.get(source_id)
            if previous is None or float(source.get("visible_area_px") or 0) > float(previous.get("visible_area_px") or 0):
                all_sources[source_id] = {**source, "slide_id": slide_id}
            if not source.get("loaded"):
                findings.append(_finding(
                    "slides_source_evidence_broken",
                    f"slide {slide_id} source evidence {source_id} did not load",
                    slide_id=slide_id,
                    source_id=source_id,
                ))
            if not source.get("effectively_visible"):
                findings.append(_finding(
                    "slides_source_evidence_not_visible",
                    f"slide {slide_id} source evidence {source_id} is clipped or outside its slide",
                    slide_id=slide_id,
                    source_id=source_id,
                    visible_area_px=source.get("visible_area_px", 0),
                    visible_ratio=source.get("visible_ratio", 0),
                ))
        for unit in slide.get("visual_units") or []:
            if isinstance(unit, dict) and not unit.get("effectively_visible"):
                findings.append(_finding(
                    "slides_visual_unit_not_visible",
                    f"slide {slide_id} visual unit is clipped or outside its slide",
                    slide_id=slide_id,
                    visual_unit=str(unit.get("visual_unit") or ""),
                ))
    for source_id in sorted(required - set(all_sources)):
        findings.append(_finding(
            "slides_source_evidence_missing",
            f"browser did not render required source evidence {source_id}",
            source_id=source_id,
        ))
    return findings


def _slides_js_disabled_findings(
    enabled: dict[str, Any],
    disabled: dict[str, Any],
    required: set[str],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    enabled_slides = {
        str(item.get("slide_id") or ""): item
        for item in enabled.get("slides") or []
        if isinstance(item, dict)
    }
    disabled_slides = {
        str(item.get("slide_id") or ""): item
        for item in disabled.get("slides") or []
        if isinstance(item, dict)
    }
    for slide_id, enabled_slide in enabled_slides.items():
        disabled_slide = disabled_slides.get(slide_id)
        if disabled_slide is None or (
            enabled_slide.get("effectively_visible")
            and not disabled_slide.get("effectively_visible")
        ):
            findings.append(_finding(
                "slides_core_content_js_dependent",
                f"slide {slide_id or '<unnamed>'} disappears when page JavaScript is disabled",
                slide_id=slide_id,
            ))
            continue
        enabled_words = int(enabled_slide.get("visible_word_count") or 0)
        disabled_words = int(disabled_slide.get("visible_word_count") or 0)
        if enabled_words >= 5 and disabled_words < max(3, math.floor(enabled_words * 0.8)):
            findings.append(_finding(
                "slides_core_content_js_dependent",
                f"slide {slide_id or '<unnamed>'} loses core text when page JavaScript is disabled",
                slide_id=slide_id,
                enabled_visible_words=enabled_words,
                disabled_visible_words=disabled_words,
            ))
    enabled_sources = _slide_source_map(enabled)
    disabled_sources = _slide_source_map(disabled)
    expected = required or set(enabled_sources)
    lost_sources = [
        source_id
        for source_id in sorted(expected)
        if enabled_sources.get(source_id, {}).get("effectively_visible")
        and not disabled_sources.get(source_id, {}).get("effectively_visible")
    ]
    if lost_sources:
        findings.append(_finding(
            "slides_source_evidence_js_dependent",
            f"source evidence depends on page JavaScript: {', '.join(lost_sources)}",
            source_ids=lost_sources,
        ))
    return findings


def _source_map(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for source in snapshot.get("sources") or []:
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("source_id") or "")
        previous = result.get(source_id)
        if previous is None or float(source.get("visible_area_px") or 0) > float(previous.get("visible_area_px") or 0):
            result[source_id] = source
    return result


def _slide_source_map(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for slide in snapshot.get("slides") or []:
        if not isinstance(slide, dict):
            continue
        for source in slide.get("sources") or []:
            if not isinstance(source, dict):
                continue
            source_id = str(source.get("source_id") or "")
            previous = result.get(source_id)
            if previous is None or float(source.get("visible_area_px") or 0) > float(previous.get("visible_area_px") or 0):
                result[source_id] = source
    return result


def _normalized_ids(values: Iterable[str] | None) -> set[str]:
    return {str(value).strip() for value in (values or []) if str(value).strip()}


def _finding(finding_id: str, message: str, **evidence: Any) -> dict[str, Any]:
    return {
        "id": finding_id,
        "severity": "error",
        "message": message,
        "evidence": evidence,
    }


def _dedupe_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for finding in findings:
        evidence = finding.get("evidence") if isinstance(finding.get("evidence"), dict) else {}
        key = (
            str(finding.get("id") or ""),
            str(evidence.get("viewport") or evidence.get("slide_id") or ""),
            str(evidence.get("source_id") or ""),
        )
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return unique


def _unavailable_report(artifact_type: str, exc: Exception) -> dict[str, Any]:
    finding = _finding(
        "artifact_browser_audit_unavailable",
        f"Playwright browser audit failed: {type(exc).__name__}: {exc}",
    )
    return {
        "kind": "artifact_browser_audit",
        "version": 1,
        "artifact_type": artifact_type,
        "backend": "unavailable",
        "status": "error",
        "accepted": False,
        "findings": [finding],
        "metrics": {},
        "warnings": [finding["message"]],
    }


_COMMON_GEOMETRY_JS = r"""
  const EPS = 0.5;
  const rectObject = rect => ({
    left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom,
    width: Math.max(0, rect.right - rect.left),
    height: Math.max(0, rect.bottom - rect.top)
  });
  const intersection = (a, b) => {
    const left = Math.max(a.left, b.left);
    const top = Math.max(a.top, b.top);
    const right = Math.min(a.right, b.right);
    const bottom = Math.min(a.bottom, b.bottom);
    return rectObject({left, top, right: Math.max(left, right), bottom: Math.max(top, bottom)});
  };
  const area = rect => Math.max(0, rect.width) * Math.max(0, rect.height);
  const pixelValue = (token, dimension) => {
    const value = parseFloat(token || '0');
    if (!Number.isFinite(value)) return 0;
    return String(token).includes('%') ? dimension * value / 100 : value;
  };
  const applyInsetClip = (rect, clipPath) => {
    const match = String(clipPath || '').match(/^inset\(([^)]*)\)/i);
    if (!match) return rect;
    const raw = match[1].split(/\s+round\s+/i)[0].trim();
    const tokens = raw.split(/\s+/).filter(Boolean);
    if (!tokens.length || tokens.length > 4) return rect;
    let top, right, bottom, left;
    if (tokens.length === 1) top = right = bottom = left = tokens[0];
    else if (tokens.length === 2) { top = bottom = tokens[0]; right = left = tokens[1]; }
    else if (tokens.length === 3) { top = tokens[0]; right = left = tokens[1]; bottom = tokens[2]; }
    else [top, right, bottom, left] = tokens;
    return rectObject({
      left: rect.left + pixelValue(left, rect.width),
      top: rect.top + pixelValue(top, rect.height),
      right: Math.max(rect.left, rect.right - pixelValue(right, rect.width)),
      bottom: Math.max(rect.top, rect.bottom - pixelValue(bottom, rect.height))
    });
  };
  const styleAllowsPaint = el => {
    if (!el || !el.isConnected) return false;
    if (typeof el.checkVisibility === 'function' && !el.checkVisibility({
      checkOpacity: true, checkVisibilityCSS: true
    })) return false;
    let current = el;
    while (current && current.nodeType === Node.ELEMENT_NODE) {
      const style = getComputedStyle(current);
      if (style.display === 'none' || style.visibility === 'hidden' ||
          style.visibility === 'collapse' || style.contentVisibility === 'hidden' ||
          Number(style.opacity || '1') <= 0.01) return false;
      current = current.parentElement;
    }
    return true;
  };
  const clippedRect = (el, boundary, stopAt) => {
    const raw = rectObject(el.getBoundingClientRect());
    let visible = intersection(raw, boundary);
    visible = applyInsetClip(visible, getComputedStyle(el).clipPath);
    let parent = el.parentElement;
    while (parent && parent !== stopAt && parent !== document.documentElement) {
      const style = getComputedStyle(parent);
      const parentRect = rectObject(parent.getBoundingClientRect());
      const nonClippingInline = style.display === 'inline' &&
        parent.clientWidth === 0 && parent.clientHeight === 0;
      const clipsX = !nonClippingInline &&
        ['hidden', 'clip', 'scroll', 'auto'].includes(style.overflowX);
      const clipsY = !nonClippingInline &&
        ['hidden', 'clip', 'scroll', 'auto'].includes(style.overflowY);
      if (clipsX || clipsY) {
        const clip = {
          left: clipsX ? parentRect.left + parent.clientLeft : visible.left,
          right: clipsX ? parentRect.left + parent.clientLeft +
            (parent.clientWidth || parentRect.width) : visible.right,
          top: clipsY ? parentRect.top + parent.clientTop : visible.top,
          bottom: clipsY ? parentRect.top + parent.clientTop +
            (parent.clientHeight || parentRect.height) : visible.bottom
        };
        visible = intersection(visible, rectObject(clip));
      }
      visible = applyInsetClip(visible, style.clipPath);
      parent = parent.parentElement;
    }
    return {raw, visible};
  };
  const visualNode = root => {
    if (root.matches && root.matches('img,video,svg,canvas')) return root;
    return root.querySelector ? root.querySelector('img,video,svg,canvas') || root : root;
  };
  const sourceId = el => el.getAttribute('data-source-id') ||
    (el.closest('[data-source-id]') ? el.closest('[data-source-id]').getAttribute('data-source-id') : '');
  const measured = (el, boundary, stopAt) => {
    const node = visualNode(el);
    const geometry = clippedRect(node, boundary, stopAt);
    const rawArea = area(geometry.raw);
    const visibleArea = area(geometry.visible);
    const loaded = node.tagName === 'IMG'
      ? Boolean(node.complete && node.naturalWidth > 0 && node.naturalHeight > 0)
      : true;
    const visibleRatio = rawArea > EPS ? visibleArea / rawArea : 0;
    const effectivelyVisible = styleAllowsPaint(node) && loaded &&
      geometry.visible.width >= 16 && geometry.visible.height >= 16 &&
      visibleArea >= 256 && visibleRatio >= 0.5;
    return {
      source_id: sourceId(el),
      loaded,
      effectively_visible: effectivelyVisible,
      raw_rect: geometry.raw,
      visible_rect: geometry.visible,
      visible_area_px: Math.round(visibleArea * 100) / 100,
      visible_ratio: Math.round(visibleRatio * 10000) / 10000,
      clip_path: getComputedStyle(node).clipPath || 'none'
    };
  };
  const textMetrics = (node, boundary, stopAt) => {
    const value = node.nodeValue || '';
    const parentElement = node.parentElement;
    if (!value.trim() || !parentElement ||
        ['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE'].includes(parentElement.tagName)) {
      return {visible: false, clipped: false, raw_area: 0, visible_area: 0, visible_ratio: 0};
    }
    const range = document.createRange();
    range.selectNodeContents(node);
    const rects = Array.from(range.getClientRects()).map(rectObject).filter(rect => area(rect) > EPS);
    range.detach();
    if (!rects.length) {
      return {visible: false, clipped: false, raw_area: 0, visible_area: 0, visible_ratio: 0};
    }
    let clip = boundary;
    let current = parentElement;
    while (current && current !== stopAt && current !== document.documentElement) {
      const style = getComputedStyle(current);
      const currentRect = rectObject(current.getBoundingClientRect());
      const nonClippingInline = style.display === 'inline' &&
        current.clientWidth === 0 && current.clientHeight === 0;
      const clipsX = !nonClippingInline &&
        ['hidden', 'clip', 'scroll', 'auto'].includes(style.overflowX);
      const clipsY = !nonClippingInline &&
        ['hidden', 'clip', 'scroll', 'auto'].includes(style.overflowY);
      if (clipsX || clipsY) {
        clip = intersection(clip, rectObject({
          left: clipsX ? currentRect.left + current.clientLeft : clip.left,
          right: clipsX ? currentRect.left + current.clientLeft +
            (current.clientWidth || currentRect.width) : clip.right,
          top: clipsY ? currentRect.top + current.clientTop : clip.top,
          bottom: clipsY ? currentRect.top + current.clientTop +
            (current.clientHeight || currentRect.height) : clip.bottom
        }));
      }
      clip = applyInsetClip(clip, style.clipPath);
      current = current.parentElement;
    }
    const rawArea = rects.reduce((total, rect) => total + area(rect), 0);
    const visibleArea = rects.reduce(
      (total, rect) => total + area(intersection(rect, clip)),
      0
    );
    const visibleRatio = rawArea > EPS ? visibleArea / rawArea : 0;
    const paint = styleAllowsPaint(parentElement);
    return {
      visible: paint && visibleArea > 1 && visibleRatio >= 0.98,
      clipped: paint && rawArea > 1 && visibleRatio < 0.98,
      raw_area: Math.round(rawArea * 100) / 100,
      visible_area: Math.round(visibleArea * 100) / 100,
      visible_ratio: Math.round(visibleRatio * 10000) / 10000
    };
  };
  const visibleWords = (root, boundary, stopAt) => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let text = '';
    while (walker.nextNode()) {
      if (textMetrics(walker.currentNode, boundary, stopAt).visible) {
        text += ' ' + walker.currentNode.nodeValue;
      }
    }
    return (text.match(/[\p{L}\p{N}][\p{L}\p{N}'’-]*/gu) || []).length;
  };
  const clippedTextEntries = (root, boundary, stopAt) => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const entries = [];
    while (walker.nextNode() && entries.length < 50) {
      const node = walker.currentNode;
      const metrics = textMetrics(node, boundary, stopAt);
      if (!metrics.clipped) continue;
      const parent = node.parentElement;
      entries.push({
        tag: parent ? parent.tagName.toLowerCase() : '',
        text: String(node.nodeValue || '').replace(/\s+/g, ' ').trim().slice(0, 120),
        visible_ratio: metrics.visible_ratio,
        raw_area: metrics.raw_area,
        visible_area: metrics.visible_area
      });
    }
    return entries;
  };
"""


_LANDING_SNAPSHOT_JS = """() => {
""" + _COMMON_GEOMETRY_JS + r"""
  const doc = document.documentElement;
  const body = document.body;
  const documentBoundary = rectObject({
    left: 0, top: 0,
    right: Math.max(doc.scrollWidth, body ? body.scrollWidth : 0, doc.clientWidth),
    bottom: Math.max(doc.scrollHeight, body ? body.scrollHeight : 0, doc.clientHeight)
  });
  const root = document.querySelector('main') || body || doc;
  const sourceRoots = Array.from(document.querySelectorAll('[data-source-id]')).filter((el, index, all) =>
    !el.parentElement || !el.parentElement.closest('[data-source-id]') ||
    el.parentElement.closest('[data-source-id]') === el ||
    !all.includes(el.parentElement.closest('[data-source-id]'))
  );
  const sources = sourceRoots.map(el => measured(el, documentBoundary, null));
  const title = document.querySelector('h1');
  const titleVisible = !!title && visibleWords(title, documentBoundary, null) > 0;
  const sections = Array.from(document.querySelectorAll('section,article')).filter(el =>
    styleAllowsPaint(el) && visibleWords(el, documentBoundary, null) > 0
  );
  const clippedContent = clippedTextEntries(root, documentBoundary, null);
  return {
    document_width: documentBoundary.width,
    document_height: documentBoundary.height,
    horizontal_overflow_px: Math.max(0, documentBoundary.width - doc.clientWidth),
    title_visible: titleVisible,
    visible_section_count: sections.length,
    visible_word_count: visibleWords(root, documentBoundary, null),
    sources,
    clipped_content: clippedContent
  };
}"""


_LANDING_SOURCE_MEASUREMENT_JS = """inputSourceIndex => {
""" + _COMMON_GEOMETRY_JS + r"""
  const roots = Array.from(document.querySelectorAll('[data-source-id]')).filter(el =>
    (!el.parentElement || !el.parentElement.closest('[data-source-id]'))
  );
  const root = roots[inputSourceIndex];
  if (!root) return null;
  const absoluteTop = root.getBoundingClientRect().top + window.scrollY;
  window.scrollTo(window.scrollX, Math.max(0, absoluteTop - window.innerHeight / 2));
  const viewport = rectObject({
    left: 0, top: 0, right: window.innerWidth, bottom: window.innerHeight
  });
  return measured(root, viewport, null);
}"""


_SLIDES_SNAPSHOT_JS = """() => {
""" + _COMMON_GEOMETRY_JS + r"""
  const slides = Array.from(document.querySelectorAll('.deck-slide'));
  const originalState = slides.map(slide => ({
    className: slide.getAttribute('class'),
    style: slide.getAttribute('style'),
    ariaHidden: slide.getAttribute('aria-hidden')
  }));
  const activateForMeasurement = activeIndex => {
    slides.forEach((slide, index) => {
      const active = index === activeIndex;
      slide.classList.toggle('is-active', active);
      slide.setAttribute('aria-hidden', active ? 'false' : 'true');
      if (active) {
        slide.style.removeProperty('display');
        if (getComputedStyle(slide).display === 'none') {
          slide.style.setProperty('display', 'grid', 'important');
        }
      } else {
        slide.style.setProperty('display', 'none', 'important');
      }
    });
  };
  const restoreSlides = () => {
    slides.forEach((slide, index) => {
      const state = originalState[index];
      if (state.className === null) slide.removeAttribute('class');
      else slide.setAttribute('class', state.className);
      if (state.style === null) slide.removeAttribute('style');
      else slide.setAttribute('style', state.style);
      if (state.ariaHidden === null) slide.removeAttribute('aria-hidden');
      else slide.setAttribute('aria-hidden', state.ariaHidden);
    });
  };
  const measuredSlides = slides.map((slide, index) => {
    activateForMeasurement(index);
    const frame = rectObject(slide.getBoundingClientRect());
    const slideId = slide.id || slide.getAttribute('data-slide-index') || String(index + 1);
    const sourceRoots = Array.from(slide.querySelectorAll('[data-source-id]')).filter((el, idx, all) =>
      !el.parentElement || !el.parentElement.closest('[data-source-id]') ||
      el.parentElement.closest('[data-source-id]') === el ||
      !all.includes(el.parentElement.closest('[data-source-id]'))
    );
    const sources = sourceRoots.map(el => measured(el, frame, slide));
    const visualUnits = Array.from(slide.querySelectorAll('[data-visual-unit]')).map(el => ({
      visual_unit: el.getAttribute('data-visual-unit') || '',
      ...measured(el, frame, slide)
    }));
    const clippedContent = clippedTextEntries(slide, frame, slide);
    const slideStyle = getComputedStyle(slide);
    return {
      slide_id: slideId,
      width: frame.width,
      height: frame.height,
      layout_width: slide.offsetWidth,
      layout_height: slide.offsetHeight,
      effectively_visible: styleAllowsPaint(slide) && frame.width > 0 && frame.height > 0,
      scroll_overflow_x: Math.max(0, slide.scrollWidth - slide.clientWidth),
      scroll_overflow_y: Math.max(0, slide.scrollHeight - slide.clientHeight),
      overflow_x_behavior: slideStyle.overflowX,
      overflow_y_behavior: slideStyle.overflowY,
      visible_word_count: visibleWords(slide, frame, slide),
      sources,
      visual_units: visualUnits,
      clipped_content: clippedContent
    };
  });
  restoreSlides();
  const visibleNavigationControls = Array.from(document.querySelectorAll(
    '.deck-controls, .slide-controls, nav[aria-label*="slide" i], ' +
    '[role="navigation"][aria-label*="slide" i]'
  )).filter(element => !element.closest('.deck-slide')).filter(element => {
    const frame = rectObject(element.getBoundingClientRect());
    return styleAllowsPaint(element) && frame.width > 0 && frame.height > 0;
  }).map(element => {
    const frame = rectObject(element.getBoundingClientRect());
    return {
      tag: element.tagName.toLowerCase(),
      id: element.id || '',
      class_name: typeof element.className === 'string' ? element.className : '',
      aria_label: element.getAttribute('aria-label') || '',
      width: frame.width,
      height: frame.height
    };
  });
  return {
    slides: measuredSlides,
    visible_navigation_controls: visibleNavigationControls
  };
}"""
