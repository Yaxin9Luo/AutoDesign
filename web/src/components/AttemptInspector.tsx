import { useEffect, useMemo, useReducer, useState } from "react";
import {
  attemptRunIdForConversation,
  candidateAction,
  isAttemptDraftEditing,
  type AttemptCandidateSummary,
} from "@/lib/attempt_candidates";
import { translate, type UiLanguage } from "@/lib/i18n";
import { paperBundleBlocksAttemptActions } from "@/lib/paper_bundle";
import {
  candidatePublicationIsActive,
  publishedAttemptForkForSourceRun,
  sourceRunIsActiveForConversation,
  useApp,
  useCurrentConversation,
} from "@/lib/store";
import { I } from "./icons";

const COLLAPSE_KEY = "autodesign.attempt-inspector.collapsed";

export function attemptFinalizationPrompt(
  language: UiLanguage,
  attempt: number,
  sourceActive: boolean,
): { title: string; detail: string } {
  if (sourceActive) {
    return {
      title: translate(language, "Stop generating and finalize Attempt {attempt}?", {
        attempt,
      }),
      detail: translate(
        language,
        "The active attempt will be terminated and discarded. Completed attempts remain available after finalization.",
      ),
    };
  }
  return {
    title: translate(language, "Finalize Attempt {attempt}?", { attempt }),
    detail: translate(
      language,
      "The selected completed attempt will be finalized as the result.",
    ),
  };
}

export interface AttemptActionDiagnosticState {
  runId: string;
  key: string;
  message: string;
}

type AttemptActionDiagnosticEvent =
  | {
      type: "failed";
      runId: string;
      key: string;
      message: string;
    }
  | {
      type: "succeeded";
      runId: string;
      key: string;
    }
  | {
      type: "run_changed";
      runId: string;
    };

export function reduceAttemptActionDiagnostic(
  state: AttemptActionDiagnosticState | null,
  event: AttemptActionDiagnosticEvent,
): AttemptActionDiagnosticState | null {
  if (event.type === "failed") {
    return {
      runId: event.runId,
      key: event.key,
      message: event.message,
    };
  }
  if (event.type === "run_changed") {
    return state?.runId === event.runId ? state : null;
  }
  return state?.runId === event.runId && state.key === event.key ? null : state;
}

export function AttemptDiagnostics({
  runId,
  language,
  stateError,
  actionError,
  hasUsableOutput,
}: {
  runId: string;
  language: UiLanguage;
  stateError?: string;
  actionError: AttemptActionDiagnosticState | null;
  hasUsableOutput: boolean;
}) {
  const diagnostics = [
    stateError,
    actionError?.runId === runId ? actionError.message : undefined,
  ].filter((diagnostic): diagnostic is string => Boolean(diagnostic));
  if (!diagnostics.length) return null;
  if (hasUsableOutput) {
    return (
      <details className="mx-3 mt-3 shrink-0 rounded-md border border-ink-300/55 bg-vellum/45 px-3 py-2 text-[11px] text-ink-600">
        <summary className="cursor-pointer font-medium text-ink-700">
          {translate(language, "Automatic finalization stopped; attempt kept")}
        </summary>
        {diagnostics.map((diagnostic, index) => (
          <div className="mt-1 leading-snug" key={`${index}:${diagnostic}`}>
            {diagnostic}
          </div>
        ))}
      </details>
    );
  }
  return (
    <div className="mx-3 mt-3 shrink-0 rounded-md border border-red-800/20 bg-red-50 px-3 py-2 text-[11px] text-red-900" role="alert">
      {diagnostics.map((diagnostic, index) => (
        <div key={`${index}:${diagnostic}`}>{diagnostic}</div>
      ))}
    </div>
  );
}

export function AttemptInspector({
  runId: explicitRunId,
  variant,
}: {
  runId?: string;
  variant: "rail" | "panel";
}) {
  const conversation = useCurrentConversation();
  const runId = explicitRunId || (conversation
    ? attemptRunIdForConversation(conversation)
    : undefined);
  const state = useApp((store) => runId ? store.run_attempts[runId] : undefined);
  const load = useApp((store) => store.loadRunAttempts);
  const language = useApp((store) => store.ui_language);
  const [collapsed, setCollapsed] = useState(() => {
    if (typeof window === "undefined") return false;
    try {
      return window.localStorage.getItem(COLLAPSE_KEY) === "1";
    } catch {
      return false;
    }
  });
  const [sheetOpen, setSheetOpen] = useState(false);
  const t = (text: string, params?: Record<string, string | number>) =>
    translate(language, text, params);

  useEffect(() => {
    if (!runId) return;
    void load(runId);
    const timer = window.setInterval(() => {
      const current = useApp.getState().run_attempts[runId];
      if (
        conversation?.pending
        || current?.selection_phase === "requested"
        || current?.selection_phase === "terminating"
        || current?.selection_phase === "promoting"
        || current?.selection_phase === "delivering"
      ) {
        if (current?.loading) return;
        void useApp.getState().loadRunAttempts(runId);
      }
    }, 1800);
    return () => window.clearInterval(timer);
  }, [conversation?.pending, load, runId]);

  if (!runId || (!state?.loading && !state?.candidates.length && !state?.error)) {
    return null;
  }

  const setRailCollapsed = (next: boolean) => {
    setCollapsed(next);
    try {
      window.localStorage.setItem(COLLAPSE_KEY, next ? "1" : "0");
    } catch {
      // The rail still works when storage is unavailable.
    }
  };
  const count = state?.candidates.length ?? 0;

  if (variant === "panel") {
    return <AttemptInspectorBody runId={runId} />;
  }

  return (
    <>
      <aside
        data-attempt-inspector="rail"
        className={`hidden h-full min-h-0 shrink-0 border-l border-ink-300/55 bg-paper/92 backdrop-blur-md min-[1180px]:flex ${
          collapsed ? "w-12 items-start justify-center" : "w-[312px] flex-col"
        }`}
        aria-label={t("Attempts")}
      >
        {collapsed ? (
          <button
            type="button"
            className="mt-4 flex flex-col items-center gap-2 rounded-md px-2 py-2 text-ink-600 transition hover:bg-vellum hover:text-ink-900"
            aria-label={t("Expand attempts")}
            onClick={() => setRailCollapsed(false)}
          >
            <I.PanelRight width={17} height={17} />
            <span className="tabular text-[10px]">{count}</span>
          </button>
        ) : (
          <AttemptInspectorBody
            runId={runId}
            onCollapse={() => setRailCollapsed(true)}
          />
        )}
      </aside>

      <div className="min-[1180px]:hidden">
        <button
          type="button"
          className="absolute right-4 top-4 z-40 rounded-full border border-ink-300/70 bg-paper/95 px-3 py-1.5 text-[10px] font-medium uppercase text-ink-700 shadow-soft backdrop-blur-md"
          onClick={() => setSheetOpen(true)}
        >
          {t("Attempts")} {count}
        </button>
        {sheetOpen ? (
          <div
            className="fixed inset-0 z-[85] bg-ink-900/20 backdrop-blur-[2px]"
            role="dialog"
            aria-modal="true"
            aria-label={t("Attempts")}
          >
            <aside className="absolute inset-y-0 right-0 flex w-full max-w-[360px] flex-col border-l border-ink-300 bg-paper shadow-page">
              <AttemptInspectorBody
                runId={runId}
                onClose={() => setSheetOpen(false)}
              />
            </aside>
          </div>
        ) : null}
      </div>
    </>
  );
}

function AttemptInspectorBody({
  runId,
  onCollapse,
  onClose,
}: {
  runId: string;
  onCollapse?: () => void;
  onClose?: () => void;
}) {
  const conversation = useCurrentConversation();
  const state = useApp((store) => store.run_attempts[runId]);
  const select = useApp((store) => store.selectAttempt);
  const open = useApp((store) => store.openAttemptInCanvas);
  const publish = useApp((store) => store.publishActiveCandidateDraft);
  const mode = useApp((store) => store.mode);
  const language = useApp((store) => store.ui_language);
  const runProgress = useApp((store) => (
    conversation ? store.runs_progress[conversation.id] : undefined
  ));
  const publicationActive = useApp((store) => (
    conversation
      ? candidatePublicationIsActive(store, conversation.id)
      : false
  ));
  const sourceActive = useApp((store) => Boolean(
    conversation
    && sourceRunIsActiveForConversation(store.conversations, conversation.id, runId)
  ));
  const bundleActionsBlocked = useApp((store) => {
    if (!conversation) return false;
    const parent = conversation.paper_bundle?.kind === "child"
      ? store.conversations[conversation.paper_bundle.parent_conversation_id]
      : conversation.paper_bundle?.kind === "parent"
        ? conversation
        : undefined;
    return parent?.paper_bundle?.kind === "parent"
      ? paperBundleBlocksAttemptActions(parent.paper_bundle, runId)
      : false;
  });
  const active = conversation?.active_artifact_id
    ? conversation.artifacts[conversation.active_artifact_id]
    : undefined;
  const activeDraftCandidateId = active?.candidate_draft
    ? active.attempt_lineage?.source_candidate_id
    : undefined;
  const [confirming, setConfirming] = useState<AttemptCandidateSummary | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, dispatchActionError] = useReducer(
    reduceAttemptActionDiagnostic,
    null,
  );
  const t = (text: string, params?: Record<string, string | number>) =>
    translate(language, text, params);
  const candidates = useMemo(
    () => [...(state?.candidates ?? [])].sort((a, b) => a.attempt - b.attempt),
    [state?.candidates],
  );
  const activeDraftCandidate = activeDraftCandidateId
    ? candidates.find((candidate) => candidate.candidate_id === activeDraftCandidateId)
    : undefined;
  const activeDraftTarget = conversation
    && active?.candidate_draft
    && active.attempt_lineage?.source_run_id
    && active.attempt_lineage?.source_candidate_id
    ? {
        conversationId: conversation.id,
        artifactId: active.artifact_id,
        sourceRunId: active.attempt_lineage.source_run_id,
        sourceCandidateId: active.attempt_lineage.source_candidate_id,
      }
    : undefined;
  const activeDraftIsPublishable = Boolean(
    activeDraftTarget
    && activeDraftTarget.sourceRunId === runId
    && state
    && !state.loading
    && !state.error
    && activeDraftCandidate
  );
  const hasFinalArtifact = Boolean(
    publishedAttemptForkForSourceRun(conversation, runId),
  );
  const finalized = state?.selection_phase === "complete" || hasFinalArtifact;
  const selectionBusy = state?.selection_phase
    && !["idle", "complete", "failed"].includes(state.selection_phase);
  const selectedCandidateId = state?.selection?.candidate_id;
  const candidateActionsBlocked = bundleActionsBlocked
    || runProgress?.phase === "cancelling"
    || publicationActive;
  const phaseLabel = state?.selection_phase === "delivering"
    ? t("Rendering selected Video")
    : state?.selection_phase === "promoting"
      ? t("Finalizing Attempt {attempt}", {
        attempt: state.selection?.source_attempt ?? "",
      })
      : undefined;
  const selectionFailure = state?.selection_phase === "failed"
    ? state.selection?.error_message || t("Attempt finalization failed")
    : undefined;
  const lastCompleted = candidates.at(-1)?.attempt ?? 0;
  const generatingAttempt = conversation?.pending
    && state?.active_attempt
    && state.active_attempt > lastCompleted
    ? state.active_attempt
    : undefined;
  const confirmationPrompt = confirming
    ? attemptFinalizationPrompt(language, confirming.attempt, sourceActive)
    : undefined;

  useEffect(() => {
    dispatchActionError({ type: "run_changed", runId });
  }, [runId]);

  const perform = async (
    key: string,
    action: () => Promise<void>,
  ) => {
    setBusy(key);
    try {
      await action();
      await useApp.getState().loadRunAttempts(runId);
      dispatchActionError({ type: "succeeded", runId, key });
    } catch (error) {
      dispatchActionError({
        type: "failed",
        runId,
        key,
        message: error instanceof Error ? error.message : String(error),
      });
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="flex h-full min-h-0 w-full flex-col" aria-label={t("Attempts")}>
      <div className="flex shrink-0 items-center justify-between gap-2 border-b border-ink-300/55 px-4 py-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="eyebrow-rule">{t("Attempts")}</span>
          {candidates.length ? (
            <span className="tabular text-[10px] text-ink-400">
              {candidates.length}
            </span>
          ) : null}
        </div>
        <div className="flex items-center gap-1">
          {onCollapse ? (
            <button
              type="button"
              className="icon-btn h-7 w-7"
              aria-label={t("Collapse attempts")}
              onClick={onCollapse}
            >
              <I.PanelRight width={15} height={15} />
            </button>
          ) : null}
          {onClose ? (
            <button
              type="button"
              className="icon-btn h-7 w-7"
              aria-label={t("Close attempts")}
              onClick={onClose}
            >
              <I.Close width={15} height={15} />
            </button>
          ) : null}
        </div>
      </div>

      {active?.candidate_draft ? (
        <div className="shrink-0 border-b border-ink-300/45 px-4 py-3">
          <button
            type="button"
            className="w-full rounded-md bg-ink-900 px-3 py-2 text-[11px] font-medium text-white transition hover:bg-accent-deep disabled:opacity-45"
            disabled={
              busy !== null
              || candidateActionsBlocked
              || !activeDraftIsPublishable
            }
            onClick={() => void perform(
              "publish",
              () => activeDraftTarget
                ? publish(activeDraftTarget)
                : Promise.reject(
                    new Error("The selected attempt draft is no longer available."),
                  ),
            )}
          >
            {busy === "publish" ? t("Publishing") : t("Publish as new final")}
          </button>
        </div>
      ) : null}

      {phaseLabel ? (
        <div
          className="shrink-0 border-b border-accent/15 bg-accent-soft/45 px-4 py-2 text-[11px] text-accent-deep"
          role="status"
        >
          {phaseLabel}
        </div>
      ) : null}

      {selectionFailure && candidates.length ? (
        <details className="shrink-0 border-b border-ink-300/55 bg-vellum/45 px-4 py-2 text-[11px] text-ink-600">
          <summary className="cursor-pointer font-medium text-ink-700">
            {t("Automatic finalization stopped; attempt kept")}
          </summary>
          <div className="mt-1 leading-snug">{selectionFailure}</div>
        </details>
      ) : null}

      <AttemptDiagnostics
        runId={runId}
        language={language}
        stateError={state?.error}
        actionError={actionError}
        hasUsableOutput={candidates.length > 0 || hasFinalArtifact}
      />

      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
        {candidates.map((candidate) => {
          const current = state?.selection_phase === "complete"
            && candidate.candidate_id === selectedCandidateId;
          const editing = isAttemptDraftEditing(
            mode,
            candidate.candidate_id,
            activeDraftCandidateId,
          );
          const action = editing
            ? "editing"
            : candidateAction(
              candidate,
              state?.selection_phase ?? "idle",
              finalized,
              selectedCandidateId,
            );
          const preview = candidate.preview_urls[0];
          const key = `${candidate.candidate_id}:${action}`;
          const canvasAction = action === "fix" || action === "open";
          return (
            <article
              key={candidate.candidate_id}
              className={`overflow-hidden rounded-lg border bg-white shadow-soft ${
                current || editing
                  ? "border-accent ring-1 ring-accent/20"
                  : "border-ink-300/65"
              }`}
            >
              <a
                href={candidate.source_url}
                target="_blank"
                rel="noreferrer"
                className="block aspect-[16/9] bg-vellum"
                title={`${t("Open attempt")} ${candidate.attempt}`}
              >
                {preview ? (
                  <img
                    src={preview}
                    alt={`${t("Attempt")} ${candidate.attempt}`}
                    className="h-full w-full object-cover"
                    loading="lazy"
                  />
                ) : (
                  <span className="flex h-full items-center justify-center text-ink-400">
                    <I.Layout width={22} height={22} />
                  </span>
                )}
              </a>
              <div className="space-y-3 p-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="tabular text-[11px] font-semibold uppercase text-ink-800">
                    {t("Attempt")} {candidate.attempt}
                  </span>
                  <time className="tabular text-[9.5px] text-ink-400">
                    {formatAttemptTime(candidate.created_at)}
                  </time>
                </div>
                <div className="grid grid-cols-2 gap-2">
                  <a
                    href={candidate.source_url}
                    target="_blank"
                    rel="noreferrer"
                    className="rounded-md border border-ink-300 bg-paper px-2.5 py-1.5 text-center text-[10.5px] font-medium text-ink-700 transition hover:border-accent/50"
                  >
                    {t("Preview")}
                  </a>
                  <button
                    type="button"
                    disabled={
                      Boolean(selectionBusy)
                      || busy !== null
                      || candidateActionsBlocked
                      || current
                      || editing
                    }
                    className={`rounded-md border px-2.5 py-1.5 text-[10.5px] font-medium transition disabled:cursor-not-allowed disabled:opacity-45 ${
                      action === "select"
                        ? "border-accent bg-accent text-white hover:bg-accent-deep"
                        : "border-ink-300 bg-paper text-ink-700 hover:border-accent/50"
                    }`}
                    onClick={() => {
                      if (action === "select") setConfirming(candidate);
                      else if (canvasAction) {
                        void perform(key, () => open(runId, candidate));
                      }
                    }}
                  >
                    {busy === key
                      ? t("Opening")
                      : action === "editing"
                        ? t("Editing in Canvas")
                        : action === "current"
                          ? t("Current final")
                          : canvasAction
                            ? action === "fix"
                              ? t("Fix in Canvas · generation continues")
                              : t("Open in Canvas")
                            : state?.selection_phase === "failed"
                              && candidate.candidate_id === selectedCandidateId
                              ? t("Retry finalization")
                              : t("Use this attempt")}
                  </button>
                </div>
              </div>
            </article>
          );
        })}

        {generatingAttempt ? (
          <div
            className="flex items-center gap-2 rounded-lg border border-accent/20 bg-accent-soft/35 px-3 py-3 text-[11px] text-accent-deep"
            role="status"
          >
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />
            <span>
              {t("Attempt")} {generatingAttempt} · {t("Generating")}
            </span>
          </div>
        ) : null}
      </div>

      {confirming ? (
        <div
          className="fixed inset-0 z-[90] flex items-center justify-center bg-ink-900/25 p-5 backdrop-blur-[2px]"
          role="dialog"
          aria-modal="true"
        >
          <div className="w-full max-w-[440px] rounded-xl border border-ink-300 bg-paper p-5 shadow-page">
            <h3 className="font-display text-[20px] text-ink-900">
              {confirmationPrompt?.title}
            </h3>
            <p className="mt-2 text-[12px] leading-relaxed text-ink-600">
              {confirmationPrompt?.detail}
            </p>
            {confirming.warnings.length ? (
              <div className="mt-3 rounded-md border border-amber-700/20 bg-amber-50 px-3 py-2 text-[11px] text-amber-900">
                {confirming.warnings.map((warning) => warning.message).join(" · ")}
              </div>
            ) : null}
            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                className="rounded-md border border-ink-300 px-3 py-1.5 text-[11px] text-ink-700"
                onClick={() => setConfirming(null)}
              >
                {t("Cancel")}
              </button>
              <button
                type="button"
                disabled={candidateActionsBlocked}
                className="rounded-md bg-accent px-3 py-1.5 text-[11px] font-medium text-white hover:bg-accent-deep"
                onClick={() => {
                  if (candidateActionsBlocked) return;
                  const candidate = confirming;
                  setConfirming(null);
                  void perform(
                    `select:${candidate.candidate_id}`,
                    () => select(runId, candidate, conversation?.id),
                  );
                }}
              >
                {t("Use this attempt")}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </section>
  );
}

function formatAttemptTime(createdAt: string): string {
  const match = createdAt.match(/T(\d{2}):(\d{2})/);
  return match ? `${match[1]}:${match[2]}` : "";
}
