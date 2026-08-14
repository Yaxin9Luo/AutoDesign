import { useEffect, useMemo, useState, type SVGProps } from "react";
import { createPortal } from "react-dom";
import { exportArtifactRequest, type ArtifactExportFormat } from "@/lib/api";
import { artifactTypeForArtifact, useApp } from "@/lib/store";
import type { Artifact } from "@/lib/types";
import { translate } from "@/lib/i18n";
import { I } from "./icons";

interface ArtifactDownloadMenuProps {
  artifact: Artifact;
  className?: string;
  label?: string;
  compact?: boolean;
  pptxExportDisabled?: boolean;
}

type ExportOption = {
  format: ArtifactExportFormat;
  title: string;
  suffix: string;
  desc: string;
  icon: (p: SVGProps<SVGSVGElement>) => JSX.Element;
};

const OPTIONS: ExportOption[] = [
  {
    format: "pdf",
    title: "PDF",
    suffix: "",
    desc: "Standalone PDF of the current design.",
    icon: I.File,
  },
  {
    format: "pptx",
    title: "PowerPoint",
    suffix: ".pptx",
    desc: "Agent-converted editable PowerPoint export.",
    icon: I.Deck,
  },
  {
    format: "original_html",
    title: "Original HTML",
    suffix: ".html",
    desc: "The current source file as generated or saved.",
    icon: I.Layout,
  },
  {
    format: "standalone_html",
    title: "Standalone HTML",
    suffix: ".html",
    desc: "One self-contained file with local assets inlined.",
    icon: I.File,
  },
];

export function ArtifactDownloadMenu({
  artifact,
  className,
  label = "Download",
  compact = false,
  pptxExportDisabled = false,
}: ArtifactDownloadMenuProps) {
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<ArtifactExportFormat>("pdf");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const recordDownload = useApp((s) => s.recordArtifactDownloaded);
  const exportArtifactPptx = useApp((s) => s.exportArtifactPptx);
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);

  const supportsHtmlExport = useMemo(() => {
    return Boolean(
      artifact.native_format === "html" ||
      artifact.view_format === "html" ||
      artifact.native_file_url?.endsWith(".html") ||
      artifact.download_url?.endsWith(".html"),
    );
  }, [artifact]);
  const originalUrl = artifact.download_url ?? artifact.native_file_url ?? artifact.view_file_url ?? "";
  const hasOriginal = Boolean(originalUrl);
  const originalFormat = originalFileFormat(artifact, originalUrl);
  const originalSuffix = originalFormat ? `.${originalFormat}` : "";
  const originalTitle = originalFormat === "html"
    ? t("Original HTML")
    : originalFormat
      ? translate(language, "Original {format}", { format: originalFormat.toUpperCase() })
      : t("Original file");

  useEffect(() => {
    if (!open) return;
    if (!supportsHtmlExport && hasOriginal) setSelected("original_html");
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [hasOriginal, open, supportsHtmlExport]);

  const selectedOption = OPTIONS.find((option) => option.format === selected) ?? OPTIONS[0];
  const canUseFormat = (format: ArtifactExportFormat) => (
    format === "original_html" ? hasOriginal : supportsHtmlExport
  );
  const unavailable = isArtifactExportDisabled(
    selected,
    !canUseFormat(selected),
    false,
    pptxExportDisabled,
  );
  const modal = open ? (
    <div className="fixed inset-0 z-[120] flex items-center justify-center bg-ink-900/20 px-4 py-8">
      <div
        className="absolute inset-0"
        onClick={() => !busy && setOpen(false)}
        aria-hidden="true"
      />
      <div className="relative max-h-full w-full max-w-[720px] overflow-hidden rounded-lg border border-ink-300/80 bg-paper shadow-page">
        <div className="flex items-center justify-between border-b border-ink-300/70 px-5 py-4">
          <div>
            <div
              className="text-[12px] font-medium uppercase text-ink-500"
              style={{ letterSpacing: "0.16em" }}
            >
              {t("Export")}
            </div>
            <div className="mt-1 font-display text-[20px] text-ink-900">
              {translate(language, `Download ${artifactTypeForArtifact(artifact)}`)}
            </div>
          </div>
          <button
            type="button"
            onClick={() => !busy && setOpen(false)}
            className="inline-flex h-8 w-8 items-center justify-center rounded-sm text-ink-500 transition hover:bg-ink-100 hover:text-ink-900"
            title={t("Close")}
          >
            <I.X width={15} height={15} />
          </button>
        </div>
        <div className="max-h-[70vh] overflow-auto px-5 py-5">
          <div className="mb-3 text-[14px] text-ink-700">{t("Format")}</div>
          <div className="grid gap-3 sm:grid-cols-2">
            {OPTIONS.map((option) => {
              const Icon = option.icon;
              const active = selected === option.format;
              const disabled = isArtifactExportDisabled(
                option.format,
                !canUseFormat(option.format),
                busy,
                pptxExportDisabled,
              );
              const optionTitle = option.format === "original_html"
                ? originalTitle
                : t(option.title);
              const optionSuffix = option.format === "original_html"
                ? originalSuffix
                : option.suffix;
              return (
                <button
                  key={option.format}
                  type="button"
                  disabled={disabled}
                  onClick={() => setSelected(option.format)}
                  className={`relative min-h-[128px] rounded-md border bg-surface-raised p-4 text-left transition ${
                    active
                      ? "border-accent bg-accent-soft/35 shadow-sm"
                      : "border-ink-300/75 hover:border-ink-500"
                  } disabled:cursor-not-allowed disabled:opacity-50`}
                >
                  <span className="inline-flex h-12 w-12 items-center justify-center rounded-md bg-vellum text-ink-700">
                    <Icon width={24} height={24} />
                  </span>
                  <span
                    className={`absolute right-4 top-4 h-6 w-6 rounded-full border ${
                      active
                        ? "border-accent bg-accent text-paper"
                        : "border-ink-300 bg-paper"
                    }`}
                  >
                    {active && (
                      <span className="flex h-full w-full items-center justify-center">
                        <I.Check width={13} height={13} />
                      </span>
                    )}
                  </span>
                  <div className="mt-4 text-[20px] font-semibold text-ink-900">
                    {optionTitle}
                    {optionSuffix && (
                      <span className="ml-1 font-normal text-ink-500">{optionSuffix}</span>
                    )}
                  </div>
                  <div className="mt-1.5 max-w-[260px] text-[14px] leading-relaxed text-ink-600">
                    {t(option.desc)}
                  </div>
                </button>
              );
            })}
          </div>
          {!supportsHtmlExport && (
            <div className="mt-4 rounded-sm border border-amber-700/30 bg-amber-50 px-3 py-2 text-[12px] leading-relaxed text-amber-900">
              {t("PDF, PowerPoint, and standalone HTML export need an HTML artifact. This file can still be downloaded in its original format.")}
            </div>
          )}
          {error && (
            <div className="mt-4 rounded-sm border border-red-800/25 bg-red-50 px-3 py-2 text-[12px] leading-relaxed text-red-900">
              {error}
            </div>
          )}
        </div>
        <div className="flex items-center justify-between gap-3 border-t border-ink-300/70 bg-surface-raised px-5 py-4">
          <div className="text-[12px] text-ink-500">
            {selected === "pptx"
              ? t("PowerPoint export uses the configured code editor agent.")
              : translate(language, "Selected export is generated from the current saved artifact.", {
                  format: selected === "original_html" ? originalTitle : t(selectedOption.title),
                })}
          </div>
          <button
            type="button"
            onClick={onDownload}
            disabled={busy || unavailable}
            className="inline-flex h-10 items-center gap-2 rounded-sm bg-ink-900 px-4 text-[12px] font-medium uppercase text-ink-50 transition hover:bg-ink-700 disabled:cursor-wait disabled:opacity-50"
            style={{ letterSpacing: "0.16em" }}
          >
            <I.File width={14} height={14} />
            {busy ? t("Exporting") : t("Download")}
          </button>
        </div>
      </div>
    </div>
  ) : null;

  async function onDownload() {
    if (unavailable || busy) return;
    if (selected === "pptx") {
      setError(null);
      setOpen(false);
      void exportArtifactPptx(artifact.artifact_id).catch((err) => {
        console.error("PowerPoint export failed", err);
      });
      return;
    }
    setBusy(true);
    setError(null);
    try {
      if (selected === "original_html" && originalUrl) {
        triggerBrowserDownload(originalUrl, filenameFromUrl(originalUrl, originalSuffix));
        recordDownload(artifact.artifact_id);
        setOpen(false);
        return;
      }
      const result = await exportArtifactRequest({ artifact, format: selected });
      triggerBrowserDownload(result.url, result.filename);
      recordDownload(artifact.artifact_id);
      setOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : t("Export failed"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button
        type="button"
        onClick={() => {
          setError(null);
          setOpen(true);
        }}
        className={className ?? defaultButtonClass(compact)}
        style={{ letterSpacing: compact ? "0.14em" : "0.16em" }}
        title={t("Download or export this artifact")}
      >
        <I.File width={compact ? 11 : 13} height={compact ? 11 : 13} />
        {t(label)}
      </button>
      {modal ? createPortal(modal, document.body) : null}
    </>
  );
}

export function isArtifactExportDisabled(
  format: ArtifactExportFormat,
  unavailable: boolean,
  busy: boolean,
  pptxExportDisabled: boolean,
): boolean {
  return unavailable || busy || (format === "pptx" && pptxExportDisabled);
}

function defaultButtonClass(compact: boolean) {
  return compact
    ? "inline-flex items-center gap-1.5 rounded-sm border border-ink-300 bg-paper px-2.5 py-1.5 text-[10px] font-medium uppercase text-ink-700 transition hover:border-ink-500 hover:text-ink-900"
    : "inline-flex items-center gap-1.5 rounded-sm border border-ink-300 bg-paper px-3 py-1.5 text-[11px] font-medium uppercase text-ink-700 transition hover:border-ink-500 hover:text-ink-900";
}

function triggerBrowserDownload(url: string, filename: string) {
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.rel = "noreferrer";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}

function filenameFromUrl(url: string, fallbackSuffix = "") {
  try {
    const parsed = new URL(url, window.location.href);
    const last = parsed.pathname.split("/").filter(Boolean).pop();
    const filename = decodeURIComponent(last || "artifact");
    return fallbackSuffix && !filename.toLowerCase().endsWith(fallbackSuffix.toLowerCase())
      ? `${filename}${fallbackSuffix}`
      : filename;
  } catch {
    return `artifact${fallbackSuffix}`;
  }
}

function originalFileFormat(artifact: Artifact, url: string): string | null {
  try {
    const pathname = new URL(url, window.location.href).pathname;
    const match = pathname.match(/\.([a-z0-9]{2,8})$/i);
    if (match?.[1]) return match[1].toLowerCase() === "htm" ? "html" : match[1].toLowerCase();
  } catch {
    // Fall through to artifact metadata for non-URL values.
  }
  return artifact.native_format ?? artifact.view_format ?? null;
}
