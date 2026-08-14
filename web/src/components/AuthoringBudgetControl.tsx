import { useCallback, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";

import {
  AUTHORING_BUDGET_MAX,
  AUTHORING_BUDGET_MIN,
  DEFAULT_AUTHORING_BUDGETS,
  type AuthoringBudgets,
} from "@/lib/authoring_budget";
import { translate } from "@/lib/i18n";
import { useApp } from "@/lib/store";
import type { ArtifactType } from "@/lib/types";
import { I } from "./icons";

const rows: Array<{ artifactType: ArtifactType; label: string }> = [
  { artifactType: "poster", label: "Poster" },
  { artifactType: "deck", label: "Slides" },
  { artifactType: "landing", label: "Landing" },
  { artifactType: "video", label: "Video" },
];

export interface AuthoringBudgetControlProps {
  budgets: AuthoringBudgets;
  intent: ArtifactType | null;
  compact?: boolean;
  disabled?: boolean;
  demoMode?: boolean;
  onChange: (budgets: AuthoringBudgets) => void;
}

export function AuthoringBudgetControl({
  budgets,
  intent,
  compact,
  disabled,
  demoMode,
  onChange,
}: AuthoringBudgetControlProps) {
  const language = useApp((state) => state.ui_language);
  const t = (text: string) => translate(language, text);
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ left: 8, bottom: 8 });
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  const dialogId = useId();

  const updatePosition = useCallback(() => {
    const trigger = triggerRef.current;
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    const width = Math.min(288, window.innerWidth - 16);
    setPosition({
      left: Math.min(
        Math.max(8, rect.left),
        Math.max(8, window.innerWidth - width - 8),
      ),
      bottom: Math.max(8, window.innerHeight - rect.top + 8),
    });
  }, []);

  const closeAndRestoreFocus = useCallback(() => {
    setOpen(false);
    requestAnimationFrame(() => triggerRef.current?.focus());
  }, []);

  useEffect(() => {
    if (!open) return;
    updatePosition();
    requestAnimationFrame(() => popoverRef.current?.focus());
    const closeOnOutsidePointer = (event: PointerEvent) => {
      const target = event.target as Node;
      if (
        !triggerRef.current?.contains(target)
        && !popoverRef.current?.contains(target)
      ) {
        setOpen(false);
      }
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    window.addEventListener("resize", updatePosition);
    window.addEventListener("scroll", updatePosition, true);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
    };
  }, [open, updatePosition]);

  const changeBudget = (artifactType: ArtifactType, delta: number) => {
    const nextValue = Math.min(
      AUTHORING_BUDGET_MAX,
      Math.max(AUTHORING_BUDGET_MIN, budgets[artifactType] + delta),
    );
    if (nextValue === budgets[artifactType]) return;
    onChange({ ...budgets, [artifactType]: nextValue });
  };

  const current = intent ? budgets[intent] : null;
  const triggerLabel = current === null ? t("Budgets") : `${t("Budget")} · ${current}`;

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? dialogId : undefined}
        disabled={disabled}
        onClick={() => {
          if (!open) updatePosition();
          setOpen((value) => !value);
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape" && open) {
            event.preventDefault();
            closeAndRestoreFocus();
          }
        }}
        title={t("Authoring attempts")}
        className={compact
          ? "inline-flex h-8 min-w-8 items-center justify-center gap-1 rounded-md border border-ink-300/70 bg-paper/75 px-1.5 text-[11px] font-semibold text-ink-700 transition hover:border-accent/70 disabled:opacity-40"
          : "inline-flex h-8 shrink-0 items-center overflow-hidden rounded-md border border-ink-300/70 bg-paper/75 text-left transition hover:border-accent/70 disabled:opacity-40"}
      >
        {compact ? (
          <>
            <I.Settings width={13} height={13} />
            {current !== null && <span className="tabular">{current}</span>}
          </>
        ) : (
          <>
            <span className="flex h-full w-9 shrink-0 items-center justify-center border-r border-ink-300/55 bg-vellum/80">
              <I.Settings width={13} height={13} />
            </span>
            <span className="shrink-0 whitespace-nowrap px-2.5 text-[12px] font-semibold text-ink-800">
              {triggerLabel}
            </span>
          </>
        )}
      </button>

      {open && createPortal(
        <div
          ref={popoverRef}
          id={dialogId}
          role="dialog"
          tabIndex={-1}
          aria-label={t("Authoring attempts")}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              closeAndRestoreFocus();
            }
          }}
          style={{
            left: position.left,
            bottom: position.bottom,
            width: "min(288px, calc(100vw - 16px))",
          }}
          className="fixed z-[80] rounded-md border border-ink-300/80 bg-surface-raised p-3 shadow-page"
        >
          <div className="mb-2.5 flex items-start justify-between gap-3">
            <div>
              <div className="text-[12px] font-semibold text-ink-900">
                {t("Authoring attempts")}
              </div>
              <div className="mt-0.5 text-[10.5px] leading-4 text-ink-500">
                {t("Maximum attempts for each artifact")}
              </div>
            </div>
            <button
              type="button"
              onClick={closeAndRestoreFocus}
              className="icon-btn h-6 w-6"
              title={t("Close")}
            >
              <I.X width={12} height={12} />
            </button>
          </div>

          <div className="space-y-1.5">
            {rows.map(({ artifactType, label }) => {
              const rowDisabled = Boolean(demoMode && artifactType !== "poster");
              const value = budgets[artifactType];
              return (
                <div
                  key={artifactType}
                  className={`flex items-center justify-between gap-3 rounded-sm border border-ink-300/55 bg-vellum/30 px-2.5 py-2 ${
                    rowDisabled ? "opacity-40" : ""
                  }`}
                >
                  <span className="text-[12px] font-medium text-ink-800">{t(label)}</span>
                  <div className="flex items-center gap-1">
                    <button
                      type="button"
                      disabled={rowDisabled || value <= AUTHORING_BUDGET_MIN}
                      onClick={() => changeBudget(artifactType, -1)}
                      className="icon-btn h-6 w-6 border border-ink-300/65 bg-paper disabled:opacity-30"
                      aria-label={`${t("Fewer attempts")}: ${t(label)}`}
                    >
                      <span aria-hidden>−</span>
                    </button>
                    <span
                      role="spinbutton"
                      tabIndex={rowDisabled ? -1 : 0}
                      aria-label={`${t("Authoring attempts")}: ${t(label)}`}
                      aria-valuemin={AUTHORING_BUDGET_MIN}
                      aria-valuemax={AUTHORING_BUDGET_MAX}
                      aria-valuenow={value}
                      onKeyDown={(event) => {
                        if (rowDisabled) return;
                        if (event.key === "ArrowDown") {
                          event.preventDefault();
                          changeBudget(artifactType, -1);
                        }
                        if (event.key === "ArrowUp") {
                          event.preventDefault();
                          changeBudget(artifactType, 1);
                        }
                      }}
                      className="inline-flex h-6 w-8 items-center justify-center rounded-sm bg-paper text-[12px] font-semibold tabular text-ink-900 outline-none focus:ring-2 focus:ring-accent/45"
                    >
                      {value}
                    </span>
                    <button
                      type="button"
                      disabled={rowDisabled || value >= AUTHORING_BUDGET_MAX}
                      onClick={() => changeBudget(artifactType, 1)}
                      className="icon-btn h-6 w-6 border border-ink-300/65 bg-paper disabled:opacity-30"
                      aria-label={`${t("More attempts")}: ${t(label)}`}
                    >
                      <I.Plus width={11} height={11} />
                    </button>
                  </div>
                </div>
              );
            })}
          </div>

          <button
            type="button"
            onClick={() => onChange({ ...DEFAULT_AUTHORING_BUDGETS })}
            className="mt-2.5 w-full rounded-sm border border-ink-300/65 bg-paper px-2.5 py-1.5 text-[11px] font-medium text-ink-700 transition hover:border-accent/60 hover:text-ink-900"
          >
            {t("Reset defaults")}
          </button>
        </div>,
        document.body,
      )}
    </>
  );
}
