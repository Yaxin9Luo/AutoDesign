#!/usr/bin/env python3
"""Strictly read-only DOM measurements for the portable Poster Skill."""

from __future__ import annotations

import argparse
import math
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping


sys.dont_write_bytecode = True

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import _portable as core  # noqa: E402
import setup_browser  # noqa: E402


REPORT_FORMAT_VERSION = 1
ROOT_OVERFLOW_TOLERANCE_PX = 4.0
TEXT_CLIP_TOLERANCE_PX = 1.0
VIEWPORT_TOLERANCE_PX = 4.0
IMAGE_EFFECTIVE_RESOLUTION_MIN = 1.5
TABLE_OVERFLOW_TOLERANCE_PX = 4.0
TABLE_MIN_FONT_PX = 24.0
SOURCE_FLOW_MIN_GUTTER_PX = 18.0
BLANK_BAND_ROWS = 24
BLANK_BAND_MIN_RATIO = 0.28

STABLE_FINDING_CODES = (
    "poster-dom-root-overflow",
    "poster-dom-text-clipping",
    "poster-dom-text-overlap",
    "poster-dom-viewport-escape",
    "poster-dom-blank-band",
    "poster-dom-sparse-oversized-panel",
    "poster-dom-image-low-effective-resolution",
    "poster-dom-table-overflow",
    "poster-dom-table-text-small",
    "poster-dom-source-flow-gutter",
    "poster-dom-source-flow-sibling",
    "poster-dom-screen-print-mismatch",
    "poster-dom-template-boxiness",
)
_FINDING_ORDER = {code: index for index, code in enumerate(STABLE_FINDING_CODES)}
_FONT_READY_SCRIPT = (
    "async () => { if (document.fonts && document.fonts.ready) "
    "await document.fonts.ready; return true; }"
)


_READ_ONLY_MEASUREMENT_SCRIPT = r"""
(media) => {
  const round = (value) => Math.round(Number(value || 0) * 100) / 100;
  const rectOf = (value) => ({
    x: round(value.x), y: round(value.y), w: round(value.width), h: round(value.height),
    right: round(value.right), bottom: round(value.bottom),
  });
  const root = document.querySelector('main.paper-poster[data-autodesign-artifact="poster"]')
    || document.querySelector('.paper-poster');
  if (!root) throw new Error('Poster DOM root is missing');
  const rootRect = root.getBoundingClientRect();
  const identity = (element) => {
    let current = element && element.nodeType === Node.ELEMENT_NODE ? element : element?.parentElement;
    while (current) {
      for (const name of ['data-block-id', 'data-claim-id', 'data-section-role', 'data-role', 'id']) {
        const value = current.getAttribute && current.getAttribute(name);
        if (value) return String(value).slice(0, 160);
      }
      if (current === root) break;
      current = current.parentElement;
    }
    return 'paper-poster-root';
  };
  const words = (value) => (String(value || '').match(/[A-Za-z0-9][A-Za-z0-9_+./%-]*/g) || []).length;
  const evidenceTokens = (element) => {
    const result = new Set();
    if (!element) return result;
    const value = element.getAttribute('data-source-ids') || '';
    for (const token of value.split(/[\s,;]+/)) if (token) result.add(token);
    return result;
  };
  const clippedRect = (node, sourceRect) => {
    let left = sourceRect.left;
    let top = sourceRect.top;
    let right = sourceRect.right;
    let bottom = sourceRect.bottom;
    let clippedBy = '';
    let current = node.parentElement;
    while (current) {
      const style = getComputedStyle(current);
      const clipsX = /hidden|clip|scroll|auto/.test(style.overflowX);
      const clipsY = /hidden|clip|scroll|auto/.test(style.overflowY);
      if (clipsX || clipsY) {
        const boundary = current.getBoundingClientRect();
        const nextLeft = clipsX ? Math.max(left, boundary.left) : left;
        const nextRight = clipsX ? Math.min(right, boundary.right) : right;
        const nextTop = clipsY ? Math.max(top, boundary.top) : top;
        const nextBottom = clipsY ? Math.min(bottom, boundary.bottom) : bottom;
        if (nextLeft > left || nextRight < right || nextTop > top || nextBottom < bottom) {
          clippedBy ||= identity(current);
        }
        left = nextLeft;
        right = nextRight;
        top = nextTop;
        bottom = nextBottom;
      }
      if (current === root) break;
      current = current.parentElement;
    }
    return {
      rect: {x: left, y: top, width: Math.max(0, right - left), height: Math.max(0, bottom - top), right, bottom},
      clippedBy,
    };
  };
  const visible = (element) => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || 1) > 0
      && rect.width > 0 && rect.height > 0;
  };
  const textRectsWithin = (container) => {
    const result = [];
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (!String(node.nodeValue || '').trim() || !visible(node.parentElement)) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      for (const item of range.getClientRects()) {
        if (item.width > 0 && item.height > 0) result.push(rectOf(item));
      }
    }
    return result;
  };
  const hasDistinctBackground = (value) => {
    const normalized = String(value || '').trim().toLowerCase();
    if (!normalized || normalized === 'transparent') return false;
    const match = normalized.match(
      /^rgba?\(\s*([\d.]+)\s*[, ]\s*([\d.]+)\s*[, ]\s*([\d.]+)(?:\s*[,/]\s*([\d.]+))?\s*\)$/
    );
    if (!match) return true;
    const red = Number(match[1]);
    const green = Number(match[2]);
    const blue = Number(match[3]);
    const alpha = match[4] === undefined ? 1 : Number(match[4]);
    return alpha > 0 && Math.min(red, green, blue) < 248;
  };

  const textNodes = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode;
    const text = String(node.nodeValue || '').trim();
    if (!text || !visible(node.parentElement)) continue;
    const range = document.createRange();
    range.selectNodeContents(node);
    const source = range.getBoundingClientRect();
    if (source.width <= 0 || source.height <= 0) continue;
    const clipped = clippedRect(node, source);
    textNodes.push({
      block_id: identity(node.parentElement), text: text.slice(0, 1200), rect: rectOf(source),
      visible_rect: rectOf(clipped.rect), clipped_by: clipped.clippedBy,
    });
  }

  const elementSelector = [
    '[data-block-id]', '[data-claim-id]', '[data-section-role]', '[data-role]',
    'section', 'article', '.panel', '.metric-card', '.source-flow-unit'
  ].join(',');
  const elements = Array.from(root.querySelectorAll(elementSelector)).filter(visible).map((element) => {
    const style = getComputedStyle(element);
    const background = style.backgroundColor;
    const border = Math.max(
      parseFloat(style.borderTopWidth) || 0, parseFloat(style.borderRightWidth) || 0,
      parseFloat(style.borderBottomWidth) || 0, parseFloat(style.borderLeftWidth) || 0
    );
    return {
      block_id: identity(element), tag: element.tagName.toLowerCase(),
      role: element.getAttribute('data-role') || element.getAttribute('data-section-role') || '',
      class_name: String(element.className || '').slice(0, 240),
      text: String(element.textContent || '').trim().slice(0, 1200),
      word_count: words(element.textContent), rect: rectOf(element.getBoundingClientRect()),
      border_width_px: round(border),
      background_distinct: hasDistinctBackground(background),
      has_shadow: style.boxShadow !== 'none',
    };
  });

  const images = Array.from(root.querySelectorAll('img')).filter(visible).map((element) => ({
    block_id: identity(element), source_id: element.getAttribute('data-source-id') || '',
    rect: rectOf(element.getBoundingClientRect()), complete: Boolean(element.complete),
    naturalWidth: Number(element.naturalWidth || 0), naturalHeight: Number(element.naturalHeight || 0),
  }));

  const tables = Array.from(root.querySelectorAll('table')).filter(visible).map((element) => {
    const container = element.parentElement;
    const fonts = Array.from(element.querySelectorAll('th,td')).map((cell) => parseFloat(getComputedStyle(cell).fontSize) || 0);
    return {
      block_id: identity(element), rect: rectOf(element.getBoundingClientRect()),
      container_rect: rectOf(container.getBoundingClientRect()),
      scrollWidth: element.scrollWidth, scrollHeight: element.scrollHeight,
      clientWidth: element.clientWidth, clientHeight: element.clientHeight,
      overflowX: getComputedStyle(element).overflowX, overflowY: getComputedStyle(element).overflowY,
      font_px: fonts.length ? Math.min(...fonts.filter((value) => value > 0)) : (parseFloat(getComputedStyle(element).fontSize) || 0),
    };
  });

  const panels = Array.from(root.querySelectorAll(
    '[data-panel],.panel,.poster-section,.flow-panel,[data-panel-role],[data-slot-id],section,article,aside,figure,div'
  )).filter((element) => {
    if (!visible(element)) return false;
    const haystack = [
      element.getAttribute('data-block-id') || '', element.getAttribute('data-panel-role') || '',
      element.getAttribute('data-slot-id') || '', element.getAttribute('data-role') || '',
      String(element.className || '')
    ].join(' ').toLowerCase();
    return /(panel|slot|evidence|method|result|analysis|limitation)/.test(haystack);
  }).map((element) => {
      const contentRects = textRectsWithin(element);
      for (const child of element.querySelectorAll('img,table,figure,svg,canvas')) {
        if (visible(child)) contentRects.push(rectOf(child.getBoundingClientRect()));
      }
      return {block_id: identity(element), rect: rectOf(element.getBoundingClientRect()),
        word_count: words(element.textContent), content_rects: contentRects};
    });

  const lists = Array.from(root.querySelectorAll('ol,ul')).filter(visible).map((element) => {
    const flow = element.closest('.source-flow-unit');
    const siblings = flow ? Array.from(flow.children) : [];
    const floatedSource = siblings.some((item) => {
      if (item === element || !item.matches('figure,img,picture,[data-source-id]')) return false;
      return getComputedStyle(item).float !== 'none';
    });
    const style = getComputedStyle(element);
    return {
      block_id: identity(element), rect: rectOf(element.getBoundingClientRect()),
      item_count: element.querySelectorAll(':scope > li').length,
      has_source_flow_ancestor: Boolean(flow), is_direct_source_flow_child: Boolean(flow && element.parentElement === flow),
      has_floated_source_sibling: floatedSource,
      paddingInlineStartPx: parseFloat(style.paddingInlineStart) || 0,
      paddingLeftPx: parseFloat(style.paddingLeft) || 0,
      textIndentPx: parseFloat(style.textIndent) || 0,
    };
  });

  const sourceFlows = Array.from(root.querySelectorAll('.source-flow-unit')).filter(visible).map((flow) => {
    const children = Array.from(flow.children);
    const sources = children.filter((item) =>
      item.matches('figure,img,picture,[data-source-id]')
      && !item.matches('ol,ul,[data-source-readout],[data-role*="readout"]')
    );
    const readouts = children.filter((item) => item.matches('ol,ul,[data-source-readout],[data-role*="readout"]'));
    let directSibling = false;
    let evidenceIntersects = false;
    for (const source of sources) for (const readout of readouts) {
      const adjacent = source.nextElementSibling === readout || readout.nextElementSibling === source;
      if (adjacent) directSibling = true;
      const sourceTokens = evidenceTokens(source);
      const readoutTokens = evidenceTokens(readout);
      if (adjacent && Array.from(sourceTokens).some((token) => readoutTokens.has(token))) {
        evidenceIntersects = true;
      }
    }
    return {
      block_id: identity(flow), rect: rectOf(flow.getBoundingClientRect()),
      source_child_count: sources.length, readout_child_count: readouts.length,
      direct_sibling: directSibling, evidence_ids_intersect: evidenceIntersects,
    };
  });

  const rootStyle = getComputedStyle(root);
  return {
    media,
    viewport: {width: window.innerWidth, height: window.innerHeight,
      document_width: document.documentElement.scrollWidth, document_height: document.documentElement.scrollHeight},
    root: {block_id: identity(root), rect: rectOf(rootRect), scrollWidth: root.scrollWidth,
      scrollHeight: root.scrollHeight, clientWidth: root.clientWidth, clientHeight: root.clientHeight,
      overflowX: rootStyle.overflowX, overflowY: rootStyle.overflowY},
    text_nodes: textNodes, elements, images, tables, lists, panels, source_flows: sourceFlows,
  };
}
"""


def browser_probe_script() -> str:
    """Return the one read-only JavaScript measurement source used by the probe."""

    return _READ_ONLY_MEASUREMENT_SCRIPT


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _rect(value: Any) -> dict[str, float]:
    raw = value if isinstance(value, Mapping) else {}
    x = _number(raw.get("x"))
    y = _number(raw.get("y"))
    width = max(0.0, _number(raw.get("w", raw.get("width"))))
    height = max(0.0, _number(raw.get("h", raw.get("height"))))
    return {
        "x": x,
        "y": y,
        "w": width,
        "h": height,
        "right": _number(raw.get("right"), x + width),
        "bottom": _number(raw.get("bottom"), y + height),
    }


def _rounded_rect(value: Any) -> dict[str, float]:
    return {name: round(number, 2) for name, number in _rect(value).items()}


def _overlap_area(left: Mapping[str, float], right: Mapping[str, float]) -> float:
    x1 = max(left["x"], right["x"])
    y1 = max(left["y"], right["y"])
    x2 = min(left["right"], right["right"])
    y2 = min(left["bottom"], right["bottom"])
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def _word_count(value: Any) -> int:
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9_+./%-]*", str(value or "")))


def _finding(
    code: str,
    block_id: str,
    severity: str,
    geometry: Mapping[str, Any],
    message: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "block_id": block_id,
        "severity": severity,
        "geometry": dict(geometry),
        "message": message,
        "suggested_repair_route": "layout_repair",
    }


def _content_blank_band(
    panel_rect: Mapping[str, float], content_rects: list[dict[str, float]]
) -> dict[str, Any]:
    row_height = max(1.0, panel_rect["h"] / BLANK_BAND_ROWS)
    marked: list[bool] = []
    for row in range(BLANK_BAND_ROWS):
        top = panel_rect["y"] + row * row_height
        bottom = top + row_height
        overlap_width = 0.0
        for content in content_rects:
            height = min(content["bottom"], bottom) - max(content["y"], top)
            if height <= row_height * 0.08:
                continue
            overlap_width += max(
                0.0,
                min(content["right"], panel_rect["right"])
                - max(content["x"], panel_rect["x"]),
            )
        marked.append(overlap_width >= panel_rect["w"] * 0.08)
    best_start = 0
    best_length = 0
    run_start = 0
    run_length = 0
    for index, occupied in enumerate(marked):
        if occupied:
            run_length = 0
            continue
        if run_length == 0:
            run_start = index
        run_length += 1
        if run_length > best_length:
            best_start = run_start
            best_length = run_length
    return {
        "max_blank_run_ratio": round(best_length / BLANK_BAND_ROWS, 4),
        "blank_start_ratio": round(best_start / BLANK_BAND_ROWS, 4),
        "blank_end_ratio": round((best_start + best_length) / BLANK_BAND_ROWS, 4),
    }


def _panel_content_area_ratio(
    panel_rect: Mapping[str, float], content_rects: list[dict[str, float]]
) -> float:
    area = sum(_overlap_area(panel_rect, item) for item in content_rects)
    return round(min(1.0, area / max(1.0, panel_rect["w"] * panel_rect["h"])), 4)


def _clip_rect_to_bounds(
    rect: Mapping[str, float], bounds: Mapping[str, float]
) -> dict[str, float] | None:
    left = max(rect["x"], bounds["x"])
    top = max(rect["y"], bounds["y"])
    right = min(rect["right"], bounds["right"])
    bottom = min(rect["bottom"], bounds["bottom"])
    if right <= left or bottom <= top:
        return None
    return {
        "x": left,
        "y": top,
        "w": right - left,
        "h": bottom - top,
        "right": right,
        "bottom": bottom,
    }


def _canvas_content_boxes(
    snapshot: Mapping[str, Any], canvas_rect: Mapping[str, float]
) -> list[dict[str, float]]:
    candidates: list[Mapping[str, Any]] = []
    for text in snapshot.get("text_nodes", []):
        if isinstance(text, Mapping) and _word_count(text.get("text")) >= 2:
            candidates.append({"rect": text.get("visible_rect", text.get("rect"))})
    for key in ("images", "tables"):
        candidates.extend(
            item for item in snapshot.get(key, []) if isinstance(item, Mapping)
        )

    boxes: list[dict[str, float]] = []
    seen: set[tuple[float, float, float, float]] = set()
    for candidate in candidates:
        clipped = _clip_rect_to_bounds(_rect(candidate.get("rect")), canvas_rect)
        if clipped is None or clipped["w"] * clipped["h"] < 120:
            continue
        key = tuple(round(clipped[name], 2) for name in ("x", "y", "w", "h"))
        if key not in seen:
            seen.add(key)
            boxes.append(clipped)
    return boxes


def _band_grid_coverage(
    boxes: list[dict[str, float]],
    canvas_rect: Mapping[str, float],
    *,
    start_ratio: float,
    end_ratio: float,
) -> float:
    band_top = canvas_rect["y"] + canvas_rect["h"] * start_ratio
    band_bottom = canvas_rect["y"] + canvas_rect["h"] * end_ratio
    columns = 48
    rows = 12
    cell_width = canvas_rect["w"] / columns
    cell_height = max(1.0, band_bottom - band_top) / rows
    marked = 0
    for row in range(rows):
        for column in range(columns):
            cell = {
                "x": canvas_rect["x"] + column * cell_width,
                "y": band_top + row * cell_height,
                "w": cell_width,
                "h": cell_height,
                "right": canvas_rect["x"] + (column + 1) * cell_width,
                "bottom": band_top + (row + 1) * cell_height,
            }
            cell_area = max(1.0, cell_width * cell_height)
            if any(_overlap_area(box, cell) >= cell_area * 0.03 for box in boxes):
                marked += 1
    return round(marked / float(columns * rows), 4)


def _canvas_fill_metrics(
    snapshot: Mapping[str, Any], canvas_rect: Mapping[str, float]
) -> tuple[dict[str, Any], list[dict[str, float]]]:
    boxes = _canvas_content_boxes(snapshot, canvas_rect)
    if boxes:
        content_bottom = max(box["bottom"] for box in boxes)
        bottom_ratio = min(
            1.0,
            max(0.0, (content_bottom - canvas_rect["y"]) / canvas_rect["h"]),
        )
    else:
        content_bottom = canvas_rect["y"]
        bottom_ratio = 0.0
    portrait = canvas_rect["h"] > canvas_rect["w"]
    minimums = {
        "min_content_bottom_ratio": 0.92 if portrait else 0.90,
        "min_lower_quarter_content_coverage": 0.10 if portrait else 0.12,
        "min_lower_half_content_coverage": 0.18 if portrait else 0.22,
        "min_middle_lower_content_coverage": 0.16 if portrait else 0.18,
    }
    metrics = {
        "canvas_content_box_count": len(boxes),
        "canvas_content_bottom_px": round(content_bottom - canvas_rect["y"], 2),
        "canvas_content_bottom_ratio": round(bottom_ratio, 4),
        "canvas_lower_blank_ratio": round(1.0 - bottom_ratio, 4),
        "canvas_lower_quarter_content_coverage": _band_grid_coverage(
            boxes, canvas_rect, start_ratio=0.75, end_ratio=1.0
        ),
        "canvas_lower_half_content_coverage": _band_grid_coverage(
            boxes, canvas_rect, start_ratio=0.50, end_ratio=1.0
        ),
        "canvas_middle_lower_content_coverage": _band_grid_coverage(
            boxes, canvas_rect, start_ratio=0.42, end_ratio=0.74
        ),
        **minimums,
    }
    reasons: list[str] = []
    if metrics["canvas_content_bottom_ratio"] < minimums["min_content_bottom_ratio"]:
        reasons.append("content_stops_before_bottom")
    if (
        metrics["canvas_lower_quarter_content_coverage"]
        < minimums["min_lower_quarter_content_coverage"]
    ):
        reasons.append("lower_quarter_sparse")
    if (
        metrics["canvas_lower_half_content_coverage"]
        < minimums["min_lower_half_content_coverage"]
    ):
        reasons.append("lower_half_sparse")
    if (
        metrics["canvas_middle_lower_content_coverage"]
        < minimums["min_middle_lower_content_coverage"]
    ):
        reasons.append("middle_lower_sparse")
    metrics["canvas_fill_reasons"] = reasons
    return metrics, boxes


def _matches_size(rect: Mapping[str, float], width: float, height: float) -> bool:
    return abs(rect["w"] - width) <= 2.0 and abs(rect["h"] - height) <= 2.0


def evaluate_dom_snapshot(
    snapshot: Mapping[str, Any],
    *,
    canvas: Mapping[str, Any],
    print_size: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate one immutable media snapshot without filesystem or DOM writes."""

    canvas_width = _number(canvas.get("width_px"))
    canvas_height = _number(canvas.get("height_px"))
    print_width = _number(print_size.get("width_mm")) / 25.4 * 96.0
    print_height = _number(print_size.get("height_mm")) / 25.4 * 96.0
    media = str(snapshot.get("media") or "")
    if media not in {"screen", "print"}:
        raise ValueError("DOM snapshot media must be 'screen' or 'print'")
    findings: list[dict[str, Any]] = []

    root = snapshot.get("root")
    root = root if isinstance(root, Mapping) else {}
    root_rect = _rect(root.get("rect"))
    matches_canvas = _matches_size(root_rect, canvas_width, canvas_height)
    matches_physical_print = _matches_size(root_rect, print_width, print_height)
    use_physical_print = (
        media == "print" and matches_physical_print and not matches_canvas
    )
    evaluation_width = print_width if use_physical_print else canvas_width
    evaluation_height = print_height if use_physical_print else canvas_height
    root_client_width = _number(root.get("clientWidth")) or root_rect["w"]
    root_client_height = _number(root.get("clientHeight")) or root_rect["h"]
    root_width_gap = max(
        0.0,
        _number(root.get("scrollWidth")) - root_client_width,
    )
    root_height_gap = max(
        0.0,
        _number(root.get("scrollHeight")) - root_client_height,
    )
    viewport = snapshot.get("viewport")
    viewport = viewport if isinstance(viewport, Mapping) else {}
    viewport_width = _number(viewport.get("width")) or canvas_width
    viewport_height = _number(viewport.get("height")) or canvas_height
    document_bound_width = max(viewport_width, evaluation_width)
    document_bound_height = max(viewport_height, evaluation_height)
    document_width_gap = max(
        0.0,
        _number(viewport.get("document_width"), document_bound_width)
        - document_bound_width,
    )
    document_height_gap = max(
        0.0,
        _number(viewport.get("document_height"), document_bound_height)
        - document_bound_height,
    )
    maximum_root_gap = max(
        root_width_gap,
        root_height_gap,
        document_width_gap,
        document_height_gap,
    )
    if maximum_root_gap > ROOT_OVERFLOW_TOLERANCE_PX:
        findings.append(
            _finding(
                "poster-dom-root-overflow",
                str(root.get("block_id") or "paper-poster-root"),
                "P0" if maximum_root_gap >= 24 else "P1",
                {
                    "root_rect": _rounded_rect(root_rect),
                    "width_gap_px": round(root_width_gap, 2),
                    "height_gap_px": round(root_height_gap, 2),
                    "document_width_px": round(
                        _number(viewport.get("document_width")), 2
                    ),
                    "document_height_px": round(
                        _number(viewport.get("document_height")), 2
                    ),
                    "document_width_gap_px": round(document_width_gap, 2),
                    "document_height_gap_px": round(document_height_gap, 2),
                },
                "Poster root or document has rendered scroll overflow beyond the fixed viewport.",
            )
        )

    root_viewport_gaps = {
        "left_gap_px": max(0.0, -root_rect["x"]),
        "top_gap_px": max(0.0, -root_rect["y"]),
        "right_gap_px": max(0.0, root_rect["right"] - evaluation_width),
        "bottom_gap_px": max(0.0, root_rect["bottom"] - evaluation_height),
    }
    if max(root_viewport_gaps.values()) > VIEWPORT_TOLERANCE_PX:
        findings.append(
            _finding(
                "poster-dom-viewport-escape",
                str(root.get("block_id") or "paper-poster-root"),
                "P0" if max(root_viewport_gaps.values()) >= 24 else "P1",
                {
                    **{
                        key: round(value, 2)
                        for key, value in root_viewport_gaps.items()
                    },
                    "rect": _rounded_rect(root_rect),
                    "evaluation_width_px": round(evaluation_width, 2),
                    "evaluation_height_px": round(evaluation_height, 2),
                },
                "Rendered Poster root escapes the fixed canvas viewport.",
            )
        )

    text_nodes = [
        item for item in snapshot.get("text_nodes", []) if isinstance(item, Mapping)
    ]
    for item in text_nodes:
        rect = _rect(item.get("rect"))
        visible = _rect(item.get("visible_rect", item.get("rect")))
        clipped_width = max(0.0, rect["w"] - visible["w"])
        clipped_height = max(0.0, rect["h"] - visible["h"])
        if str(item.get("clipped_by") or "") and max(clipped_width, clipped_height) > TEXT_CLIP_TOLERANCE_PX:
            findings.append(
                _finding(
                    "poster-dom-text-clipping",
                    str(item.get("block_id") or "text"),
                    "P0" if clipped_height >= 12 or clipped_width >= 24 else "P1",
                    {
                        "text_rect": _rounded_rect(rect),
                        "visible_rect": _rounded_rect(visible),
                        "clipped_by": str(item.get("clipped_by") or ""),
                        "clipped_width_px": round(clipped_width, 2),
                        "clipped_height_px": round(clipped_height, 2),
                    },
                    "Editable text is clipped by a rendered ancestor.",
                )
            )

    overlap_nodes: list[dict[str, Any]] = []
    for item in text_nodes:
        block_id = str(item.get("block_id") or "")
        rect = _rect(item.get("visible_rect", item.get("rect")))
        words = _word_count(item.get("text"))
        if block_id and words >= 3 and rect["w"] > 1 and rect["h"] > 1:
            overlap_nodes.append({"block_id": block_id, "rect": rect, "words": words})
    for index, left in enumerate(overlap_nodes):
        for right in overlap_nodes[index + 1 :]:
            if left["block_id"] == right["block_id"]:
                continue
            overlap = _overlap_area(left["rect"], right["rect"])
            if overlap <= 24:
                continue
            smaller = max(
                1.0,
                min(
                    left["rect"]["w"] * left["rect"]["h"],
                    right["rect"]["w"] * right["rect"]["h"],
                ),
            )
            ratio = overlap / smaller
            if ratio < 0.08 and overlap < 160:
                continue
            findings.append(
                _finding(
                    "poster-dom-text-overlap",
                    left["block_id"],
                    "P0" if ratio >= 0.15 or (overlap >= 1200 and ratio >= 0.12) else "P1",
                    {
                        "left_block_id": left["block_id"],
                        "right_block_id": right["block_id"],
                        "left_rect": _rounded_rect(left["rect"]),
                        "right_rect": _rounded_rect(right["rect"]),
                        "overlap_area_px": round(overlap, 2),
                        "overlap_ratio_of_smaller": round(ratio, 4),
                    },
                    "Two editable text blocks overlap in the rendered Poster.",
                )
            )

    viewport_candidates: list[Mapping[str, Any]] = []
    for key in ("elements", "images", "tables"):
        viewport_candidates.extend(
            item for item in snapshot.get(key, []) if isinstance(item, Mapping)
        )
    for item in viewport_candidates:
        rect = _rect(item.get("rect"))
        gaps = {
            "left_gap_px": max(0.0, -rect["x"]),
            "top_gap_px": max(0.0, -rect["y"]),
            "right_gap_px": max(0.0, rect["right"] - evaluation_width),
            "bottom_gap_px": max(0.0, rect["bottom"] - evaluation_height),
        }
        if max(gaps.values()) <= VIEWPORT_TOLERANCE_PX:
            continue
        findings.append(
            _finding(
                "poster-dom-viewport-escape",
                str(item.get("block_id") or item.get("element_id") or item.get("tag") or "element"),
                "P0" if max(gaps.values()) >= 24 else "P1",
                {**{key: round(value, 2) for key, value in gaps.items()}, "rect": _rounded_rect(rect)},
                "Rendered Poster content escapes the fixed canvas viewport.",
            )
        )

    canvas_rect = root_rect if root_rect["w"] > 0 and root_rect["h"] > 0 else _rect(
        {"x": 0, "y": 0, "w": evaluation_width, "h": evaluation_height}
    )
    canvas_fill_metrics, canvas_content_boxes = _canvas_fill_metrics(
        snapshot, canvas_rect
    )
    canvas_blank = _content_blank_band(canvas_rect, canvas_content_boxes)
    canvas_fill_metrics["canvas_max_blank_run_ratio"] = canvas_blank[
        "max_blank_run_ratio"
    ]
    canvas_fill_metrics["canvas_blank_start_ratio"] = canvas_blank[
        "blank_start_ratio"
    ]
    canvas_fill_metrics["canvas_blank_end_ratio"] = canvas_blank["blank_end_ratio"]
    if canvas_blank["max_blank_run_ratio"] >= BLANK_BAND_MIN_RATIO:
        findings.append(
            _finding(
                "poster-dom-blank-band",
                str(root.get("block_id") or "paper-poster-root"),
                "P1",
                {
                    "scope": "canvas",
                    "canvas_rect": _rounded_rect(canvas_rect),
                    **canvas_blank,
                    "lower_blank_ratio": canvas_fill_metrics[
                        "canvas_lower_blank_ratio"
                    ],
                },
                "The Poster canvas contains a substantial rendered blank band.",
            )
        )
    fill_reasons = list(canvas_fill_metrics["canvas_fill_reasons"])
    near_bottom_only = (
        fill_reasons == ["content_stops_before_bottom"]
        and canvas_fill_metrics["canvas_content_bottom_ratio"]
        >= canvas_fill_metrics["min_content_bottom_ratio"] - 0.035
        and canvas_fill_metrics["canvas_lower_quarter_content_coverage"]
        >= canvas_fill_metrics["min_lower_quarter_content_coverage"] * 2.0
        and canvas_fill_metrics["canvas_lower_half_content_coverage"]
        >= canvas_fill_metrics["min_lower_half_content_coverage"] * 2.0
        and canvas_fill_metrics["canvas_middle_lower_content_coverage"]
        >= canvas_fill_metrics["min_middle_lower_content_coverage"] * 2.0
    )
    if fill_reasons and not near_bottom_only:
        findings.append(
            _finding(
                "poster-dom-sparse-oversized-panel",
                str(root.get("block_id") or "paper-poster-root"),
                "P1",
                {
                    "scope": "canvas",
                    "canvas_rect": _rounded_rect(canvas_rect),
                    "content_box_count": canvas_fill_metrics[
                        "canvas_content_box_count"
                    ],
                    "content_bottom_ratio": canvas_fill_metrics[
                        "canvas_content_bottom_ratio"
                    ],
                    "lower_quarter_content_coverage": canvas_fill_metrics[
                        "canvas_lower_quarter_content_coverage"
                    ],
                    "lower_half_content_coverage": canvas_fill_metrics[
                        "canvas_lower_half_content_coverage"
                    ],
                    "middle_lower_content_coverage": canvas_fill_metrics[
                        "canvas_middle_lower_content_coverage"
                    ],
                    "reasons": fill_reasons,
                },
                "The Poster canvas is underfilled across its columns or lower bands.",
            )
        )

    panels = [
        item for item in snapshot.get("panels", []) if isinstance(item, Mapping)
    ]
    canvas_area = max(1.0, canvas_width * canvas_height)
    for panel in panels:
        rect = _rect(panel.get("rect"))
        if rect["h"] < canvas_height * 0.12 or rect["w"] < canvas_width * 0.22:
            continue
        content_rects = [
            _rect(item)
            for item in panel.get("content_rects", [])
            if isinstance(item, Mapping)
        ]
        blank = _content_blank_band(rect, content_rects)
        area_ratio = _panel_content_area_ratio(rect, content_rects)
        word_count = int(_number(panel.get("word_count")))
        block_id = str(panel.get("block_id") or "panel")
        if blank["max_blank_run_ratio"] >= BLANK_BAND_MIN_RATIO:
            findings.append(
                _finding(
                    "poster-dom-blank-band",
                    block_id,
                    "P1",
                    {"panel_rect": _rounded_rect(rect), **blank},
                    "A large Poster panel contains a substantial internal blank band.",
                )
            )
        sparse = area_ratio < 0.14
        if sparse:
            findings.append(
                _finding(
                    "poster-dom-sparse-oversized-panel",
                    block_id,
                    "P1",
                    {
                        "panel_rect": _rounded_rect(rect),
                        "panel_area_ratio": round(rect["w"] * rect["h"] / canvas_area, 4),
                        "content_area_ratio": area_ratio,
                        "word_count": word_count,
                    },
                    "A large Poster panel occupies substantial area without enough rendered information.",
                )
            )

    for image in snapshot.get("images", []):
        if not isinstance(image, Mapping) or image.get("complete") is not True:
            continue
        rect = _rect(image.get("rect"))
        if rect["w"] <= 0 or rect["h"] <= 0:
            continue
        width_ratio = _number(image.get("naturalWidth")) / rect["w"]
        height_ratio = _number(image.get("naturalHeight")) / rect["h"]
        if min(width_ratio, height_ratio) >= IMAGE_EFFECTIVE_RESOLUTION_MIN:
            continue
        findings.append(
            _finding(
                "poster-dom-image-low-effective-resolution",
                str(image.get("block_id") or image.get("source_id") or "image"),
                "P1",
                {
                    "rendered_rect": _rounded_rect(rect),
                    "natural_width_px": int(_number(image.get("naturalWidth"))),
                    "natural_height_px": int(_number(image.get("naturalHeight"))),
                    "natural_to_rendered_width": round(width_ratio, 3),
                    "natural_to_rendered_height": round(height_ratio, 3),
                    "minimum_ratio": IMAGE_EFFECTIVE_RESOLUTION_MIN,
                },
                "A source image has insufficient effective pixel resolution at its rendered size.",
            )
        )

    for table in snapshot.get("tables", []):
        if not isinstance(table, Mapping):
            continue
        rect = _rect(table.get("rect"))
        client_width = _number(table.get("clientWidth")) or rect["w"]
        client_height = _number(table.get("clientHeight")) or rect["h"]
        width_gap = max(
            0.0,
            _number(table.get("scrollWidth")) - client_width,
        )
        height_gap = max(
            0.0,
            _number(table.get("scrollHeight")) - client_height,
        )
        container_rect = _rect(table.get("container_rect"))
        container_escape = 0.0
        container_tolerance = 0.0
        if container_rect["w"] > 0 and container_rect["h"] > 0:
            horizontal_escape = max(
                0.0,
                container_rect["x"] - rect["x"],
                rect["right"] - container_rect["right"],
            )
            vertical_escape = max(
                0.0,
                container_rect["y"] - rect["y"],
                rect["bottom"] - container_rect["bottom"],
            )
            horizontal_tolerance = max(8.0, container_rect["w"] * 0.03)
            vertical_tolerance = max(8.0, container_rect["h"] * 0.03)
            if horizontal_escape > horizontal_tolerance:
                container_escape = horizontal_escape
                container_tolerance = horizontal_tolerance
            if vertical_escape > vertical_tolerance and vertical_escape > container_escape:
                container_escape = vertical_escape
                container_tolerance = vertical_tolerance
        block_id = str(table.get("block_id") or "table")
        if (
            max(width_gap, height_gap) > TABLE_OVERFLOW_TOLERANCE_PX
            or container_escape > 0
        ):
            findings.append(
                _finding(
                    "poster-dom-table-overflow",
                    block_id,
                    "P1",
                    {
                        "table_rect": _rounded_rect(rect),
                        "container_rect": _rounded_rect(container_rect),
                        "width_gap_px": round(width_gap, 2),
                        "height_gap_px": round(height_gap, 2),
                        "container_escape_px": round(container_escape, 2),
                        "container_escape_tolerance_px": round(container_tolerance, 2),
                    },
                    "A native table has rendered scroll overflow.",
                )
            )
        font_px = _number(table.get("font_px"))
        if 0 < font_px < TABLE_MIN_FONT_PX:
            findings.append(
                _finding(
                    "poster-dom-table-text-small",
                    block_id,
                    "P1",
                    {
                        "table_rect": _rounded_rect(rect),
                        "font_px": round(font_px, 2),
                        "minimum_font_px": TABLE_MIN_FONT_PX,
                    },
                    "Native table text is below the Poster readability floor.",
                )
            )

    for item in snapshot.get("lists", []):
        if not isinstance(item, Mapping):
            continue
        if not (
            item.get("has_source_flow_ancestor") is True
            and item.get("is_direct_source_flow_child") is True
            and item.get("has_floated_source_sibling") is True
            and _number(item.get("item_count")) > 0
        ):
            continue
        padding = max(
            _number(item.get("paddingInlineStartPx")),
            _number(item.get("paddingLeftPx")),
        )
        text_indent = _number(item.get("textIndentPx"))
        if padding >= SOURCE_FLOW_MIN_GUTTER_PX and text_indent >= -1.0:
            continue
        findings.append(
            _finding(
                "poster-dom-source-flow-gutter",
                str(item.get("block_id") or item.get("element_id") or "source-flow-list"),
                "P1",
                {
                    "list_rect": _rounded_rect(item.get("rect")),
                    "padding_inline_start_px": round(padding, 2),
                    "minimum_padding_px": SOURCE_FLOW_MIN_GUTTER_PX,
                    "text_indent_px": round(text_indent, 2),
                },
                "A direct source-flow list does not reserve enough marker and text gutter.",
            )
        )

    for flow in snapshot.get("source_flows", []):
        if not isinstance(flow, Mapping):
            continue
        source_count = int(_number(flow.get("source_child_count")))
        readout_count = int(_number(flow.get("readout_child_count")))
        direct_sibling = flow.get("direct_sibling") is True
        evidence_intersects = flow.get("evidence_ids_intersect") is True
        if (
            source_count > 0
            and readout_count > 0
            and direct_sibling
            and evidence_intersects
        ):
            continue
        findings.append(
            _finding(
                "poster-dom-source-flow-sibling",
                str(flow.get("block_id") or "source-flow"),
                "P1",
                {
                    "flow_rect": _rounded_rect(flow.get("rect")),
                    "source_child_count": source_count,
                    "readout_child_count": readout_count,
                    "direct_sibling": direct_sibling,
                    "evidence_ids_intersect": evidence_intersects,
                },
                "Source evidence and its explanatory readout are not direct, evidence-bound siblings.",
            )
        )

    matches_allowed = matches_canvas if media == "screen" else (
        matches_canvas or matches_physical_print
    )
    if not matches_allowed:
        findings.append(
            _finding(
                "poster-dom-screen-print-mismatch",
                str(root.get("block_id") or "paper-poster-root"),
                "P0",
                {
                    "root_rect": _rounded_rect(root_rect),
                    "expected_canvas_width_px": round(canvas_width, 2),
                    "expected_canvas_height_px": round(canvas_height, 2),
                    "expected_print_width_css_px": round(print_width, 2),
                    "expected_print_height_css_px": round(print_height, 2),
                    "matches_canvas": matches_canvas,
                    "matches_physical_print": matches_physical_print,
                },
                "The rendered Poster canvas does not match the size allowed for this media.",
            )
        )

    text_units: list[Mapping[str, Any]] = []
    boxy_units: list[Mapping[str, Any]] = []
    micro_units: list[Mapping[str, Any]] = []
    size_bins: set[tuple[int, int]] = set()
    for element in snapshot.get("elements", []):
        if not isinstance(element, Mapping):
            continue
        tag = str(element.get("tag") or "").lower()
        haystack = " ".join(
            str(element.get(key) or "")
            for key in ("role", "class_name", "block_id")
        ).lower()
        words = int(_number(element.get("word_count"), _word_count(element.get("text"))))
        if words < 3 or tag in {"table", "th", "td", "h1", "h2"}:
            continue
        if any(token in haystack for token in ("panel", "slot", "header", "footer", "caption", "section-bar")):
            continue
        text_units.append(element)
        rect = _rect(element.get("rect"))
        area = rect["w"] * rect["h"]
        styled = (
            _number(element.get("border_width_px")) >= 0.8
            or element.get("background_distinct") is True
            or element.get("has_shadow") is True
        )
        if not styled or area <= 0 or area > canvas_area * 0.06:
            continue
        boxy_units.append(element)
        size_bins.add((int(round(rect["w"] / 24.0)), int(round(rect["h"] / 16.0))))
        if area <= canvas_area * 0.018:
            micro_units.append(element)
    boxy_ratio = len(boxy_units) / max(1, len(text_units))
    too_boxy = (len(boxy_units) >= 10 and boxy_ratio >= 0.35) or (
        len(micro_units) >= 8 and len(size_bins) <= max(6, len(boxy_units) // 2)
    )
    if too_boxy:
        sample = [
            {
                "block_id": str(item.get("block_id") or ""),
                "rect": _rounded_rect(item.get("rect")),
            }
            for item in boxy_units[:12]
        ]
        findings.append(
            _finding(
                "poster-dom-template-boxiness",
                str(boxy_units[0].get("block_id") or "poster"),
                "P1",
                {
                    "boxy_text_unit_count": len(boxy_units),
                    "text_unit_count": len(text_units),
                    "boxy_text_unit_ratio": round(boxy_ratio, 4),
                    "micro_boxy_text_unit_count": len(micro_units),
                    "size_bin_count": len(size_bins),
                    "sample": sample,
                },
                "Poster composition overuses repeated small bordered or filled text boxes.",
            )
        )

    for finding in findings:
        finding["geometry"] = {"media": media, **finding["geometry"]}
        finding["message"] = f"{media.capitalize()} media: {finding['message']}"

    findings.sort(
        key=lambda item: (
            _FINDING_ORDER[item["code"]],
            item["block_id"],
            repr(item["geometry"]),
        )
    )
    return {
        "format_version": REPORT_FORMAT_VERSION,
        "passed": not findings,
        "findings": findings,
        "metrics": {
            f"{media}_text_node_count": len(text_nodes),
            f"{media}_element_count": len(snapshot.get("elements", [])),
            f"{media}_image_count": len(snapshot.get("images", [])),
            f"{media}_table_count": len(snapshot.get("tables", [])),
            f"{media}_panel_count": len(panels),
            **{
                f"{media}_{key}": value
                for key, value in canvas_fill_metrics.items()
            },
        },
    }


def _evaluate_media_snapshots(
    screen_snapshot: Mapping[str, Any],
    print_snapshot: Mapping[str, Any],
    *,
    canvas: Mapping[str, Any],
    print_size: Mapping[str, Any],
) -> dict[str, Any]:
    evaluations = [
        evaluate_dom_snapshot(screen_snapshot, canvas=canvas, print_size=print_size),
        evaluate_dom_snapshot(print_snapshot, canvas=canvas, print_size=print_size),
    ]
    findings = [
        finding
        for evaluation in evaluations
        for finding in evaluation["findings"]
    ]
    media_order = {"screen": 0, "print": 1}
    findings.sort(
        key=lambda item: (
            _FINDING_ORDER[item["code"]],
            media_order.get(str(item["geometry"].get("media")), 2),
            item["block_id"],
            repr(item["geometry"]),
        )
    )
    metrics = {
        key: value
        for evaluation in evaluations
        for key, value in evaluation["metrics"].items()
    }
    return {
        "format_version": REPORT_FORMAT_VERSION,
        "passed": all(evaluation["passed"] for evaluation in evaluations),
        "findings": findings,
        "metrics": metrics,
    }


def _read_json_object(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise core.PathSafetyError(f"expected a regular JSON file: {path}")
    try:
        value = __import__("json").loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise core.IntegrityError(f"invalid JSON object: {path}") from error
    if not isinstance(value, dict):
        raise core.IntegrityError(f"JSON contract must be an object: {path}")
    return value


def _artifact_tree(artifact: Path) -> tuple[list[dict[str, Any]], str]:
    if artifact.is_symlink() or not artifact.is_dir():
        raise core.PathSafetyError(f"attempt artifact must be a regular directory: {artifact}")
    entries: list[dict[str, Any]] = []
    for path in sorted(artifact.rglob("*")):
        relative = path.relative_to(artifact).as_posix()
        if path.is_symlink():
            raise core.PathSafetyError(f"artifact contains a symlink: {relative}")
        if path.is_dir():
            continue
        if not path.is_file() or path.stat().st_nlink != 1:
            raise core.PathSafetyError(f"artifact file must not be hardlinked: {relative}")
        entries.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": core.sha256_file(path),
            }
        )
    if not entries:
        raise core.IntegrityError("attempt artifact directory is empty")
    return entries, core.sha256_bytes(core._stored_json_bytes(entries))


def _require_output_directory(path: Path, attempt: Path) -> None:
    try:
        relative = path.relative_to(attempt)
    except ValueError as error:
        raise core.PathSafetyError("DOM audit output escapes the attempt") from error
    cursor = attempt
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise core.PathSafetyError(f"DOM audit output contains a symlink: {relative}")
        if cursor.exists() and not cursor.is_dir():
            raise core.PathSafetyError(f"DOM audit output parent is not a directory: {relative}")
    path.mkdir(parents=True, exist_ok=True)


def _require_existing_output_directory(path: Path, attempt: Path) -> None:
    try:
        relative = path.relative_to(attempt)
    except ValueError as error:
        raise core.PathSafetyError("DOM audit output escapes the attempt") from error
    cursor = attempt
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink() or not cursor.is_dir():
            raise core.PathSafetyError(
                f"DOM audit output directory is missing or unsafe: {relative}"
            )


def _require_safe_output_file(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as error:
        raise core.PathSafetyError("DOM audit output escapes its QA directory") from error
    if path.is_symlink():
        raise core.PathSafetyError(f"DOM audit output must not be a symlink: {path.name}")
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise core.PathSafetyError(f"DOM audit output must not be hardlinked: {path.name}")


def _verified_attempt(run_dir: Path | str, attempt_id: str) -> dict[str, Any]:
    run = Path(run_dir).absolute()
    if core.inspect_run_format(run) != core.AGENT_FIRST_RUN_FORMAT_VERSION:
        raise core.ContractError("Poster DOM audit requires an Agent-first v2 run")
    attempt = core.safe_path(run / "attempts", attempt_id, must_exist=True)
    if attempt.is_symlink() or not attempt.is_dir():
        raise core.PathSafetyError("Poster DOM audit attempt must be a regular directory")
    context_path = core.safe_path(attempt, "attempt-context.json", must_exist=True)
    context = _read_json_object(context_path)
    if (
        context.get("run_format_version") != core.AGENT_FIRST_RUN_FORMAT_VERSION
        or context.get("attempt_id") != attempt_id
    ):
        raise core.IntegrityError("Poster DOM audit attempt context is invalid")
    plan = core.load_attempt_plan(run, attempt_id)
    catalog = core.load_attempt_visual_catalog(run, attempt_id)
    poster = core.safe_path(attempt, "artifact/poster.html", must_exist=True)
    if poster.is_symlink() or not poster.is_file() or poster.stat().st_nlink != 1:
        raise core.PathSafetyError("Poster DOM audit requires a regular artifact/poster.html")
    artifact = core.safe_path(attempt, "artifact", must_exist=True)
    return {
        "run": run,
        "attempt": attempt,
        "artifact": artifact,
        "poster": poster,
        "context": context,
        "context_path": context_path,
        "plan": plan,
        "catalog": catalog,
    }


def _invoke_browser_worker(
    *,
    poster: Path,
    artifact: Path,
    canvas: Mapping[str, Any],
    cache_root: Path | None,
    allow_browser_install: bool,
) -> dict[str, Any]:
    width = int(_number(canvas.get("width_px")))
    height = int(_number(canvas.get("height_px")))
    if width <= 0 or height <= 0:
        raise core.ContractError("Poster DOM audit canvas is invalid")
    runtime = setup_browser.ensure_browser_runtime(
        cache_root=cache_root,
        allow_install=allow_browser_install,
    )
    with tempfile.TemporaryDirectory(prefix="autodesign-poster-dom-") as temporary:
        output = Path(temporary)
        report_path = output / "probe.json"
        screen_path = output / "screen.png"
        print_path = output / "print.png"
        command = [
            str(runtime.python_executable),
            "-I",
            "-B",
            str(Path(__file__).resolve()),
            "__browser-probe",
            "--poster",
            str(poster),
            "--artifact-root",
            str(artifact),
            "--output-dir",
            str(output),
            "--width",
            str(width),
            "--height",
            str(height),
        ]
        environment = setup_browser.isolated_environment(
            browsers_path=runtime.browsers_path,
            allow_network_configuration=False,
        )
        try:
            result = setup_browser._default_command_runner(  # noqa: SLF001
                command,
                env=environment,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise setup_browser.BrowserRuntimeError(
                "Poster DOM browser probe did not complete"
            ) from error
        if result.returncode != 0:
            raise setup_browser.BrowserRuntimeError(
                f"Poster DOM browser probe failed (exit {result.returncode})"
            )
        payload = _read_json_object(report_path)
        screen = screen_path.read_bytes() if screen_path.is_file() else b""
        printed = print_path.read_bytes() if print_path.is_file() else b""
        if not screen or not printed:
            raise core.IntegrityError("Poster DOM browser probe screenshots are missing")
        return {
            "screen_snapshot": payload.get("screen_snapshot"),
            "print_snapshot": payload.get("print_snapshot"),
            "screenshots": {"screen": screen, "print": printed},
            "diagnostics": payload.get("diagnostics"),
        }


def _dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for record in records:
        key = json.dumps(record, sort_keys=True, ensure_ascii=True)
        if key not in seen:
            seen.add(key)
            result.append(record)
    return result


def _capture_media_snapshot(page: Any, media: str) -> tuple[Mapping[str, Any], bytes]:
    page.emulate_media(media=media)
    page.evaluate(_FONT_READY_SCRIPT)
    snapshot = page.evaluate(browser_probe_script(), media)
    screenshot = page.screenshot(full_page=True)
    return snapshot, screenshot


def _run_browser_probe(
    *,
    poster: Path,
    artifact_root: Path,
    output_dir: Path,
    width: int,
    height: int,
) -> None:
    """Measure one Poster in the isolated pinned runtime without writing its DOM."""

    artifact = artifact_root.resolve(strict=True)
    html = poster.resolve(strict=True)
    if (
        artifact_root.is_symlink()
        or not artifact.is_dir()
        or poster.is_symlink()
        or not html.is_file()
        or html.stat().st_nlink != 1
    ):
        raise core.PathSafetyError("Poster DOM probe requires regular artifact inputs")
    try:
        html.relative_to(artifact)
    except ValueError as error:
        raise core.PathSafetyError("Poster DOM probe HTML escapes the artifact") from error
    output = output_dir.resolve(strict=True)
    if output_dir.is_symlink() or not output.is_dir():
        raise core.PathSafetyError("Poster DOM probe output must be a regular directory")
    if width <= 0 or height <= 0:
        raise core.ContractError("Poster DOM probe viewport is invalid")

    import browser_worker  # noqa: PLC0415

    blocked_requests: list[dict[str, Any]] = []
    blocked_popups: list[dict[str, Any]] = []
    blocked_workers: list[dict[str, Any]] = []
    console_errors: list[dict[str, Any]] = []
    request_errors: list[dict[str, Any]] = []
    page_errors: list[dict[str, Any]] = []

    def route_request(route: Any) -> None:
        decision = browser_worker.classify_request(route.request.url, artifact)
        if decision.allowed:
            route.continue_()
            return
        resource_type = str(route.request.resource_type)
        blocked_requests.append(
            {
                "url": decision.sanitized_url,
                "reason": decision.reason,
                "resource_type": resource_type,
            }
        )
        if resource_type in {"worker", "serviceworker"}:
            blocked_workers.append(
                {
                    "url": decision.sanitized_url,
                    "reason": "worker_request_blocked",
                }
            )
        route.abort("blockedbyclient")

    def block_websocket(socket: Any) -> None:
        blocked_requests.append(
            {
                "url": browser_worker._sanitize_url(socket.url, artifact),  # noqa: SLF001
                "reason": "websocket_blocked",
                "resource_type": "websocket",
            }
        )

    def console_message(message: Any) -> None:
        if str(message.type).lower() == "error":
            console_errors.append(
                {
                    "type": "error",
                    "text": browser_worker._sanitize_diagnostic_text(  # noqa: SLF001
                        str(message.text), artifact
                    ),
                }
            )

    def page_error(error: Any) -> None:
        value = getattr(error, "error", error)
        page_errors.append(
            {
                "message": browser_worker._sanitize_diagnostic_text(  # noqa: SLF001
                    str(value), artifact
                )
            }
        )

    def request_failed(request: Any) -> None:
        failure = request.failure
        request_errors.append(
            {
                "url": browser_worker._sanitize_url(request.url, artifact),  # noqa: SLF001
                "error": browser_worker._sanitize_diagnostic_text(  # noqa: SLF001
                    failure if isinstance(failure, str) else str(failure or "request failed"),
                    artifact,
                ),
            }
        )

    def popup_opened(popup: Any) -> None:
        blocked_popups.append(
            {
                "url": browser_worker._sanitize_url(str(popup.url), artifact),  # noqa: SLF001
                "reason": "popup_blocked",
            }
        )
        try:
            popup.close()
        except Exception:
            pass

    def worker_opened(worker: Any) -> None:
        blocked_workers.append(
            {
                "url": browser_worker._sanitize_url(str(worker.url), artifact),  # noqa: SLF001
                "reason": "worker_blocked",
            }
        )
        try:
            worker.evaluate("self.close()")
        except Exception:
            pass

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise setup_browser.BrowserRuntimeError(
            "Pinned Playwright runtime is unavailable"
        ) from error

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=list(browser_worker._BROWSER_NETWORK_REDUCTION_ARGS),  # noqa: SLF001
        )
        try:
            context = browser.new_context(
                viewport={"width": width, "height": height},
                service_workers="block",
                reduced_motion="reduce",
                offline=True,
            )
            try:
                context.route("**/*", route_request)
                context.route_web_socket("**/*", block_websocket)
                context.on("console", console_message)
                context.on("weberror", page_error)
                context.on("requestfailed", request_failed)
                page = context.new_page()
                page.on("popup", popup_opened)
                page.on("worker", worker_opened)
                try:
                    page.goto(html.as_uri(), wait_until="load", timeout=30000)
                    page.wait_for_timeout(350)
                except Exception as error:
                    page_errors.append(
                        {
                            "message": browser_worker._sanitize_diagnostic_text(  # noqa: SLF001
                                str(error), artifact
                            )
                        }
                    )
                screen_snapshot, screen_bytes = _capture_media_snapshot(page, "screen")
                print_snapshot, print_bytes = _capture_media_snapshot(page, "print")
            finally:
                context.close()
        finally:
            browser.close()

    diagnostics = {
        "blocked_requests": _dedupe_records(blocked_requests),
        "blocked_popups": _dedupe_records(blocked_popups),
        "blocked_workers": _dedupe_records(blocked_workers),
        "console_errors": _dedupe_records(console_errors),
        "request_errors": _dedupe_records(request_errors),
        "page_errors": _dedupe_records(page_errors),
    }
    core.atomic_write_bytes(output / "screen.png", screen_bytes)
    core.atomic_write_bytes(output / "print.png", print_bytes)
    core.atomic_write_json(
        output / "probe.json",
        {
            "screen_snapshot": screen_snapshot,
            "print_snapshot": print_snapshot,
            "diagnostics": diagnostics,
        },
    )


def _diagnostics_clean(diagnostics: Mapping[str, Any]) -> bool:
    return not any(
        diagnostics.get(key)
        for key in (
            "blocked_requests",
            "blocked_popups",
            "blocked_workers",
            "console_errors",
            "request_errors",
            "page_errors",
        )
    )


def run_poster_dom_audit(
    run_dir: Path | str,
    attempt_id: str,
    *,
    cache_root: Path | None = None,
    allow_browser_install: bool = True,
) -> dict[str, Any]:
    """Run the isolated read-only probe and atomically persist only QA outputs."""

    verified = _verified_attempt(run_dir, attempt_id)
    artifact_entries_before, artifact_hash_before = _artifact_tree(verified["artifact"])
    qa = verified["attempt"] / "qa"
    previews = qa / "previews"
    _require_output_directory(qa, verified["attempt"])
    _require_output_directory(previews, verified["attempt"])
    report_path = qa / "dom-audit.json"
    screen_path = previews / "dom-screen.png"
    print_path = previews / "dom-print.png"
    for output in (report_path, screen_path, print_path):
        _require_safe_output_file(output, qa)

    payload = _invoke_browser_worker(
        poster=verified["poster"],
        artifact=verified["artifact"],
        canvas=verified["plan"]["canvas"],
        cache_root=cache_root,
        allow_browser_install=allow_browser_install,
    )
    screen_snapshot = payload.get("screen_snapshot")
    print_snapshot = payload.get("print_snapshot")
    screenshots = payload.get("screenshots")
    diagnostics = payload.get("diagnostics")
    if (
        not isinstance(screen_snapshot, Mapping)
        or not isinstance(print_snapshot, Mapping)
        or not isinstance(screenshots, Mapping)
        or not isinstance(diagnostics, Mapping)
        or not isinstance(screenshots.get("screen"), bytes)
        or not isinstance(screenshots.get("print"), bytes)
    ):
        raise core.IntegrityError("Poster DOM browser worker returned an invalid payload")

    artifact_entries_after, artifact_hash_after = _artifact_tree(verified["artifact"])
    if artifact_entries_after != artifact_entries_before or artifact_hash_after != artifact_hash_before:
        raise core.IntegrityError("Poster artifact bytes changed during the read-only DOM audit")

    evaluated = _evaluate_media_snapshots(
        screen_snapshot,
        print_snapshot,
        canvas=verified["plan"]["canvas"],
        print_size=verified["plan"]["print"],
    )
    screen_bytes = screenshots["screen"]
    print_bytes = screenshots["print"]
    report = {
        "format_version": REPORT_FORMAT_VERSION,
        "run_format_version": core.AGENT_FIRST_RUN_FORMAT_VERSION,
        "attempt_id": attempt_id,
        "attempt_context_sha256": core.sha256_file(verified["context_path"]),
        "plan_sha256": str(verified["context"]["plan_sha256"]),
        "catalog_sha256": str(verified["context"]["catalog_sha256"]),
        "artifact_tree": artifact_entries_before,
        "artifact_tree_sha256_before": artifact_hash_before,
        "artifact_tree_sha256_after": artifact_hash_after,
        "artifact_unchanged": True,
        "findings": evaluated["findings"],
        "metrics": evaluated["metrics"],
        "browser_diagnostics": dict(diagnostics),
        "screenshots": {
            "screen": {
                "path": "qa/previews/dom-screen.png",
                "sha256": core.sha256_bytes(screen_bytes),
            },
            "print": {
                "path": "qa/previews/dom-print.png",
                "sha256": core.sha256_bytes(print_bytes),
            },
        },
        "passed": evaluated["passed"] is True and _diagnostics_clean(diagnostics),
    }
    core.atomic_write_bytes(screen_path, screen_bytes)
    core.atomic_write_bytes(print_path, print_bytes)
    core.atomic_write_json(report_path, report)
    return report


def load_verified_poster_dom_audit(
    run_dir: Path | str, attempt_id: str
) -> dict[str, Any]:
    """Load a persisted report only when its exact artifact and screenshots still match."""

    verified = _verified_attempt(run_dir, attempt_id)
    qa = verified["attempt"] / "qa"
    previews = qa / "previews"
    _require_existing_output_directory(qa, verified["attempt"])
    _require_existing_output_directory(previews, verified["attempt"])
    report_path = qa / "dom-audit.json"
    _require_safe_output_file(report_path, qa)
    report = _read_json_object(report_path)
    if (
        report.get("format_version") != REPORT_FORMAT_VERSION
        or report.get("run_format_version") != core.AGENT_FIRST_RUN_FORMAT_VERSION
        or report.get("attempt_id") != attempt_id
        or report.get("artifact_unchanged") is not True
        or report.get("attempt_context_sha256") != core.sha256_file(verified["context_path"])
        or report.get("plan_sha256") != verified["context"].get("plan_sha256")
        or report.get("catalog_sha256") != verified["context"].get("catalog_sha256")
    ):
        raise core.IntegrityError("persisted Poster DOM audit binding is invalid")
    entries, current_hash = _artifact_tree(verified["artifact"])
    if (
        report.get("artifact_tree") != entries
        or report.get("artifact_tree_sha256_before") != current_hash
        or report.get("artifact_tree_sha256_after") != current_hash
    ):
        raise core.IntegrityError("persisted Poster DOM audit does not match the artifact tree")
    screenshots = report.get("screenshots")
    if not isinstance(screenshots, Mapping):
        raise core.IntegrityError("persisted Poster DOM audit screenshots are invalid")
    for label in ("screen", "print"):
        item = screenshots.get(label)
        if not isinstance(item, Mapping):
            raise core.IntegrityError("persisted Poster DOM audit screenshot binding is invalid")
        expected_path = f"qa/previews/dom-{label}.png"
        if item.get("path") != expected_path:
            raise core.IntegrityError("persisted Poster DOM audit screenshot path is invalid")
        path = verified["attempt"] / expected_path
        _require_safe_output_file(path, qa)
        if not path.is_file() or core.sha256_file(path) != item.get("sha256"):
            raise core.IntegrityError("persisted Poster DOM audit screenshot hash mismatch")
    return report


def _browser_probe_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--poster", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    args = parser.parse_args(argv)
    _run_browser_probe(
        poster=args.poster,
        artifact_root=args.artifact_root,
        output_dir=args.output_dir,
        width=args.width,
        height=args.height,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["__browser-probe"]:
        return _browser_probe_cli(arguments[1:])
    raise core.ContractError("poster_dom_audit.py is an internal Poster Skill module")


if __name__ == "__main__":
    raise SystemExit(main())
