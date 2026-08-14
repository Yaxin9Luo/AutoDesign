/**
 * Bridges the iframe's already-contenteditable text layers to the
 * parent React app's Zustand store. Mounts on the iframe load event,
 * walks `[contenteditable="true"]` children, and listens for focus +
 * input + blur to drive:
 *
 * - `selectLayer` — so the right-rail Sidebar mirrors the selection
 * - `updateLayer({ text })` — accumulates into pending_edits for manual save
 * - the floating toolbar's anchor rect + active-layer state
 *
 * The iframe is same-origin (Vite proxies /api/files/runs/* through
 * :5173) so direct DOM access is safe — no postMessage needed. The
 * agent's renderer (composite.py) emits contenteditable=true on text
 * layers, so we don't have to inject that attribute.
 */
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { useApp } from "@/lib/store";
import { hasPaperAssetDrag, readPaperAssetDrag } from "@/lib/paper_asset_drag";
import { translate } from "@/lib/i18n";
import { stripDeckNavigationState } from "@/lib/deck_navigation";
import {
  applyOptimisticPatch,
  editableLayerForPointerTarget,
  flowOffsetForBBox,
  layerRectInParent,
  readLayerBBox,
  readLayerKind,
  readLayerState,
  walkEditableLayers,
  walkInteractiveLayers,
  writeLayerBBox,
  type LayerRect,
  type ToolbarLayerState,
} from "@/lib/iframe_bridge";
import type { Align, ArtifactAsset, Bbox, HtmlLayoutPatch, Layer, PendingEditsPayload } from "@/lib/types";
import { FloatingToolbar } from "./FloatingToolbar";
import { findInjectedStyle } from "./styleTagCompatibility";
import { I } from "../icons";

interface Props {
  /** The iframe element to bridge. Caller (Canvas.tsx) holds the ref;
   *  we attach listeners on every onLoad (the iframe re-loads after
   *  every successful save, since the artifact's
   *  `native_file_url` changes). */
  iframe: HTMLIFrameElement | null;
  /** Parent canvas zoom. Iframe CSS is authored in artwork pixels, so
   *  active outlines need to get thicker as the poster zooms down. */
  scale?: number;
}

type ResizeHandle = "n" | "e" | "s" | "w" | "nw" | "ne" | "sw" | "se";
type ImageReplacementAsset = {
  url: string;
  name?: string;
  layer_id?: string;
};

export function InPlaceEditor({ iframe, scale = 1 }: Props) {
  const updateLayer = useApp((s) => s.updateLayer);
  const recordHtmlLayoutPatch = useApp((s) => s.recordHtmlLayoutPatch);
  const replaceActiveArtifactPendingEdits = useApp((s) => s.replaceActiveArtifactPendingEdits);
  const flushAutoSave = useApp((s) => s.flushAutoSave);
  const selectLayer = useApp((s) => s.selectLayer);
  const syncLayerInspection = useApp((s) => s.syncLayerInspection);
  const selectedIds = useApp((s) => s.selected_layer_ids);
  const selectedPaperAsset = useApp((s) => s.selected_paper_asset);
  const captureHistorySnapshot = useApp((s) => s.captureHistorySnapshot);
  const [active, setActive] = useState<ToolbarLayerState | null>(null);
  const [rect, setRect] = useState<LayerRect | null>(null);
  const [htmlHistoryTick, setHtmlHistoryTick] = useState(0);
  const htmlHistoryRef = useRef<HtmlEditHistory>({ past: [], future: [] });
  // Tracks the active iframe layer element so the toolbar can call
  // applyOptimisticPatch on it for immediate visual feedback.
  const [activeEl, setActiveEl] = useState<HTMLElement | null>(null);
  const canUndoHtml = htmlHistoryTick >= 0 && htmlHistoryRef.current.past.length > 0;
  const canRedoHtml = htmlHistoryTick >= 0 && htmlHistoryRef.current.future.length > 0;

  const currentPendingEdits = (): PendingEditsPayload | undefined => {
    const state = useApp.getState();
    const conv = state.conversations[state.current_conversation_id];
    const artId = conv?.active_artifact_id;
    const edits = artId ? conv?.pending_edits?.[artId] : undefined;
    return clonePendingEdits(edits);
  };

  const captureHtmlSnapshot = (): HtmlEditSnapshot | null => {
    const doc = iframe?.contentDocument;
    const root = doc?.querySelector<HTMLElement>(
      ".paper-poster, [data-autodesign-artifact-root]",
    );
    if (!doc || !root) return null;
    const clone = root.cloneNode(true) as HTMLElement;
    cleanupEditorDom(clone);
    return {
      posterHtml: clone.innerHTML,
      pendingEdits: currentPendingEdits(),
    };
  };

  const restoreHtmlSnapshot = (snapshot: HtmlEditSnapshot) => {
    const root = iframe?.contentDocument?.querySelector<HTMLElement>(
      ".paper-poster, [data-autodesign-artifact-root]",
    );
    if (!iframe || !root) return;
    root.innerHTML = snapshot.posterHtml;
    replaceActiveArtifactPendingEdits(clonePendingEdits(snapshot.pendingEdits));
    setActive(null);
    setActiveEl(null);
    setRect(null);
    selectLayer(null);
    window.setTimeout(() => {
      iframe.dispatchEvent(new Event("load"));
    }, 0);
  };

  const rememberHtmlEdit = (before: HtmlEditSnapshot | null) => {
    if (!before) return;
    htmlHistoryRef.current = {
      past: [...htmlHistoryRef.current.past.slice(-49), before],
      future: [],
    };
    setHtmlHistoryTick((tick) => tick + 1);
  };

  const undoHtmlEdit = () => {
    const history = htmlHistoryRef.current;
    if (!history.past.length) return;
    const current = captureHtmlSnapshot();
    const previous = history.past[history.past.length - 1];
    htmlHistoryRef.current = {
      past: history.past.slice(0, -1),
      future: current ? [current, ...history.future].slice(0, 50) : history.future,
    };
    restoreHtmlSnapshot(previous);
    setHtmlHistoryTick((tick) => tick + 1);
  };

  const redoHtmlEdit = () => {
    const history = htmlHistoryRef.current;
    if (!history.future.length) return;
    const current = captureHtmlSnapshot();
    const next = history.future[0];
    htmlHistoryRef.current = {
      past: current ? [...history.past, current].slice(-50) : history.past,
      future: history.future.slice(1),
    };
    restoreHtmlSnapshot(next);
    setHtmlHistoryTick((tick) => tick + 1);
  };

  const deleteActiveDomObject = () => {
    if (!iframe || !active || !activeEl) return;
    const target = deletableDomTarget(activeEl);
    if (!target) return;
    const before = captureHtmlSnapshot();
    const patch = buildDomDeletePatch(target, active);
    target.remove();
    recordHtmlLayoutPatch(patch);
    rememberHtmlEdit(before);
    setActive(null);
    setActiveEl(null);
    setRect(null);
    selectLayer(null);
  };

  const replaceImageElementWithAsset = useCallback((el: HTMLElement, asset: ImageReplacementAsset) => {
    if (!iframe) return;
    const state = readLayerState(el);
    if (!state || state.kind !== "image") return;

    const before = captureHtmlSnapshot();
    captureHistorySnapshot();
    const bbox = readLayerBBox(el);
    applyPaperAssetToImageElement(el, asset.url);
    if (bbox) writeLayerBBox(el, bbox);
    const flowOffset = bbox ? flowOffsetForBBox(el, bbox) : null;
    updateLayer(state.layer_id, {
      src: asset.url,
      fit: "contain",
      object_position: { x: 0.5, y: 0.5 },
      ...(bbox ? { bbox } : {}),
      ...(flowOffset ? { flow_offset: flowOffset } : {}),
    });
    setActive(state);
    setActiveEl(el);
    setRect(layerRectInParent(el, iframe));
    el.ownerDocument.querySelectorAll(".ld-active").forEach((node) => {
      node.classList.remove("ld-active");
    });
    el.classList.add("ld-active");
    selectLayer(state.layer_id);
    rememberHtmlEdit(before);
  }, [captureHistorySnapshot, iframe, selectLayer, updateLayer]);

  const replaceActiveImageWithAsset = useCallback((asset: ImageReplacementAsset) => {
    const doc = iframe?.contentDocument;
    const target =
      asset.layer_id && doc
        ? findLayerElementForSelection(doc, asset.layer_id)
        : activeEl;
    if (!target) return;
    replaceImageElementWithAsset(target, asset);
  }, [activeEl, iframe, replaceImageElementWithAsset]);

  useEffect(() => {
    if (!iframe) return;

    let cleanup: (() => void) | null = null;

    const wire = () => {
      // Tear down prior listeners (this fires on every iframe load,
      // including the post-save re-render).
      if (cleanup) cleanup();
      const doc = iframe.contentDocument;
      if (!doc?.head || !doc.body) return;
      injectSelectionStyles(doc, scale);

      // The agent's HTML has `[data-reveal]` elements that start at
      // `opacity: 0` and rely on the iframe's bundled JS (a scroll
      // observer) to add `.is-revealed` and animate them in. We use
      // `sandbox="allow-same-origin"` WITHOUT `allow-scripts` so the
      // bundled ld-toolbar JS doesn't compete with our floating
      // toolbar — but that also means the reveal observer never runs.
      // Force-reveal every section so the page is visible immediately.
      doc.querySelectorAll<HTMLElement>("[data-reveal]").forEach((el) => {
        el.classList.add("is-revealed");
      });

      const textLayers = walkEditableLayers(doc);
      walkInteractiveLayers(doc);
      const detachers: Array<() => void> = [];

      const activateLayer = (el: HTMLElement) => {
        const state = readLayerState(el);
        if (!state) return;
        setActive(state);
        setActiveEl(el);
        setRect(layerRectInParent(el, iframe));
        doc.querySelectorAll(".ld-active").forEach((node) => {
          node.classList.remove("ld-active");
        });
        el.classList.add("ld-active");
        selectLayer(state.layer_id);
        syncLayerInspection(state.layer_id, inspectedLayerPatch(state));
      };
      const sectionGripDetachers = installSectionGrips(doc, activateLayer);

      const startImageDrag = (el: HTMLElement, e: PointerEvent) => {
        if (e.button !== 0 || readLayerKind(el) !== "image") return;
        const state = readLayerState(el);
        const startBox = readLayerBBox(el);
        const win = iframe.contentWindow;
        if (!state || !startBox || !win) return;

        e.preventDefault();
        e.stopPropagation();
        activateLayer(el);
        captureHistorySnapshot();
        const before = captureHtmlSnapshot();
        const startX = e.clientX;
        const startY = e.clientY;
        let moved = false;

        const onMove = (ev: PointerEvent) => {
          const dx = ev.clientX - startX;
          const dy = ev.clientY - startY;
          if (!moved && Math.abs(dx) + Math.abs(dy) < 2) return;
          moved = true;
          const next = {
            ...startBox,
            x: Math.round(startBox.x + dx),
            y: Math.round(startBox.y + dy),
          };
          writeLayerBBox(el, next);
          const flowOffset = flowOffsetForBBox(el, next);
          setRect(layerRectInParent(el, iframe));
          updateLayer(
            state.layer_id,
            { bbox: next, ...(flowOffset ? { flow_offset: flowOffset } : {}) },
            { history: false },
          );
        };
        const onUp = () => {
          win.removeEventListener("pointermove", onMove);
          win.removeEventListener("pointerup", onUp);
          if (moved) rememberHtmlEdit(before);
        };
        win.addEventListener("pointermove", onMove);
        win.addEventListener("pointerup", onUp);
      };

      const onPointerDown = (e: PointerEvent) => {
        const el = editableLayerForPointerTarget(e.target);
        if (!el) return;
        activateLayer(el);
        startImageDrag(el, e);
      };
      const onFocus = (e: FocusEvent) => {
        activateLayer(e.currentTarget as HTMLElement);
      };
      const onBlur = (e: FocusEvent) => {
        const el = e.currentTarget as HTMLElement;
        const state = readLayerState(el);
        if (!state) return;
        updateLayer(state.layer_id, { text: el.textContent ?? "" });
      };
      const onInput = (e: Event) => {
        const el = e.currentTarget as HTMLElement;
        const state = readLayerState(el);
        if (!state) return;
        updateLayer(state.layer_id, { text: el.textContent ?? "" });
        // Live-update the toolbar's view of the active layer's text.
        setActive((prev) =>
          prev && prev.layer_id === state.layer_id
            ? { ...prev, text: state.text }
            : prev,
        );
      };

      doc.addEventListener("pointerdown", onPointerDown);
      detachers.push(() => doc.removeEventListener("pointerdown", onPointerDown));

      for (const el of textLayers) {
        el.addEventListener("focus", onFocus);
        el.addEventListener("blur", onBlur);
        el.addEventListener("input", onInput);
        detachers.push(() => {
          el.removeEventListener("focus", onFocus);
          el.removeEventListener("blur", onBlur);
          el.removeEventListener("input", onInput);
        });
      }

      // Re-position the toolbar on iframe scroll / window resize.
      // ResizeObserver tracks the iframe element itself; the iframe's
      // own scroll fires through contentWindow.
      const onLayout = () => {
        const focused = doc.activeElement as HTMLElement | null;
        const layer =
          focused?.getAttribute("data-layer-id")
            ? focused
            : doc.querySelector<HTMLElement>(".ld-active[data-layer-id]");
        if (!layer) return;
        setRect(layerRectInParent(layer, iframe));
      };
      const ro = new ResizeObserver(onLayout);
      ro.observe(iframe);
      iframe.contentWindow?.addEventListener("scroll", onLayout, {
        passive: true,
      });
      window.addEventListener("resize", onLayout);
      detachers.push(() => {
        ro.disconnect();
        iframe.contentWindow?.removeEventListener("scroll", onLayout);
        window.removeEventListener("resize", onLayout);
      });

      // Click on the iframe document outside any layer → dismiss.
      const onDocMouseDown = (e: MouseEvent) => {
        const target = e.target;
        const t =
          target instanceof Element
            ? target
            : target instanceof Node
              ? target.parentElement
              : null;
        if (!t?.closest('[data-layer-id]')) {
          setActive(null);
          setActiveEl(null);
          setRect(null);
          doc.querySelectorAll(".ld-active").forEach((node) => {
            node.classList.remove("ld-active");
          });
          selectLayer(null);
        }
      };
      doc.addEventListener("mousedown", onDocMouseDown);
      detachers.push(() => doc.removeEventListener("mousedown", onDocMouseDown));

      let paperAssetDropTarget: HTMLElement | null = null;
      const clearPaperAssetDropTarget = () => {
        paperAssetDropTarget?.classList.remove("od-paper-asset-drop-target");
        paperAssetDropTarget = null;
      };
      const onPaperAssetDragOver = (e: DragEvent) => {
        if (!hasPaperAssetDrag(e.dataTransfer)) return;
        const target = imageElementForPaperAssetDrop(e.target);
        clearPaperAssetDropTarget();
        if (!target) return;
        e.preventDefault();
        if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
        paperAssetDropTarget = target;
        target.classList.add("od-paper-asset-drop-target");
      };
      const onPaperAssetDragLeave = (e: DragEvent) => {
        if (e.target === paperAssetDropTarget) clearPaperAssetDropTarget();
      };
      const onPaperAssetDrop = (e: DragEvent) => {
        const asset = readPaperAssetDrag(e.dataTransfer);
        if (!asset) return;
        const target = imageElementForPaperAssetDrop(e.target);
        clearPaperAssetDropTarget();
        if (!target) return;
        e.preventDefault();
        e.stopPropagation();
        replaceImageElementWithAsset(target, asset);
      };
      doc.addEventListener("dragover", onPaperAssetDragOver);
      doc.addEventListener("dragleave", onPaperAssetDragLeave);
      doc.addEventListener("drop", onPaperAssetDrop);
      detachers.push(() => {
        clearPaperAssetDropTarget();
        doc.removeEventListener("dragover", onPaperAssetDragOver);
        doc.removeEventListener("dragleave", onPaperAssetDragLeave);
        doc.removeEventListener("drop", onPaperAssetDrop);
      });

      // Cmd+S inside the iframe — flush save now. The keydown bubbles
      // up to the iframe's document (not the parent), so we listen on
      // doc directly.
      const onIframeKey = (e: KeyboardEvent) => {
        const key = e.key.toLowerCase();
        if ((e.metaKey || e.ctrlKey) && key === "z") {
          e.preventDefault();
          if (e.shiftKey) redoHtmlEdit();
          else undoHtmlEdit();
          return;
        }
        if ((e.metaKey || e.ctrlKey) && key === "y") {
          e.preventDefault();
          redoHtmlEdit();
          return;
        }
        if ((e.key === "Delete" || e.key === "Backspace") && activeEl && active?.kind !== "text") {
          e.preventDefault();
          deleteActiveDomObject();
          return;
        }
        if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
          e.preventDefault();
          void flushAutoSave();
        }
      };
      doc.addEventListener("keydown", onIframeKey);
      detachers.push(() => doc.removeEventListener("keydown", onIframeKey));

      cleanup = () => {
        sectionGripDetachers.forEach((d) => d());
        for (const d of detachers) d();
      };
    };

    iframe.addEventListener("load", wire);
    // The iframe may have already loaded by the time this effect runs.
    if (iframe.contentDocument?.readyState === "complete") wire();

    return () => {
      iframe.removeEventListener("load", wire);
      if (cleanup) cleanup();
    };
  }, [
    iframe,
    updateLayer,
    flushAutoSave,
    selectLayer,
    syncLayerInspection,
    captureHistorySnapshot,
    scale,
    active,
    activeEl,
    undoHtmlEdit,
    redoHtmlEdit,
    deleteActiveDomObject,
    replaceImageElementWithAsset,
  ]);

  // Keep iframe highlight in sync when the user selects from the Layers
  // panel instead of clicking the iframe itself.
  useEffect(() => {
    const doc = iframe?.contentDocument;
    if (!iframe || !doc?.head || !doc.body) return;
    injectSelectionStyles(doc, scale);

    doc.querySelectorAll(".ld-active").forEach((node) => {
      node.classList.remove("ld-active");
    });

    const layerId = selectedIds.length === 1 ? selectedIds[0] : null;
    if (!layerId) {
      setActive(null);
      setActiveEl(null);
      setRect(null);
      return;
    }

    const el =
      activeEl?.ownerDocument === doc && activeEl.getAttribute("data-layer-id") === layerId
        ? activeEl
        : findLayerElementForSelection(doc, layerId);
    if (!el) return;

    el.classList.add("ld-active");
    const state = readLayerState(el);
    if (state) {
      setActive(state);
      setActiveEl(el);
      setRect(layerRectInParent(el, iframe));
      syncLayerInspection(state.layer_id, inspectedLayerPatch(state));
    } else {
      setActive(null);
      setActiveEl(null);
      setRect(null);
    }
  }, [iframe, selectedIds, scale, activeEl, syncLayerInspection]);

  // Cmd+S in the parent document also flushes — the iframe doesn't
  // capture key events when the focus is in the chrome (e.g., the
  // floating toolbar input).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const key = e.key.toLowerCase();
      if ((e.metaKey || e.ctrlKey) && key === "z") {
        e.preventDefault();
        if (e.shiftKey) redoHtmlEdit();
        else undoHtmlEdit();
        return;
      }
      if ((e.metaKey || e.ctrlKey) && key === "y") {
        e.preventDefault();
        redoHtmlEdit();
        return;
      }
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        void flushAutoSave();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [flushAutoSave, undoHtmlEdit, redoHtmlEdit]);

  useEffect(() => {
    const onReplace = (e: Event) => {
      const asset = (e as CustomEvent<ImageReplacementAsset>).detail;
      if (!isImageReplacementAsset(asset)) return;
      replaceActiveImageWithAsset(asset);
    };
    window.addEventListener("paper-asset:replace-selected", onReplace);
    return () => window.removeEventListener("paper-asset:replace-selected", onReplace);
  }, [replaceActiveImageWithAsset]);

  const onToolbarPatch = (
    patch: { font_size_px?: number; fill?: string; align?: Align },
  ) => {
    if (!active) return;
    const before = captureHtmlSnapshot();
    if (activeEl) applyOptimisticPatch(activeEl, patch);
    updateLayer(active.layer_id, {
      ...(patch.font_size_px !== undefined ? { font_size_px: patch.font_size_px } : {}),
      // The backend reads `effects.fill` for text color (see
      // _patch_text_layer). Sidebar atoms use the same shape.
      ...(patch.fill !== undefined ? { effects: { fill: patch.fill } } : {}),
      ...(patch.align !== undefined ? { align: patch.align } : {}),
    });
    rememberHtmlEdit(before);
    setActive((prev) => (prev ? { ...prev, ...patch } : prev));
  };

  const onImageResizeStart = (handle: ResizeHandle, e: ReactPointerEvent<HTMLButtonElement>) => {
    if (!iframe || !active || active.kind !== "image" || !activeEl || !rect) return;
    const startBox = readLayerBBox(activeEl);
    if (!startBox) return;

    e.preventDefault();
    e.stopPropagation();
    captureHistorySnapshot();
    const before = captureHtmlSnapshot();

    const startClientX = e.clientX;
    const startClientY = e.clientY;
    const xScale = startBox.w / Math.max(1, rect.width);
    const yScale = startBox.h / Math.max(1, rect.height);
    const minSize = 24;

    const onMove = (ev: PointerEvent) => {
      ev.preventDefault();
      const dx = Math.round((ev.clientX - startClientX) * xScale);
      const dy = Math.round((ev.clientY - startClientY) * yScale);
      const next = resizeBBox(startBox, handle, dx, dy, minSize);
      writeLayerBBox(activeEl, next);
      const flowOffset = flowOffsetForBBox(activeEl, next);
      updateLayer(
        active.layer_id,
        { bbox: next, ...(flowOffset ? { flow_offset: flowOffset } : {}) },
        { history: false },
      );
      setRect(layerRectInParent(activeEl, iframe));
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      rememberHtmlEdit(before);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  };

  const onTextMoveStart = (e: ReactPointerEvent<HTMLButtonElement>) => {
    if (!iframe || !active || active.kind !== "text" || !activeEl || !rect) return;
    const startBox = readLayerBBox(activeEl);
    if (!startBox) return;

    e.preventDefault();
    e.stopPropagation();
    captureHistorySnapshot();
    const before = captureHtmlSnapshot();
    const startClientX = e.clientX;
    const startClientY = e.clientY;
    const xScale = startBox.w / Math.max(1, rect.width);
    const yScale = startBox.h / Math.max(1, rect.height);
    let moved = false;

    const onMove = (event: PointerEvent) => {
      event.preventDefault();
      const dx = Math.round((event.clientX - startClientX) * xScale);
      const dy = Math.round((event.clientY - startClientY) * yScale);
      if (!moved && Math.abs(dx) + Math.abs(dy) < 2) return;
      moved = true;
      const next = {
        ...startBox,
        x: startBox.x + dx,
        y: startBox.y + dy,
      };
      writeLayerBBox(activeEl, next);
      const flowOffset = flowOffsetForBBox(activeEl, next);
      updateLayer(
        active.layer_id,
        { bbox: next, ...(flowOffset ? { flow_offset: flowOffset } : {}) },
        { history: false },
      );
      setRect(layerRectInParent(activeEl, iframe));
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      if (moved) rememberHtmlEdit(before);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  };

  const onSectionResizeStart = (handle: ResizeHandle, e: ReactPointerEvent<HTMLButtonElement>) => {
    if (!iframe || !active || active.kind !== "section" || !activeEl || !rect) return;
    const sectionId = sectionPatchId(activeEl);
    const startBox = readLayerBBox(activeEl);
    if (!sectionId || !startBox) return;

    e.preventDefault();
    e.stopPropagation();
    captureHistorySnapshot();
    const before = captureHtmlSnapshot();

    const startClientX = e.clientX;
    const startClientY = e.clientY;
    const xScale = startBox.w / Math.max(1, rect.width);
    const yScale = startBox.h / Math.max(1, rect.height);
    const bounds = sectionResizeBounds(activeEl, startBox);
    let latest = startBox;

    const onMove = (ev: PointerEvent) => {
      ev.preventDefault();
      const dx = Math.round((ev.clientX - startClientX) * xScale);
      const dy = Math.round((ev.clientY - startClientY) * yScale);
      const next = clampSectionBox(resizeBBox(startBox, handle, dx, dy, 1), bounds, startBox, handle);
      applySectionBox(activeEl, next);
      latest = next;
      setRect(layerRectInParent(activeEl, iframe));
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      const offset = flowOffsetForBBox(activeEl, latest);
      recordHtmlLayoutPatch({
        kind: "section_size",
        section_id: sectionId,
        width_px: latest.w,
        height_px: latest.h,
        ...(offset ? { offset_x_px: offset.dx, offset_y_px: offset.dy } : {}),
      });
      rememberHtmlEdit(before);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  };

  if (!active || !rect) return null;
  if (active.kind === "image") {
    return (
      <ImageResizeOverlay
        rect={rect}
        onResizeStart={onImageResizeStart}
        selectedAsset={selectedPaperAsset}
        onReplaceAsset={
          selectedPaperAsset ? () => replaceActiveImageWithAsset(selectedPaperAsset) : undefined
        }
        canUndo={canUndoHtml}
        canRedo={canRedoHtml}
        onUndo={undoHtmlEdit}
        onRedo={redoHtmlEdit}
        onDelete={deleteActiveDomObject}
      />
    );
  }
  if (active.kind === "section") {
    return (
      <SectionResizeOverlay
        rect={rect}
        onResizeStart={onSectionResizeStart}
        canUndo={canUndoHtml}
        canRedo={canRedoHtml}
        onUndo={undoHtmlEdit}
        onRedo={redoHtmlEdit}
        onDelete={deleteActiveDomObject}
      />
    );
  }
  if (active.kind !== "text") return null;
  return (
    <FloatingToolbar
      state={active}
      rect={rect}
      onPatch={onToolbarPatch}
      onDismiss={() => {
        setActive(null);
        setActiveEl(null);
        setRect(null);
      }}
      canUndo={canUndoHtml}
      canRedo={canRedoHtml}
      onUndo={undoHtmlEdit}
      onRedo={redoHtmlEdit}
      onDelete={deleteActiveDomObject}
      onMoveStart={onTextMoveStart}
    />
  );
}

function resizeBBox(
  start: Bbox,
  handle: ResizeHandle,
  dx: number,
  dy: number,
  minSize: number,
): Bbox {
  let x = start.x;
  let y = start.y;
  let w = start.w;
  let h = start.h;

  if (handle.includes("e")) w = start.w + dx;
  if (handle.includes("s")) h = start.h + dy;
  if (handle.includes("w")) {
    x = start.x + dx;
    w = start.w - dx;
  }
  if (handle.includes("n")) {
    y = start.y + dy;
    h = start.h - dy;
  }

  if (w < minSize) {
    if (handle.includes("w")) x = start.x + start.w - minSize;
    w = minSize;
  }
  if (h < minSize) {
    if (handle.includes("n")) y = start.y + start.h - minSize;
    h = minSize;
  }

  return {
    x: Math.round(x),
    y: Math.round(y),
    w: Math.round(w),
    h: Math.round(h),
  };
}

function findLayerElementForSelection(doc: Document, layerId: string): HTMLElement | null {
  const escaped = CSS.escape(layerId);
  const selectors = [
    `img[data-layer-id="${escaped}"]`,
    `[data-layer-id="${escaped}"][data-kind="image"]`,
    `[data-layer-id="${escaped}"][contenteditable="true"]`,
    `[data-layer-id="${escaped}"]`,
  ];
  for (const selector of selectors) {
    const el = doc.querySelector<HTMLElement>(selector);
    if (el) return el;
  }
  return null;
}

function sectionPatchId(el: HTMLElement): string | null {
  return el.getAttribute("data-block-id") || el.id || el.getAttribute("data-layer-id");
}

interface HtmlEditSnapshot {
  posterHtml: string;
  pendingEdits?: PendingEditsPayload;
}

interface HtmlEditHistory {
  past: HtmlEditSnapshot[];
  future: HtmlEditSnapshot[];
}

function clonePendingEdits(edits: PendingEditsPayload | undefined): PendingEditsPayload | undefined {
  if (!edits) return undefined;
  return JSON.parse(JSON.stringify(edits)) as PendingEditsPayload;
}

function cleanupEditorDom(root: HTMLElement): void {
  stripDeckNavigationState(root);
  root.querySelectorAll(
    ".da-section-grip,.od-flow-layout-handle,.od-flow-drop-marker",
  ).forEach((node) => node.remove());
  root.querySelectorAll(".ld-active,.od-flow-editable-section,.od-flow-dragging,.od-flow-resizing,.od-flow-drop-column,.od-paper-asset-drop-target")
    .forEach((node) => {
      node.classList.remove(
        "ld-active",
        "od-flow-editable-section",
        "od-flow-dragging",
        "od-flow-resizing",
        "od-flow-drop-column",
        "od-paper-asset-drop-target",
      );
    });
}

function inspectedLayerPatch(state: ToolbarLayerState): Partial<Layer> {
  const patch: Partial<Layer> = { text: state.text };
  if (state.font_family !== undefined) patch.font_family = state.font_family;
  if (state.font_size_px !== undefined) patch.font_size_px = state.font_size_px;
  if (state.font_weight !== undefined) patch.font_weight = state.font_weight;
  if (state.font_style !== undefined) patch.font_style = state.font_style;
  if (state.line_height !== undefined) patch.line_height = state.line_height;
  if (state.letter_spacing !== undefined) patch.letter_spacing = state.letter_spacing;
  if (state.text_transform !== undefined) patch.text_transform = state.text_transform;
  if (state.align !== undefined) patch.align = state.align;
  if (state.fill !== undefined) patch.effects = { fill: state.fill };
  return patch;
}

function deletableDomTarget(el: HTMLElement): HTMLElement | null {
  if (el.classList.contains("paper-poster")
    || el.classList.contains("poster-columns")
    || el.classList.contains("poster-column")
    || el.classList.contains("deck-slide")
    || el.hasAttribute("data-autodesign-artifact-root")) {
    return null;
  }
  if (!el.closest(".paper-poster, [data-autodesign-artifact-root]")) return null;
  return el;
}

function buildDomDeletePatch(el: HTMLElement, state: ToolbarLayerState): HtmlLayoutPatch {
  const targetId = state.layer_id || el.getAttribute("data-layer-id") || el.getAttribute("data-block-id") || el.id;
  const kind = normalizeDomDeleteKind(state.kind);
  return {
    kind: "dom_delete",
    ...(targetId ? { target_id: targetId } : {}),
    ...(kind ? { target_kind: kind } : {}),
    ...(el.getAttribute("data-block-id") ? { block_id: el.getAttribute("data-block-id") ?? undefined } : {}),
    ...(selectorForDomDelete(el) ? { selector: selectorForDomDelete(el) ?? undefined } : {}),
    ...(deleteLabel(el) ? { label: deleteLabel(el) ?? undefined } : {}),
  };
}

function normalizeDomDeleteKind(kind: string | undefined): "text" | "image" | "section" | "layer" | undefined {
  if (kind === "text" || kind === "image" || kind === "section" || kind === "layer") return kind;
  return undefined;
}

function selectorForDomDelete(el: HTMLElement): string | null {
  const layerId = el.getAttribute("data-layer-id");
  if (layerId) return `[data-layer-id="${CSS.escape(layerId)}"]`;
  const blockId = el.getAttribute("data-block-id");
  if (blockId) return `[data-block-id="${CSS.escape(blockId)}"]`;
  if (el.id) return `#${CSS.escape(el.id)}`;
  return null;
}

function deleteLabel(el: HTMLElement): string | null {
  const label = el.getAttribute("aria-label")
    || el.getAttribute("alt")
    || el.getAttribute("data-block-id")
    || el.textContent;
  const clean = (label ?? "").replace(/\s+/g, " ").trim();
  return clean ? clean.slice(0, 120) : null;
}

function isImageReplacementAsset(value: unknown): value is ImageReplacementAsset {
  const asset = value as Partial<ImageReplacementAsset> | null;
  return !!asset && typeof asset.url === "string" && asset.url.length > 0;
}

function imageElementForPaperAssetDrop(target: EventTarget | null): HTMLElement | null {
  const candidate = target as (EventTarget & {
    closest?: (selector: string) => HTMLElement | null;
    parentElement?: HTMLElement | null;
  }) | null;
  const el =
    typeof candidate?.closest === "function"
      ? candidate
      : candidate?.parentElement ?? null;
  if (!el || typeof el.closest !== "function") return null;
  return el.closest(
    'img[data-layer-id], [data-layer-id][data-kind="image"], [data-layer-id][role="img"]',
  ) as HTMLElement | null;
}

export function isIframeImageElement(value: unknown): value is HTMLImageElement {
  return (
    !!value
    && typeof value === "object"
    && String((value as { tagName?: unknown }).tagName ?? "").toUpperCase() === "IMG"
  );
}

function applyPaperAssetToImageElement(el: HTMLElement, url: string): void {
  if (isIframeImageElement(el)) {
    el.src = url;
    el.setAttribute("src", url);
    el.style.objectFit = "contain";
    el.style.objectPosition = "50% 50%";
    return;
  }
  el.style.backgroundImage = `url("${url.replace(/"/g, '\\"')}")`;
  el.style.backgroundSize = "contain";
  el.style.backgroundPosition = "50% 50%";
  el.style.backgroundRepeat = "no-repeat";
  el.setAttribute("data-src", url);
}

function sectionResizeBounds(el: HTMLElement, startBox: Bbox): {
  minW: number;
  maxW: number;
  minH: number;
  maxH: number;
} {
  const parent = el.parentElement;
  const parentBox = parent ? readLayerBBox(parent) : null;
  return {
    minW: 120,
    maxW: Math.max(120, parentBox?.w ?? startBox.w),
    minH: 80,
    maxH: 1536,
  };
}

function clampSectionBox(
  box: Bbox,
  bounds: { minW: number; maxW: number; minH: number; maxH: number },
  start: Bbox,
  handle: ResizeHandle,
): Bbox {
  const w = Math.round(Math.max(bounds.minW, Math.min(bounds.maxW, box.w)));
  const h = Math.round(Math.max(bounds.minH, Math.min(bounds.maxH, box.h)));
  return {
    ...box,
    x: handle.includes("w") ? Math.round(start.x + start.w - w) : box.x,
    y: handle.includes("n") ? Math.round(start.y + start.h - h) : box.y,
    w,
    h,
  };
}

function applySectionBox(el: HTMLElement, box: Bbox): void {
  const offset = flowOffsetForBBox(el, box);
  el.style.boxSizing = "border-box";
  el.style.width = `${box.w}px`;
  el.style.height = `${box.h}px`;
  el.style.minHeight = `${box.h}px`;
  if (offset) {
    el.style.position = "relative";
    el.style.left = `${offset.dx}px`;
    el.style.top = `${offset.dy}px`;
    el.setAttribute("data-layout-offset-x-px", String(offset.dx));
    el.setAttribute("data-layout-offset-y-px", String(offset.dy));
  }
  el.setAttribute("data-layout-width-px", String(box.w));
  el.setAttribute("data-layout-height-px", String(box.h));
}

function installSectionGrips(
  doc: Document,
  activateLayer: (el: HTMLElement) => void,
): Array<() => void> {
  const detachers: Array<() => void> = [];
  doc
    .querySelectorAll<HTMLElement>(
      ".paper-poster .poster-section[data-layer-id], .paper-poster .poster-header[data-layer-id]",
    )
    .forEach((section) => {
      if (section.querySelector(":scope > .da-section-grip")) return;
      const grip = doc.createElement("button");
      grip.type = "button";
      grip.className = "da-section-grip";
      grip.setAttribute("aria-label", "Select panel box");
      grip.setAttribute("title", "Select panel box");
      grip.textContent = "BOX";
      if (!section.style.position) section.style.position = "relative";
      section.appendChild(grip);

      const onPointerDown = (e: PointerEvent) => {
        if (e.button !== 0) return;
        e.preventDefault();
        e.stopPropagation();
        activateLayer(section);
      };
      grip.addEventListener("pointerdown", onPointerDown);
      detachers.push(() => {
        grip.removeEventListener("pointerdown", onPointerDown);
        grip.remove();
      });
    });
  return detachers;
}

function ImageResizeOverlay({
  rect,
  onResizeStart,
  selectedAsset,
  onReplaceAsset,
  canUndo,
  canRedo,
  onUndo,
  onRedo,
  onDelete,
}: {
  rect: LayerRect;
  onResizeStart: (handle: ResizeHandle, e: ReactPointerEvent<HTMLButtonElement>) => void;
  selectedAsset?: ArtifactAsset | null;
  onReplaceAsset?: () => void;
  canUndo: boolean;
  canRedo: boolean;
  onUndo: () => void;
  onRedo: () => void;
  onDelete: () => void;
}) {
  const handles: Array<{
    id: ResizeHandle;
    position: CSSProperties;
    cursor: string;
  }> = [
    { id: "nw", position: { left: -18, top: -18 }, cursor: "nwse-resize" },
    { id: "ne", position: { right: -18, top: -18 }, cursor: "nesw-resize" },
    { id: "sw", position: { left: -18, bottom: -18 }, cursor: "nesw-resize" },
    { id: "se", position: { right: -18, bottom: -18 }, cursor: "nwse-resize" },
  ];
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  return (
    <div
      aria-label="Selected image resize controls"
      style={{
        position: "fixed",
        top: rect.top,
        left: rect.left,
        width: rect.width,
        height: rect.height,
        zIndex: 58,
        pointerEvents: "none",
      }}
      className="border-2 border-sky-500 bg-sky-200/10 shadow-[0_0_0_4px_rgba(255,255,255,0.92),0_12px_40px_rgba(14,116,144,0.22)]"
    >
      <ObjectActionBar
        tone="sky"
        canUndo={canUndo}
        canRedo={canRedo}
        onUndo={onUndo}
        onRedo={onRedo}
        onDelete={onDelete}
      />
      {selectedAsset && onReplaceAsset && (
        <button
          type="button"
          title={selectedAsset.name}
          onClick={onReplaceAsset}
          style={{ pointerEvents: "auto" }}
          className="absolute bottom-0 right-0 flex translate-y-[calc(100%+10px)] items-center gap-1 rounded-md border border-sky-300/80 bg-ink-900 px-2.5 py-1.5 text-[10px] font-medium uppercase text-ink-50 shadow-page transition hover:bg-ink-700"
        >
          <I.Image width={12} height={12} />
          <span>{t("Replace")}</span>
        </button>
      )}
      {handles.map((handle) => (
        <button
          key={handle.id}
          type="button"
          aria-label={`Resize image ${handle.id}`}
          title="Drag to resize image"
          onPointerDown={(e) => onResizeStart(handle.id, e)}
          onDragStart={(e) => e.preventDefault()}
          style={{
            ...handle.position,
            cursor: handle.cursor,
            pointerEvents: "auto",
            touchAction: "none",
          }}
          className="group absolute flex h-9 w-9 items-center justify-center rounded-full bg-sky-500/0"
        >
          <span className="h-4 w-4 rounded-full border-2 border-white bg-sky-500 shadow-md ring-4 ring-sky-500/20 transition-transform group-hover:scale-125 group-active:scale-110" />
        </button>
      ))}
    </div>
  );
}

function SectionResizeOverlay({
  rect,
  onResizeStart,
  canUndo,
  canRedo,
  onUndo,
  onRedo,
  onDelete,
}: {
  rect: LayerRect;
  onResizeStart: (handle: ResizeHandle, e: ReactPointerEvent<HTMLButtonElement>) => void;
  canUndo: boolean;
  canRedo: boolean;
  onUndo: () => void;
  onRedo: () => void;
  onDelete: () => void;
}) {
  const handles: Array<{
    id: ResizeHandle;
    position: CSSProperties;
    className: string;
    cursor: string;
    label: string;
  }> = [
    {
      id: "nw",
      position: { left: -16, top: -16 },
      className: "h-10 w-10",
      cursor: "nwse-resize",
      label: "Resize section up and left",
    },
    {
      id: "n",
      position: { left: "50%", top: -14, transform: "translateX(-50%)" },
      className: "h-7 w-16",
      cursor: "ns-resize",
      label: "Resize section upward",
    },
    {
      id: "ne",
      position: { right: -16, top: -16 },
      className: "h-10 w-10",
      cursor: "nesw-resize",
      label: "Resize section up and right",
    },
    {
      id: "w",
      position: { left: -14, top: "50%", transform: "translateY(-50%)" },
      className: "h-16 w-7",
      cursor: "ew-resize",
      label: "Resize section left",
    },
    {
      id: "e",
      position: { right: -14, top: "50%", transform: "translateY(-50%)" },
      className: "h-16 w-7",
      cursor: "ew-resize",
      label: "Resize section width",
    },
    {
      id: "s",
      position: { left: "50%", bottom: -14, transform: "translateX(-50%)" },
      className: "h-7 w-16",
      cursor: "ns-resize",
      label: "Resize section height",
    },
    {
      id: "sw",
      position: { left: -16, bottom: -16 },
      className: "h-10 w-10",
      cursor: "nesw-resize",
      label: "Resize section down and left",
    },
    {
      id: "se",
      position: { right: -16, bottom: -16 },
      className: "h-10 w-10",
      cursor: "nwse-resize",
      label: "Resize section",
    },
  ];
  return (
    <div
      aria-label="Selected section resize controls"
      style={{
        position: "fixed",
        top: rect.top,
        left: rect.left,
        width: rect.width,
        height: rect.height,
        zIndex: 57,
        pointerEvents: "none",
      }}
      className="border-2 border-amber-500 bg-amber-200/10 shadow-[0_0_0_4px_rgba(255,255,255,0.92),0_12px_36px_rgba(217,119,6,0.20)]"
    >
      <ObjectActionBar
        tone="amber"
        canUndo={canUndo}
        canRedo={canRedo}
        onUndo={onUndo}
        onRedo={onRedo}
        onDelete={onDelete}
      />
      {handles.map((handle) => (
        <button
          key={handle.id}
          type="button"
          aria-label={handle.label}
          title={handle.label}
          onPointerDown={(e) => onResizeStart(handle.id, e)}
          onDragStart={(e) => e.preventDefault()}
          style={{
            ...handle.position,
            cursor: handle.cursor,
            pointerEvents: "auto",
            touchAction: "none",
          }}
          className={`group absolute flex items-center justify-center rounded-full bg-amber-500/0 ${handle.className}`}
        >
          <span className="h-4 w-4 rounded-full border-2 border-white bg-amber-500 shadow-md ring-4 ring-amber-500/20 transition-transform group-hover:scale-125 group-active:scale-110" />
        </button>
      ))}
    </div>
  );
}

function ObjectActionBar({
  tone,
  canUndo,
  canRedo,
  onUndo,
  onRedo,
  onDelete,
}: {
  tone: "sky" | "amber";
  canUndo: boolean;
  canRedo: boolean;
  onUndo: () => void;
  onRedo: () => void;
  onDelete: () => void;
}) {
  const toneClasses = tone === "sky"
    ? "border-sky-300/70 text-sky-800"
    : "border-amber-300/70 text-amber-800";
  return (
    <div
      className={`absolute left-1/2 top-0 flex -translate-x-1/2 -translate-y-[calc(100%+10px)] items-center gap-1 rounded-md border bg-paper px-1.5 py-1 shadow-page ${toneClasses}`}
      style={{ pointerEvents: "auto" }}
      onMouseDown={(e) => e.preventDefault()}
    >
      <button
        type="button"
        title="Undo edit"
        disabled={!canUndo}
        onClick={onUndo}
        className="rounded px-1.5 py-1 transition hover:bg-surface-raised disabled:cursor-not-allowed disabled:opacity-35"
      >
        <I.Undo width={12} height={12} />
      </button>
      <button
        type="button"
        title="Redo edit"
        disabled={!canRedo}
        onClick={onRedo}
        className="rounded px-1.5 py-1 transition hover:bg-surface-raised disabled:cursor-not-allowed disabled:opacity-35"
      >
        <I.Redo width={12} height={12} />
      </button>
      <span className="mx-0.5 h-4 w-px bg-ink-200" />
      <button
        type="button"
        title="Delete DOM object"
        onClick={onDelete}
        className="rounded px-1.5 py-1 text-red-600 transition hover:bg-red-50"
      >
        <I.Trash width={12} height={12} />
      </button>
    </div>
  );
}

function injectSelectionStyles(doc: Document, scale: number) {
  if (!doc.head) return;
  const safeScale = Math.max(0.02, scale || 1);
  const stroke = Math.round(Math.max(4, Math.min(28, 2.8 / safeScale)));
  const hoverStroke = Math.round(Math.max(3, Math.min(18, 1.8 / safeScale)));
  const offset = Math.round(Math.max(4, Math.min(24, 3.5 / safeScale)));
  const halo = Math.round(Math.max(7, Math.min(34, 5 / safeScale)));
  const styleId = "autodesign-web-selection-style";
  let style = findInjectedStyle(
    doc,
    styleId,
    "designanything-web-selection-style",
  );
  if (!style) {
    style = doc.createElement("style");
    style.id = styleId;
    doc.head.appendChild(style);
  }
  style.textContent = `
    .layer[data-layer-id],
    .od-layer[data-layer-id],
    .paper-poster [data-layer-id] {
      transition: outline-color 120ms ease, box-shadow 120ms ease, background-color 120ms ease;
    }
    .paper-poster .da-section-grip {
      all: unset;
      position: absolute;
      top: 6px;
      right: 8px;
      z-index: 1000;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 72px;
      height: 28px;
      border: 2px solid rgba(217, 119, 6, 0.82);
      border-radius: 6px;
      background: rgba(255, 251, 235, 0.96);
      color: #92400e;
      cursor: pointer;
      font: 700 14px/1 Arial, sans-serif;
      letter-spacing: 0;
      opacity: 0.72;
      box-shadow: 0 4px 14px rgba(120, 53, 15, 0.18);
    }
    .paper-poster .da-section-grip:hover,
    .paper-poster .da-section-grip:focus-visible {
      opacity: 1;
      background: #f59e0b;
      color: #ffffff;
      outline: none;
    }
    .layer[data-layer-id]:hover,
    .od-layer[data-layer-id]:hover,
    .paper-poster [data-layer-id]:hover {
      outline: ${hoverStroke}px dashed rgba(14, 165, 233, 0.75) !important;
      outline-offset: ${offset}px !important;
    }
    .layer[data-layer-id].ld-active,
    .od-layer[data-layer-id].ld-active,
    .paper-poster [data-layer-id].ld-active {
      outline: ${stroke}px solid #0ea5e9 !important;
      outline-offset: ${offset}px !important;
      box-shadow:
        0 0 0 ${halo}px rgba(255, 255, 255, 0.98),
        0 0 0 ${halo + stroke}px rgba(14, 165, 233, 0.38),
        0 20px 80px rgba(3, 105, 161, 0.22) !important;
      z-index: 999 !important;
    }
    .paper-poster .od-paper-asset-drop-target {
      outline: ${stroke}px solid #0284c7 !important;
      outline-offset: ${offset}px !important;
      box-shadow:
        0 0 0 ${halo}px rgba(255, 255, 255, 0.98),
        0 0 0 ${halo + stroke}px rgba(2, 132, 199, 0.45),
        0 20px 80px rgba(2, 132, 199, 0.24) !important;
      z-index: 1000 !important;
    }
    .layer.text[data-layer-id].ld-active,
    .od-layer[data-layer-id].ld-active,
    .paper-poster [data-layer-id].ld-active {
      background-color: rgba(14, 165, 233, 0.08) !important;
    }
  `;
}
