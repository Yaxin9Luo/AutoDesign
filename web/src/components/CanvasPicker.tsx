import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { translate } from "@/lib/i18n";
import { canvasPickerKeyAction } from "@/lib/poster_canvas_state";
import { useApp } from "@/lib/store";
import type { PosterCanvasPreset } from "@/lib/types";
import { I } from "./icons";

export interface CanvasPickerProps {
  presets: PosterCanvasPreset[];
  status: "idle" | "loading" | "ready" | "error";
  error: string | null;
  selectedId: string;
  compact?: boolean;
  openRequest: number;
  invalid: boolean;
  onSelect: (presetId: string) => void;
  onRetry: () => void;
}

function PopoverHost({ portaled, children }: { portaled: boolean; children: ReactNode }) {
  return portaled ? createPortal(children, document.body) : children;
}

export function CanvasPicker({
  presets,
  status,
  error,
  selectedId,
  compact,
  openRequest,
  invalid,
  onSelect,
  onRetry,
}: CanvasPickerProps) {
  const language = useApp((state) => state.ui_language);
  const t = (text: string) => translate(language, text);
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const [compactPosition, setCompactPosition] = useState<{
    left: number;
    bottom: number;
    width: number;
  } | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const statusRef = useRef<HTMLDivElement | null>(null);
  const retryRef = useRef<HTMLButtonElement | null>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const previousOpenRequest = useRef(openRequest);
  const listboxId = useId();
  const selectedIndex = presets.findIndex((preset) => preset.id === selectedId);
  const selected = selectedIndex >= 0 ? presets[selectedIndex] : null;
  const visuallyInvalid = invalid || status === "error";

  const updateCompactPosition = useCallback(() => {
    if (!compact || !rootRef.current || !triggerRef.current) return;
    const triggerRect = triggerRef.current.getBoundingClientRect();
    const railRect = rootRef.current.closest(".app-panel")?.getBoundingClientRect();
    const viewportLeft = 8;
    const viewportRight = window.innerWidth - 8;
    const width = Math.max(0, Math.min(320, viewportRight - viewportLeft));
    const visibleRailLeft = Math.max(viewportLeft, (railRect?.left ?? 0) + 8);
    const visibleRailRight = Math.min(viewportRight, (railRect?.right ?? window.innerWidth) - 8);
    const railCanContain = visibleRailRight - visibleRailLeft >= width;
    const minLeft = railCanContain ? visibleRailLeft : viewportLeft;
    const maxLeft = railCanContain ? visibleRailRight - width : viewportRight - width;
    setCompactPosition({
      left: Math.min(Math.max(triggerRect.right - width, minLeft), maxLeft),
      bottom: Math.max(8, window.innerHeight - triggerRect.top + 8),
      width,
    });
  }, [compact]);

  const focusCurrentState = useCallback((index = selectedIndex >= 0 ? selectedIndex : 0) => {
    requestAnimationFrame(() => {
      if (status === "ready" && presets.length) {
        optionRefs.current[index]?.focus();
      } else if (status === "error") {
        retryRef.current?.focus();
      } else {
        statusRef.current?.focus();
      }
    });
  }, [presets.length, selectedIndex, status]);

  const openAndFocus = useCallback((index = selectedIndex >= 0 ? selectedIndex : 0) => {
    const nextIndex = presets.length ? Math.min(Math.max(index, 0), presets.length - 1) : 0;
    setActiveIndex(nextIndex);
    updateCompactPosition();
    setOpen(true);
    focusCurrentState(nextIndex);
  }, [focusCurrentState, presets.length, selectedIndex, updateCompactPosition]);

  useEffect(() => {
    if (previousOpenRequest.current === openRequest) return;
    previousOpenRequest.current = openRequest;
    openAndFocus();
  }, [openAndFocus, openRequest]);

  useEffect(() => {
    if (!open) return;
    const nextIndex = selectedIndex >= 0 ? selectedIndex : 0;
    if (status === "ready" && presets.length) setActiveIndex(nextIndex);
    focusCurrentState(nextIndex);
  }, [focusCurrentState, open, presets.length, selectedIndex, status]);

  useEffect(() => {
    if (!open || !compact) return;
    updateCompactPosition();
    window.addEventListener("resize", updateCompactPosition);
    window.addEventListener("scroll", updateCompactPosition, true);
    return () => {
      window.removeEventListener("resize", updateCompactPosition);
      window.removeEventListener("scroll", updateCompactPosition, true);
    };
  }, [compact, open, updateCompactPosition]);

  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!rootRef.current?.contains(target) && !popoverRef.current?.contains(target)) {
        setOpen(false);
      }
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, [open]);

  const closeAndRestoreFocus = () => {
    setOpen(false);
    requestAnimationFrame(() => triggerRef.current?.focus());
  };

  const focusOption = (index: number) => {
    setActiveIndex(index);
    optionRefs.current[index]?.focus();
  };

  const selectPreset = (presetId: string) => {
    onSelect(presetId);
    closeAndRestoreFocus();
  };

  return (
    <div ref={rootRef} className="relative shrink-0">
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listboxId : undefined}
        aria-invalid={visuallyInvalid || undefined}
        onClick={() => (open ? setOpen(false) : openAndFocus())}
        onKeyDown={(event) => {
          if (["ArrowDown", "ArrowUp", "Enter", " "].includes(event.key)) {
            event.preventDefault();
            const start = event.key === "ArrowUp" && selectedIndex < 0
              ? presets.length - 1
              : selectedIndex >= 0 ? selectedIndex : 0;
            openAndFocus(start);
          }
        }}
        title={selected ? t(selected.label) : t("Choose a canvas")}
        className={compact
          ? `flex h-8 w-8 items-center justify-center rounded-md border bg-paper/75 transition hover:border-accent/70 ${visuallyInvalid ? "border-red-600 ring-2 ring-red-500/25" : "border-ink-300/70"}`
          : `inline-flex h-8 min-w-0 max-w-[190px] items-center overflow-hidden rounded-md border bg-paper/75 text-left transition hover:border-accent/70 ${visuallyInvalid ? "border-red-600 ring-2 ring-red-500/25" : "border-ink-300/70"}`}
      >
        {compact ? (
          <CanvasGlyph preset={selected} />
        ) : (
          <>
            <span className="flex h-full w-9 shrink-0 items-center justify-center border-r border-ink-300/55 bg-vellum/80">
              <CanvasGlyph preset={selected} />
            </span>
            <span className="flex min-w-0 flex-1 flex-col justify-center px-2.5 leading-none">
              <span className="text-[9.5px] text-ink-500">{t("Canvas")}</span>
              <span className="mt-0.5 truncate text-[12px] font-semibold text-ink-900">
                {selected ? t(selected.label) : t("Choose a canvas")}
              </span>
            </span>
          </>
        )}
      </button>

      {open && (
        <PopoverHost portaled={!!compact}>
          <div
            ref={popoverRef}
            id={status === "ready" ? undefined : listboxId}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.preventDefault();
                closeAndRestoreFocus();
              }
            }}
            style={compact && compactPosition ? compactPosition : undefined}
            className={`${compact ? "fixed" : "absolute bottom-full right-0 mb-2 w-[min(360px,calc(100vw-16px))]"} z-50 rounded-md border border-ink-300/80 bg-surface-raised p-2 shadow-page`}
          >
            <div className="mb-2 flex items-center justify-between gap-3 px-1">
              <span className="text-[12px] font-semibold text-ink-900">{t("Choose a canvas")}</span>
              <I.Layout width={13} height={13} className="text-ink-500" />
            </div>
            {invalid && (
              <p className="mb-2 rounded-sm bg-red-50 px-2 py-1.5 text-[11px] leading-4 text-red-800" role="alert">
                {t("Choose a valid canvas preset before creating a poster.")}
              </p>
            )}
            {(status === "idle" || status === "loading") && (
              <div
                ref={statusRef}
                tabIndex={-1}
                role="status"
                aria-live="polite"
                className="px-2 py-5 text-center text-[12px] text-ink-500 outline-none focus:ring-2 focus:ring-accent/45"
              >
                {t("Loading canvas presets...")}
              </div>
            )}
            {status === "error" && (
              <div className="px-2 py-3 text-center" role="alert">
                <p className="text-[12px] font-medium text-ink-800">
                  {t("Canvas preset catalog unavailable")}
                </p>
                {error && <p className="mt-1 line-clamp-2 text-[10.5px] text-ink-500">{error}</p>}
                <button
                  ref={retryRef}
                  type="button"
                  onClick={onRetry}
                  className="mt-2 inline-flex items-center gap-1 rounded-sm border border-ink-300 bg-paper px-2.5 py-1 text-[11px] font-medium text-ink-800 hover:border-accent/60 focus:outline-none focus:ring-2 focus:ring-accent/50"
                >
                  <I.Refresh width={11} height={11} />
                  {t("Retry")}
                </button>
              </div>
            )}
            {status === "ready" && (
              <div
                id={listboxId}
                role="listbox"
                aria-label={t("Canvas")}
                className="grid max-h-[320px] grid-cols-1 gap-1.5 overflow-y-auto"
              >
                {presets.map((preset, index) => (
                  <button
                    key={preset.id}
                    ref={(node) => { optionRefs.current[index] = node; }}
                    type="button"
                    role="option"
                    aria-selected={preset.id === selectedId}
                    tabIndex={index === activeIndex ? 0 : -1}
                    onFocus={() => setActiveIndex(index)}
                    onClick={() => selectPreset(preset.id)}
                    onKeyDown={(event) => {
                      const action = canvasPickerKeyAction(event.key, index, presets.length);
                      if (action.kind === "none") return;
                      event.preventDefault();
                      if (action.kind === "close") closeAndRestoreFocus();
                      if (action.kind === "focus") focusOption(action.index);
                      if (action.kind === "select") selectPreset(presets[action.index]!.id);
                    }}
                    className={`flex min-w-0 items-center gap-3 rounded-sm border px-2.5 py-2 text-left transition focus:outline-none focus:ring-2 focus:ring-accent/50 ${
                      preset.id === selectedId
                        ? "border-accent/70 bg-accent/5"
                        : "border-ink-300/60 bg-paper/55 hover:border-accent/55 hover:bg-white"
                    }`}
                  >
                    <CanvasGlyph preset={preset} large />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-[11.5px] font-medium text-ink-900">
                        {t(preset.label)}
                      </span>
                      <span className="mt-0.5 block text-[10px] text-ink-500">
                        {preset.canvas
                          ? `${preset.canvas.aspect_ratio} · ${preset.canvas.w_px}×${preset.canvas.h_px}`
                          : t("Prompt first · CVPR default when unspecified")}
                      </span>
                    </span>
                    {preset.id === selectedId && (
                      <I.Check width={12} height={12} className="shrink-0 text-accent-deep" />
                    )}
                  </button>
                ))}
              </div>
            )}
          </div>
        </PopoverHost>
      )}
    </div>
  );
}

function CanvasGlyph({ preset, large }: { preset: PosterCanvasPreset | null; large?: boolean }) {
  if (!preset?.canvas) {
    return <I.Layout width={large ? 20 : 14} height={large ? 20 : 14} className="text-ink-500" />;
  }
  const landscape = preset.canvas.w_px >= preset.canvas.h_px;
  return (
    <span
      aria-hidden
      className={`inline-flex shrink-0 items-center justify-center rounded-[2px] border border-ink-500/70 bg-white ${
        large
          ? landscape ? "h-7 w-10" : "h-9 w-7"
          : landscape ? "h-3.5 w-5" : "h-5 w-3.5"
      }`}
    />
  );
}
