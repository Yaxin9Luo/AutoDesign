/**
 * Floating toolbar that anchors above the active text layer. Mounted
 * in the parent React tree (NOT inside the iframe) so it floats above
 * the iframe and isn't clipped by it. v1 keeps the surface narrow:
 * font size + color + alignment. Full typography controls live in the
 * sidebar so this surface stays compact.
 */
import {
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { translate } from "@/lib/i18n";
import { useApp } from "@/lib/store";
import type { Align } from "@/lib/types";
import type { LayerRect, ToolbarLayerState } from "@/lib/iframe_bridge";
import { I } from "../icons";

const FONT_SIZES: number[] = [12, 14, 16, 18, 20, 24, 28, 32, 40, 48, 60, 72, 96, 120];
const PRESET_COLORS: string[] = [
  "#1A1A1A", "#444444", "#7A7A7A", "#FFFFFF",
  "#2C3E50", "#1F6F4A", "#9C2A2A", "#6F4A1F",
];

interface Props {
  state: ToolbarLayerState;
  rect: LayerRect;
  onPatch: (patch: { font_size_px?: number; fill?: string; align?: Align }) => void;
  onDismiss: () => void;
  canUndo?: boolean;
  canRedo?: boolean;
  onUndo?: () => void;
  onRedo?: () => void;
  onDelete?: () => void;
  onMoveStart?: (event: ReactPointerEvent<HTMLButtonElement>) => void;
}

export function FloatingToolbar({
  state,
  rect,
  onPatch,
  onDismiss,
  canUndo = false,
  canRedo = false,
  onUndo,
  onRedo,
  onDelete,
  onMoveStart,
}: Props) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [openMenu, setOpenMenu] = useState<"size" | "color" | null>(null);
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);

  // Position: 12 px above the layer's top edge, horizontally centered
  // on the layer (clamped to viewport). If the layer is near the top
  // of the screen, flip below.
  const TOOLBAR_W = 420; // approximate; CSS clamps actual width
  const TOOLBAR_H = 36;
  const above_y = rect.top - TOOLBAR_H - 12;
  const flip_below = above_y < 16;
  const top = flip_below ? rect.top + rect.height + 12 : above_y;
  const center_x = rect.left + rect.width / 2 - TOOLBAR_W / 2;
  const left = Math.max(
    12,
    Math.min(window.innerWidth - TOOLBAR_W - 12, center_x),
  );

  // Esc closes any submenu first, then dismisses.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        if (openMenu) setOpenMenu(null);
        else onDismiss();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openMenu, onDismiss]);

  // Close menus on outside click. The layer click itself goes to the
  // iframe, which is a separate document tree — we just close on any
  // pointerdown that isn't inside our floating element.
  useEffect(() => {
    if (!openMenu) return;
    const onDown = (e: MouseEvent) => {
      if (!ref.current) return;
      if (!ref.current.contains(e.target as Node)) {
        setOpenMenu(null);
      }
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [openMenu]);

  return (
    <div
      ref={ref}
      role="toolbar"
      aria-label={t("Edit selected layer")}
      style={{
        position: "fixed",
        top,
        left,
        zIndex: 60,
      }}
      className="flex items-center gap-1 rounded-md border border-ink-300/70 bg-paper px-1.5 py-1 shadow-page animate-riseIn"
      // Block focus from leaving the iframe layer when clicking a
      // toolbar button — preserves the contenteditable selection.
      onMouseDown={(e) => e.preventDefault()}
    >
      {onMoveStart && (
        <>
          <button
            type="button"
            aria-label={t("Move text box")}
            title={t("Move text box")}
            onPointerDown={onMoveStart}
            className="cursor-move rounded px-1.5 py-1 text-ink-500 transition hover:bg-surface-raised hover:text-ink-900"
          >
            <I.Move width={12} height={12} />
          </button>
          <span className="mx-0.5 h-4 w-px bg-ink-200" />
        </>
      )}

      {/* Font size */}
      <div className="relative">
        <button
          type="button"
          className="tabular flex items-center gap-1 rounded px-2 py-1 text-[11.5px] text-ink-700 transition hover:bg-surface-raised"
          onClick={() => setOpenMenu(openMenu === "size" ? null : "size")}
          title={t("Font size")}
        >
          <I.TextSize width={11} height={11} className="text-ink-500" />
          <span className="font-medium">{state.font_size_px ?? 16}</span>
          <I.ChevronDown width={9} height={9} className="text-ink-500" />
        </button>
        {openMenu === "size" && (
          <div className="absolute left-0 top-full z-10 mt-1 max-h-[260px] w-[80px] overflow-y-auto rounded-md border border-ink-300/70 bg-paper py-1 shadow-page">
            {FONT_SIZES.map((s) => (
              <button
                key={s}
                type="button"
                className={`tabular flex w-full items-center justify-between px-3 py-1 text-[11.5px] transition hover:bg-surface-raised ${
                  state.font_size_px === s ? "text-ink-900" : "text-ink-700"
                }`}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => {
                  onPatch({ font_size_px: s });
                  setOpenMenu(null);
                }}
              >
                <span>{s}</span>
                {state.font_size_px === s && (
                  <I.Check width={10} height={10} className="text-accent" />
                )}
              </button>
            ))}
          </div>
        )}
      </div>

      <span className="mx-0.5 h-4 w-px bg-ink-200" />

      {/* Color */}
      <div className="relative">
        <button
          type="button"
          className="flex items-center gap-1.5 rounded px-2 py-1 text-[11.5px] text-ink-700 transition hover:bg-surface-raised"
          onClick={() => setOpenMenu(openMenu === "color" ? null : "color")}
          title={t("Text color")}
        >
          <span
            aria-hidden
            className="h-3 w-3 rounded-full border border-ink-300/70"
            style={{ background: state.fill ?? "#1A1A1A" }}
          />
          <I.ChevronDown width={9} height={9} className="text-ink-500" />
        </button>
        {openMenu === "color" && (
          <div className="absolute left-0 top-full z-10 mt-1 grid w-[148px] grid-cols-4 gap-1.5 rounded-md border border-ink-300/70 bg-paper p-2 shadow-page">
            {PRESET_COLORS.map((c) => (
              <button
                key={c}
                type="button"
                className="h-6 w-6 rounded border border-ink-300/70 transition hover:scale-110"
                style={{ background: c }}
                title={c}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => {
                  onPatch({ fill: c });
                  setOpenMenu(null);
                }}
              />
            ))}
            <label className="col-span-4 mt-1 flex items-center gap-1.5 text-[10px] uppercase text-ink-500" style={{ letterSpacing: "0.12em" }}>
              <span>{t("custom")}</span>
              <input
                type="color"
                className="h-5 w-12 cursor-pointer rounded border border-ink-300/70 bg-paper"
                value={state.fill ?? "#1A1A1A"}
                onChange={(e) => onPatch({ fill: e.target.value })}
                onMouseDown={(e) => e.stopPropagation()}
              />
            </label>
          </div>
        )}
      </div>

      <span className="mx-0.5 h-4 w-px bg-ink-200" />

      {/* Align */}
      <div className="flex items-center">
        {(["left", "center", "right"] as Align[]).map((a) => {
          const Icon =
            a === "left" ? I.AlignLeft : a === "center" ? I.AlignCenter : I.AlignRight;
          const active = state.align === a;
          return (
            <button
              key={a}
              type="button"
              title={t(`Align ${a}`)}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => onPatch({ align: a })}
              className={`rounded px-1.5 py-1 transition hover:bg-surface-raised ${
                active ? "bg-surface-raised text-ink-900" : "text-ink-500"
              }`}
            >
              <Icon width={12} height={12} />
            </button>
          );
        })}
      </div>

      {(onUndo || onRedo || onDelete) && (
        <>
          <span className="mx-0.5 h-4 w-px bg-ink-200" />
          <div className="flex items-center">
            {onUndo && (
              <button
                type="button"
                title={t("Undo edit")}
                disabled={!canUndo}
                onMouseDown={(e) => e.preventDefault()}
                onClick={onUndo}
                className="rounded px-1.5 py-1 text-ink-500 transition hover:bg-surface-raised disabled:cursor-not-allowed disabled:opacity-35"
              >
                <I.Undo width={12} height={12} />
              </button>
            )}
            {onRedo && (
              <button
                type="button"
                title={t("Redo edit")}
                disabled={!canRedo}
                onMouseDown={(e) => e.preventDefault()}
                onClick={onRedo}
                className="rounded px-1.5 py-1 text-ink-500 transition hover:bg-surface-raised disabled:cursor-not-allowed disabled:opacity-35"
              >
                <I.Redo width={12} height={12} />
              </button>
            )}
            {onDelete && (
              <button
                type="button"
                title={t("Delete DOM object")}
                onMouseDown={(e) => e.preventDefault()}
                onClick={onDelete}
                className="rounded px-1.5 py-1 text-red-600 transition hover:bg-red-50"
              >
                <I.Trash width={12} height={12} />
              </button>
            )}
          </div>
        </>
      )}
    </div>
  );
}
