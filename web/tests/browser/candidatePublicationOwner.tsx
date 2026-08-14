import { createRoot } from "react-dom/client";

import { AttemptInspector } from "@/components/AttemptInspector";
import { CompactAttemptDock } from "@/components/CompactAttemptDock";
import { candidatePublishOperationId, useApp } from "@/lib/store";
import type { Artifact, Conversation } from "@/lib/types";

const conversationId = "candidate-publication-mounted";
const sourceRunId = "run-candidate-publication-mounted";
const operationConversationId = candidatePublishOperationId(conversationId);
const artifact: Artifact = {
  artifact_id: "artifact-candidate-publication-mounted",
  name: "Mounted publication source",
  artifact_type: "poster",
  canvas: { w: 1600, h: 900 },
  layers: [],
};
const conversation: Conversation = {
  id: conversationId,
  title: "Mounted publication regression",
  created_at: 1,
  updated_at: 1,
  messages: [],
  artifacts: { [artifact.artifact_id]: artifact },
  active_artifact_id: artifact.artifact_id,
  pending: false,
  run_id: sourceRunId,
};
const candidate = {
  candidate_id: "candidate-publication-mounted-ready",
  run_id: sourceRunId,
  artifact_type: "poster" as const,
  attempt: 1,
  max_attempts: 4,
  created_at: "2026-08-05T00:00:00Z",
  source_sha256: "d".repeat(64),
  safety_state: "ready" as const,
  hard_blockers: [],
  warnings: [],
  source_url: "/candidate-publication-mounted.html",
  preview_urls: [],
};

const originalLoadRunAttempts = useApp.getState().loadRunAttempts;
useApp.setState({
  mode: "chat",
  ui_language: "en",
  current_conversation_id: conversationId,
  conversations: { [conversationId]: conversation },
  runs_progress: {},
  candidate_publication_owners: {},
  run_attempts: {
    [sourceRunId]: {
      run_id: sourceRunId,
      candidates: [candidate],
      selection_phase: "idle",
      loading: false,
    },
  },
  loadRunAttempts: async () => undefined,
});

let observing = false;
let nonOwnerBaseline: string | null = null;
const apiRequests: string[] = [];
const timerCalls: Array<{ kind: "timeout" | "interval"; delay?: number }> = [];
const nonOwnerMutations: string[] = [];
const ownerToken = Symbol("candidate-publication-mounted-owner");
const originalFetch = window.fetch;
const originalSetTimeout = window.setTimeout;
const originalSetInterval = window.setInterval;

window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  if (observing) {
    const url = typeof input === "string"
      ? input
      : input instanceof URL
        ? input.href
        : input.url;
    apiRequests.push(url);
    return Promise.reject(new Error(`Unexpected fetch during owner transition: ${url}`));
  }
  return originalFetch.call(window, input, init);
}) as typeof window.fetch;
window.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
  if (observing) timerCalls.push({ kind: "timeout", delay: timeout });
  return originalSetTimeout(handler, timeout, ...args);
}) as typeof window.setTimeout;
window.setInterval = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
  if (observing) timerCalls.push({ kind: "interval", delay: timeout });
  return originalSetInterval(handler, timeout, ...args);
}) as typeof window.setInterval;

const serializedNonOwnerState = () => JSON.stringify(
  Object.fromEntries(
    Object.entries(useApp.getState()).filter(
      ([key]) => key !== "candidate_publication_owners",
    ),
  ),
  (_key, value) => typeof value === "function" ? undefined : value,
);

const unsubscribe = useApp.subscribe((state, previousState) => {
  if (!observing) return;
  for (const key of Object.keys(state)) {
    if (
      key !== "candidate_publication_owners"
      && state[key as keyof typeof state] !== previousState[key as keyof typeof state]
    ) {
      nonOwnerMutations.push(key);
    }
  }
});

function MountedPublicationActions() {
  return (
    <>
      <section data-harness="inspector">
        <AttemptInspector runId={sourceRunId} variant="panel" />
      </section>
      <section data-harness="dock">
        <CompactAttemptDock
          runId={sourceRunId}
          conversationId={conversationId}
          pending={false}
          finalized={false}
          actionsDisabled={false}
        />
      </section>
    </>
  );
}

const rootElement = document.getElementById("root");
if (!rootElement) throw new Error("Mounted publication fixture root is missing.");
const root = createRoot(rootElement);
root.render(<MountedPublicationActions />);

let unmounted = false;
window.__candidatePublicationHarness = {
  mountCount: 1,
  beginObservation: () => {
    if (observing) throw new Error("Owner observation is already active.");
    apiRequests.length = 0;
    timerCalls.length = 0;
    nonOwnerMutations.length = 0;
    nonOwnerBaseline = serializedNonOwnerState();
    observing = true;
  },
  installOwner: () => {
    if (!observing) throw new Error("Owner observation has not started.");
    useApp.setState({
      candidate_publication_owners: {
        [conversationId]: { token: ownerToken, operationConversationId },
      },
    });
  },
  clearOwner: () => {
    const owner = useApp.getState().candidate_publication_owners[conversationId];
    if (owner?.token !== ownerToken) {
      throw new Error("Mounted publication fixture no longer owns the token.");
    }
    useApp.setState({ candidate_publication_owners: {} });
  },
  snapshot: () => {
    const state = useApp.getState();
    const current = state.conversations[conversationId];
    return {
      ownerActive: Boolean(state.candidate_publication_owners[conversationId]),
      operationConversationId:
        state.candidate_publication_owners[conversationId]?.operationConversationId,
      sourceRunId: current?.run_id,
      pending: current?.pending,
      candidateMessages: current?.messages.filter(
        (message) => message.task_type === "candidate_publish",
      ).length ?? 0,
      candidateProgress: Boolean(
        state.runs_progress[operationConversationId]
        || state.runs_progress[conversationId]?.mode === "attempt_publish",
      ),
      nonOwnerMutations: [...nonOwnerMutations],
      initialLocalStorageLength:
        window.__candidatePublicationInitialStorageLength ?? -1,
    };
  },
  finishObservation: () => {
    const result = {
      apiRequests: [...apiRequests],
      timerCalls: [...timerCalls],
      nonOwnerMutations: [...nonOwnerMutations],
      nonOwnerStateEqual: nonOwnerBaseline === serializedNonOwnerState(),
    };
    observing = false;
    return result;
  },
  hydrateTerminalFailure: () => {
    const diagnostic = "narration_timing_unfit scene=scene_11 measured_s=30.677 available_s=29.750 max_speed=1.35 final_speed=1.35";
    useApp.setState((state) => ({
      candidate_publication_owners: {},
      runs_progress: {},
      conversations: {
        ...state.conversations,
        [conversationId]: {
          ...state.conversations[conversationId],
          pending: false,
          run_id: undefined,
          messages: [{
            id: "candidate-publication-terminal-failure",
            role: "assistant",
            text: diagnostic,
            ts: 2,
            run_id: "candidate-publication-terminal-run",
            status: "error",
            task_type: "candidate_publish",
            task_payload: {
              artifact_type: "poster",
              source_run_id: sourceRunId,
              source_candidate_id: candidate.candidate_id,
            },
            failure: {
              status: "error",
              phase: "candidate_publish",
              error_code: "narration_timing_unfit",
              error_message: diagnostic,
              produced_files: [],
              artifact_type: "poster",
            },
          }],
        },
      },
      run_attempts: {
        ...state.run_attempts,
        [sourceRunId]: {
          run_id: sourceRunId,
          candidates: [candidate],
          selection_phase: "failed",
          selection: {
            candidate_id: candidate.candidate_id,
            source_attempt: candidate.attempt,
            state: "failed",
            error_code: "narration_timing_unfit",
            error_message: diagnostic,
          },
          loading: false,
        },
      },
    }));
  },
  unmount: () => {
    if (unmounted) return;
    unmounted = true;
    observing = false;
    unsubscribe();
    root.unmount();
    useApp.setState({
      candidate_publication_owners: {},
      loadRunAttempts: originalLoadRunAttempts,
    });
    window.fetch = originalFetch;
    window.setTimeout = originalSetTimeout;
    window.setInterval = originalSetInterval;
    window.localStorage.clear();
  },
};

declare global {
  interface Window {
    __candidatePublicationInitialStorageLength?: number;
    __candidatePublicationHarness?: {
      mountCount: number;
      beginObservation: () => void;
      installOwner: () => void;
      clearOwner: () => void;
      snapshot: () => Record<string, unknown>;
      finishObservation: () => Record<string, unknown>;
      hydrateTerminalFailure: () => void;
      unmount: () => void;
    };
  }
}
