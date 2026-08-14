/**
 * Replaces the markdown wall-of-text the chat used to show when a run
 * finished without an artifact (or was cancelled / errored). The card
 * leads with a rescue path — Retry CTA dominant, agent's note collapsed
 * by default, diagnostics deemphasized — because by the time this
 * renders the user has typically been waiting 4+ minutes.
 *
 * Wire: drives off `Message.failure` (mirrored from
 * scripts/web_server.py `Failure` model). The `text` field on the
 * message is preserved as a fallback for very old messages or for
 * accessibility tools.
 */
import { useState } from "react";
import { translate, type UiLanguage } from "@/lib/i18n";
import { useApp } from "@/lib/store";
import type { Message, MessageFailure } from "@/lib/types";
import { I } from "./icons";

interface Props {
  m: Message;
  compact?: boolean;
}

export function FailureCard({ m, compact }: Props) {
  const failure = m.failure;
  const retryRun = useApp((s) => s.retryRun);
  const resumeRun = useApp((s) => s.resumeRun);
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  const [retrying, setRetrying] = useState(false);
  const [noteOpen, setNoteOpen] = useState(false);

  const onResume = async () => {
    setRetrying(true);
    try {
      await resumeRun(m.id);
    } finally {
      setRetrying(false);
    }
  };

  if (!failure) {
    if (isConnectionLossText(m.text)) {
      return (
        <div
          className={`mt-1.5 max-w-[480px] overflow-hidden rounded-md border border-amber-700/30 bg-amber-50/40 ${
            compact ? "text-[12.5px]" : "text-[13px]"
          }`}
          role="status"
          aria-live="polite"
        >
          <div className="flex items-start gap-2.5 border-b border-amber-700/15 px-3.5 py-2.5">
            <I.Alert width={14} height={14} className="mt-0.5 shrink-0 text-amber-800" />
            <div className="min-w-0 flex-1">
            <div className="font-medium text-amber-900">{t("Connection interrupted")}</div>
            <div className="mt-0.5 text-[10.5px] uppercase text-amber-800/80" style={{ letterSpacing: "0.12em" }}>
                {t("Resume this run")}
              </div>
            </div>
          </div>
          <div className="border-b border-amber-700/10 px-3.5 py-3">
            <p className="whitespace-pre-wrap text-[12.5px] leading-relaxed text-ink-700">
              {m.text}
            </p>
          </div>
          <div className="px-3.5 py-2.5">
            <button
              type="button"
              onClick={onResume}
              disabled={retrying}
              className="inline-flex items-center gap-1.5 rounded-md bg-ink-900 px-3 py-1.5 text-[11px] font-medium text-ink-50 transition hover:bg-ink-700 disabled:opacity-60"
              style={{ letterSpacing: "0.04em" }}
            >
              {retrying ? (
                <>
                  <span className="h-1 w-1 animate-pulse rounded-full bg-ink-50" />
                  <span>{t("Resuming...")}</span>
                </>
              ) : (
                <>
                  <I.Refresh width={12} height={12} />
                  <span>{t("Resume run")}</span>
                </>
              )}
            </button>
          </div>
        </div>
      );
    }
    // No structured data — fall back to plain text. Should be rare
    // (only old messages or unexpected wire shapes).
    return (
      <p className={`whitespace-pre-wrap leading-[1.55] text-ink-700 ${compact ? "text-[13px]" : "text-[14.5px]"}`}>
        {m.text}
      </p>
    );
  }

  const cancelled = failure.status === "cancelled";
  const checkpointResumable = Boolean(failure.resume_available) && !cancelled;
  const taskCanResume =
    m.task_type === "poster_code_edit" || m.task_type === "artifact_export_pptx";
  const resumable =
    checkpointResumable ||
    failure.status === "connection_lost" ||
    failure.status === "artifact_delivery_failed" ||
    (!cancelled && taskCanResume);
  const headline = headlineFor(failure, language);
  const errorMessage = errorMessageFor(failure, language);
  const elapsed = failure.elapsed_ms ? formatElapsed(failure.elapsed_ms) : null;
  const suggestedDesigner = failure.suggested_designer ?? failure.suggested_planner;
  const suggestedShort = suggestedDesigner
    ? shortenModelId(suggestedDesigner)
    : null;

  const onRetry = async () => {
    setRetrying(true);
    try {
      if (checkpointResumable) await retryRun(m.id);
      else if (resumable) await resumeRun(m.id);
      else await retryRun(m.id, suggestedDesigner);
    } finally {
      setRetrying(false);
    }
  };

  return (
    <div
      className={`mt-1.5 max-w-[480px] rounded-md border border-amber-700/30 bg-amber-50/40 ${
        compact ? "text-[12.5px]" : "text-[13px]"
      }`}
      role="status"
      aria-live="polite"
    >
      <div className="flex items-start gap-2.5 border-b border-amber-700/15 px-3.5 py-2.5">
        <I.Alert width={14} height={14} className="mt-0.5 shrink-0 text-amber-800" />
        <div className="min-w-0 flex-1">
          <div className="font-medium text-amber-900">{headline}</div>
          <div className="mt-0.5 text-[10.5px] uppercase text-amber-800/80" style={{ letterSpacing: "0.12em" }}>
            <span className="tabular">{failure.status}</span>
            {elapsed && (
              <>
                <span className="mx-1.5 text-amber-700/40">·</span>
                <span className="tabular">{elapsed}</span>
              </>
            )}
            {failure.phase && (
              <>
                <span className="mx-1.5 text-amber-700/40">·</span>
                <span>
                  {failure.status === "max_turns" ? t("stalled at") : t("failed during")}{" "}
                  {failure.phase}
                </span>
              </>
            )}
          </div>
        </div>
      </div>

      {errorMessage && (
        <div className="border-b border-amber-700/10 px-3.5 py-3">
          <p className="whitespace-pre-wrap text-[12.5px] leading-relaxed text-ink-700">
            {errorMessage}
          </p>
          {checkpointResumable && failure.resume_from_attempt != null && (
            <p className="mt-1.5 text-[11.5px] font-medium text-amber-900">
              {translate(language, "Saved checkpoint: attempt {attempt}", {
                attempt: failure.resume_from_attempt,
              })}
              {failure.next_attempt != null
                ? ` · ${translate(language, "next attempt {attempt}", {
                    attempt: failure.next_attempt,
                  })}`
                : ""}
            </p>
          )}
        </div>
      )}

      {failure.agent_last_note && (
        <button
          type="button"
          onClick={() => setNoteOpen((v) => !v)}
          className="group flex w-full items-center justify-between gap-2 border-b border-amber-700/10 bg-amber-50/30 px-3.5 py-2 text-left transition hover:bg-amber-50/60"
          aria-expanded={noteOpen}
        >
          <span className="eyebrow text-amber-800">{t("Run note")}</span>
          <I.ChevronDown
            width={11}
            height={11}
            className="text-amber-700/70 transition"
            style={{ transform: noteOpen ? "rotate(180deg)" : undefined }}
          />
        </button>
      )}
      {failure.agent_last_note && noteOpen && (
        <div className="border-b border-amber-700/10 px-3.5 py-3">
          <p className="whitespace-pre-wrap text-[12.5px] leading-relaxed text-ink-700">
            {failure.agent_last_note}
          </p>
        </div>
      )}

      {failure.produced_files.length > 0 && (
        <div className="border-b border-amber-700/10 px-3.5 py-2.5">
          <div className="eyebrow mb-1.5 text-amber-800">{t("On disk")}</div>
          <ul className="space-y-0.5">
            {failure.produced_files.map((f) => (
              <li
                key={f}
                className="tabular truncate text-[11.5px] text-ink-600"
                title={f}
              >
                {f}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="flex items-center justify-between gap-3 px-3.5 py-2.5">
        {!cancelled ? (
          <button
            type="button"
            onClick={onRetry}
            disabled={retrying}
            className="inline-flex items-center gap-1.5 rounded-md bg-ink-900 px-3 py-1.5 text-[11px] font-medium text-ink-50 transition hover:bg-ink-700 disabled:opacity-60"
            style={{ letterSpacing: "0.04em" }}
          >
            {retrying ? (
              <>
                <span className="h-1 w-1 animate-pulse rounded-full bg-ink-50" />
                <span>{resumable ? t("Resuming...") : t("Repairing...")}</span>
              </>
            ) : (
              <>
                <I.Refresh width={12} height={12} />
                <span>
                  {checkpointResumable && failure.resume_from_attempt != null
                    ? translate(language, "Continue with attempt {attempt}", {
                        attempt:
                          failure.next_attempt ?? failure.resume_from_attempt + 1,
                      })
                    : resumable
                      ? t("Resume run")
                    : suggestedShort ? translate(language, "Repair with {model}", { model: suggestedShort }) : t("Retry / repair")}
                </span>
              </>
            )}
          </button>
        ) : (
          <span className="text-[11.5px] italic text-ink-500">
            {t("You cancelled this run.")}
          </span>
        )}
        <DiagnosticsLink failure={failure} />
      </div>
    </div>
  );
}

function DiagnosticsLink({ failure }: { failure: MessageFailure }) {
  const [open, setOpen] = useState(false);
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  return (
    <div className="relative flex items-center">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="text-[10.5px] uppercase text-amber-800/70 transition hover:text-amber-900"
        style={{ letterSpacing: "0.14em" }}
      >
        {open ? t("Hide diagnostics") : t("Diagnostics")}
      </button>
      {open && (
        <div
          className="absolute right-0 top-full z-10 mt-2 max-h-64 w-[min(440px,calc(100vw-2rem))] overflow-auto rounded-md border border-amber-700/30 bg-paper p-3 shadow-page"
        >
          <code className="tabular block whitespace-pre-wrap text-[10.5px] text-ink-600">
            {failure.status}
            {failure.phase ? ` · ${failure.phase}` : ""}
            {failure.elapsed_ms ? ` · ${formatElapsed(failure.elapsed_ms)}` : ""}
            {failure.error_code ? `\n${failure.error_code}` : ""}
            {failure.run_id ? `\nrun ${failure.run_id}` : ""}
            {failure.error_message ? `\n${failure.error_message}` : ""}
            {failure.error_detail ? `\n\nAuthor output:\n${failure.error_detail}` : ""}
            {failure.resume_available && failure.resume_from_attempt != null
              ? `\ncheckpoint attempt ${failure.resume_from_attempt}${
                  failure.next_attempt != null
                    ? ` · next attempt ${failure.next_attempt}`
                    : ""
                }`
              : ""}
          </code>
        </div>
      )}
    </div>
  );
}

// ---- helpers ----

function headlineFor(f: MessageFailure, language: UiLanguage): string {
  const t = (text: string) => translate(language, text);
  if (f.error_code === "provider_rate_limit") {
    return t("Provider rate limit interrupted authoring");
  }
  if (f.status === "connection_lost") return t("Connection interrupted");
  if (f.status === "artifact_delivery_failed") return t("Artifact delivery unavailable");
  if (f.status === "run_status_unavailable") return t("Run status unavailable");
  if (f.status === "cancelled") return t("Run cancelled");
  if (f.status === "max_turns") {
    return `${t("Run stalled at")} ${f.phase ?? "the designer"}`;
  }
  if (f.status === "error") return t("Run errored");
  return `${t("Run ended")} (${f.status})`;
}

function errorMessageFor(f: MessageFailure, language: UiLanguage): string | undefined {
  if (f.error_code === "provider_rate_limit") {
    return translate(
      language,
      "The coding-harness provider rejected the request because its per-minute rate limit was exceeded. Wait briefly, then resume from the saved checkpoint.",
    );
  }
  return f.error_message;
}

function isConnectionLossText(text: string): boolean {
  const t = text.toLowerCase();
  return t.includes("event stream closed") || t.includes("backend may have crashed");
}

function formatElapsed(ms: number): string {
  const total = Math.floor(ms / 1000);
  if (total < 60) return `${total}s`;
  const m = Math.floor(total / 60);
  const s = total - m * 60;
  return s === 0 ? `${m}m` : `${m}m ${s}s`;
}

/** "anthropic/claude-opus-4-7" → "Claude Opus 4.7"; "moonshotai/kimi-k2.6" →
 *  "Kimi K2.6". Falls back to the bare slug if not recognized. */
function shortenModelId(id: string): string {
  const slug = id.includes("/") ? id.slice(id.lastIndexOf("/") + 1) : id;
  const known: Record<string, string> = {
    "claude-opus-4-7": "Claude Opus 4.7",
    "claude-sonnet-4-6": "Claude Sonnet 4.6",
    "kimi-k2.6": "Kimi K2.6",
    "gemini-2.5-pro": "Gemini 2.5 Pro",
    "gemini-3.1-pro-preview": "Gemini 3.1 Pro Preview",
    "google/gemini-3.1-pro-preview": "Gemini 3.1 Pro Preview",
    "gpt-5": "GPT-5",
  };
  return known[slug] ?? slug;
}
