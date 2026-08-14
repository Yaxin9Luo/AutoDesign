import { useEffect } from "react";
import { useApp } from "@/lib/store";
import type { HtmlLayoutPatch } from "@/lib/types";
import { findInjectedStyle } from "./styleTagCompatibility";

interface Props {
  iframe: HTMLIFrameElement | null;
  scale?: number;
}

const MIN_SECTION_HEIGHT = 80;
const MIN_COLUMN_PERCENT = 22;

export function HtmlFlowLayoutEditor({ iframe, scale = 1 }: Props) {
  const recordHtmlLayoutPatch = useApp((s) => s.recordHtmlLayoutPatch);

  useEffect(() => {
    if (!iframe) return;
    let cleanup: (() => void) | null = null;

    const wire = () => {
      if (cleanup) cleanup();
      const doc = iframe.contentDocument;
      const win = iframe.contentWindow;
      if (!doc || !win) return;
      const root = doc.querySelector<HTMLElement>(".paper-poster");
      const columnsRoot = root?.querySelector<HTMLElement>(".poster-columns");
      if (!root || !columnsRoot) return;

      injectFlowEditorStyles(doc, scale);
      doc.querySelectorAll(".od-flow-layout-handle,.od-flow-drop-marker").forEach((node) => {
        node.remove();
      });

      const detachers: Array<() => void> = [];
      const columns = directColumnChildren(columnsRoot);
      const sections = columns.flatMap((column) => directSectionChildren(column));
      let draggingSection: HTMLElement | null = null;
      let activeDropMarker: HTMLElement | null = null;

      const clearDropMarker = () => {
        activeDropMarker?.remove();
        activeDropMarker = null;
        columns.forEach((column) => column.classList.remove("od-flow-drop-column"));
      };

      const recordSectionOrder = () => {
        const patch: HtmlLayoutPatch = {
          kind: "section_order",
          columns: columns.map((column, idx) => ({
            column_id: columnId(column, idx),
            section_ids: directSectionChildren(column)
              .map(sectionId)
              .filter((id): id is string => !!id),
          })),
        };
        recordHtmlLayoutPatch(patch);
      };

      const dropIntoColumn = (column: HTMLElement, before: HTMLElement | null) => {
        if (!draggingSection || !columns.includes(column)) return;
        if (before === draggingSection) return;
        column.insertBefore(draggingSection, before);
        recordSectionOrder();
        clearDropMarker();
      };

      sections.forEach((section) => {
        const id = sectionId(section);
        if (!id) return;
        section.classList.add("od-flow-editable-section");
        if (getComputedStyle(section).position === "static") {
          section.style.position = "relative";
        }

        const dragHandle = doc.createElement("div");
        dragHandle.className = "od-flow-layout-handle od-flow-drag-handle";
        dragHandle.draggable = true;
        dragHandle.title = "Drag section";
        dragHandle.textContent = "⋮⋮";
        section.prepend(dragHandle);

        const dragStart = (event: DragEvent) => {
          draggingSection = section;
          section.classList.add("od-flow-dragging");
          event.stopPropagation();
          event.dataTransfer?.setData("text/plain", id);
          if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
        };
        const dragEnd = () => {
          section.classList.remove("od-flow-dragging");
          draggingSection = null;
          clearDropMarker();
        };
        const dragOver = (event: DragEvent) => {
          if (!draggingSection || draggingSection === section) return;
          event.preventDefault();
          event.stopPropagation();
          const rect = section.getBoundingClientRect();
          const before = event.clientY < rect.top + rect.height / 2;
          showDropMarker(doc, section, before);
          activeDropMarker = doc.querySelector<HTMLElement>(".od-flow-drop-marker");
        };
        const drop = (event: DragEvent) => {
          if (!draggingSection || draggingSection === section) return;
          event.preventDefault();
          event.stopPropagation();
          const rect = section.getBoundingClientRect();
          const before = event.clientY < rect.top + rect.height / 2;
          const column = section.parentElement as HTMLElement | null;
          dropIntoColumn(column!, before ? section : section.nextElementSibling as HTMLElement | null);
        };

        dragHandle.addEventListener("dragstart", dragStart);
        dragHandle.addEventListener("dragend", dragEnd);
        section.addEventListener("dragover", dragOver);
        section.addEventListener("drop", drop);
        detachers.push(() => {
          dragHandle.removeEventListener("dragstart", dragStart);
          dragHandle.removeEventListener("dragend", dragEnd);
          section.removeEventListener("dragover", dragOver);
          section.removeEventListener("drop", drop);
        });

        const resizeHandle = doc.createElement("div");
        resizeHandle.className = "od-flow-layout-handle od-flow-section-resize";
        resizeHandle.title = "Resize section height";
        section.appendChild(resizeHandle);

        const resizeDown = (event: PointerEvent) => {
          if (event.button !== 0) return;
          event.preventDefault();
          event.stopPropagation();
          const startY = event.clientY;
          const startHeight = section.getBoundingClientRect().height;
          section.classList.add("od-flow-resizing");

          const onMove = (moveEvent: PointerEvent) => {
            const next = Math.max(
              MIN_SECTION_HEIGHT,
              Math.round(startHeight + moveEvent.clientY - startY),
            );
            section.style.height = `${next}px`;
            section.style.minHeight = `${next}px`;
          };
          const onUp = () => {
            win.removeEventListener("pointermove", onMove);
            win.removeEventListener("pointerup", onUp);
            section.classList.remove("od-flow-resizing");
            const next = Math.max(MIN_SECTION_HEIGHT, Math.round(section.getBoundingClientRect().height));
            recordHtmlLayoutPatch({ kind: "section_height", section_id: id, height_px: next });
          };
          win.addEventListener("pointermove", onMove);
          win.addEventListener("pointerup", onUp, { once: true });
        };
        resizeHandle.addEventListener("pointerdown", resizeDown);
        detachers.push(() => resizeHandle.removeEventListener("pointerdown", resizeDown));
      });

      columns.forEach((column) => {
        const dragOver = (event: DragEvent) => {
          if (!draggingSection) return;
          event.preventDefault();
          event.stopPropagation();
          column.classList.add("od-flow-drop-column");
        };
        const dragLeave = (event: DragEvent) => {
          if (event.relatedTarget instanceof Node && column.contains(event.relatedTarget)) return;
          column.classList.remove("od-flow-drop-column");
        };
        const drop = (event: DragEvent) => {
          if (!draggingSection) return;
          event.preventDefault();
          event.stopPropagation();
          dropIntoColumn(column, null);
        };
        column.addEventListener("dragover", dragOver);
        column.addEventListener("dragleave", dragLeave);
        column.addEventListener("drop", drop);
        detachers.push(() => {
          column.removeEventListener("dragover", dragOver);
          column.removeEventListener("dragleave", dragLeave);
          column.removeEventListener("drop", drop);
        });
      });

      const columnHandles = installColumnResizeHandles({
        doc,
        win,
        columnsRoot,
        columns,
        recordHtmlLayoutPatch,
      });
      detachers.push(columnHandles);

      cleanup = () => {
        detachers.forEach((detach) => detach());
        doc.querySelectorAll(".od-flow-layout-handle,.od-flow-drop-marker").forEach((node) => {
          node.remove();
        });
        sections.forEach((section) => {
          section.classList.remove("od-flow-editable-section", "od-flow-dragging", "od-flow-resizing");
        });
        columns.forEach((column) => column.classList.remove("od-flow-drop-column"));
      };
    };

    iframe.addEventListener("load", wire);
    if (iframe.contentDocument?.readyState === "complete") wire();
    return () => {
      iframe.removeEventListener("load", wire);
      if (cleanup) cleanup();
    };
  }, [iframe, recordHtmlLayoutPatch, scale]);

  return null;
}

function directColumnChildren(columnsRoot: HTMLElement): HTMLElement[] {
  return Array.from(columnsRoot.children).filter(
    (child): child is HTMLElement =>
      child instanceof HTMLElement
      && (child.classList.contains("poster-column") || child.hasAttribute("data-column-id")),
  );
}

function directSectionChildren(column: HTMLElement): HTMLElement[] {
  return Array.from(column.children).filter(
    (child): child is HTMLElement =>
      child instanceof HTMLElement
      && child.classList.contains("poster-section"),
  );
}

function sectionId(section: HTMLElement): string | null {
  return section.getAttribute("data-block-id")
    || section.getAttribute("data-layer-id")
    || section.id
    || null;
}

function columnId(column: HTMLElement, idx: number): string {
  return column.getAttribute("data-column-id")
    || column.getAttribute("data-block-id")
    || column.id
    || `column_${idx + 1}`;
}

function columnsId(columnsRoot: HTMLElement): string {
  return columnsRoot.getAttribute("data-block-id")
    || columnsRoot.getAttribute("data-layout-region")
    || columnsRoot.id
    || "poster-columns";
}

function showDropMarker(doc: Document, section: HTMLElement, before: boolean) {
  doc.querySelectorAll(".od-flow-drop-marker").forEach((node) => node.remove());
  const marker = doc.createElement("div");
  marker.className = "od-flow-drop-marker";
  if (before) section.before(marker);
  else section.after(marker);
}

function installColumnResizeHandles({
  doc,
  win,
  columnsRoot,
  columns,
  recordHtmlLayoutPatch,
}: {
  doc: Document;
  win: Window;
  columnsRoot: HTMLElement;
  columns: HTMLElement[];
  recordHtmlLayoutPatch: (patch: HtmlLayoutPatch) => void;
}) {
  if (columns.length !== 3) return () => {};
  if (getComputedStyle(columnsRoot).position === "static") {
    columnsRoot.style.position = "relative";
  }

  const handles = [0, 1].map((idx) => {
    const handle = doc.createElement("div");
    handle.className = "od-flow-layout-handle od-flow-column-resize";
    handle.title = "Resize columns";
    columnsRoot.appendChild(handle);
    return { idx, handle };
  });

  const updateHandlePositions = () => {
    const rootRect = columnsRoot.getBoundingClientRect();
    handles.forEach(({ idx, handle }) => {
      const colRect = columns[idx].getBoundingClientRect();
      handle.style.left = `${Math.round(colRect.right - rootRect.left - 6)}px`;
    });
  };
  updateHandlePositions();
  const resizeObserver = new ResizeObserver(updateHandlePositions);
  resizeObserver.observe(columnsRoot);
  columns.forEach((column) => resizeObserver.observe(column));

  const detachers: Array<() => void> = [() => resizeObserver.disconnect()];

  handles.forEach(({ idx, handle }) => {
    const onDown = (event: PointerEvent) => {
      if (event.button !== 0) return;
      event.preventDefault();
      event.stopPropagation();
      const rootWidth = Math.max(1, columnsRoot.getBoundingClientRect().width);
      const startX = event.clientX;
      const start = columns.map((column) => (column.getBoundingClientRect().width / rootWidth) * 100);
      columnsRoot.classList.add("od-flow-resizing");

      const onMove = (moveEvent: PointerEvent) => {
        const deltaPct = ((moveEvent.clientX - startX) / rootWidth) * 100;
        const next = [...start];
        const maxDelta = start[idx + 1] - MIN_COLUMN_PERCENT;
        const minDelta = MIN_COLUMN_PERCENT - start[idx];
        const clamped = Math.max(minDelta, Math.min(maxDelta, deltaPct));
        next[idx] = start[idx] + clamped;
        next[idx + 1] = start[idx + 1] - clamped;
        applyColumnWidths(columnsRoot, next);
        updateHandlePositions();
      };
      const onUp = () => {
        win.removeEventListener("pointermove", onMove);
        win.removeEventListener("pointerup", onUp);
        columnsRoot.classList.remove("od-flow-resizing");
        const rootWidthNow = Math.max(1, columnsRoot.getBoundingClientRect().width);
        const widths = columns.map((column) =>
          Number(((column.getBoundingClientRect().width / rootWidthNow) * 100).toFixed(2)),
        );
        recordHtmlLayoutPatch({
          kind: "column_widths",
          columns_id: columnsId(columnsRoot),
          widths,
        });
      };
      win.addEventListener("pointermove", onMove);
      win.addEventListener("pointerup", onUp, { once: true });
    };
    handle.addEventListener("pointerdown", onDown);
    detachers.push(() => handle.removeEventListener("pointerdown", onDown));
  });

  return () => {
    detachers.forEach((detach) => detach());
    handles.forEach(({ handle }) => handle.remove());
    columnsRoot.classList.remove("od-flow-resizing");
  };
}

function applyColumnWidths(columnsRoot: HTMLElement, widths: number[]) {
  columnsRoot.style.display = "grid";
  columnsRoot.style.gridTemplateColumns = widths.map((width) => `${width.toFixed(2)}%`).join(" ");
}

function injectFlowEditorStyles(doc: Document, scale: number) {
  const safeScale = Math.max(0.02, scale || 1);
  const stroke = Math.round(Math.max(2, Math.min(12, 2 / safeScale)));
  const handle = Math.round(Math.max(8, Math.min(28, 8 / safeScale)));
  const styleId = "autodesign-web-flow-layout-editor";
  let style = findInjectedStyle(
    doc,
    styleId,
    "designanything-web-flow-layout-editor",
  );
  if (!style) {
    style = doc.createElement("style");
    style.id = styleId;
    doc.head.appendChild(style);
  }
  style.textContent = `
    .paper-poster .od-flow-editable-section:hover {
      outline: ${stroke}px dashed rgba(14, 165, 233, 0.58) !important;
      outline-offset: ${Math.max(2, Math.round(2 / safeScale))}px !important;
    }
    .od-flow-layout-handle {
      box-sizing: border-box;
      font-family: Arial, Helvetica, sans-serif;
      user-select: none;
      -webkit-user-select: none;
    }
    .od-flow-drag-handle {
      position: absolute;
      right: ${Math.round(5 / safeScale)}px;
      top: ${Math.round(5 / safeScale)}px;
      z-index: 1000;
      width: ${handle}px;
      height: ${handle}px;
      display: flex;
      align-items: center;
      justify-content: center;
      border: 1px solid rgba(14, 165, 233, 0.5);
      border-radius: ${Math.max(2, Math.round(3 / safeScale))}px;
      background: rgba(255, 255, 255, 0.94);
      color: #0369a1;
      cursor: grab;
      font-size: ${Math.max(9, Math.round(11 / safeScale))}px;
      line-height: 1;
      opacity: 0;
      transition: opacity 120ms ease;
    }
    .od-flow-editable-section:hover > .od-flow-drag-handle,
    .od-flow-drag-handle:hover,
    .od-flow-dragging > .od-flow-drag-handle {
      opacity: 1;
    }
    .od-flow-section-resize {
      position: absolute;
      left: 0;
      right: 0;
      bottom: ${Math.round(-5 / safeScale)}px;
      z-index: 999;
      height: ${Math.max(8, Math.round(10 / safeScale))}px;
      cursor: row-resize;
    }
    .od-flow-section-resize::after {
      content: "";
      position: absolute;
      left: 12%;
      right: 12%;
      top: 50%;
      height: ${Math.max(1, Math.round(1 / safeScale))}px;
      background: rgba(14, 165, 233, 0);
      transition: background 120ms ease;
    }
    .od-flow-section-resize:hover::after,
    .od-flow-resizing > .od-flow-section-resize::after {
      background: rgba(14, 165, 233, 0.85);
    }
    .od-flow-column-resize {
      position: absolute;
      top: 0;
      bottom: 0;
      z-index: 1001;
      width: ${Math.max(10, Math.round(12 / safeScale))}px;
      cursor: col-resize;
    }
    .od-flow-column-resize::after {
      content: "";
      position: absolute;
      top: 4%;
      bottom: 4%;
      left: 50%;
      width: ${Math.max(1, Math.round(1 / safeScale))}px;
      background: rgba(14, 165, 233, 0);
      transition: background 120ms ease;
    }
    .od-flow-column-resize:hover::after,
    .poster-columns.od-flow-resizing .od-flow-column-resize::after {
      background: rgba(14, 165, 233, 0.85);
    }
    .od-flow-drop-marker {
      height: ${Math.max(8, Math.round(10 / safeScale))}px;
      margin: ${Math.max(2, Math.round(3 / safeScale))}px 0;
      border-radius: 999px;
      background: rgba(14, 165, 233, 0.82);
      box-shadow: 0 0 0 ${Math.max(2, Math.round(3 / safeScale))}px rgba(14, 165, 233, 0.14);
    }
    .od-flow-drop-column {
      outline: ${stroke}px solid rgba(14, 165, 233, 0.22) !important;
      outline-offset: ${Math.max(2, Math.round(3 / safeScale))}px !important;
    }
    .od-flow-dragging {
      opacity: 0.45;
    }
  `;
}
