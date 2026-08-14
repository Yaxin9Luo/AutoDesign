import { useEffect, useState } from "react";
import {
  candidateAction,
  type AttemptCandidateSummary,
} from "@/lib/attempt_candidates";
import { translate } from "@/lib/i18n";
import {
  candidatePublicationIsActive,
  sourceRunIsActiveForConversation,
  useApp,
} from "@/lib/store";
import { attemptFinalizationPrompt } from "./AttemptInspector";

export async function settleCompactAttemptAction(
  action: () => Promise<void>,
): Promise<Error | null> {
  try {
    await action();
    return null;
  } catch (error) {
    return error instanceof Error ? error : new Error(String(error));
  }
}

export function compactAttemptErrorForRun(
  error: { runId: string; message: string } | null,
  runId: string,
): string | null {
  return error?.runId === runId ? error.message : null;
}

export function CompactAttemptDock({
  runId,
  conversationId,
  pending,
  finalized,
  actionsDisabled = false,
}: {
  runId?: string;
  conversationId: string;
  pending: boolean;
  finalized: boolean;
  actionsDisabled?: boolean;
}) {
  const state = useApp((store) => runId ? store.run_attempts[runId] : undefined);
  const load = useApp((store) => store.loadRunAttempts);
  const select = useApp((store) => store.selectAttempt);
  const open = useApp((store) => store.openAttemptInCanvas);
  const language = useApp((store) => store.ui_language);
  const publicationActive = useApp((store) => (
    candidatePublicationIsActive(store, conversationId)
  ));
  const sourceActive = useApp((store) => Boolean(
    runId
    && sourceRunIsActiveForConversation(store.conversations, conversationId, runId)
  ));
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<{
    runId: string;
    message: string;
  } | null>(null);
  const t = (text: string, params?: Record<string, string | number>) =>
    translate(language, text, params);

  useEffect(() => {
    if (!runId) return;
    void load(runId);
    if (!pending) return;
    const timer = window.setInterval(() => {
      if (useApp.getState().run_attempts[runId]?.loading) return;
      void useApp.getState().loadRunAttempts(runId);
    }, 1800);
    return () => window.clearInterval(timer);
  }, [load, pending, runId]);

  if (!runId) return null;
  if (!state?.candidates.length) {
    if (!state?.error) return null;
    return (
      <div className="mt-2 flex items-center justify-between gap-2 border-y border-ink-200/75 bg-vellum/45 px-2 py-1.5 text-[9px] text-ink-600">
        <span>{t("Attempt history unavailable")}</span>
        <button
          type="button"
          disabled={state.loading}
          onClick={() => void load(runId)}
          className="shrink-0 rounded-sm border border-ink-300 bg-paper px-2 py-1 font-medium text-ink-700 transition hover:border-accent/45 disabled:opacity-50"
        >
          {t("Retry attempts")}
        </button>
      </div>
    );
  }

  const selectionBusy = !["idle", "complete", "failed"].includes(
    state.selection_phase,
  );
  const hasFinal = state.selection_phase === "complete" || finalized;
  const actionFailure = compactAttemptErrorForRun(actionError, runId)
    ?? (state.selection_phase === "failed" ? state.selection?.error_message : undefined);

  const act = async (candidate: AttemptCandidateSummary) => {
    if (busy || selectionBusy || actionsDisabled || publicationActive) return;
    setBusy(candidate.candidate_id);
    setActionError(null);
    try {
      const action = candidateAction(
        candidate,
        state.selection_phase,
        hasFinal,
        state.selection?.candidate_id,
      );
      if (action === "select") {
        const prompt = attemptFinalizationPrompt(
          language,
          candidate.attempt,
          sourceActive,
        );
        const confirmed = window.confirm(
          prompt.title,
        );
        if (!confirmed) return;
        const failure = await settleCompactAttemptAction(
          () => select(runId, candidate, conversationId),
        );
        if (failure) setActionError({ runId, message: failure.message });
      } else if (action === "fix" || action === "open") {
        const failure = await settleCompactAttemptAction(
          () => open(runId, candidate, conversationId),
        );
        if (failure) setActionError({ runId, message: failure.message });
      }
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="mt-2 flex items-center gap-2 overflow-x-auto border-y border-ink-200/75 bg-vellum/45 px-2 py-1.5">
      <span className="shrink-0 text-[9px] font-medium uppercase text-ink-500" style={{ letterSpacing: "0.11em" }}>
        {t("Attempts")}
      </span>
      {actionFailure ? (
        <details className="shrink-0 text-[9px] text-ink-600">
          <summary className="cursor-pointer font-medium">
            {t("Attempt action failed. Please retry.")}
          </summary>
          <div className="mt-1 max-w-56 whitespace-normal leading-snug">
            {actionFailure}
          </div>
        </details>
      ) : null}
      {state.candidates.map((candidate) => {
        const action = candidateAction(
          candidate,
          state.selection_phase,
          hasFinal,
          state.selection?.candidate_id,
        );
        const current = action === "current";
        const blocked = candidate.safety_state === "blocked";
        const blockedLabel = `${t("Attempt")} ${candidate.attempt}`;
        const readyLabel = candidate.artifact_type === "video"
          ? t("Source ready")
          : t("Ready");
        const finalizationPrompt = attemptFinalizationPrompt(
          language,
          candidate.attempt,
          sourceActive,
        );
        return (
          <button
            key={candidate.candidate_id}
            type="button"
            disabled={
              actionsDisabled
              || publicationActive
              || selectionBusy
              || busy !== null
              || current
            }
            onClick={() => void act(candidate)}
            title={
              current
                ? t("Current final")
                : blocked
                  ? blockedLabel
                  : hasFinal
                    ? t("Open in Canvas")
                    : state.selection_phase === "failed"
                      && state.selection?.candidate_id === candidate.candidate_id
                      ? t("Retry finalization")
                      : finalizationPrompt.title
            }
            className={`tabular inline-flex h-6 shrink-0 items-center gap-1 rounded-full border px-2 text-[9px] font-medium transition disabled:opacity-50 ${
              current
                ? "border-accent bg-accent text-white"
                : blocked
                  ? "border-ink-300 bg-paper text-ink-700 hover:border-accent/45"
                  : candidate.safety_state === "ready_with_warnings"
                    ? "border-amber-700/25 bg-amber-50 text-amber-900"
                    : "border-emerald-700/20 bg-emerald-50 text-emerald-800 hover:border-accent/45"
            }`}
          >
            {blocked ? (
              <span>{blockedLabel}</span>
            ) : (
              <>
                <span>{candidate.attempt}</span>
                <span aria-hidden>·</span>
                <span>
                  {current
                    ? t("Final")
                    : candidate.safety_state === "ready_with_warnings"
                      ? t("Warnings")
                      : readyLabel}
                </span>
              </>
            )}
          </button>
        );
      })}
    </div>
  );
}
