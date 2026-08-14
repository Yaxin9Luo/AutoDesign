import { useEffect, useMemo, useState } from "react";
import { translate } from "@/lib/i18n";
import { useApp } from "@/lib/store";
import { findInjectedStyle } from "./styleTagCompatibility";

type StyleScope = "global" | "section";
type StyleKey = "accent" | "accent2" | "background" | "ink";
type StyleValues = Record<StyleKey, string>;

const DEFAULT_STYLES: StyleValues = {
  accent: "#17345c",
  accent2: "#9a3712",
  background: "#fbf7ec",
  ink: "#17130f",
};

const STYLE_ROWS: Array<{ key: StyleKey; label: string; hint: string }> = [
  { key: "accent", label: "Primary", hint: "Headers and section bands" },
  { key: "accent2", label: "Accent", hint: "Metrics and highlights" },
  { key: "background", label: "Paper", hint: "Poster or panel background" },
  { key: "ink", label: "Text", hint: "Body copy color" },
];

export function PosterStyleEditor({
  iframe,
  active,
}: {
  iframe: HTMLIFrameElement | null;
  active: boolean;
}) {
  const selectedIds = useApp((s) => s.selected_layer_ids);
  const recordHtmlLayoutPatch = useApp((s) => s.recordHtmlLayoutPatch);
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  const [scope, setScope] = useState<StyleScope>("global");
  const [styles, setStyles] = useState<StyleValues>(DEFAULT_STYLES);

  const doc = iframe?.contentDocument ?? null;
  const selectedSection = useMemo(
    () => (doc ? currentSelectedSection(doc) : null),
    [doc, selectedIds],
  );
  const selectedSectionId = selectedSection ? sectionPatchId(selectedSection) : null;
  const selectedSectionLabel = selectedSection ? sectionLabel(selectedSection) : null;
  const canUseSection = !!selectedSectionId;

  useEffect(() => {
    if (scope === "section" && !canUseSection) setScope("global");
  }, [canUseSection, scope]);

  if (!active || !doc) return null;

  const apply = (next: StyleValues, nextScope: StyleScope = scope) => {
    setStyles(next);
    if (nextScope === "section" && selectedSection && selectedSectionId) {
      applySectionStyle(selectedSection, next);
      recordHtmlLayoutPatch({
        kind: "poster_style",
        scope: "section",
        section_id: selectedSectionId,
        styles: next,
      });
      return;
    }
    applyGlobalStyle(doc, next);
    recordHtmlLayoutPatch({
      kind: "poster_style",
      scope: "global",
      styles: next,
    });
  };

  const switchScope = (nextScope: StyleScope) => {
    if (nextScope === "section" && !canUseSection) return;
    setScope(nextScope);
  };

  return (
    <div className="pointer-events-none absolute right-5 top-5 z-40 w-[290px]">
      <div className="pointer-events-auto rounded-lg border border-ink-300/80 bg-surface-raised/95 p-3 shadow-xl backdrop-blur-md">
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="eyebrow text-ink-500">{t("Style")}</div>
            <div className="mt-1 font-display text-[16px] leading-tight text-ink-900">
              {t("Color controls")}
            </div>
          </div>
        </div>

        <div className="mt-3 grid grid-cols-2 gap-1 rounded-md border border-ink-200 bg-paper p-1">
          <button
            type="button"
            onClick={() => switchScope("global")}
            className={`h-8 rounded-sm text-[10px] font-medium uppercase ${
              scope === "global"
                ? "bg-ink-900 text-white"
                : "text-ink-600 hover:bg-white hover:text-ink-900"
            }`}
            style={{ letterSpacing: "0.14em" }}
          >
            {t("Whole Poster")}
          </button>
          <button
            type="button"
            onClick={() => switchScope("section")}
            disabled={!canUseSection}
            className={`h-8 rounded-sm text-[10px] font-medium uppercase ${
              scope === "section"
                ? "bg-accent text-white"
                : "text-ink-600 hover:bg-white hover:text-ink-900"
            } disabled:cursor-not-allowed disabled:opacity-45`}
            style={{ letterSpacing: "0.14em" }}
            title={canUseSection ? t("Style the selected panel only") : t("Select a panel BOX first")}
          >
            {t("Panel")}
          </button>
        </div>

        <div className="mt-3 rounded-md border border-ink-200 bg-paper/70 px-2.5 py-2 text-[11px] text-ink-500">
          {scope === "section"
            ? selectedSectionLabel ?? t("Selected panel")
            : t("Applies to the poster color system.")}
        </div>

        <div className="mt-3 space-y-2">
          {STYLE_ROWS.map((row) => (
            <label
              key={row.key}
              className="flex items-center justify-between gap-3 rounded-md border border-ink-200 bg-white/75 px-2.5 py-2"
            >
              <span className="min-w-0">
                <span className="block text-[12px] font-medium text-ink-900">{t(row.label)}</span>
                <span className="block truncate text-[10.5px] text-ink-500">{t(row.hint)}</span>
              </span>
              <span className="flex shrink-0 items-center gap-2">
                <span className="tabular text-[10px] uppercase text-ink-500">{styles[row.key]}</span>
                <input
                  type="color"
                  value={styles[row.key]}
                  onChange={(e) => apply({ ...styles, [row.key]: e.target.value })}
                  className="h-8 w-9 cursor-pointer rounded border border-ink-300 bg-transparent p-0.5"
                  title={translate(language, "Change {label}", { label: t(row.label) })}
                />
              </span>
            </label>
          ))}
        </div>
      </div>
    </div>
  );
}

function currentSelectedSection(doc: Document): HTMLElement | null {
  return doc.querySelector<HTMLElement>(
    ".paper-poster .poster-section.ld-active, .paper-poster .poster-header.ld-active",
  );
}

function sectionPatchId(el: HTMLElement): string | null {
  return el.getAttribute("data-block-id") || el.id || el.getAttribute("data-layer-id");
}

function sectionLabel(el: HTMLElement): string {
  const heading = el.querySelector("h1, h2, h3, .section-title, .section-heading");
  return (heading?.textContent ?? el.getAttribute("data-panel-role") ?? "Selected panel")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 72);
}

function applySectionStyle(section: HTMLElement, styles: StyleValues): void {
  section.style.background = styles.background;
  section.style.color = styles.ink;
  section.style.borderColor = styles.accent;
  section.setAttribute("data-style-tweaked", "section");
  const title = section.querySelector<HTMLElement>(".section-title, .section-heading, h2, h3");
  if (title) {
    title.style.background = styles.accent;
    title.style.borderColor = styles.accent;
    title.style.boxShadow = `inset 6px 0 0 ${styles.accent2}`;
    title.style.color = "#ffffff";
  }
}

function applyGlobalStyle(doc: Document, styles: StyleValues): void {
  const root = doc.querySelector<HTMLElement>(".paper-poster");
  if (!root) return;
  root.style.setProperty("--da-accent", styles.accent);
  root.style.setProperty("--da-accent2", styles.accent2);
  root.style.setProperty("--da-background", styles.background);
  root.style.setProperty("--da-ink", styles.ink);
  root.style.background = styles.background;
  root.style.color = styles.ink;
  root.setAttribute("data-style-tweaked", "global");

  let tag = findInjectedStyle(
    doc,
    "autodesign-style-tweaks",
    "designanything-style-tweaks",
  );
  if (!tag) {
    tag = doc.createElement("style");
    tag.id = "autodesign-style-tweaks";
    doc.head.appendChild(tag);
  }
  tag.textContent = posterStyleCss(styles);
}

function posterStyleCss(styles: StyleValues): string {
  return `
.paper-poster {
  --da-accent: ${styles.accent};
  --da-accent2: ${styles.accent2};
  --da-background: ${styles.background};
  --da-ink: ${styles.ink};
}
.paper-poster {
  background: var(--da-background) !important;
  color: var(--da-ink) !important;
}
.paper-poster .poster-header,
.paper-poster .section-title,
.paper-poster .section-heading {
  background: var(--da-accent) !important;
  border-color: var(--da-accent) !important;
}
.paper-poster .metric,
.paper-poster .stat,
.paper-poster .highlight,
.paper-poster .formula,
.paper-poster .readout,
.paper-poster .native-row strong {
  color: var(--da-accent2) !important;
}
.paper-poster table th,
.paper-poster .comparison-item,
.paper-poster .identity-badge {
  border-color: var(--da-accent2) !important;
}
`.trim();
}
