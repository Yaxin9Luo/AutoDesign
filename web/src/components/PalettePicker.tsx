import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { translate } from "@/lib/i18n";
import { useApp } from "@/lib/store";
import type { PosterPalette } from "@/lib/types";
import { I } from "./icons";

export interface PalettePickerProps {
  palettes: PosterPalette[];
  status: "idle" | "loading" | "ready" | "error";
  error: string | null;
  selectedId: string | null;
  compact?: boolean;
  openRequest: number;
  invalid: boolean;
  onSelect: (paletteId: string) => void;
  onRetry: () => void;
}

function PopoverHost({ portaled, children }: { portaled: boolean; children: ReactNode }) {
  return portaled ? createPortal(children, document.body) : children;
}

export function PalettePicker({
  palettes,
  status,
  error,
  selectedId,
  compact,
  openRequest,
  invalid,
  onSelect,
  onRetry,
}: PalettePickerProps) {
  const language = useApp((s) => s.ui_language);
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
  const selectedIndex = palettes.findIndex((palette) => palette.id === selectedId);
  const selected = selectedIndex >= 0 ? palettes[selectedIndex] : null;

  const updateCompactPosition = useCallback(() => {
    if (!compact || !rootRef.current || !triggerRef.current) return;
    const triggerRect = triggerRef.current.getBoundingClientRect();
    const railRect = rootRef.current.closest(".app-panel")?.getBoundingClientRect();
    const viewportLeft = 8;
    const viewportRight = window.innerWidth - 8;
    const width = Math.max(0, Math.min(244, viewportRight - viewportLeft));
    const visibleRailLeft = Math.max(viewportLeft, (railRect?.left ?? 0) + 8);
    const visibleRailRight = Math.min(viewportRight, (railRect?.right ?? window.innerWidth) - 8);
    const railCanContainPopover = visibleRailRight - visibleRailLeft >= width;
    const minLeft = railCanContainPopover ? visibleRailLeft : viewportLeft;
    const maxLeft = railCanContainPopover ? visibleRailRight - width : viewportRight - width;
    const left = Math.min(Math.max(triggerRect.right - width, minLeft), maxLeft);
    setCompactPosition({
      left,
      bottom: Math.max(8, window.innerHeight - triggerRect.top + 8),
      width,
    });
  }, [compact]);

  const focusCurrentState = useCallback((index = selectedIndex >= 0 ? selectedIndex : 0) => {
    requestAnimationFrame(() => {
      if (status === "ready" && palettes.length) {
        optionRefs.current[index]?.focus();
      } else if (status === "error") {
        retryRef.current?.focus();
      } else {
        statusRef.current?.focus();
      }
    });
  }, [palettes.length, selectedIndex, status]);

  const openAndFocus = useCallback((index = selectedIndex >= 0 ? selectedIndex : 0) => {
    const nextIndex = palettes.length ? Math.min(Math.max(index, 0), palettes.length - 1) : 0;
    setActiveIndex(nextIndex);
    updateCompactPosition();
    setOpen(true);
    focusCurrentState(nextIndex);
  }, [focusCurrentState, palettes.length, selectedIndex, updateCompactPosition]);

  useEffect(() => {
    if (previousOpenRequest.current === openRequest) return;
    previousOpenRequest.current = openRequest;
    openAndFocus();
  }, [openAndFocus, openRequest]);

  useEffect(() => {
    if (!open) return;
    const nextIndex = selectedIndex >= 0 ? selectedIndex : 0;
    if (status === "ready" && palettes.length) setActiveIndex(nextIndex);
    focusCurrentState(nextIndex);
  }, [focusCurrentState, open, palettes.length, selectedIndex, status]);

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
      if (
        !rootRef.current?.contains(target)
        && !popoverRef.current?.contains(target)
      ) {
        setOpen(false);
      }
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, [open]);

  const focusOption = (index: number) => {
    if (!palettes.length) return;
    const nextIndex = (index + palettes.length) % palettes.length;
    setActiveIndex(nextIndex);
    optionRefs.current[nextIndex]?.focus();
  };

  const closeAndRestoreFocus = () => {
    setOpen(false);
    requestAnimationFrame(() => triggerRef.current?.focus());
  };

  const selectPalette = (paletteId: string) => {
    onSelect(paletteId);
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
        aria-invalid={invalid || undefined}
        onClick={() => (open ? setOpen(false) : openAndFocus())}
        onKeyDown={(event) => {
          if (["ArrowDown", "ArrowUp", "Enter", " "].includes(event.key)) {
            event.preventDefault();
            const start = event.key === "ArrowUp" && selectedIndex < 0
              ? palettes.length - 1
              : selectedIndex >= 0 ? selectedIndex : 0;
            openAndFocus(start);
          }
          if (event.key === "Escape" && open) {
            event.preventDefault();
            setOpen(false);
          }
        }}
        title={selected?.name ?? t("Choose a palette")}
        className={compact
          ? `flex h-8 w-8 items-center justify-center rounded-md border bg-paper/75 transition hover:border-accent/70 ${
              invalid ? "border-red-600 ring-2 ring-red-500/25" : "border-ink-300/70"
            }`
          : `inline-flex h-8 min-w-0 max-w-[190px] items-center overflow-hidden rounded-md border bg-paper/75 text-left transition hover:border-accent/70 ${
              invalid ? "border-red-600 ring-2 ring-red-500/25" : "border-ink-300/70"
            }`}
      >
        {compact ? (
          selected ? (
            <PaletteSwatches palette={selected} compact />
          ) : (
            <I.Paintbrush width={14} height={14} className={invalid ? "text-red-700" : "text-ink-500"} />
          )
        ) : (
          <>
            <span className="flex h-full w-9 shrink-0 items-center justify-center border-r border-ink-300/55 bg-vellum/80">
              {selected ? <PaletteSwatches palette={selected} compact /> : <I.Paintbrush width={14} height={14} />}
            </span>
            <span className="flex min-w-0 flex-1 flex-col justify-center px-2.5 leading-none">
              <span className="text-[9.5px] text-ink-500">{t("Palette")}</span>
              <span className={`mt-0.5 truncate text-[12px] font-semibold ${selected ? "text-ink-900" : "text-ink-500"}`}>
                {selected?.name ?? t("Choose a palette")}
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
            style={compact && compactPosition ? {
              left: compactPosition.left,
              bottom: compactPosition.bottom,
              width: compactPosition.width,
            } : undefined}
            className={`${compact ? "fixed" : "absolute bottom-full right-0 mb-2 w-[min(430px,calc(100vw-16px))]"} z-50 rounded-md border border-ink-300/80 bg-surface-raised p-2 shadow-page ${
              compact
                ? "max-w-[calc(100vw-16px)]"
                : ""
            }`}
          >
          <div className="mb-2 flex items-center justify-between gap-3 px-1">
            <span className="text-[12px] font-semibold text-ink-900">{t("Choose a palette")}</span>
            <I.Paintbrush width={13} height={13} className="text-ink-500" />
          </div>
          {invalid && (
            <p className="mb-2 rounded-sm bg-red-50 px-2 py-1.5 text-[11px] leading-4 text-red-800" role="alert">
              {t("Select a palette before creating or revising a poster.")}
            </p>
          )}
          {(status === "idle" || status === "loading") && (
            <div
              ref={statusRef}
              tabIndex={-1}
              className="px-2 py-5 text-center text-[12px] text-ink-500 outline-none focus:ring-2 focus:ring-accent/45"
              role="status"
              aria-live="polite"
            >
              {t("Loading palettes...")}
            </div>
          )}
          {status === "error" && (
            <div className="px-2 py-3 text-center">
              <p className="text-[12px] font-medium text-ink-800">{t("Palette catalog unavailable")}</p>
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
              aria-label={t("Palette")}
              className={`grid max-h-[240px] overflow-y-auto sm:max-h-[320px] ${compact ? "grid-cols-1" : "grid-cols-2"} gap-1.5`}
            >
              {palettes.map((palette, index) => (
                <button
                  key={palette.id}
                  ref={(node) => { optionRefs.current[index] = node; }}
                  type="button"
                  role="option"
                  aria-selected={palette.id === selectedId}
                  tabIndex={index === activeIndex ? 0 : -1}
                  onFocus={() => setActiveIndex(index)}
                  onClick={() => selectPalette(palette.id)}
                  onKeyDown={(event) => {
                    if (event.key === "ArrowDown" || event.key === "ArrowRight") {
                      event.preventDefault();
                      focusOption(index + 1);
                    } else if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
                      event.preventDefault();
                      focusOption(index - 1);
                    } else if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      selectPalette(palette.id);
                    } else if (event.key === "Escape") {
                      event.preventDefault();
                      closeAndRestoreFocus();
                    } else if (event.key === "Home") {
                      event.preventDefault();
                      focusOption(0);
                    } else if (event.key === "End") {
                      event.preventDefault();
                      focusOption(palettes.length - 1);
                    }
                  }}
                  className={`flex min-w-0 items-center gap-2 rounded-sm border px-2 py-2 text-left transition focus:outline-none focus:ring-2 focus:ring-accent/50 ${
                    palette.id === selectedId
                      ? "border-accent/70 bg-accent/5"
                      : "border-ink-300/60 bg-paper/55 hover:border-accent/55 hover:bg-white"
                  }`}
                >
                  <PaletteSwatches palette={palette} />
                  <span className="min-w-0 flex-1 truncate text-[11.5px] font-medium text-ink-900">
                    {palette.name}
                  </span>
                  {palette.id === selectedId && <I.Check width={12} height={12} className="shrink-0 text-accent-deep" />}
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

function PaletteSwatches({ palette, compact }: { palette: PosterPalette; compact?: boolean }) {
  const colors = [
    palette.roles.primary,
    palette.roles.secondary,
    palette.roles.accent,
    palette.roles.background,
  ];
  return (
    <span
      aria-hidden
      className={`grid shrink-0 grid-cols-2 overflow-hidden rounded-sm border border-black/10 ${compact ? "h-4 w-4" : "h-7 w-9"}`}
    >
      {colors.map((color, index) => (
        <span key={`${color}-${index}`} style={{ backgroundColor: color }} />
      ))}
    </span>
  );
}
