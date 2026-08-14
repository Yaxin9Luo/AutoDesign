/**
 * Invisible-until-hover drag gutter for resizing a panel along one edge.
 *
 * Sits absolute on `side` of the parent (which must be `position:
 * relative`). On mousedown captures the panel's start size + cursor
 * coord; mousemove deltas drive `setSize`. The store action is expected
 * to clamp to its own min/max — this component is not opinionated about
 * limits.
 *
 * Iframe-mouse-steal is the classic trap with this kind of UI: when the
 * cursor enters an iframe mid-drag, the parent stops getting events. We
 * mitigate by rendering a viewport-spanning transparent overlay during
 * drag — it absorbs all mouse events, so iframes inside the canvas
 * never see them.
 *
 * Direction → axis mapping:
 *   side="left"  | "right"  → horizontal drag, resizes WIDTH
 *   side="top"   | "bottom" → vertical drag,   resizes HEIGHT
 */

import { useEffect, useRef, useState } from "react";

type Side = "left" | "right" | "top" | "bottom";

interface Props {
  side: Side;
  /** Current size in px — captured on mousedown to compute the new
   *  size from the drag delta without race conditions. */
  getCurrentSize: () => number;
  /** Called continuously during drag with the proposed new size in px.
   *  The store action should clamp to its own min/max. */
  setSize: (px: number) => void;
}

const isHorizontal = (s: Side) => s === "left" || s === "right";

export function ResizeHandle({ side, getCurrentSize, setSize }: Props) {
  const [dragging, setDragging] = useState(false);
  const start = useRef({ coord: 0, size: 0 });

  useEffect(() => {
    if (!dragging) return;
    const onMove = (e: MouseEvent) => {
      const horiz = isHorizontal(side);
      const raw = horiz
        ? e.clientX - start.current.coord
        : e.clientY - start.current.coord;
      // "right"/"bottom" handles grow when cursor moves AWAY from origin;
      // "left"/"top" grow when cursor moves TOWARD origin → flip the sign.
      const sign = side === "right" || side === "bottom" ? 1 : -1;
      setSize(start.current.size + raw * sign);
    };
    const stop = () => setDragging(false);
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", stop);
    window.addEventListener("blur", stop);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", stop);
      window.removeEventListener("blur", stop);
    };
  }, [dragging, side, setSize]);

  const horiz = isHorizontal(side);
  const cursorCls = horiz ? "cursor-col-resize" : "cursor-row-resize";

  // Hit-target: 6px-wide gutter offset 3px past the panel edge so the
  // hot zone straddles the border line.
  const positionCls = {
    left: "top-0 left-0 -ml-[3px] h-full w-1.5",
    right: "top-0 right-0 -mr-[3px] h-full w-1.5",
    top: "left-0 top-0 -mt-[3px] h-1.5 w-full",
    bottom: "left-0 bottom-0 -mb-[3px] h-1.5 w-full",
  }[side];

  // Visible 1px line — same edge as the hit zone, hover or drag tinted.
  const linePosCls = {
    left: "top-0 left-[3px] h-full w-px",
    right: "top-0 right-[3px] h-full w-px",
    top: "left-0 top-[3px] w-full h-px",
    bottom: "left-0 bottom-[3px] w-full h-px",
  }[side];

  return (
    <>
      <div
        role="separator"
        aria-orientation={horiz ? "vertical" : "horizontal"}
        onMouseDown={(e) => {
          if (e.button !== 0) return;
          e.preventDefault();
          start.current = {
            coord: horiz ? e.clientX : e.clientY,
            size: getCurrentSize(),
          };
          setDragging(true);
        }}
        title="Drag to resize"
        className={`group absolute z-30 ${cursorCls} ${positionCls}`}
        style={{ touchAction: "none" }}
      >
        <div
          className={`pointer-events-none absolute transition-colors ${linePosCls} ${
            dragging
              ? "bg-accent"
              : "bg-transparent group-hover:bg-ink-400"
          }`}
        />
      </div>

      {/* Viewport-wide overlay during drag — kills iframe mouse-steal +
          locks cursor to the right resize cursor across the page. */}
      {dragging && (
        <div
          className={`fixed inset-0 z-[100] ${cursorCls}`}
          style={{ background: "transparent" }}
        />
      )}
    </>
  );
}
