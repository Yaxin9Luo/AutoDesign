import { useEffect, useRef, useState } from "react";
import {
  LANGUAGE_OPTIONS,
  languageLabel,
  languageShortLabel,
  translate,
  type UiLanguage,
} from "@/lib/i18n";
import { useApp } from "@/lib/store";
import { I } from "./icons";

export function LanguageMenu({ compact = false }: { compact?: boolean }) {
  const language = useApp((s) => s.ui_language);
  const setLanguage = useApp((s) => s.setUiLanguage);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  const t = (text: string) => translate(language, text);

  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (!ref.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const choose = (next: UiLanguage) => {
    setLanguage(next);
    setOpen(false);
  };

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className={`inline-flex items-center gap-1.5 rounded-md border border-ink-300/60 bg-paper/75 text-ink-600 transition hover:border-ink-400 hover:bg-white hover:text-ink-900 ${
          compact ? "px-1.5 py-1 text-[10px]" : "px-2 py-1 text-[11px]"
        }`}
        title={t("Language")}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <span className="tabular font-medium uppercase" style={{ letterSpacing: "0.08em" }}>
          {languageShortLabel(language)}
        </span>
        <I.ChevronDown
          width={10}
          height={10}
          className="text-ink-400 transition"
          style={{ transform: open ? "rotate(180deg)" : undefined }}
        />
      </button>
      {open && (
        <div
          role="menu"
          className={`absolute right-0 z-50 mt-2 w-[196px] overflow-hidden rounded-md border border-ink-300/70 bg-paper shadow-page ${
            compact ? "bottom-full mb-2 mt-0" : ""
          }`}
        >
          <div className="border-b border-ink-300/55 px-3 py-2">
            <div className="eyebrow text-ink-500">{t("Language")}</div>
          </div>
          <div className="max-h-[320px] overflow-y-auto py-1">
            {LANGUAGE_OPTIONS.map((option) => {
              const active = option.id === language;
              return (
                <button
                  key={option.id}
                  type="button"
                  role="menuitemradio"
                  aria-checked={active}
                  onClick={() => choose(option.id)}
                  className={`flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-[12px] transition ${
                    active
                      ? "bg-accent-soft text-accent-deep"
                      : "text-ink-700 hover:bg-vellum hover:text-ink-900"
                  }`}
                >
                  <span className="min-w-0">
                    <span className="block truncate font-medium">{option.nativeLabel}</span>
                    <span className="block truncate text-[10.5px] text-ink-500">
                      {t(option.label)}
                    </span>
                  </span>
                  <span className="tabular text-[10px] uppercase text-ink-400" style={{ letterSpacing: "0.1em" }}>
                    {active ? "✓" : option.shortLabel}
                  </span>
                </button>
              );
            })}
          </div>
          <div className="border-t border-ink-300/55 px-3 py-2 text-[10.5px] leading-relaxed text-ink-500">
            {languageLabel(language)}
          </div>
        </div>
      )}
    </div>
  );
}
