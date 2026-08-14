import { useEffect, useState } from "react";
import { useApp } from "@/lib/store";
import {
  type ActivityEvent,
  PHASES,
  POSTER_CODE_EDIT_PHASES,
  type RunProgress,
  type StepStatus,
} from "@/lib/progress";
import { translate } from "@/lib/i18n";
import { useElapsed } from "@/lib/elapsed";
import { I } from "./icons";

/**
 * Replaces the old three-dots-and-"Thinking…" bubble with a multi-pane
 * card: phase pipeline (left), current sub-step + activity tail
 * (right). Compact mode strips the activity tail for the canvas rail.
 *
 * Reads `state.runs_progress[<current_conv_id>]` so each conversation
 * gets its own card (parallel runs across conversations are first-
 * class). The Cancel button calls `cancelRun()` and keeps the live
 * stream visible until the backend confirms cancellation.
 */
export function ProgressCard({ compact }: { compact?: boolean }) {
  const cid = useApp((s) => s.current_conversation_id);
  const progress = useApp((s) => s.runs_progress[cid]);
  const cancelRun = useApp((s) => s.cancelRun);
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);

  if (!progress) {
    // Fallback for the brief moment between user pressing Send and the
    // first SSE event landing — shows the legacy three dots.
    return <DotsFallback compact={compact} />;
  }

  const onCancel = () => {
    if (window.confirm(t("Cancel this run? Work done so far is discarded."))) {
      void cancelRun(cid);
    }
  };

  return (
    <div
      className={`overflow-hidden rounded-md border border-ink-300/65 bg-surface-raised/86 shadow-soft ${compact ? "" : "p-0"}`}
    >
      <Header progress={progress} compact={compact} onCancel={onCancel} />
      {!compact && progress.runtime_alert && (
        <RuntimeAlertPanel alert={progress.runtime_alert} />
      )}
      {compact ? (
        <CompactBody progress={progress} />
      ) : (
        <FullBody progress={progress} />
      )}
    </div>
  );
}

function RuntimeAlertPanel({
  alert,
}: {
  alert: NonNullable<RunProgress["runtime_alert"]>;
}) {
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  return (
    <div
      className="border-b border-amber-700/20 bg-amber-50/55 px-4 py-3"
      role="alert"
      aria-live="assertive"
    >
      <div className="flex items-start gap-2.5">
        <I.Alert width={15} height={15} className="mt-0.5 shrink-0 text-amber-800" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            {alert.status_code && (
              <span
                className="tabular text-[10px] font-semibold uppercase text-amber-800"
                style={{ letterSpacing: "0.12em" }}
              >
                {alert.status_code}
              </span>
            )}
            <span className="font-medium text-[12.5px] text-amber-950">
              {t(alert.title)}
            </span>
          </div>
          <p className="mt-1 break-words text-[12px] leading-relaxed text-ink-800">
            {alert.message}
          </p>
          <p className="mt-1 text-[11.5px] leading-relaxed text-amber-900">
            {t(alert.hint)}
          </p>
          <details className="mt-2">
            <summary className="cursor-pointer select-none text-[10.5px] font-medium uppercase text-amber-800/80 hover:text-amber-950">
              {t("Show technical details")}
            </summary>
            <pre className="mt-1.5 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-sm border border-amber-700/20 bg-paper/80 px-2.5 py-2 font-mono text-[10.5px] leading-relaxed text-ink-700">
              {alert.technical_detail}
            </pre>
          </details>
        </div>
      </div>
    </div>
  );
}

function Header({
  progress,
  compact,
  onCancel,
}: {
  progress: RunProgress;
  compact?: boolean;
  onCancel?: () => void;
}) {
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  const elapsed = useElapsed(progress.started_at);
  const isError = progress.phase === "error";
  const isCancelling = progress.phase === "cancelling";
  const cancelRequestInFlight = progress.cancel_request_in_flight === true;
  // "Live" = the run is still in flight. Both queued and running count
  // because the queued→running transition can happen on the *first*
  // event we receive (we may have raced and missed `run.start`); don't
  // hide Cancel during that micro-window.
  const isLive = progress.phase === "queued"
    || progress.phase === "running"
    || progress.phase === "cancelling";
  return (
    <div
      className={`flex items-center justify-between gap-3 border-b border-ink-300/50 px-4 py-2.5 ${compact ? "py-2.5 text-[12px]" : ""}`}
    >
      <div className="flex min-w-0 items-center gap-2.5">
        <span
          className={`relative h-1.5 w-1.5 shrink-0 rounded-full ${isError ? "bg-amber-600" : "bg-accent"}`}
        >
          {isLive && !isError && (
            <span className="absolute inset-0 animate-ping rounded-full bg-accent/45" />
          )}
        </span>
        <span
          className="truncate font-display text-[14px] text-ink-900"
          style={{ fontVariationSettings: '"opsz" 36' }}
        >
          {t(progress.label)}
        </span>
        {progress.current_step && (
          <span className="truncate text-[11px] italic text-ink-500">
            {t(progress.current_step)}…
          </span>
        )}
      </div>
      <div className="flex shrink-0 items-baseline gap-3.5 text-[10px] text-ink-500" style={{ letterSpacing: "0.06em" }}>
        {!compact && progress.counts.attempts > 0 && (
          <span className="tabular uppercase" title="Author attempts">
            {t("attempt")} {progress.counts.attempts}
          </span>
        )}
        {!compact && progress.counts.tool_calls > 0 && (
          <span className="tabular uppercase" title="Tool calls">
            {progress.counts.tool_calls} {t("steps")}
          </span>
        )}
        {!compact && progress.counts.images_started > 0 && (
          <span className="tabular" title="Images">
            <span className="uppercase">img</span> {progress.counts.images_done}/{progress.counts.images_started}
          </span>
        )}
        {progress.counts.warnings > 0 && (
          <span className="tabular text-amber-700" title="Warnings">
            ⚠ {progress.counts.warnings}
          </span>
        )}
        <span className="tabular font-mono text-ink-700">{elapsed}</span>
        {isLive && onCancel && (
          <button
            type="button"
            onClick={onCancel}
            disabled={cancelRequestInFlight}
            className="inline-flex items-center gap-1 rounded-md border border-ink-300/70 bg-paper/80 px-2 py-0.5 text-[10px] font-medium uppercase text-ink-600 transition hover:border-amber-500 hover:bg-amber-50 hover:text-amber-900 disabled:cursor-wait disabled:opacity-70"
            style={{ letterSpacing: "0.16em" }}
            title={t(
              cancelRequestInFlight
                ? "Cancellation pending"
                : isCancelling
                  ? "Retry cancellation"
                  : "Cancel this run",
            )}
          >
            <I.Close width={9} height={9} />
            {t(
              cancelRequestInFlight
                ? "Cancelling…"
                : isCancelling
                  ? "Retry cancellation"
                  : "Cancel",
            )}
          </button>
        )}
      </div>
    </div>
  );
}

function FullBody({ progress }: { progress: RunProgress }) {
  return (
    <div className="grid grid-cols-[156px_1fr]">
      <Pipeline progress={progress} />
      <div className="min-w-0">
        <ActivityTail events={progress.recent} />
      </div>
    </div>
  );
}

function CompactBody({ progress }: { progress: RunProgress }) {
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  // In the rail we only have ~280px wide. Drop the tail; show the
  // pipeline as a single thin progress rule so it fits in one row.
  return (
    <div className="flex flex-col gap-2.5 px-4 py-3">
      <PipelineHorizontal progress={progress} />
      {progress.recent[0] && (
        <div className="line-clamp-1 text-[11px] text-ink-500">
          <span className="text-ink-700">{progress.recent[0].label}</span>
          {progress.recent[0].detail && (
            <span className="ml-1.5 italic">· {t(progress.recent[0].detail)}</span>
          )}
          {progress.attempt_previews.length > 0 && (
            <span className="ml-1.5 text-ink-400">
              · {progress.attempt_previews.length} previews
            </span>
          )}
        </div>
      )}
    </div>
  );
}

// ---------- pipeline ----------

function Pipeline({ progress }: { progress: RunProgress }) {
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  const visible = phaseCatalog(progress).filter((p) => !p.conditional || p.id in progress.phases);
  return (
    <ol className="border-r border-ink-300/50 bg-vellum/45 px-4 py-3">
      {visible.map((p, i) => {
        const status = progress.phases[p.id] ?? "pending";
        return (
          <li key={p.id} className="flex items-baseline gap-2 py-1">
            <span
              className="tabular w-5 shrink-0 text-[9.5px] text-ink-400"
              style={{ letterSpacing: "0.06em" }}
            >
              {String(i + 1).padStart(2, "0")}
            </span>
            <StatusDot status={status} />
            <span className={statusTextClass(status)}>
              {t(p.label)}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

function PipelineHorizontal({ progress }: { progress: RunProgress }) {
  const visible = phaseCatalog(progress).filter((p) => !p.conditional || p.id in progress.phases);
  const doneCount = visible.filter((x) => progress.phases[x.id] === "done").length;
  const totalCount = visible.length;
  // Single thin progress rule replaces the segmented bar — calmer, less
  // "loading bar from 2014".
  const pct = totalCount > 0 ? Math.round((doneCount / totalCount) * 100) : 0;
  const hasError = visible.some((p) => progress.phases[p.id] === "warning");
  return (
    <div className="flex items-center gap-2.5">
      <div className="relative h-px flex-1 bg-ink-200">
        <div
          className={`absolute left-0 top-0 h-px transition-all ${hasError ? "bg-amber-600" : "bg-accent"}`}
          style={{ width: `${pct}%`, transitionTimingFunction: "cubic-bezier(0.2, 0.8, 0.2, 1)", transitionDuration: "320ms" }}
        />
      </div>
      <span
        className="tabular text-[9.5px] uppercase text-ink-500"
        style={{ letterSpacing: "0.16em" }}
      >
        {doneCount}/{totalCount}
      </span>
    </div>
  );
}

function StatusDot({ status }: { status: StepStatus }) {
  const cls =
    status === "done" ? "border-accent bg-accent text-white"
    : status === "in_progress" ? "border-accent text-accent-deep"
    : status === "warning" ? "border-amber-700 text-amber-800"
    : status === "skipped" ? "border-ink-300 text-ink-400"
    : "border-ink-300 text-ink-400";
  return (
    <span
      className={`inline-flex h-3 w-3 shrink-0 items-center justify-center rounded-full border ${cls}`}
    >
      {status === "done" && <I.Check width={8} height={8} />}
      {status === "in_progress" && (
        <span className="h-[3px] w-[3px] animate-pulse rounded-full bg-accent" />
      )}
      {status === "warning" && (
        <span className="text-[8px] font-bold leading-none">!</span>
      )}
      {status === "skipped" && (
        <span className="text-[8px] leading-none">·</span>
      )}
    </span>
  );
}

function phaseCatalog(progress: RunProgress) {
  return progress.mode === "poster_code_edit" ? POSTER_CODE_EDIT_PHASES : PHASES;
}

function statusTextClass(status: StepStatus): string {
  // Editorial type for the pipeline list — eyebrow-style for active, lighter for done/pending.
  if (status === "done") return "text-[10.5px] uppercase text-ink-700";
  if (status === "in_progress") return "text-[10.5px] uppercase font-medium text-ink-900";
  if (status === "warning") return "text-[10.5px] uppercase text-amber-800";
  if (status === "skipped") return "text-[10.5px] uppercase text-ink-400 line-through";
  return "text-[10.5px] uppercase text-ink-400";
}

// ---------- activity tail ----------

function ActivityTail({ events }: { events: ActivityEvent[] }) {
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  if (events.length === 0) {
    return (
      <div className="flex items-center justify-center px-4 py-6 text-[11px] italic text-ink-500">
        {t("Waiting for the first agent step…")}
      </div>
    );
  }
  return (
    <ol className="max-h-[360px] space-y-2 overflow-y-auto px-4 py-3">
      {events.map((e, idx) => (
        <ActivityRow key={e.ts + e.raw_event + idx} ev={e} />
      ))}
    </ol>
  );
}

function ActivityRow({ ev }: { ev: ActivityEvent }) {
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  const ago = useRelativeTime(ev.ts);
  const cls =
    ev.category === "warning" ? "text-amber-800"
    : ev.category === "error" ? "text-amber-900"
    : ev.category === "phase" ? "text-ink-900"
    : "text-ink-700";
  return (
    <li className="rounded-md border border-ink-200/80 bg-paper/55 px-3 py-2 text-[11px] shadow-[0_1px_0_rgba(38,31,24,0.03)]">
      <div className="flex items-start gap-2.5">
        <span className="tabular w-10 shrink-0 pt-0.5 font-mono text-[10px] text-ink-400">
          {ago === "now" ? t("now") : ago}
        </span>
        <div className={`min-w-0 flex-1 ${cls}`}>
          <div className="flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5">
            <span className="font-medium">{t(ev.label)}</span>
            {ev.detail && (
              <span className="italic text-ink-500">· {t(ev.detail)}</span>
            )}
          </div>
          {ev.body && (
            <pre className="mt-1.5 max-h-44 overflow-y-auto whitespace-pre-wrap rounded-sm border border-ink-200/70 bg-vellum/70 px-2 py-1.5 font-sans text-[10.5px] leading-relaxed text-ink-700">
              {ev.body}
            </pre>
          )}
          {ev.preview_url && (
            <a
              href={ev.html_url ?? ev.preview_url}
              target="_blank"
              rel="noreferrer"
              className="mt-2 block w-44 overflow-hidden rounded-md border border-ink-300/65 bg-vellum transition hover:border-accent/60"
            >
              <img
                src={ev.preview_url}
                alt={ev.label}
                className="aspect-[2/1] w-full object-cover"
                loading="lazy"
              />
            </a>
          )}
        </div>
      </div>
    </li>
  );
}

function useRelativeTime(ts: number): string {
  // Re-render every 5s for the visible tail; keeps the "12s ago" labels
  // honest without burning RAF cycles.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 5000);
    return () => window.clearInterval(id);
  }, []);
  const d = Math.max(0, Math.floor((now - ts) / 1000));
  if (d < 5) return "now";
  if (d < 60) return `${d}s`;
  const m = Math.floor(d / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h`;
}

// ---------- pre-first-event fallback ----------

function DotsFallback({ compact }: { compact?: boolean }) {
  const language = useApp((s) => s.ui_language);
  return (
    <div
      className={`rounded-md border border-ink-300/65 bg-surface-raised/86 px-4 py-3 text-[13px] italic text-ink-500 shadow-soft ${compact ? "px-3 py-2 text-[12px]" : ""}`}
    >
      <span className="inline-flex items-center gap-1">
        <span className="h-1 w-1 animate-pulse rounded-full bg-ink-500" />
        <span
          className="h-1 w-1 animate-pulse rounded-full bg-ink-500"
          style={{ animationDelay: "120ms" }}
        />
        <span
          className="h-1 w-1 animate-pulse rounded-full bg-ink-500"
          style={{ animationDelay: "240ms" }}
        />
        <span className="ml-2">{translate(language, "Connecting to agent…")}</span>
      </span>
    </div>
  );
}
