"""Browser DOM grounding for rendered AutoDesign HTML frames.

The renderer owns the DOM, so browser geometry is the cheapest reliable
ground truth for output-layout failures. This module is optional like
``browser_render``: missing Playwright returns explicit warnings instead of
failing smoke tests.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


Rect = dict[str, float]


@dataclass
class LayoutGroundingResult:
    backend: str
    warnings: list[str] = field(default_factory=list)
    elements: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.backend == "playwright" and not self.warnings

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "warnings": self.warnings,
            "elements": self.elements,
            "issues": self.issues,
        }


def ground_html_layout(
    html_path: Path,
    frame_selector: str,
    *,
    viewport_width: int,
    viewport_height: int,
    timeout_ms: int = 15_000,
) -> LayoutGroundingResult:
    """Return DOM-derived element boxes and layout issues for an HTML frame.

    Coordinates are relative to each matched frame. For posters this is the
    single ``.canvas``; for decks it can be every ``.deck-slide``. The report
    intentionally uses simple JSON dictionaries so it can be persisted into
    composite payloads and fed back to the planner without schema churn.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return LayoutGroundingResult(
            backend="unavailable",
            warnings=[f"playwright_unavailable: {type(e).__name__}: {e}"],
        )

    try:
        with sync_playwright() as p:
            browser = _launch_chromium(p)
            page = browser.new_page(
                viewport={
                    "width": max(1, int(viewport_width)),
                    "height": max(1, int(viewport_height)),
                },
                device_scale_factor=1,
            )
            page.set_default_timeout(timeout_ms)
            page.set_default_navigation_timeout(timeout_ms)
            page.goto(
                html_path.resolve().as_uri(),
                wait_until="load",
                timeout=timeout_ms,
            )
            try:
                page.wait_for_load_state("networkidle", timeout=min(2000, timeout_ms))
            except Exception:
                pass
            payload = page.evaluate(_GROUNDING_JS, frame_selector)
            browser.close()
    except Exception as e:
        return LayoutGroundingResult(
            backend="unavailable",
            warnings=[f"playwright_grounding_failed: {type(e).__name__}: {e}"],
        )

    warnings = list(payload.get("warnings") or [])
    elements = list(payload.get("elements") or [])
    issues = _detect_layout_issues(elements)
    return LayoutGroundingResult(
        backend="playwright",
        warnings=warnings,
        elements=elements,
        issues=issues,
    )


def _launch_chromium(p: Any) -> Any:
    try:
        return p.chromium.launch(args=["--no-sandbox"])
    except Exception as primary:
        allow_chrome = (
            os.getenv("AUTODESIGN_ALLOW_CHROME_CHANNEL_FALLBACK", "").strip()
            or os.getenv("DESIGN_ANYTHING_ALLOW_CHROME_CHANNEL_FALLBACK", "").strip()
        ).lower() in {"1", "true", "yes", "on"}
        if not allow_chrome:
            raise RuntimeError(
                f"{primary}; install Playwright Chromium with "
                "`uv run python -m playwright install chromium`"
            ) from primary
        try:
            return p.chromium.launch(channel="chrome", args=["--no-sandbox"])
        except Exception as secondary:
            raise RuntimeError(
                f"{primary}; chrome_channel_failed: {secondary}"
            ) from secondary


def _detect_layout_issues(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    visible = [
        e for e in elements
        if e.get("layer_id")
        and e.get("visible", True)
        and e.get("box_rect")
        and not _is_empty_text(e)
    ]

    for e in visible:
        frame = e.get("frame_rect") or {}
        box = e.get("box_rect") or {}
        ink = _rect_for_collision(e)
        fw = float(frame.get("w") or 0)
        fh = float(frame.get("h") or 0)
        if fw <= 0 or fh <= 0:
            continue
        overflow = {
            "left": max(0.0, -float(box.get("x") or 0)),
            "top": max(0.0, -float(box.get("y") or 0)),
            "right": max(0.0, float(box.get("x") or 0) + float(box.get("w") or 0) - fw),
            "bottom": max(0.0, float(box.get("y") or 0) + float(box.get("h") or 0) - fh),
        }
        if max(overflow.values()) > 1.0:
            issues.append({
                "type": "off_canvas",
                "severity": "high",
                "frame_id": e.get("frame_id"),
                "layer_id": e.get("layer_id"),
                "kind": e.get("kind"),
                "overflow_px": _round_rect(overflow),
                "box_rect": _round_rect(box),
            })

        if _is_text(e):
            text_over = _text_overflow(e, ink)
            if max(text_over.values()) > 2.0:
                issues.append({
                    "type": "text_overflow_box",
                    "severity": "high",
                    "frame_id": e.get("frame_id"),
                    "layer_id": e.get("layer_id"),
                    "kind": e.get("kind"),
                    "overflow_px": _round_rect(text_over),
                    "box_rect": _round_rect(box),
                    "ink_rect": _round_rect(ink),
                })

        clipped_bottom = max(
            0.0,
            float(ink.get("y") or 0) + float(ink.get("h") or 0) - fh,
        )
        if clipped_bottom > 2.0:
            issues.append({
                "type": "clipped_bottom",
                "severity": "high",
                "frame_id": e.get("frame_id"),
                "layer_id": e.get("layer_id"),
                "kind": e.get("kind"),
                "overflow_px": {"bottom": round(clipped_bottom, 2)},
                "box_rect": _round_rect(box),
                "ink_rect": _round_rect(ink),
            })

    for i, a in enumerate(visible):
        for b in visible[i + 1:]:
            if a.get("frame_id") != b.get("frame_id"):
                continue
            if _is_background(a) or _is_background(b):
                continue
            if _is_layout_container(a) or _is_layout_container(b):
                continue
            ar = _rect_for_collision(a)
            br = _rect_for_collision(b)
            ov = _overlap(ar, br)
            if ov is None:
                continue
            x_ov, y_ov, area = ov
            if x_ov < 4 or y_ov < 2 or area < 24:
                continue
            a_text = _is_text(a)
            b_text = _is_text(b)
            if a_text and b_text:
                issue_type = "text_text_overlap"
            elif a_text or b_text:
                issue_type = "text_visual_overlap"
            else:
                continue
            issues.append({
                "type": issue_type,
                "severity": "high",
                "frame_id": a.get("frame_id"),
                "layer_a": a.get("layer_id"),
                "kind_a": a.get("kind"),
                "layer_b": b.get("layer_id"),
                "kind_b": b.get("kind"),
                "overlap_px": {
                    "x": round(x_ov, 2),
                    "y": round(y_ov, 2),
                    "area": round(area, 2),
                },
                "rect_a": _round_rect(ar),
                "rect_b": _round_rect(br),
            })

    return issues


def _is_text(e: dict[str, Any]) -> bool:
    kind = str(e.get("kind") or "")
    classes = str(e.get("class_name") or "")
    return kind == "text" or " text" in f" {classes} " or "od-text" in classes


def _is_empty_text(e: dict[str, Any]) -> bool:
    if not _is_text(e):
        return False
    try:
        return int(e.get("text_chars") or 0) <= 0
    except (TypeError, ValueError):
        return False


def _is_background(e: dict[str, Any]) -> bool:
    kind = str(e.get("kind") or "")
    classes = str(e.get("class_name") or "")
    return kind == "background" or " bg" in f" {classes} " or "od-bg" in classes


def _is_layout_container(e: dict[str, Any]) -> bool:
    """Non-visual structural frames should not collide with their children."""
    kind = str(e.get("kind") or "")
    classes = f" {e.get('class_name') or ''} "
    return (
        kind in {"section", "slide", "group", "shape"}
        or " ld-section " in classes
        or " deck-slide " in classes
    )


def _rect_for_collision(e: dict[str, Any]) -> Rect:
    if _is_empty_text(e):
        return {"x": 0, "y": 0, "w": 0, "h": 0}
    if _is_text(e) and e.get("ink_rect"):
        return e["ink_rect"]
    return e.get("box_rect") or {"x": 0, "y": 0, "w": 0, "h": 0}


def _text_overflow(e: dict[str, Any], ink: Rect) -> dict[str, float]:
    box = e.get("box_rect") or {}
    scroll = e.get("scroll_overflow") or {}
    clips_x = _clips_overflow(e, axis="x")
    clips_y = _clips_overflow(e, axis="y")
    left = (
        max(0.0, float(box.get("x") or 0) - float(ink.get("x") or 0))
        if clips_x else 0.0
    )
    top = (
        max(0.0, float(box.get("y") or 0) - float(ink.get("y") or 0))
        if clips_y else 0.0
    )
    right = (
        max(
            0.0,
            float(ink.get("x") or 0) + float(ink.get("w") or 0)
            - float(box.get("x") or 0) - float(box.get("w") or 0),
        )
        if clips_x else 0.0
    )
    bottom = (
        max(
            0.0,
            float(ink.get("y") or 0) + float(ink.get("h") or 0)
            - float(box.get("y") or 0) - float(box.get("h") or 0),
        )
        if clips_y else 0.0
    )
    return {
        "left": max(left, float(scroll.get("x") or 0) if clips_x else 0.0),
        "top": top,
        "right": right,
        "bottom": max(bottom, float(scroll.get("y") or 0) if clips_y else 0.0),
    }


def _clips_overflow(e: dict[str, Any], *, axis: str) -> bool:
    base = str(e.get("overflow") or "").strip().lower()
    axis_key = "overflow_x" if axis == "x" else "overflow_y"
    value = str(e.get(axis_key) or base).strip().lower()
    if not value or value in {"visible", "unset", "initial", "inherit"}:
        return False
    return True


def _overlap(a: Rect, b: Rect) -> tuple[float, float, float] | None:
    ax = float(a.get("x") or 0)
    ay = float(a.get("y") or 0)
    aw = float(a.get("w") or 0)
    ah = float(a.get("h") or 0)
    bx = float(b.get("x") or 0)
    by = float(b.get("y") or 0)
    bw = float(b.get("w") or 0)
    bh = float(b.get("h") or 0)
    x_ov = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    y_ov = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    if x_ov <= 0 or y_ov <= 0:
        return None
    return x_ov, y_ov, x_ov * y_ov


def _round_rect(rect: dict[str, Any]) -> dict[str, float]:
    return {
        str(k): round(float(v or 0), 2)
        for k, v in rect.items()
    }


_GROUNDING_JS = r"""
(frameSelector) => {
  const warnings = [];
  const frames = Array.from(document.querySelectorAll(frameSelector || 'body'));
  if (!frames.length) warnings.push('frame_selector_not_found: ' + frameSelector);

  function rectObj(rect, frameRect) {
    return {
      x: rect.left - frameRect.left,
      y: rect.top - frameRect.top,
      w: rect.width,
      h: rect.height,
    };
  }

  function unionRects(rects, frameRect) {
    let left = Infinity, top = Infinity, right = -Infinity, bottom = -Infinity;
    for (const rect of rects) {
      if (!rect || rect.width <= 0 || rect.height <= 0) continue;
      left = Math.min(left, rect.left);
      top = Math.min(top, rect.top);
      right = Math.max(right, rect.right);
      bottom = Math.max(bottom, rect.bottom);
    }
    if (!Number.isFinite(left)) return null;
    return rectObj({left, top, width: right - left, height: bottom - top}, frameRect);
  }

  function inferKind(el) {
    const explicit = el.getAttribute('data-kind');
    if (explicit) return explicit;
    const cls = el.className || '';
    if (/\bod-text\b|\btext\b/.test(cls)) return 'text';
    if (/\bod-bg\b|\bbg\b/.test(cls)) return 'background';
    if (/\bod-image\b|\bimage\b/.test(cls)) return 'image';
    if (/\bod-table\b|\btable\b/.test(cls) || el.querySelector('table')) return 'table';
    if (/\bod-callout\b/.test(cls)) return 'callout';
    return 'unknown';
  }

  function isVisible(el) {
    const style = window.getComputedStyle(el);
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      parseFloat(style.opacity || '1') > 0.01;
  }

  function textInkRect(el, frameRect) {
    const walker = document.createTreeWalker(
      el,
      NodeFilter.SHOW_TEXT,
      {
        acceptNode(node) {
          if (!node.nodeValue || !node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
          const parent = node.parentElement;
          if (parent && parent.closest('.ld-drag-handle')) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        }
      }
    );
    const rects = [];
    while (walker.nextNode()) {
      const range = document.createRange();
      range.selectNodeContents(walker.currentNode);
      rects.push(...Array.from(range.getClientRects()));
      range.detach();
    }
    return unionRects(rects, frameRect);
  }

  const elements = [];
  frames.forEach((frame, frameIndex) => {
    const frameRectRaw = frame.getBoundingClientRect();
    const frameRect = {
      left: frameRectRaw.left,
      top: frameRectRaw.top,
      right: frameRectRaw.right,
      bottom: frameRectRaw.bottom,
      width: frameRectRaw.width,
      height: frameRectRaw.height,
    };
    const frameId = frame.getAttribute('data-layer-id') ||
      frame.getAttribute('data-slide-index') ||
      frame.id ||
      String(frameIndex);
    const layers = Array.from(frame.querySelectorAll('[data-layer-id]'));
    layers.forEach((el) => {
      if (el === frame) return;
      const boxRaw = el.getBoundingClientRect();
      const kind = inferKind(el);
      const box = rectObj(boxRaw, frameRect);
      const textChars = kind === 'text' ? (el.innerText || '').trim().length : 0;
      const ink = kind === 'text'
        ? (textChars > 0 ? (textInkRect(el, frameRect) || box) : null)
        : box;
      const style = window.getComputedStyle(el);
      elements.push({
        frame_id: String(frameId),
        frame_index: frameIndex,
        frame_rect: {x: 0, y: 0, w: frameRect.width, h: frameRect.height},
        layer_id: el.getAttribute('data-layer-id') || '',
        name: el.getAttribute('data-layer-name') || '',
        kind,
        class_name: String(el.className || ''),
        z_index: Number(el.getAttribute('data-z-index') || style.zIndex || 0) || 0,
        visible: isVisible(el),
        overflow: style.overflow,
        overflow_x: style.overflowX,
        overflow_y: style.overflowY,
        box_rect: box,
        ink_rect: ink,
        scroll_overflow: {
          x: Math.max(0, el.scrollWidth - el.clientWidth),
          y: Math.max(0, el.scrollHeight - el.clientHeight),
        },
        text_chars: textChars,
      });
    });
  });
  return {warnings, elements};
}
"""
