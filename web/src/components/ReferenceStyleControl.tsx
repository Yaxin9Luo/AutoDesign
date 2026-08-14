import { translate } from "@/lib/i18n";
import { useApp } from "@/lib/store";
import type { Attachment } from "@/lib/types";
import { I } from "./icons";

export interface ReferencePosterPreview {
  url: string;
  width: number;
  height: number;
}

interface ReferenceStyleControlProps {
  compact?: boolean;
  reference?: Attachment;
  preview?: ReferencePosterPreview | null;
  error?: string | null;
  hasPaperPdf: boolean;
  onChoose: () => void;
  onRemove: () => void;
  onAttachPaperPdf: () => void;
}

export function ReferenceStyleControl({
  compact,
  reference,
  preview,
  error,
  hasPaperPdf,
  onChoose,
  onRemove,
  onAttachPaperPdf,
}: ReferenceStyleControlProps) {
  const language = useApp((state) => state.ui_language);
  const t = (text: string) => translate(language, text);
  const selected = Boolean(reference && preview);
  const label = selected ? t("Replace reference") : t("Reference style");

  return (
    <div className="shrink-0">
      <button
        type="button"
        aria-label={label}
        title={label}
        onClick={onChoose}
        className={compact
          ? `flex h-8 w-8 items-center justify-center overflow-hidden rounded-md border bg-paper/75 transition hover:border-accent/70 ${
              error ? "border-red-600 ring-2 ring-red-500/25" : "border-ink-300/70"
            }`
          : `inline-flex h-8 min-w-0 max-w-[180px] items-center overflow-hidden rounded-md border bg-paper/75 text-left transition hover:border-accent/70 ${
              error ? "border-red-600 ring-2 ring-red-500/25" : "border-ink-300/70"
            }`}
      >
        {compact ? (
          selected ? (
            <img
              src={preview!.url}
              alt=""
              className="h-6 w-6 rounded-sm object-cover"
            />
          ) : (
            <I.Image width={14} height={14} className={error ? "text-red-700" : "text-ink-500"} />
          )
        ) : (
          <>
            <span className="flex h-full w-9 shrink-0 items-center justify-center border-r border-ink-300/55 bg-vellum/80">
              {selected ? (
                <img src={preview!.url} alt="" className="h-6 w-7 object-cover" />
              ) : (
                <I.Image width={14} height={14} />
              )}
            </span>
            <span className="min-w-0 px-2.5 text-[12px] font-semibold text-ink-800">
              {t("Reference style")}
            </span>
          </>
        )}
      </button>

      {(selected || error) && (
        <div className="absolute bottom-full right-0 z-50 mb-2 w-[280px] max-w-[calc(100vw-16px)] rounded-md border border-ink-300/80 bg-surface-raised p-2.5 shadow-page">
          {selected && reference && preview && (
            <>
              <div className="flex min-w-0 gap-2.5">
                <img
                  src={preview.url}
                  alt={reference.name}
                  className="h-20 w-28 shrink-0 rounded-sm border border-ink-300/60 bg-paper object-contain"
                />
                <div className="flex min-w-0 flex-1 flex-col">
                  <span className="truncate text-[12px] font-semibold text-ink-900" title={reference.name}>
                    {reference.name}
                  </span>
                  <span className="mt-0.5 tabular text-[10.5px] text-ink-500">
                    {preview.width} x {preview.height}
                  </span>
                  <div className="mt-auto flex items-center gap-1.5">
                    <button
                      type="button"
                      onClick={onChoose}
                      className="rounded-sm border border-ink-300 bg-paper px-2 py-1 text-[10.5px] font-medium text-ink-800 hover:border-accent/60"
                    >
                      {t("Replace reference")}
                    </button>
                    <button
                      type="button"
                      onClick={onRemove}
                      aria-label={t("Remove reference")}
                      title={t("Remove reference")}
                      className="icon-btn h-7 w-7 text-ink-500 hover:text-red-700"
                    >
                      <I.Trash width={13} height={13} />
                    </button>
                  </div>
                </div>
              </div>
              <p className="mt-2 text-[10.5px] leading-4 text-ink-600">
                {t("Layout from reference; colors from Palette.")}
              </p>
              {!hasPaperPdf && (
                <button
                  type="button"
                  onClick={onAttachPaperPdf}
                  className="mt-1 text-[10.5px] font-semibold text-accent-deep underline decoration-accent/40 underline-offset-2 hover:text-accent"
                >
                  {t("Attach a paper PDF")}
                </button>
              )}
            </>
          )}
          {error && (
            <p
              role="alert"
              className={`${selected ? "mt-2 border-t border-red-200 pt-2" : ""} min-h-4 text-[10.5px] leading-4 text-red-800`}
            >
              {t(error)}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
