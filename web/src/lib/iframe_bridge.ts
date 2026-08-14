/**
 * Pure helpers for reading layer state out of the iframe that renders
 * the agent's poster.html / index.html. The iframe is same-origin
 * (Vite proxies `/api/files/runs/*` so the parent and child share an
 * origin), and `sandbox="allow-same-origin"` lets the parent reach
 * `iframe.contentDocument` directly — no postMessage needed.
 *
 * The rendered HTML (composite.py emits this) tags every editable
 * layer with `<div class="layer text" data-layer-id="..." data-bbox-x
 * data-bbox-y data-bbox-w data-bbox-h data-font-size-px data-font-weight
 * data-line-height data-fill data-align contenteditable="true">`. This module only reads from
 * those attributes — patches go through the existing Zustand
 * `updateLayer` action so the same wire format used by the right-rail
 * Sidebar Apply path is used for in-place edits.
 */

import type { Align, Bbox } from "./types";

/** Subset of `Layer` properties the floating toolbar can change. Keep
 *  small + flat; matches `_ALLOWED_DIFF_FIELDS` on the backend agent
 *  edit_layer tool. */
export interface ToolbarLayerState {
  layer_id: string;
  text: string;
  font_family?: string;
  font_size_px?: number;
  font_weight?: number;
  font_style?: "normal" | "italic";
  line_height?: number;
  letter_spacing?: number;
  text_transform?: "none" | "uppercase";
  fill?: string;
  align?: Align;
  kind?: string;
  slot_id?: string;
  panel_role?: string;
  layout_archetype?: string;
}

/** Pixel rect (in the parent React app's viewport coordinates) where
 *  a given layer is currently displayed. Used to position the
 *  FloatingToolbar absolutely over the iframe. */
export interface LayerRect {
  top: number;
  left: number;
  width: number;
  height: number;
}

const NUM = (s: string | null) => {
  const n = s ? Number(s) : NaN;
  return Number.isFinite(n) ? n : undefined;
};

const pxValue = (raw: string | null): number | undefined => {
  if (!raw) return undefined;
  const clean = raw.trim().endsWith("px") ? raw.trim().slice(0, -2) : raw.trim();
  return NUM(clean);
};

const FLOW_TEXT_TAGS = new Set([
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "p",
  "li",
  "figcaption",
  "blockquote",
  "td",
  "th",
]);

const FLOW_TEXT_SELECTOR = [
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "p",
  "li",
  "figcaption",
  "blockquote",
  "td",
  "th",
  ".identity-badge",
  ".comparison-item",
  ".formula",
  ".footer-note",
  ".lead",
  ".mechanism-side-callout",
  ".metric",
  ".muted",
  ".native-row",
  ".readout",
  ".stage",
].join(", ");
const SCOPED_FLOW_TEXT_SELECTOR = FLOW_TEXT_SELECTOR
  .split(", ")
  .map((selector) => `.paper-poster ${selector}`)
  .join(", ");

const FLOW_SECTION_SELECTOR = [
  ".paper-poster .poster-header[data-block-id]",
  ".paper-poster .poster-section[data-block-id]",
].join(", ");
const AUTHORED_HTML_LAYER_SELECTOR =
  '[data-autodesign-editable="true"][data-layer-id]';

const isPaperPosterFlowText = (el: Element): boolean => {
  if (!el.closest(".paper-poster")) return false;
  if (!el.getAttribute("data-block-id") && !el.getAttribute("data-layer-id")) return false;
  return FLOW_TEXT_TAGS.has(el.tagName.toLowerCase()) || isPaperPosterTextUnit(el);
};

const isPaperPosterTextUnit = (el: Element): boolean =>
  el.classList.contains("identity-badge")
  || el.classList.contains("comparison-item")
  || el.classList.contains("formula")
  || el.classList.contains("footer-note")
  || el.classList.contains("lead")
  || el.classList.contains("mechanism-side-callout")
  || el.classList.contains("metric")
  || el.classList.contains("muted")
  || el.classList.contains("native-row")
  || el.classList.contains("readout")
  || el.classList.contains("stage");

const inlineStyleMap = (el: HTMLElement): Record<string, string> => {
  const out: Record<string, string> = {};
  for (const part of el.getAttribute("style")?.split(";") ?? []) {
    const [key, ...rest] = part.split(":");
    if (!key || !rest.length) continue;
    out[key.trim().toLowerCase()] = rest.join(":").trim();
  }
  return out;
};

export function readLayerKind(el: Element): string {
  const attr = el.getAttribute("data-kind");
  if (attr) return attr;
  if (el.tagName.toLowerCase() === "img") return "image";
  if (isPaperPosterFlowText(el)) return "text";
  if (el.classList.contains("poster-section") || el.classList.contains("poster-header")) {
    return "section";
  }
  if (el.classList.contains("text")) return "text";
  if (el.classList.contains("image")) return "image";
  return "layer";
}

/** Read the layer's current state from its `data-*` attributes.
 *  Returns null if the element doesn't carry a layer-id (defensive
 *  guard against stray clicks on the canvas chrome). */
export function readLayerState(el: Element): ToolbarLayerState | null {
  const layer_id = el.getAttribute("data-layer-id") ?? el.getAttribute("data-block-id");
  if (!layer_id) return null;
  const align = el.getAttribute("data-align") ?? styleValue(el, "text-align");
  const a: Align | undefined =
    align === "left" || align === "center" || align === "right" ? align : undefined;
  const fontSize =
    NUM(el.getAttribute("data-font-size-px")) ??
    NUM(styleValue(el, "font-size")?.replace("px", "") ?? null);
  const fontStyle = el.getAttribute("data-font-style") ?? styleValue(el, "font-style");
  const textTransform =
    el.getAttribute("data-text-transform") ?? styleValue(el, "text-transform");
  const lineHeight = normalizedLineHeight(styleValue(el, "line-height"), fontSize);
  return {
    layer_id,
    text: el.textContent ?? "",
    font_family: normalizedFontFamily(styleValue(el, "font-family")),
    font_size_px: fontSize,
    font_weight:
      NUM(el.getAttribute("data-font-weight")) ??
      NUM(styleValue(el, "font-weight")),
    font_style: fontStyle === "italic" ? "italic" : fontStyle === "normal" ? "normal" : undefined,
    line_height:
      NUM(el.getAttribute("data-line-height")) ??
      lineHeight,
    letter_spacing:
      NUM(el.getAttribute("data-letter-spacing")) ??
      pxValue(styleValue(el, "letter-spacing")),
    text_transform:
      textTransform === "uppercase" ? "uppercase" : textTransform === "none" ? "none" : undefined,
    fill:
      el.getAttribute("data-fill") ??
      normalizedCssColor(styleValue(el, "color")) ??
      undefined,
    align: a,
    kind: readLayerKind(el),
    slot_id: el.getAttribute("data-slot-id") ?? undefined,
    panel_role: el.getAttribute("data-panel-role") ?? undefined,
    layout_archetype: el.getAttribute("data-layout-archetype") ?? undefined,
  };
}

export function readLayerBBox(el: HTMLElement): Bbox | null {
  if (isAuthoredHtmlFlowElement(el)) {
    const domBox = readAuthoredHtmlDomBBox(el);
    if (domBox) return domBox;
  }
  const styles = inlineStyleMap(el);
  const x =
    NUM(el.getAttribute("data-bbox-x")) ??
    NUM(el.getAttribute("data-bbox-tx")) ??
    pxValue(styles.left) ??
    0;
  const y =
    NUM(el.getAttribute("data-bbox-y")) ??
    NUM(el.getAttribute("data-bbox-ty")) ??
    pxValue(styles.top) ??
    0;
  const w = NUM(el.getAttribute("data-bbox-w")) ?? pxValue(styles.width);
  const h = NUM(el.getAttribute("data-bbox-h")) ?? pxValue(styles.height);
  if (!(w && h)) return null;
  return { x: Math.round(x), y: Math.round(y), w: Math.round(w), h: Math.round(h) };
}

function isAuthoredHtmlFlowElement(el: HTMLElement): boolean {
  return !!el.closest(".paper-poster, [data-autodesign-artifact-root]")
    && !el.classList.contains("layer")
    && !el.classList.contains("od-layer");
}

function readAuthoredHtmlDomBBox(el: HTMLElement): Bbox | null {
  const root = el.closest<HTMLElement>(".deck-slide")
    ?? el.closest<HTMLElement>(".paper-poster, [data-autodesign-artifact-root]");
  if (!root) return null;
  const rect = el.getBoundingClientRect();
  const rootRect = root.getBoundingClientRect();
  if (!(rect.width > 0 && rect.height > 0)) return null;
  const scaleX = root.offsetWidth ? rootRect.width / root.offsetWidth : 1;
  const scaleY = root.offsetHeight ? rootRect.height / root.offsetHeight : 1;
  const safeScaleX = Number.isFinite(scaleX) && scaleX > 0 ? scaleX : 1;
  const safeScaleY = Number.isFinite(scaleY) && scaleY > 0 ? scaleY : 1;
  const styles = inlineStyleMap(el);
  const dx = pxValue(styles.left) ?? 0;
  const dy = pxValue(styles.top) ?? 0;
  const x = Math.round((rect.left - rootRect.left) / safeScaleX);
  const y = Math.round((rect.top - rootRect.top) / safeScaleY);
  el.setAttribute("data-flow-origin-x", String(Math.round(x - dx)));
  el.setAttribute("data-flow-origin-y", String(Math.round(y - dy)));
  return {
    x,
    y,
    w: Math.round(rect.width / safeScaleX),
    h: Math.round(rect.height / safeScaleY),
  };
}

export function flowOffsetForBBox(el: HTMLElement, bbox: Bbox): { dx: number; dy: number } | null {
  if (!isAuthoredHtmlFlowElement(el)) return null;
  const originX = NUM(el.getAttribute("data-flow-origin-x"));
  const originY = NUM(el.getAttribute("data-flow-origin-y"));
  if (originX === undefined || originY === undefined) return null;
  return {
    dx: Math.round(bbox.x - originX),
    dy: Math.round(bbox.y - originY),
  };
}

export function writeLayerBBox(el: HTMLElement, bbox: Bbox): void {
  const x = Math.round(bbox.x);
  const y = Math.round(bbox.y);
  const w = Math.max(1, Math.round(bbox.w));
  const h = Math.max(1, Math.round(bbox.h));
  el.setAttribute("data-bbox-x", String(x));
  el.setAttribute("data-bbox-y", String(y));
  el.setAttribute("data-bbox-w", String(w));
  el.setAttribute("data-bbox-h", String(h));
  const flowOffset = flowOffsetForBBox(el, bbox);
  if (flowOffset) {
    el.setAttribute("data-flow-offset-x", String(flowOffset.dx));
    el.setAttribute("data-flow-offset-y", String(flowOffset.dy));
    el.style.position = "relative";
    el.style.left = `${flowOffset.dx}px`;
    el.style.top = `${flowOffset.dy}px`;
    el.style.width = `${w}px`;
    el.style.height = `${h}px`;
    return;
  }
  if (el.hasAttribute("data-bbox-tx") || el.hasAttribute("data-bbox-ty")) {
    el.setAttribute("data-bbox-tx", String(x));
    el.setAttribute("data-bbox-ty", String(y));
  }
  el.style.left = `${x}px`;
  el.style.top = `${y}px`;
  el.style.width = `${w}px`;
  el.style.height = `${h}px`;
}

/** Compute where a layer is on screen *from the parent React app's
 *  perspective*, accounting for the iframe's own offset. The toolbar
 *  is mounted in the parent DOM (so it floats above the iframe and
 *  isn't clipped by the iframe's overflow), so it needs parent-frame
 *  coordinates. */
export function layerRectInParent(
  layer: Element,
  iframe: HTMLIFrameElement,
): LayerRect {
  const layerRect = layer.getBoundingClientRect();
  const iframeRect = iframe.getBoundingClientRect();
  const scaleX = iframe.offsetWidth ? iframeRect.width / iframe.offsetWidth : 1;
  const scaleY = iframe.offsetHeight ? iframeRect.height / iframe.offsetHeight : 1;
  return {
    top: iframeRect.top + layerRect.top * scaleY,
    left: iframeRect.left + layerRect.left * scaleX,
    width: layerRect.width * scaleX,
    height: layerRect.height * scaleY,
  };
}

/** All editable text layers in the iframe's document, in DOM order.
 *  composite.py already sets contenteditable="true" — we just listen. */
export function walkEditableLayers(doc: Document): HTMLElement[] {
  ensurePaperPosterEditableIds(doc);
  const els = doc.querySelectorAll<HTMLElement>(
    `.layer[contenteditable="true"], .layer.text[data-layer-id], .od-layer.od-editable[data-layer-id], .paper-poster [data-layer-id], ${AUTHORED_HTML_LAYER_SELECTOR}`,
  );
  const out = Array.from(new Set(Array.from(els))).filter((el) =>
    el.classList.contains("layer")
    || el.classList.contains("od-layer")
    || isPaperPosterFlowText(el)
    || el.matches(AUTHORED_HTML_LAYER_SELECTOR)
  );
  for (const el of out) {
    if (!el.getAttribute("data-layer-id")) {
      const blockId = el.getAttribute("data-block-id");
      if (blockId) el.setAttribute("data-layer-id", blockId);
    }
    if (!el.getAttribute("data-kind") && isPaperPosterFlowText(el)) {
      el.setAttribute("data-kind", "text");
    }
    if (!el.isContentEditable) {
      el.setAttribute("contenteditable", "true");
      el.setAttribute("spellcheck", "false");
    }
  }
  return out;
}

export function walkInteractiveLayers(doc: Document): HTMLElement[] {
  ensurePaperPosterEditableIds(doc);
  const els = doc.querySelectorAll<HTMLElement>(
    `.layer[data-layer-id], .od-layer[data-layer-id], .paper-poster [data-layer-id], ${AUTHORED_HTML_LAYER_SELECTOR}`,
  );
  const out = Array.from(new Set(Array.from(els)));
  for (const el of out) {
    if (readLayerKind(el) === "image") {
      el.style.cursor = "move";
      el.style.pointerEvents = "auto";
      el.draggable = false;
      el.querySelectorAll("img").forEach((img) => {
        img.draggable = false;
      });
    }
  }
  return out;
}

export function editableLayerForPointerTarget(target: unknown): HTMLElement | null {
  const node = target as {
    closest?: (selector: string) => HTMLElement | null;
    parentElement?: {
      closest?: (selector: string) => HTMLElement | null;
    } | null;
  } | null;
  if (typeof node?.closest === "function") {
    return node.closest("[data-layer-id]");
  }
  if (typeof node?.parentElement?.closest === "function") {
    return node.parentElement.closest("[data-layer-id]");
  }
  return null;
}

export function paperPosterTextLayerId(
  el: { getAttribute: (name: string) => string | null },
  index: number,
  nearestId?: string | null,
): string {
  return el.getAttribute("data-layer-id")
    || el.getAttribute("data-block-id")
    || `flow_text_${slugId(nearestId || `text_${index}`)}_${index}`;
}

function ensurePaperPosterEditableIds(doc: Document): void {
  const root = doc.querySelector(".paper-poster");
  if (!root) return;

  doc.querySelectorAll<HTMLElement>(FLOW_SECTION_SELECTOR).forEach((el, idx) => {
    if (!el.getAttribute("data-layer-id")) {
      const blockId = el.getAttribute("data-block-id") || `section_${idx + 1}`;
      el.setAttribute("data-layer-id", `flow_section_${slugId(blockId)}_${idx + 1}`);
    }
    if (!el.getAttribute("data-kind")) el.setAttribute("data-kind", "section");
  });

  let textIdx = 0;
  doc.querySelectorAll<HTMLElement>(SCOPED_FLOW_TEXT_SELECTOR).forEach((el) => {
    const text = (el.textContent ?? "").replace(/\s+/g, " ").trim();
    if (!text) return;
    textIdx += 1;
    if (!el.getAttribute("data-layer-id")) {
      el.setAttribute(
        "data-layer-id",
        paperPosterTextLayerId(el, textIdx, nearestBlockId(el)),
      );
    }
    if (!el.getAttribute("data-kind")) el.setAttribute("data-kind", "text");
  });

  let imageIdx = 0;
  doc.querySelectorAll<HTMLElement>(".paper-poster img").forEach((el) => {
    imageIdx += 1;
    if (!el.getAttribute("data-layer-id")) {
      const blockId =
        el.getAttribute("data-source-id")
        || el.getAttribute("alt")
        || nearestBlockId(el)
        || `image_${imageIdx}`;
      el.setAttribute("data-layer-id", `flow_image_${slugId(blockId)}_${imageIdx}`);
    }
    if (!el.getAttribute("data-kind")) el.setAttribute("data-kind", "image");
    el.draggable = false;
  });
}

function nearestBlockId(el: Element): string | null {
  let node: Element | null = el;
  while (node) {
    const blockId = node.getAttribute("data-block-id") || node.getAttribute("data-column-id");
    if (blockId) return blockId;
    node = node.parentElement;
  }
  return null;
}

function slugId(raw: string): string {
  const slug = raw
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 72);
  return slug || "item";
}

/** Apply a toolbar-driven change to the iframe DOM optimistically, so
 *  the user sees the visual change *immediately* — before the
 *  apply_edits round-trip lands. Mirrors the data-* contract that
 *  composite.py emits. */
export function applyOptimisticPatch(
  el: HTMLElement,
  patch: {
    font_size_px?: number;
    font_weight?: number;
    font_style?: "normal" | "italic";
    line_height?: number;
    letter_spacing?: number;
    text_transform?: "none" | "uppercase";
    fill?: string;
    align?: Align;
    text?: string;
    bbox?: Bbox;
  },
): void {
  if (patch.font_size_px !== undefined) {
    el.setAttribute("data-font-size-px", String(patch.font_size_px));
    el.style.fontSize = `${patch.font_size_px}px`;
  }
  if (patch.fill !== undefined) {
    el.setAttribute("data-fill", patch.fill);
    el.style.color = patch.fill;
  }
  if (patch.font_weight !== undefined) {
    el.setAttribute("data-font-weight", String(patch.font_weight));
    el.style.fontWeight = String(patch.font_weight);
  }
  if (patch.font_style !== undefined) {
    el.setAttribute("data-font-style", patch.font_style);
    el.style.fontStyle = patch.font_style;
  }
  if (patch.line_height !== undefined) {
    el.setAttribute("data-line-height", String(patch.line_height));
    el.style.lineHeight = String(patch.line_height);
  }
  if (patch.letter_spacing !== undefined) {
    el.setAttribute("data-letter-spacing", String(patch.letter_spacing));
    el.style.letterSpacing = `${patch.letter_spacing}px`;
  }
  if (patch.text_transform !== undefined) {
    el.setAttribute("data-text-transform", patch.text_transform);
    el.style.textTransform = patch.text_transform;
  }
  if (patch.align !== undefined) {
    el.setAttribute("data-align", patch.align);
    el.style.textAlign = patch.align;
  }
  if (patch.bbox !== undefined) writeLayerBBox(el, patch.bbox);
  // text is handled by contenteditable directly; we don't reset
  // textContent here because that would blow away the cursor position.
}

function styleValue(el: Element, prop: string): string | null {
  if (!(el instanceof HTMLElement)) return null;
  const inline = el.style.getPropertyValue(prop);
  if (inline) return inline.trim();
  const computed = el.ownerDocument.defaultView
    ?.getComputedStyle(el)
    .getPropertyValue(prop)
    .trim();
  if (computed) return computed;
  return null;
}

function normalizedFontFamily(raw: string | null): string | undefined {
  const first = raw?.split(",", 1)[0]?.trim().replace(/^['"]|['"]$/g, "");
  return first || undefined;
}

function normalizedLineHeight(
  raw: string | null,
  fontSize: number | undefined,
): number | undefined {
  if (!raw || raw === "normal") return undefined;
  if (raw.endsWith("px")) {
    const pixels = pxValue(raw);
    return pixels !== undefined && fontSize ? pixels / fontSize : undefined;
  }
  return NUM(raw);
}

function normalizedCssColor(raw: string | null): string | undefined {
  if (!raw) return undefined;
  const value = raw.trim();
  if (/^#[0-9a-f]{6}$/i.test(value)) return value.toLowerCase();
  const channels = value.match(/[\d.]+/g)?.slice(0, 3).map(Number);
  if (!channels || channels.length !== 3 || channels.some((item) => !Number.isFinite(item))) {
    return value;
  }
  return `#${channels
    .map((channel) => Math.max(0, Math.min(255, Math.round(channel))).toString(16).padStart(2, "0"))
    .join("")}`;
}
