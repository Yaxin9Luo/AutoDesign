import type { SVGProps } from "react";
import {
  derivePaperBundleStatus,
  paperBundleBlocksAttemptActions,
  PAPER_BUNDLE_ARTIFACT_ORDER,
} from "@/lib/paper_bundle";
import { useApp } from "@/lib/store";
import type {
  Artifact,
  ArtifactType,
  PaperBundleParentState,
  PaperBundleStatus,
  PaperBundleTask,
} from "@/lib/types";
import { translate, type UiLanguage } from "@/lib/i18n";
import type { RunProgress } from "@/lib/progress";
import type { RunAttemptState } from "@/lib/attempt_candidates";
import { useElapsed } from "@/lib/elapsed";
import { ArtifactDownloadMenu } from "./ArtifactDownloadMenu";
import { CompactAttemptDock } from "./CompactAttemptDock";
import { I } from "./icons";

const ARTIFACT_LABELS: Record<ArtifactType, string> = {
  poster: "Poster",
  deck: "Deck",
  landing: "Landing",
  video: "Video",
};

const ARTIFACT_ICONS: Record<
  ArtifactType,
  (props: SVGProps<SVGSVGElement>) => JSX.Element
> = {
  poster: I.Poster,
  deck: I.Deck,
  landing: I.Layout,
  video: I.Video,
};

const STATUS_LABELS: Record<PaperBundleStatus, string> = {
  running: "Running",
  cancelling: "Cancelling",
  complete: "Complete",
  partial: "Partially complete",
  failed: "Failed",
  cancelled: "Cancelled",
};

const TASK_STATUS_LABELS: Record<PaperBundleTask["status"], string> = {
  pending: "Pending",
  uploading: "Uploading",
  running: "Running",
  cancelling: "Cancelling",
  complete: "Ready",
  failed: "Failed",
  cancelled: "Cancelled",
};

export function PaperBundleCard({
  bundle,
  compact = false,
}: {
  bundle: PaperBundleParentState;
  compact?: boolean;
}) {
  const parentConversationId = useApp((s) => s.current_conversation_id);
  const conversations = useApp((s) => s.conversations);
  const runsProgress = useApp((s) => s.runs_progress);
  const runAttempts = useApp((s) => s.run_attempts);
  const enterCanvas = useApp((s) => s.enterCanvas);
  const cancelPaperBundle = useApp((s) => s.cancelPaperBundle);
  const cancelPaperBundleTask = useApp((s) => s.cancelPaperBundleTask);
  const retryPaperBundleTask = useApp((s) => s.retryPaperBundleTask);
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  const backendStatus: PaperBundleStatus = bundle.backend_state === undefined
    ? derivePaperBundleStatus(bundle.tasks)
    : bundle.backend_state === "reserved"
      ? "running"
      : bundle.backend_state === "completed"
        ? "complete"
        : bundle.backend_state;
  const hasAvailableOutput = PAPER_BUNDLE_ARTIFACT_ORDER.some((artifactType) => {
    const task = bundle.tasks[artifactType];
    const attemptRunId = task.authoring_run_id ?? task.run_id;
    return Boolean(
      task.artifact_id && conversations[parentConversationId]?.artifacts[task.artifact_id],
    ) || Boolean(attemptRunId && runAttempts[attemptRunId]?.candidates.length);
  });
  const hasPendingAttemptHydration = PAPER_BUNDLE_ARTIFACT_ORDER.some((artifactType) => {
    const task = bundle.tasks[artifactType];
    const attemptRunId = task.authoring_run_id ?? task.run_id;
    const attemptState = attemptRunId ? runAttempts[attemptRunId] : undefined;
    const artifact = task.artifact_id
      ? conversations[parentConversationId]?.artifacts[task.artifact_id]
      : undefined;
    return (task.status === "failed" || task.status === "cancelled")
      && Boolean(attemptRunId)
      && !artifact
      && (attemptState === undefined || attemptState.loading === true);
  });
  const hasAttemptHydrationFailure = PAPER_BUNDLE_ARTIFACT_ORDER.some((artifactType) => {
    const task = bundle.tasks[artifactType];
    const attemptRunId = task.authoring_run_id ?? task.run_id;
    const attemptState = attemptRunId ? runAttempts[attemptRunId] : undefined;
    return task.status === "failed"
      && Boolean(attemptRunId)
      && !task.artifact_id
      && Boolean(attemptState?.error)
      && !attemptState?.candidates.length;
  });
  const status: PaperBundleStatus = (
    ((backendStatus === "failed" || backendStatus === "cancelled") && hasAvailableOutput)
    || (backendStatus === "failed" && (hasPendingAttemptHydration || hasAttemptHydrationFailure))
  ) ? "partial" : backendStatus;
  const cancellationRequestInFlight = bundle.cancel_request_in_flight === true;
  const readyCount = PAPER_BUNDLE_ARTIFACT_ORDER.filter(
    (artifactType) => bundle.tasks[artifactType].status === "complete",
  ).length;
  const aggregateStatus = `${t("Paper All-in-One")}: ${t(STATUS_LABELS[status])}. ${translate(
    language,
    "{count} of 4 ready",
    { count: readyCount },
  )}.`;

  const onCancelAll = () => {
    if (window.confirm(t("Cancel all running paper bundle tasks? Completed artifacts will be kept."))) {
      void cancelPaperBundle(parentConversationId);
    }
  };

  const onCancelTask = (artifactType: ArtifactType) => {
    if (window.confirm(t("Cancel this task? Other tasks will keep running."))) {
      void cancelPaperBundleTask(parentConversationId, artifactType);
    }
  };
  const onRetryTask = (artifactType: ArtifactType) => {
    void retryPaperBundleTask(parentConversationId, artifactType);
  };

  return (
    <section
      className="overflow-hidden rounded-md border border-ink-300/70 bg-surface-raised/88 shadow-soft"
      aria-label={t("Paper All-in-One")}
    >
      <span className="sr-only" role="status" aria-live="polite" aria-atomic="true">
        {aggregateStatus}
      </span>
      <header className={`min-h-16 border-b border-ink-300/60 px-4 py-3 ${
        compact
          ? "flex flex-col items-stretch gap-2"
          : "flex flex-wrap items-center justify-between gap-3"
      }`}>
        <div className={`flex min-w-0 items-center gap-3 ${compact ? "w-full" : "flex-1"}`}>
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-ink-300/70 bg-paper text-ink-700">
            <I.File width={16} height={16} />
          </span>
          <div className="min-w-0">
            <div className="truncate font-display text-[14px] font-medium text-ink-900">
              {bundle.source_name}
            </div>
            <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px] font-medium uppercase text-ink-500">
              <span style={{ letterSpacing: "0.14em" }}>{t("Paper All-in-One")}</span>
              <span aria-hidden className="h-3 w-px bg-ink-300" />
              <span className="tabular" style={{ letterSpacing: "0.1em" }}>
                {translate(language, "{count} of 4 ready", { count: readyCount })}
              </span>
            </div>
          </div>
        </div>
        <div className={`flex shrink-0 items-center gap-2 ${compact ? "justify-between" : ""}`}>
          <BundleStatus status={status} language={language} />
          {(status === "running" || status === "cancelling") && (
            <button
              type="button"
              onClick={onCancelAll}
              disabled={cancellationRequestInFlight}
              aria-label={t(cancellationRequestInFlight
                ? "Cancellation pending"
                : status === "cancelling" ? "Retry cancellation" : "Cancel all running tasks")}
              className="inline-flex items-center gap-1.5 rounded-sm border border-amber-700/35 bg-paper px-2.5 py-1.5 text-[10px] font-medium uppercase text-amber-900 transition hover:bg-amber-50 disabled:cursor-wait disabled:opacity-70"
              style={{ letterSpacing: "0.12em" }}
              title={t(cancellationRequestInFlight
                ? "Cancellation pending"
                : status === "cancelling" ? "Retry cancellation" : "Cancel all running tasks")}
            >
              <I.Close width={10} height={10} />
              {t(cancellationRequestInFlight
                ? "Cancelling"
                : status === "cancelling" ? "Retry cancellation" : "Cancel all")}
            </button>
          )}
        </div>
      </header>

      {bundle.cancel_error && (
        <div className="border-b border-amber-700/20 bg-amber-50 px-4 py-2 text-[11px] text-amber-950" role="alert">
          {t(bundle.cancel_error)}
        </div>
      )}

      <div className={compact ? "grid grid-cols-1" : "grid grid-cols-1 md:grid-cols-2"}>
        {PAPER_BUNDLE_ARTIFACT_ORDER.map((artifactType) => {
          const task = bundle.tasks[artifactType];
          const attemptRunId = task.authoring_run_id ?? task.run_id;
          const artifact = resolveTaskArtifact(
            task,
            conversations[parentConversationId]?.artifacts,
          );
          const attemptState = attemptRunId ? runAttempts[attemptRunId] : undefined;
          return (
            <ArtifactTaskRow
              key={artifactType}
              task={task}
              artifact={artifact}
              attemptState={attemptState}
              progress={runsProgress[task.child_conversation_id]}
              compact={compact}
              language={language}
              pptxExportDisabled={status === "running" || status === "cancelling"}
              retryEnabled={!bundle.job_id}
              attemptRunId={attemptRunId}
              attemptActionsDisabled={Boolean(
                attemptRunId
                && paperBundleBlocksAttemptActions(bundle, attemptRunId)
              )}
              onOpen={() => artifact && enterCanvas(artifact.artifact_id)}
              onCancel={() => onCancelTask(artifactType)}
              onRetry={() => onRetryTask(artifactType)}
            />
          );
        })}
      </div>
    </section>
  );
}

function ArtifactTaskRow({
  task,
  artifact,
  attemptState,
  progress,
  compact,
  language,
  pptxExportDisabled,
  retryEnabled,
  attemptRunId,
  attemptActionsDisabled,
  onOpen,
  onCancel,
  onRetry,
}: {
  task: PaperBundleTask;
  artifact: Artifact | undefined;
  attemptState: RunAttemptState | undefined;
  progress: RunProgress | undefined;
  compact: boolean;
  language: UiLanguage;
  pptxExportDisabled: boolean;
  retryEnabled: boolean;
  attemptRunId?: string;
  attemptActionsDisabled: boolean;
  onOpen: () => void;
  onCancel: () => void;
  onRetry: () => void;
}) {
  const t = (text: string) => translate(language, text);
  const Icon = ARTIFACT_ICONS[task.artifact_type];
  const candidates = attemptState?.candidates ?? [];
  const attempts = progress?.counts.attempts ?? task.attempts;
  const maxAttempts = progress?.counts.max_attempts ?? task.max_attempts;
  const hasAttempts = candidates.length > 0;
  const hasPublishableAttempt = candidates.some(
    (candidate) => candidate.safety_state !== "blocked",
  );
  const terminalFailure = task.status === "failed" || task.status === "cancelled";
  const checkingAttempts = terminalFailure
    && !artifact
    && Boolean(attemptRunId)
    && (attemptState === undefined || attemptState.loading === true);
  const attemptHydrationFailed = terminalFailure
    && !artifact
    && Boolean(attemptRunId)
    && Boolean(attemptState?.error)
    && !hasAttempts;
  const degradedArtifact = artifact?.quality_status === "ready_with_warnings";
  const qualityDiagnostics = artifact?.quality_diagnostics?.filter(Boolean) ?? [];
  const recoveredFromTerminalFailure = terminalFailure
    && (Boolean(artifact) || hasPublishableAttempt);
  const blockedDraftsFromTerminalFailure = terminalFailure
    && !artifact
    && hasAttempts
    && !hasPublishableAttempt;
  const blockerMessages = Array.from(new Set(
    candidates.flatMap((candidate) => (
      candidate.hard_blockers.map((blocker) => blocker.message.trim())
    )).filter(Boolean),
  ));
  const detail = degradedArtifact
    ? t("Needs refinement")
    : attemptHydrationFailed
    ? t("Attempt history unavailable")
    : checkingAttempts
    ? t("Checking attempts")
    : blockedDraftsFromTerminalFailure
    ? t("Needs refinement")
    : recoveredFromTerminalFailure
    ? `${t("Attempt")} ${t("Available")}`
    : progress?.current_step
    || progress?.label
    || TASK_STATUS_LABELS[task.status];
  const terminal = task.status !== "pending"
    && task.status !== "uploading"
    && task.status !== "running"
    && task.status !== "cancelling";
  const elapsed = useElapsed(
    task.started_at ?? progress?.started_at,
    terminal ? task.finished_at : undefined,
  );

  return (
    <article className={`flex min-w-0 flex-col border-ink-300/55 ${
      compact
        ? "min-h-[164px] border-b p-3 last:border-b-0"
        : "min-h-[252px] border-b p-4 last:border-b-0 md:border-r md:[&:nth-child(2n)]:border-r-0 md:[&:nth-last-child(-n+2)]:border-b-0"
    }`}>
      <div className={`flex items-start justify-between gap-3 ${compact ? "min-h-8" : "min-h-10"}`}>
        <div className="flex min-w-0 items-center gap-2.5">
          <span className={`flex shrink-0 items-center justify-center rounded-md bg-vellum text-ink-700 ${compact ? "h-7 w-7" : "h-8 w-8"}`}>
            <Icon width={15} height={15} />
          </span>
          <div className="min-w-0">
            <div className="font-display text-[13px] font-medium text-ink-900">
              {t(ARTIFACT_LABELS[task.artifact_type])}
            </div>
            <div className="mt-0.5 flex min-h-4 flex-wrap items-center gap-x-2 text-[10px] text-ink-500">
              <span className="max-w-[220px] truncate">{t(detail)}</span>
              {(elapsed || (typeof attempts === "number" && attempts > 0)) && (
                <span className="inline-flex shrink-0 items-center gap-2">
                  {typeof attempts === "number" && attempts > 0 && (
                    <span className="tabular">
                      {maxAttempts
                        ? translate(language, "Attempt {current} of {total}", {
                            current: attempts,
                            total: maxAttempts,
                          })
                        : translate(language, "Attempt {current}", {
                            current: attempts,
                          })}
                    </span>
                  )}
                  {elapsed && typeof attempts === "number" && attempts > 0 && (
                    <span aria-hidden>·</span>
                  )}
                  {elapsed && (
                    <span className="tabular font-mono text-ink-600">
                      {elapsed}
                    </span>
                  )}
                </span>
              )}
            </div>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {(task.status === "pending"
            || task.status === "uploading"
            || task.status === "running"
            || (task.status === "cancelling" && Boolean(task.error))) && (
            <button
              type="button"
              onClick={onCancel}
              className="inline-flex h-6 items-center gap-1 rounded-sm border border-amber-700/30 bg-paper px-1.5 text-[9px] font-medium uppercase text-amber-900 transition hover:bg-amber-50"
              style={{ letterSpacing: "0.08em" }}
              title={t(task.status === "cancelling" ? "Retry cancellation" : "Cancel this task")}
            >
              <I.Close width={9} height={9} />
              {t(task.status === "cancelling" ? "Retry cancellation" : "Cancel")}
            </button>
          )}
          {retryEnabled
            && !checkingAttempts
            && !attemptHydrationFailed
            && (task.status === "failed" || task.status === "cancelled")
            && task.run_id && (
            <button
              type="button"
              onClick={onRetry}
              className="inline-flex h-6 items-center gap-1 rounded-sm border border-accent/35 bg-paper px-1.5 text-[9px] font-medium uppercase text-accent-deep transition hover:bg-accent-soft"
              style={{ letterSpacing: "0.08em" }}
              title={t("Retry this task")}
            >
              <I.Refresh width={9} height={9} />
              {t("Retry")}
            </button>
          )}
          <TaskStatus
            status={task.status}
            language={language}
            available={recoveredFromTerminalFailure}
            needsRefinement={blockedDraftsFromTerminalFailure || degradedArtifact}
            checking={checkingAttempts}
            attemptsUnavailable={attemptHydrationFailed}
          />
        </div>
      </div>

      {artifact && (
        <ArtifactPreview artifact={artifact} compact={compact} />
      )}

      {!artifact && (
        <div className={`flex ${compact ? "mt-2 h-14" : "mt-3 h-28"} items-center justify-center border-y border-ink-200/80 bg-vellum/45 text-[10px] uppercase text-ink-400`} style={{ letterSpacing: "0.12em" }}>
          {blockedDraftsFromTerminalFailure
            ? t("Needs refinement")
            : attemptHydrationFailed
            ? t("Attempt history unavailable")
            : checkingAttempts
            ? t("Checking attempts")
            : recoveredFromTerminalFailure
            ? `${t("Attempt")} ${t("Available")}`
            : task.status === "failed" || task.status === "cancelled"
            ? t(TASK_STATUS_LABELS[task.status])
            : t("Preparing artifact")}
        </div>
      )}

      <CompactAttemptDock
        runId={attemptRunId}
        conversationId={task.child_conversation_id}
        pending={task.status === "pending"
          || task.status === "uploading"
          || task.status === "running"
          || task.status === "cancelling"}
        finalized={Boolean(artifact)}
        actionsDisabled={attemptActionsDisabled}
      />

      <div className={`mt-auto flex flex-wrap items-end justify-between gap-2 ${compact ? "min-h-8 pt-2" : "min-h-9 pt-3"}`}>
        {degradedArtifact ? (
          <details className="min-w-0 flex-1 text-[11px] leading-snug text-amber-900">
            <summary className="cursor-pointer font-medium">
              {t("Needs refinement")}
            </summary>
            {qualityDiagnostics.length > 0 && (
              <ul className="mt-1 list-disc space-y-0.5 pl-4">
                {qualityDiagnostics.map((message) => (
                  <li key={message}>{message}</li>
                ))}
              </ul>
            )}
          </details>
        ) : blockedDraftsFromTerminalFailure ? (
          <details className="min-w-0 flex-1 text-[11px] leading-snug text-amber-900">
            <summary className="cursor-pointer font-medium">
              {t("Attempts need refinement")}
            </summary>
            {blockerMessages.length > 0 ? (
              <ul className="mt-1 list-disc space-y-0.5 pl-4">
                {blockerMessages.map((message) => (
                  <li key={message}>{message}</li>
                ))}
              </ul>
            ) : task.error ? (
              <div className="mt-1">{task.error}</div>
            ) : null}
          </details>
        ) : checkingAttempts ? (
          <span />
        ) : task.error ? (
          recoveredFromTerminalFailure ? (
            <details className="min-w-0 flex-1 text-[11px] leading-snug text-ink-600">
              <summary className="cursor-pointer font-medium text-ink-700">
                {t("Attempt finalization failed")}
              </summary>
              <div className="mt-1">{task.error}</div>
            </details>
          ) : (
            <div className="min-w-0 flex-1 text-[11px] leading-snug text-red-800" role="alert">
              {task.error}
            </div>
          )
        ) : <span />}
        {artifact && (
          <div className="ml-auto flex shrink-0 items-center gap-1.5">
            <ArtifactDownloadMenu
              artifact={artifact}
              compact
              pptxExportDisabled={pptxExportDisabled}
            />
            <button
              type="button"
              onClick={onOpen}
              className="inline-flex items-center gap-1.5 rounded-sm bg-ink-900 px-2.5 py-1.5 text-[10px] font-medium uppercase text-ink-50 transition hover:bg-ink-700"
              style={{ letterSpacing: "0.14em" }}
            >
              <I.Edit width={11} height={11} />
              {t("Open")}
            </button>
          </div>
        )}
      </div>
    </article>
  );
}

function ArtifactPreview({ artifact, compact }: { artifact: Artifact; compact: boolean }) {
  const previewUrl = artifact.card_preview_url ?? artifact.preview_url;
  const objectFit = "object-contain";
  const className = `${compact ? "mt-2 h-14" : "mt-3 h-28"} w-full border-y border-ink-200/80 bg-paper ${objectFit}`;
  if (artifact.native_format === "mp4" && artifact.native_file_url) {
    return (
      <video
        className={className}
        src={artifact.native_file_url}
        poster={previewUrl}
        muted
        playsInline
        preload="metadata"
        controls
      >
        {artifact.downloads?.vtt && (
          <track kind="subtitles" srcLang="en" label="English" src={artifact.downloads.vtt} />
        )}
      </video>
    );
  }
  if (previewUrl) {
    return <img className={className} src={previewUrl} alt={artifact.name} loading="lazy" />;
  }
  if (artifact.native_format === "svg" && artifact.native_file_url) {
    return <img className={className} src={artifact.native_file_url} alt={artifact.name} loading="lazy" />;
  }
  if (artifact.native_format === "html" && artifact.native_file_url) {
    return (
      <iframe
        className={`${className} pointer-events-none`}
        src={artifact.native_file_url}
        title={artifact.name}
        sandbox=""
        tabIndex={-1}
      />
    );
  }
  const Icon = ARTIFACT_ICONS[artifact.artifact_type];
  return (
    <div className={`${className} flex items-center justify-center text-ink-400`}>
      <Icon width={28} height={28} />
    </div>
  );
}

function BundleStatus({ status, language }: { status: PaperBundleStatus; language: UiLanguage }) {
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2 py-1 text-[10px] font-medium ${statusClass(status)}`}>
      <span className="h-1.5 w-1.5 rounded-full bg-current" />
      {translate(language, STATUS_LABELS[status])}
    </span>
  );
}

function TaskStatus({
  status,
  language,
  available = false,
  needsRefinement = false,
  checking = false,
  attemptsUnavailable = false,
}: {
  status: PaperBundleTask["status"];
  language: UiLanguage;
  available?: boolean;
  needsRefinement?: boolean;
  checking?: boolean;
  attemptsUnavailable?: boolean;
}) {
  const displayStatus = checking
    ? "pending"
    : attemptsUnavailable
    ? "partial"
    : available
    ? "complete"
    : needsRefinement
    ? "partial"
    : status;
  const label = available
    ? "Available"
    : checking
    ? "Checking attempts"
    : attemptsUnavailable
    ? "Attempt history unavailable"
    : needsRefinement
    ? "Needs refinement"
    : TASK_STATUS_LABELS[status];
  return (
    <span className={`shrink-0 rounded-full px-2 py-1 text-[9px] font-medium uppercase ${statusClass(displayStatus)}`} style={{ letterSpacing: "0.1em" }}>
      {translate(language, label)}
    </span>
  );
}

function statusClass(status: PaperBundleStatus | PaperBundleTask["status"]): string {
  if (status === "complete") return "bg-emerald-50 text-emerald-800";
  if (status === "running" || status === "uploading") return "bg-accent-soft text-accent-deep";
  if (status === "cancelling") return "bg-amber-50 text-amber-900";
  if (status === "failed") return "bg-red-50 text-red-800";
  if (status === "partial") return "bg-amber-50 text-amber-900";
  return "bg-ink-100 text-ink-600";
}

function resolveTaskArtifact(
  task: PaperBundleTask,
  parentArtifacts: Record<string, Artifact> | undefined,
): Artifact | undefined {
  if (!task.artifact_id) return undefined;
  return parentArtifacts?.[task.artifact_id];
}
