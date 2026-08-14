import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { translate } from "@/lib/i18n";
import { useApp } from "@/lib/store";
import type { Bbox, PosterAreaSelectionItem, PosterDrawingPath } from "@/lib/types";

interface Props {
  iframe: HTMLIFrameElement | null;
  active: boolean;
  scale?: number;
}

type DragState = {
  pointer_id: number;
  start_x: number;
  start_y: number;
  last_x: number;
  last_y: number;
  dragging: boolean;
  additive: boolean;
};

type AreaTool = "select" | "draw";
type DrawingPoint = { x: number; y: number };
type DrawingStroke = {
  pointer_id: number;
  points: DrawingPoint[];
  bounds: Bbox;
  color: string;
  width_px: number;
  additive: boolean;
};

const MIN_REGION_PX = 12;
const DRAW_COLOR = "#dc2f2f";
const DRAW_WIDTH_PX = 7;
const MAX_DRAW_POINTS = 160;
const MAX_AREA_ITEMS = 6;

export function HtmlAreaRevisionEditor({
  iframe,
  active,
  scale = 1,
}: Props) {
  const items = useApp((s) => s.area_revision_items);
  const focusId = useApp((s) => s.area_revision_focus_id);
  const addAreaRevisionItem = useApp((s) => s.addAreaRevisionItem);
  const clearAreaRevisionItems = useApp((s) => s.clearAreaRevisionItems);
  const setAreaRevisionActive = useApp((s) => s.setAreaRevisionActive);
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  const overlayRef = useRef<HTMLDivElement | null>(null);
  const focusFlashRef = useRef<number | null>(null);
  const [tool, setTool] = useState<AreaTool>("select");
  const [drag, setDrag] = useState<DragState | null>(null);
  const [drawing, setDrawing] = useState<DrawingStroke | null>(null);
  const [draftItem, setDraftItem] = useState<PosterAreaSelectionItem | null>(null);
  const [draftInstruction, setDraftInstruction] = useState("");
  const [flashId, setFlashId] = useState<string | null>(null);
  const [, forceRender] = useState(0);

  useEffect(() => {
    if (!active) {
      setDrag(null);
      setDrawing(null);
      setDraftItem(null);
      setDraftInstruction("");
      setTool("select");
    }
  }, [active]);

  useEffect(() => {
    clearAreaRevisionItems();
    setDrag(null);
    setDrawing(null);
    setDraftItem(null);
    setDraftInstruction("");
  }, [iframe, clearAreaRevisionItems]);

  useEffect(() => {
    if (!active) return;
    const tick = () => forceRender((n) => n + 1);
    const scroller = overlayRef.current?.closest("[data-canvas-scroll]") ?? overlayRef.current?.parentElement;
    window.addEventListener("resize", tick);
    scroller?.addEventListener("scroll", tick, { passive: true });
    return () => {
      window.removeEventListener("resize", tick);
      scroller?.removeEventListener("scroll", tick);
    };
  }, [active]);

  const frame = useCallback(() => {
    const doc = iframe?.contentDocument;
    const win = iframe?.contentWindow;
    if (!iframe || !doc || !win) return null;
    const frameRect = iframe.getBoundingClientRect();
    const overlayRect = overlayRef.current?.getBoundingClientRect();
    if (!overlayRect) return null;
    const safeScale = Math.max(0.02, scale || 1);
    return { doc, win, frameRect, overlayRect, safeScale };
  }, [iframe, scale]);

  const pointFromEvent = useCallback((event: React.PointerEvent): { x: number; y: number } | null => {
    const f = frame();
    if (!f) return null;
    const x = (event.clientX - f.frameRect.left) / f.safeScale;
    const y = (event.clientY - f.frameRect.top) / f.safeScale;
    if (x < 0 || y < 0 || x > f.doc.documentElement.scrollWidth || y > f.doc.documentElement.scrollHeight) {
      return null;
    }
    return { x, y };
  }, [frame]);

  const parentRect = useCallback((rect: Bbox) => {
    const f = frame();
    if (!f) return null;
    return {
      left: f.frameRect.left - f.overlayRect.left + rect.x * f.safeScale,
      top: f.frameRect.top - f.overlayRect.top + rect.y * f.safeScale,
      width: rect.w * f.safeScale,
      height: rect.h * f.safeScale,
    };
  }, [frame]);

  const dragRect = useMemo(() => {
    if (!drag) return null;
    const rect = normalizeRect(drag.start_x, drag.start_y, drag.last_x, drag.last_y);
    return rect.w >= MIN_REGION_PX || rect.h >= MIN_REGION_PX ? rect : null;
  }, [drag]);

  const drawingPaths = useMemo(() => {
    const paths: Array<{ path: PosterDrawingPath; selection_id?: string; index?: number }> = [];
    items.forEach((item, idx) => {
      item.drawing_paths?.forEach((path) => paths.push({
        path,
        selection_id: item.selection_id,
        index: idx + 1,
      }));
    });
    if (drawing) {
      paths.push({
        path: {
          points: drawing.points,
          color: drawing.color,
          width_px: drawing.width_px,
        },
      });
    }
    draftItem?.drawing_paths?.forEach((path) => paths.push({
      path,
      selection_id: draftItem.selection_id,
    }));
    return paths;
  }, [items, drawing, draftItem]);

  useEffect(() => {
    if (!active || !focusId) return;
    const item = items.find((candidate) => candidate.selection_id === focusId);
    if (!item) return;
    const mapped = parentRect(item.rect);
    if (!mapped) return;
    const scroller = overlayRef.current?.closest<HTMLElement>("[data-canvas-scroll]");
    if (scroller) {
      const scrollerRect = scroller.getBoundingClientRect();
      scroller.scrollTo({
        left: scroller.scrollLeft + mapped.left + mapped.width / 2 - scrollerRect.width / 2,
        top: scroller.scrollTop + mapped.top + mapped.height / 2 - scrollerRect.height / 2,
        behavior: "smooth",
      });
    }
    setFlashId(focusId);
    if (focusFlashRef.current) window.clearTimeout(focusFlashRef.current);
    focusFlashRef.current = window.setTimeout(() => setFlashId(null), 900);
    return () => {
      if (focusFlashRef.current) window.clearTimeout(focusFlashRef.current);
    };
  }, [active, focusId, items, parentRect]);

  useEffect(() => {
    if (!active) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (isTypingTarget(event.target)) return;
      event.preventDefault();
      if (items.length > 0) {
        clearAreaRevisionItems();
      } else {
        setAreaRevisionActive(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active, clearAreaRevisionItems, items.length, setAreaRevisionActive]);

  const stageItem = useCallback((item: PosterAreaSelectionItem) => {
    setDraftItem(item);
    setDraftInstruction(item.instruction ?? "");
  }, []);

  const draftMatchesExisting = useMemo(() => (
    !!draftItem && items.some((item) => sameAreaTarget(item, draftItem))
  ), [draftItem, items]);
  const canConfirmDraft = !!draftInstruction.trim()
    && (!!draftMatchesExisting || items.length < MAX_AREA_ITEMS);

  const confirmDraftItem = useCallback(() => {
    if (!draftItem) return;
    if (!draftInstruction.trim()) return;
    if (!draftMatchesExisting && items.length >= MAX_AREA_ITEMS) return;
    addAreaRevisionItem(
      { ...draftItem, instruction: draftInstruction },
      { append: true, toggle: false },
    );
    setDraftItem(null);
    setDraftInstruction("");
  }, [addAreaRevisionItem, draftInstruction, draftItem, draftMatchesExisting, items.length]);

  const selectElementAt = useCallback((x: number, y: number, additive: boolean) => {
    const f = frame();
    if (!f) return;
    const raw = f.doc.elementFromPoint(x, y);
    const element = closestPosterTarget(raw);
    if (!element) return;
    void additive;
    stageItem(itemForElement(element, f.win));
  }, [frame, stageItem]);

  const resetPointerState = useCallback((event?: React.PointerEvent<HTMLDivElement>) => {
    if (event && event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    setDrag(null);
    setDrawing(null);
  }, []);

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!active || event.button !== 0) return;
    if ((event.target as HTMLElement).closest("[data-area-revision-control]")) return;
    const point = pointFromEvent(event);
    if (!point) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    const additive = event.shiftKey || event.metaKey || event.ctrlKey;
    if (tool === "draw") {
      setDrawing({
        pointer_id: event.pointerId,
        points: [point],
        bounds: { x: point.x, y: point.y, w: 0, h: 0 },
        color: DRAW_COLOR,
        width_px: DRAW_WIDTH_PX,
        additive,
      });
      setDrag(null);
      return;
    }
    setDrag({
      pointer_id: event.pointerId,
      start_x: point.x,
      start_y: point.y,
      last_x: point.x,
      last_y: point.y,
      dragging: false,
      additive,
    });
  };

  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    if (drawing && event.pointerId === drawing.pointer_id) {
      const point = pointFromEvent(event);
      if (!point) return;
      setDrawing((prev) => {
        if (!prev) return prev;
        const last = prev.points[prev.points.length - 1];
        if (last && Math.hypot(point.x - last.x, point.y - last.y) < 2) return prev;
        const nextPoints = prev.points.length >= MAX_DRAW_POINTS
          ? [...prev.points.slice(0, -1), point]
          : [...prev.points, point];
        return {
          ...prev,
          points: nextPoints,
          bounds: expandPointBounds(prev.bounds, point),
        };
      });
      return;
    }
    if (!drag || event.pointerId !== drag.pointer_id) return;
    const point = pointFromEvent(event);
    if (!point) return;
    const moved = Math.hypot(point.x - drag.start_x, point.y - drag.start_y) > 4;
    setDrag((prev) => prev
      ? { ...prev, last_x: point.x, last_y: point.y, dragging: prev.dragging || moved }
      : prev);
  };

  const onPointerUp = (event: React.PointerEvent<HTMLDivElement>) => {
    if (drawing && event.pointerId === drawing.pointer_id) {
      const point = pointFromEvent(event);
      const points = point ? [...drawing.points, point] : drawing.points;
      const bounds = point ? expandPointBounds(drawing.bounds, point) : drawing.bounds;
      resetPointerState(event);
      const rect = rectForBounds(bounds, drawing.width_px, iframe?.contentDocument ?? null);
      if (points.length >= 2 && rect.w >= MIN_REGION_PX && rect.h >= MIN_REGION_PX) {
        void drawing.additive;
        stageItem(itemForDrawing(points, drawing, iframe?.contentDocument ?? null, rect));
      }
      return;
    }
    if (!drag || event.pointerId !== drag.pointer_id) return;
    const point = pointFromEvent(event);
    const wasDragging = drag.dragging && point;
    const additive = drag.additive;
    resetPointerState(event);
    if (!point) return;
    if (wasDragging) {
      const rect = normalizeRect(drag.start_x, drag.start_y, point.x, point.y);
      if (rect.w >= MIN_REGION_PX && rect.h >= MIN_REGION_PX) {
        void additive;
        stageItem(itemForRegion(rect, iframe?.contentDocument ?? null));
      }
      return;
    }
    selectElementAt(point.x, point.y, additive);
  };

  if (!active || !iframe) return null;

  const draftRect = draftItem ? parentRect(draftItem.rect) : null;
  const draftPopoverStyle = draftRect
    ? popoverPosition(draftRect, overlayRef.current?.getBoundingClientRect() ?? null)
    : undefined;

  return (
    <div
      ref={overlayRef}
      className="absolute inset-0 z-30 cursor-crosshair"
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={resetPointerState}
      onLostPointerCapture={resetPointerState}
    >
      <div
        data-area-revision-control
        className="absolute left-1/2 top-3 flex -translate-x-1/2 items-center gap-2 rounded-md border border-accent/30 bg-surface-raised px-2 py-1.5 text-[11px] font-medium text-ink-800 shadow-soft"
        onPointerDown={(event) => event.stopPropagation()}
      >
        <span className="hidden text-ink-600 sm:inline">
          {tool === "draw"
            ? t("Draw markup, then add an area note.")
            : t("Click sections or drag areas, then add an area note.")}
        </span>
        <span className="flex overflow-hidden rounded-sm border border-ink-300/70 bg-paper">
          <ToolButton active={tool === "select"} onClick={() => setTool("select")}>
            {t("Select")}
          </ToolButton>
          <ToolButton active={tool === "draw"} onClick={() => setTool("draw")}>
            {t("Draw")}
          </ToolButton>
        </span>
        {items.length > 0 && (
          <button
            type="button"
            className="px-2 py-1 text-[10px] font-medium uppercase text-ink-600 transition hover:text-ink-900"
            style={{ letterSpacing: "0.12em" }}
            onClick={clearAreaRevisionItems}
          >
            {t("Clear")}
          </button>
        )}
      </div>
      {drawingPaths.length > 0 && (
        <svg className="pointer-events-none absolute inset-0 h-full w-full overflow-visible">
          {drawingPaths.map((entry, idx) => (
            <polyline
              key={`${entry.selection_id ?? "live"}-${idx}`}
              points={drawingSvgPoints(entry.path.points, parentRect)}
              fill="none"
              stroke={entry.path.color ?? DRAW_COLOR}
              strokeWidth={Math.max(2, (entry.path.width_px ?? DRAW_WIDTH_PX) * Math.max(0.4, scale))}
              strokeLinecap="round"
              strokeLinejoin="round"
              opacity={entry.selection_id === flashId ? 1 : 0.92}
            />
          ))}
        </svg>
      )}
      {items.map((item, idx) => {
        const mapped = parentRect(item.rect);
        if (!mapped) return null;
        const activeFlash = item.selection_id === flashId;
        const badgeClass = item.kind === "drawing"
          ? "bg-[#dc2f2f] text-white"
          : "bg-sky-600 text-white";
        return (
          <div key={item.selection_id} className="pointer-events-none absolute">
            {item.kind !== "drawing" && (
              <div
                className={`absolute rounded-md border-2 border-sky-500 bg-sky-500/10 shadow-[0_0_0_4px_rgba(255,255,255,0.92)] ${
                  activeFlash ? "ring-4 ring-sky-300/70" : ""
                }`}
                style={{
                  left: mapped.left,
                  top: mapped.top,
                  width: Math.max(1, mapped.width),
                  height: Math.max(1, mapped.height),
                }}
              />
            )}
            <div
              className={`absolute flex h-5 min-w-5 items-center justify-center rounded-full px-1.5 text-[10px] font-semibold shadow-soft ${badgeClass} ${
                activeFlash ? "ring-4 ring-white" : ""
              }`}
              style={{
                left: Math.max(4, mapped.left - 8),
                top: Math.max(4, mapped.top - 8),
              }}
            >
              {idx + 1}
            </div>
          </div>
        );
      })}
      {draftItem && draftItem.kind !== "drawing" && draftRect && (
        <div
          className="pointer-events-none absolute rounded-md border-2 border-accent bg-accent/10 shadow-[0_0_0_4px_rgba(255,255,255,0.92)]"
          style={{
            left: draftRect.left,
            top: draftRect.top,
            width: Math.max(1, draftRect.width),
            height: Math.max(1, draftRect.height),
          }}
        />
      )}
      {draftItem && draftRect && (
        <div
          className="pointer-events-none absolute flex h-5 min-w-5 items-center justify-center rounded-full bg-accent px-1.5 text-[10px] font-semibold text-white shadow-soft"
          style={{
            left: Math.max(4, draftRect.left - 8),
            top: Math.max(4, draftRect.top - 8),
          }}
        >
          {items.length + 1}
        </div>
      )}
      {dragRect && (
        <SelectionBox rect={dragRect} parentRect={parentRect} />
      )}
      {draftItem && draftPopoverStyle && (
        <div
          data-area-revision-control
          className="absolute w-[340px] rounded-md border border-ink-300/75 bg-surface-raised p-3 shadow-raised"
          style={draftPopoverStyle}
          onPointerDown={(event) => event.stopPropagation()}
        >
          <div className="mb-2 flex items-center justify-between gap-2">
            <div className="min-w-0">
              <div className="eyebrow">{t("Area")} {items.length + 1}</div>
              <div className="mt-0.5 truncate text-[12px] font-medium text-ink-800">
                {draftItem.label}
              </div>
            </div>
            <button
              type="button"
              className="icon-btn h-6 w-6"
              onClick={() => {
                setDraftItem(null);
                setDraftInstruction("");
              }}
              title={t("Cancel area")}
            >
              x
            </button>
          </div>
          <textarea
            value={draftInstruction}
            onChange={(event) => setDraftInstruction(event.target.value)}
            placeholder={t("What should change in this area?")}
            className="min-h-[86px] w-full resize-none rounded-md border border-ink-300/80 bg-white px-2.5 py-2 text-[12.5px] leading-5 text-ink-900 outline-none placeholder:text-ink-500 focus:border-accent"
            autoFocus
          />
          <div className="mt-3 flex items-center justify-end gap-2">
            <button
              type="button"
              className="inline-flex h-7 items-center rounded-md border border-ink-300/75 bg-paper/80 px-2.5 text-[10px] font-medium uppercase text-ink-700 transition hover:bg-white"
              style={{ letterSpacing: "0.14em" }}
              onClick={() => {
                setDraftItem(null);
                setDraftInstruction("");
              }}
            >
              {t("Cancel")}
            </button>
            <button
              type="button"
              className="inline-flex h-7 items-center rounded-md border border-accent bg-accent px-2.5 text-[10px] font-medium uppercase text-white transition hover:bg-accent-deep disabled:cursor-not-allowed disabled:opacity-55"
              style={{ letterSpacing: "0.14em" }}
              disabled={!canConfirmDraft}
              onClick={confirmDraftItem}
            >
              {t("Add area")}
            </button>
          </div>
          {!draftMatchesExisting && items.length >= MAX_AREA_ITEMS && (
            <div className="mt-2 text-[11px] text-amber-800">
              {t("Remove an existing area before adding another.")}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ToolButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`px-2 py-1 text-[10px] font-medium uppercase transition ${
        active
          ? "bg-accent text-white"
          : "text-ink-600 hover:bg-vellum hover:text-ink-900"
      }`}
      style={{ letterSpacing: "0.12em" }}
    >
      {children}
    </button>
  );
}

function SelectionBox({
  rect,
  parentRect,
}: {
  rect: Bbox;
  parentRect: (rect: Bbox) => { left: number; top: number; width: number; height: number } | null;
}) {
  const mapped = parentRect(rect);
  if (!mapped) return null;
  return (
    <div
      className="pointer-events-none absolute rounded-md border-2 border-sky-500 bg-sky-500/10 shadow-[0_0_0_4px_rgba(255,255,255,0.92)]"
      style={{
        left: mapped.left,
        top: mapped.top,
        width: Math.max(1, mapped.width),
        height: Math.max(1, mapped.height),
      }}
    />
  );
}

function popoverPosition(
  rect: { left: number; top: number; width: number; height: number },
  bounds: DOMRect | null,
) {
  const popW = 340;
  const popH = 188;
  const pad = 12;
  const maxLeft = Math.max(pad, (bounds?.width ?? 1200) - popW - pad);
  const maxTop = Math.max(pad, (bounds?.height ?? 800) - popH - pad);
  const right = rect.left + rect.width + 10;
  const below = rect.top + rect.height + 10;
  return {
    left: Math.min(maxLeft, right),
    top: Math.min(maxTop, right <= maxLeft ? rect.top : below),
  };
}

function sameAreaTarget(a: PosterAreaSelectionItem, b: PosterAreaSelectionItem): boolean {
  if (a.block_id && b.block_id) return a.block_id === b.block_id;
  if (a.selector && b.selector) return a.selector === b.selector;
  return a.selection_id === b.selection_id;
}

function nextSelectionId(kind: string): string {
  return `area_${kind}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

function normalizeRect(x1: number, y1: number, x2: number, y2: number): Bbox {
  const x = Math.min(x1, x2);
  const y = Math.min(y1, y2);
  return {
    x: Math.round(x),
    y: Math.round(y),
    w: Math.round(Math.abs(x2 - x1)),
    h: Math.round(Math.abs(y2 - y1)),
  };
}

function rectForPoints(points: DrawingPoint[], widthPx = 0, doc: Document | null = null): Bbox {
  const xs = points.map((point) => point.x);
  const ys = points.map((point) => point.y);
  const pad = Math.max(4, Math.ceil(widthPx / 2));
  const minX = Math.min(...xs) - pad;
  const minY = Math.min(...ys) - pad;
  const maxX = Math.max(...xs) + pad;
  const maxY = Math.max(...ys) + pad;
  return clampRectToDocument({
    x: Math.round(minX),
    y: Math.round(minY),
    w: Math.round(maxX - minX),
    h: Math.round(maxY - minY),
  }, doc);
}

function expandPointBounds(bounds: Bbox, point: DrawingPoint): Bbox {
  const left = Math.min(bounds.x, point.x);
  const top = Math.min(bounds.y, point.y);
  const right = Math.max(bounds.x + bounds.w, point.x);
  const bottom = Math.max(bounds.y + bounds.h, point.y);
  return {
    x: left,
    y: top,
    w: right - left,
    h: bottom - top,
  };
}

function rectForBounds(bounds: Bbox, widthPx = 0, doc: Document | null = null): Bbox {
  const pad = Math.max(4, Math.ceil(widthPx / 2));
  return clampRectToDocument({
    x: Math.round(bounds.x - pad),
    y: Math.round(bounds.y - pad),
    w: Math.round(bounds.w + pad * 2),
    h: Math.round(bounds.h + pad * 2),
  }, doc);
}

function clampRectToDocument(rect: Bbox, doc: Document | null): Bbox {
  if (!doc) return rect;
  const maxW = Math.max(1, doc.documentElement.scrollWidth || rect.x + rect.w);
  const maxH = Math.max(1, doc.documentElement.scrollHeight || rect.y + rect.h);
  const x = Math.max(0, Math.min(rect.x, maxW - 1));
  const y = Math.max(0, Math.min(rect.y, maxH - 1));
  const right = Math.max(x + 1, Math.min(rect.x + rect.w, maxW));
  const bottom = Math.max(y + 1, Math.min(rect.y + rect.h, maxH));
  return {
    x: Math.round(x),
    y: Math.round(y),
    w: Math.round(right - x),
    h: Math.round(bottom - y),
  };
}

function drawingSvgPoints(
  points: DrawingPoint[],
  parentRect: (rect: Bbox) => { left: number; top: number; width: number; height: number } | null,
): string {
  return points
    .map((point) => {
      const mapped = parentRect({ x: point.x, y: point.y, w: 0, h: 0 });
      return mapped ? `${mapped.left},${mapped.top}` : "";
    })
    .filter(Boolean)
    .join(" ");
}

function closestPosterTarget(node: Element | null): HTMLElement | null {
  if (!(node instanceof HTMLElement)) return null;
  const selector = [
    ".poster-section",
    "[data-block-id]",
    "section",
    "article",
    ".panel",
    ".card",
    ".figure",
    ".chart",
    ".table",
    ".metric",
  ].join(",");
  return node.closest<HTMLElement>(selector)
    ?? node.closest<HTMLElement>(".paper-poster")
    ?? node;
}

function itemForDrawing(
  points: DrawingPoint[],
  stroke: Pick<DrawingStroke, "color" | "width_px">,
  doc: Document | null,
  rectOverride?: Bbox,
): PosterAreaSelectionItem {
  const compactPoints = compactDrawingPoints(points);
  const rect = rectOverride ?? rectForPoints(points, stroke.width_px, doc);
  const target = doc ? bestElementForRegion(doc, rect) : null;
  return {
    selection_id: nextSelectionId("drawing"),
    kind: "drawing",
    label: target ? labelForElement(target) : "Drawn markup",
    rect,
    selector: target ? selectorForElement(target) : undefined,
    block_id: target?.dataset.blockId || target?.getAttribute("data-block-id") || undefined,
    text_excerpt: target ? textExcerpt(target) : undefined,
    html_excerpt: target ? htmlExcerpt(target) : undefined,
    nearby_headings: doc ? headingsNearRegion(doc, rect) : undefined,
    drawing_paths: [{
      points: compactPoints,
      color: stroke.color,
      width_px: stroke.width_px,
    }],
  };
}

function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName.toLowerCase();
  return tag === "input"
    || tag === "textarea"
    || tag === "select"
    || target.isContentEditable;
}

function itemForElement(element: HTMLElement, win: Window): PosterAreaSelectionItem {
  const rect = element.getBoundingClientRect();
  const doc = element.ownerDocument;
  const documentRect = {
    x: Math.round(rect.left + win.scrollX),
    y: Math.round(rect.top + win.scrollY),
    w: Math.round(rect.width),
    h: Math.round(rect.height),
  };
  const blockId = element.dataset.blockId || element.getAttribute("data-block-id") || undefined;
  return {
    selection_id: blockId ? `block:${blockId}` : nextSelectionId("element"),
    kind: "element",
    label: labelForElement(element),
    rect: documentRect,
    selector: selectorForElement(element),
    block_id: blockId,
    text_excerpt: textExcerpt(element),
    html_excerpt: htmlExcerpt(element),
    nearby_headings: nearbyHeadings(element, doc, documentRect),
  };
}

function itemForRegion(rect: Bbox, doc: Document | null): PosterAreaSelectionItem {
  const target = doc ? bestElementForRegion(doc, rect) : null;
  return {
    selection_id: nextSelectionId("region"),
    kind: "region",
    label: target ? labelForElement(target) : "Selected region",
    rect: clampRectToDocument(rect, doc),
    selector: target ? selectorForElement(target) : undefined,
    block_id: target?.dataset.blockId || target?.getAttribute("data-block-id") || undefined,
    text_excerpt: target ? textExcerpt(target) : undefined,
    html_excerpt: target ? htmlExcerpt(target) : undefined,
    nearby_headings: doc ? headingsNearRegion(doc, rect) : undefined,
  };
}

function labelForElement(element: HTMLElement): string {
  const heading = Array.from(element.querySelectorAll<HTMLElement>("h1,h2,h3,h4,.section-title,.poster-section-title"))
    .map((node) => textExcerpt(node))
    .find(Boolean);
  if (heading) return heading.slice(0, 70);
  const blockId = element.dataset.blockId || element.getAttribute("data-block-id");
  if (blockId) return blockId.replace(/[_-]+/g, " ").slice(0, 70);
  const text = textExcerpt(element);
  if (text) return text.slice(0, 70);
  return "Selected section";
}

function compactDrawingPoints(points: DrawingPoint[]): DrawingPoint[] {
  if (points.length <= MAX_DRAW_POINTS) {
    return points.map(roundPoint);
  }
  const step = Math.ceil(points.length / MAX_DRAW_POINTS);
  const sampled = points.filter((_, idx) => idx % step === 0);
  const last = points[points.length - 1];
  if (last && sampled[sampled.length - 1] !== last) {
    sampled.push(last);
  }
  return sampled.slice(0, MAX_DRAW_POINTS).map(roundPoint);
}

function roundPoint(point: DrawingPoint): DrawingPoint {
  return { x: Math.round(point.x), y: Math.round(point.y) };
}

function selectorForElement(element: HTMLElement): string | undefined {
  const blockId = element.dataset.blockId || element.getAttribute("data-block-id");
  if (blockId) return `[data-block-id="${cssEscape(blockId)}"]`;
  if (element.id) return `#${cssEscape(element.id)}`;
  const parent = element.parentElement;
  if (!parent) return element.tagName.toLowerCase();
  const index = Array.from(parent.children).indexOf(element) + 1;
  return `${element.tagName.toLowerCase()}:nth-child(${index})`;
}

function cssEscape(value: string): string {
  const esc = (globalThis as any).CSS?.escape;
  return typeof esc === "function" ? esc(value) : value.replace(/["\\]/g, "\\$&");
}

function textExcerpt(element: HTMLElement): string | undefined {
  const text = (element.innerText || element.textContent || "")
    .replace(/\s+/g, " ")
    .trim();
  return text ? text.slice(0, 900) : undefined;
}

function htmlExcerpt(element: HTMLElement): string | undefined {
  const clone = element.cloneNode(true) as HTMLElement;
  clone.querySelectorAll(".od-flow-layout-handle,.od-flow-drop-marker,[data-area-revision-control]").forEach((node) => node.remove());
  [clone, ...Array.from(clone.querySelectorAll<HTMLElement>("*"))].forEach((node) => {
    node.classList.remove("ld-active", "od-flow-editable-section", "od-flow-dragging", "od-flow-resizing", "od-flow-drop-column");
    node.removeAttribute("contenteditable");
  });
  const html = clone.outerHTML.replace(/\s+/g, " ").trim();
  return html ? html.slice(0, 1000) : undefined;
}

function bestElementForRegion(doc: Document, rect: Bbox): HTMLElement | null {
  const selector = [
    ".poster-section",
    "[data-block-id]",
    "section",
    "article",
    ".panel",
    ".card",
    ".figure",
    ".chart",
    ".table",
    ".metric",
  ].join(",");
  const center = { x: rect.x + rect.w / 2, y: rect.y + rect.h / 2 };
  const candidates = Array.from(doc.querySelectorAll<HTMLElement>(selector))
    .map((element) => {
      const region = regionRect(element);
      const overlap = overlapArea(region, rect);
      const area = Math.max(1, region.w * region.h);
      const cx = region.x + region.w / 2;
      const cy = region.y + region.h / 2;
      return {
        element,
        overlap,
        ratio: overlap / area,
        distance: Math.hypot(center.x - cx, center.y - cy),
        area,
      };
    })
    .filter((item) => item.overlap > 0)
    .sort((a, b) =>
      b.ratio - a.ratio ||
      b.overlap - a.overlap ||
      a.distance - b.distance ||
      a.area - b.area
    );
  return candidates[0]?.element ?? null;
}

function regionRect(element: HTMLElement): Bbox {
  const win = element.ownerDocument.defaultView;
  const rect = element.getBoundingClientRect();
  return {
    x: Math.round(rect.left + (win?.scrollX ?? 0)),
    y: Math.round(rect.top + (win?.scrollY ?? 0)),
    w: Math.round(rect.width),
    h: Math.round(rect.height),
  };
}

function overlapArea(a: Bbox, b: Bbox): number {
  const x = Math.max(0, Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x));
  const y = Math.max(0, Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y));
  return x * y;
}

function nearbyHeadings(element: HTMLElement, doc: Document, rect: Bbox): string[] | undefined {
  const headings = Array.from(element.querySelectorAll<HTMLElement>("h1,h2,h3,h4,.section-title,.poster-section-title"))
    .map((node) => textExcerpt(node))
    .filter((text): text is string => !!text)
    .slice(0, 4);
  if (headings.length) return headings;
  return headingsNearRegion(doc, rect);
}

function headingsNearRegion(doc: Document, rect: Bbox): string[] | undefined {
  const centerY = rect.y + rect.h / 2;
  const candidates = Array.from(doc.querySelectorAll<HTMLElement>("h1,h2,h3,h4,.section-title,.poster-section-title"))
    .map((node) => {
      const r = node.getBoundingClientRect();
      return { text: textExcerpt(node), distance: Math.abs((r.top + r.height / 2) - centerY) };
    })
    .filter((item): item is { text: string; distance: number } => !!item.text)
    .sort((a, b) => a.distance - b.distance)
    .slice(0, 4)
    .map((item) => item.text);
  return candidates.length ? candidates : undefined;
}
