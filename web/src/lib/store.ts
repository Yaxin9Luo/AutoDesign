import { create } from "zustand";
import {
  createJSONStorage,
  persist,
  type PersistOptions,
  type StateStorage,
} from "zustand/middleware";
import type {
  AppMode,
  Artifact,
  ArtifactAsset,
  ArtifactType,
  Attachment,
  BackendInfo,
  Bbox,
  Conversation,
  HtmlLayoutPatch,
  Layer,
  LayerGroup,
  Message,
		  PaperBundleState,
		  PaperBundleBackendState,
		  PaperBundleTask,
		  PaperBundleTaskMap,
		  PaperBundleTaskStatus,
		  PendingArtifactEdits,
		  PendingEditsPayload,
		  PosterAreaSelectionItem,
		  PosterAreaSelectionKind,
		  PosterSelectionContext,
		  PosterSelectionSummary,
		  PosterCanvasPreset,
		  PosterPalette,
		  RecoverableTaskType,
		  MessageTaskPayload,
		} from "./types";
import { nextId, sampleLandingPage, samplePoster, sampleSlides, sampleVideo } from "./mock";
import { restoredPosterPaletteId } from "./poster_palette_state";
import {
  isCanvasValidationError,
  posterCanvasRequestSelection,
  restoredPosterCanvasPresetId,
  validatePosterCanvasCatalog,
} from "./poster_canvas_state";
import { detectSlideFrames } from "./slide_frames";
import {
  ApiError,
  RunStartAmbiguousError,
  RunStatusError,
  type GenerateAck,
  type GenerateResponse,
  type OpenResearchProjectResult,
  type ServerHistoryConversationSummary,
  applyEditsRequest,
  cancelPaperBundleRequest,
  cancelRunRequest,
  createPaperBundle,
  fetchOpenResearchProject,
  fetchHealth,
  fetchPosterCanvasPresets,
  fetchPosterPalettes,
  fetchRunArtifact,
  fetchRunStatus,
  fetchRunAttempts,
  forkRunAttempt,
  fetchServerHistory,
  fetchServerHistoryConversation,
  getPaperBundle,
  publishCandidateDraft,
  publishRunAttempt,
  preparePaperBundleInput,
  renderVideoRequest,
  retryRunRequest,
  retryVideoExportRequest,
  runIdFromArtifactId,
  runIdFromMessage,
  sendDesignEvent,
  selectRunAttempt,
  startGenerate,
  startReservedRun,
  startArtifactPptxExport,
  startOpenResearchProject,
  startPosterCodeEdit,
  uploadReservedRunInput,
  listPaperBundles,
  type PaperBundleCancelResponse,
  type PaperBundleChildDescriptor,
  type PaperBundleCreateRequest,
  type PaperBundleJobResponse,
  type PaperBundlePublicationDescriptor,
  type RunCancelResponse,
  type RunLifecycleState,
  type RunStartReplay,
} from "./api";
import {
  type AttemptCandidateSummary,
  type RunAttemptState,
} from "./attempt_candidates";
import type { OpenResearchSubmitOptions } from "./openresearch";
import { applyEvent, initialProgress, type RunProgress } from "./progress";
import {
  VIDEO_SCENE_DURATION_MAX_S,
  VIDEO_SCENE_DURATION_MIN_S,
} from "./presets";
import { isSupportedLanguage, type UiLanguage } from "./i18n";
import { currentDemoUserScope } from "./api_settings";
import {
  attachmentsForReferencePosterSubmission,
  bindReferencePosterHandle,
  partitionReferenceAttachments,
} from "./reference_poster";
import {
  authoringBudgetFor,
  readAuthoringBudgets,
} from "./authoring_budget";
import {
  PAPER_BUNDLE_ARTIFACT_ORDER,
  createPaperBundleChildState,
  createPaperBundleParentState,
  createPaperBundleRequestSpecs,
  paperBundleBlocksAttemptActions,
  paperBundleBlocksPptxExport,
  resolvedCompletedTaskError,
} from "./paper_bundle";

// Reference RunProgress here so the type doesn't get tree-shaken when
// only used through the store interface — keeps tsc happy.
export type { RunProgress };

/** Live EventSource handles, keyed by conversation_id. Not part of the
 *  store state because EventSource is a non-serializable browser
 *  resource and lives outside React's reactivity. */
const _SSE_HANDLES: Map<string, EventSource> = new Map();
let _SERVER_HISTORY_LOAD: Promise<void> | null = null;
const _SERVER_HISTORY_DETAIL_LOADS = new Map<string, Promise<void>>();
type RunWaitOwner = {
  runId: string;
  controller: AbortController;
  abort: (error: Error) => void;
  reconcile: (startReplay?: RunStartReplay) => void;
};
const _SSE_WAIT_ABORTS = new Map<string, RunWaitOwner>();
const _RUN_EVENT_IDS = new Map<string, Set<string>>();
const _RUN_CANCEL_REQUESTS = new Map<
  string,
  ReturnType<typeof cancelRunRequest>
>();
const _AUTHORITATIVE_RUN_CANCELLATIONS = new Set<string>();
const _RESERVED_RUN_UPLOAD_ABORTS = new Map<string, {
  runId: string;
  controller: AbortController;
}>();
const _PAPER_BUNDLE_UPLOADS = new Map<string, {
  parentConversationId: string;
  artifactType: ArtifactType;
  runId: string;
  controller: AbortController;
}>();
const _PAPER_BUNDLE_OPERATIONS = new Map<string, {
  jobId: string;
  ownerScope: string;
  cancelRequested: boolean;
  createDispatched: boolean;
}>();
const _PAPER_BUNDLE_CANCEL_REQUESTS = new Map<string, Promise<void>>();
const _PAPER_BUNDLE_RECOVERIES = new Map<string, Promise<void>>();
const _ACTIVE_RUN_RECOVERIES = new Map<string, Promise<void>>();
type RunArtifactFetchOwner = {
  runId: string;
  controller: AbortController;
};
const _RUN_ARTIFACT_FETCH_OWNERS = new Map<string, RunArtifactFetchOwner>();
const _RUN_ARTIFACT_FETCH_PROMISES = new Map<string, {
  owner: RunArtifactFetchOwner;
  mode: "once" | "retry";
  promise: Promise<GenerateResponse>;
}>();
const _PAPER_BUNDLE_PUBLICATION_HYDRATIONS = new Map<string, Promise<void>>();
const _CANCELLED_PAPER_BUNDLE_PARENTS = new Set<string>();
const _CANCELLED_PAPER_BUNDLE_TASKS = new Set<string>();
const _ACTIVE_PPTX_EXPORT_CONVERSATIONS = new Set<string>();
type CandidatePublicationCancellation = {
  attempted: boolean;
  confirmed: boolean;
  error?: string;
};
export type CandidatePublicationReactiveOwner = {
  token: symbol;
  operationConversationId: string;
};
type CandidatePublicationOwner = {
  kind: "candidate_publish";
  token: symbol;
  ownerScope: string;
  operationConversationId: string;
  controller: AbortController;
  cancelRequested: boolean;
  runId?: string;
  flowComplete: boolean;
  settlement: Promise<CandidatePublicationCancellation>;
  resolveSettlement: (result: CandidatePublicationCancellation) => void;
  cancellationRequest?: Promise<CandidatePublicationCancellation>;
  settlementResult?: CandidatePublicationCancellation;
  release?: () => void;
};
type AttemptForkOwner = Omit<CandidatePublicationOwner, "kind"> & {
  kind: "attempt_fork";
};
type DirectCandidatePublicationLineage = {
  conversationId: string;
  sourceRunId: string;
  sourceCandidateId: string;
  parentConversationId?: string;
  artifactType?: ArtifactType;
  parentJobId?: string;
  authoringRunId?: string;
};
export type CandidateDraftPublicationTarget = {
  conversationId: string;
  artifactId: string;
  sourceRunId: string;
  sourceCandidateId: string;
};
type ActiveDerivedRunOwner = CandidatePublicationOwner | AttemptForkOwner | {
  kind: "video_render";
  token: symbol;
};
const _ACTIVE_DERIVED_RUN_CONVERSATIONS = new Map<
  string,
  ActiveDerivedRunOwner
>();

export function installTokenizedPublicationOwner<T extends { token: symbol }>(
  moduleOwners: Map<string, T>,
  conversationId: string,
  moduleOwner: T,
  reactiveOwner: CandidatePublicationReactiveOwner,
  currentReactiveOwner: () => CandidatePublicationReactiveOwner | undefined,
  replaceReactiveOwner: (
    owner: CandidatePublicationReactiveOwner | undefined,
  ) => void,
): () => void {
  moduleOwners.set(conversationId, moduleOwner);
  replaceReactiveOwner(reactiveOwner);
  return () => {
    if (moduleOwners.get(conversationId) === moduleOwner) {
      moduleOwners.delete(conversationId);
    }
    if (currentReactiveOwner()?.token === moduleOwner.token) {
      replaceReactiveOwner(undefined);
    }
  };
}
const PAPER_BUNDLE_POST_TERMINAL_ARTIFACT_GRACE_MS = 60 * 1000;
const RUN_STATUS_POLL_INITIAL_MS = 250;
const RUN_STATUS_POLL_MAX_MS = 5000;
const RUN_STATUS_REQUEST_TIMEOUT_MS = 10 * 1000;
const RUN_START_REPLAY_TIMEOUT_MS = 60 * 1000;
const RUN_ATTEMPT_REQUEST_TIMEOUT_MS = 10 * 1000;
const runAttemptHydrationOwners = new Map<string, symbol>();
const RUN_STATUS_PERMANENT_CONFIRMATIONS = 3;
const RUN_ARTIFACT_RETRY_MAX_ATTEMPTS = 16;
const CANCELLATION_REQUEST_TIMEOUT_MS = 10 * 1000;
const PAPER_BUNDLE_CANCEL_POLL_INTERVAL_MS = 250;
const PAPER_BUNDLE_CANCEL_POLL_REQUEST_TIMEOUT_MS = 3 * 1000;
const PAPER_BUNDLE_CANCEL_POLL_TOTAL_MS = 15 * 1000;
const PAPER_BUNDLE_CANCEL_POLL_MAX_ATTEMPTS = Math.ceil(
  PAPER_BUNDLE_CANCEL_POLL_TOTAL_MS / PAPER_BUNDLE_CANCEL_POLL_INTERVAL_MS,
);

function paperBundleBackendOwnerId(
  ownerScope: string,
  backendInfo: BackendInfo | null,
): string {
  return backendInfo?.user_isolation === false ? "local" : ownerScope;
}

function isQuiescentTerminalPaperBundle(job: PaperBundleJobResponse): boolean {
  return job.terminal
    && Object.values(job.children).every((child) => child.terminal && child.process_free);
}

function monotonicNow(): () => number {
  const performanceNow = typeof globalThis.performance?.now === "function"
    ? globalThis.performance.now.bind(globalThis.performance)
    : null;
  let last = 0;
  return () => {
    let candidate: number;
    try {
      candidate = performanceNow ? performanceNow() : Date.now();
    } catch {
      candidate = Date.now();
    }
    if (Number.isFinite(candidate)) last = Math.max(last, candidate);
    return last;
  };
}

async function pollPaperBundleCancellation(
  jobId: string,
  ownerScope: string,
  backendOwnerId: string,
  pendingCreation = false,
): Promise<PaperBundleJobResponse | PaperBundleCancelResponse> {
  const now = monotonicNow();
  const deadline = now() + PAPER_BUNDLE_CANCEL_POLL_TOTAL_MS;
  let activeController: AbortController | null = null;
  let totalDeadlineReached = false;
  let totalTimeout: number | undefined;
  const totalDeadlineError = new Error("paper bundle cancellation confirmation timed out");
  const totalDeadline = new Promise<never>((_resolve, reject) => {
    totalTimeout = window.setTimeout(() => {
      totalDeadlineReached = true;
      activeController?.abort(totalDeadlineError);
      reject(totalDeadlineError);
    }, PAPER_BUNDLE_CANCEL_POLL_TOTAL_MS);
  });
  const poll = async () => {
    for (let attempt = 0; attempt < PAPER_BUNDLE_CANCEL_POLL_MAX_ATTEMPTS; attempt += 1) {
      if (currentDemoUserScope() !== ownerScope) {
        throw new Error("paper bundle cancellation owner changed");
      }
      if (totalDeadlineReached) break;
      const remaining = deadline - now();
      if (remaining <= 0) break;
      const controller = new AbortController();
      activeController = controller;
      const timeout = window.setTimeout(
        () => controller.abort(new Error("paper bundle cancellation poll timed out")),
        Math.min(PAPER_BUNDLE_CANCEL_POLL_REQUEST_TIMEOUT_MS, remaining),
      );
      let snapshot: PaperBundleJobResponse | PaperBundleCancelResponse;
      try {
        snapshot = pendingCreation
          ? await cancelPaperBundleRequest(jobId, controller.signal)
          : await getPaperBundle(jobId, controller.signal);
      } finally {
        window.clearTimeout(timeout);
        if (activeController === controller) activeController = null;
      }
      if (
        currentDemoUserScope() !== ownerScope
        || snapshot.owner_id !== backendOwnerId
      ) {
        throw new Error("paper bundle cancellation owner changed");
      }
      if (totalDeadlineReached) throw totalDeadlineError;
      if (
        ("confirmed" in snapshot && snapshot.http_status === 200 && snapshot.confirmed)
        || ("terminal" in snapshot && isQuiescentTerminalPaperBundle(snapshot))
      ) return snapshot;
      const waitMs = Math.min(PAPER_BUNDLE_CANCEL_POLL_INTERVAL_MS, deadline - now());
      if (waitMs <= 0) break;
      await new Promise<void>((resolve) => window.setTimeout(resolve, waitMs));
    }
    throw new Error("paper bundle cancellation confirmation timed out");
  };
  try {
    return await Promise.race([poll(), totalDeadline]);
  } finally {
    window.clearTimeout(totalTimeout);
  }
}

const runOperationKey = (conversationId: string, runId: string) =>
  `${conversationId}:${runId}`;

export const publishedAttemptForkForSourceRun = (
  conversation: Conversation | undefined,
  sourceRunId: string,
): Artifact | undefined => {
  const publishedId = conversation?.published_artifact_id;
  const artifact = publishedId
    ? conversation?.artifacts[publishedId]
    : undefined;
  return (
    artifact
    && !artifact.candidate_draft
    && artifact.attempt_lineage?.status === "published"
    && artifact.attempt_lineage.source_run_id === sourceRunId
  )
    ? artifact
    : undefined;
};
type PaperBundleArtifactRetryState = {
  terminalSettledAt?: number;
  terminalDeadlineReached?: boolean;
  terminalDeadlineTimer?: number;
  onTerminal?: () => void;
  onTerminalDeadline?: () => void;
};

function paperBundleJobGeneration(jobId: string): string {
  return `job:${jobId}`;
}

function paperBundleCancellationGeneration(
  bundle: Extract<PaperBundleState, { kind: "parent" }>,
): string {
  if (bundle.job_id) return paperBundleJobGeneration(bundle.job_id);
  return `legacy:${PAPER_BUNDLE_ARTIFACT_ORDER.map((artifactType) => {
    const task = bundle.tasks[artifactType];
    return `${artifactType}:${task.run_id ?? "pending"}:${task.started_at ?? "unknown"}`;
  }).join("|")}`;
}

function paperBundleParentCancellationKey(
  ownerScope: string,
  parentConversationId: string,
  generation: string,
): string {
  return `${ownerScope}:${parentConversationId}:${generation}`;
}

function paperBundleTaskCancellationKey(
  ownerScope: string,
  parentConversationId: string,
  generation: string,
  artifactType: ArtifactType,
): string {
  return `${paperBundleParentCancellationKey(
    ownerScope,
    parentConversationId,
    generation,
  )}:${artifactType}`;
}

function paperBundleTaskWasCancelled(
  ownerScope: string,
  parentConversationId: string,
  generation: string,
  artifactType: ArtifactType,
): boolean {
  return _CANCELLED_PAPER_BUNDLE_PARENTS.has(
    paperBundleParentCancellationKey(ownerScope, parentConversationId, generation),
  )
    || _CANCELLED_PAPER_BUNDLE_TASKS.has(
      paperBundleTaskCancellationKey(
        ownerScope,
        parentConversationId,
        generation,
        artifactType,
      ),
    );
}

function clearPaperBundleCancellationIntents(
  ownerScope: string,
  parentConversationId: string,
  generation: string,
): void {
  _CANCELLED_PAPER_BUNDLE_PARENTS.delete(
    paperBundleParentCancellationKey(ownerScope, parentConversationId, generation),
  );
  for (const artifactType of PAPER_BUNDLE_ARTIFACT_ORDER) {
    _CANCELLED_PAPER_BUNDLE_TASKS.delete(
      paperBundleTaskCancellationKey(
        ownerScope,
        parentConversationId,
        generation,
        artifactType,
      ),
    );
  }
}

const runCancellationRequestKey = (
  ownerScope: string,
  conversationId: string,
  runId: string,
) => `${ownerScope}:${runOperationKey(conversationId, runId)}`;

const paperBundleCancelRequestKey = (ownerScope: string, jobId: string) =>
  `${ownerScope}:${jobId}`;

function paperBundleTaskStatusFromBackend(
  state: PaperBundleChildDescriptor["state"],
): PaperBundleTaskStatus {
  if (state === "reserved" || state === "uploading") return "uploading";
  if (state === "queued" || state === "running" || state === "completing") return "running";
  if (state === "cancelling") return "cancelling";
  if (state === "completed") return "complete";
  if (state === "cancelled") return "cancelled";
  return "failed";
}

function isActivePaperBundleTaskStatus(status: PaperBundleTaskStatus): boolean {
  return status === "pending"
    || status === "uploading"
    || status === "running"
    || status === "cancelling";
}

export function sourceRunIsActiveForConversation(
  conversations: Record<string, Conversation>,
  conversationId: string,
  runId: string,
): boolean {
  const conversation = conversations[conversationId];
  if (conversation?.pending === true && conversation.run_id === runId) return true;
  if (conversation?.paper_bundle?.kind !== "child") return false;
  const parent = conversations[conversation.paper_bundle.parent_conversation_id];
  if (parent?.paper_bundle?.kind !== "parent") return false;
  const task = parent.paper_bundle.tasks[conversation.paper_bundle.artifact_type];
  return isActivePaperBundleTaskStatus(task.status)
    && (task.run_id === runId || task.authoring_run_id === runId);
}

function sourceRunIsKnownTerminalForConversation(
  conversations: Record<string, Conversation>,
  conversationId: string,
  runId: string,
): boolean {
  const conversation = conversations[conversationId];
  if (!conversation) return false;
  if (conversation.paper_bundle?.kind === "child") {
    const parent = conversations[conversation.paper_bundle.parent_conversation_id];
    if (parent?.paper_bundle?.kind !== "parent") return false;
    const task = parent.paper_bundle.tasks[conversation.paper_bundle.artifact_type];
    const matches = task.run_id === runId || task.authoring_run_id === runId;
    return matches && !isActivePaperBundleTaskStatus(task.status);
  }
  return conversation.messages.some((message) => (
    message.run_id === runId
    && (message.status === "done" || message.status === "error")
  ));
}

function isTerminalPaperBundleBackendState(
  state: PaperBundleBackendState | undefined,
): boolean {
  return state === "cancelled"
    || state === "completed"
    || state === "partial"
    || state === "failed";
}

function terminalPaperBundleTaskStats(
  task: PaperBundleTask,
  progress?: RunProgress,
  finishedAt = Date.now(),
): Pick<PaperBundleTask, "started_at" | "finished_at" | "attempts" | "max_attempts"> {
  const attempts = Math.max(task.attempts ?? 0, progress?.counts.attempts ?? 0);
  const maxAttempts = Math.max(task.max_attempts ?? 0, progress?.counts.max_attempts ?? 0);
  return {
    started_at: task.started_at ?? progress?.started_at ?? finishedAt,
    finished_at: task.finished_at ?? finishedAt,
    ...(attempts > 0 ? { attempts } : { attempts: undefined }),
    ...(maxAttempts > 0 ? { max_attempts: maxAttempts } : { max_attempts: undefined }),
  };
}

class RunWaitCancelledError extends Error {
  constructor() {
    super("Run cancelled.");
    this.name = "RunWaitCancelledError";
  }
}

function isSetupError(err: unknown): err is ApiError {
  return err instanceof ApiError
    && (
      err.code === "no_api_key"
      || err.code === "missing_external_author_command"
      || err.code === "missing_code_editor_command"
      || err.code === "video_runtime_unavailable"
    );
}

function setupErrorText(err: ApiError, apiKeyMessage: string): string {
  if (err.code === "no_api_key") return apiKeyMessage;
  if (err.code === "missing_code_editor_command") {
    return err.message || "Poster revision setup required — configure the external code editor command.";
  }
  if (err.code === "video_runtime_unavailable") {
    return err.message || "Video runtime setup required — run autodesign setup, then retry this Video task.";
  }
  return err.message || "Paper poster setup required — configure the external author command.";
}

function triggerStoreDownload(url: string, filename: string) {
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.rel = "noreferrer";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}

const GENERATE_TASK: RecoverableTaskType = "generate";
const POSTER_CODE_EDIT_TASK: RecoverableTaskType = "poster_code_edit";
const PPTX_EXPORT_TASK: RecoverableTaskType = "artifact_export_pptx";
const CANDIDATE_PUBLISH_TASK: RecoverableTaskType = "candidate_publish";
const PPTX_EXPORT_PREFIX = "Export this design as an editable PPTX:";
const ORPHANED_PPTX_EXPORT_MESSAGE = "PowerPoint export was interrupted. Resume to try again.";

function isPptxExportMessage(message?: Pick<Message, "text" | "task_type"> | null): boolean {
  return Boolean(
    message
    && (
      message.task_type === PPTX_EXPORT_TASK
      || message.text.trim().startsWith(PPTX_EXPORT_PREFIX)
    ),
  );
}

function activeCandidatePublishMessage(conversation: Conversation): Message | undefined {
  return [...conversation.messages].reverse().find((message) => (
    message.role === "assistant"
    && message.status === "streaming"
    && message.task_type === CANDIDATE_PUBLISH_TASK
    && Boolean(message.run_id)
  ));
}

export const candidatePublishOperationId = (conversationId: string) =>
  `${conversationId}:candidate-publish`;

const attemptForkOperationId = (conversationId: string) =>
  `${conversationId}:attempt-fork`;

function candidatePublicationProgressIsActive(
  progress: RunProgress | undefined,
): boolean {
  return Boolean(
    progress?.mode === "attempt_publish"
    && (
      progress.phase === "queued"
      || progress.phase === "running"
      || progress.phase === "cancelling"
    )
  );
}

export function candidatePublicationIsActive(
  state: {
    candidate_publication_owners: Record<string, CandidatePublicationReactiveOwner>;
    conversations: Record<string, Conversation>;
    runs_progress: Record<string, RunProgress>;
  },
  conversationId: string,
): boolean {
  const conversation = state.conversations[conversationId];
  return Boolean(
    state.candidate_publication_owners[conversationId]
    || (conversation && activeCandidatePublishMessage(conversation))
    || candidatePublicationProgressIsActive(
      state.runs_progress[candidatePublishOperationId(conversationId)],
    )
    || candidatePublicationProgressIsActive(state.runs_progress[conversationId])
  );
}

function candidatePublishMessageMatches(
  conversation: Conversation | undefined,
  messageId: string,
  runId: string,
): boolean {
  return Boolean(conversation?.messages.some((message) => (
    message.id === messageId
    && message.task_type === CANDIDATE_PUBLISH_TASK
    && message.run_id === runId
  )));
}

function ownsCandidatePublishMessage(
  conversation: Conversation | undefined,
  messageId: string,
  runId: string,
): boolean {
  return candidatePublishMessageMatches(conversation, messageId, runId)
    && conversation?.messages.some((message) => (
      message.id === messageId && message.status === "streaming"
    )) === true;
}

function hasActivePptxOperation(conversationId: string, conversation?: Conversation): boolean {
  return _ACTIVE_PPTX_EXPORT_CONVERSATIONS.has(conversationId)
    || Boolean(conversation?.messages.some((message) => (
      message.status === "streaming" && isPptxExportMessage(message)
    )));
}

function acquirePptxOperation(conversationId: string, conversation: Conversation): () => void {
  if (hasActivePptxOperation(conversationId, conversation)) {
    throw new Error("A PowerPoint export is already running for this conversation.");
  }
  _ACTIVE_PPTX_EXPORT_CONVERSATIONS.add(conversationId);
  return () => {
    _ACTIVE_PPTX_EXPORT_CONVERSATIONS.delete(conversationId);
  };
}

function isHtmlPosterArtifact(artifact?: Artifact): artifact is Artifact {
  return Boolean(
    artifact
    && artifactTypeForArtifact(artifact) === "poster"
    && artifact.native_format === "html"
    && artifact.native_file_url
  );
}

function htmlArtifactForPptxExport(artifact?: Artifact): artifact is Artifact {
  return Boolean(
    artifact
    && (!artifact.native_format || artifact.native_format === "html")
    && (artifact.native_file_url || artifact.view_file_url || artifact.download_url)
  );
}

function resolvePptxExportArtifact(
  conv: Conversation,
  priorUser: Message,
  failedIndex: number,
): Artifact | undefined {
  if (priorUser.source_artifact_id) {
    const direct = conv.artifacts[priorUser.source_artifact_id];
    if (htmlArtifactForPptxExport(direct)) return direct;
  }
  const before = conv.messages.slice(0, failedIndex).reverse();
  for (const msg of before) {
    if (!msg.artifact_id) continue;
    const art = conv.artifacts[msg.artifact_id];
    if (htmlArtifactForPptxExport(art)) return art;
  }
  const active = conv.active_artifact_id ? conv.artifacts[conv.active_artifact_id] : undefined;
  return htmlArtifactForPptxExport(active) ? active : undefined;
}

function attachmentRefsFromMessage(message: Message): MessageTaskPayload["attachment_refs"] {
  const { content } = partitionReferenceAttachments(message.attachments ?? []);
  const refs = content.map((a) => ({
    name: a.name,
    size: a.size,
    kind: a.kind,
    role: a.role,
  })) ?? [];
  return refs.length ? refs : undefined;
}

function referencePosterRefFromMessage(message: Message): MessageTaskPayload["reference_poster_ref"] {
  const { reference } = partitionReferenceAttachments(message.attachments ?? []);
  return reference
    ? {
        name: reference.name,
        size: reference.size,
        kind: reference.kind,
        role: reference.role,
        reference_handle: reference.reference_handle,
      }
    : undefined;
}

function sourceArtifactFromMessage(
  conv: Conversation,
  message?: Pick<Message, "source_artifact_id" | "task_payload"> | null,
): Artifact | undefined {
  const payloadSource = message?.task_payload?.source_artifact_id;
  const sourceId = payloadSource || message?.source_artifact_id;
  return sourceId ? conv.artifacts[sourceId] : undefined;
}

function latestHtmlPosterArtifactBefore(
  conv: Conversation,
  failedIndex: number,
): Artifact | undefined {
  const before = conv.messages.slice(0, failedIndex).reverse();
  for (const msg of before) {
    if (!msg.artifact_id) continue;
    const art = conv.artifacts[msg.artifact_id];
    if (isHtmlPosterArtifact(art)) return art;
  }
  const active = conv.active_artifact_id ? conv.artifacts[conv.active_artifact_id] : undefined;
  return isHtmlPosterArtifact(active) ? active : undefined;
}

interface RecoverableTask {
  task_type: RecoverableTaskType;
  instruction: string;
  task_payload?: MessageTaskPayload;
  source_artifact?: Artifact;
}

function resolveRecoverableTask(
  conv: Conversation,
  failedIndex: number,
  failedMsg: Message,
): RecoverableTask | null {
  const priorUser = conv.messages
    .slice(0, failedIndex)
    .reverse()
    .find((message) => message.role === "user" && message.text.trim());
  if (!priorUser) return null;

  const task_type = failedMsg.task_type || priorUser.task_type;
  const task_payload = failedMsg.task_payload || priorUser.task_payload;
  const instruction = priorUser.text.trim();
  const source_artifact = sourceArtifactFromMessage(conv, failedMsg)
    || sourceArtifactFromMessage(conv, priorUser);

  if (task_type === PPTX_EXPORT_TASK || isPptxExportMessage(priorUser)) {
    const source = htmlArtifactForPptxExport(source_artifact)
      ? source_artifact
      : resolvePptxExportArtifact(conv, priorUser, failedIndex);
    return source
      ? {
          task_type: PPTX_EXPORT_TASK,
          instruction,
          source_artifact: source,
          task_payload: {
            ...(task_payload ?? {}),
            source_artifact_id: source.artifact_id,
            export_format: "pptx",
          },
        }
      : null;
  }

  if (task_type === POSTER_CODE_EDIT_TASK) {
    const source = isHtmlPosterArtifact(source_artifact)
      ? source_artifact
      : latestHtmlPosterArtifactBefore(conv, failedIndex);
    return source
      ? {
          task_type: POSTER_CODE_EDIT_TASK,
          instruction,
          source_artifact: source,
          task_payload: {
            ...(task_payload ?? {}),
            source_artifact_id: source.artifact_id,
          },
        }
      : null;
  }

  if (!task_type && !(priorUser.attachments?.length) && latestHtmlPosterArtifactBefore(conv, failedIndex)) {
    const source = latestHtmlPosterArtifactBefore(conv, failedIndex);
    if (source) {
      return {
        task_type: POSTER_CODE_EDIT_TASK,
        instruction,
        source_artifact: source,
        task_payload: {
          source_artifact_id: source.artifact_id,
        },
      };
    }
  }

  return {
    task_type: GENERATE_TASK,
    instruction,
    task_payload: {
      ...(task_payload ?? {}),
      artifact_type: task_payload?.artifact_type || failedMsg.failure?.artifact_type,
      attachment_refs: task_payload?.attachment_refs || attachmentRefsFromMessage(priorUser),
      reference_poster_ref: task_payload?.reference_poster_ref || referencePosterRefFromMessage(priorUser),
    },
  };
}

// ---- Manual save plumbing (in-place editor) ------------------------
// These live outside Zustand because they are transient request guards.
// Edits accumulate in `pending_edits`; only the toolbar Save button or
// Cmd+S sends them through /api/edits/apply.
let _autosave_in_flight = false;
let _autosave_dirty_after_save = false;

const isStructuredPendingEdits = (edits: PendingEditsPayload | undefined): edits is PendingArtifactEdits => {
  if (!edits || typeof edits !== "object") return false;
  return "layers" in edits || "layout" in edits;
};

const pendingLayerEdits = (
  edits: PendingEditsPayload | undefined,
): Record<string, Partial<Layer>> => {
  if (!edits) return {};
  if (isStructuredPendingEdits(edits)) return edits.layers ?? {};
  return edits;
};

export const hasPendingEditsPayload = (edits: PendingEditsPayload | undefined): boolean => {
  if (!edits) return false;
  if (!isStructuredPendingEdits(edits)) return Object.keys(edits).length > 0;
  return Object.keys(edits.layers ?? {}).length > 0 || (edits.layout ?? []).length > 0;
};

const mergeLayerEdit = (
  edits: PendingEditsPayload | undefined,
  layerId: string,
  patch: Partial<Layer>,
): PendingArtifactEdits => {
  const layers = pendingLayerEdits(edits);
  const prior = layers[layerId] ?? {};
  const layout = isStructuredPendingEdits(edits) ? edits.layout : undefined;
  return {
    layers: {
      ...layers,
      [layerId]: {
        ...prior,
        ...patch,
        effects: { ...prior.effects, ...(patch.effects ?? {}) },
      },
    },
    ...(layout && layout.length ? { layout } : {}),
  };
};

const sameLayoutPatchTarget = (a: HtmlLayoutPatch, b: HtmlLayoutPatch): boolean => {
  if (a.kind !== b.kind) return false;
  if (
    (a.kind === "section_height" || a.kind === "section_size")
    && (b.kind === "section_height" || b.kind === "section_size")
  ) {
    return a.section_id === b.section_id;
  }
  if (a.kind === "column_widths" && b.kind === "column_widths") {
    return a.columns_id === b.columns_id;
  }
  if (a.kind === "poster_style" && b.kind === "poster_style") {
    return (a.scope === "section" ? a.section_id : "global")
      === (b.scope === "section" ? b.section_id : "global");
  }
  return a.kind === "section_order";
};

const mergeLayoutEdit = (
  edits: PendingEditsPayload | undefined,
  patch: HtmlLayoutPatch,
): PendingArtifactEdits => {
  const layers = pendingLayerEdits(edits);
  const priorLayout = isStructuredPendingEdits(edits) ? edits.layout ?? [] : [];
  return {
    ...(Object.keys(layers).length ? { layers } : {}),
    layout: [
      ...priorLayout.filter((existing) => !sameLayoutPatchTarget(existing, patch)),
      patch,
    ],
  };
};

export type SelectionMode = "replace" | "add" | "toggle";
export type AlignMode = "left" | "center" | "right" | "top" | "middle" | "bottom";
export type DistributeMode = "horizontal" | "vertical";
export type InsertPlacement = "single" | "frame-relative" | "absolute";
export type InsertPlacementMode = "near-selection" | "center" | "click-to-place";

export interface InsertLayersOptions {
  placement?: InsertPlacement;
  select?: boolean;
  strategy?: "near-selection" | "center" | "point";
  anchor?: { x: number; y: number };
}

export interface SendMessageOptions {
  selection_context?: PosterSelectionContext;
  authoring_max_attempts?: number;
}

export interface PendingInsert {
  layers: Layer[];
  placement: InsertPlacement;
}

export interface EditorStyleClipboard {
  kind: "text" | "shape" | "image";
  patch: Partial<Layer>;
}

interface EditorHistory {
  past: Artifact[];
  future: Artifact[];
}

interface UpdateOptions {
  history?: boolean;
}

// How many user+assistant turns to replay in the brief preamble. Each
// turn is truncated server-side; this is just the raw cap. Bigger →
// more context but longer prompt. 6 keeps the preamble around ~250
// tokens, comfortable margin under designer's input budget.
const HISTORY_TURNS_MAX = 6;
// Skip transient streaming/error placeholders — they don't represent
// useful agent output and would confuse the designer if echoed back.
const _isUsefulMessage = (m: Message): boolean => {
  if (!m.text || !m.text.trim()) return false;
  if (m.status === "streaming") return false;
  if (m.status === "error") return false;
  return true;
};

const connectionLostFailure = (
  message: string,
  artifact_type?: ArtifactType | null,
): Message["failure"] => ({
  status: "connection_lost",
  phase: "connection",
  agent_last_note: message,
  produced_files: [],
  ...(artifact_type ? { artifact_type } : {}),
});

class RunArtifactDeliveryError extends Error {
  readonly runId: string;
  constructor(runId: string, cause: unknown) {
    super(
      cause instanceof Error
        ? `The run completed, but its artifact could not be delivered: ${cause.message}`
        : "The run completed, but its artifact could not be delivered.",
      { cause },
    );
    this.runId = runId;
  }
}

const primaryRunClientError = (error: unknown): unknown => {
  if (!(error instanceof AggregateError)) return error;
  return error.errors.find(
    (candidate) => candidate instanceof RunStatusError && !candidate.retryable,
  )
    ?? error.errors.find((candidate) => candidate instanceof RunArtifactDeliveryError)
    ?? error.errors[0]
    ?? error;
};

const runClientFailure = (
  error: unknown,
  artifact_type?: ArtifactType | null,
  runId?: string,
): Message["failure"] => {
  if (error instanceof RunArtifactDeliveryError) {
    return {
      status: "artifact_delivery_failed",
      run_id: error.runId,
      phase: "artifact_delivery",
      error_code: "artifact_delivery_unavailable",
      error_message: error.message,
      agent_last_note: error.message,
      produced_files: [],
      ...(artifact_type ? { artifact_type } : {}),
    };
  }
  if (error instanceof RunStatusError && !error.retryable) {
    return {
      status: "run_status_unavailable",
      ...(runId ? { run_id: runId } : {}),
      phase: "run_status",
      error_code: `run_status_${error.kind}`,
      error_message: error.message,
      agent_last_note: error.message,
      produced_files: [],
      ...(artifact_type ? { artifact_type } : {}),
    };
  }
  return connectionLostFailure(
    error instanceof Error ? error.message : "unknown",
    artifact_type,
  );
};

/** Build the per-turn memory the backend's `_apply_conversation_prologue`
 *  consumes. Pure projection — no fetches, no I/O. */
function buildConversationMemory(conv: Conversation | undefined): {
  history: Array<{ role: "user" | "assistant"; text: string; artifact_id?: string }>;
  artifacts: Array<{
    artifact_id: string;
    name: string;
    type: ArtifactType;
    canvas: { w: number; h: number };
    native_format?: string;
  }>;
} {
  if (!conv) return { history: [], artifacts: [] };
  const history = conv.messages
    .filter(_isUsefulMessage)
    .slice(-HISTORY_TURNS_MAX)
    .map((m) => ({
      role: m.role,
      text: m.text,
      ...(m.artifact_id ? { artifact_id: m.artifact_id } : {}),
    }));
  // Artifacts ordered by creation index in the conversation. Chat
  // history is already temporally ordered, so we walk it and dedupe.
  const seen = new Set<string>();
  const artifacts: ReturnType<typeof buildConversationMemory>["artifacts"] = [];
  for (const m of conv.messages) {
    const aid = m.artifact_id;
    if (!aid || seen.has(aid)) continue;
    const a = conv.artifacts[aid];
    if (!a) continue;
    seen.add(aid);
    artifacts.push({
      artifact_id: a.artifact_id,
      name: a.name,
      type: a.artifact_type,
      canvas: { w: a.canvas.w, h: a.canvas.h },
      ...(a.native_format ? { native_format: a.native_format } : {}),
    });
  }
  for (const a of Object.values(conv.artifacts)) {
    if (seen.has(a.artifact_id)) continue;
    seen.add(a.artifact_id);
    artifacts.push({
      artifact_id: a.artifact_id,
      name: a.name,
      type: a.artifact_type,
      canvas: { w: a.canvas.w, h: a.canvas.h },
      ...(a.native_format ? { native_format: a.native_format } : {}),
    });
  }
  return { history, artifacts };
}

interface AppStore {
  // app-wide UI state
  mode: AppMode;
  selected_layer_id: string | null;
  selected_layer_ids: string[];
  intent_type: ArtifactType | null;
  history_sidebar_open: boolean;
  properties_sidebar_open: boolean;
  design_focus_mode: boolean;
  /** Populated by loadBackendInfo() on app boot. */
  backend_info: BackendInfo | null;
  /** True if the backend booted without any .env credential. Drives the
   *  first-run "set up your API key" CTA. Updated alongside backend_info
   *  on every loadBackendInfo() call. */
  backend_needs_setup: boolean;
  /** Whether the Settings drawer is open. */
  settings_open: boolean;
  ui_language: UiLanguage;
  /** Per-conversation phase progress. Lives outside `Conversation`
   *  itself so the (very chatty) per-event mutations don't trigger
   *  localStorage writes — only the small `pending` + `run_id` flags
   *  on the conversation get persisted. */
  runs_progress: Record<string, RunProgress>;
  candidate_publication_owners: Record<
    string,
    CandidatePublicationReactiveOwner
  >;
  run_attempts: Record<string, RunAttemptState>;
  poster_palettes: PosterPalette[];
  poster_palettes_status: PosterPaletteStatus;
  poster_palettes_error: string | null;
  poster_canvas_presets: PosterCanvasPreset[];
  poster_canvas_presets_status: PosterCanvasPresetStatus;
  poster_canvas_presets_error: string | null;
  canvas_validation_errors: Record<string, { brief: string; message: string }>;

  // multi-conversation
  conversations: Record<string, Conversation>;
  current_conversation_id: string;
  history_user_scope: string | null;

  // boot
  loadBackendInfo: () => Promise<void>;
  loadServerHistory: () => Promise<void>;
  hydrateServerHistoryConversation: (id: string) => Promise<void>;
  loadPosterPalettes: () => Promise<void>;
  loadPosterCanvasPresets: () => Promise<void>;
  recoverActiveRuns: () => void;
  recoverPaperBundles: () => Promise<void>;
  loadRunAttempts: (runId: string) => Promise<void>;
  selectAttempt: (
    runId: string,
    candidate: AttemptCandidateSummary,
    conversationId?: string,
  ) => Promise<void>;
  openAttemptInCanvas: (
    runId: string,
    candidate: AttemptCandidateSummary,
    conversationId?: string,
  ) => Promise<void>;
  publishActiveCandidateDraft: (
    target: CandidateDraftPublicationTarget,
  ) => Promise<void>;
  // settings drawer
  openSettings: () => void;
  closeSettings: () => void;
  setUiLanguage: (language: UiLanguage) => void;

  // mode
  enterCanvas: (artifact_id?: string) => void;
  enterChat: () => void;

  // sidebar visibility
  toggleHistorySidebar: () => void;
  togglePropertiesSidebar: () => void;
  setDesignFocusMode: (focused: boolean) => void;
  toggleDesignFocusMode: () => void;
  // sidebar widths — drag-resizable, persisted to localStorage
  history_sidebar_width: number;   // default 260, range 200–420
  chat_rail_width: number;         // default 320, range 260–500
  properties_sidebar_width: number;// default 320, range 240–520
  setSidebarWidth: (
    which: "history" | "chat_rail" | "properties",
    px: number
  ) => void;
  // Deck nav bar (slide thumbnail strip) height — drag-resizable on
  // its bottom edge. Thumbnail dims derive from this so dragging
  // visually grows/shrinks the slides too.
  deck_navbar_height: number;      // default 120, range 80–320
  setDeckNavBarHeight: (px: number) => void;
  // Active slide index for editable (layer-mode) deck artifacts.
  // Drives single-slide canvas rendering: clicking a thumbnail switches
  // which slide's layers the canvas shows. Resets when the active
  // artifact changes.
  active_slide_idx: number;
  setActiveSlideIdx: (idx: number) => void;
  addSlideAfter: () => void;
  duplicateActiveSlide: () => void;
  deleteActiveSlide: () => void;
  moveActiveSlide: (dir: "up" | "down") => void;
  moveActiveSlideToIndex: (targetIdx: number) => void;
  snap_enabled: boolean;
  toggleSnap: () => void;
  grid_visible: boolean;
  rulers_visible: boolean;
  safe_margins_visible: boolean;
  smart_guides_visible: boolean;
  grid_size_px: number;
  grid_major_every: number;
  safe_margin_pct: number;
  toggleGrid: () => void;
  toggleRulers: () => void;
  toggleSafeMargins: () => void;
  toggleSmartGuides: () => void;
  setGridSize: (px: number) => void;
  setSafeMarginPct: (pct: number) => void;
  recent_colors: string[];
  rememberColor: (color: string) => void;
  insert_placement_mode: InsertPlacementMode;
  setInsertPlacementMode: (mode: InsertPlacementMode) => void;
  pending_insert: PendingInsert | null;
  setPendingInsert: (layers: Layer[], options?: { placement?: InsertPlacement }) => void;
  cancelPendingInsert: () => void;
  commitPendingInsert: (anchor: { x: number; y: number }) => void;
  layer_group_collapsed: Record<string, boolean>;
  setLayerGroupCollapsed: (group_id: string, collapsed: boolean) => void;

  // conversation management
  newConversation: () => void;
  switchConversation: (id: string) => void;
  deleteConversation: (id: string) => void;
  renameConversation: (id: string, title: string) => void;
  setPosterPalette: (paletteId: string | null) => void;
  setPosterCanvasPreset: (presetId: string) => void;
  clearCanvasValidationError: (conversationId?: string) => void;
  /** Inject a hard-coded demo artifact (no agent call, no money spent).
   *  Used to dry-run the canvas UI — DeckNavBar, in-place editor, etc. */
  loadDemoDeck: () => void;
  /** Inject a layer-based editable poster (no agent call, no money
   *  spent). Used to dogfood the canvas editor controls. */
  loadDemoPoster: () => void;
  /** Inject a layer-based editable slide stack (no agent call, no money). */
  loadDemoSlides: () => void;
  /** Inject a layer-based editable landing page (no agent call, no money). */
  loadDemoLanding: () => void;
  /** Inject a layer-based editable video project (no agent call, no money). */
  loadDemoVideo: () => void;

	  // intent (quick-action card / in-composer pill selection)
	  setIntent: (type: ArtifactType | null) => void;

	  // visual area revision selection (transient composer context)
	  area_revision_active: boolean;
	  area_revision_items: PosterAreaSelectionItem[];
	  area_revision_focus_id: string | null;
	  selected_paper_asset: ArtifactAsset | null;
	  setAreaRevisionActive: (active: boolean) => void;
	  setAreaRevisionItems: (items: PosterAreaSelectionItem[]) => void;
	  addAreaRevisionItem: (
	    item: PosterAreaSelectionItem,
	    options?: { append?: boolean; toggle?: boolean },
	  ) => void;
	  updateAreaRevisionItemInstruction: (selection_id: string, instruction: string) => void;
	  removeAreaRevisionItem: (selection_id: string) => void;
	  clearAreaRevisionItems: () => void;
	  focusAreaRevisionItem: (selection_id: string) => void;
	  setSelectedPaperAsset: (asset: ArtifactAsset | null) => void;

	  // chat
	  sendMessage: (text: string, attachments: Attachment[], options?: SendMessageOptions) => Promise<void>;
  startPaperBundle: (file: File, parentConversationId?: string) => Promise<void>;
  cancelPaperBundleTask: (
    parentConversationId: string,
    artifactType: ArtifactType,
  ) => Promise<void>;
  retryPaperBundleTask: (
    parentConversationId: string,
    artifactType: ArtifactType,
  ) => Promise<void>;
  cancelPaperBundle: (parentConversationId?: string) => Promise<void>;
  exportArtifactPptx: (artifact_id?: string) => Promise<void>;
  /** Request cancellation for the current conversation's in-flight run.
   *  Local state remains live until the backend confirms the complete
   *  worker/process tree has stopped. */
  cancelRun: (conversation_id?: string) => Promise<void>;
  /** Re-fire a failed run. The FailureCard's "Retry with X" CTA calls
   *  this with the assistant message's id and an optional designer
   *  override (e.g., Claude Opus 4.7 for paper-poster Kimi stalls).
   *  Backend reuses the original brief + uploaded files, so the user
   *  doesn't have to re-upload or re-type. */
  retryRun: (
    message_id: string,
    designer_override?: string,
    video_export_only?: boolean,
    authoring_run_id?: string,
    on_run_started?: (run_id: string) => void,
  ) => Promise<string | undefined>;
  /** Re-run the user turn that produced a connection-lost assistant
   *  message. Unlike retryRun(), this does not depend on backend
   *  in-memory `_RUNS`, so it works after uvicorn restarted. */
  resumeRun: (message_id: string) => Promise<void>;

  // artifact / layer ops (operate on the active conversation's active artifact)
  selectLayer: (layer_id: string | null, mode?: SelectionMode) => void;
  setSelection: (layer_ids: string[]) => void;
  clearSelection: () => void;
  syncLayerInspection: (layer_id: string, patch: Partial<Layer>) => void;
  updateLayer: (layer_id: string, patch: Partial<Layer>, opts?: UpdateOptions) => void;
  recordHtmlLayoutPatch: (patch: HtmlLayoutPatch) => void;
  replaceActiveArtifactPendingEdits: (edits?: PendingEditsPayload) => void;
  addLayer: (layer: Layer) => void;
  insertLayers: (layers: Layer[], options?: InsertLayersOptions) => void;
  removeLayer: (layer_id: string) => void;
  reorderLayer: (layer_id: string, dir: "up" | "down", scope_layer_ids?: string[]) => void;
  reorderLayerBlock: (
    layer_ids: string[],
    target_layer_ids: string[],
    position: "before" | "after",
    scope_layer_ids?: string[]
  ) => void;
  reorderSelection: (dir: "up" | "down") => void;
  toggleLayerProp: (layer_id: string, prop: "visible" | "locked") => void;
  toggleGroupProp: (group_id: string, prop: "visible" | "locked") => void;
  deleteGroup: (group_id: string) => void;
  setSelectionLocked: (locked: boolean) => void;
  groupSelection: () => void;
  ungroupSelection: (group_id?: string) => void;
  renameGroup: (group_id: string, name: string) => void;
  updateCanvas: (patch: Partial<Artifact["canvas"]>) => void;
  captureHistorySnapshot: () => void;
  undo: () => void;
  redo: () => void;
  editor_history: Record<string, EditorHistory>;
  editor_clipboard: Layer[];
  editor_clipboard_groups: LayerGroup[];
  editor_style_clipboard: EditorStyleClipboard | null;
  copySelection: () => void;
  pasteSelection: () => void;
  copySelectionStyle: () => void;
  pasteSelectionStyle: () => void;
  updateSelectionStyle: (patch: Partial<Layer>) => void;
  duplicateSelection: () => void;
  deleteSelection: () => void;
  alignSelection: (mode: AlignMode, reference?: Bbox) => void;
  distributeSelection: (mode: DistributeMode) => void;
  nudgeSelection: (dx: number, dy: number) => void;
  recordArtifactDownloaded: (artifact_id?: string) => void;
  submitOpenResearchProject: (artifact_id?: string, options?: OpenResearchSubmitOptions) => Promise<void>;
  updateVideoSceneDuration: (scene_id: string, duration_s: number) => void;
  renderActiveVideo: () => Promise<void>;

  // edit round-trip (native HTML artifacts only — Sidebar Apply footer)
  applyEdits: () => Promise<void>;
  discardEdits: () => void;
  pending_apply: boolean;

  // ---- Manual save (in-place canvas editor) ----
  /** Lifecycle of the apply_edits round-trip the Save button triggers. */
  autosave_state: "idle" | "editing" | "saving" | "saved" | "error";
  /** Wall-clock of the last successful save. */
  autosave_last_saved_at: number | null;
  /** Last apply-edits failure message — null on success. */
  autosave_error: string | null;
  /** Mark the active native-HTML artifact as having unsaved edits. */
  scheduleAutoSave: () => void;
  /** Save now. Triggered by the toolbar button or Cmd+S. */
  flushAutoSave: () => Promise<void>;
}

const DEFAULT_BRIEF: Record<ArtifactType, string> = {
  poster: "Create a poster",
  landing: "Create a landing page",
  deck: "Create a slide deck",
  video: "Generate a narrated video walk-through",
};

const normalizeColor = (color: string | undefined): string | null => {
  const clean = (color ?? "").trim().toLowerCase();
  if (!/^#[0-9a-f]{6}$/.test(clean)) return null;
  return clean;
};

const freshConversation = (): Conversation => {
  const now = Date.now();
  return {
    id: nextId("conv"),
    title: "New chat",
    created_at: now,
    updated_at: now,
    messages: [],
    artifacts: {},
    active_artifact_id: null,
    poster_palette_id: null,
    poster_canvas_preset_id: "auto",
  };
};

type PosterPaletteStatus = "idle" | "loading" | "ready" | "error";
type PosterCanvasPresetStatus = "idle" | "loading" | "ready" | "error";

const ARTIFACT_TYPES = new Set<ArtifactType>(["poster", "deck", "landing", "video"]);
const VIDEO_ARTIFACT_MARKERS = [
  "video", "mp4", "movie", "动画", "视频", "讲解视频", "演示视频",
];
const DECK_ARTIFACT_MARKERS = [
  "slides", "slide deck", "deck", "ppt", "pptx", "keynote",
  "presentation", "talk deck", "幻灯片", "演示文稿", "汇报", "路演",
];
const LANDING_ARTIFACT_MARKERS = [
  "landing page", "project page", "paper page", "web page",
  "homepage", "website", "site", "网页", "页面", "网站", "官网",
  "主页", "项目页", "介绍页",
];

/** Keep request classification aligned with scripts.web_server._coerce_artifact_type. */
export const effectiveArtifactType = (
  intent: unknown,
  brief: string,
): ArtifactType => {
  if (typeof intent === "string" && ARTIFACT_TYPES.has(intent as ArtifactType)) {
    return intent as ArtifactType;
  }
  const compact = brief.trim().toLowerCase().replace(/\s+/g, " ");
  if (VIDEO_ARTIFACT_MARKERS.some((marker) => compact.includes(marker))) return "video";
  if (DECK_ARTIFACT_MARKERS.some((marker) => compact.includes(marker))) return "deck";
  if (LANDING_ARTIFACT_MARKERS.some((marker) => compact.includes(marker))) return "landing";
  if (compact.includes("page") && !["poster", "海报"].some((marker) => compact.includes(marker))) {
    return "landing";
  }
  if (["poster", "海报"].some((marker) => compact.includes(marker))) return "poster";
  return "poster";
};

const ARTIFACT_TYPE_LABELS: Record<ArtifactType, string> = {
  poster: "Poster",
  deck: "Slide deck",
  landing: "Landing page",
  video: "Video",
};
const LAYER_KINDS = new Set<Layer["kind"]>(["background", "text", "image", "shape", "section"]);
const MAX_PERSISTED_CONVERSATIONS = 60;
const MAX_PERSISTED_MESSAGES = 120;
const MAX_HISTORY_SIDEBAR_ITEMS = 40;
const SERVER_HISTORY_IMPORT_LIMIT = 25;
const MAX_INLINE_SRC_CHARS = 2048;
const AREA_REVISION_MAX_ITEMS = 6;
const AREA_REVISION_MAX_HTML_CHARS = 1000;
const AREA_REVISION_MAX_TEXT_CHARS = 900;
const AREA_REVISION_MAX_INSTRUCTION_CHARS = 700;
const AREA_REVISION_MAX_DRAW_POINTS = 160;

const artifactTypeFromFileUrl = (value: unknown): ArtifactType | null => {
  if (typeof value !== "string" || !value.trim()) return null;
  const pathname = value.split(/[?#]/, 1)[0]?.toLowerCase() ?? "";
  if (pathname.endsWith("/poster.html")) return "poster";
  if (pathname.endsWith("/index.html")) return "landing";
  if (pathname.endsWith("/deck.html") || pathname.endsWith("/deck.pptx")) return "deck";
  if (pathname.endsWith(".mp4")) return "video";
  return null;
};

export const artifactTypeForArtifact = (artifact: Artifact): ArtifactType =>
  artifactTypeFromFileUrl(artifact.native_file_url)
  ?? artifactTypeFromFileUrl(artifact.view_file_url)
  ?? artifactTypeFromFileUrl(artifact.download_url)
  ?? artifact.artifact_type;

const finiteNumber = (v: unknown, fallback: number): number => {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
};

const positiveNumber = (v: unknown, fallback: number): number => {
  const n = finiteNumber(v, fallback);
  return n > 0 ? n : fallback;
};

const nonNegativeNumber = (v: unknown): number | undefined =>
  typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : undefined;

const isPlainRecord = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === "object" && !Array.isArray(v);

const areaRevisionLabel = (item: PosterAreaSelectionItem): string => {
  const clean = item.label?.trim();
  if (clean) return clean.slice(0, 80);
  if (item.block_id) return item.block_id.slice(0, 80);
  if (item.kind === "drawing") return "Drawn markup";
  if (item.kind === "region") return "Selected region";
  return "Selected section";
};

const normalizeAreaRect = (rect: Bbox): Bbox => ({
  x: Math.round(finiteNumber(rect.x, 0)),
  y: Math.round(finiteNumber(rect.y, 0)),
  w: Math.max(1, Math.round(finiteNumber(rect.w, 1))),
  h: Math.max(1, Math.round(finiteNumber(rect.h, 1))),
});

const compactAreaDrawingPoints = (
  points: Array<{ x: number; y: number }>,
): Array<{ x: number; y: number }> => {
  const rounded = points
    .filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y))
    .map((point) => ({ x: Math.round(point.x), y: Math.round(point.y) }));
  if (rounded.length <= AREA_REVISION_MAX_DRAW_POINTS) return rounded;
  const step = Math.ceil(rounded.length / AREA_REVISION_MAX_DRAW_POINTS);
  const sampled = rounded.filter((_, idx) => idx % step === 0);
  const last = rounded[rounded.length - 1];
  if (last && sampled[sampled.length - 1] !== last) sampled.push(last);
  return sampled.slice(0, AREA_REVISION_MAX_DRAW_POINTS);
};

const normalizeAreaRevisionItem = (
  item: PosterAreaSelectionItem,
): PosterAreaSelectionItem => {
  const drawing_paths = item.drawing_paths
    ?.map((path) => ({
      points: compactAreaDrawingPoints(path.points ?? []),
      color: path.color,
      width_px: path.width_px,
    }))
    .filter((path) => path.points.length >= 2);
  return {
    ...item,
    selection_id: item.selection_id || nextId("area"),
    label: areaRevisionLabel(item),
    instruction: typeof item.instruction === "string"
      ? item.instruction.slice(0, AREA_REVISION_MAX_INSTRUCTION_CHARS)
      : undefined,
    rect: normalizeAreaRect(item.rect),
    text_excerpt: item.text_excerpt?.slice(0, AREA_REVISION_MAX_TEXT_CHARS),
    html_excerpt: item.html_excerpt?.slice(0, AREA_REVISION_MAX_HTML_CHARS),
    nearby_headings: item.nearby_headings
      ?.map((heading) => heading.trim())
      .filter(Boolean)
      .slice(0, 6),
    drawing_paths: drawing_paths && drawing_paths.length ? drawing_paths : undefined,
  };
};

const areaRevisionKey = (item: PosterAreaSelectionItem): string => {
  if (item.kind === "element") {
    return `element:${item.block_id || item.selector || item.selection_id}`;
  }
  return item.selection_id;
};

const unionAreaRect = (items: PosterAreaSelectionItem[]): Bbox | null => {
  if (items.length === 0) return null;
  const left = Math.min(...items.map((item) => item.rect.x));
  const top = Math.min(...items.map((item) => item.rect.y));
  const right = Math.max(...items.map((item) => item.rect.x + item.rect.w));
  const bottom = Math.max(...items.map((item) => item.rect.y + item.rect.h));
  return normalizeAreaRect({ x: left, y: top, w: right - left, h: bottom - top });
};

const buildAreaSelectionContext = (
  items: PosterAreaSelectionItem[],
): PosterSelectionContext | undefined => {
  const safeItems = items.slice(0, AREA_REVISION_MAX_ITEMS).map(normalizeAreaRevisionItem);
  if (safeItems.length === 0) return undefined;
  if (safeItems.length === 1) {
    const single = safeItems[0];
    const instruction = single.instruction?.trim() || undefined;
    return {
      kind: single.kind,
      rect: single.rect,
      instruction,
      selector: single.selector,
      block_id: single.block_id,
      text_excerpt: single.text_excerpt,
      html_excerpt: single.html_excerpt,
      nearby_headings: single.nearby_headings,
      drawing_paths: single.drawing_paths,
    };
  }
  const rect = unionAreaRect(safeItems);
  if (!rect) return undefined;
  return {
    kind: "multi",
    rect,
    items: safeItems.map((item) => ({
      ...item,
      instruction: item.instruction?.trim() || undefined,
    })),
  };
};

const buildAreaSelectionSummaryFromItems = (
  items: PosterAreaSelectionItem[],
): PosterSelectionSummary | undefined => {
  const safeItems = items.slice(0, AREA_REVISION_MAX_ITEMS).map(normalizeAreaRevisionItem);
  if (safeItems.length === 0) return undefined;
  return {
    kind: "area_selection",
    count: safeItems.length,
    labels: safeItems.map(areaRevisionLabel),
    item_kinds: safeItems.map((item) => item.kind),
    area_instructions: safeItems
      .map((item, idx) => ({ item, idx }))
      .filter(({ item }) => item.instruction?.trim())
      .map(({ item, idx }) => ({
        index: idx + 1,
        label: areaRevisionLabel(item),
        instruction: item.instruction!.trim(),
      })),
  };
};

const buildAreaSelectionSummaryFromContext = (
  context: PosterSelectionContext | undefined,
): PosterSelectionSummary | undefined => {
  if (!context) return undefined;
  if (context.kind === "multi" && context.items?.length) {
    return buildAreaSelectionSummaryFromItems(context.items);
  }
  const kind = context.kind as PosterAreaSelectionKind;
  const label =
    context.block_id
      ?? context.nearby_headings?.[0]
      ?? (kind === "drawing" ? "Drawn markup" : kind === "region" ? "Selected region" : "Selected section");
  return {
    kind: "area_selection",
    count: 1,
    labels: [label],
    item_kinds: [kind],
    area_instructions: context.instruction?.trim()
      ? [{ index: 1, label, instruction: context.instruction.trim() }]
      : undefined,
  };
};

const buildAreaInstructionBrief = (items: PosterAreaSelectionItem[]): string => {
  const notes = items
    .slice(0, AREA_REVISION_MAX_ITEMS)
    .map(normalizeAreaRevisionItem)
    .map((item, idx) => ({ item, idx }))
    .filter(({ item }) => item.instruction?.trim())
    .map(({ item, idx }) => `${idx + 1}. ${areaRevisionLabel(item)}: ${item.instruction!.trim()}`);
  return notes.length
    ? `Apply these selected-area edits:\n${notes.join("\n")}`
    : "";
};

const normalizeLayer = (raw: unknown, idx: number): Layer | null => {
  if (!isPlainRecord(raw)) return null;
  const rawKind = raw.kind;
  const kind = LAYER_KINDS.has(rawKind as Layer["kind"])
    ? (rawKind as Layer["kind"])
    : "shape";
  const layer_id =
    typeof raw.layer_id === "string" && raw.layer_id.trim()
      ? raw.layer_id
      : `legacy_layer_${idx}`;
  const bbox = isPlainRecord(raw.bbox)
    ? {
        x: finiteNumber(raw.bbox.x, 0),
        y: finiteNumber(raw.bbox.y, 0),
        w: positiveNumber(raw.bbox.w, 1),
        h: positiveNumber(raw.bbox.h, 1),
      }
    : undefined;
  return {
    ...(raw as Partial<Layer>),
    layer_id,
    name:
      typeof raw.name === "string" && raw.name.trim()
        ? raw.name
        : layer_id,
    kind,
    z_index: finiteNumber(raw.z_index, idx),
    bbox,
  } as Layer;
};

const normalizeArtifact = (raw: unknown): Artifact | null => {
  if (!isPlainRecord(raw)) return null;
  const raw_type = ARTIFACT_TYPES.has(raw.artifact_type as ArtifactType)
    ? (raw.artifact_type as ArtifactType)
    : null;
  const file_type =
    artifactTypeFromFileUrl(raw.native_file_url)
    ?? artifactTypeFromFileUrl(raw.view_file_url)
    ?? artifactTypeFromFileUrl(raw.download_url);
  const artifact_type = file_type ?? raw_type;
  const artifact_id =
    typeof raw.artifact_id === "string" && raw.artifact_id.trim()
      ? raw.artifact_id
      : null;
  if (!artifact_type || !artifact_id) return null;
  const typeCorrected = !!file_type && !!raw_type && file_type !== raw_type;
  const rawName = typeof raw.name === "string" && raw.name.trim()
    ? raw.name.trim()
    : "";
  const name = typeCorrected && rawName
    ? rawName.replace(
        /^(Poster|Slide deck|Landing page|Video)\s+—\s+/,
        `${ARTIFACT_TYPE_LABELS[artifact_type]} — `,
      )
    : rawName;
  const canvasRaw = isPlainRecord(raw.canvas) ? raw.canvas : {};
  const canvas = {
    w: positiveNumber(canvasRaw.w, artifact_type === "poster" ? 900 : 1280),
    h: positiveNumber(canvasRaw.h, artifact_type === "poster" ? 1200 : 720),
    background:
      typeof canvasRaw.background === "string"
        ? canvasRaw.background
        : undefined,
  };
  const layers = Array.isArray(raw.layers)
    ? raw.layers
        .map((layer, idx) => normalizeLayer(layer, idx))
        .filter((layer): layer is Layer => !!layer)
    : [];
  return {
    ...(raw as Partial<Artifact>),
    artifact_id,
    name: name || `${artifact_type} artifact`,
    artifact_type,
    canvas,
    layers,
  } as Artifact;
};

const paperBundlePublicationArtifactMatches = (
  artifact: Artifact | null | undefined,
  artifactType: ArtifactType,
  publication: PaperBundlePublicationDescriptor,
): artifact is Artifact => {
  const lineage = artifact?.attempt_lineage;
  return !!artifact
    && artifact.artifact_id === publication.artifact_id
    && artifact.artifact_type === artifactType
    && artifact.candidate_draft !== true
    && lineage?.status === "published"
    && lineage.source_run_id === publication.source_run_id
    && lineage.source_attempt === publication.source_attempt
    && lineage.source_candidate_id === publication.source_candidate_id
    && lineage.source_candidate_sha256 === publication.source_candidate_sha256;
};

const paperBundleEditedArtifactDescendsFromPublication = (
  artifact: Artifact | null | undefined,
  artifactType: ArtifactType,
  publication: PaperBundlePublicationDescriptor,
): artifact is Artifact => {
  const lineage = artifact?.attempt_lineage;
  return !!artifact
    && artifact.artifact_type === artifactType
    && artifact.candidate_draft === true
    && lineage?.status === "draft"
    && lineage.published_artifact_id_at_fork === publication.artifact_id
    && lineage.source_run_id === publication.source_run_id
    && lineage.source_attempt === publication.source_attempt
    && lineage.source_candidate_id === publication.source_candidate_id
    && lineage.source_candidate_sha256 === publication.source_candidate_sha256;
};

const validatedPaperBundlePublicationArtifact = (
  result: GenerateResponse,
  artifactType: ArtifactType,
  publication: PaperBundlePublicationDescriptor,
): Artifact | null => {
  const artifact = normalizeArtifact(result.artifact);
  if (
    result.message.run_id !== publication.publication_run_id
    || result.message.artifact_id !== publication.artifact_id
    || result.message.status !== "done"
    || result.message.failure != null
    || !paperBundlePublicationArtifactMatches(artifact, artifactType, publication)
  ) {
    return null;
  }
  return artifact;
};

const PAPER_BUNDLE_TASK_STATUSES = new Set<PaperBundleTaskStatus>([
  "pending",
  "uploading",
  "running",
  "cancelling",
  "complete",
  "failed",
  "cancelled",
]);
const PAPER_BUNDLE_BACKEND_STATES = new Set<PaperBundleBackendState>([
  "reserved",
  "running",
  "cancelling",
  "cancelled",
  "completed",
  "partial",
  "failed",
]);

const normalizePaperBundleTask = (
  raw: unknown,
  artifactType: ArtifactType,
): PaperBundleTask | null => {
  if (!isPlainRecord(raw)) return null;
  if (raw.artifact_type !== artifactType) return null;
  if (
    typeof raw.child_conversation_id !== "string"
    || !raw.child_conversation_id.trim()
    || !PAPER_BUNDLE_TASK_STATUSES.has(raw.status as PaperBundleTaskStatus)
  ) {
    return null;
  }
  const startedAt = nonNegativeNumber(raw.started_at);
  const finishedAt = nonNegativeNumber(raw.finished_at);
  const attempts = nonNegativeNumber(raw.attempts);
  const maxAttempts = nonNegativeNumber(raw.max_attempts);
  return {
    artifact_type: artifactType,
    child_conversation_id: raw.child_conversation_id,
    status: raw.status as PaperBundleTaskStatus,
    ...(typeof raw.run_id === "string" && raw.run_id ? { run_id: raw.run_id } : {}),
    ...(typeof raw.authoring_run_id === "string" && raw.authoring_run_id
      ? { authoring_run_id: raw.authoring_run_id }
      : {}),
    ...(typeof raw.artifact_id === "string" && raw.artifact_id
      ? { artifact_id: raw.artifact_id }
      : {}),
    ...(typeof raw.error === "string" && raw.error ? { error: raw.error } : {}),
    ...(startedAt !== undefined ? { started_at: startedAt } : {}),
    ...(finishedAt !== undefined ? { finished_at: finishedAt } : {}),
    ...(attempts !== undefined ? { attempts: Math.floor(attempts) } : {}),
    ...(maxAttempts !== undefined ? { max_attempts: Math.floor(maxAttempts) } : {}),
    ...(typeof raw.terminal === "boolean" ? { terminal: raw.terminal } : {}),
    ...(typeof raw.process_free === "boolean" ? { process_free: raw.process_free } : {}),
  };
};

const normalizePaperBundle = (raw: unknown): PaperBundleState | undefined => {
  if (!isPlainRecord(raw)) return undefined;
  if (raw.kind === "child") {
    if (
      typeof raw.parent_conversation_id !== "string"
      || !raw.parent_conversation_id.trim()
      || !ARTIFACT_TYPES.has(raw.artifact_type as ArtifactType)
    ) {
      return undefined;
    }
    return {
      kind: "child",
      parent_conversation_id: raw.parent_conversation_id,
      artifact_type: raw.artifact_type as ArtifactType,
    };
  }
  if (
    raw.kind !== "parent"
    || raw.prompt_version !== 1
    || typeof raw.source_name !== "string"
    || !raw.source_name.trim()
    || !isPlainRecord(raw.tasks)
  ) {
    return undefined;
  }
  const rawTasks = raw.tasks;
  const tasks = Object.fromEntries(
    PAPER_BUNDLE_ARTIFACT_ORDER.map((artifactType) => [
      artifactType,
      normalizePaperBundleTask(rawTasks[artifactType], artifactType),
    ]),
  );
  if (Object.values(tasks).some((task) => !task)) return undefined;
  return {
    kind: "parent",
    prompt_version: 1,
    source_name: raw.source_name,
    tasks: tasks as NonNullable<Extract<PaperBundleState, { kind: "parent" }>["tasks"]>,
    ...(typeof raw.job_id === "string" && raw.job_id ? { job_id: raw.job_id } : {}),
    ...(Number.isInteger(raw.revision) && (raw.revision as number) >= 0
      ? { revision: raw.revision as number }
      : {}),
    ...(
      typeof raw.backend_state === "string"
      && PAPER_BUNDLE_BACKEND_STATES.has(raw.backend_state as PaperBundleBackendState)
        ? { backend_state: raw.backend_state as PaperBundleBackendState }
        : {}
    ),
    ...(typeof raw.cancel_error === "string" && raw.cancel_error
      ? { cancel_error: raw.cancel_error }
      : {}),
  };
};

const paperBundleHasActiveTasks = (
  bundle: Extract<PaperBundleState, { kind: "parent" }>,
): boolean => PAPER_BUNDLE_ARTIFACT_ORDER.some((artifactType) => {
  const status = bundle.tasks[artifactType].status;
  return status === "pending"
    || status === "uploading"
    || status === "running"
    || status === "cancelling";
});

const normalizeHistoryLastRun = (raw: unknown): Conversation["history_last_run"] => {
  if (!isPlainRecord(raw) || typeof raw.run_id !== "string" || !raw.run_id.trim()) {
    return undefined;
  }
  const status = raw.status === "streaming" || raw.status === "done" || raw.status === "error"
    ? raw.status
    : undefined;
  const artifact_id = typeof raw.artifact_id === "string" && raw.artifact_id.trim()
    ? raw.artifact_id
    : undefined;
  return { run_id: raw.run_id, status, artifact_id };
};

const normalizeConversation = (raw: unknown): Conversation | null => {
  if (!isPlainRecord(raw)) return null;
  const id = typeof raw.id === "string" && raw.id.trim() ? raw.id : null;
  if (!id) return null;
  const artifacts: Record<string, Artifact> = {};
  const rawArtifacts = isPlainRecord(raw.artifacts) ? raw.artifacts : {};
  for (const [key, value] of Object.entries(rawArtifacts)) {
    const art = normalizeArtifact(value);
    if (art) artifacts[art.artifact_id || key] = art;
  }
  const active =
    typeof raw.active_artifact_id === "string" && artifacts[raw.active_artifact_id]
      ? raw.active_artifact_id
      : Object.keys(artifacts)[0] ?? null;
  const published = typeof raw.published_artifact_id === "string"
    && artifacts[raw.published_artifact_id]
    ? raw.published_artifact_id
    : raw.published_artifact_id === null
      ? null
      : undefined;
  const messages = Array.isArray(raw.messages) ? (raw.messages as Message[]) : [];
  const paperBundle = normalizePaperBundle(raw.paper_bundle);
  const historyLastRun = normalizeHistoryLastRun(raw.history_last_run);
  const now = Date.now();
  return {
    id,
    title:
      typeof raw.title === "string" && raw.title.trim()
        ? raw.title
        : "New chat",
    created_at: finiteNumber(raw.created_at, now),
    updated_at: finiteNumber(raw.updated_at, finiteNumber(raw.created_at, now)),
    messages,
    artifacts,
    active_artifact_id: active,
    published_artifact_id: published,
    poster_palette_id: restoredPosterPaletteId(messages, raw.poster_palette_id),
    poster_canvas_preset_id: restoredPosterCanvasPresetId(
      messages,
      raw.poster_canvas_preset_id,
    ),
    paper_bundle: paperBundle,
    pending: paperBundle?.kind === "parent"
      ? paperBundleHasActiveTasks(paperBundle)
      : raw.pending === true
        ? true
        : undefined,
    run_id: typeof raw.run_id === "string" ? raw.run_id : undefined,
    history_summary: raw.history_summary === true ? true : undefined,
    history_source_id: typeof raw.history_source_id === "string"
      ? raw.history_source_id
      : undefined,
    history_message_count: finiteNumber(raw.history_message_count, 0) || undefined,
    history_last_run: historyLastRun,
    pending_edits: isPlainRecord(raw.pending_edits) ? raw.pending_edits as Conversation["pending_edits"] : {},
  };
};

const conversationFromServerHistorySummary = (
  summary: ServerHistoryConversationSummary,
): Conversation => {
  const legacy = summary as unknown as { messages?: unknown };
  if (Array.isArray(legacy.messages)) {
    const conversation = normalizeConversation(summary);
    if (conversation) return conversation;
  }
  const artifacts: Record<string, Artifact> = {};
  for (const [artifactId, preview] of Object.entries(summary.artifacts)) {
    artifacts[preview.artifact_id || artifactId] = {
      ...preview,
      layers: [],
    };
  }
  const pendingArtifactType = summary.pending_artifact_type
    ?? summary.pending_task_payload?.artifact_type;
  const pendingMessage: Message[] = summary.pending
    && summary.run_id
    && pendingArtifactType
    ? [{
        id: `msg_${summary.run_id}`,
        role: "assistant",
        text: "",
        ts: summary.updated_at,
        run_id: summary.run_id,
        status: "streaming",
        task_type: summary.pending_task_type ?? GENERATE_TASK,
        task_payload: summary.pending_task_payload
          ?? { artifact_type: pendingArtifactType },
      }]
    : [];
  const active_artifact_id = summary.active_artifact_id
    && artifacts[summary.active_artifact_id]
    ? summary.active_artifact_id
    : Object.keys(artifacts)[0] ?? null;
  return {
    id: summary.id,
    title: summary.title,
    created_at: summary.created_at,
    updated_at: summary.updated_at,
    messages: pendingMessage,
    artifacts,
    active_artifact_id,
    poster_palette_id: summary.poster_palette_id,
    poster_canvas_preset_id: restoredPosterCanvasPresetId(
      pendingMessage,
      summary.poster_canvas_preset_id,
    ),
    pending: summary.pending,
    run_id: summary.run_id,
    pending_edits: {},
    history_summary: true,
    history_source_id: summary.id,
    history_message_count: summary.message_count || undefined,
    history_last_run: normalizeHistoryLastRun(summary.last_run),
  };
};

const normalizeConversations = (
  raw: unknown,
): Record<string, Conversation> => {
  if (!isPlainRecord(raw)) return {};
  const out: Record<string, Conversation> = {};
  for (const [key, value] of Object.entries(raw)) {
    const conv = normalizeConversation(value);
    if (conv) out[conv.id || key] = conv;
  }
  return out;
};

const interruptOrphanedPptxExports = (
  conversations: Record<string, Conversation>,
): Record<string, Conversation> => Object.fromEntries(
  Object.entries(conversations).map(([id, conversation]) => {
    let interrupted = false;
    const messages = conversation.messages.map((message) => {
      if (
        message.role === "assistant"
        && message.status === "streaming"
        && isPptxExportMessage(message)
      ) {
        interrupted = true;
        return {
          ...message,
          text: ORPHANED_PPTX_EXPORT_MESSAGE,
          status: "error" as const,
          task_type: PPTX_EXPORT_TASK,
          failure: connectionLostFailure(ORPHANED_PPTX_EXPORT_MESSAGE),
        };
      }
      return message;
    });
    return [
      id,
      interrupted
        ? { ...conversation, messages, pending: undefined, run_id: undefined }
        : conversation,
    ];
  }),
);

const hasRehydratedOrphanedPptxExport = (conversation: Conversation): boolean => (
  conversation.messages.some((message) => (
    message.role === "assistant"
    && message.status === "error"
    && isPptxExportMessage(message)
    && message.text === ORPHANED_PPTX_EXPORT_MESSAGE
    && message.failure?.status === "connection_lost"
  ))
);

const validatePosterPaletteSelections = (
  conversations: Record<string, Conversation>,
  status: PosterPaletteStatus,
  palettes: PosterPalette[],
): Record<string, Conversation> => {
  if (status !== "ready") return conversations;
  const paletteIds = new Set(palettes.map((palette) => palette.id));
  const now = Date.now();
  let changed = false;
  const validated = Object.fromEntries(
    Object.entries(conversations).map(([id, conversation]) => {
      if (!conversation.poster_palette_id || paletteIds.has(conversation.poster_palette_id)) {
        return [id, conversation];
      }
      changed = true;
      return [id, { ...conversation, poster_palette_id: null, updated_at: now }];
    }),
  );
  return changed ? validated : conversations;
};

const validatePosterCanvasSelections = (
  conversations: Record<string, Conversation>,
  status: PosterCanvasPresetStatus,
  presets: PosterCanvasPreset[],
): Record<string, Conversation> => {
  if (status !== "ready") return conversations;
  const presetIds = new Set(presets.map((preset) => preset.id));
  const now = Date.now();
  let changed = false;
  const validated = Object.fromEntries(
    Object.entries(conversations).map(([id, conversation]) => {
      const selected = conversation.poster_canvas_preset_id ?? "auto";
      if (presetIds.has(selected)) return [id, conversation];
      changed = true;
      return [id, {
        ...conversation,
        poster_canvas_preset_id: "auto",
        updated_at: now,
      }];
    }),
  );
  return changed ? validated : conversations;
};

const normalizePersistedShape = (
  raw: unknown,
  fallback: AppStore,
): Partial<PersistedShape> => {
  if (!isPlainRecord(raw)) return {};
  const persistedScope = typeof raw.history_user_scope === "string"
    ? raw.history_user_scope
    : null;
  const currentScope = currentDemoUserScope();
  if (persistedScope && persistedScope !== currentScope) {
    const fresh = freshConversation();
    return {
      ...(raw as Partial<PersistedShape>),
      conversations: { [fresh.id]: fresh },
      current_conversation_id: fresh.id,
      history_user_scope: currentScope,
      ui_language: isSupportedLanguage(raw.ui_language) ? raw.ui_language : "en",
    };
  }
  const conversations = interruptOrphanedPptxExports(
    normalizeConversations(raw.conversations),
  );
  const convKeys = Object.keys(conversations);
  const current =
    typeof raw.current_conversation_id === "string" &&
    conversations[raw.current_conversation_id]
      ? raw.current_conversation_id
      : convKeys[0] ?? fallback.current_conversation_id;
  return {
    ...(raw as Partial<PersistedShape>),
    conversations: convKeys.length ? conversations : fallback.conversations,
    current_conversation_id: current,
    history_user_scope: persistedScope,
    ui_language: isSupportedLanguage(raw.ui_language) ? raw.ui_language : "en",
  };
};

const compactLayerForStorage = (layer: Layer): Layer => {
  const next = { ...layer };
  if (
    typeof next.src === "string" &&
    next.src.startsWith("data:") &&
    next.src.length > MAX_INLINE_SRC_CHARS
  ) {
    delete next.src;
  }
  return next;
};

const compactArtifactForStorage = (artifact: Artifact): Artifact => {
  const art = normalizeArtifact(artifact) ?? artifact;
  const next: Artifact = {
    ...art,
    layers: (Array.isArray(art.layers) ? art.layers : []).map(compactLayerForStorage),
  };
  if (typeof next.preview_url === "string" && next.preview_url.startsWith("data:")) {
    delete next.preview_url;
  }
  if (typeof next.download_url === "string" && next.download_url.startsWith("data:")) {
    delete next.download_url;
  }
  if (typeof next.native_file_url === "string" && next.native_file_url.startsWith("data:")) {
    delete next.native_file_url;
    delete next.native_format;
  }
  if (typeof next.view_file_url === "string" && next.view_file_url.startsWith("data:")) {
    delete next.view_file_url;
    delete next.view_format;
  }
  return next;
};

const compactMessageForStorage = (message: Message): Message => ({
  ...message,
  attachments: message.attachments?.map((a) => {
    const { file: _file, ...rest } = a;
    return rest;
  }),
  failure: message.failure
    ? {
        ...message.failure,
        agent_last_note: message.failure.agent_last_note?.slice(0, 1200),
      }
    : undefined,
});

const compactConversationForStorage = (conversation: Conversation): Conversation => {
  const c = normalizeConversation(conversation) ?? conversation;
  const artifacts: Record<string, Artifact> = {};
  for (const [id, artifact] of Object.entries(c.artifacts ?? {})) {
    artifacts[id] = compactArtifactForStorage(artifact);
  }
  const messages = (Array.isArray(c.messages) ? c.messages : [])
    .slice(-MAX_PERSISTED_MESSAGES)
    .map(compactMessageForStorage);
  const candidatePublish = c.pending && c.run_id
    ? activeCandidatePublishMessage({ ...c, messages })
    : undefined;
  const preserveCandidatePublish = candidatePublish?.run_id === c.run_id;
  const preserveSourceRun = Boolean(
    c.pending
    && c.run_id
    && [...messages].reverse().some((message) => (
      message.role === "assistant"
      && message.status === "streaming"
      && message.task_type !== CANDIDATE_PUBLISH_TASK
      && (message.run_id === c.run_id || !message.run_id)
    )),
  );
  const preserveRunOwner = preserveCandidatePublish || preserveSourceRun;
  return {
    ...c,
    messages,
    artifacts,
    pending: preserveRunOwner ? true : undefined,
    run_id: preserveRunOwner ? c.run_id : undefined,
  };
};

const compactConversationsForStorage = (
  conversations: Record<string, Conversation>,
): Record<string, Conversation> => {
  const out: Record<string, Conversation> = {};
  for (const c of Object.values(conversations)
    .sort((a, b) => b.updated_at - a.updated_at)
    .slice(0, MAX_PERSISTED_CONVERSATIONS)) {
    out[c.id] = compactConversationForStorage(c);
  }
  return out;
};

const mergeServerConversation = (
  local: Conversation | undefined,
  remote: Conversation,
): Conversation => {
  const safeRemote = normalizeConversation(remote) ?? remote;
  const safeLocal = local ? normalizeConversation(local) ?? local : undefined;
  if (!safeLocal) return safeRemote;
  const remoteWins = safeRemote.updated_at >= safeLocal.updated_at;
  const primary = remoteWins ? safeRemote : safeLocal;
  const secondary = remoteWins ? safeLocal : safeRemote;
  const remoteIsSummary = safeRemote.history_summary === true;
  const localIsSummary = safeLocal.history_summary === true;
  const historySourceId = remoteIsSummary
    ? safeRemote.history_source_id ?? safeRemote.id
    : undefined;
  const summaryNeedsHydration = remoteIsSummary && (
    localIsSummary || historySourceId !== safeLocal.id
  );
  const localSummaryHasNewerState =
    localIsSummary && !remoteIsSummary && safeLocal.updated_at > safeRemote.updated_at;
  const messages: Message[] = [];
  const seen = new Set<string>();
  const messageSources = remoteIsSummary && !localIsSummary
    ? [safeLocal.messages]
    : localIsSummary && !remoteIsSummary
      ? localSummaryHasNewerState
        ? [safeRemote.messages, safeLocal.messages]
        : [safeRemote.messages]
      : [secondary.messages, primary.messages];
  for (const m of messageSources.flat()) {
    const key = m.id || `${m.role}:${m.run_id ?? ""}:${m.text}`;
    if (seen.has(key)) continue;
    seen.add(key);
    messages.push(m);
  }
  messages.sort((a, b) => a.ts - b.ts);
  // Server-imported artifacts are re-hydrated from out/runs, so they may gain
  // a richer editable shape after a web update (for example deck.html -> layers
  // or video MP4 -> scene project). Preserve local-only artifacts, but let the
  // canonical server artifact replace stale cached copies with the same id.
  const artifacts = remoteIsSummary && !localIsSummary
    ? { ...safeRemote.artifacts, ...safeLocal.artifacts }
    : localIsSummary && !remoteIsSummary
      ? { ...safeLocal.artifacts, ...safeRemote.artifacts }
      : { ...safeLocal.artifacts, ...safeRemote.artifacts };
  const active =
    primary.active_artifact_id && artifacts[primary.active_artifact_id]
      ? primary.active_artifact_id
      : secondary.active_artifact_id && artifacts[secondary.active_artifact_id]
        ? secondary.active_artifact_id
        : (() => {
            const keys = Object.keys(artifacts);
            return keys[keys.length - 1] ?? null;
          })();
  return {
    ...primary,
    created_at: Math.min(safeLocal.created_at, safeRemote.created_at),
    updated_at: Math.max(safeLocal.updated_at, safeRemote.updated_at),
    messages,
    artifacts,
    active_artifact_id: active,
    published_artifact_id: safeLocal.published_artifact_id
      ?? safeRemote.published_artifact_id,
    paper_bundle: safeLocal.paper_bundle ?? safeRemote.paper_bundle,
    pending: safeLocal.pending || safeRemote.pending || undefined,
    run_id: safeLocal.pending && safeLocal.run_id
      ? safeLocal.run_id
      : safeRemote.pending
        ? safeRemote.run_id
        : undefined,
    history_summary: summaryNeedsHydration ? true : undefined,
    history_source_id: summaryNeedsHydration ? historySourceId : undefined,
    history_message_count: summaryNeedsHydration
      ? Math.max(safeLocal.history_message_count ?? 0, safeRemote.history_message_count ?? 0) || undefined
      : undefined,
    history_last_run: remoteIsSummary
      ? safeRemote.history_last_run ?? safeLocal.history_last_run
      : localIsSummary
        ? undefined
        : safeLocal.history_last_run ?? safeRemote.history_last_run,
    pending_edits: safeLocal.pending_edits,
  };
};

const reconcilePaperBundleGraph = (
  conversations: Record<string, Conversation>,
): Record<string, Conversation> => {
  const next = { ...conversations };
  for (const parent of Object.values(conversations)) {
    const bundle = parent.paper_bundle;
    if (bundle?.kind !== "parent") continue;
    next[parent.id] = {
      ...parent,
      pending: paperBundleHasActiveTasks(bundle),
    };
    for (const artifactType of PAPER_BUNDLE_ARTIFACT_ORDER) {
      const task = bundle.tasks[artifactType];
      const active = isActivePaperBundleTaskStatus(task.status);
      const child = next[task.child_conversation_id] ?? conversationForRecoveredBundleTask(
        parent,
        artifactType,
      );
      next[task.child_conversation_id] = {
        ...child,
        paper_bundle: createPaperBundleChildState(parent.id, artifactType),
        pending: active,
        run_id: active ? task.run_id : undefined,
      };
    }
  }
  return next;
};

function conversationForRecoveredBundleTask(
  parent: Conversation,
  artifactType: ArtifactType,
): Conversation {
  const task = parent.paper_bundle?.kind === "parent"
    ? parent.paper_bundle.tasks[artifactType]
    : null;
  const active = !!task && isActivePaperBundleTaskStatus(task.status);
  return {
    id: task?.child_conversation_id ?? `${parent.id}:paper-bundle:${artifactType}`,
    title: `${parent.paper_bundle?.kind === "parent" ? parent.paper_bundle.source_name : parent.title} - ${ARTIFACT_TYPE_LABELS[artifactType]}`,
    created_at: parent.created_at,
    updated_at: parent.updated_at,
    messages: active
      ? [{
          id: nextId("msg"),
          role: "assistant",
          text: "",
          ts: parent.updated_at,
          status: "streaming",
          task_type: GENERATE_TASK,
          task_payload: { artifact_type: artifactType },
        }]
      : [],
    artifacts: {},
    active_artifact_id: null,
    poster_palette_id: artifactType === "poster" ? parent.poster_palette_id : null,
    poster_canvas_preset_id: artifactType === "poster"
      ? parent.poster_canvas_preset_id ?? "auto"
      : "auto",
    paper_bundle: createPaperBundleChildState(parent.id, artifactType),
    pending: active,
    run_id: active ? task?.run_id : undefined,
  };
}

const historySummaryTerminalMessage = (conversation: Conversation): Message | undefined => {
  const run = conversation.history_last_run;
  if (
    !conversation.history_summary
    || !run
    || (run.status !== "done" && run.status !== "error")
  ) {
    return undefined;
  }
  return {
    id: `history_${run.run_id}`,
    role: "assistant",
    text: "",
    ts: conversation.updated_at,
    run_id: run.run_id,
    artifact_id: run.artifact_id,
    status: run.status,
  };
};

const mergeServerHistoryConversations = (
  local: Record<string, Conversation>,
  incoming: Conversation[],
  userIsolated: boolean,
  preservedLocalIds: ReadonlySet<string> = new Set(),
): Record<string, Conversation> => {
  type LinkedBundleChild = {
    parentConversationId: string;
    childConversationId: string;
    artifactType: ArtifactType;
  };
  const linkedChildren = new Map<string, LinkedBundleChild>();
  const linkedRuns = new Map<string, LinkedBundleChild>();
  for (const conversation of Object.values(local)) {
    const bundle = conversation.paper_bundle;
    if (bundle?.kind !== "parent") continue;
    for (const artifactType of PAPER_BUNDLE_ARTIFACT_ORDER) {
      const task = bundle.tasks[artifactType];
      const linked = {
        parentConversationId: conversation.id,
        childConversationId: task.child_conversation_id,
        artifactType,
      };
      linkedChildren.set(task.child_conversation_id, linked);
      if (task.run_id) linkedRuns.set(task.run_id, linked);
    }
  }
  const ordinaryRuns = new Map<string, string>();
  const ordinaryConversations = Object.values(local)
    .filter((conversation) => !conversation.paper_bundle)
    .sort((a, b) => (
      Number(a.id.startsWith("server_run_"))
      - Number(b.id.startsWith("server_run_"))
    ));
  for (const conversation of ordinaryConversations) {
    const runIds = new Set<string>();
    if (conversation.run_id) runIds.add(conversation.run_id);
    if (conversation.history_last_run?.run_id) {
      runIds.add(conversation.history_last_run.run_id);
    }
    for (const message of conversation.messages) {
      const runId = runIdFromMessage(message);
      if (runId) runIds.add(runId);
    }
    for (const artifact of Object.values(conversation.artifacts)) {
      const sourceRunId = artifact.attempt_lineage?.source_run_id;
      if (sourceRunId) runIds.add(sourceRunId);
    }
    for (const runId of runIds) {
      if (!ordinaryRuns.has(runId)) ordinaryRuns.set(runId, conversation.id);
    }
  }

  const incomingLinks = new Map<string, LinkedBundleChild>();
  const incomingOrdinaryLinks = new Map<string, string>();
  for (const remote of incoming) {
    let linked = linkedChildren.get(remote.id);
    if (!linked && remote.run_id) linked = linkedRuns.get(remote.run_id);
    if (!linked && remote.history_last_run?.run_id) {
      linked = linkedRuns.get(remote.history_last_run.run_id);
    }
    if (!linked) {
      for (const message of remote.messages) {
        linked = linkedRuns.get(runIdFromMessage(message));
        if (linked) break;
      }
    }
    if (linked) incomingLinks.set(remote.id, linked);
    if (linked) continue;
    const runIds = new Set<string>();
    if (remote.run_id) runIds.add(remote.run_id);
    if (remote.history_last_run?.run_id) {
      runIds.add(remote.history_last_run.run_id);
    }
    for (const message of remote.messages) {
      const runId = runIdFromMessage(message);
      if (runId) runIds.add(runId);
    }
    for (const runId of runIds) {
      const conversationId = ordinaryRuns.get(runId);
      if (conversationId) {
        incomingOrdinaryLinks.set(remote.id, conversationId);
        break;
      }
    }
  }

  const retainedBundleParents = new Set<string>();
  for (const conversationId of preservedLocalIds) {
    const linked = linkedChildren.get(conversationId);
    if (linked) retainedBundleParents.add(linked.parentConversationId);
    if (local[conversationId]?.paper_bundle?.kind === "parent") {
      retainedBundleParents.add(conversationId);
    }
  }
  for (const linked of incomingLinks.values()) {
    retainedBundleParents.add(linked.parentConversationId);
  }

  const next: Record<string, Conversation> = userIsolated ? {} : { ...local };
  if (userIsolated) {
    for (const conversationId of preservedLocalIds) {
      if (local[conversationId]) next[conversationId] = local[conversationId];
    }
    for (const parentConversationId of retainedBundleParents) {
      const parent = local[parentConversationId];
      if (parent?.paper_bundle?.kind !== "parent") continue;
      next[parentConversationId] = parent;
      for (const task of Object.values(parent.paper_bundle.tasks)) {
        const child = local[task.child_conversation_id];
        if (child) next[task.child_conversation_id] = child;
      }
    }
  }

  for (const remote of incoming) {
    const linked = incomingLinks.get(remote.id);
    const ordinaryTargetId = incomingOrdinaryLinks.get(remote.id);
    const targetId = linked?.childConversationId ?? ordinaryTargetId ?? remote.id;
    if (
      userIsolated
      && (
        preservedLocalIds.has(targetId)
        || (linked && preservedLocalIds.has(linked.parentConversationId))
      )
    ) continue;
    const keepLocal = !userIsolated
      || !!linked
      || !!ordinaryTargetId;
    const merged = mergeServerConversation(
      keepLocal ? local[targetId] : undefined,
      targetId === remote.id ? remote : { ...remote, id: targetId },
    );
    if (targetId !== remote.id) delete next[remote.id];
    const linkedConversation = linked
      ? {
          ...merged,
          paper_bundle: createPaperBundleChildState(
            linked.parentConversationId,
            linked.artifactType,
          ),
        }
      : merged;
    next[targetId] = linkedConversation;
    if (!linked) continue;

    const parent = next[linked.parentConversationId];
    if (parent?.paper_bundle?.kind !== "parent") continue;
    const task = parent.paper_bundle.tasks[linked.artifactType];
    const terminalMessage = [...remote.messages].reverse().find((message) => (
      (message.status === "done" || message.status === "error")
      && (!task.run_id || runIdFromMessage(message) === task.run_id)
    )) ?? historySummaryTerminalMessage(remote);
    if (!terminalMessage) continue;
    if (task.run_id && runIdFromMessage(terminalMessage) !== task.run_id) continue;
    const artifactId = terminalMessage.artifact_id
      && linkedConversation.artifacts[terminalMessage.artifact_id]
      ? terminalMessage.artifact_id
      : remote.active_artifact_id
        && linkedConversation.artifacts[remote.active_artifact_id]
        ? remote.active_artifact_id
        : undefined;
    const promotedArtifact = artifactId
      ? linkedConversation.artifacts[artifactId]
      : undefined;
    const failureStatus = terminalMessage.failure?.status.toLowerCase() ?? "";
    const taskStatus: PaperBundleTaskStatus = promotedArtifact
      ? "complete"
      : failureStatus.includes("cancel")
        ? "cancelled"
        : "failed";
    const incomingTaskError = terminalMessage.failure
      ? terminalMessage.failure.agent_last_note
        || terminalMessage.text
        || terminalMessage.failure.status
      : undefined;
    const sameRunTaskError = task.run_id
      && task.run_id === runIdFromMessage(terminalMessage)
      ? task.error
      : undefined;
    const terminalFinishedAt = Math.max(
      task.started_at ?? 0,
      Number.isFinite(terminalMessage.ts) ? terminalMessage.ts : Date.now(),
    );
    const terminalRunId = runIdFromMessage(terminalMessage);
    const preserveFinishedAt = task.status === taskStatus
      && (
        !task.run_id
        || !terminalRunId
        || task.run_id === terminalRunId
      );
    const taskForStats = preserveFinishedAt
      ? task
      : { ...task, finished_at: undefined };
    const tasks = {
      ...parent.paper_bundle.tasks,
      [linked.artifactType]: {
        ...task,
        ...terminalPaperBundleTaskStats(taskForStats, undefined, terminalFinishedAt),
        status: taskStatus,
        ...(artifactId ? { artifact_id: artifactId } : {}),
        ...(taskStatus === "complete"
          ? {
              error: resolvedCompletedTaskError(
                terminalMessage.failure != null,
                incomingTaskError,
                sameRunTaskError,
              ),
            }
          : {
              error: incomingTaskError
                || terminalMessage.text
                || "Run failed.",
            }),
      },
    };
    const artifacts = promotedArtifact
      ? { ...parent.artifacts, [promotedArtifact.artifact_id]: promotedArtifact }
      : parent.artifacts;
    const activeArtifactId = parent.active_artifact_id && artifacts[parent.active_artifact_id]
      ? parent.active_artifact_id
      : artifactId ?? parent.active_artifact_id;
    next[parent.id] = {
      ...parent,
      updated_at: Math.max(parent.updated_at, linkedConversation.updated_at),
      artifacts,
      active_artifact_id: activeArtifactId,
      paper_bundle: { ...parent.paper_bundle, tasks },
      pending: paperBundleHasActiveTasks({ ...parent.paper_bundle, tasks }),
    };
  }
  const ownedDerivedArtifacts = new Set(
    Object.values(next)
      .filter((conversation) => !conversation.id.startsWith("server_run_"))
      .flatMap((conversation) => (
        Object.values(conversation.artifacts)
          .filter((artifact) => artifact.attempt_lineage?.source_run_id)
          .map((artifact) => artifact.artifact_id)
      )),
  );
  for (const [conversationId, conversation] of Object.entries(next)) {
    if (
      conversationId.startsWith("server_run_")
      && Object.keys(conversation.artifacts).some(
        (artifactId) => ownedDerivedArtifacts.has(artifactId),
      )
    ) {
      delete next[conversationId];
    }
  }
  return reconcilePaperBundleGraph(next);
};

const artifactHydrationKeys = (artifact: Artifact): string[] => {
  const keys = [
    artifact.artifact_id,
    artifact.native_file_url,
    artifact.view_file_url,
    artifact.download_url,
    artifact.preview_url,
  ];
  return keys.filter((v): v is string => typeof v === "string" && v.length > 0);
};

const buildServerArtifactIndex = (
  conversations: Conversation[],
): Map<string, Artifact> => {
  const index = new Map<string, Artifact>();
  for (const conversation of conversations) {
    if (conversation.history_summary) continue;
    for (const artifact of Object.values(conversation.artifacts)) {
      for (const key of artifactHydrationKeys(artifact)) {
        index.set(key, artifact);
      }
    }
  }
  return index;
};

const hydrateLocalArtifactsFromServer = (
  conversations: Record<string, Conversation>,
  serverArtifacts: Map<string, Artifact>,
  preservedLocalIds: ReadonlySet<string> = new Set(),
): Record<string, Conversation> => {
  if (serverArtifacts.size === 0) return conversations;
  const next = { ...conversations };
  for (const [conversationId, conversation] of Object.entries(conversations)) {
    if (preservedLocalIds.has(conversationId)) continue;
    let changed = false;
    const artifacts = { ...conversation.artifacts };
    for (const [artifactId, artifact] of Object.entries(conversation.artifacts)) {
      const server = artifactHydrationKeys(artifact)
        .map((key) => serverArtifacts.get(key))
        .find((candidate): candidate is Artifact => !!candidate);
      if (!server) continue;
      artifacts[artifactId] = {
        ...server,
        artifact_id: artifact.artifact_id,
        parent_artifact_id: artifact.parent_artifact_id ?? server.parent_artifact_id,
        openresearch: server.openresearch ?? artifact.openresearch,
      };
      changed = true;
    }
    if (changed) {
      next[conversationId] = { ...conversation, artifacts };
    }
  }
  return next;
};

const initialConversation = freshConversation();

const isStorageQuotaError = (error: unknown): boolean =>
  error instanceof DOMException &&
  (error.name === "QuotaExceededError" ||
    error.name === "NS_ERROR_DOM_QUOTA_REACHED" ||
    error.code === 22 ||
    error.code === 1014);

const fallbackPersistedValue = (raw: string): string | null => {
  try {
    const parsed = JSON.parse(raw) as { state?: Partial<PersistedShape>; version?: number };
    if (!parsed || typeof parsed !== "object" || !parsed.state) return null;
    const conversations = normalizeConversations(parsed.state.conversations);
    const currentId = parsed.state.current_conversation_id;
    const current =
      typeof currentId === "string" && conversations[currentId]
        ? conversations[currentId]
        : Object.values(conversations).sort((a, b) => b.updated_at - a.updated_at)[0];
    return JSON.stringify({
      ...parsed,
      state: {
        ...parsed.state,
        conversations: current ? { [current.id]: compactConversationForStorage(current) } : {},
        current_conversation_id: current?.id ?? null,
      },
    });
  } catch {
    return null;
  }
};

const PERSIST_KEY = "autodesign.web.v1";
const LEGACY_PERSIST_KEY = "designanything.web.v1";

const safeStorage: StateStorage = {
  getItem: (name) => {
    const current = window.localStorage.getItem(name);
    if (current !== null || name !== PERSIST_KEY) return current;
    const legacy = window.localStorage.getItem(LEGACY_PERSIST_KEY);
    if (legacy === null) return null;
    try {
      window.localStorage.setItem(PERSIST_KEY, legacy);
      window.localStorage.removeItem(LEGACY_PERSIST_KEY);
    } catch {
      // Return the legacy state even if quota prevents eager migration.
    }
    return legacy;
  },
  setItem: (name, value) => {
    try {
      window.localStorage.setItem(name, value);
    } catch (error) {
      if (!isStorageQuotaError(error)) throw error;
      const fallback = fallbackPersistedValue(value);
      try {
        if (fallback) {
          window.localStorage.setItem(name, fallback);
        } else {
          window.localStorage.removeItem(name);
        }
      } catch {
        window.localStorage.removeItem(name);
      }
    }
  },
  removeItem: (name) => {
    window.localStorage.removeItem(name);
    if (name === PERSIST_KEY) window.localStorage.removeItem(LEGACY_PERSIST_KEY);
  },
};

// Whitelisted keys we round-trip through localStorage. Anything not in
// this list (e.g. transient run state, file handles) is rebuilt from
// scratch on each session boot. The version tag triggers a wipe on
// schema bumps — bump it whenever the persisted shape becomes
// backward-incompatible.
type PersistedShape = Pick<AppStore,
  | "conversations"
  | "current_conversation_id"
  | "history_user_scope"
  | "history_sidebar_open"
  | "properties_sidebar_open"
  | "design_focus_mode"
  | "history_sidebar_width"
  | "chat_rail_width"
  | "properties_sidebar_width"
  | "deck_navbar_height"
  | "grid_visible"
  | "rulers_visible"
  | "safe_margins_visible"
  | "smart_guides_visible"
  | "grid_size_px"
  | "grid_major_every"
  | "safe_margin_pct"
  | "recent_colors"
  | "insert_placement_mode"
  | "layer_group_collapsed"
  | "ui_language"
>;

const persistOptions: PersistOptions<AppStore, PersistedShape> = {
  name: PERSIST_KEY,
  version: 1,
  storage: createJSONStorage(() => safeStorage),
  partialize: (s): PersistedShape => ({
    conversations: compactConversationsForStorage(s.conversations),
    current_conversation_id: s.current_conversation_id,
    history_user_scope: s.history_user_scope,
    history_sidebar_open: s.history_sidebar_open,
    properties_sidebar_open: s.properties_sidebar_open,
    design_focus_mode: s.design_focus_mode,
    history_sidebar_width: s.history_sidebar_width,
    chat_rail_width: s.chat_rail_width,
    properties_sidebar_width: s.properties_sidebar_width,
    deck_navbar_height: s.deck_navbar_height,
    grid_visible: s.grid_visible,
    rulers_visible: s.rulers_visible,
    safe_margins_visible: s.safe_margins_visible,
    smart_guides_visible: s.smart_guides_visible,
    grid_size_px: s.grid_size_px,
    grid_major_every: s.grid_major_every,
    safe_margin_pct: s.safe_margin_pct,
    recent_colors: s.recent_colors,
    insert_placement_mode: s.insert_placement_mode,
    layer_group_collapsed: s.layer_group_collapsed,
    ui_language: s.ui_language,
  }),
  merge: (persistedState, currentState): AppStore => ({
    ...currentState,
    ...normalizePersistedShape(persistedState, currentState),
  }),
  onRehydrateStorage: () => (state, error) => {
    if (!error && state) {
      queueMicrotask(() => { void state.recoverPaperBundles(); });
    }
  },
  // On rehydrate, conversations carry references to artifacts whose
  // native_file_url points at out/runs/<id>/. If the user wiped the
  // out/ tree, those URLs 404 — we don't pre-emptively prune; the
  // ArtifactThumbnail handles the broken-image case gracefully.
};

export const useApp = create<AppStore>()(persist((set, get) => {
  // --- internal helpers (capture set so we don't repeat the boilerplate) ---

  const patchConversation = (
    id: string,
    mutator: (c: Conversation) => Conversation
  ) => {
    set((s) => {
      const c = s.conversations[id];
      if (!c) return s;
      const next = mutator(c);
      if (next === c) return s;
      return {
        conversations: {
          ...s.conversations,
          [id]: { ...next, updated_at: Date.now() },
        },
      };
    });
  };

  const cloneArtifact = (a: Artifact): Artifact =>
    JSON.parse(JSON.stringify(a)) as Artifact;

  const cloneLayer = (l: Layer): Layer =>
    JSON.parse(JSON.stringify(l)) as Layer;

  const openResearchStateFromResult = (
    result: OpenResearchProjectResult,
  ): NonNullable<Artifact["openresearch"]> => ({
    status: result.status,
    job_id: result.job_id,
    result_url: result.result_url,
    api_log_url: result.api_log_url,
    project_id: result.project_id,
    project_url: result.project_url,
    org_id: result.org_id,
    paper_id: result.paper_id,
    repo_full_name: result.repo_full_name,
    gui_submitter_status: result.gui_submitter_status,
    gui_submitter_reason: result.gui_submitter_reason,
    gui_submitter_error: result.gui_submitter_error,
    gui_submitter_session_url: result.gui_submitter_session_url,
    agent_prompt_url: result.agent_prompt_url,
    submitter_log_url: result.submitter_log_url,
    latest_report_id: result.latest_report_id,
    latest_report_url: result.latest_report_url,
    error: result.error,
  });

  const waitForOpenResearchResult = async (
    jobId: string,
    onUpdate: (state: NonNullable<Artifact["openresearch"]>) => void,
  ): Promise<NonNullable<Artifact["openresearch"]>> => {
    const started = Date.now();
    let delayMs = 1000;
    let latest: NonNullable<Artifact["openresearch"]> = {
      status: "running",
      job_id: jobId,
    };
    while (Date.now() - started < 60 * 60 * 1000) {
      const result = await fetchOpenResearchProject(jobId);
      latest = openResearchStateFromResult(result);
      onUpdate(latest);
      if (latest.status !== "running") return latest;
      await new Promise((resolve) => window.setTimeout(resolve, delayMs));
      delayMs = Math.min(5000, Math.round(delayMs * 1.5));
    }
    return latest;
  };

  const withoutGroupId = (layer: Layer): Layer => {
    const next = { ...layer };
    delete next.group_id;
    return next;
  };

  const getActiveContext = () => {
    const convId = get().current_conversation_id;
    const conv = get().conversations[convId];
    const artId = conv?.active_artifact_id;
    const art = artId ? conv?.artifacts[artId] : undefined;
    return { convId, conv, artId, art };
  };

  const resolveReservedRunStart = async ({
    request,
    reservedRunId,
    isCurrent,
    placeholderMessage,
    progressMode,
  }: {
    request: Promise<GenerateAck>;
    reservedRunId: () => string | undefined;
    isCurrent: (runId: string) => boolean;
    placeholderMessage: Message;
    progressMode?: string;
  }): Promise<{
    ack: GenerateAck;
    reconcileImmediately: boolean;
    startReplay?: RunStartReplay;
  }> => {
    try {
      return { ack: await request, reconcileImmediately: false };
    } catch (error) {
      if (!(error instanceof RunStartAmbiguousError)) throw error;
      const runId = reservedRunId();
      if (runId !== error.runId || !isCurrent(runId)) {
        throw new RunWaitCancelledError();
      }
      return {
        ack: {
          run_id: runId,
          progress_mode: progressMode,
          placeholder_message: {
            ...placeholderMessage,
            run_id: runId,
            status: "streaming",
          },
        },
        reconcileImmediately: true,
        startReplay: error.retryStart,
      };
    }
  };

  const waitForRunTerminal = ({
    convId,
    runId,
    terminalEvents,
    timeoutMs,
    timeoutMessage,
    closedMessage,
    reconcileImmediately = false,
    startReplay,
  }: {
    convId: string;
    runId: string;
    terminalEvents: readonly string[];
    timeoutMs?: number | null;
    timeoutMessage: string;
    closedMessage: string;
	  reconcileImmediately?: boolean;
	  startReplay?: RunStartReplay;
	  }): Promise<string> => new Promise((resolve, reject) => {
      void timeoutMessage;
      void closedMessage;
      _SSE_WAIT_ABORTS.get(convId)?.abort(new Error("Run replaced by a newer request."));
      _SSE_HANDLES.get(convId)?.close();

      type WaitState = "OPEN" | "RECONCILING" | "TERMINAL" | "ABORTED";
      const terminals = new Set(terminalEvents);
      const eventKey = runOperationKey(convId, runId);
      if (!_RUN_EVENT_IDS.has(eventKey) && _RUN_EVENT_IDS.size >= 256) {
        const oldestKey = _RUN_EVENT_IDS.keys().next().value;
        if (typeof oldestKey === "string") _RUN_EVENT_IDS.delete(oldestKey);
      }
      const seenEventIds = _RUN_EVENT_IDS.get(eventKey) ?? new Set<string>();
      _RUN_EVENT_IDS.set(eventKey, seenEventIds);
      const controller = new AbortController();
      let state: WaitState = "OPEN";
      let settled = false;
      let source: EventSource | undefined;
      let transportTimer: number | undefined;
      let reconcileTimer: number | undefined;
      let statusRequestInFlight = false;
      let statusRetryMs = RUN_STATUS_POLL_INITIAL_MS;
      let streamRetryMs = RUN_STATUS_POLL_INITIAL_MS;
      let permanentStatusFailures = 0;
      let pendingStartReplay = startReplay;
      let owner!: RunWaitOwner;
      let reconcileStatus!: () => void;
      let openStream!: () => void;
      let beginReconciliation!: (expectedSource: EventSource) => void;

      const isActiveOwner = () => (
        !settled
        && !controller.signal.aborted
        && _SSE_WAIT_ABORTS.get(convId) === owner
      );
      const clearTransportTimer = () => {
        if (transportTimer !== undefined) window.clearTimeout(transportTimer);
        transportTimer = undefined;
      };
      const clearReconcileTimer = () => {
        if (reconcileTimer !== undefined) window.clearTimeout(reconcileTimer);
        reconcileTimer = undefined;
      };
      const cleanup = () => {
        clearTransportTimer();
        clearReconcileTimer();
        source?.close();
        if (source && _SSE_HANDLES.get(convId) === source) {
          _SSE_HANDLES.delete(convId);
        }
        if (_SSE_WAIT_ABORTS.get(convId) === owner) {
          _SSE_WAIT_ABORTS.delete(convId);
        }
      };
      const fail = (error: Error) => {
        if (!isActiveOwner()) return;
        settled = true;
        state = "ABORTED";
        controller.abort(error);
        cleanup();
        reject(error);
      };
      const succeed = (event: string) => {
        if (!isActiveOwner()) return;
        settled = true;
        state = "TERMINAL";
        controller.abort(new Error(`Run reached terminal event ${event}.`));
        if (_RESERVED_RUN_UPLOAD_ABORTS.get(convId)?.runId === runId) {
          _RESERVED_RUN_UPLOAD_ABORTS.delete(convId);
        }
        cleanup();
        resolve(event);
      };
      owner = {
        runId,
        controller,
        abort: fail,
        reconcile: (nextStartReplay) => {
          if (!isActiveOwner()) return;
          if (nextStartReplay) pendingStartReplay = nextStartReplay;
          if (source) beginReconciliation(source);
          else reconcileStatus();
        },
      };
      _SSE_WAIT_ABORTS.set(convId, owner);

      const applyRunEvent = (payload: Record<string, unknown>) => {
        if (!isActiveOwner()) return;
        set((current) => {
          const progress = current.runs_progress[convId];
          if (!progress || progress.run_id !== runId) return current;
          return {
            runs_progress: {
              ...current.runs_progress,
              [convId]: applyEvent(progress, payload),
            },
          };
        });
      };
      const markReconnecting = (durableState?: RunLifecycleState) => {
        if (!isActiveOwner()) return;
        set((current) => {
          const progress = current.runs_progress[convId];
          if (!progress || progress.run_id !== runId) return current;
          const cancelling = progress.phase === "cancelling" || durableState === "cancelling";
          return {
            runs_progress: {
              ...current.runs_progress,
              [convId]: {
                ...progress,
                phase: cancelling ? "cancelling" : progress.phase,
                label: cancelling ? "Stopping run…" : "Reconnecting to run…",
              },
            },
          };
        });
      };
      const terminalEventForStatus = (
        durableState: RunLifecycleState,
        terminalEvent: string | null,
      ): string | null => {
        const durableEvent = durableState === "completed"
          ? "run.done"
          : durableState === "cancelled"
            ? "run.cancelled"
            : durableState === "failed"
              ? "run.error"
              : null;
        return terminalEvent === durableEvent ? terminalEvent : durableEvent;
      };

      const scheduleStatusRetry = () => {
        if (!isActiveOwner()) return;
        state = "RECONCILING";
        markReconnecting();
        clearReconcileTimer();
        const delay = statusRetryMs;
        statusRetryMs = Math.min(RUN_STATUS_POLL_MAX_MS, statusRetryMs * 2);
        reconcileTimer = window.setTimeout(() => {
          reconcileTimer = undefined;
          if (isActiveOwner()) reconcileStatus();
        }, delay);
      };
      const scheduleStreamRetry = () => {
        if (!isActiveOwner()) return;
        state = "RECONCILING";
        markReconnecting();
        clearReconcileTimer();
        const delay = streamRetryMs;
        streamRetryMs = Math.min(RUN_STATUS_POLL_MAX_MS, streamRetryMs * 2);
        reconcileTimer = window.setTimeout(() => {
          reconcileTimer = undefined;
          if (isActiveOwner()) openStream();
        }, delay);
      };
      reconcileStatus = () => {
        if (!isActiveOwner() || statusRequestInFlight) return;
        state = "RECONCILING";
        markReconnecting();
        statusRequestInFlight = true;
        const requestController = new AbortController();
        const abortRequest = () => requestController.abort(controller.signal.reason);
        controller.signal.addEventListener("abort", abortRequest, { once: true });
        const requestTimeout = window.setTimeout(
          () => requestController.abort(new Error("run status request timed out")),
          RUN_STATUS_REQUEST_TIMEOUT_MS,
        );
        void fetchRunStatus(runId, requestController.signal).then(async (status) => {
          if (!isActiveOwner() || state !== "RECONCILING") return;
          window.clearTimeout(requestTimeout);
          controller.signal.removeEventListener("abort", abortRequest);
          permanentStatusFailures = 0;
          const terminalEvent = terminalEventForStatus(status.run_state, status.terminal_event);
          if (terminalEvent && terminals.has(terminalEvent)) {
            applyRunEvent({ event: terminalEvent });
            succeed(terminalEvent);
            return;
          }
          if (status.run_state === "queued" && pendingStartReplay) {
            const replay = pendingStartReplay;
            const replayController = new AbortController();
            const abortReplay = () => replayController.abort(controller.signal.reason);
            controller.signal.addEventListener("abort", abortReplay, { once: true });
            let replayTimedOut = false;
            const replayTimeout = window.setTimeout(() => {
              replayTimedOut = true;
              replayController.abort(new Error("run start replay timed out"));
            }, RUN_START_REPLAY_TIMEOUT_MS);
            try {
              await replay(replayController.signal);
            } catch (error) {
              if (!isActiveOwner() || state !== "RECONCILING") return;
              if (replayTimedOut) {
                scheduleStatusRetry();
                return;
              }
              if (error instanceof RunStartAmbiguousError && error.runId === runId) {
                pendingStartReplay = error.retryStart;
                scheduleStatusRetry();
                return;
              }
              fail(error instanceof Error ? error : new Error("Run start replay failed."));
              return;
            } finally {
              window.clearTimeout(replayTimeout);
              controller.signal.removeEventListener("abort", abortReplay);
            }
            if (!isActiveOwner() || state !== "RECONCILING") return;
            pendingStartReplay = undefined;
            markReconnecting(status.run_state);
            statusRetryMs = RUN_STATUS_POLL_INITIAL_MS;
            scheduleStreamRetry();
            return;
          }
          if (status.run_state !== "reserved" && status.run_state !== "uploading") {
            pendingStartReplay = undefined;
          }
          markReconnecting(status.run_state);
          statusRetryMs = RUN_STATUS_POLL_INITIAL_MS;
          scheduleStreamRetry();
        }).catch((error: unknown) => {
          if (!isActiveOwner() || state !== "RECONCILING") return;
          if (error instanceof RunStatusError && !error.retryable) {
            permanentStatusFailures += 1;
            if (permanentStatusFailures >= RUN_STATUS_PERMANENT_CONFIRMATIONS) {
              fail(error);
            }
            return;
          }
          permanentStatusFailures = 0;
        }).finally(() => {
          window.clearTimeout(requestTimeout);
          controller.signal.removeEventListener("abort", abortRequest);
          statusRequestInFlight = false;
          if (
            isActiveOwner()
            && state === "RECONCILING"
            && reconcileTimer === undefined
          ) {
            scheduleStatusRetry();
          }
        });
      };
      beginReconciliation = (expectedSource: EventSource) => {
        if (!isActiveOwner() || source !== expectedSource) return;
        clearTransportTimer();
        expectedSource.close();
        state = "RECONCILING";
        markReconnecting();
        reconcileStatus();
      };
      openStream = () => {
        if (!isActiveOwner()) return;
        clearTransportTimer();
        clearReconcileTimer();
        source?.close();
        const nextSource = new EventSource(`/api/runs/${runId}/events`);
        source = nextSource;
        state = "OPEN";
        _SSE_HANDLES.set(convId, nextSource);
        if (typeof timeoutMs === "number" && timeoutMs > 0) {
          transportTimer = window.setTimeout(
            () => beginReconciliation(nextSource),
            timeoutMs,
          );
        }
        nextSource.onmessage = (msg: MessageEvent<string>) => {
          if (!isActiveOwner() || source !== nextSource) return;
          try {
            const payload = JSON.parse(msg.data) as Record<string, unknown>;
            const eventId = typeof payload.event_id === "string"
              ? payload.event_id
              : msg.lastEventId;
            if (eventId) {
              if (seenEventIds.has(eventId)) return;
              seenEventIds.add(eventId);
            }
            const event = String(payload.event ?? "");
            streamRetryMs = RUN_STATUS_POLL_INITIAL_MS;
            applyRunEvent(payload);
            if (terminals.has(event)) succeed(event);
          } catch {
            /* ignore malformed keepalive frames */
          }
        };
        nextSource.onerror = () => {
          if (!isActiveOwner() || source !== nextSource) return;
          if (nextSource.readyState === EventSource.CLOSED) {
            beginReconciliation(nextSource);
          }
        };
      };

	      openStream();
      if (reconcileImmediately) owner.reconcile();
	  });

	  const fetchPaperBundleRunArtifact = async (
	    convId: string,
	    runId: string,
	    retryState: PaperBundleArtifactRetryState,
	    respectCancellation = true,
	  ): Promise<GenerateResponse> => {
	    const retryableStatuses = new Set([404, 409, 425, 429, 500, 502, 503, 504]);
	    const controller = new AbortController();
	    const owner: RunArtifactFetchOwner = {
        runId,
        controller,
      };
	    _RUN_ARTIFACT_FETCH_OWNERS.get(convId)?.controller.abort(
	      new Error("Artifact fetch replaced by a newer request."),
	    );
	    _RUN_ARTIFACT_FETCH_OWNERS.set(convId, owner);
	    let retryDelayMs = 250;
	    let lastError: unknown = new Error("Run artifact is unavailable.");
	    const remainingPostTerminalGraceMs = () => {
	      if (retryState.terminalSettledAt === undefined) return undefined;
	      return Math.max(
	        0,
	        retryState.terminalSettledAt
	          + PAPER_BUNDLE_POST_TERMINAL_ARTIFACT_GRACE_MS
	          - Date.now(),
	      );
	    };
	    const abortReason = () =>
	      controller.signal.reason instanceof Error
	        ? controller.signal.reason
	        : new RunWaitCancelledError();
	    const ensureTerminalDeadline = () => {
	      if (
	        retryState.terminalSettledAt === undefined
	        || retryState.terminalDeadlineReached
	        || retryState.terminalDeadlineTimer !== undefined
	      ) {
	        return;
	      }
	      const remainingGraceMs = remainingPostTerminalGraceMs() ?? 0;
	      if (remainingGraceMs === 0) {
	        retryState.terminalDeadlineReached = true;
	        retryState.onTerminalDeadline?.();
	        return;
	      }
	      retryState.terminalDeadlineTimer = window.setTimeout(() => {
	        retryState.terminalDeadlineTimer = undefined;
	        retryState.terminalDeadlineReached = true;
	        retryState.onTerminalDeadline?.();
	      }, remainingGraceMs);
	    };
	    const waitForRetry = (delayMs: number) =>
	      new Promise<void>((resolve, reject) => {
	        if (controller.signal.aborted) {
	          reject(abortReason());
	          return;
	        }
	        const finishWait = () => {
	          controller.signal.removeEventListener("abort", onAbort);
	          if (retryState.onTerminal === onTerminal) retryState.onTerminal = undefined;
	        };
	        const timeout = window.setTimeout(() => {
	          finishWait();
	          resolve();
	        }, delayMs);
	        const onAbort = () => {
	          window.clearTimeout(timeout);
	          finishWait();
	          reject(abortReason());
	        };
	        const onTerminal = () => {
	          window.clearTimeout(timeout);
	          finishWait();
	          resolve();
	        };
	        controller.signal.addEventListener("abort", onAbort, { once: true });
	        if (retryState.terminalSettledAt === undefined) {
	          retryState.onTerminal = onTerminal;
	        }
	      });
	    try {
	      for (;;) {
	        const child = get().conversations[convId];
	        const parentId = child?.paper_bundle?.kind === "child"
	          ? child.paper_bundle.parent_conversation_id
	          : undefined;
		        const artifactType = child?.paper_bundle?.kind === "child"
		          ? child.paper_bundle.artifact_type
		          : undefined;
		        const parentBundle = parentId
		          ? get().conversations[parentId]?.paper_bundle
		          : undefined;
		        if (
		          controller.signal.aborted
		          || (
		            respectCancellation
		            && parentId
		            && artifactType
		            && parentBundle?.kind === "parent"
		            && paperBundleTaskWasCancelled(
		              currentDemoUserScope(),
		              parentId,
		              paperBundleCancellationGeneration(parentBundle),
		              artifactType,
		            )
		          )
	        ) {
	          throw new RunWaitCancelledError();
	        }
	        ensureTerminalDeadline();
	        const remainingGraceMs = remainingPostTerminalGraceMs();
	        if (
	          retryState.terminalDeadlineReached
	          || remainingGraceMs === 0
	        ) {
	          throw new RunArtifactDeliveryError(runId, lastError);
	        }

	        const requestController = new AbortController();
	        const abortRequest = () => requestController.abort(abortReason());
	        controller.signal.addEventListener("abort", abortRequest, { once: true });
	        const restartAfterTerminal = () => requestController.abort(
	          new Error("Run reached terminal state; restarting bounded artifact read."),
	        );
	        if (retryState.terminalSettledAt === undefined) {
	          retryState.onTerminal = restartAfterTerminal;
	        }
	        const abortAfterTerminalDeadline = () => requestController.abort(
	          new Error("Post-terminal artifact recovery grace expired."),
	        );
	        if (retryState.terminalSettledAt !== undefined) {
	          retryState.onTerminalDeadline = abortAfterTerminalDeadline;
	        }
	        const requestTimeout = window.setTimeout(
	          () => requestController.abort(new Error("run artifact request timed out")),
	          Math.min(130 * 1000, remainingGraceMs ?? 130 * 1000),
	        );
	        try {
	          const response = await fetchRunArtifact(runId, requestController.signal);
            if (_RUN_ARTIFACT_FETCH_OWNERS.get(convId) !== owner) {
              throw new Error("Artifact fetch replaced by a newer request.");
            }
            return response;
	        } catch (error) {
	          if (controller.signal.aborted) throw abortReason();
	          lastError = error;
	          if (error instanceof ApiError && !retryableStatuses.has(error.status)) {
	            throw retryState.terminalSettledAt === undefined
	              ? error
	              : new RunArtifactDeliveryError(runId, error);
	          }
	        } finally {
	          window.clearTimeout(requestTimeout);
	          controller.signal.removeEventListener("abort", abortRequest);
	          if (retryState.onTerminal === restartAfterTerminal) {
	            retryState.onTerminal = undefined;
	          }
	          if (retryState.onTerminalDeadline === abortAfterTerminalDeadline) {
	            retryState.onTerminalDeadline = undefined;
	          }
	        }

	        const remainingGraceAfterRequestMs = remainingPostTerminalGraceMs();
	        if (
	          retryState.terminalDeadlineReached
	          || remainingGraceAfterRequestMs === 0
	        ) {
	          throw new RunArtifactDeliveryError(runId, lastError);
	        }
	        await waitForRetry(
	          Math.min(retryDelayMs, remainingGraceAfterRequestMs ?? retryDelayMs),
	        );
	        retryDelayMs = Math.min(5000, retryDelayMs * 2);
	      }
	    } finally {
	      if (_RUN_ARTIFACT_FETCH_OWNERS.get(convId) === owner) {
	        _RUN_ARTIFACT_FETCH_OWNERS.delete(convId);
	      }
	      if (retryState.terminalDeadlineTimer !== undefined) {
	        window.clearTimeout(retryState.terminalDeadlineTimer);
	        retryState.terminalDeadlineTimer = undefined;
	      }
	      retryState.onTerminal = undefined;
	      retryState.onTerminalDeadline = undefined;
	    }
	  };

    const fetchRunArtifactOnce = async (
      convId: string,
      runId: string,
    ): Promise<GenerateResponse> => {
      const shared = _RUN_ARTIFACT_FETCH_PROMISES.get(convId);
      if (shared?.owner.runId === runId) return shared.promise;
      const controller = new AbortController();
      const owner: RunArtifactFetchOwner = {
        runId,
        controller,
      };
      _RUN_ARTIFACT_FETCH_OWNERS.get(convId)?.controller.abort(
        new Error("Artifact fetch replaced by a newer request."),
      );
      _RUN_ARTIFACT_FETCH_OWNERS.set(convId, owner);
      const operation = (async () => {
        try {
          const response = await fetchRunArtifact(runId, controller.signal);
          if (_RUN_ARTIFACT_FETCH_OWNERS.get(convId) !== owner) {
            throw new Error("Artifact fetch replaced by a newer request.");
          }
          return response;
        } finally {
          if (_RUN_ARTIFACT_FETCH_OWNERS.get(convId) === owner) {
            _RUN_ARTIFACT_FETCH_OWNERS.delete(convId);
          }
          if (_RUN_ARTIFACT_FETCH_PROMISES.get(convId)?.owner === owner) {
            _RUN_ARTIFACT_FETCH_PROMISES.delete(convId);
          }
        }
      })();
      _RUN_ARTIFACT_FETCH_PROMISES.set(convId, {
        owner,
        mode: "once",
        promise: operation,
      });
      return operation;
    };

    const fetchRunArtifactAfterTerminal = async (
      convId: string,
      runId: string,
    ): Promise<GenerateResponse> => {
      const shared = _RUN_ARTIFACT_FETCH_PROMISES.get(convId);
      if (shared?.owner.runId === runId) {
        if (shared.mode === "retry") return shared.promise;
        try {
          return await shared.promise;
        } catch {
          /* The terminal path below owns the bounded retry. */
        }
        const replacement = _RUN_ARTIFACT_FETCH_PROMISES.get(convId);
        if (replacement) {
          if (replacement.owner.runId !== runId) {
            throw new Error("Artifact fetch replaced by a newer request.");
          }
          if (replacement !== shared) return replacement.promise;
        }
      }
      const retryableStatuses = new Set([404, 409, 425, 429, 500, 502, 503, 504]);
      const controller = new AbortController();
      const owner: RunArtifactFetchOwner = {
        runId,
        controller,
      };
      _RUN_ARTIFACT_FETCH_OWNERS.get(convId)?.controller.abort(
        new Error("Artifact fetch replaced by a newer request."),
      );
      _RUN_ARTIFACT_FETCH_OWNERS.set(convId, owner);
      const operation = (async () => {
        let delayMs = 250;
        let lastError: unknown = new Error("Run artifact is unavailable.");
        const deadline = window.setTimeout(
          () => controller.abort(new RunArtifactDeliveryError(
            runId,
            new Error("Post-terminal artifact recovery grace expired."),
          )),
          PAPER_BUNDLE_POST_TERMINAL_ARTIFACT_GRACE_MS,
        );
        const abortReason = () => controller.signal.reason instanceof Error
          ? controller.signal.reason
          : new Error("Artifact fetch aborted.");
        const waitForRetry = () => new Promise<void>((resolve, reject) => {
          if (controller.signal.aborted) {
            reject(abortReason());
            return;
          }
          const timeout = window.setTimeout(() => {
            controller.signal.removeEventListener("abort", onAbort);
            resolve();
          }, delayMs);
          const onAbort = () => {
            window.clearTimeout(timeout);
            reject(abortReason());
          };
          controller.signal.addEventListener("abort", onAbort, { once: true });
        });
        try {
          for (let attempt = 0; attempt < RUN_ARTIFACT_RETRY_MAX_ATTEMPTS; attempt += 1) {
            if (
              controller.signal.aborted
              || _RUN_ARTIFACT_FETCH_OWNERS.get(convId) !== owner
            ) {
              throw abortReason();
            }
            try {
              const response = await fetchRunArtifact(runId, controller.signal);
              if (_RUN_ARTIFACT_FETCH_OWNERS.get(convId) !== owner) {
                throw new Error("Artifact fetch replaced by a newer request.");
              }
              return response;
            } catch (error) {
              if (controller.signal.aborted) throw abortReason();
              lastError = error;
              if (
                error instanceof ApiError
                && !retryableStatuses.has(error.status)
              ) {
                throw new RunArtifactDeliveryError(runId, error);
              }
            }
            if (attempt + 1 >= RUN_ARTIFACT_RETRY_MAX_ATTEMPTS) break;
            await waitForRetry();
            delayMs = Math.min(RUN_STATUS_POLL_MAX_MS, delayMs * 2);
          }
          throw new RunArtifactDeliveryError(runId, lastError);
        } finally {
          window.clearTimeout(deadline);
          if (_RUN_ARTIFACT_FETCH_OWNERS.get(convId) === owner) {
            _RUN_ARTIFACT_FETCH_OWNERS.delete(convId);
          }
          if (_RUN_ARTIFACT_FETCH_PROMISES.get(convId)?.owner === owner) {
            _RUN_ARTIFACT_FETCH_PROMISES.delete(convId);
          }
        }
      })();
      _RUN_ARTIFACT_FETCH_PROMISES.set(convId, {
        owner,
        mode: "retry",
        promise: operation,
      });
      return operation;
    };

	  const runGenerateAckFlow = async ({
	    convId,
	    placeholderId,
	    ack,
	    timeoutMs,
	    timeoutMessage,
	    closedMessage,
	    task_type,
	    task_payload,
	    source_artifact_id,
	    activateArtifact = true,
	    recoverExisting = false,
	    retryPaperBundleArtifact = false,
	    artifactOnlyRecovery = false,
	    reconcileImmediately = false,
	    startReplay,
	    onBeforeProgressClear,
	    shouldApplyResult,
	    validateResult,
	  }: {
	    convId: string;
	    placeholderId: string;
	    ack: GenerateAck;
	    timeoutMs?: number | null;
	    timeoutMessage: string;
	    closedMessage: string;
	    task_type?: RecoverableTaskType;
	    task_payload?: MessageTaskPayload;
	    source_artifact_id?: string;
	    activateArtifact?: boolean;
	    recoverExisting?: boolean;
	    retryPaperBundleArtifact?: boolean;
	    artifactOnlyRecovery?: boolean;
	    reconcileImmediately?: boolean;
	    startReplay?: RunStartReplay;
	    onBeforeProgressClear?: (progress: RunProgress | undefined) => void;
	    shouldApplyResult?: () => boolean;
	    validateResult?: (result: GenerateResponse) => boolean;
	  }): Promise<GenerateResponse> => {
	    patchConversation(convId, (c) => ({ ...c, run_id: ack.run_id }));
	    set((s) => ({
	      runs_progress: {
	        ...s.runs_progress,
	        [convId]: initialProgress(ack.run_id, ack.progress_mode),
	      },
	    }));
	    let res: GenerateResponse;
	    if (artifactOnlyRecovery) {
	      res = await fetchRunArtifactAfterTerminal(convId, ack.run_id);
	    } else {
	      const terminal = waitForRunTerminal({
	        convId,
	        runId: ack.run_id,
	        terminalEvents: ["run.done", "run.error", "run.cancelled"],
	        timeoutMs,
	        timeoutMessage,
	        closedMessage,
	        reconcileImmediately,
	        startReplay,
	      });
	      const artifactRetryState: PaperBundleArtifactRetryState = {};
	      void terminal.then(
	        () => {
	          artifactRetryState.terminalSettledAt ??= Date.now();
	          artifactRetryState.onTerminal?.();
	        },
	        () => undefined,
	      );
	      const fetchArtifact = () => retryPaperBundleArtifact
	        ? fetchPaperBundleRunArtifact(convId, ack.run_id, artifactRetryState)
	        : fetchRunArtifactAfterTerminal(convId, ack.run_id);
	      const terminalFailure = () => terminal.then(
	        () => new Promise<never>(() => undefined),
	        (error: unknown) => {
	          const artifactOwner = _RUN_ARTIFACT_FETCH_OWNERS.get(convId);
	          if (artifactOwner?.runId === ack.run_id) {
	            artifactOwner.controller.abort(
	              error instanceof Error ? error : new Error("Run recovery failed."),
	            );
	          }
	          throw error;
	        },
	      );
	      res = recoverExisting && retryPaperBundleArtifact
	        ? await Promise.race([fetchArtifact(), terminalFailure()])
	        : recoverExisting
	          ? await Promise.any([
	              fetchRunArtifactOnce(convId, ack.run_id),
	              terminal.then(fetchArtifact),
	            ])
	          : await terminal.then(fetchArtifact);
	    }
	    if (validateResult && !validateResult(res)) {
	      throw new Error("Run artifact did not match the owning request.");
	    }
	    if (shouldApplyResult && !shouldApplyResult()) return res;
	    if (recoverExisting) {
	      const owner = _SSE_WAIT_ABORTS.get(convId);
	      if (owner?.runId === ack.run_id) {
	        owner.abort(new Error("Recovered run artifact."));
	      }
	    }
	    patchConversation(convId, (c) => {
	      if (shouldApplyResult && !shouldApplyResult()) return c;
	      if (c.run_id !== ack.run_id) return c;
	      const messages = c.messages.map((m) =>
	        m.id === placeholderId
	          ? {
	              ...res.message,
	              id: m.id,
	              run_id: ack.run_id,
	              task_type,
	              task_payload,
	              source_artifact_id,
	            }
	          : m
	      );
	      const next: Conversation = {
	        ...c,
	        messages,
	        pending: false,
	        run_id: undefined,
	      };
	      if (!res.artifact) return next;
	      const withArtifact: Conversation = {
	        ...next,
	        artifacts: { ...c.artifacts, [res.artifact.artifact_id]: res.artifact },
	      };
	      const active = c.active_artifact_id
	        ? c.artifacts[c.active_artifact_id]
	        : undefined;
	      if (
	        !activateArtifact
	        || active?.attempt_lineage?.source_run_id === ack.run_id
	      ) return withArtifact;
	      return {
	        ...withArtifact,
	        active_artifact_id: res.artifact.artifact_id,
	      };
	    });
	    if (shouldApplyResult && !shouldApplyResult()) return res;
	    const terminalProgress = get().runs_progress[convId];
	    onBeforeProgressClear?.(
	      terminalProgress?.run_id === ack.run_id ? terminalProgress : undefined,
	    );
	    set((s) => {
	      if (shouldApplyResult && !shouldApplyResult()) return s;
	      if (s.runs_progress[convId]?.run_id !== ack.run_id) return s;
	      const next = { ...s.runs_progress };
	      delete next[convId];
	      return { runs_progress: next };
	    });
	    return res;
	  };

  const patchPaperBundleTask = (
    parentConversationId: string,
    artifactType: ArtifactType,
    mutator: (task: PaperBundleTask) => PaperBundleTask,
    guard?: { ownerScope: string; jobId?: string },
  ) => {
    patchConversation(parentConversationId, (conversation) => {
      const bundle = conversation.paper_bundle;
      if (bundle?.kind !== "parent") return conversation;
      if (
        guard
        && (
          currentDemoUserScope() !== guard.ownerScope
          || bundle.job_id !== guard.jobId
        )
      ) {
        return conversation;
      }
      const tasks = {
        ...bundle.tasks,
        [artifactType]: mutator(bundle.tasks[artifactType]),
      };
      return {
        ...conversation,
        paper_bundle: {
          ...bundle,
          tasks,
        },
        pending: paperBundleHasActiveTasks({ ...bundle, tasks }),
      };
    });
  };

  const clearRunProgress = (conversationId: string, runId?: string) => {
    set((state) => {
      const progress = state.runs_progress[conversationId];
      if (!progress || (runId && progress.run_id !== runId)) return state;
      const runsProgress = { ...state.runs_progress };
      delete runsProgress[conversationId];
      return { runs_progress: runsProgress };
    });
  };

  const markRunProgressCancelling = (
    conversationId: string,
    runId: string,
    label = "Stopping run…",
  ) => {
    set((state) => {
      const progress = state.runs_progress[conversationId];
      if (!progress || progress.run_id !== runId) return state;
      return {
        runs_progress: {
          ...state.runs_progress,
          [conversationId]: {
            ...progress,
            phase: "cancelling",
            label,
          },
        },
      };
    });
  };

  const runCancellationDisposition = (
    conversationId: string,
    runId: string | undefined,
    error: unknown,
  ): "confirmed" | "pending" | null => {
    if (!runId) return null;
    if (
      error instanceof RunWaitCancelledError
      || _AUTHORITATIVE_RUN_CANCELLATIONS.has(runOperationKey(conversationId, runId))
    ) {
      return "confirmed";
    }
    const conversation = get().conversations[conversationId];
    const progress = get().runs_progress[conversationId];
    return conversation?.run_id === runId
      && progress?.run_id === runId
      && progress.phase === "cancelling"
      ? "pending"
      : null;
  };

  const cleanupReservedRunOwner = (
    conversationId: string,
    runId: string | undefined,
  ) => {
    if (!runId) return;
    const owner = _RESERVED_RUN_UPLOAD_ABORTS.get(conversationId);
    if (owner?.runId === runId) {
      _RESERVED_RUN_UPLOAD_ABORTS.delete(conversationId);
    }
    _AUTHORITATIVE_RUN_CANCELLATIONS.delete(
      runOperationKey(conversationId, runId),
    );
  };

  const conversationOwnsRun = (
    conversation: Conversation | undefined,
    runId: string,
  ): boolean => conversation?.pending === true && conversation.run_id === runId;

  const sourceRunOwnsOrdinarySlot = (
    conversation: Conversation | undefined,
    runId: string,
  ): boolean => Boolean(
    conversation
    && sourceRunIsActiveForConversation(get().conversations, conversation.id, runId)
  );

  const candidatePublishOperationFor = (
    conversationId: string,
    message?: Message,
  ): string => {
    const conversation = get().conversations[conversationId];
    return message?.run_id && conversationOwnsRun(conversation, message.run_id)
      ? conversationId
      : candidatePublishOperationId(conversationId);
  };

  const hasActiveCandidatePublication = (conversationId: string): boolean => {
    return candidatePublicationIsActive(get(), conversationId);
  };

  const attemptRunHasActiveCandidatePublication = (
    runId: string,
    targetConversationId?: string,
  ): boolean => Object.values(get().conversations).some((conversation) => {
    if (targetConversationId && conversation.id !== targetConversationId) return false;
    if (!hasActiveCandidatePublication(conversation.id)) return false;
    return conversation.messages.some((message) => (
      message.task_type === CANDIDATE_PUBLISH_TASK
      && message.task_payload?.source_run_id === runId
    )) || Object.values(conversation.artifacts).some((artifact) => (
      artifact.candidate_draft
      && artifact.attempt_lineage?.source_run_id === runId
    ));
  });

  const requestExactRunCancellation = async (
    ownerScope: string,
    operationConversationId: string,
    runId: string,
    timeoutMessage: string,
  ): Promise<RunCancelResponse> => {
    const cancelKey = runCancellationRequestKey(
      ownerScope,
      operationConversationId,
      runId,
    );
    let request = _RUN_CANCEL_REQUESTS.get(cancelKey);
    if (!request) {
      request = (async () => {
        const controller = new AbortController();
        const timeout = window.setTimeout(
          () => controller.abort(new Error(timeoutMessage)),
          CANCELLATION_REQUEST_TIMEOUT_MS,
        );
        try {
          return await cancelRunRequest(runId, controller.signal);
        } finally {
          window.clearTimeout(timeout);
        }
      })();
      _RUN_CANCEL_REQUESTS.set(cancelKey, request);
    }
    try {
      return await request;
    } finally {
      if (_RUN_CANCEL_REQUESTS.get(cancelKey) === request) {
        _RUN_CANCEL_REQUESTS.delete(cancelKey);
      }
    }
  };

  const activeCandidatePublicationOwner = (
    conversationId: string,
  ): CandidatePublicationOwner | undefined => {
    const owner = _ACTIVE_DERIVED_RUN_CONVERSATIONS.get(conversationId);
    return owner?.kind === "candidate_publish" ? owner : undefined;
  };

  const activeAttemptForkOwner = (
    conversationId: string,
  ): AttemptForkOwner | undefined => {
    const owner = _ACTIVE_DERIVED_RUN_CONVERSATIONS.get(conversationId);
    return owner?.kind === "attempt_fork" ? owner : undefined;
  };

  const settleCandidatePublicationOwner = (
    owner: CandidatePublicationOwner,
    result: CandidatePublicationCancellation,
  ) => {
    owner.settlementResult = result;
    owner.resolveSettlement(result);
  };

  const releaseCandidatePublicationOwner = (owner: CandidatePublicationOwner) => {
    owner.release?.();
  };

  const settleAttemptForkOwner = (
    owner: AttemptForkOwner,
    result: CandidatePublicationCancellation,
  ) => {
    owner.settlementResult = result;
    owner.resolveSettlement(result);
  };

  const releaseAttemptForkOwner = (owner: AttemptForkOwner) => {
    owner.release?.();
  };

  const cancelDerivedRun = async (
    kind: "candidate_publish" | "attempt_fork",
    conversationId: string,
    ownerScope: string,
    operationConversationId: string,
    runId: string,
    message?: Message,
  ): Promise<CandidatePublicationCancellation> => {
    markRunProgressCancelling(
      operationConversationId,
      runId,
      kind === "candidate_publish" ? "Stopping publication…" : "Stopping attempt draft…",
    );
    set((state) => {
      const progress = state.runs_progress[operationConversationId];
      if (progress?.run_id !== runId) return state;
      return {
        runs_progress: {
          ...state.runs_progress,
          [operationConversationId]: {
            ...progress,
            cancel_request_in_flight: true,
          },
        },
      };
    });

    let result: RunCancelResponse;
    try {
      result = await requestExactRunCancellation(
        ownerScope,
        operationConversationId,
        runId,
        kind === "candidate_publish"
          ? "candidate publication cancellation timed out"
          : "attempt fork cancellation timed out",
      );
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      if (currentDemoUserScope() !== ownerScope) {
        return { attempted: true, confirmed: false, error: detail };
      }
      set((state) => {
        const progress = state.runs_progress[operationConversationId];
        if (progress?.run_id !== runId) return state;
        return {
          runs_progress: {
            ...state.runs_progress,
            [operationConversationId]: {
              ...progress,
              phase: "cancelling",
              label: "Cancellation not confirmed; backend may still be stopping",
              cancel_request_in_flight: false,
            },
          },
        };
      });
      return { attempted: true, confirmed: false, error: detail };
    }

    if (currentDemoUserScope() !== ownerScope) {
      return {
        attempted: true,
        confirmed: false,
        error: "Derived run cancellation owner changed.",
      };
    }

    const confirmed = result.http_status === 200
      && result.confirmed
      && (result.status === "cancelled" || result.status === "already_cancelled");
    if (!confirmed) {
      const detail = result.status === "already_terminal"
        ? `Run already ${result.run_state}; cancellation was not applied.`
        : "Cancellation not confirmed; backend may still be stopping.";
      set((state) => {
        const progress = state.runs_progress[operationConversationId];
        if (progress?.run_id !== runId) return state;
        return {
          runs_progress: {
            ...state.runs_progress,
            [operationConversationId]: {
              ...progress,
              phase: "cancelling",
              label: detail,
              cancel_request_in_flight: false,
            },
          },
        };
      });
      return { attempted: true, confirmed: false, error: detail };
    }

    const uploadOwner = _RESERVED_RUN_UPLOAD_ABORTS.get(operationConversationId);
    if (uploadOwner?.runId === runId && !uploadOwner.controller.signal.aborted) {
      uploadOwner.controller.abort(new RunWaitCancelledError());
    }
    const waitOwner = _SSE_WAIT_ABORTS.get(operationConversationId);
    if (waitOwner?.runId === runId) {
      waitOwner.abort(new RunWaitCancelledError());
    }
    const artifactFetch = _RUN_ARTIFACT_FETCH_OWNERS.get(operationConversationId);
    if (artifactFetch?.runId === runId) {
      artifactFetch.controller.abort(new RunWaitCancelledError());
    }
    if (kind === "candidate_publish") patchConversation(conversationId, (current) => {
      if (!message || !ownsCandidatePublishMessage(current, message.id, runId)) {
        return current;
      }
      return {
        ...current,
        ...(operationConversationId === conversationId && current.run_id === runId
          ? { pending: false, run_id: undefined }
          : {}),
        messages: current.messages.map((currentMessage) => (
          currentMessage.id === message.id
          && currentMessage.run_id === runId
          && currentMessage.task_type === CANDIDATE_PUBLISH_TASK
          && currentMessage.status === "streaming"
            ? {
                ...currentMessage,
                text: "Run cancelled.",
                status: "error",
                failure: {
                  status: "cancelled",
                  produced_files: [],
                  artifact_type: currentMessage.task_payload?.artifact_type,
                },
              }
            : currentMessage
        )),
      };
    });
    if (kind === "attempt_fork" && operationConversationId === conversationId) {
      patchConversation(conversationId, (current) => (
        conversationOwnsRun(current, runId)
          ? { ...current, pending: false, run_id: undefined }
          : current
      ));
    }
    clearRunProgress(operationConversationId, runId);
    cleanupReservedRunOwner(operationConversationId, runId);
    return { attempted: true, confirmed: true };
  };

  const cancelCandidatePublicationRun = (
    conversationId: string,
    ownerScope: string,
    operationConversationId: string,
    runId: string,
    message?: Message,
  ) => cancelDerivedRun(
    "candidate_publish",
    conversationId,
    ownerScope,
    operationConversationId,
    runId,
    message,
  );

  const cancelAttemptForkRun = (
    conversationId: string,
    ownerScope: string,
    operationConversationId: string,
    runId: string,
  ) => cancelDerivedRun(
    "attempt_fork",
    conversationId,
    ownerScope,
    operationConversationId,
    runId,
  );

  const requestOwnedCandidatePublicationCancellation = (
    owner: CandidatePublicationOwner,
    conversationId: string,
    ownerScope: string,
    message?: Message,
  ): Promise<CandidatePublicationCancellation> => {
    if (!owner.runId) {
      return Promise.resolve({ attempted: true, confirmed: true });
    }
    if (!owner.cancellationRequest) {
      owner.cancellationRequest = cancelCandidatePublicationRun(
        conversationId,
        ownerScope,
        owner.operationConversationId,
        owner.runId,
        message,
      );
    }
    return owner.cancellationRequest;
  };

  const cancelCandidatePublication = async (
    conversationId: string,
    ownerScope: string,
  ): Promise<CandidatePublicationCancellation> => {
    const conversation = get().conversations[conversationId];
    const message = conversation ? activeCandidatePublishMessage(conversation) : undefined;
    const owner = activeCandidatePublicationOwner(conversationId);
    if (owner) {
      owner.cancelRequested = true;
      if (!owner.controller.signal.aborted) {
        owner.controller.abort(new RunWaitCancelledError());
      }
      const runId = owner.runId ?? message?.run_id;
      if (owner.settlementResult?.confirmed === false && runId) {
        owner.runId = runId;
        owner.settlementResult = undefined;
        owner.cancellationRequest = undefined;
      }
      if (runId) {
        owner.runId = runId;
        const cancellation = await requestOwnedCandidatePublicationCancellation(
          owner,
          conversationId,
          owner.ownerScope,
          message,
        );
        settleCandidatePublicationOwner(owner, cancellation);
        if (owner.flowComplete && cancellation.confirmed) {
          releaseCandidatePublicationOwner(owner);
        }
        return cancellation;
      }
      return owner.settlement;
    }
    const runId = message?.run_id;
    if (!message || !runId) return { attempted: false, confirmed: true };
    return cancelCandidatePublicationRun(
      conversationId,
      ownerScope,
      candidatePublishOperationFor(conversationId, message),
      runId,
      message,
    );
  };

  const requestOwnedAttemptForkCancellation = (
    owner: AttemptForkOwner,
    conversationId: string,
  ): Promise<CandidatePublicationCancellation> => {
    if (!owner.runId) {
      return Promise.resolve({ attempted: true, confirmed: true });
    }
    if (!owner.cancellationRequest) {
      owner.cancellationRequest = cancelAttemptForkRun(
        conversationId,
        owner.ownerScope,
        owner.operationConversationId,
        owner.runId,
      );
    }
    return owner.cancellationRequest;
  };

  const cancelAttemptFork = async (
    conversationId: string,
  ): Promise<CandidatePublicationCancellation> => {
    const owner = activeAttemptForkOwner(conversationId);
    if (!owner) return { attempted: false, confirmed: true };
    owner.cancelRequested = true;
    if (!owner.controller.signal.aborted) {
      owner.controller.abort(new RunWaitCancelledError());
    }
    if (owner.settlementResult?.confirmed === false && owner.runId) {
      owner.settlementResult = undefined;
      owner.cancellationRequest = undefined;
    }
    if (owner.runId) {
      const cancellation = await requestOwnedAttemptForkCancellation(owner, conversationId);
      settleAttemptForkOwner(owner, cancellation);
      if (owner.flowComplete && cancellation.confirmed) {
        releaseAttemptForkOwner(owner);
      }
      return cancellation;
    }
    return owner.settlement;
  };

  const acquireDerivedRunOperation = (
    conversationId: string,
    kind: "candidate_publish" | "attempt_fork" | "video_render",
    requestedOwner?: ActiveDerivedRunOwner,
  ): (() => void) => {
    const progress = kind === "candidate_publish"
      ? get().runs_progress[candidatePublishOperationId(conversationId)]
        ?? get().runs_progress[conversationId]
      : kind === "attempt_fork"
        ? get().runs_progress[attemptForkOperationId(conversationId)]
          ?? get().runs_progress[conversationId]
        : get().runs_progress[conversationId];
    const expectedMode = kind === "candidate_publish"
      ? "attempt_publish"
      : kind === "attempt_fork"
        ? "attempt_fork"
        : "video_render";
    const progressBlocksStart = progress
      && (
        progress.phase === "cancelling"
        || (
          progress.mode === expectedMode
          && (progress.phase === "queued" || progress.phase === "running")
        )
      );
    if (
      _ACTIVE_DERIVED_RUN_CONVERSATIONS.has(conversationId)
      || progressBlocksStart
      || (kind === "candidate_publish" && candidatePublicationIsActive(get(), conversationId))
    ) {
      throw new Error("The current run is still active for this conversation.");
    }
    if ((kind === "candidate_publish" || kind === "attempt_fork") && !requestedOwner) {
      throw new Error("Derived run owner is required.");
    }
    const owner: ActiveDerivedRunOwner = requestedOwner ?? {
      kind: "video_render",
      token: Symbol(kind),
    };
    if (owner.kind === "candidate_publish") {
      const release = installTokenizedPublicationOwner(
        _ACTIVE_DERIVED_RUN_CONVERSATIONS,
        conversationId,
        owner,
        {
          token: owner.token,
          operationConversationId: owner.operationConversationId,
        },
        () => get().candidate_publication_owners[conversationId],
        (reactiveOwner) => {
          set((state) => {
            const owners = { ...state.candidate_publication_owners };
            if (reactiveOwner) owners[conversationId] = reactiveOwner;
            else delete owners[conversationId];
            return { candidate_publication_owners: owners };
          });
        },
      );
      owner.release = release;
      return release;
    }
    if (owner.kind === "attempt_fork") {
      _ACTIVE_DERIVED_RUN_CONVERSATIONS.set(conversationId, owner);
      const release = () => {
        if (_ACTIVE_DERIVED_RUN_CONVERSATIONS.get(conversationId) === owner) {
          _ACTIVE_DERIVED_RUN_CONVERSATIONS.delete(conversationId);
        }
      };
      owner.release = release;
      return release;
    }
    _ACTIVE_DERIVED_RUN_CONVERSATIONS.set(conversationId, owner);
    return () => {
      if (_ACTIVE_DERIVED_RUN_CONVERSATIONS.get(conversationId) === owner) {
        _ACTIVE_DERIVED_RUN_CONVERSATIONS.delete(conversationId);
      }
    };
  };

  const applyPublishedCandidate = (
    conversationId: string,
    sourceArtifactId: string,
    published: Artifact,
    publicationMessageId?: string,
    sourceRunStaysLive = false,
  ) => {
    set((current) => {
      const latest = current.conversations[conversationId];
      if (!latest) return current;
      const source = latest.artifacts[sourceArtifactId];
      const messages = [...latest.messages];
      const messageIndex = publicationMessageId
        ? messages.findIndex((message) => message.id === publicationMessageId)
        : (() => {
          const reverseIndex = [...messages].reverse().findIndex(
            (message) => (
              message.role === "assistant"
              && (
                message.artifact_id === sourceArtifactId
                || (
                  message.status === "streaming"
                  && (
                    !message.run_id
                    || message.run_id === source?.attempt_lineage?.source_run_id
                  )
                )
              )
            ),
          );
          return reverseIndex < 0 ? -1 : messages.length - 1 - reverseIndex;
        })();
      if (messageIndex >= 0) {
        messages[messageIndex] = {
          ...messages[messageIndex],
          text: "Published selected attempt.",
          artifact_id: published.artifact_id,
          run_id: messages[messageIndex].run_id
            ?? runIdFromArtifactId(published.artifact_id),
          status: "done",
        };
      }
      const updatedAt = Date.now();
      const updatedChild: Conversation = {
        ...latest,
        messages,
        artifacts: {
          ...latest.artifacts,
          [published.artifact_id]: published,
        },
        active_artifact_id: published.artifact_id,
        published_artifact_id: published.artifact_id,
        ...(sourceRunStaysLive ? {} : { pending: false, run_id: undefined }),
        updated_at: updatedAt,
      };
      const conversations = {
        ...current.conversations,
        [conversationId]: updatedChild,
      };
      if (latest.paper_bundle?.kind === "child") {
        const parentId = latest.paper_bundle.parent_conversation_id;
        const artifactType = latest.paper_bundle.artifact_type;
        const parent = conversations[parentId];
        if (parent?.paper_bundle?.kind === "parent") {
          const tasks = {
            ...parent.paper_bundle.tasks,
            [artifactType]: {
              ...parent.paper_bundle.tasks[artifactType],
              artifact_id: published.artifact_id,
              status: "complete" as const,
              error: undefined,
              finished_at: updatedAt,
            },
          };
          conversations[parentId] = {
            ...parent,
            artifacts: {
              ...parent.artifacts,
              [published.artifact_id]: published,
            },
            active_artifact_id: published.artifact_id,
            updated_at: updatedAt,
            pending: paperBundleHasActiveTasks({
              ...parent.paper_bundle,
              tasks,
            }),
            paper_bundle: {
              ...parent.paper_bundle,
              tasks,
            },
          };
        }
      }
      return { conversations };
    });
  };

  const captureDirectCandidatePublicationLineage = (
    conversationId: string,
    sourceRunId: string,
    sourceCandidateId: string,
  ): DirectCandidatePublicationLineage => {
    const conversation = get().conversations[conversationId];
    if (!conversation) {
      throw new Error("The target conversation is no longer available.");
    }
    if (conversation.paper_bundle?.kind !== "child") {
      return { conversationId, sourceRunId, sourceCandidateId };
    }
    const parentConversationId = conversation.paper_bundle.parent_conversation_id;
    const artifactType = conversation.paper_bundle.artifact_type;
    const parent = get().conversations[parentConversationId];
    const task = parent?.paper_bundle?.kind === "parent"
      ? parent.paper_bundle.tasks[artifactType]
      : undefined;
    return {
      conversationId,
      sourceRunId,
      sourceCandidateId,
      parentConversationId,
      artifactType,
      parentJobId: parent?.paper_bundle?.kind === "parent"
        ? parent.paper_bundle.job_id
        : undefined,
      authoringRunId: task?.authoring_run_id ?? sourceRunId,
    };
  };

  const directCandidatePublicationLineageIsCurrent = (
    lineage: DirectCandidatePublicationLineage,
  ): boolean => {
    const conversation = get().conversations[lineage.conversationId];
    if (!conversation) return false;
    if (!lineage.parentConversationId || !lineage.artifactType) {
      return conversation.paper_bundle?.kind !== "child";
    }
    if (
      conversation.paper_bundle?.kind !== "child"
      || conversation.paper_bundle.parent_conversation_id !== lineage.parentConversationId
      || conversation.paper_bundle.artifact_type !== lineage.artifactType
    ) {
      return false;
    }
    const parent = get().conversations[lineage.parentConversationId];
    if (
      parent?.paper_bundle?.kind !== "parent"
      || parent.paper_bundle.job_id !== lineage.parentJobId
    ) {
      return false;
    }
    const task = parent.paper_bundle.tasks[lineage.artifactType];
    return (task.authoring_run_id ?? task.run_id) === lineage.authoringRunId;
  };

  const directCandidatePublicationMessageIsCurrent = (
    lineage: DirectCandidatePublicationLineage,
    messageId: string,
    publicationRunId: string,
  ): boolean => {
    if (!directCandidatePublicationLineageIsCurrent(lineage)) return false;
    return Boolean(get().conversations[lineage.conversationId]?.messages.some((message) => (
      message.id === messageId
      && message.run_id === publicationRunId
      && message.task_type === CANDIDATE_PUBLISH_TASK
      && message.task_payload?.source_run_id === lineage.sourceRunId
      && message.task_payload?.source_candidate_id === lineage.sourceCandidateId
    )));
  };

  const applyDirectPublishedAttempt = (
    lineage: DirectCandidatePublicationLineage,
    publicationRunId: string,
    publicationMessageId: string,
    published: Artifact,
    sourceRunStaysLive: boolean,
  ) => {
    if (!directCandidatePublicationMessageIsCurrent(
      lineage,
      publicationMessageId,
      publicationRunId,
    )) return;
    set((state) => {
      const child = state.conversations[lineage.conversationId];
      if (!child) return state;
      const publicationMessage = child.messages.find((message) => (
        message.id === publicationMessageId
        && message.run_id === publicationRunId
        && message.task_type === CANDIDATE_PUBLISH_TASK
        && message.task_payload?.source_run_id === lineage.sourceRunId
        && message.task_payload?.source_candidate_id === lineage.sourceCandidateId
      ));
      if (!publicationMessage) return state;
      const updatedAt = Date.now();
      const updatedChild: Conversation = {
        ...child,
        messages: child.messages.map((message) => (
          message.id === publicationMessageId
            ? {
                ...message,
                text: "Published selected attempt.",
                artifact_id: published.artifact_id,
                status: "done" as const,
                failure: undefined,
              }
            : message
        )),
        artifacts: {
          ...child.artifacts,
          [published.artifact_id]: published,
        },
        active_artifact_id: published.artifact_id,
        published_artifact_id: published.artifact_id,
        ...(
          !sourceRunStaysLive && child.run_id === publicationRunId
            ? { pending: false, run_id: undefined }
            : {}
        ),
        updated_at: updatedAt,
      };
      const conversations = {
        ...state.conversations,
        [lineage.conversationId]: updatedChild,
      };
      if (lineage.parentConversationId && lineage.artifactType) {
        const parent = conversations[lineage.parentConversationId];
        if (
          parent?.paper_bundle?.kind !== "parent"
          || parent.paper_bundle.job_id !== lineage.parentJobId
        ) {
          return state;
        }
        const task = parent.paper_bundle.tasks[lineage.artifactType];
        if ((task.authoring_run_id ?? task.run_id) !== lineage.authoringRunId) {
          return state;
        }
        const tasks = {
          ...parent.paper_bundle.tasks,
          [lineage.artifactType]: {
            ...task,
            status: "complete" as const,
            run_id: publicationRunId,
            authoring_run_id: lineage.authoringRunId,
            artifact_id: published.artifact_id,
            error: undefined,
            finished_at: updatedAt,
            terminal: true,
            process_free: true,
          },
        };
        conversations[lineage.parentConversationId] = {
          ...parent,
          artifacts: {
            ...parent.artifacts,
            [published.artifact_id]: published,
          },
          active_artifact_id: published.artifact_id,
          updated_at: updatedAt,
          pending: paperBundleHasActiveTasks({
            ...parent.paper_bundle,
            tasks,
          }),
          paper_bundle: {
            ...parent.paper_bundle,
            tasks,
          },
        };
      }
      return { conversations };
    });
  };

  const ensureRunStillOwned = (conversationId: string, runId: string) => {
    if (get().conversations[conversationId]?.run_id !== runId) {
      throw new RunWaitCancelledError();
    }
  };

  const applyPaperBundleResult = (
    parentConversationId: string,
    artifactType: ArtifactType,
    runId: string,
    result: GenerateResponse,
    progress?: RunProgress,
    shouldApplyResult?: () => boolean,
  ) => {
    if (shouldApplyResult && !shouldApplyResult()) return;
    const parent = get().conversations[parentConversationId];
    const task = parent?.paper_bundle?.kind === "parent"
      ? parent.paper_bundle.tasks[artifactType]
      : undefined;
    if (
      task
      && publishedAttemptForkForSourceRun(
        get().conversations[task.child_conversation_id],
        runId,
      )
    ) {
      return;
    }
    const failureStatus = result.message.failure?.status.toLowerCase() ?? "";
    const taskStatus: PaperBundleTaskStatus = parent?.paper_bundle?.kind === "parent"
      && paperBundleTaskWasCancelled(
        currentDemoUserScope(),
        parentConversationId,
        paperBundleCancellationGeneration(parent.paper_bundle),
        artifactType,
      )
      ? "cancelled"
      : result.artifact
        ? "complete"
        : failureStatus.includes("cancel")
        ? "cancelled"
        : "failed";
    const taskError = taskStatus === "cancelled"
      ? "Run cancelled."
      : result.artifact
        ? undefined
        : result.message.failure
        ? result.message.failure.agent_last_note
          || result.message.text
          || result.message.failure.status
        : result.message.text
          || "Run failed.";
    patchConversation(parentConversationId, (parent) => {
      if (shouldApplyResult && !shouldApplyResult()) return parent;
      const bundle = parent.paper_bundle;
      if (bundle?.kind !== "parent") return parent;
      const task = bundle.tasks[artifactType];
      if (task.run_id !== runId) return parent;
      const artifact = result.artifact;
      const tasks = {
        ...bundle.tasks,
        [artifactType]: {
          ...task,
          ...terminalPaperBundleTaskStats(task, progress),
          status: taskStatus,
          run_id: runId,
          terminal: true,
          process_free: true,
          ...(artifact ? { artifact_id: artifact.artifact_id } : {}),
          ...(taskError ? { error: taskError } : { error: undefined }),
        },
      };
      const firstCompletedBundleArtifactId = PAPER_BUNDLE_ARTIFACT_ORDER
        .map((type) => tasks[type])
        .find((candidate) => candidate.status === "complete" && candidate.artifact_id)
        ?.artifact_id;
      const artifacts = artifact
        ? {
            ...parent.artifacts,
            [artifact.artifact_id]: artifact,
          }
        : parent.artifacts;
      const validActiveArtifactId = parent.active_artifact_id
        && artifacts[parent.active_artifact_id]
        ? parent.active_artifact_id
        : null;
      return {
        ...parent,
        paper_bundle: {
          ...bundle,
          tasks,
        },
        pending: paperBundleHasActiveTasks({ ...bundle, tasks }),
        ...(artifact
          ? {
              artifacts,
              active_artifact_id: validActiveArtifactId
                ?? (artifactType === "poster"
                  ? artifact.artifact_id
                  : firstCompletedBundleArtifactId ?? artifact.artifact_id),
            }
          : {}),
      };
    });
  };

	  const sanitizeSelection = (ids: string[], art?: Artifact | null) => {
    if (!art) return [];
    const valid = new Set(art.layers.map((l) => l.layer_id));
    const out: string[] = [];
    for (const id of ids) {
      if (valid.has(id) && !out.includes(id)) out.push(id);
    }
    return out;
  };

  const setSelectionState = (ids: string[]) => {
    const { art } = getActiveContext();
    const selected = sanitizeSelection(ids, art);
    set({
      selected_layer_ids: selected,
      selected_layer_id: selected[selected.length - 1] ?? null,
    });
  };

  const selectedLayers = (art: Artifact) => {
    const ids = get().selected_layer_ids.length
      ? get().selected_layer_ids
      : get().selected_layer_id
        ? [get().selected_layer_id!]
        : [];
    const selected = new Set(ids);
    return art.layers.filter((l) => selected.has(l.layer_id));
  };

  const editableSelectedLayers = (art: Artifact) =>
    selectedLayers(art).filter((l) => l.bbox && !l.locked);

  const boundsOf = (layers: Layer[]): Bbox | null => {
    const boxes = layers.map((l) => l.bbox).filter(Boolean) as Bbox[];
    if (!boxes.length) return null;
    const left = Math.min(...boxes.map((b) => b.x));
    const top = Math.min(...boxes.map((b) => b.y));
    const right = Math.max(...boxes.map((b) => b.x + b.w));
    const bottom = Math.max(...boxes.map((b) => b.y + b.h));
    return { x: left, y: top, w: right - left, h: bottom - top };
  };

  const clampBoxToFrame = (box: Bbox, frame: Bbox): Bbox => ({
    ...box,
    x: Math.round(Math.min(Math.max(box.x, frame.x), frame.x + frame.w - box.w)),
    y: Math.round(Math.min(Math.max(box.y, frame.y), frame.y + frame.h - box.h)),
  });

  const layerCenterInFrame = (layer: Layer, frame: { bbox: Bbox }) => {
    const b = layer.bbox;
    if (!b) return false;
    if (layer.kind === "background" && (b.w > frame.bbox.w * 1.05 || b.h > frame.bbox.h * 1.05)) {
      return false;
    }
    const cx = b.x + b.w / 2;
    const cy = b.y + b.h / 2;
    return (
      cx >= frame.bbox.x &&
      cx <= frame.bbox.x + frame.bbox.w &&
      cy >= frame.bbox.y &&
      cy <= frame.bbox.y + frame.bbox.h
    );
  };

  const layerScopeKey = (
    art: Artifact,
    layer: Layer,
    frames = detectSlideFrames(art)
  ) => {
    if (frames.length >= 2) {
      const frame = frames.find((f) => layerCenterInFrame(layer, f));
      return frame ? `slide:${frame.idx}` : "canvas";
    }
    return "canvas";
  };

  const selectionScopeKey = (art: Artifact, layers: Layer[]) => {
    if (!layers.length) return null;
    const frames = detectSlideFrames(art);
    const scopes = new Set(layers.map((l) => layerScopeKey(art, l, frames)));
    return scopes.size === 1 ? [...scopes][0] : null;
  };

  const nextGroupName = (art: Artifact) => `Group ${(art.layer_groups?.length ?? 0) + 1}`;

  const pruneLayerGroups = (art: Artifact): Artifact => {
    const groups = art.layer_groups ?? [];
    if (!groups.length) return art;
    const referenced = new Set(
      art.layers.map((l) => l.group_id).filter(Boolean) as string[]
    );
    const keptGroups = groups.filter((g) => referenced.has(g.group_id));
    const valid = new Set(keptGroups.map((g) => g.group_id));
    const layers = art.layers.map((l) =>
      l.group_id && !valid.has(l.group_id) ? withoutGroupId(l) : l
    );
    return {
      ...art,
      layers,
      layer_groups: keptGroups.length ? keptGroups : undefined,
    };
  };

  const completeSelectedGroups = (art: Artifact, source: Layer[]) => {
    const selected = new Set(source.map((l) => l.layer_id));
    return (art.layer_groups ?? []).filter((g) => {
      const childIds = art.layers
        .filter((l) => l.group_id === g.group_id)
        .map((l) => l.layer_id);
      return childIds.length > 0 && childIds.every((id) => selected.has(id));
    });
  };

  const normalizeCopiedLayersForGroups = (
    art: Artifact,
    source: Layer[]
  ): { layers: Layer[]; groups: LayerGroup[] } => {
    const completeGroups = completeSelectedGroups(art, source);
    const completeGroupIds = new Set(completeGroups.map((g) => g.group_id));
    return {
      groups: completeGroups.map((g) => ({ ...g })),
      layers: source.map((l) =>
        l.group_id && !completeGroupIds.has(l.group_id)
          ? withoutGroupId(cloneLayer(l))
          : cloneLayer(l)
      ),
    };
  };

  const slideGap = (frames: Array<{ bbox: Bbox }>) => {
    if (frames.length >= 2) {
      return Math.max(40, Math.round(frames[1].bbox.y - frames[0].bbox.y - frames[0].bbox.h));
    }
    return 60;
  };

  const activeInsertFrame = (art: Artifact): Bbox => {
    const frames = detectSlideFrames(art);
    if (frames.length) {
      return frames[Math.min(get().active_slide_idx, frames.length - 1)].bbox;
    }
    return { x: 0, y: 0, w: art.canvas.w, h: art.canvas.h };
  };

  const textStylePatch = (l: Layer): Partial<Layer> => ({
    font_family: l.font_family,
    font_size_px: l.font_size_px,
    font_weight: l.font_weight,
    font_style: l.font_style,
    line_height: l.line_height,
    letter_spacing: l.letter_spacing,
    align: l.align,
    text_transform: l.text_transform,
    list_style: l.list_style,
    effects: cloneLayer(l).effects,
  });

  const shapeStylePatch = (l: Layer): Partial<Layer> => ({
    fill_color: l.fill_color,
    stroke_color: l.stroke_color,
    stroke_width: l.stroke_width,
    stroke_dash: l.stroke_dash,
    corner_radius: l.corner_radius,
    opacity: l.opacity,
    shadow: cloneLayer(l).shadow,
  });

  const imageStylePatch = (l: Layer): Partial<Layer> => ({
    fit: l.fit,
    object_position: l.object_position,
    corner_radius: l.corner_radius,
    opacity: l.opacity,
    shadow: cloneLayer(l).shadow,
  });

  const recordHistory = (art: Artifact) => {
    const snapshot = cloneArtifact(art);
    set((s) => {
      const current = s.editor_history[art.artifact_id] ?? { past: [], future: [] };
      return {
        editor_history: {
          ...s.editor_history,
          [art.artifact_id]: {
            past: [...current.past.slice(-49), snapshot],
            future: [],
          },
        },
      };
    });
  };

  const patchActiveArtifact = (
    mutator: (a: Artifact) => Artifact,
    opts: UpdateOptions = {}
  ) => {
    const { history = true } = opts;
    const id = get().current_conversation_id;
    const c = get().conversations[id];
    const active = c?.active_artifact_id;
    const art = active ? c?.artifacts[active] : undefined;
    if (history && art) recordHistory(art);
    patchConversation(id, (c) => {
      if (!c.active_artifact_id) return c;
      const a = c.artifacts[c.active_artifact_id];
      if (!a) return c;
      return {
        ...c,
        artifacts: { ...c.artifacts, [c.active_artifact_id]: mutator(a) },
      };
    });
  };

  const emitArtifactEvent = (
    conversation_id: string,
    event:
      | "artifact.opened"
      | "artifact.downloaded"
      | "openresearch.project_requested"
      | "openresearch.project_ready"
      | "openresearch.project_failed",
    artifact_id?: string | null,
    extra?: Record<string, unknown>,
  ) => {
    const conv = get().conversations[conversation_id];
    const aid = artifact_id || conv?.active_artifact_id;
    if (!conv || !aid) return;
    const art = conv.artifacts[aid];
    if (!art) return;
    void sendDesignEvent({
      conversation_id,
      event,
      run_id: runIdFromArtifactId(art.artifact_id),
      artifact_id: art.artifact_id,
      data: {
        artifact_type: artifactTypeForArtifact(art),
        name: art.name,
        native_format: art.native_format,
        canvas: art.canvas,
        parent_artifact_id: art.parent_artifact_id,
        ...(extra ?? {}),
      },
    }).catch(() => undefined);
  };

  const publishAttemptCandidateDirect = async (
    sourceRunId: string,
    candidate: AttemptCandidateSummary,
    conversationId: string,
    idempotencyKey: string,
  ): Promise<void> => {
    const lineage = captureDirectCandidatePublicationLineage(
      conversationId,
      sourceRunId,
      candidate.candidate_id,
    );
    const sourceRunStaysLive = sourceRunIsActiveForConversation(
      get().conversations,
      conversationId,
      sourceRunId,
    );
    const operationConversationId = sourceRunStaysLive
      ? candidatePublishOperationId(conversationId)
      : conversationId;
    const controller = new AbortController();
    let resolveSettlement!: (result: CandidatePublicationCancellation) => void;
    const publicationOwner: CandidatePublicationOwner = {
      kind: "candidate_publish",
      token: Symbol("candidate_publish"),
      ownerScope: currentDemoUserScope(),
      operationConversationId,
      controller,
      cancelRequested: false,
      flowComplete: false,
      settlement: new Promise<CandidatePublicationCancellation>((resolve) => {
        resolveSettlement = resolve;
      }),
      resolveSettlement: (result) => resolveSettlement(result),
    };
    const releaseDerivedRun = acquireDerivedRunOperation(
      conversationId,
      "candidate_publish",
      publicationOwner,
    );
    const placeholderId = nextId("msg");
    const taskPayload: MessageTaskPayload = {
      artifact_type: candidate.artifact_type,
      source_run_id: sourceRunId,
      source_candidate_id: candidate.candidate_id,
    };
    let activeRunId: string | undefined;
    try {
      const { ack, reconcileImmediately, startReplay } = await resolveReservedRunStart({
        request: publishRunAttempt(
          sourceRunId,
          candidate.attempt,
          candidate.source_sha256,
          idempotencyKey,
          conversationId,
          controller.signal,
          (reservedRunId) => {
          if (_ACTIVE_DERIVED_RUN_CONVERSATIONS.get(conversationId) !== publicationOwner) {
            throw new RunWaitCancelledError();
          }
          activeRunId = reservedRunId;
          publicationOwner.runId = reservedRunId;
          if (publicationOwner.cancelRequested) {
            throw new RunWaitCancelledError();
          }
          if (!directCandidatePublicationLineageIsCurrent(lineage)) {
            throw new RunWaitCancelledError();
          }
          _RESERVED_RUN_UPLOAD_ABORTS.set(operationConversationId, {
            runId: reservedRunId,
            controller,
          });
          patchConversation(conversationId, (current) => {
            const placeholder: Message = {
              id: placeholderId,
              role: "assistant",
              text: "Publishing selected attempt.",
              ts: Date.now(),
              run_id: reservedRunId,
              status: "streaming",
              task_type: CANDIDATE_PUBLISH_TASK,
              task_payload: taskPayload,
            };
            const hasPlaceholder = current.messages.some(
              (message) => message.id === placeholderId,
            );
            return {
              ...current,
              ...(!sourceRunStaysLive
                ? { pending: true, run_id: reservedRunId }
                : {}),
              messages: hasPlaceholder
                ? current.messages.map((message) => (
                    message.id === placeholderId ? { ...message, ...placeholder } : message
                  ))
                : [...current.messages, placeholder],
            };
          });
          set((state) => ({
            runs_progress: {
              ...state.runs_progress,
              [operationConversationId]: initialProgress(reservedRunId, "attempt_publish"),
              },
            }));
          },
        ),
        reservedRunId: () => activeRunId,
        isCurrent: (runId) => (
          _ACTIVE_DERIVED_RUN_CONVERSATIONS.get(conversationId) === publicationOwner
          && publicationOwner.runId === runId
          && !publicationOwner.cancelRequested
          && directCandidatePublicationLineageIsCurrent(lineage)
        ),
        placeholderMessage: {
          id: placeholderId,
          role: "assistant",
          text: "Publishing selected attempt.",
          ts: Date.now(),
          status: "streaming",
          task_type: CANDIDATE_PUBLISH_TASK,
          task_payload: taskPayload,
        },
        progressMode: "attempt_publish",
      });
      if (activeRunId && ack.run_id !== activeRunId) {
        throw new Error("Candidate publication start returned a different run.");
      }
      activeRunId = ack.run_id;
      publicationOwner.runId = ack.run_id;
      if (publicationOwner.cancelRequested) {
        throw new RunWaitCancelledError();
      }
      const publicationIsCurrent = () => directCandidatePublicationMessageIsCurrent(
        lineage,
        placeholderId,
        ack.run_id,
      );
      const result = await runGenerateAckFlow({
        convId: operationConversationId,
        placeholderId,
        ack,
        timeoutMs: 10 * 60 * 1000,
        timeoutMessage: "attempt publish still running after 10 min",
        closedMessage: "attempt publish event stream closed",
        task_type: CANDIDATE_PUBLISH_TASK,
        task_payload: taskPayload,
        activateArtifact: false,
        reconcileImmediately,
        startReplay,
        shouldApplyResult: publicationIsCurrent,
        validateResult: (response) => Boolean(
          response.message.run_id === ack.run_id
          && response.artifact
          && response.artifact.attempt_lineage?.source_run_id === sourceRunId
          && response.artifact.attempt_lineage?.source_candidate_id === candidate.candidate_id
        ),
      });
      if (publicationOwner.cancelRequested) {
        throw new RunWaitCancelledError();
      }
      if (!publicationIsCurrent()) return;
      if (!result.artifact) {
        throw new Error("Attempt publish did not produce a published artifact.");
      }
      applyDirectPublishedAttempt(
        lineage,
        ack.run_id,
        placeholderId,
        result.artifact,
        sourceRunStaysLive,
      );
      if (lineage.parentConversationId && lineage.parentJobId) {
        await get().recoverPaperBundles();
      }
    } catch (error) {
      if (publicationOwner.cancelRequested) {
        let cancellation = publicationOwner.settlementResult;
        if (!cancellation && !publicationOwner.cancellationRequest) {
          cancellation = activeRunId
            ? await requestOwnedCandidatePublicationCancellation(
              publicationOwner,
              conversationId,
              publicationOwner.ownerScope,
              activeCandidatePublishMessage(get().conversations[conversationId]),
            )
            : { attempted: true, confirmed: true };
          settleCandidatePublicationOwner(publicationOwner, cancellation);
        }
        return;
      }
      const cancellation = runCancellationDisposition(
        operationConversationId,
        activeRunId,
        error,
      );
      if (cancellation) return;
      patchConversation(conversationId, (current) => {
        const ownsPlaceholder = current.messages.some((message) => (
          message.id === placeholderId
          && (!activeRunId || message.run_id === activeRunId)
          && message.task_type === CANDIDATE_PUBLISH_TASK
          && message.task_payload?.source_run_id === sourceRunId
          && message.task_payload?.source_candidate_id === candidate.candidate_id
        ));
        if (!ownsPlaceholder) return current;
        const detail = error instanceof Error ? error.message : "Attempt publish failed.";
        return {
          ...current,
          ...(
            !sourceRunStaysLive
            && (!activeRunId || current.run_id === activeRunId)
              ? { pending: false, run_id: undefined }
              : {}
          ),
          messages: current.messages.map((message) => (
            message.id === placeholderId
              ? {
                  ...message,
                  text: detail,
                  status: "error" as const,
                  failure: runClientFailure(error, candidate.artifact_type, activeRunId),
                }
              : message
          )),
        };
      });
      clearRunProgress(operationConversationId, activeRunId);
      throw error;
    } finally {
      publicationOwner.flowComplete = true;
      if (
        !publicationOwner.cancelRequested
        || publicationOwner.settlementResult?.confirmed === true
      ) {
        cleanupReservedRunOwner(operationConversationId, activeRunId);
        releaseDerivedRun();
      }
    }
  };

  return {
    mode: "chat",
    selected_layer_id: null,
    selected_layer_ids: [],
    intent_type: null,
    history_sidebar_open: true,
    properties_sidebar_open: true,
    design_focus_mode: false,
    history_sidebar_width: 260,
    chat_rail_width: 320,
    properties_sidebar_width: 320,
    deck_navbar_height: 120,
    active_slide_idx: 0,
    snap_enabled: true,
    grid_visible: true,
    rulers_visible: true,
    safe_margins_visible: true,
    smart_guides_visible: true,
    grid_size_px: 8,
    grid_major_every: 5,
    safe_margin_pct: 0.06,
    recent_colors: ["#17130f", "#fbf7ec", "#176448", "#92342e", "#6d665d"],
    insert_placement_mode: "near-selection",
    area_revision_active: false,
    area_revision_items: [],
    area_revision_focus_id: null,
    selected_paper_asset: null,
    pending_insert: null,
    layer_group_collapsed: {},
    backend_info: null,
    backend_needs_setup: false,
    settings_open: false,
    ui_language: "en",
    pending_apply: false,
    editor_history: {},
    editor_clipboard: [],
    editor_clipboard_groups: [],
    editor_style_clipboard: null,
    runs_progress: {},
    candidate_publication_owners: {},
    run_attempts: {},
    poster_palettes: [],
    poster_palettes_status: "idle",
    poster_palettes_error: null,
    poster_canvas_presets: [],
    poster_canvas_presets_status: "idle",
    poster_canvas_presets_error: null,
    canvas_validation_errors: {},
    conversations: { [initialConversation.id]: initialConversation },
    current_conversation_id: initialConversation.id,
    history_user_scope: null,

    // ---- boot ----

    loadBackendInfo: async () => {
      const info = await fetchHealth();
      if (info) {
        set({
          backend_info: {
            designer_model: info.designer_model,
            image_model: info.image_model,
            models: info.models,
            demo_mode: info.demo_mode,
            public_user_isolation: info.public_user_isolation,
            user_isolation: info.user_isolation,
            demo: info.demo,
            backend_profile: info.backend_profile,
          },
          backend_needs_setup: info.needs_setup,
        });
      }
    },

    loadRunAttempts: async (runId) => {
      const clean = runId.trim();
      if (!clean) return;
      const owner = Symbol(clean);
      runAttemptHydrationOwners.set(clean, owner);
      set((state) => ({
        run_attempts: {
          ...state.run_attempts,
          [clean]: {
            ...(state.run_attempts[clean] ?? {
              run_id: clean,
              candidates: [],
              selection_phase: "idle" as const,
            }),
            loading: true,
            error: undefined,
          },
        },
      }));
      const controller = new AbortController();
      const timeout = window.setTimeout(
        () => controller.abort(new Error("Attempt history request timed out.")),
        RUN_ATTEMPT_REQUEST_TIMEOUT_MS,
      );
      try {
        const attempts = await fetchRunAttempts(clean, controller.signal);
        if (runAttemptHydrationOwners.get(clean) !== owner) return;
        set((state) => ({
          run_attempts: { ...state.run_attempts, [clean]: attempts },
        }));
      } catch (error) {
        if (runAttemptHydrationOwners.get(clean) !== owner) return;
        set((state) => ({
          run_attempts: {
            ...state.run_attempts,
            [clean]: {
              ...(state.run_attempts[clean] ?? {
                run_id: clean,
                candidates: [],
                selection_phase: "idle" as const,
                loading: false,
              }),
              loading: false,
              error: error instanceof Error ? error.message : String(error),
            },
          },
        }));
      } finally {
        window.clearTimeout(timeout);
        if (runAttemptHydrationOwners.get(clean) === owner) {
          runAttemptHydrationOwners.delete(clean);
        }
      }
    },

    selectAttempt: async (runId, candidate, targetConversationId) => {
      const conversationId = targetConversationId ?? get().current_conversation_id;
      if (attemptRunHasActiveCandidatePublication(runId, conversationId)) {
        throw new Error("Wait for candidate publication to finish before using an attempt.");
      }
      const blocked = Object.values(get().conversations).some((conversation) => (
        conversation.paper_bundle?.kind === "parent"
        && paperBundleBlocksAttemptActions(conversation.paper_bundle, runId)
      ));
      if (blocked) {
        throw new Error("Wait for Paper All-in-One cancellation to finish before using an attempt.");
      }
      const idempotencyKey = globalThis.crypto?.randomUUID?.()
        ?? `attempt-${candidate.attempt}-${Date.now()}`;
      const sourceActive = sourceRunIsActiveForConversation(
        get().conversations,
        conversationId,
        runId,
      );
      const sourceKnownTerminal = sourceRunIsKnownTerminalForConversation(
        get().conversations,
        conversationId,
        runId,
      );
      if (sourceActive || !sourceKnownTerminal) {
        try {
          const next = await selectRunAttempt(
            runId,
            candidate.attempt,
            candidate.source_sha256,
            idempotencyKey,
          );
          set((state) => ({
            run_attempts: { ...state.run_attempts, [runId]: next },
          }));
          return;
        } catch (error) {
          if (!(error instanceof ApiError) || error.code !== "run_not_selectable") {
            throw error;
          }
        }
      }
      await publishAttemptCandidateDirect(
        runId,
        candidate,
        conversationId,
        idempotencyKey,
      );
    },

    openAttemptInCanvas: async (runId, candidate, targetConversationId) => {
      if (attemptRunHasActiveCandidatePublication(runId, targetConversationId)) {
        throw new Error("Wait for candidate publication to finish before editing an attempt.");
      }
      const blocked = Object.values(get().conversations).some((conversation) => (
        conversation.paper_bundle?.kind === "parent"
        && paperBundleBlocksAttemptActions(conversation.paper_bundle, runId)
      ));
      if (blocked) {
        throw new Error("Wait for Paper All-in-One cancellation to finish before editing an attempt.");
      }
      const conversationId = targetConversationId ?? get().current_conversation_id;
      const current = get().conversations[conversationId];
      const matchingDrafts = current
        ? Object.values(current.artifacts).filter((artifact) => (
          artifact.candidate_draft
          && Number(artifact.attempt_lineage?.materialization_version ?? 0) >= 2
          && artifact.attempt_lineage?.source_run_id === runId
          && artifact.attempt_lineage?.source_candidate_id === candidate.candidate_id
        ))
        : [];
      const activeDraft = current?.active_artifact_id
        ? current.artifacts[current.active_artifact_id]
        : undefined;
      const existingDraft = activeDraft && matchingDrafts.includes(activeDraft)
        ? activeDraft
        : matchingDrafts.at(-1);
      if (existingDraft) {
        set((state) => {
          const conversation = state.conversations[conversationId];
          if (!conversation) return state;
          return {
            conversations: {
              ...state.conversations,
              [conversationId]: {
                ...conversation,
                active_artifact_id: existingDraft.artifact_id,
                poster_palette_id:
                  existingDraft.attempt_lineage?.poster_palette_id
                  ?? conversation.poster_palette_id,
                updated_at: Date.now(),
              },
            },
            current_conversation_id: conversationId,
            mode: "canvas",
            properties_sidebar_open: true,
            selected_layer_id: null,
            selected_layer_ids: [],
          };
        });
        return;
      }
	      const sourceRunStaysLive = sourceRunOwnsOrdinarySlot(current, runId);
      const operationConversationId = sourceRunStaysLive
        ? attemptForkOperationId(conversationId)
        : conversationId;
      const controller = new AbortController();
      let resolveSettlement!: (result: CandidatePublicationCancellation) => void;
      const forkOwner: AttemptForkOwner = {
        kind: "attempt_fork",
        token: Symbol("attempt_fork"),
        ownerScope: currentDemoUserScope(),
        operationConversationId,
        controller,
        cancelRequested: false,
        flowComplete: false,
        settlement: new Promise<CandidatePublicationCancellation>((resolve) => {
          resolveSettlement = resolve;
        }),
        resolveSettlement: (result) => resolveSettlement(result),
      };
      const releaseDerivedRun = acquireDerivedRunOperation(
        conversationId,
        "attempt_fork",
        forkOwner,
      );
      let activeRunId: string | undefined;
      let artifact: Artifact;
      try {
        const { ack, reconcileImmediately, startReplay } = await resolveReservedRunStart({
          request: forkRunAttempt(
            runId,
            candidate.attempt,
            conversationId,
            controller.signal,
            (reservedRunId) => {
            if (_ACTIVE_DERIVED_RUN_CONVERSATIONS.get(conversationId) !== forkOwner) {
              throw new RunWaitCancelledError();
            }
            activeRunId = reservedRunId;
            forkOwner.runId = reservedRunId;
            if (forkOwner.cancelRequested) {
              throw new RunWaitCancelledError();
            }
            _RESERVED_RUN_UPLOAD_ABORTS.set(operationConversationId, {
              runId: reservedRunId,
              controller,
            });
            if (!sourceRunStaysLive) {
              patchConversation(conversationId, (conversation) => ({
                ...conversation,
                pending: true,
                run_id: reservedRunId,
              }));
            }
            set((state) => ({
              runs_progress: {
                ...state.runs_progress,
                [operationConversationId]: initialProgress(reservedRunId, "attempt_fork"),
                },
              }));
            },
          ),
          reservedRunId: () => activeRunId,
          isCurrent: (reservedRunId) => (
            _ACTIVE_DERIVED_RUN_CONVERSATIONS.get(conversationId) === forkOwner
            && forkOwner.runId === reservedRunId
            && !forkOwner.cancelRequested
          ),
          placeholderMessage: {
            id: `msg_${runId}_attempt_${candidate.attempt}`,
            role: "assistant",
            text: "Opening selected attempt.",
            ts: Date.now(),
            status: "streaming",
          },
          progressMode: "attempt_fork",
        });
        activeRunId = ack.run_id;
        forkOwner.runId = ack.run_id;
        if (forkOwner.cancelRequested) {
          throw new RunWaitCancelledError();
        }
        if (!sourceRunStaysLive) {
          ensureRunStillOwned(conversationId, ack.run_id);
        }
        const result = await runGenerateAckFlow({
          convId: operationConversationId,
          placeholderId: "",
          ack,
          timeoutMs: 10 * 60 * 1000,
          timeoutMessage: "attempt materialization still running after 10 min",
          closedMessage: "attempt materialization event stream closed",
          activateArtifact: false,
          reconcileImmediately,
          startReplay,
        });
        if (!result.artifact) {
          throw new Error("Attempt materialization did not produce an editable artifact.");
        }
        artifact = result.artifact;
      } catch (error) {
        if (forkOwner.cancelRequested) {
          let cancellation = forkOwner.settlementResult;
          if (!cancellation && !forkOwner.cancellationRequest) {
            cancellation = activeRunId
              ? await requestOwnedAttemptForkCancellation(forkOwner, conversationId)
              : { attempted: true, confirmed: true };
            settleAttemptForkOwner(forkOwner, cancellation);
          }
          return;
        }
        const cancellation = runCancellationDisposition(
          operationConversationId,
          activeRunId,
          error,
        );
        if (cancellation) return;
        if (!sourceRunStaysLive) {
          patchConversation(conversationId, (conversation) => (
            !activeRunId || conversation.run_id === activeRunId
              ? { ...conversation, pending: false, run_id: undefined }
              : conversation
          ));
        }
        clearRunProgress(operationConversationId, activeRunId);
        throw error;
      } finally {
        forkOwner.flowComplete = true;
        if (!forkOwner.cancelRequested || forkOwner.settlementResult?.confirmed === true) {
          cleanupReservedRunOwner(operationConversationId, activeRunId);
          releaseDerivedRun();
        }
      }
      if (forkOwner.cancelRequested) return;
      set((state) => {
        const conversation = state.conversations[conversationId];
        if (!conversation) return state;
        const currentArtifact = conversation.active_artifact_id
          ? conversation.artifacts[conversation.active_artifact_id]
          : undefined;
        const publishedArtifactId = conversation.published_artifact_id
          ?? (
            currentArtifact && !currentArtifact.candidate_draft
              ? currentArtifact.artifact_id
              : null
          );
        return {
          conversations: {
            ...state.conversations,
            [conversationId]: {
              ...conversation,
              artifacts: {
                ...conversation.artifacts,
                [artifact.artifact_id]: artifact,
              },
              active_artifact_id: artifact.artifact_id,
              published_artifact_id: publishedArtifactId,
              poster_palette_id:
                artifact.attempt_lineage?.poster_palette_id
                ?? conversation.poster_palette_id,
              updated_at: Date.now(),
            },
          },
          current_conversation_id: conversationId,
          mode: "canvas",
          properties_sidebar_open: true,
          selected_layer_id: null,
          selected_layer_ids: [],
        };
      });
    },

    publishActiveCandidateDraft: async (target) => {
      const state = get();
      const conversationId = target.conversationId;
      const conversation = state.conversations[conversationId];
      const active = conversation?.artifacts[target.artifactId];
      if (!active?.candidate_draft) {
        throw new Error("The selected attempt draft is no longer available.");
      }
      const sourceRunId = active.attempt_lineage?.source_run_id;
      const sourceCandidateId = active.attempt_lineage?.source_candidate_id;
      if (
        sourceRunId !== target.sourceRunId
        || sourceCandidateId !== target.sourceCandidateId
      ) {
        throw new Error(
          "The selected attempt draft has changed. Reopen the attempt before publishing.",
        );
      }
      const attemptState = sourceRunId ? get().run_attempts[sourceRunId] : undefined;
      const activeCandidate = attemptState?.candidates.find(
        (candidate) => candidate.candidate_id === sourceCandidateId,
      );
      if (
        !sourceRunId
        || !sourceCandidateId
        || !attemptState
        || attemptState.loading
        || Boolean(attemptState.error)
        || !activeCandidate
      ) {
        throw new Error("Load the exact attempt before publishing this draft.");
      }
      const lineage = captureDirectCandidatePublicationLineage(
        conversationId,
        sourceRunId,
        sourceCandidateId,
      );
      const sourceRunStaysLive = sourceRunOwnsOrdinarySlot(
        conversation,
        sourceRunId,
      );
      const operationConversationId = sourceRunStaysLive
        ? candidatePublishOperationId(conversationId)
        : conversationId;
      const controller = new AbortController();
      let resolveSettlement!: (result: CandidatePublicationCancellation) => void;
      const publicationOwner: CandidatePublicationOwner = {
        kind: "candidate_publish",
        token: Symbol("candidate_publish"),
        ownerScope: currentDemoUserScope(),
        operationConversationId,
        controller,
        cancelRequested: false,
        flowComplete: false,
        settlement: new Promise<CandidatePublicationCancellation>((resolve) => {
          resolveSettlement = resolve;
        }),
        resolveSettlement: (result) => resolveSettlement(result),
      };
      const releaseDerivedRun = acquireDerivedRunOperation(
        conversationId,
        "candidate_publish",
        publicationOwner,
      );
      let activeRunId: string | undefined;
      let published: Artifact;
      const placeholderId = nextId("msg");
      const taskPayload: MessageTaskPayload = {
        artifact_type: artifactTypeForArtifact(active),
        source_artifact_id: active.artifact_id,
        source_run_id: active.attempt_lineage?.source_run_id,
        source_candidate_id: active.attempt_lineage?.source_candidate_id,
      };
      const publicationLineageIsCurrent = () => (
        publicationOwner.ownerScope === currentDemoUserScope()
        && directCandidatePublicationLineageIsCurrent(lineage)
      );
      try {
        const { ack, reconcileImmediately, startReplay } = await resolveReservedRunStart({
          request: publishCandidateDraft(
            active.artifact_id,
            conversationId,
            controller.signal,
            (runId) => {
            if (_ACTIVE_DERIVED_RUN_CONVERSATIONS.get(conversationId) !== publicationOwner) {
              throw new RunWaitCancelledError();
            }
            activeRunId = runId;
            publicationOwner.runId = runId;
            if (publicationOwner.cancelRequested) {
              throw new RunWaitCancelledError();
            }
            _RESERVED_RUN_UPLOAD_ABORTS.set(operationConversationId, { runId, controller });
            patchConversation(conversationId, (current) => {
              const placeholder: Message = {
                id: placeholderId,
                role: "assistant",
                text: "Publishing selected attempt.",
                ts: Date.now(),
                run_id: runId,
                artifact_id: active.artifact_id,
                status: "streaming",
                task_type: CANDIDATE_PUBLISH_TASK,
                task_payload: taskPayload,
                source_artifact_id: active.artifact_id,
              };
              const hasPlaceholder = current.messages.some(
                (message) => message.id === placeholderId,
              );
              return {
                ...current,
                ...(!sourceRunStaysLive ? { pending: true, run_id: runId } : {}),
                messages: hasPlaceholder
                  ? current.messages.map((message) => (
                      message.id === placeholderId ? { ...message, ...placeholder } : message
                    ))
                  : [...current.messages, placeholder],
              };
            });
            set((current) => ({
              runs_progress: {
                ...current.runs_progress,
                [operationConversationId]: initialProgress(runId, "attempt_publish"),
                },
              }));
            },
          ),
          reservedRunId: () => activeRunId,
          isCurrent: (runId) => (
            _ACTIVE_DERIVED_RUN_CONVERSATIONS.get(conversationId) === publicationOwner
            && publicationOwner.runId === runId
            && !publicationOwner.cancelRequested
            && publicationLineageIsCurrent()
          ),
          placeholderMessage: {
            id: placeholderId,
            role: "assistant",
            text: "Publishing selected attempt.",
            ts: Date.now(),
            artifact_id: active.artifact_id,
            status: "streaming",
            task_type: CANDIDATE_PUBLISH_TASK,
            task_payload: taskPayload,
            source_artifact_id: active.artifact_id,
          },
          progressMode: "attempt_publish",
        });
        if (activeRunId && ack.run_id !== activeRunId) {
          throw new Error("Candidate publication start returned a different run.");
        }
        activeRunId = ack.run_id;
        const stillOwnsPublish = () => ownsCandidatePublishMessage(
          get().conversations[conversationId],
          placeholderId,
          ack.run_id,
        );
        const result = await runGenerateAckFlow({
          convId: operationConversationId,
          placeholderId,
          ack,
          timeoutMs: 10 * 60 * 1000,
          timeoutMessage: "attempt publish still running after 10 min",
          closedMessage: "attempt publish event stream closed",
          task_type: CANDIDATE_PUBLISH_TASK,
          task_payload: taskPayload,
          source_artifact_id: active.artifact_id,
          activateArtifact: false,
          reconcileImmediately,
          startReplay,
          shouldApplyResult: sourceRunStaysLive
            ? () => stillOwnsPublish() && publicationLineageIsCurrent()
            : publicationLineageIsCurrent,
          validateResult: (result) => (
            result.message.run_id === ack.run_id
            && stillOwnsPublish()
          ),
        });
        if (publicationOwner.cancelRequested) {
          throw new RunWaitCancelledError();
        }
        if (sourceRunStaysLive && !stillOwnsPublish()) {
          throw new RunWaitCancelledError();
        }
        if (!result.artifact) {
          throw new Error("Attempt publish did not produce a published artifact.");
        }
        published = result.artifact;
      } catch (error) {
        if (publicationOwner.cancelRequested) {
          let cancellation = publicationOwner.settlementResult;
          if (!cancellation && !publicationOwner.cancellationRequest) {
            cancellation = activeRunId
              ? await requestOwnedCandidatePublicationCancellation(
                publicationOwner,
                conversationId,
                publicationOwner.ownerScope,
                activeCandidatePublishMessage(get().conversations[conversationId]),
              )
              : { attempted: true, confirmed: true };
            settleCandidatePublicationOwner(publicationOwner, cancellation);
          }
          return;
        }
        const cancellation = runCancellationDisposition(
          operationConversationId,
          activeRunId,
          error,
        );
        if (cancellation) return;
        patchConversation(conversationId, (current) => (
          {
            ...current,
            ...(
              !sourceRunStaysLive
              && (!activeRunId || current.run_id === activeRunId)
                ? { pending: false, run_id: undefined }
                : {}
            ),
            messages: current.messages.map((message) => (
              message.id === placeholderId
                ? {
                    ...message,
                    text: error instanceof Error
                      ? error.message
                      : "Attempt publish failed.",
                    status: "error",
                    failure: runClientFailure(
                      error,
                      artifactTypeForArtifact(active),
                      activeRunId,
                    ),
                  }
                : message
            )),
          }
        ));
        clearRunProgress(operationConversationId, activeRunId);
        throw error;
      } finally {
        publicationOwner.flowComplete = true;
        if (
          !publicationOwner.cancelRequested
          || publicationOwner.settlementResult?.confirmed === true
        ) {
          cleanupReservedRunOwner(operationConversationId, activeRunId);
          releaseDerivedRun();
        }
      }
      if (!publicationLineageIsCurrent()) {
        if (publicationOwner.ownerScope === currentDemoUserScope()) {
          patchConversation(conversationId, (current) => ({
            ...current,
            ...(current.run_id === activeRunId
              ? { pending: false, run_id: undefined }
              : {}),
            messages: current.messages.filter((message) => !(
              message.id === placeholderId && message.run_id === activeRunId
            )),
          }));
          clearRunProgress(operationConversationId, activeRunId);
        }
        return;
      }
      applyPublishedCandidate(
        conversationId,
        active.artifact_id,
        published,
        placeholderId,
        sourceRunStaysLive,
      );
      if (lineage.parentConversationId && lineage.parentJobId) {
        await get().recoverPaperBundles();
      }
    },

    loadServerHistory: () => {
      if (_SERVER_HISTORY_LOAD) return _SERVER_HISTORY_LOAD;
      const load = (async () => {
        const requestStart = new Map(
          Object.entries(get().conversations).map(([conversationId, conversation]) => [
            conversationId,
            {
              conversation,
              updatedAt: conversation.updated_at,
              pending: conversation.pending,
              runId: conversation.run_id,
            },
          ]),
        );
        let recoverAfterRequest = true;
        try {
          const history = await fetchServerHistory({
            limit: SERVER_HISTORY_IMPORT_LIMIT,
          });
          const incoming = Object.values(history.conversations)
            .map(conversationFromServerHistorySummary);
          const incomingConversationIds = new Set(incoming.map((conversation) => conversation.id));
          const currentScope = currentDemoUserScope();
          if (history.user_isolated && history.request_scope !== currentScope) {
            recoverAfterRequest = false;
            set((s) => {
              const conversations = Object.fromEntries(
                Object.entries(s.conversations).filter(([conversationId, conversation]) => (
                  !requestStart.has(conversationId)
                  && conversation.paper_bundle?.kind !== "child"
                )),
              );
              const visible = Object.values(conversations)
                .sort((a, b) => b.updated_at - a.updated_at);
              if (visible.length === 0) {
                const fresh = freshConversation();
                conversations[fresh.id] = fresh;
                visible.push(fresh);
              }
              const current = conversations[s.current_conversation_id]
                ? s.current_conversation_id
                : visible[0].id;
              return {
                conversations,
                current_conversation_id: current,
                history_user_scope: currentScope,
                editor_history: {},
                selected_layer_id: null,
                selected_layer_ids: [],
              };
            });
            const current = get().conversations[get().current_conversation_id];
            if (current?.history_summary) {
              void get().hydrateServerHistoryConversation(current.id);
            }
            return;
          }
          set((s) => {
            const scopeChanged = history.user_isolated
              && s.history_user_scope !== history.request_scope;
            const preservedLocalIds = history.user_isolated
              ? new Set(
                  Object.entries(s.conversations)
                    .filter(([conversationId, conversation]) => {
                      const start = requestStart.get(conversationId);
                      return (!start && (
                        !scopeChanged || conversation.paper_bundle?.kind !== "child"
                      )) || (!scopeChanged && !!start && (
                        start.conversation !== conversation
                        || start.updatedAt !== conversation.updated_at
                        || start.pending !== conversation.pending
                        || start.runId !== conversation.run_id
                        || (
                          hasRehydratedOrphanedPptxExport(conversation)
                          && !incomingConversationIds.has(conversationId)
                        )
                        || (
                          activeCandidatePublishMessage(conversation)
                          && !incomingConversationIds.has(conversationId)
                        )
                        || (
                          conversation.paper_bundle?.kind === "child"
                          && Object.values(conversation.artifacts).some(
                            (artifact) => artifact.candidate_draft,
                          )
                        )
                      ));
                    })
                    .map(([conversationId]) => conversationId),
                )
              : new Set<string>();
            const localForMerge = scopeChanged
              ? Object.fromEntries(
                  Object.entries(s.conversations)
                    .filter(([conversationId]) => preservedLocalIds.has(conversationId)),
                )
              : s.conversations;
            const serverArtifacts = buildServerArtifactIndex(incoming);
            const hydratedLocal = hydrateLocalArtifactsFromServer(
              localForMerge,
              serverArtifacts,
              preservedLocalIds,
            );
            const conversations = mergeServerHistoryConversations(
              hydratedLocal,
              incoming,
              history.user_isolated,
              preservedLocalIds,
            );
            let visibleConversations = Object.values(conversations)
              .filter((conversation) => conversation.paper_bundle?.kind !== "child")
              .sort((a, b) => b.updated_at - a.updated_at);
            if (visibleConversations.length === 0) {
              const fresh = freshConversation();
              conversations[fresh.id] = fresh;
              visibleConversations = [fresh];
            }
            const currentConversation = conversations[s.current_conversation_id];
            const currentArtifact = currentConversation?.active_artifact_id
              ? currentConversation.artifacts[currentConversation.active_artifact_id]
              : undefined;
            const preservesCanvasDraft = Boolean(
              s.mode === "canvas"
              && currentConversation?.paper_bundle?.kind === "child"
              && currentArtifact?.candidate_draft,
            );
            const current = currentConversation
              && (
                currentConversation.paper_bundle?.kind !== "child"
                || preservesCanvasDraft
              )
              ? s.current_conversation_id
              : visibleConversations[0].id;
            return {
              conversations: validatePosterCanvasSelections(
                validatePosterPaletteSelections(
                  conversations,
                  s.poster_palettes_status,
                  s.poster_palettes,
                ),
                s.poster_canvas_presets_status,
                s.poster_canvas_presets,
              ),
              current_conversation_id: current,
              ...(history.user_isolated
                ? {
                    history_user_scope: history.request_scope,
                    editor_history: {},
                    selected_layer_id: null,
                    selected_layer_ids: [],
                  }
                : {}),
            };
          });
          const hydrationIds = new Set<string>();
          const current = get().conversations[get().current_conversation_id];
          if (current?.history_summary) hydrationIds.add(current.id);
          for (const conversation of Object.values(get().conversations)) {
            if (
              conversation.history_summary
              && (conversation.pending || conversation.paper_bundle?.kind === "child")
            ) {
              hydrationIds.add(conversation.id);
            }
          }
          for (const conversationId of hydrationIds) {
            void get().hydrateServerHistoryConversation(conversationId);
          }
        } catch {
          // Server history is a cross-browser convenience. Keep the local
          // cache usable if the backend is down or still reloading.
        } finally {
          if (recoverAfterRequest) {
            await get().recoverPaperBundles();
            get().recoverActiveRuns();
          }
        }
      })();
      _SERVER_HISTORY_LOAD = load;
      void load.finally(() => {
        if (_SERVER_HISTORY_LOAD === load) _SERVER_HISTORY_LOAD = null;
      }).catch(() => undefined);
      return load;
    },

    hydrateServerHistoryConversation: (id) => {
      const summary = get().conversations[id];
      if (!summary?.history_summary) return Promise.resolve();
      const existing = _SERVER_HISTORY_DETAIL_LOADS.get(id);
      if (existing) return existing;
      const load = (async () => {
        try {
          const response = await fetchServerHistoryConversation(summary.history_source_id ?? id);
          if (response.user_isolated && response.request_scope !== currentDemoUserScope()) {
            return;
          }
          const conversation = normalizeConversation(response.conversation);
          if (!conversation) return;
          set((s) => {
            const current = s.conversations[id];
            if (!current?.history_summary) return s;
            const remote = {
              ...conversation,
              history_last_run: conversation.history_last_run ?? current.history_last_run,
            };
            return {
              conversations: mergeServerHistoryConversations(
                s.conversations,
                [remote],
                false,
              ),
            };
          });
        } catch {
          // Keep a summary row usable while a local backend is restarting.
        }
      })();
      _SERVER_HISTORY_DETAIL_LOADS.set(id, load);
      void load.finally(() => {
        if (_SERVER_HISTORY_DETAIL_LOADS.get(id) === load) {
          _SERVER_HISTORY_DETAIL_LOADS.delete(id);
        }
      }).catch(() => undefined);
      return load;
    },

    recoverActiveRuns: () => {
      for (const conversation of Object.values(get().conversations)) {
        const streamingMessage = activeCandidatePublishMessage(conversation);
        const runId = streamingMessage?.run_id;
        const artifactType = streamingMessage?.task_payload?.artifact_type;
        const sourceArtifactId = streamingMessage?.task_payload?.source_artifact_id
          ?? streamingMessage?.source_artifact_id;
        const sourceRunId = streamingMessage?.task_payload?.source_run_id;
        const sourceCandidateId = streamingMessage?.task_payload?.source_candidate_id;
        if (
          !streamingMessage
          || !runId
          || !artifactType
          || !ARTIFACT_TYPES.has(artifactType)
          || (
            !sourceArtifactId
            && (!sourceRunId || !sourceCandidateId)
          )
        ) continue;
	        const directLineage = !sourceArtifactId && sourceRunId && sourceCandidateId
	          ? captureDirectCandidatePublicationLineage(
	            conversation.id,
	            sourceRunId,
	            sourceCandidateId,
	          )
	          : undefined;
	        const operationConversationId = candidatePublishOperationFor(
	          conversation.id,
	          streamingMessage,
	        );
        const sourceRunStaysLive = operationConversationId !== conversation.id;
        const recoveryKey = `${operationConversationId}:${runId}`;
        if (_SSE_WAIT_ABORTS.get(operationConversationId)?.runId === runId) continue;
        if (_RESERVED_RUN_UPLOAD_ABORTS.get(operationConversationId)?.runId === runId) continue;
        if (_ACTIVE_RUN_RECOVERIES.has(recoveryKey)) continue;
        const stillOwnsPublish = () => ownsCandidatePublishMessage(
          get().conversations[conversation.id],
          streamingMessage.id,
          runId,
        );
        const publicationIsCurrent = () => candidatePublishMessageMatches(
          get().conversations[conversation.id],
          streamingMessage.id,
          runId,
        )
          && (!directLineage || directCandidatePublicationLineageIsCurrent(directLineage));
        const recovery = (async () => {
          try {
            const latestConversation = get().conversations[conversation.id];
            const latestParent = latestConversation?.paper_bundle?.kind === "child"
              ? get().conversations[latestConversation.paper_bundle.parent_conversation_id]
              : undefined;
            const latestTask = latestParent?.paper_bundle?.kind === "parent"
              && latestConversation?.paper_bundle?.kind === "child"
              ? latestParent.paper_bundle.tasks[latestConversation.paper_bundle.artifact_type]
              : undefined;
            const publicationCancelling = sourceRunStaysLive
              && latestTask?.status === "cancelling";
            const flow = runGenerateAckFlow({
              convId: operationConversationId,
              placeholderId: streamingMessage.id,
              ack: {
                run_id: runId,
                progress_mode: "attempt_publish",
                placeholder_message: streamingMessage,
              },
              timeoutMs: 60 * 60 * 1000,
              timeoutMessage: "attempt publish recovery still running after 60 min",
              closedMessage: "attempt publish recovery event stream closed",
              task_type: CANDIDATE_PUBLISH_TASK,
              task_payload: streamingMessage.task_payload,
              source_artifact_id: sourceArtifactId,
              activateArtifact: false,
              recoverExisting: !publicationCancelling,
              shouldApplyResult: publicationIsCurrent,
              validateResult: (result) => (
                result.message.run_id === runId
                && publicationIsCurrent()
                && (
                  !directLineage
                  || (
                    result.artifact?.attempt_lineage?.source_run_id
                      === directLineage.sourceRunId
                    && result.artifact?.attempt_lineage?.source_candidate_id
                      === directLineage.sourceCandidateId
                  )
                )
              ),
            });
            if (publicationCancelling) {
              markRunProgressCancelling(
                operationConversationId,
                runId,
                "Stopping publication…",
              );
            }
            const result = await flow;
            if (sourceRunStaysLive && !stillOwnsPublish()) return;
            if (!result.artifact) {
              throw new Error("Recovered candidate publish did not produce an artifact.");
            }
            if (sourceArtifactId) {
              applyPublishedCandidate(
                conversation.id,
                sourceArtifactId,
                result.artifact,
                streamingMessage.id,
                sourceRunStaysLive,
              );
            } else if (directLineage) {
              applyDirectPublishedAttempt(
                directLineage,
                runId,
                streamingMessage.id,
                result.artifact,
                sourceRunStaysLive,
              );
            }
          } catch (error) {
            if (error instanceof RunWaitCancelledError) return;
            const firstError = primaryRunClientError(error);
            const detail = firstError instanceof Error
              ? firstError.message
              : "Attempt publish recovery failed.";
            patchConversation(conversation.id, (current) => {
              if (!ownsCandidatePublishMessage(current, streamingMessage.id, runId)) {
                return current;
              }
              return {
                ...current,
                ...(current.run_id === runId ? { pending: false, run_id: undefined } : {}),
                messages: current.messages.map((message) => (
                  message.id === streamingMessage.id
                    ? {
                        ...message,
                        text: detail,
                        status: "error",
                        failure: runClientFailure(firstError, artifactType, runId),
                      }
                    : message
                )),
              };
            });
            clearRunProgress(operationConversationId, runId);
          } finally {
            cleanupReservedRunOwner(operationConversationId, runId);
          }
        })();
        _ACTIVE_RUN_RECOVERIES.set(recoveryKey, recovery);
        void recovery.finally(() => {
          if (_ACTIVE_RUN_RECOVERIES.get(recoveryKey) === recovery) {
            _ACTIVE_RUN_RECOVERIES.delete(recoveryKey);
          }
        });
      }

      for (const conversation of Object.values(get().conversations)) {
        if (
          !conversation.pending
          || !conversation.run_id
          || conversation.paper_bundle
        ) continue;
        const runId = conversation.run_id;
        const recoveryKey = `${conversation.id}:${runId}`;
        if (_SSE_WAIT_ABORTS.get(conversation.id)?.runId === runId) continue;
        if (_RESERVED_RUN_UPLOAD_ABORTS.get(conversation.id)?.runId === runId) continue;
        if (_ACTIVE_RUN_RECOVERIES.has(recoveryKey)) continue;
	        const streamingMessages = [...conversation.messages].reverse().filter((message) => (
	          message.role === "assistant"
	          && message.status === "streaming"
	          && message.task_type !== CANDIDATE_PUBLISH_TASK
	        ));
	        const streamingMessage = streamingMessages.find(
	          (message) => message.run_id === runId,
	        ) ?? streamingMessages.find((message) => !message.run_id);
        const artifactType = streamingMessage?.task_payload?.artifact_type;
        const isPptxExportRecovery = streamingMessage?.task_type === PPTX_EXPORT_TASK;
        if (
          !isPptxExportRecovery
          && (!artifactType || !ARTIFACT_TYPES.has(artifactType))
        ) continue;
        const sourceArtifactId = streamingMessage?.source_artifact_id
          ?? streamingMessage?.task_payload?.source_artifact_id;
        const placeholderId = streamingMessage?.id ?? nextId("msg");
        if (!streamingMessage) {
          patchConversation(conversation.id, (current) => ({
            ...current,
            messages: [...current.messages, {
              id: placeholderId,
              role: "assistant",
              text: "",
              ts: Date.now(),
              run_id: runId,
              status: "streaming",
              task_type: GENERATE_TASK,
              task_payload: { artifact_type: artifactType },
            }],
          }));
        }
        const recovery = (async () => {
          try {
            const result = await runGenerateAckFlow({
              convId: conversation.id,
              placeholderId,
              ack: {
                run_id: runId,
                placeholder_message: streamingMessage ?? {
                  id: placeholderId,
                  role: "assistant",
                  text: "",
                  ts: Date.now(),
                },
              },
              timeoutMs: isPptxExportRecovery ? 30 * 60 * 1000 : 60 * 60 * 1000,
              timeoutMessage: isPptxExportRecovery
                ? "PowerPoint export recovery still running after 30 min"
                : "run recovery still running after 60 min",
              closedMessage: isPptxExportRecovery
                ? "PowerPoint export recovery event stream closed"
                : "run recovery event stream closed",
              task_type: isPptxExportRecovery ? PPTX_EXPORT_TASK : GENERATE_TASK,
              task_payload: streamingMessage?.task_payload ?? { artifact_type: artifactType },
              source_artifact_id: sourceArtifactId,
              activateArtifact: !isPptxExportRecovery,
              recoverExisting: true,
            });
            if (isPptxExportRecovery && result.message.download_url) {
              const source = sourceArtifactId
                ? get().conversations[conversation.id]?.artifacts[sourceArtifactId]
                : undefined;
              triggerStoreDownload(
                result.message.download_url,
                result.message.download_filename || `${source?.name ?? "artifact"}.pptx`,
              );
              if (sourceArtifactId) {
                emitArtifactEvent(
                  conversation.id,
                  "artifact.downloaded",
                  sourceArtifactId,
                  { format: "pptx", run_id: runId, resumed: true },
                );
              }
            }
          } catch (error) {
            const firstError = primaryRunClientError(error);
            const detail = firstError instanceof Error
              ? firstError.message
              : "Run recovery failed.";
            patchConversation(conversation.id, (current) => {
              if (current.run_id !== runId) return current;
              return {
                ...current,
                pending: false,
                run_id: undefined,
                messages: current.messages.map((message) => (
                  message.id === placeholderId
                    ? {
                        ...message,
                        run_id: runId,
                        text: detail,
                        status: "error",
                        failure: runClientFailure(firstError, artifactType, runId),
                      }
                    : message
                )),
              };
            });
            clearRunProgress(conversation.id, runId);
          }
        })();
        _ACTIVE_RUN_RECOVERIES.set(recoveryKey, recovery);
        void recovery.finally(() => {
          if (_ACTIVE_RUN_RECOVERIES.get(recoveryKey) === recovery) {
            _ACTIVE_RUN_RECOVERIES.delete(recoveryKey);
          }
        });
      }
    },

    recoverPaperBundles: async () => {
      const requestScope = currentDemoUserScope();
      const backendOwnerId = paperBundleBackendOwnerId(requestScope, get().backend_info);
      let publicationHydrations: Promise<void>[] = [];
      try {
        const jobs = await listPaperBundles();
        if (currentDemoUserScope() !== requestScope) return;
        set((state) => {
          if (currentDemoUserScope() !== requestScope) return state;
          const conversations = { ...state.conversations };
          for (const job of jobs) {
            if (job.owner_id !== backendOwnerId) continue;
            const existingParent = conversations[job.conversation_id];
            const currentBundle = existingParent?.paper_bundle?.kind === "parent"
              ? existingParent.paper_bundle
              : undefined;
            if (
              (currentBundle?.job_id && currentBundle.job_id !== job.job_id)
              || (
                currentBundle?.job_id === job.job_id
                && (
                  (
                    currentBundle.revision !== undefined
                    && job.revision < currentBundle.revision
                  )
                  || (
                    isTerminalPaperBundleBackendState(currentBundle.backend_state)
                    && !job.terminal
                  )
                )
              )
            ) {
              continue;
            }
            const existingBundle = existingParent?.paper_bundle?.kind === "parent"
              && existingParent.paper_bundle.job_id === job.job_id
              ? existingParent.paper_bundle
              : undefined;
            const startedAt = Math.round(job.created_at * 1000);
            const updatedAt = Math.round(job.updated_at * 1000);
            const initialBundle = createPaperBundleParentState(
              job.conversation_id,
              job.source_name,
              startedAt,
            );
            const tasks: Record<ArtifactType, PaperBundleTask> = {} as Record<ArtifactType, PaperBundleTask>;
            for (const artifactType of PAPER_BUNDLE_ARTIFACT_ORDER) {
              const descriptor = job.children[artifactType];
              const publication = job.publications[artifactType];
              const previous = existingBundle?.tasks[artifactType]
                ?? initialBundle.tasks[artifactType];
              const childConversationId = descriptor.conversation_id;
              const existingChild = conversations[childConversationId]
                ?? conversations[previous.child_conversation_id];
              const previousArtifact = previous.artifact_id
                ? existingParent?.artifacts[previous.artifact_id]
                  ?? existingChild?.artifacts[previous.artifact_id]
                : undefined;
              const preservedEditedArtifact = publication
                && paperBundleEditedArtifactDescendsFromPublication(
                  previousArtifact,
                  artifactType,
                  publication,
                )
                ? previousArtifact
                : undefined;
              const status = publication
                ? "complete"
                : paperBundleTaskStatusFromBackend(descriptor.state);
              const interruptedUpload = status === "uploading"
                && !(
                  _PAPER_BUNDLE_UPLOADS.get(childConversationId)?.runId
                  === descriptor.run_id
                );
              const artifactId = preservedEditedArtifact
                ? preservedEditedArtifact.artifact_id
                : publication?.artifact_id
                ?? previous.artifact_id
                ?? (status === "complete" ? existingChild?.active_artifact_id ?? undefined : undefined);
              const terminal = publication ? true : descriptor.terminal;
              const processFree = publication ? true : descriptor.process_free;
              const publicationFinishedAt = publication
                ? Math.round(publication.published_at * 1000)
                : undefined;
              tasks[artifactType] = {
                ...previous,
                artifact_type: artifactType,
                child_conversation_id: childConversationId,
                status,
                run_id: preservedEditedArtifact
                  ? previous.run_id ?? runIdFromArtifactId(preservedEditedArtifact.artifact_id)
                  : publication?.publication_run_id ?? descriptor.run_id,
                ...(publication
                  ? { authoring_run_id: publication.source_run_id }
                  : {}),
                terminal,
                process_free: processFree,
                ...(artifactId ? { artifact_id: artifactId } : {}),
                ...(interruptedUpload
                  ? {
                      error: "Upload was interrupted before the run started. Cancel this bundle or start again in a new conversation.",
                    }
                  : status === "cancelling"
                    ? { error: "Cancellation is still pending on the backend." }
                    : status === "failed"
                      ? { error: previous.error || "Run failed." }
                      : { error: undefined }),
                ...(terminal
                  ? {
                      ...terminalPaperBundleTaskStats(
                        previous,
                        undefined,
                        publicationFinishedAt ?? updatedAt,
                      ),
                      ...(publicationFinishedAt !== undefined
                        ? {
                            finished_at: Math.max(
                              previous.finished_at ?? 0,
                              publicationFinishedAt,
                            ),
                          }
                        : {}),
                    }
                  : {}),
              };
            }
            const bundle: Extract<PaperBundleState, { kind: "parent" }> = {
              ...initialBundle,
              ...existingBundle,
              job_id: job.job_id,
              revision: job.revision,
              backend_state: job.state,
              cancel_error: job.state === "cancelling"
                ? existingBundle?.cancel_error
                  || "Cancellation not confirmed; backend may still be stopping."
                : undefined,
              cancel_request_in_flight: job.state === "cancelling"
                && existingBundle?.cancel_request_in_flight === true,
              tasks: tasks as PaperBundleTaskMap,
            };
            const parent: Conversation = existingParent
              ? {
                  ...existingParent,
                  title: existingParent.title || job.source_name,
                  updated_at: Math.max(existingParent.updated_at, updatedAt),
                  paper_bundle: bundle,
                  pending: paperBundleHasActiveTasks(bundle),
                }
              : {
                  id: job.conversation_id,
                  title: job.source_name,
                  created_at: startedAt,
                  updated_at: updatedAt,
                  messages: [],
                  artifacts: {},
                  active_artifact_id: null,
                  paper_bundle: bundle,
                  pending: paperBundleHasActiveTasks(bundle),
                };
            const parentArtifacts = { ...parent.artifacts };
            for (const artifactType of PAPER_BUNDLE_ARTIFACT_ORDER) {
              const task = bundle.tasks[artifactType];
              const child = conversations[task.child_conversation_id];
              if (task.artifact_id && child?.artifacts[task.artifact_id]) {
                parentArtifacts[task.artifact_id] = child.artifacts[task.artifact_id];
              }
            }
            conversations[job.conversation_id] = {
              ...parent,
              artifacts: parentArtifacts,
            };
            for (const artifactType of PAPER_BUNDLE_ARTIFACT_ORDER) {
              const task = bundle.tasks[artifactType];
              const existingChild = conversations[task.child_conversation_id];
              const child = existingChild
                ?? conversationForRecoveredBundleTask(
                  conversations[job.conversation_id],
                  artifactType,
                );
              const hasSourceStreamingPlaceholder = child.messages.some((message) => (
                message.role === "assistant"
                && message.status === "streaming"
                && message.task_type !== CANDIDATE_PUBLISH_TASK
                && (!message.run_id || message.run_id === task.run_id)
              ));
              conversations[task.child_conversation_id] = {
                ...child,
                paper_bundle: createPaperBundleChildState(job.conversation_id, artifactType),
                pending: isActivePaperBundleTaskStatus(task.status),
                run_id: isActivePaperBundleTaskStatus(task.status) ? task.run_id : undefined,
                messages: hasSourceStreamingPlaceholder || !isActivePaperBundleTaskStatus(task.status)
                  ? child.messages
                  : [
                      ...child.messages,
                      {
                        id: nextId("msg"),
                        role: "assistant",
                        text: "",
                        ts: updatedAt,
                        status: "streaming",
                        task_type: GENERATE_TASK,
                        task_payload: { artifact_type: artifactType },
                      },
                    ],
              };
            }
          }
          return { conversations };
        });
        publicationHydrations = jobs.flatMap((job) => (
          PAPER_BUNDLE_ARTIFACT_ORDER.flatMap((artifactType) => {
            const publication = job.publications[artifactType];
            if (!publication || job.owner_id !== backendOwnerId) return [];
            const hydrationKey = [
              requestScope,
              job.job_id,
              artifactType,
              publication.source_run_id,
              publication.publication_run_id,
              publication.artifact_id,
              publication.source_attempt,
              publication.source_candidate_id,
              publication.source_candidate_sha256,
              publication.generation,
            ].join("|");
            const existingHydration = _PAPER_BUNDLE_PUBLICATION_HYDRATIONS.get(
              hydrationKey,
            );
            if (existingHydration) return [existingHydration];
            const hydration = (async () => {
              const publicationStillCurrent = () => {
                if (currentDemoUserScope() !== requestScope) return false;
                const parent = get().conversations[job.conversation_id];
                if (
                  parent?.paper_bundle?.kind !== "parent"
                  || parent.paper_bundle.job_id !== job.job_id
                ) {
                  return false;
                }
                const task = parent.paper_bundle.tasks[artifactType];
                return task.status === "complete"
                  && task.run_id === publication.publication_run_id
                  && task.authoring_run_id === publication.source_run_id
                  && task.artifact_id === publication.artifact_id;
              };
              if (!publicationStillCurrent()) return;
              const parent = get().conversations[job.conversation_id];
              const task = parent.paper_bundle?.kind === "parent"
                ? parent.paper_bundle.tasks[artifactType]
                : undefined;
              if (!task) return;
              const child = get().conversations[task.child_conversation_id];
              if (
                paperBundlePublicationArtifactMatches(
                  parent.artifacts[publication.artifact_id],
                  artifactType,
                  publication,
                )
                && paperBundlePublicationArtifactMatches(
                  child?.artifacts[publication.artifact_id],
                  artifactType,
                  publication,
                )
              ) {
                return;
              }
              let result: GenerateResponse;
              try {
                result = await fetchPaperBundleRunArtifact(
                  task.child_conversation_id,
                  publication.publication_run_id,
                  { terminalSettledAt: Date.now() },
                  false,
                );
              } catch {
                return;
              }
              const artifact = validatedPaperBundlePublicationArtifact(
                result,
                artifactType,
                publication,
              );
              if (!artifact || !publicationStillCurrent()) return;
              set((state) => {
                if (currentDemoUserScope() !== requestScope) return state;
                const currentParent = state.conversations[job.conversation_id];
                if (
                  currentParent?.paper_bundle?.kind !== "parent"
                  || currentParent.paper_bundle.job_id !== job.job_id
                ) {
                  return state;
                }
                const currentTask = currentParent.paper_bundle.tasks[artifactType];
                if (
                  currentTask.status !== "complete"
                  || currentTask.run_id !== publication.publication_run_id
                  || currentTask.authoring_run_id !== publication.source_run_id
                  || currentTask.artifact_id !== publication.artifact_id
                ) {
                  return state;
                }
                const currentChild = state.conversations[currentTask.child_conversation_id];
                if (!currentChild) return state;
                const updatedChild: Conversation = {
                  ...currentChild,
                  artifacts: {
                    ...currentChild.artifacts,
                    [artifact.artifact_id]: artifact,
                  },
                  active_artifact_id: artifact.artifact_id,
                  published_artifact_id: artifact.artifact_id,
                  updated_at: Math.max(currentChild.updated_at, Math.round(job.updated_at * 1000)),
                };
                const currentParentActive = currentParent.active_artifact_id
                  && currentParent.artifacts[currentParent.active_artifact_id]
                  ? currentParent.active_artifact_id
                  : artifact.artifact_id;
                return {
                  conversations: {
                    ...state.conversations,
                    [currentTask.child_conversation_id]: updatedChild,
                    [job.conversation_id]: {
                      ...currentParent,
                      artifacts: {
                        ...currentParent.artifacts,
                        [artifact.artifact_id]: artifact,
                      },
                      active_artifact_id: currentParentActive,
                    },
                  },
                };
              });
            })();
            _PAPER_BUNDLE_PUBLICATION_HYDRATIONS.set(hydrationKey, hydration);
            void hydration.finally(() => {
              if (_PAPER_BUNDLE_PUBLICATION_HYDRATIONS.get(hydrationKey) === hydration) {
                _PAPER_BUNDLE_PUBLICATION_HYDRATIONS.delete(hydrationKey);
              }
            }).catch(() => undefined);
            return [hydration];
          })
        ));
      } catch {
        // Keep local legacy bundles recoverable while the backend is unavailable.
      }
      set((state) => ({
        conversations: reconcilePaperBundleGraph(state.conversations),
      }));
      for (const parent of Object.values(get().conversations)) {
        const bundle = parent.paper_bundle;
        if (bundle?.kind !== "parent") continue;
        for (const artifactType of PAPER_BUNDLE_ARTIFACT_ORDER) {
          const task = bundle.tasks[artifactType];
          if (bundle.job_id && !isActivePaperBundleTaskStatus(task.status)) continue;
          if (!bundle.job_id && task.status !== "pending" && task.status !== "running") continue;
          const childId = task.child_conversation_id;
          const runId = task.run_id;
          if (!runId) {
            if (_PAPER_BUNDLE_UPLOADS.has(childId)) continue;
            const detail = "Interrupted before the server acknowledged the upload.";
            patchConversation(childId, (child) => ({
              ...child,
              pending: false,
              run_id: undefined,
              messages: child.messages.map((message) =>
                message.role === "assistant" && message.status === "streaming"
                  ? {
                      ...message,
                      text: detail,
                      status: "error",
                      failure: connectionLostFailure(detail, artifactType),
                    }
                  : message
              ),
            }));
            patchPaperBundleTask(parent.id, artifactType, (current) =>
              current.run_id || current.status === "complete"
                ? current
                : {
                    ...current,
                    ...terminalPaperBundleTaskStats(current),
                    status: "failed",
                    error: detail,
                  }
            );
            continue;
          }
          if (_SSE_WAIT_ABORTS.get(childId)?.runId === runId) continue;
          const recoveryKey = `${childId}:${runId}`;
          if (_PAPER_BUNDLE_RECOVERIES.has(recoveryKey)) continue;
          const recovery = (async () => {
            let terminalProgress: RunProgress | undefined;
            const currentChild = get().conversations[childId];
            const existingPlaceholder = [...(currentChild?.messages ?? [])]
              .reverse()
              .find((message) =>
                message.role === "assistant"
                && message.status === "streaming"
                && message.task_type !== CANDIDATE_PUBLISH_TASK
                && (!message.run_id || message.run_id === runId)
              );
            const placeholderId = existingPlaceholder?.id ?? nextId("msg");
            if (!existingPlaceholder) {
              patchConversation(childId, (child) => ({
                ...child,
                messages: [
                  ...child.messages,
                  {
                    id: placeholderId,
                    role: "assistant",
                    text: "",
                    ts: Date.now(),
                    status: "streaming",
                    task_type: GENERATE_TASK,
                    task_payload: { artifact_type: artifactType },
                  },
                ],
              }));
            }
            try {
              const flow = runGenerateAckFlow({
                convId: childId,
                placeholderId,
                ack: { run_id: runId, placeholder_message: existingPlaceholder ?? {
                  id: placeholderId,
                  role: "assistant",
                  text: "",
                  ts: Date.now(),
                } },
                timeoutMs: null,
                timeoutMessage: "paper bundle recovery timed out",
                closedMessage: "paper bundle recovery event stream closed",
                task_type: GENERATE_TASK,
                task_payload: { artifact_type: artifactType },
                recoverExisting: !bundle.job_id,
                retryPaperBundleArtifact: true,
                onBeforeProgressClear: (progress) => {
                  terminalProgress = progress;
                },
              });
              const latestBundle = get().conversations[parent.id]?.paper_bundle;
              if (
                latestBundle?.kind === "parent"
                && latestBundle.tasks[artifactType].status === "cancelling"
              ) {
                markRunProgressCancelling(childId, runId);
              }
              const result = await flow;
              applyPaperBundleResult(
                parent.id,
                artifactType,
                runId,
                result,
                terminalProgress,
              );
            } catch (error) {
              const latestBundle = get().conversations[parent.id]?.paper_bundle;
              const cancelled = error instanceof RunWaitCancelledError
                || (
                  latestBundle?.kind === "parent"
                  && paperBundleTaskWasCancelled(
                    currentDemoUserScope(),
                    parent.id,
                    paperBundleCancellationGeneration(latestBundle),
                    artifactType,
                  )
                );
              const firstError = primaryRunClientError(error);
              const detail = cancelled
                ? "Run cancelled."
                : firstError instanceof Error
                  ? firstError.message
                  : "Run recovery failed.";
              patchConversation(childId, (child) => {
                if (child.run_id !== runId) return child;
                return {
                  ...child,
                  pending: false,
                  run_id: undefined,
                  messages: child.messages.map((message) =>
                    message.id === placeholderId
                      ? {
                          ...message,
                          run_id: runId,
                          text: detail,
                          status: "error",
                          failure: cancelled
                            ? { status: "cancelled", produced_files: [], artifact_type: artifactType }
                            : runClientFailure(firstError, artifactType, runId),
                        }
                      : message
                  ),
                };
              });
              patchPaperBundleTask(parent.id, artifactType, (current) =>
                current.run_id !== runId
                  ? current
                  : {
                      ...current,
                      ...terminalPaperBundleTaskStats(
                        current,
                        terminalProgress ?? get().runs_progress[childId],
                      ),
                      status: cancelled ? "cancelled" : "failed",
                      error: detail,
                    }
              );
              clearRunProgress(childId, runId);
            }
          })();
          _PAPER_BUNDLE_RECOVERIES.set(recoveryKey, recovery);
          void recovery.finally(() => {
            if (_PAPER_BUNDLE_RECOVERIES.get(recoveryKey) === recovery) {
              _PAPER_BUNDLE_RECOVERIES.delete(recoveryKey);
            }
          });
        }
      }
      await Promise.allSettled(publicationHydrations);
    },

    loadPosterPalettes: async () => {
      const status = get().poster_palettes_status;
      if (status === "loading" || status === "ready") return;
      set({
        poster_palettes_status: "loading",
        poster_palettes_error: null,
      });
      try {
        const catalog = await fetchPosterPalettes();
        set((s) => {
          return {
            poster_palettes: catalog.palettes,
            poster_palettes_status: "ready",
            poster_palettes_error: null,
            conversations: validatePosterPaletteSelections(
              s.conversations,
              "ready",
              catalog.palettes,
            ),
          };
        });
      } catch (error) {
        set({
          poster_palettes: [],
          poster_palettes_status: "error",
          poster_palettes_error: error instanceof Error ? error.message : "Failed to load Poster palettes.",
        });
      }
    },

    loadPosterCanvasPresets: async () => {
      const status = get().poster_canvas_presets_status;
      if (status === "loading" || status === "ready") return;
      set({
        poster_canvas_presets_status: "loading",
        poster_canvas_presets_error: null,
      });
      try {
        const catalog = await fetchPosterCanvasPresets();
        const presets = validatePosterCanvasCatalog(catalog);
        set((state) => ({
          poster_canvas_presets: presets,
          poster_canvas_presets_status: "ready",
          poster_canvas_presets_error: null,
          conversations: validatePosterCanvasSelections(
            state.conversations,
            "ready",
            presets,
          ),
        }));
      } catch (error) {
        set({
          poster_canvas_presets: [],
          poster_canvas_presets_status: "error",
          poster_canvas_presets_error: error instanceof Error
            ? error.message
            : "Failed to load Poster canvas presets.",
        });
      }
    },

    // ---- settings drawer ----

    openSettings: () => set({ settings_open: true }),
    closeSettings: () => set({ settings_open: false }),
    setUiLanguage: (language) => set({ ui_language: language }),

    // ---- mode ----

    enterCanvas: (artifact_id) => {
      const conversation_id = get().current_conversation_id;
      emitArtifactEvent(conversation_id, "artifact.opened", artifact_id);
      set((s) => {
        const c = normalizeConversation(s.conversations[s.current_conversation_id]);
        if (!c) return s;
        const next: Conversation = artifact_id
          ? { ...c, active_artifact_id: artifact_id }
          : c;
        return {
          mode: "canvas",
          history_sidebar_open: false,
          selected_layer_id: null,
          selected_layer_ids: [],
          pending_insert: null,
          area_revision_active: false,
          area_revision_items: [],
          area_revision_focus_id: null,
          conversations: artifact_id
            ? { ...s.conversations, [s.current_conversation_id]: next }
            : s.conversations,
        };
      });
    },

    enterChat: () => set({ mode: "chat", pending_insert: null }),

    // ---- sidebar visibility ----

    toggleHistorySidebar: () =>
      set((s) => ({ history_sidebar_open: !s.history_sidebar_open })),

    togglePropertiesSidebar: () =>
      set((s) => ({ properties_sidebar_open: !s.properties_sidebar_open })),

    setDesignFocusMode: (focused) => set({ design_focus_mode: focused }),

    toggleDesignFocusMode: () =>
      set((s) => ({ design_focus_mode: !s.design_focus_mode })),

    setSidebarWidth: (which, px) => {
      // Per-panel min/max — keeps the user from accidentally collapsing a
      // sidebar to nothing or stretching it past the canvas.
      const RANGES = {
        history: [200, 420] as const,
        chat_rail: [260, 500] as const,
        properties: [240, 520] as const,
      };
      const [lo, hi] = RANGES[which];
      const clamped = Math.max(lo, Math.min(hi, Math.round(px)));
      const key = `${which === "chat_rail" ? "chat_rail" : `${which}_sidebar`}_width` as
        | "history_sidebar_width"
        | "chat_rail_width"
        | "properties_sidebar_width";
      set({ [key]: clamped } as any);
    },

    setActiveSlideIdx: (idx) => {
      const nextIdx = Math.max(0, Math.floor(idx));
      const { art } = getActiveContext();
      const frames = art ? detectSlideFrames(art) : [];
      if (!art || frames.length < 2) {
        set({ active_slide_idx: nextIdx, pending_insert: null });
        return;
      }
      const safeIdx = Math.min(nextIdx, frames.length - 1);
      const frame = frames[safeIdx];
      const selected = sanitizeSelection(get().selected_layer_ids, art).filter((id) => {
        const layer = art.layers.find((l) => l.layer_id === id);
        return layer ? layerCenterInFrame(layer, frame) : false;
      });
      set({
        active_slide_idx: safeIdx,
        selected_layer_ids: selected,
        selected_layer_id: selected[selected.length - 1] ?? null,
        pending_insert: null,
      });
    },

    addSlideAfter: () => {
      const { art } = getActiveContext();
      if (!art) return;
      const frames = detectSlideFrames(art);
      if (!frames.length) return;
      const idx = Math.min(get().active_slide_idx, frames.length - 1);
      const frame = frames[idx];
      const gap = slideGap(frames);
      const shift = frame.bbox.h + gap;
      const insertY = frame.bbox.y + shift;
      const newIds: string[] = [];
      patchActiveArtifact((current) => {
        const maxZ = Math.max(0, ...current.layers.map((l) => l.z_index));
        const paperId = nextId("slide");
        const numId = nextId("slide_num");
        newIds.push(paperId, numId);
        const shifted = current.layers.map((l) =>
          l.bbox && l.bbox.y >= insertY
            ? { ...l, bbox: { ...l.bbox, y: l.bbox.y + shift } }
            : l
        );
        const paper: Layer = {
          layer_id: paperId,
          name: `Slide ${idx + 2} paper`,
          kind: "shape",
          shape_kind: "rect",
          z_index: maxZ + 1,
          bbox: { ...frame.bbox, y: insertY },
          fill_color: "#fbf7ec",
          visible: true,
          locked: true,
        };
        const num: Layer = {
          layer_id: numId,
          name: `Slide ${idx + 2} number`,
          kind: "text",
          z_index: maxZ + 2,
          bbox: { x: frame.bbox.x + frame.bbox.w - 90, y: insertY + 36, w: 44, h: 20 },
          text: String(idx + 2).padStart(2, "0"),
          font_family: "Inter",
          font_size_px: 16,
          font_weight: 500,
          align: "right",
          effects: { fill: "#5f564b" },
          visible: true,
        };
        return {
          ...current,
          canvas: {
            ...current.canvas,
            h: Math.max(current.canvas.h + shift, insertY + frame.bbox.h + gap),
          },
          layers: [...shifted, paper, num],
        };
      });
      setSelectionState(newIds);
      set({ active_slide_idx: idx + 1 });
    },

    duplicateActiveSlide: () => {
      const { art } = getActiveContext();
      if (!art) return;
      const frames = detectSlideFrames(art);
      if (!frames.length) return;
      const idx = Math.min(get().active_slide_idx, frames.length - 1);
      const frame = frames[idx];
      const gap = slideGap(frames);
      const shift = frame.bbox.h + gap;
      const insertY = frame.bbox.y + shift;
      const source = art.layers.filter((l) => layerCenterInFrame(l, frame));
      const copiedGroupIds = new Set(
        source.map((l) => l.group_id).filter(Boolean) as string[]
      );
      const copiedGroups = (art.layer_groups ?? []).filter((g) =>
        copiedGroupIds.has(g.group_id)
      );
      const groupIdMap = new Map(copiedGroups.map((g) => [g.group_id, nextId("grp")]));
      const newIds: string[] = [];
      patchActiveArtifact((current) => {
        const maxZ = Math.max(0, ...current.layers.map((l) => l.z_index));
        const shifted = current.layers.map((l) =>
          l.bbox && l.bbox.y >= insertY
            ? { ...l, bbox: { ...l.bbox, y: l.bbox.y + shift } }
            : l
        );
        const copies = source.map((l, copyIdx) => {
          const id = nextId("lyr");
          newIds.push(id);
          return {
            ...cloneLayer(l),
            layer_id: id,
            name: `${l.name} copy`,
            z_index: maxZ + copyIdx + 1,
            bbox: l.bbox ? { ...l.bbox, y: l.bbox.y + shift } : l.bbox,
            group_id: l.group_id ? groupIdMap.get(l.group_id) : undefined,
          };
        });
        const newGroups = copiedGroups.map((g) => ({
          group_id: groupIdMap.get(g.group_id)!,
          name: `${g.name} copy`,
        }));
        return {
          ...current,
          canvas: {
            ...current.canvas,
            h: Math.max(current.canvas.h + shift, insertY + frame.bbox.h + gap),
          },
          layer_groups: [...(current.layer_groups ?? []), ...newGroups],
          layers: [...shifted, ...copies],
        };
      });
      setSelectionState(newIds);
      set({ active_slide_idx: idx + 1 });
    },

    deleteActiveSlide: () => {
      const { art } = getActiveContext();
      if (!art) return;
      const frames = detectSlideFrames(art);
      if (frames.length <= 1) return;
      const idx = Math.min(get().active_slide_idx, frames.length - 1);
      const frame = frames[idx];
      const gap = slideGap(frames);
      const shift = frame.bbox.h + gap;
      const removeIds = new Set(
        art.layers.filter((l) => layerCenterInFrame(l, frame)).map((l) => l.layer_id)
      );
      patchActiveArtifact((current) => ({
        ...pruneLayerGroups({
          ...current,
          canvas: { ...current.canvas, h: Math.max(frame.bbox.h + gap * 2, current.canvas.h - shift) },
          layers: current.layers
            .filter((l) => !removeIds.has(l.layer_id))
            .map((l) =>
              l.bbox && l.bbox.y > frame.bbox.y
                ? { ...l, bbox: { ...l.bbox, y: l.bbox.y - shift } }
                : l
            ),
        }),
      }));
      setSelectionState([]);
      set({ active_slide_idx: Math.max(0, Math.min(idx, frames.length - 2)) });
    },

    moveActiveSlide: (dir) => {
      const { art } = getActiveContext();
      if (!art) return;
      const frames = detectSlideFrames(art);
      if (frames.length < 2) return;
      const idx = Math.min(get().active_slide_idx, frames.length - 1);
      const otherIdx = dir === "up" ? idx - 1 : idx + 1;
      if (otherIdx < 0 || otherIdx >= frames.length) return;
      const a = frames[idx];
      const b = frames[otherIdx];
      const aIds = new Set(art.layers.filter((l) => layerCenterInFrame(l, a)).map((l) => l.layer_id));
      const bIds = new Set(art.layers.filter((l) => layerCenterInFrame(l, b)).map((l) => l.layer_id));
      const dyA = b.bbox.y - a.bbox.y;
      const dyB = a.bbox.y - b.bbox.y;
      patchActiveArtifact((current) => ({
        ...current,
        layers: current.layers.map((l) => {
          if (!l.bbox) return l;
          if (aIds.has(l.layer_id)) return { ...l, bbox: { ...l.bbox, y: l.bbox.y + dyA } };
          if (bIds.has(l.layer_id)) return { ...l, bbox: { ...l.bbox, y: l.bbox.y + dyB } };
          return l;
        }),
      }));
      set({ active_slide_idx: otherIdx });
    },

    moveActiveSlideToIndex: (targetIdx) => {
      const { art } = getActiveContext();
      if (!art) return;
      const frames = detectSlideFrames(art);
      if (frames.length < 2) return;
      const fromIdx = Math.min(get().active_slide_idx, frames.length - 1);
      const toIdx = Math.max(0, Math.min(Math.floor(targetIdx), frames.length - 1));
      if (fromIdx === toIdx) return;
      const blocks = frames.map((frame) => ({
        frame,
        layers: art.layers.filter((l) => layerCenterInFrame(l, frame)),
      }));
      const [moving] = blocks.splice(fromIdx, 1);
      blocks.splice(toIdx, 0, moving);
      const dyById = new Map<string, number>();
      blocks.forEach((block, idx) => {
        const targetY = frames[idx].bbox.y;
        const dy = targetY - block.frame.bbox.y;
        block.layers.forEach((l) => dyById.set(l.layer_id, dy));
      });
      patchActiveArtifact((current) => ({
        ...current,
        layers: current.layers.map((l) => {
          const dy = dyById.get(l.layer_id);
          return dy !== undefined && l.bbox
            ? { ...l, bbox: { ...l.bbox, y: l.bbox.y + dy } }
            : l;
        }),
      }));
      set({ active_slide_idx: toIdx });
    },

    toggleSnap: () => set((s) => ({ snap_enabled: !s.snap_enabled })),
    toggleGrid: () => set((s) => ({ grid_visible: !s.grid_visible })),
    toggleRulers: () => set((s) => ({ rulers_visible: !s.rulers_visible })),
    toggleSafeMargins: () => set((s) => ({ safe_margins_visible: !s.safe_margins_visible })),
    toggleSmartGuides: () => set((s) => ({ smart_guides_visible: !s.smart_guides_visible })),
    setGridSize: (px) =>
      set({ grid_size_px: Math.max(4, Math.min(24, Math.round(px))) }),
    setSafeMarginPct: (pct) =>
      set({ safe_margin_pct: Math.max(0.04, Math.min(0.1, pct)) }),
    rememberColor: (color) => {
      const clean = normalizeColor(color);
      if (!clean) return;
      set((s) => ({
        recent_colors: [clean, ...s.recent_colors.filter((c) => c !== clean)].slice(0, 12),
      }));
    },
    setInsertPlacementMode: (mode) => set({ insert_placement_mode: mode }),
    setPendingInsert: (layers, options = {}) => {
      if (!layers.length) {
        set({ pending_insert: null });
        return;
      }
      set({
        pending_insert: {
          layers: layers.map(cloneLayer),
          placement: options.placement ?? (layers.length === 1 ? "single" : "frame-relative"),
        },
      });
    },
    cancelPendingInsert: () => set({ pending_insert: null }),
    commitPendingInsert: (anchor) => {
      const pending = get().pending_insert;
      if (!pending) return;
      set({ pending_insert: null });
      get().insertLayers(pending.layers, {
        placement: pending.placement,
        strategy: "point",
        anchor,
      });
    },
    setLayerGroupCollapsed: (group_id, collapsed) =>
      set((s) => ({
        layer_group_collapsed: {
          ...s.layer_group_collapsed,
          [group_id]: collapsed,
        },
      })),

    setDeckNavBarHeight: (px) => {
      const clamped = Math.max(80, Math.min(320, Math.round(px)));
      set({ deck_navbar_height: clamped });
    },

    // ---- conversation management ----

	    newConversation: () => {
	      const c = freshConversation();
	      set((s) => ({
	        conversations: { ...s.conversations, [c.id]: c },
	        current_conversation_id: c.id,
	        mode: "chat",
	        selected_layer_id: null,
	        selected_layer_ids: [],
	        intent_type: null,
	        area_revision_active: false,
	        area_revision_items: [],
	        area_revision_focus_id: null,
	      }));
	    },

    switchConversation: (id) => {
      const shouldHydrate = get().conversations[id]?.history_summary === true;
      set((s) => {
        const c = normalizeConversation(s.conversations[id]);
        if (!c) return s;
        return {
          conversations: { ...s.conversations, [id]: c },
          current_conversation_id: id,
          mode: "chat",
          selected_layer_id: null,
          selected_layer_ids: [],
          intent_type: null,
          area_revision_active: false,
          area_revision_items: [],
          area_revision_focus_id: null,
        };
      });
      if (shouldHydrate) {
        void get().hydrateServerHistoryConversation(id);
      }
    },

    setPosterPalette: (paletteId) => {
      const poster_palette_id = paletteId?.trim() || null;
      patchConversation(get().current_conversation_id, (conversation) => ({
        ...conversation,
        poster_palette_id,
      }));
    },

    setPosterCanvasPreset: (presetId) => {
      const clean = presetId.trim();
      if (!clean) return;
      const current = get();
      if (
        clean !== "auto"
        && (
          current.poster_canvas_presets_status !== "ready"
          || !current.poster_canvas_presets.some((preset) => preset.id === clean)
        )
      ) {
        return;
      }
      const conversationId = current.current_conversation_id;
      patchConversation(conversationId, (conversation) => ({
        ...conversation,
        poster_canvas_preset_id: clean,
      }));
      set((state) => {
        if (!state.canvas_validation_errors[conversationId]) return state;
        const errors = { ...state.canvas_validation_errors };
        delete errors[conversationId];
        return { canvas_validation_errors: errors };
      });
    },

    clearCanvasValidationError: (conversationId) => {
      const id = conversationId ?? get().current_conversation_id;
      set((state) => {
        if (!state.canvas_validation_errors[id]) return state;
        const errors = { ...state.canvas_validation_errors };
        delete errors[id];
        return { canvas_validation_errors: errors };
      });
    },

    deleteConversation: (id) => {
      const target = get().conversations[id];
      const targetBundle = target?.paper_bundle;
      const childIds = targetBundle?.kind === "parent"
        ? new Set([
            ...PAPER_BUNDLE_ARTIFACT_ORDER.map(
              (artifactType) => targetBundle.tasks[artifactType].child_conversation_id,
            ),
            ...Object.values(get().conversations)
              .filter((conversation) =>
                conversation.paper_bundle?.kind === "child"
                && conversation.paper_bundle.parent_conversation_id === id
              )
              .map((conversation) => conversation.id),
          ])
        : new Set<string>();
      if (targetBundle?.kind === "parent") {
        void get().cancelPaperBundle(id);
      }
      set((s) => {
        const next = { ...s.conversations };
        delete next[id];
        for (const childId of childIds) delete next[childId];
        let nextActive = s.current_conversation_id;
        if (s.current_conversation_id === id || childIds.has(s.current_conversation_id)) {
          const remaining = Object.values(next)
            .filter((conversation) => conversation.paper_bundle?.kind !== "child")
            .sort((a, b) => b.updated_at - a.updated_at);
          if (remaining.length > 0) {
            nextActive = remaining[0].id;
          } else {
            const fresh = freshConversation();
            next[fresh.id] = fresh;
            nextActive = fresh.id;
          }
        }
	        return {
	          conversations: next,
	          current_conversation_id: nextActive,
	          mode: "chat",
	          selected_layer_id: null,
	          selected_layer_ids: [],
	          intent_type: null,
	          area_revision_active: false,
	          area_revision_items: [],
	          area_revision_focus_id: null,
	        };
      });
    },

    renameConversation: (id, title) =>
      patchConversation(id, (c) => ({ ...c, title: title.trim() || c.title })),

    loadDemoDeck: () => {
      // Pure-frontend demo: a fake conversation with a fake artifact
      // pointing at web/public/sample_deck.html. No /api/generate call.
      const conv = freshConversation();
      const now = Date.now();
      const art_id = `art_demo_${now}`;
      const artifact: Artifact = {
        artifact_id: art_id,
        name: "Demo Deck — Multi-Agent AI Workflows",
        artifact_type: "deck",
        canvas: { w: 1280, h: 720 },
        native_file_url: "/sample_deck.html",
        native_format: "html",
        download_url: "/sample_deck.html",
        layers: [],
      };
      const userMsg: Message = {
        id: `msg_demo_user_${now}`,
        role: "user",
        ts: now,
        text: "Show me what a 6-slide deck looks like in canvas (demo, no agent).",
        status: "done",
      };
      const agentMsg: Message = {
        id: `msg_demo_agent_${now}`,
        role: "assistant",
        ts: now + 1,
        text:
          "Demo deck loaded — open it in canvas to see the slide-nav strip in action. " +
          "No agent ran, no money spent.",
        artifact_id: art_id,
        status: "done",
      };
      const populated: Conversation = {
        ...conv,
        title: "Demo deck (no agent)",
        updated_at: now,
        messages: [userMsg, agentMsg],
        artifacts: { [art_id]: artifact },
        active_artifact_id: art_id,
	      };
	      set((s) => ({
	        conversations: { ...s.conversations, [populated.id]: populated },
	        current_conversation_id: populated.id,
	        mode: "canvas",
	        history_sidebar_open: false,
	        selected_layer_id: null,
	        selected_layer_ids: [],
	        intent_type: null,
	        area_revision_active: false,
	        area_revision_items: [],
	        area_revision_focus_id: null,
	      }));
	    },

    loadDemoPoster: () => {
      // Pure-frontend editable demo: a fake conversation with the
      // layer-based sample poster. This exercises the right sidebar,
      // drag-to-move, layer ordering, visibility, and insert tools
      // without making an agent/API call.
      const conv = freshConversation();
      const now = Date.now();
      const artifact = samplePoster();
      const userMsg: Message = {
        id: `msg_demo_poster_user_${now}`,
        role: "user",
        ts: now,
        text: "Open an editable poster demo so I can test the canvas tools.",
        status: "done",
      };
      const agentMsg: Message = {
        id: `msg_demo_poster_agent_${now}`,
        role: "assistant",
        ts: now + 1,
        text:
          "Editable poster demo loaded — use the canvas and properties panel " +
          "to move layers, change typography, and insert objects. No agent ran.",
        artifact_id: artifact.artifact_id,
        status: "done",
      };
      const populated: Conversation = {
        ...conv,
        title: "Editable poster demo",
        updated_at: now,
        messages: [userMsg, agentMsg],
        artifacts: { [artifact.artifact_id]: artifact },
        active_artifact_id: artifact.artifact_id,
	      };
	      set((s) => ({
	        conversations: { ...s.conversations, [populated.id]: populated },
	        current_conversation_id: populated.id,
	        mode: "canvas",
	        history_sidebar_open: false,
	        selected_layer_id: null,
	        selected_layer_ids: [],
	        intent_type: null,
	        area_revision_active: false,
	        area_revision_items: [],
	        area_revision_focus_id: null,
	      }));
	    },

    loadDemoSlides: () => {
      // Pure-frontend editable slide stack: three 16:9 slides arranged
      // vertically on one canvas. This is intentionally not a PPTX or
      // HTML export, so designers can dogfood immediate layer editing.
      const conv = freshConversation();
      const now = Date.now();
      const artifact = sampleSlides();
      const userMsg: Message = {
        id: `msg_demo_slides_user_${now}`,
        role: "user",
        ts: now,
        text: "Open editable slides so I can test slide design tools.",
        status: "done",
      };
      const agentMsg: Message = {
        id: `msg_demo_slides_agent_${now}`,
        role: "assistant",
        ts: now + 1,
        text:
          "Editable slides demo loaded — select any title, metric, chart bar, " +
          "or chip to test slide-level layout editing. No agent ran.",
        artifact_id: artifact.artifact_id,
        status: "done",
      };
      const populated: Conversation = {
        ...conv,
        title: "Editable slides demo",
        updated_at: now,
        messages: [userMsg, agentMsg],
        artifacts: { [artifact.artifact_id]: artifact },
        active_artifact_id: artifact.artifact_id,
	      };
	      set((s) => ({
	        conversations: { ...s.conversations, [populated.id]: populated },
	        current_conversation_id: populated.id,
	        mode: "canvas",
	        history_sidebar_open: false,
	        selected_layer_id: null,
	        selected_layer_ids: [],
	        intent_type: null,
	        area_revision_active: false,
	        area_revision_items: [],
	        area_revision_focus_id: null,
	      }));
	    },

    loadDemoLanding: () => {
      // Pure-frontend editable landing page: a tall canvas with hero,
	      // cards, stats, and CTA sections. No native HTML source means no
	      // save round-trip; all changes are local and immediate.
      const conv = freshConversation();
      const now = Date.now();
      const artifact = sampleLandingPage();
      const userMsg: Message = {
        id: `msg_demo_landing_user_${now}`,
        role: "user",
        ts: now,
        text: "Open an editable landing page demo for canvas testing.",
        status: "done",
      };
      const agentMsg: Message = {
        id: `msg_demo_landing_agent_${now}`,
        role: "assistant",
        ts: now + 1,
        text:
          "Editable landing page demo loaded — test hero copy, cards, stats, " +
          "CTA spacing, and long-page composition. No agent ran.",
        artifact_id: artifact.artifact_id,
        status: "done",
      };
      const populated: Conversation = {
        ...conv,
        title: "Editable landing demo",
        updated_at: now,
        messages: [userMsg, agentMsg],
        artifacts: { [artifact.artifact_id]: artifact },
        active_artifact_id: artifact.artifact_id,
	      };
	      set((s) => ({
	        conversations: { ...s.conversations, [populated.id]: populated },
	        current_conversation_id: populated.id,
	        mode: "canvas",
	        history_sidebar_open: false,
	        selected_layer_id: null,
	        selected_layer_ids: [],
	        intent_type: null,
	        area_revision_active: false,
	        area_revision_items: [],
	        area_revision_focus_id: null,
	      }));
	    },

    loadDemoVideo: () => {
      // Pure-frontend editable video project: stacked 16:9 scenes on
      // one layer canvas. Rendering is explicit via /api/video/render.
      const conv = freshConversation();
      const now = Date.now();
      const artifact = sampleVideo();
      const userMsg: Message = {
        id: `msg_demo_video_user_${now}`,
        role: "user",
        ts: now,
        text: "Open an editable video demo for scene-based video editing.",
        status: "done",
      };
      const agentMsg: Message = {
        id: `msg_demo_video_agent_${now}`,
        role: "assistant",
        ts: now + 1,
        text:
          "Editable video demo loaded — edit scene layers, tune duration, " +
          "then render a real MP4. No agent ran.",
        artifact_id: artifact.artifact_id,
        status: "done",
      };
      const populated: Conversation = {
        ...conv,
        title: "Editable video demo",
        updated_at: now,
        messages: [userMsg, agentMsg],
        artifacts: { [artifact.artifact_id]: artifact },
        active_artifact_id: artifact.artifact_id,
	      };
	      set((s) => ({
	        conversations: { ...s.conversations, [populated.id]: populated },
	        current_conversation_id: populated.id,
	        mode: "canvas",
	        history_sidebar_open: false,
	        selected_layer_id: null,
	        selected_layer_ids: [],
	        intent_type: null,
	        area_revision_active: false,
	        area_revision_items: [],
	        area_revision_focus_id: null,
	      }));
	    },

    // ---- intent ----

    setIntent: (type) => set({ intent_type: type }),

    // ---- visual area revision selection ----

    setAreaRevisionActive: (active) => set({
      area_revision_active: active,
      ...(active ? {} : {
        area_revision_items: [],
        area_revision_focus_id: null,
      }),
    }),
    setAreaRevisionItems: (items) => set({
      area_revision_items: items
        .slice(0, AREA_REVISION_MAX_ITEMS)
        .map(normalizeAreaRevisionItem),
      area_revision_focus_id: null,
    }),
    addAreaRevisionItem: (item, options = {}) => set((s) => {
      const nextItem = normalizeAreaRevisionItem(item);
      if (!options.append) {
        return {
          area_revision_items: [nextItem],
          area_revision_focus_id: nextItem.selection_id,
        };
      }
      const key = areaRevisionKey(nextItem);
      const existingIdx = s.area_revision_items.findIndex((candidate) =>
        areaRevisionKey(candidate) === key
      );
      if (existingIdx >= 0) {
        if (options.toggle) {
          const next = s.area_revision_items.filter((_, idx) => idx !== existingIdx);
          return {
            area_revision_items: next,
            area_revision_focus_id: next[0]?.selection_id ?? null,
          };
        }
        const next = [...s.area_revision_items];
        next[existingIdx] = nextItem;
        return {
          area_revision_items: next,
          area_revision_focus_id: nextItem.selection_id,
        };
      }
      const next = [...s.area_revision_items, nextItem].slice(-AREA_REVISION_MAX_ITEMS);
      return {
        area_revision_items: next,
        area_revision_focus_id: nextItem.selection_id,
      };
    }),
    updateAreaRevisionItemInstruction: (selection_id, instruction) => set((s) => ({
      area_revision_items: s.area_revision_items.map((item) =>
        item.selection_id === selection_id
          ? normalizeAreaRevisionItem({ ...item, instruction })
          : item
      ),
    })),
    removeAreaRevisionItem: (selection_id) => set((s) => {
      const next = s.area_revision_items.filter((item) => item.selection_id !== selection_id);
      return {
        area_revision_items: next,
        area_revision_focus_id:
          s.area_revision_focus_id === selection_id
            ? next[0]?.selection_id ?? null
            : s.area_revision_focus_id,
      };
    }),
    clearAreaRevisionItems: () => set({
      area_revision_items: [],
      area_revision_focus_id: null,
    }),
    focusAreaRevisionItem: (selection_id) => set({ area_revision_focus_id: selection_id }),
    setSelectedPaperAsset: (asset) => set({ selected_paper_asset: asset }),

    // ---- chat ----

    startPaperBundle: async (file, requestedParentConversationId) => {
      const requestScope = currentDemoUserScope();
      const parentConversationId = requestedParentConversationId
        ?? get().current_conversation_id;
      if (!file || (file.type !== "application/pdf" && !/\.pdf$/i.test(file.name))) {
        throw new Error("Select a PDF paper to start Paper All-in-One.");
      }
      if (hasActivePptxOperation(
        parentConversationId,
        get().conversations[parentConversationId],
      )) {
        throw new Error("A PowerPoint export is already running for this conversation.");
      }
      await get().loadBackendInfo();
      if (currentDemoUserScope() !== requestScope) return;
      const initialState = get();
      const backendOwnerId = paperBundleBackendOwnerId(
        requestScope,
        initialState.backend_info,
      );
      const initialParent = initialState.conversations[parentConversationId];
      if (initialState.backend_info?.demo_mode) {
        throw new Error("Paper All-in-One is unavailable in demo mode.");
      }
      if (!initialParent || initialParent.paper_bundle?.kind === "child") {
        throw new Error("Paper All-in-One requires a visible parent conversation.");
      }
      if (initialParent.pending) {
        throw new Error("Wait for the current conversation to finish before starting Paper All-in-One.");
      }
      if (initialParent.paper_bundle) {
        throw new Error("This conversation already owns a Paper All-in-One bundle.");
      }
      const initialCanvasPresetId = initialParent.poster_canvas_preset_id ?? "auto";
      if (get().poster_palettes_status !== "ready") {
        await get().loadPosterPalettes();
        while (get().poster_palettes_status === "loading") {
          await new Promise((resolve) => window.setTimeout(resolve, 25));
        }
      }
      if (
        initialCanvasPresetId !== "auto"
        && get().poster_canvas_presets_status !== "ready"
      ) {
        await get().loadPosterCanvasPresets();
        while (get().poster_canvas_presets_status === "loading") {
          await new Promise((resolve) => window.setTimeout(resolve, 25));
        }
      }
      if (currentDemoUserScope() !== requestScope) return;
      const paletteState = get();
      if (paletteState.poster_palettes_status !== "ready" || paletteState.poster_palettes.length === 0) {
        throw new Error(
          paletteState.poster_palettes_error
            || "Poster palette catalog is unavailable. No paper bundle was started.",
        );
      }
      if (
        initialCanvasPresetId !== "auto"
        && (
          paletteState.poster_canvas_presets_status !== "ready"
          || paletteState.poster_canvas_presets.length === 0
        )
      ) {
        throw new Error(
          paletteState.poster_canvas_presets_error
            || "Poster canvas preset catalog is unavailable. No paper bundle was started.",
        );
      }
      const currentParent = paletteState.conversations[parentConversationId];
      if (currentParent && hasActivePptxOperation(parentConversationId, currentParent)) {
        throw new Error("A PowerPoint export is already running for this conversation.");
      }
      if (!currentParent || currentParent.paper_bundle || currentParent.pending) {
        throw new Error("The parent conversation changed before Paper All-in-One could start.");
      }
      const canonicalPalette = currentParent.poster_palette_id
        ? paletteState.poster_palettes.find(
            (palette) => palette.id === currentParent.poster_palette_id,
          )
        : undefined;
      const selectedPalette = canonicalPalette
        ?? paletteState.poster_palettes[
          Math.floor(Math.random() * paletteState.poster_palettes.length)
        ];
      const selectedCanvas = posterCanvasRequestSelection(
        paletteState.poster_canvas_presets,
        currentParent.poster_canvas_preset_id,
      );
      const attachment: Attachment = {
        id: nextId("att"),
        name: file.name,
        size: file.size,
        kind: "pdf",
        role: "content",
        file,
      };
      const attachmentMetadata: Attachment = {
        id: attachment.id,
        name: attachment.name,
        size: attachment.size,
        kind: attachment.kind,
        role: attachment.role,
      };
      const requestSpecs = createPaperBundleRequestSpecs(
        parentConversationId,
        [attachment],
        selectedPalette.id,
        selectedCanvas.canvas_preset_id,
        selectedCanvas.template,
      );
      const authoringBudgets = readAuthoringBudgets();
      const now = Date.now();
      const jobId = crypto.randomUUID();
      const operation = {
        jobId,
        ownerScope: requestScope,
        cancelRequested: false,
        createDispatched: false,
      };
      _PAPER_BUNDLE_OPERATIONS.set(parentConversationId, operation);
      const parentMessage: Message = {
        id: nextId("msg"),
        role: "user",
        text: `Create a complete paper bundle from ${file.name}.`,
        ts: now,
        attachments: [attachmentMetadata],
        status: "done",
      };
      const childRuns = requestSpecs.map((spec) => {
        const placeholderId = nextId("msg");
        const taskPayload: MessageTaskPayload = {
          artifact_type: spec.artifact_type,
          authoring_max_attempts: authoringBudgetFor(
            authoringBudgets,
            spec.artifact_type,
          ),
          template: spec.template,
          canvas_preset_id: spec.canvas_preset_id,
          palette_id: spec.palette_id,
          attachment_refs: [{
            name: attachmentMetadata.name,
            size: attachmentMetadata.size,
            kind: attachmentMetadata.kind,
            role: attachmentMetadata.role,
          }],
        };
        const userMessage: Message = {
          id: nextId("msg"),
          role: "user",
          text: spec.brief,
          ts: now,
          attachments: [attachmentMetadata],
          status: "done",
          task_type: GENERATE_TASK,
          task_payload: taskPayload,
        };
        const placeholderMessage: Message = {
          id: placeholderId,
          role: "assistant",
          text: "",
          ts: now,
          status: "streaming",
          task_type: GENERATE_TASK,
          task_payload: taskPayload,
        };
        return { spec, placeholderId, taskPayload, userMessage, placeholderMessage };
      });
      set((state) => {
        if (currentDemoUserScope() !== requestScope) return state;
        const parent = state.conversations[parentConversationId];
        if (!parent) return state;
        const bundle = createPaperBundleParentState(parentConversationId, file.name, now);
        bundle.job_id = jobId;
        bundle.backend_state = "reserved";
        const conversations = { ...state.conversations };
        conversations[parentConversationId] = {
          ...parent,
          title: parent.messages.length === 0 ? file.name : parent.title,
          updated_at: now,
          pending: true,
          poster_palette_id: selectedPalette.id,
          poster_canvas_preset_id: selectedCanvas.canvas_preset_id,
          paper_bundle: bundle,
          messages: [...parent.messages, parentMessage],
        };
        for (const childRun of childRuns) {
          const artifactType = childRun.spec.artifact_type;
          conversations[childRun.spec.conversation_id] = {
            id: childRun.spec.conversation_id,
            title: `${file.name} - ${ARTIFACT_TYPE_LABELS[artifactType]}`,
            created_at: now,
            updated_at: now,
            messages: [childRun.userMessage, childRun.placeholderMessage],
            artifacts: {},
            active_artifact_id: null,
            poster_palette_id: artifactType === "poster" ? selectedPalette.id : null,
            poster_canvas_preset_id: artifactType === "poster"
              ? selectedCanvas.canvas_preset_id
              : "auto",
            paper_bundle: createPaperBundleChildState(parentConversationId, artifactType),
            pending: true,
          };
        }
        return { conversations };
      });

      try {
        const prepared = await preparePaperBundleInput(file);
        if (operation.cancelRequested || currentDemoUserScope() !== requestScope) return;
        const request: PaperBundleCreateRequest = {
          job_id: jobId,
          conversation_id: parentConversationId,
          source_name: file.name,
          prompt_version: "1",
          children: Object.fromEntries(childRuns.map(({ spec, taskPayload }) => [
            spec.artifact_type,
            {
              brief: spec.brief,
              artifact_type: spec.artifact_type,
              conversation_id: spec.conversation_id,
              input_slots: [prepared.slot],
              ...(spec.palette_id ? { palette_id: spec.palette_id } : {}),
              ...(spec.template ? { template: spec.template } : {}),
              ...(spec.canvas_preset_id
                ? { canvas_preset_id: spec.canvas_preset_id }
                : {}),
              ...(taskPayload.authoring_max_attempts === undefined
                ? {}
                : { authoring_max_attempts: taskPayload.authoring_max_attempts }),
            },
          ])) as PaperBundleCreateRequest["children"],
        };
        operation.createDispatched = true;
        const created = await createPaperBundle(request, jobId);
        let createApplied = false;
        set((state) => {
          if (
            currentDemoUserScope() !== requestScope
            || created.owner_id !== backendOwnerId
          ) {
            return state;
          }
          const parent = state.conversations[parentConversationId];
          if (parent?.paper_bundle?.kind !== "parent" || parent.paper_bundle.job_id !== jobId) {
            return state;
          }
          if (
            (
              parent.paper_bundle.revision !== undefined
              && created.revision < parent.paper_bundle.revision
            )
            || (
              isTerminalPaperBundleBackendState(parent.paper_bundle.backend_state)
              && !created.terminal
            )
          ) {
            return state;
          }
          createApplied = true;
          const conversations = { ...state.conversations };
          const tasks: Record<ArtifactType, PaperBundleTask> = {
            ...parent.paper_bundle.tasks,
          };
          for (const artifactType of PAPER_BUNDLE_ARTIFACT_ORDER) {
            const descriptor = created.children[artifactType];
            const task = tasks[artifactType];
            const taskCancellationRequested = paperBundleTaskWasCancelled(
              requestScope,
              parentConversationId,
              paperBundleJobGeneration(jobId),
              artifactType,
            );
            tasks[artifactType] = {
              ...task,
              run_id: descriptor.run_id,
              ...(artifactType === "video" ? { authoring_run_id: descriptor.run_id } : {}),
              status: operation.cancelRequested || taskCancellationRequested
                ? "cancelling"
                : paperBundleTaskStatusFromBackend(descriptor.state),
              terminal: descriptor.terminal,
              process_free: descriptor.process_free,
              error: taskCancellationRequested
                ? task.error || "Waiting for the server reservation before cancellation."
                : undefined,
            };
            const child = conversations[task.child_conversation_id];
            if (child) {
              conversations[task.child_conversation_id] = {
                ...child,
                pending: !descriptor.terminal,
                run_id: descriptor.terminal ? undefined : descriptor.run_id,
              };
            }
          }
          conversations[parentConversationId] = {
            ...parent,
            paper_bundle: {
              ...parent.paper_bundle,
              revision: created.revision,
              backend_state: operation.cancelRequested ? "cancelling" : created.state,
              tasks: tasks as PaperBundleTaskMap,
            },
            pending: true,
          };
          return { conversations };
        });
        if (!createApplied) return;

        const flowContexts = childRuns.map((childRun) => {
          const { spec, placeholderId, taskPayload } = childRun;
          const descriptor = created.children[spec.artifact_type];
          const controller = new AbortController();
          const upload = {
            parentConversationId,
            artifactType: spec.artifact_type,
            runId: descriptor.run_id,
            controller,
          };
          _PAPER_BUNDLE_UPLOADS.set(spec.conversation_id, upload);
          const isCurrent = () => {
            const bundle = get().conversations[parentConversationId]?.paper_bundle;
            const task = bundle?.kind === "parent"
              ? bundle.tasks[spec.artifact_type]
              : undefined;
	            return currentDemoUserScope() === requestScope
	              && bundle?.kind === "parent"
	              && bundle.job_id === jobId
	              && task?.run_id === descriptor.run_id
	              && task.terminal !== true;
          };
          let terminalProgress: RunProgress | undefined;
          const terminalFlow = runGenerateAckFlow({
            convId: spec.conversation_id,
            placeholderId,
            ack: {
              run_id: descriptor.run_id,
              placeholder_message: childRun.placeholderMessage,
            },
            timeoutMs: null,
            timeoutMessage: "paper bundle run timed out",
            closedMessage: "paper bundle event stream closed - backend may have crashed",
            task_type: GENERATE_TASK,
            task_payload: taskPayload,
            retryPaperBundleArtifact: true,
            onBeforeProgressClear: (progress) => { terminalProgress = progress; },
            shouldApplyResult: isCurrent,
          });
          void terminalFlow.catch(() => undefined);
          return {
            childRun,
            descriptor,
            controller,
            upload,
            terminalFlow,
            terminalProgress: () => terminalProgress,
            isCurrent,
          };
        });

        if (operation.cancelRequested) {
          for (const context of flowContexts) {
            context.controller.abort(new RunWaitCancelledError());
          }
          const cancelRequestKey = paperBundleCancelRequestKey(requestScope, jobId);
          const existingCancellation = _PAPER_BUNDLE_CANCEL_REQUESTS.get(cancelRequestKey);
          if (existingCancellation) {
            await existingCancellation;
            if (_PAPER_BUNDLE_CANCEL_REQUESTS.get(cancelRequestKey) === existingCancellation) {
              _PAPER_BUNDLE_CANCEL_REQUESTS.delete(cancelRequestKey);
            }
          }
          const latestBundle = get().conversations[parentConversationId]?.paper_bundle;
          if (
            latestBundle?.kind === "parent"
            && latestBundle.job_id === jobId
            && !isTerminalPaperBundleBackendState(latestBundle.backend_state)
          ) {
            await get().cancelPaperBundle(parentConversationId);
          }
          return;
        }

        await Promise.allSettled(flowContexts.map(async (context) => {
          const { childRun, descriptor, controller, upload, terminalFlow, isCurrent } = context;
          const { spec, placeholderId } = childRun;
          try {
            if (
              currentDemoUserScope() !== requestScope
              || paperBundleTaskWasCancelled(
                requestScope,
                parentConversationId,
                paperBundleJobGeneration(jobId),
                spec.artifact_type,
              )
            ) {
              controller.abort(new RunWaitCancelledError());
              if (currentDemoUserScope() === requestScope) {
                await get().cancelPaperBundleTask(parentConversationId, spec.artifact_type);
              }
              return;
            }
            await uploadReservedRunInput(
              descriptor,
              prepared.slot.name,
              prepared.file,
              controller.signal,
            );
            const currentBundle = get().conversations[parentConversationId]?.paper_bundle;
            if (
              currentDemoUserScope() !== requestScope
              || currentBundle?.kind !== "parent"
              || currentBundle.job_id !== jobId
              || operation.cancelRequested
              || currentBundle.tasks[spec.artifact_type].status === "cancelling"
            ) return;
            await startReservedRun(descriptor, controller.signal);
            patchConversation(parentConversationId, (parent) => {
              const bundle = parent.paper_bundle;
              if (
                currentDemoUserScope() !== requestScope
                || bundle?.kind !== "parent"
                || bundle.job_id !== jobId
              ) return parent;
              const task = bundle.tasks[spec.artifact_type];
              if (task.run_id !== descriptor.run_id || task.status !== "uploading") return parent;
              const tasks = {
                ...bundle.tasks,
                [spec.artifact_type]: {
                  ...task,
                  status: "running" as const,
                  error: undefined,
                },
              };
              return {
                ...parent,
                paper_bundle: { ...bundle, backend_state: "running", tasks },
              };
            });
            const result = await terminalFlow;
            applyPaperBundleResult(
              parentConversationId,
              spec.artifact_type,
              descriptor.run_id,
              result,
              context.terminalProgress(),
              isCurrent,
            );
	          } catch (caught) {
	            let error: unknown = caught;
	            if (
	              error instanceof RunStartAmbiguousError
	              && error.runId === descriptor.run_id
	              && isCurrent()
	            ) {
	              const waitOwner = _SSE_WAIT_ABORTS.get(spec.conversation_id);
	              if (waitOwner?.runId === descriptor.run_id) {
	                waitOwner.reconcile(error.retryStart);
	              }
	              try {
	                const result = await terminalFlow;
	                applyPaperBundleResult(
	                  parentConversationId,
	                  spec.artifact_type,
	                  descriptor.run_id,
	                  result,
	                  context.terminalProgress(),
	                  isCurrent,
	                );
	                return;
	              } catch (terminalError) {
	                error = terminalError;
	              }
	            }
	            if (!isCurrent()) return;
            const latest = get().conversations[parentConversationId]?.paper_bundle;
            const latestTask = latest?.kind === "parent"
              ? latest.tasks[spec.artifact_type]
              : undefined;
            if (
              operation.cancelRequested
              || latestTask?.status === "cancelling"
              || latestTask?.status === "cancelled"
            ) return;
            const setupError = isSetupError(error) ? error : null;
            const detail = setupError
              ? setupErrorText(
                  setupError,
                  "API key required - open Settings to paste your OpenRouter key.",
                )
              : error instanceof Error ? error.message : "unknown";
            const failureProgress = context.terminalProgress()
              ?? get().runs_progress[spec.conversation_id];
            const waitOwner = _SSE_WAIT_ABORTS.get(spec.conversation_id);
            if (waitOwner?.runId === descriptor.run_id) {
              waitOwner.abort(error instanceof Error ? error : new Error(detail));
            }
            patchConversation(spec.conversation_id, (child) => {
              if (!isCurrent() || child.run_id !== descriptor.run_id) return child;
              return {
                ...child,
                pending: false,
                run_id: undefined,
                messages: child.messages.map((message) => message.id === placeholderId
                  ? {
                      ...message,
                      run_id: descriptor.run_id,
                      text: `Failed: ${detail}`,
                      status: "error" as const,
                      failure: setupError
                        ? undefined
                        : runClientFailure(error, spec.artifact_type, descriptor.run_id),
                    }
                  : message),
              };
            });
            if (isCurrent()) clearRunProgress(spec.conversation_id, descriptor.run_id);
            let failureApplied = false;
            patchPaperBundleTask(
              parentConversationId,
              spec.artifact_type,
              (task) => {
                if (task.run_id !== descriptor.run_id || task.terminal) return task;
                failureApplied = true;
                return {
                  ...task,
                  ...terminalPaperBundleTaskStats(task, failureProgress),
                  status: "failed",
                  terminal: true,
                  process_free: true,
                  error: detail,
                };
              },
              { ownerScope: requestScope, jobId },
            );
            if (setupError && failureApplied) set({ settings_open: true });
          } finally {
            if (_PAPER_BUNDLE_UPLOADS.get(spec.conversation_id) === upload) {
              _PAPER_BUNDLE_UPLOADS.delete(spec.conversation_id);
            }
          }
        }));
      } catch (error) {
        if (currentDemoUserScope() !== requestScope) return;
        const currentBundle = get().conversations[parentConversationId]?.paper_bundle;
        if (
          operation.cancelRequested
          || currentBundle?.kind !== "parent"
          || currentBundle.job_id !== jobId
        ) return;
        const detail = error instanceof Error ? error.message : "Paper bundle creation failed.";
        for (const childRun of childRuns) {
          patchConversation(childRun.spec.conversation_id, (child) => {
            const bundle = get().conversations[parentConversationId]?.paper_bundle;
            if (
              currentDemoUserScope() !== requestScope
              || bundle?.kind !== "parent"
              || bundle.job_id !== jobId
            ) return child;
            return {
              ...child,
              pending: false,
              messages: child.messages.map((message) => message.id === childRun.placeholderId
                ? {
                    ...message,
                    text: `Failed: ${detail}`,
                    status: "error" as const,
                    failure: connectionLostFailure(detail, childRun.spec.artifact_type),
                  }
                : message),
            };
          });
          patchPaperBundleTask(
            parentConversationId,
            childRun.spec.artifact_type,
            (task) => (
              isActivePaperBundleTaskStatus(task.status)
                ? {
                    ...task,
                    ...terminalPaperBundleTaskStats(task),
                    status: "failed",
                    terminal: true,
                    process_free: true,
                    error: detail,
                  }
                : task
            ),
            { ownerScope: requestScope, jobId },
          );
        }
      } finally {
        clearPaperBundleCancellationIntents(
          requestScope,
          parentConversationId,
          paperBundleJobGeneration(jobId),
        );
        if (_PAPER_BUNDLE_OPERATIONS.get(parentConversationId) === operation) {
          _PAPER_BUNDLE_OPERATIONS.delete(parentConversationId);
        }
      }
    },

    cancelPaperBundleTask: async (requestedParentConversationId, artifactType) => {
      const requestScope = currentDemoUserScope();
      const requestedConversation = get().conversations[requestedParentConversationId];
      const parentConversationId = requestedConversation?.paper_bundle?.kind === "child"
        ? requestedConversation.paper_bundle.parent_conversation_id
        : requestedParentConversationId;
      const parent = get().conversations[parentConversationId];
      if (parent?.paper_bundle?.kind !== "parent") return;
      const parentBundle = parent.paper_bundle;
      const jobId = parentBundle.job_id;
      const generation = paperBundleCancellationGeneration(parentBundle);
      const task = parentBundle.tasks[artifactType];
      if (
        !isActivePaperBundleTaskStatus(task.status)
        || (task.status === "cancelling" && !task.error)
      ) return;
      const child = get().conversations[task.child_conversation_id];
      const runId = task.run_id ?? child?.run_id;
      const progress = get().runs_progress[task.child_conversation_id];
      patchPaperBundleTask(
        parentConversationId,
        artifactType,
        (current) => {
          if (
            !isActivePaperBundleTaskStatus(current.status)
            || (runId && current.run_id && current.run_id !== runId)
          ) {
            return current;
          }
          return {
            ...current,
            ...(runId ? { run_id: runId } : {}),
            status: "cancelling",
            error: undefined,
          };
        },
        { ownerScope: requestScope, jobId },
      );
      if (runId) markRunProgressCancelling(task.child_conversation_id, runId);
      const publicationCancellation = cancelCandidatePublication(
        task.child_conversation_id,
        requestScope,
      );
      if (!runId) {
        _CANCELLED_PAPER_BUNDLE_TASKS.add(
          paperBundleTaskCancellationKey(
            requestScope,
            parentConversationId,
            generation,
            artifactType,
          ),
        );
        const publication = await publicationCancellation;
        patchPaperBundleTask(
          parentConversationId,
          artifactType,
          (current) => ({
            ...current,
            error: publication.confirmed
              ? "Waiting for the server reservation before cancellation."
              : `Cancellation not confirmed: ${publication.error
                ?? "candidate publication may still be stopping."}`,
          }),
          { ownerScope: requestScope, jobId },
        );
        return;
      }

      const [sourceCancellation, publication] = await Promise.all([
        requestExactRunCancellation(
          requestScope,
          task.child_conversation_id,
          runId,
          "paper bundle child cancellation timed out",
        ).then(
          (result) => ({ result }),
          (error: unknown) => ({ error }),
        ),
        publicationCancellation,
      ]);
      if (currentDemoUserScope() !== requestScope) return;

      const result = "result" in sourceCancellation
        ? sourceCancellation.result
        : undefined;
      const sourceConfirmed = Boolean(
        result?.http_status === 200
        && result.confirmed
        && (result.status === "cancelled" || result.status === "already_cancelled")
      );
      const sourceError = "error" in sourceCancellation
        ? sourceCancellation.error
        : undefined;
      const sourceDetail = sourceError instanceof Error
        ? sourceError.message
        : sourceError !== undefined
          ? String(sourceError)
          : result?.status === "already_terminal"
            ? `Run already ${result.run_state}; cancellation was not applied.`
            : "backend may still be stopping.";

      const currentBundle = get().conversations[parentConversationId]?.paper_bundle;
      if (
        currentBundle?.kind !== "parent"
        || currentBundle.job_id !== jobId
        || currentBundle.tasks[artifactType].run_id !== runId
      ) return;

      if (sourceConfirmed) {
        const upload = _PAPER_BUNDLE_UPLOADS.get(task.child_conversation_id);
        if (upload?.runId === runId && !upload.controller.signal.aborted) {
          upload.controller.abort(new RunWaitCancelledError());
        }
        const waitOwner = _SSE_WAIT_ABORTS.get(task.child_conversation_id);
        if (waitOwner?.runId === runId) {
          waitOwner.abort(new RunWaitCancelledError());
        }
        const artifactFetch = _RUN_ARTIFACT_FETCH_OWNERS.get(
          task.child_conversation_id,
        );
        if (artifactFetch?.runId === runId) {
          artifactFetch.controller.abort(new RunWaitCancelledError());
        }
        patchConversation(task.child_conversation_id, (conversation) => {
          const latestBundle = get().conversations[parentConversationId]?.paper_bundle;
          if (
            currentDemoUserScope() !== requestScope
            || latestBundle?.kind !== "parent"
            || latestBundle.job_id !== jobId
            || latestBundle.tasks[artifactType].run_id !== runId
            || (conversation.run_id && conversation.run_id !== runId)
          ) return conversation;
          return {
            ...conversation,
            pending: false,
            run_id: undefined,
            messages: conversation.messages.map((message) => (
              message.role === "assistant"
              && message.status === "streaming"
              && message.task_type !== CANDIDATE_PUBLISH_TASK
              && (!message.run_id || message.run_id === runId)
                ? {
                    ...message,
                    run_id: runId,
                    text: "Run cancelled.",
                    status: "error",
                    failure: {
                      status: "cancelled",
                      produced_files: [],
                      artifact_type: artifactType,
                    },
                  }
                : message
            )),
          };
        });
        clearRunProgress(task.child_conversation_id, runId);
      }

      if (!sourceConfirmed || !publication.confirmed) {
        const detail = !sourceConfirmed
          ? sourceDetail
          : publication.error ?? "candidate publication may still be stopping.";
        patchPaperBundleTask(
          parentConversationId,
          artifactType,
          (current) => (
            current.run_id !== runId || current.status !== "cancelling"
              ? current
              : {
                  ...current,
                  error: `Cancellation not confirmed: ${detail}`,
                }
          ),
          { ownerScope: requestScope, jobId },
        );
        return;
      }
      patchPaperBundleTask(
        parentConversationId,
        artifactType,
        (current) => (
          current.run_id !== runId
            ? current
            : {
                ...current,
                ...terminalPaperBundleTaskStats(current, progress),
                status: "cancelled",
                terminal: true,
                process_free: true,
                error: "Run cancelled.",
              }
        ),
        { ownerScope: requestScope, jobId },
      );
      _CANCELLED_PAPER_BUNDLE_TASKS.delete(
        paperBundleTaskCancellationKey(
          requestScope,
          parentConversationId,
          generation,
          artifactType,
        ),
      );
    },

    retryPaperBundleTask: async (requestedParentConversationId, artifactType) => {
      const requestedConversation = get().conversations[requestedParentConversationId];
      const parentConversationId = requestedConversation?.paper_bundle?.kind === "child"
        ? requestedConversation.paper_bundle.parent_conversation_id
        : requestedParentConversationId;
      const parent = get().conversations[parentConversationId];
      if (parent?.paper_bundle?.kind !== "parent") return;
      const task = parent.paper_bundle.tasks[artifactType];
      if (
        (task.status !== "failed" && task.status !== "cancelled")
        || !task.run_id
      ) {
        return;
      }
      const child = get().conversations[task.child_conversation_id];
      const failedMessage = [...(child?.messages ?? [])].reverse().find(
        (message) => runIdFromMessage(message) === task.run_id,
      );
      if (!failedMessage) {
        patchPaperBundleTask(parentConversationId, artifactType, (current) => ({
          ...current,
          error: "The failed run can no longer be retried. Start a new artifact run.",
        }));
        return;
      }
      if (failedMessage.failure?.status === "artifact_delivery_failed") {
        patchPaperBundleTask(parentConversationId, artifactType, (current) => ({
          ...current,
          status: "running",
          terminal: false,
          error: undefined,
          started_at: Date.now(),
          finished_at: undefined,
        }));
        await get().resumeRun(failedMessage.id);
        const updatedChild = get().conversations[task.child_conversation_id];
        const resultMessage = [...(updatedChild?.messages ?? [])].reverse().find(
          (message) => runIdFromMessage(message) === task.run_id,
        );
        if (resultMessage) {
          const resultArtifact = resultMessage.artifact_id
            ? updatedChild?.artifacts[resultMessage.artifact_id]
            : undefined;
          applyPaperBundleResult(
            parentConversationId,
            artifactType,
            task.run_id,
            { message: resultMessage, artifact: resultArtifact ?? null },
          );
        }
        return;
      }
      if (parent.paper_bundle.job_id) return;
      const authoringRunId = artifactType === "video"
        ? task.authoring_run_id
          ?? failedMessage.failure?.parent_run_id
          ?? task.run_id
        : undefined;

      patchPaperBundleTask(parentConversationId, artifactType, (current) => ({
        ...current,
        status: "running",
        ...(authoringRunId ? { authoring_run_id: authoringRunId } : {}),
        error: undefined,
        started_at: Date.now(),
        finished_at: undefined,
      }));
      let ownedRunId = task.run_id;
      const resultRunId = await get().retryRun(
        failedMessage.id,
        undefined,
        artifactType === "video",
        authoringRunId,
        (runId) => {
          let accepted = false;
          patchPaperBundleTask(parentConversationId, artifactType, (current) => {
            if (current.status !== "running" || current.run_id !== ownedRunId) {
              return current;
            }
            accepted = true;
            return { ...current, run_id: runId };
          });
          if (accepted) ownedRunId = runId;
        },
      );
      if (!resultRunId || ownedRunId !== resultRunId) return;

      const updatedChild = get().conversations[task.child_conversation_id];
      const resultMessage = [...(updatedChild?.messages ?? [])].reverse().find(
        (message) => (
          message.role === "assistant"
          && runIdFromMessage(message) === resultRunId
        ),
      );
      if (!resultMessage) {
        patchPaperBundleTask(parentConversationId, artifactType, (current) =>
          current.status !== "running" || current.run_id !== resultRunId
            ? current
            : {
                ...current,
                status: "failed",
                finished_at: Date.now(),
                error: "Retry finished without a result.",
              }
        );
        return;
      }
      const artifact = resultMessage.artifact_id
        ? updatedChild?.artifacts[resultMessage.artifact_id]
        : updatedChild?.active_artifact_id
          ? updatedChild.artifacts[updatedChild.active_artifact_id]
          : undefined;
      applyPaperBundleResult(
        parentConversationId,
        artifactType,
        resultRunId,
        { message: resultMessage, artifact: artifact ?? null },
        get().runs_progress[task.child_conversation_id],
      );
    },

    cancelPaperBundle: async (requestedParentConversationId) => {
      const requestScope = currentDemoUserScope();
      const requestedId = requestedParentConversationId ?? get().current_conversation_id;
      const requestedConversation = get().conversations[requestedId];
      const parentConversationId = requestedConversation?.paper_bundle?.kind === "child"
        ? requestedConversation.paper_bundle.parent_conversation_id
        : requestedId;
      const parent = get().conversations[parentConversationId];
      if (parent?.paper_bundle?.kind !== "parent") return;
      const parentBundle = parent.paper_bundle;
      if (parentBundle.job_id) {
        if (isTerminalPaperBundleBackendState(parentBundle.backend_state)) return;
        const jobId = parentBundle.job_id;
        const backendOwnerId = paperBundleBackendOwnerId(requestScope, get().backend_info);
        const cancelRequestKey = paperBundleCancelRequestKey(requestScope, jobId);
        const existing = _PAPER_BUNDLE_CANCEL_REQUESTS.get(cancelRequestKey);
        if (existing) return existing;
        const operation = _PAPER_BUNDLE_OPERATIONS.get(parentConversationId);
        if (operation?.jobId === jobId && operation.ownerScope === requestScope) {
          operation.cancelRequested = true;
        }
        const bundleMatchesRequest = (
          bundle: PaperBundleState | undefined,
        ): bundle is Extract<PaperBundleState, { kind: "parent" }> => (
          currentDemoUserScope() === requestScope
          && bundle?.kind === "parent"
          && bundle.job_id === jobId
        );
        patchConversation(parentConversationId, (conversation) => {
          const bundle = conversation.paper_bundle;
          if (!bundleMatchesRequest(bundle)) return conversation;
          const tasks: Record<ArtifactType, PaperBundleTask> = { ...bundle.tasks };
          for (const artifactType of PAPER_BUNDLE_ARTIFACT_ORDER) {
            const task = tasks[artifactType];
            if (
              task.status === "pending"
              || task.status === "uploading"
              || task.status === "running"
            ) {
              tasks[artifactType] = { ...task, status: "cancelling", error: undefined };
            }
          }
          return {
            ...conversation,
            pending: true,
            paper_bundle: {
              ...bundle,
              backend_state: "cancelling",
              cancel_error: undefined,
              cancel_request_in_flight: true,
              tasks: tasks as PaperBundleTaskMap,
            },
          };
        });
        set((state) => {
          const bundle = state.conversations[parentConversationId]?.paper_bundle;
          if (!bundleMatchesRequest(bundle)) return state;
          const runsProgress = { ...state.runs_progress };
          for (const task of Object.values(parentBundle.tasks)) {
            const progress = runsProgress[task.child_conversation_id];
            if (progress && (!task.run_id || progress.run_id === task.run_id)) {
              runsProgress[task.child_conversation_id] = {
                ...progress,
                phase: "cancelling",
                label: "Stopping bundle…",
              };
            }
          }
          return { runs_progress: runsProgress };
        });
        const publicationCancellations = Promise.all(
          Object.values(parentBundle.tasks)
            .map((task) => cancelCandidatePublication(
              task.child_conversation_id,
              requestScope,
            )),
        );

        const request = (async () => {
          const controller = new AbortController();
          const timeout = window.setTimeout(
            () => controller.abort(new Error("paper bundle cancellation timed out")),
            CANCELLATION_REQUEST_TIMEOUT_MS,
          );
          let response: PaperBundleCancelResponse;
          try {
            response = await cancelPaperBundleRequest(jobId, controller.signal);
          } catch (error) {
            await publicationCancellations;
            if (currentDemoUserScope() !== requestScope) return;
            if (
              operation?.jobId === jobId
              && operation.ownerScope === requestScope
              && !operation.createDispatched
            ) {
              patchConversation(parentConversationId, (conversation) => {
                const bundle = conversation.paper_bundle;
                if (!bundleMatchesRequest(bundle)) return conversation;
                const tasks: Record<ArtifactType, PaperBundleTask> = { ...bundle.tasks };
                for (const artifactType of PAPER_BUNDLE_ARTIFACT_ORDER) {
                  const task = tasks[artifactType];
                  if (isActivePaperBundleTaskStatus(task.status)) {
                    tasks[artifactType] = {
                      ...task,
                      ...terminalPaperBundleTaskStats(task),
                      status: "cancelled",
                      terminal: true,
                      process_free: true,
                      error: "Run cancelled.",
                    };
                  }
                }
                return {
                  ...conversation,
                  pending: false,
                  paper_bundle: {
                    ...bundle,
                    backend_state: "cancelled",
                    cancel_error: undefined,
                    cancel_request_in_flight: false,
                    tasks: tasks as PaperBundleTaskMap,
                  },
                };
              });
              return;
            }
            patchConversation(parentConversationId, (conversation) => {
              const bundle = conversation.paper_bundle;
              if (!bundleMatchesRequest(bundle)) return conversation;
              if (isTerminalPaperBundleBackendState(bundle.backend_state)) return conversation;
              return {
                ...conversation,
                paper_bundle: {
                  ...bundle,
                  backend_state: "cancelling",
                  cancel_request_in_flight: false,
                  cancel_error: error instanceof Error
                    ? `Cancellation not confirmed: ${error.message}`
                    : "Cancellation not confirmed; backend may still be stopping.",
                },
              };
            });
            return;
          } finally {
            window.clearTimeout(timeout);
          }
          if (currentDemoUserScope() !== requestScope) return;
          if (response.owner_id !== undefined && response.owner_id !== backendOwnerId) {
            await publicationCancellations;
            patchConversation(parentConversationId, (conversation) => {
              const bundle = conversation.paper_bundle;
              if (!bundleMatchesRequest(bundle)) return conversation;
              return {
                ...conversation,
                paper_bundle: {
                  ...bundle,
                  cancel_request_in_flight: false,
                  cancel_error: "Cancellation not confirmed: response owner mismatch.",
                },
              };
            });
            return;
          }
          if (!response.confirmed) {
            patchConversation(parentConversationId, (conversation) => {
              const bundle = conversation.paper_bundle;
              if (!bundleMatchesRequest(bundle)) return conversation;
              if (isTerminalPaperBundleBackendState(bundle.backend_state)) return conversation;
              return {
                ...conversation,
                pending: true,
                paper_bundle: {
                  ...bundle,
                  backend_state: "cancelling",
                  revision: response.revision ?? bundle.revision,
                  cancel_error: undefined,
                  cancel_request_in_flight: true,
                },
              };
            });
            const latestBundle = get().conversations[parentConversationId]?.paper_bundle;
            if (
              bundleMatchesRequest(latestBundle)
              && isTerminalPaperBundleBackendState(latestBundle.backend_state)
            ) {
              await publicationCancellations;
              return;
            }
            try {
              const terminal = await pollPaperBundleCancellation(
                jobId,
                requestScope,
                backendOwnerId,
                response.pending_creation === true,
              );
              response = "confirmed" in terminal
                ? terminal
                : {
                    http_status: 200,
                    job_id: jobId,
                    owner_id: terminal.owner_id,
                    state: terminal.state,
                    status: terminal.state === "cancelled" ? "cancelled" : "already_terminal",
                    confirmed: true,
                    children: terminal.children,
                    publications: terminal.publications,
                    revision: terminal.revision,
                  };
            } catch (error) {
              await publicationCancellations;
              if (currentDemoUserScope() !== requestScope) return;
              patchConversation(parentConversationId, (conversation) => {
                const bundle = conversation.paper_bundle;
                if (!bundleMatchesRequest(bundle)) return conversation;
                if (isTerminalPaperBundleBackendState(bundle.backend_state)) return conversation;
                return {
                  ...conversation,
                  pending: true,
                  paper_bundle: {
                    ...bundle,
                    backend_state: "cancelling",
                    cancel_request_in_flight: false,
                    cancel_error: error instanceof Error
                      ? `Cancellation not confirmed: ${error.message}`
                      : "Cancellation not confirmed; backend may still be stopping.",
                  },
                };
              });
              return;
            }
          }

          const publicationResults = await publicationCancellations;
          const unconfirmedPublication = publicationResults.find(
            (publication) => !publication.confirmed,
          );
          if (unconfirmedPublication) {
            patchConversation(parentConversationId, (conversation) => {
              const bundle = conversation.paper_bundle;
              if (!bundleMatchesRequest(bundle)) return conversation;
              const tasks: Record<ArtifactType, PaperBundleTask> = {
                ...bundle.tasks,
              };
              for (const artifactType of PAPER_BUNDLE_ARTIFACT_ORDER) {
                const task = tasks[artifactType];
                const child = get().conversations[task.child_conversation_id];
                if (
                  child
                  && hasActiveCandidatePublication(child.id)
                ) {
                  tasks[artifactType] = {
                    ...task,
                    status: "cancelling",
                    error: `Cancellation not confirmed: ${unconfirmedPublication.error
                      ?? "candidate publication may still be stopping."}`,
                  };
                }
              }
              return {
                ...conversation,
                pending: true,
                paper_bundle: {
                  ...bundle,
                  backend_state: "cancelling",
                  cancel_request_in_flight: false,
                  cancel_error: `Cancellation not confirmed: ${unconfirmedPublication.error
                    ?? "candidate publication may still be stopping."}`,
                  tasks: tasks as PaperBundleTaskMap,
                },
              };
            });
            return;
          }

          for (const upload of _PAPER_BUNDLE_UPLOADS.values()) {
            if (
              upload.parentConversationId === parentConversationId
              && !upload.controller.signal.aborted
            ) {
              upload.controller.abort(new RunWaitCancelledError());
            }
          }

          let terminalApplied = false;
          patchConversation(parentConversationId, (conversation) => {
            const bundle = conversation.paper_bundle;
            if (
              !bundleMatchesRequest(bundle)
              || (response.owner_id !== undefined && response.owner_id !== backendOwnerId)
            ) return conversation;
            if (isTerminalPaperBundleBackendState(bundle.backend_state)) return conversation;
            if (
              (bundle.revision !== undefined && response.revision === undefined)
              || (
                bundle.revision !== undefined
                && response.revision !== undefined
                && response.revision < bundle.revision
              )
            ) {
              return conversation;
            }
            const tasks: Record<ArtifactType, PaperBundleTask> = { ...bundle.tasks };
            for (const artifactType of PAPER_BUNDLE_ARTIFACT_ORDER) {
              const task = tasks[artifactType];
              const descriptor = response.children[artifactType];
              const publication = response.publications?.[artifactType];
              const status = publication
                ? "complete"
                : descriptor
                ? paperBundleTaskStatusFromBackend(descriptor.state)
                : task.status === "complete" || task.status === "failed"
                  ? task.status
                  : "cancelled";
              tasks[artifactType] = {
                ...task,
                ...(publication
                  ? {
                      run_id: publication.publication_run_id,
                      authoring_run_id: publication.source_run_id,
                      artifact_id: publication.artifact_id,
                    }
                  : descriptor ? { run_id: descriptor.run_id } : {}),
                ...(status === "complete"
                  ? { status, error: undefined }
                  : status === "failed"
                    ? { status, error: task.error || "Run failed." }
                    : { status: "cancelled", error: "Run cancelled." }),
                ...terminalPaperBundleTaskStats(task),
                terminal: true,
                process_free: true,
              };
            }
            terminalApplied = true;
            return {
              ...conversation,
              pending: false,
              paper_bundle: {
                ...bundle,
                backend_state: response.state,
                revision: response.revision ?? bundle.revision,
                cancel_error: undefined,
                cancel_request_in_flight: false,
                tasks: tasks as PaperBundleTaskMap,
              },
            };
          });
          if (!terminalApplied) {
            patchConversation(parentConversationId, (conversation) => {
              const bundle = conversation.paper_bundle;
              if (
                !bundleMatchesRequest(bundle)
                || isTerminalPaperBundleBackendState(bundle.backend_state)
              ) {
                return conversation;
              }
              return {
                ...conversation,
                pending: true,
                paper_bundle: {
                  ...bundle,
                  cancel_request_in_flight: false,
                  cancel_error: "Cancellation not confirmed; a newer backend state is active.",
                },
              };
            });
            return;
          }
          const latest = get().conversations[parentConversationId]?.paper_bundle;
          if (!bundleMatchesRequest(latest)) return;
          for (const artifactType of PAPER_BUNDLE_ARTIFACT_ORDER) {
            const task = latest.tasks[artifactType];
            if (task.status === "complete") continue;
            const runId = task.run_id;
            if (runId) {
              const waitOwner = _SSE_WAIT_ABORTS.get(task.child_conversation_id);
              if (waitOwner?.runId === runId) {
                waitOwner.abort(new RunWaitCancelledError());
              }
              const artifactFetch = _RUN_ARTIFACT_FETCH_OWNERS.get(
                task.child_conversation_id,
              );
              if (artifactFetch?.runId === runId) {
                artifactFetch.controller.abort(new RunWaitCancelledError());
              }
            }
            patchConversation(task.child_conversation_id, (child) => {
              if (!bundleMatchesRequest(
                get().conversations[parentConversationId]?.paper_bundle,
              )) return child;
              return {
                ...child,
                pending: false,
                run_id: undefined,
                messages: child.messages.map((message) =>
                  message.role === "assistant"
                  && message.status === "streaming"
                  && message.task_type !== CANDIDATE_PUBLISH_TASK
                  && (!message.run_id || message.run_id === runId)
                    ? {
                        ...message,
                        run_id: runId,
                        text: task.status === "cancelled" ? "Run cancelled." : message.text,
                        status: "error",
                        ...(task.status === "cancelled"
                          ? {
                              failure: {
                                status: "cancelled",
                                produced_files: [],
                                artifact_type: artifactType,
                              },
                            }
                          : {}),
                      }
                    : message
                ),
              };
            });
            clearRunProgress(task.child_conversation_id, runId);
          }
          clearPaperBundleCancellationIntents(
            requestScope,
            parentConversationId,
            paperBundleJobGeneration(jobId),
          );
        })();
        _PAPER_BUNDLE_CANCEL_REQUESTS.set(cancelRequestKey, request);
        try {
          await request;
        } finally {
          if (_PAPER_BUNDLE_CANCEL_REQUESTS.get(cancelRequestKey) === request) {
            _PAPER_BUNDLE_CANCEL_REQUESTS.delete(cancelRequestKey);
          }
        }
        return;
      }
      const activeTasks = PAPER_BUNDLE_ARTIFACT_ORDER
        .map((artifactType) => parentBundle.tasks[artifactType])
        .filter((task): task is PaperBundleTask =>
          !!task && isActivePaperBundleTaskStatus(task.status)
      );
      if (activeTasks.length === 0) return;
      const generation = paperBundleCancellationGeneration(parentBundle);
      const parentCancellationKey = paperBundleParentCancellationKey(
        requestScope,
        parentConversationId,
        generation,
      );
      _CANCELLED_PAPER_BUNDLE_PARENTS.add(parentCancellationKey);
      try {
        await Promise.allSettled(
          activeTasks.map((task) =>
            get().cancelPaperBundleTask(parentConversationId, task.artifact_type)
          ),
        );
      } finally {
        _CANCELLED_PAPER_BUNDLE_PARENTS.delete(parentCancellationKey);
      }
    },

    sendMessage: async (text, attachments, options = {}) => {
      const intent = get().intent_type;
      const trimmed = text.trim();
      const convId = get().current_conversation_id;
      get().clearCanvasValidationError(convId);
      const baselineBeforeSend = get().conversations[convId];
	      const baseline = baselineBeforeSend?.active_artifact_id
	        ? baselineBeforeSend.artifacts[baselineBeforeSend.active_artifact_id]
	        : undefined;
	      const canUsePosterCodeEditor = !!(
	        attachments.length === 0
	        && baseline
	        && baseline.artifact_type === "poster"
	        && baseline.native_format === "html"
	        && baseline.native_file_url
	      );
	      if (!canUsePosterCodeEditor && get().area_revision_items.length > 0) {
	        set({
	          area_revision_active: false,
	          area_revision_items: [],
	          area_revision_focus_id: null,
	        });
	      }
	      const areaInstructionBrief = canUsePosterCodeEditor
	        ? buildAreaInstructionBrief(get().area_revision_items)
	        : "";
      const brief = trimmed || areaInstructionBrief || (intent ? DEFAULT_BRIEF[intent] : "");
      if (!brief && attachments.length === 0) return;

	      const requestedArtifactType = effectiveArtifactType(intent, brief);
	      const submissionAttachments = attachmentsForReferencePosterSubmission(
	        attachments,
	        requestedArtifactType,
	      );
      const shouldUsePosterCodeEditor = !!(
        requestedArtifactType === "poster" && canUsePosterCodeEditor
      );
		      const selectionContext = shouldUsePosterCodeEditor
		        ? options.selection_context ?? buildAreaSelectionContext(get().area_revision_items)
		        : undefined;
		      const selectionSummary = buildAreaSelectionSummaryFromContext(selectionContext);
		      const taskType: RecoverableTaskType = shouldUsePosterCodeEditor
		        ? POSTER_CODE_EDIT_TASK
		        : GENERATE_TASK;
		      const posterRequest = requestedArtifactType === "poster";
		      const canvasSelection = posterRequest
		        ? posterCanvasRequestSelection(
		            get().poster_canvas_presets,
		            baselineBeforeSend?.poster_canvas_preset_id,
		          )
		        : null;
		      const posterPaletteId = posterRequest
		        ? baselineBeforeSend?.poster_palette_id?.trim() || undefined
		        : undefined;
		      if (posterRequest && !posterPaletteId) {
		        throw new Error("Select a palette before creating or revising a poster.");
		      }

	      // Strip File handles when storing in chat history — they're only
	      // needed for the multipart upload, not for re-rendering past msgs.
	      const attachments_for_history: Attachment[] = submissionAttachments.map((a) => ({
	        id: a.id,
		        name: a.name,
	        size: a.size,
	        kind: a.kind,
	        role: a.role,
	      }));
	      const {
	        content: contentAttachmentRefs,
	        reference: referencePosterRef,
	      } = partitionReferenceAttachments(attachments_for_history);
		      let taskPayload: MessageTaskPayload = shouldUsePosterCodeEditor
		        ? {
		            artifact_type: "poster",
		            source_artifact_id: baseline?.artifact_id,
		            selection_context: selectionContext ?? null,
		            palette_id: posterPaletteId,
		            canvas_preset_id: canvasSelection?.canvas_preset_id,
		          }
		        : {
		            artifact_type: requestedArtifactType,
		            authoring_max_attempts: options.authoring_max_attempts,
		            template: canvasSelection?.template,
		            canvas_preset_id: canvasSelection?.canvas_preset_id,
		            baseline_artifact_id: baseline?.artifact_id,
	            attachment_refs: contentAttachmentRefs.length ? contentAttachmentRefs : undefined,
	            reference_poster_ref: referencePosterRef,
		            ...(posterRequest ? { palette_id: posterPaletteId } : {}),
		          };

		      const userMsg: Message = {
	        id: nextId("msg"),
	        role: "user",
	        ts: Date.now(),
	        text: brief,
		        attachments: attachments_for_history,
		        status: "done",
		        selection_summary: selectionSummary,
		        task_type: taskType,
		        task_payload: taskPayload,
		        source_artifact_id: taskPayload.source_artifact_id,
		      };
	      const placeholderId = nextId("msg");

	      patchConversation(convId, (c) => ({
	        ...c,
	        title:
	          c.messages.length === 0 && brief
	            ? brief.slice(0, 50)
	            : c.title,
	        pending: true,
	        messages: [
	          ...c.messages,
	          userMsg,
	          {
	            id: placeholderId,
	            role: "assistant",
		            text: "",
		            ts: Date.now(),
		            status: "streaming",
		            task_type: taskType,
		            task_payload: taskPayload,
		            source_artifact_id: taskPayload.source_artifact_id,
		          },
		        ],
		      }));
	      set({ intent_type: null });

	      // Conversation memory: send the last few user+assistant turns and
	      // a compact summary of the artifacts already produced in this
	      // thread. Backend stitches both into the brief preamble.
	      const memory = buildConversationMemory(get().conversations[convId]);
	      let activeRunId: string | undefined;
	      const reservedUploadController = new AbortController();
	      let reservedTerminal: Promise<
	        { event: string; error?: never } | { event?: never; error: unknown }
	      > | undefined;
	      const cleanupReservedRun = (runId: string) => {
	        const uploadOwner = _RESERVED_RUN_UPLOAD_ABORTS.get(convId);
	        if (uploadOwner?.runId === runId) {
	          _RESERVED_RUN_UPLOAD_ABORTS.delete(convId);
	        }
	        _AUTHORITATIVE_RUN_CANCELLATIONS.delete(runOperationKey(convId, runId));
	      };
	      const listenToReservedRun = (runId: string) => {
	        activeRunId = runId;
	        _RESERVED_RUN_UPLOAD_ABORTS.set(convId, {
	          runId,
	          controller: reservedUploadController,
	        });
	        patchConversation(convId, (c) => ({
	          ...c,
	          run_id: runId,
	          messages: c.messages.map((message) => (
	            message.id === placeholderId
	              ? { ...message, run_id: runId }
	              : message
	          )),
	        }));
	        set((s) => ({
	          runs_progress: {
	            ...s.runs_progress,
	            [convId]: initialProgress(runId),
	          },
	        }));
	        reservedTerminal = waitForRunTerminal({
	          convId,
	          runId,
	          terminalEvents: ["run.done", "run.error", "run.cancelled"],
	          timeoutMs: 60 * 60 * 1000,
	          timeoutMessage: "run still running after 60 min — check the backend logs",
	          closedMessage:
	            "event stream closed — the backend may have crashed or been restarted. "
	            + "Check the uvicorn terminal for the cause.",
	        }).then(
	          (event) => {
	            if (event === "run.cancelled") {
	              _AUTHORITATIVE_RUN_CANCELLATIONS.add(runOperationKey(convId, runId));
	              if (!reservedUploadController.signal.aborted) {
	                reservedUploadController.abort(new RunWaitCancelledError());
	              }
	            }
	            return { event };
	          },
	          (error: unknown) => ({ error }),
	        );
	      };
	      const finishGenerateRun = async (runId: string) => {
	        const res = await fetchRunArtifactAfterTerminal(convId, runId);
	        patchConversation(convId, (c) => {
	          if (c.run_id !== runId) return c;
	          const messages = c.messages.map((m) =>
	            m.id === placeholderId
	              ? {
	                  ...res.message,
	                  id: m.id,
	                  run_id: runId,
	                  task_type: taskType,
	                  task_payload: taskPayload,
	                  source_artifact_id: taskPayload.source_artifact_id,
	                }
	              : m
	          );
	          const next: Conversation = {
	            ...c,
	            messages,
	            pending: false,
	            run_id: undefined,
	          };
	          if (!res.artifact) return next;
	          const withArtifact: Conversation = {
	            ...next,
	            artifacts: {
	              ...c.artifacts,
	              [res.artifact.artifact_id]: res.artifact,
	            },
	          };
	          const active = c.active_artifact_id
	            ? c.artifacts[c.active_artifact_id]
	            : undefined;
	          return active?.attempt_lineage?.source_run_id === runId
	            ? withArtifact
	            : { ...withArtifact, active_artifact_id: res.artifact.artifact_id };
	        });
	        set((s) => {
	          if (s.runs_progress[convId]?.run_id !== runId) return s;
	          const next = { ...s.runs_progress };
	          delete next[convId];
	          return { runs_progress: next };
	        });
	        cleanupReservedRun(runId);
	      };

	      try {
	        let requestBaseline = baseline;
	        let requestMemory = memory;
	        if (shouldUsePosterCodeEditor) {
	          await get().flushAutoSave();
	          const afterSave = get();
	          if (afterSave.autosave_state === "error") {
	            throw new Error(afterSave.autosave_error || "Save failed before revision");
	          }
	          const refreshedConv = afterSave.conversations[convId];
	          requestBaseline = refreshedConv?.active_artifact_id
	            ? refreshedConv.artifacts[refreshedConv.active_artifact_id]
	            : undefined;
		          if (!requestBaseline) {
		            throw new Error("Active poster artifact disappeared before revision");
		          }
		          requestMemory = buildConversationMemory(refreshedConv);
		          taskPayload = {
		            ...taskPayload,
		            source_artifact_id: requestBaseline.artifact_id,
		            selection_context: selectionContext ?? null,
		          };
		          patchConversation(convId, (c) => ({
		            ...c,
		            messages: c.messages.map((m) =>
		              m.id === userMsg.id || m.id === placeholderId
		                ? {
		                    ...m,
		                    task_payload: taskPayload,
		                    source_artifact_id: taskPayload.source_artifact_id,
		                  }
		                : m
		            ),
		          }));
		        }
	        // Step 1 — POST a run request, returns run_id immediately.
	        const ack = shouldUsePosterCodeEditor
	          ? await startPosterCodeEdit({
	              artifact: requestBaseline as Artifact,
	              instruction: brief,
	              conversation_id: convId,
	              conversation_history: requestMemory.history,
	              selection_context: selectionContext,
	              palette_id: posterPaletteId,
	            }, reservedUploadController.signal, listenToReservedRun)
	          : await startGenerate({
	              brief,
		              attachments: submissionAttachments,
		              conversation_id: convId,
		              artifact_type: requestedArtifactType,
		              authoring_max_attempts: taskPayload.authoring_max_attempts,
		              template: canvasSelection?.template,
		              canvas_preset_id: canvasSelection?.canvas_preset_id,
	              palette_id: posterRequest ? posterPaletteId : undefined,
		              baseline_artifact: requestBaseline,
		              conversation_history: requestMemory.history,
	              prior_artifacts: requestMemory.artifacts,
	            }, reservedUploadController.signal, {
	              reserveUploads: true,
	              onReserved: listenToReservedRun,
	            });
	        if (ack.reference_poster_handle && taskPayload.reference_poster_ref) {
	          taskPayload = {
	            ...taskPayload,
	            reference_poster_ref: bindReferencePosterHandle(
	              taskPayload.reference_poster_ref,
	              ack.reference_poster_handle,
	            ),
	          };
	          patchConversation(convId, (c) => ({
	            ...c,
	            messages: c.messages.map((message) => (
	              message.id === userMsg.id || message.id === placeholderId
	                ? { ...message, task_payload: taskPayload }
	                : message
	            )),
	          }));
	        }
	        activeRunId = ack.run_id;
	        if (selectionContext) {
	          set({
	            area_revision_active: false,
	            area_revision_items: [],
	            area_revision_focus_id: null,
	          });
	        }
	        // Reserved uploads already exposed the run and opened SSE before
	        // their first byte was sent. Non-upload flows still start here.
	        if (!reservedTerminal) {
	          patchConversation(convId, (c) => ({
	            ...c,
	            run_id: ack.run_id,
	            messages: c.messages.map((message) => (
	              message.id === placeholderId
	                ? { ...message, run_id: ack.run_id }
	                : message
	            )),
	          }));
	          set((s) => ({
	            runs_progress: {
	              ...s.runs_progress,
	              [convId]: initialProgress(ack.run_id, ack.progress_mode),
	            },
	          }));
	        } else {
	          if (get().conversations[convId]?.run_id !== ack.run_id) {
	            throw new RunWaitCancelledError();
	          }
	          if (ack.progress_mode) {
	            set((s) => {
	              const progress = s.runs_progress[convId];
	              return progress?.run_id === ack.run_id
	                ? {
	                    runs_progress: {
	                      ...s.runs_progress,
	                      [convId]: { ...progress, mode: ack.progress_mode },
	                    },
	                  }
	                : s;
	            });
	          }
	        }

        // Step 2 — open SSE on /api/runs/{id}/events. The shared helper
        // drives the per-conversation progress reducer and resolves on
        // terminal backend events.
        const terminalResult = reservedTerminal
          ? await reservedTerminal
          : {
              event: await waitForRunTerminal({
                convId,
                runId: ack.run_id,
                terminalEvents: ["run.done", "run.error", "run.cancelled"],
                timeoutMs: 60 * 60 * 1000,
                timeoutMessage: "run still running after 60 min — check the backend logs",
                closedMessage:
                  "event stream closed — the backend may have crashed or been restarted. "
                  + "Check the uvicorn terminal for the cause.",
              }),
            };
        if ("error" in terminalResult) throw terminalResult.error;

        // Step 3 — fetch the final artifact + assistant message.
	      await finishGenerateRun(ack.run_id);
	      } catch (err) {
	      let failure: unknown = err;
	      if (
	        failure instanceof RunStartAmbiguousError
	        && activeRunId === failure.runId
	        && reservedTerminal
	        && get().conversations[convId]?.run_id === activeRunId
	      ) {
	        const waitOwner = _SSE_WAIT_ABORTS.get(convId);
	          if (waitOwner?.runId === activeRunId) {
	            waitOwner.reconcile(failure.retryStart);
	          }
	        const terminalResult = await reservedTerminal;
	        if (!("error" in terminalResult)) {
	          await finishGenerateRun(activeRunId);
	          return;
	        }
	        failure = terminalResult.error;
	      }
	      const progress = activeRunId ? get().runs_progress[convId] : undefined;
	      const authoritativeCancellation = activeRunId
	        ? _AUTHORITATIVE_RUN_CANCELLATIONS.has(runOperationKey(convId, activeRunId))
	        : false;
	      if (
	        activeRunId
	        && reservedTerminal
	        && progress?.run_id === activeRunId
	        && (progress.phase === "cancelling" || authoritativeCancellation)
	        && get().conversations[convId]?.run_id === activeRunId
	      ) {
	        const terminalResult = await reservedTerminal;
	        if (!("error" in terminalResult)) {
	          await finishGenerateRun(activeRunId);
	          return;
	        }
	        failure = terminalResult.error;
	      }
	      if (isCanvasValidationError(failure)) {
	        patchConversation(convId, (conversation) => ({
	          ...conversation,
	          pending: false,
	          run_id: undefined,
	          messages: conversation.messages.filter((message) => (
	            message.id !== userMsg.id && message.id !== placeholderId
	          )),
	        }));
	        set((state) => {
	          const progress = { ...state.runs_progress };
	          delete progress[convId];
	          return {
	            runs_progress: progress,
	            canvas_validation_errors: {
	              ...state.canvas_validation_errors,
	              [convId]: { brief, message: failure.message },
	            },
	          };
	        });
	        if (activeRunId) cleanupReservedRun(activeRunId);
	        return;
	      }
	      const waitOwner = activeRunId ? _SSE_WAIT_ABORTS.get(convId) : undefined;
	      if (waitOwner && waitOwner.runId === activeRunId) {
	        waitOwner.abort(failure instanceof Error ? failure : new Error("Run failed."));
	      }
        const setupError = isSetupError(failure) ? failure : null;
        // SSE no longer rejects on run.cancelled / run.error — those go
        // through the resolve path so we can fetch the structured
        // failure. catch() is now reserved for genuine wire errors:
        // 412 no-key, network failure, SSE 60-min timeout.
        patchConversation(convId, (c) => {
          if (activeRunId && c.run_id !== activeRunId) return c;
          const cancelled = failure instanceof RunWaitCancelledError
            || (
              activeRunId !== undefined
              && _AUTHORITATIVE_RUN_CANCELLATIONS.has(runOperationKey(convId, activeRunId))
            );
          return {
            ...c,
            pending: false,
            run_id: undefined,
            messages: c.messages.map((m) =>
              m.id === placeholderId
                ? {
                    ...m,
                    run_id: activeRunId,
		                    text: cancelled
                      ? "Run cancelled."
                      : setupError
		                      ? setupErrorText(setupError, "API key required — open Settings to paste your OpenRouter key.")
		                      : `Failed: ${failure instanceof Error ? failure.message : "unknown"}`,
		                    status: "error",
		                    task_type: taskType,
		                    task_payload: taskPayload,
		                    source_artifact_id: taskPayload.source_artifact_id,
		                    failure: setupError
                      ? undefined
                      : cancelled
                        ? { status: "cancelled", produced_files: [], artifact_type: requestedArtifactType }
                        : runClientFailure(failure, requestedArtifactType, activeRunId),
                  }
                : m
            ),
          };
        });
        set((s) => {
          if (activeRunId && s.runs_progress[convId]?.run_id !== activeRunId) {
            return { settings_open: setupError ? true : s.settings_open };
          }
          const next = { ...s.runs_progress };
          delete next[convId];
          return {
            runs_progress: next,
            settings_open: setupError ? true : s.settings_open,
          };
        });
	      if (activeRunId) cleanupReservedRun(activeRunId);
      }
    },

    exportArtifactPptx: async (artifact_id) => {
      const convId = get().current_conversation_id;
      const conv = get().conversations[convId];
      const artId = artifact_id || conv?.active_artifact_id;
      const art = artId ? conv?.artifacts[artId] : undefined;
      if (!conv || !artId || !art) return;
      if (art.native_format && art.native_format !== "html") return;
      const bundleParent = conv.paper_bundle?.kind === "parent"
        ? conv
        : conv.paper_bundle?.kind === "child"
          ? get().conversations[conv.paper_bundle.parent_conversation_id]
          : undefined;
      if (
        bundleParent?.paper_bundle?.kind === "parent"
        && paperBundleBlocksPptxExport(bundleParent.paper_bundle)
      ) {
        throw new Error("Wait for Paper All-in-One to finish before exporting PowerPoint.");
      }
      const releasePptxOperation = acquirePptxOperation(convId, conv);

      try {
        if (artId === conv.active_artifact_id) {
          await get().flushAutoSave();
          const afterSave = get();
          if (afterSave.autosave_state === "error") {
            throw new Error(afterSave.autosave_error || "Save failed before export");
          }
        }
	      const refreshedConv = get().conversations[convId];
	      const source = cloneArtifact(refreshedConv?.artifacts[artId] ?? art);
	      const userText = `Export this design as an editable PPTX: ${source.name}`;
	      const taskPayload: MessageTaskPayload = {
	        source_artifact_id: artId,
	        export_format: "pptx",
	      };
	      const userMsg: Message = {
	        id: nextId("msg"),
	        role: "user",
	        text: userText,
	        ts: Date.now(),
	        status: "done",
	        task_type: PPTX_EXPORT_TASK,
	        task_payload: taskPayload,
	        source_artifact_id: artId,
	      };
      const placeholderId = nextId("msg");
      patchConversation(convId, (c) => ({
        ...c,
        pending: true,
        messages: [
          ...c.messages,
          userMsg,
          {
            id: placeholderId,
            role: "assistant",
            text: "",
            ts: Date.now(),
	            status: "streaming",
	            task_type: PPTX_EXPORT_TASK,
	            task_payload: taskPayload,
	            source_artifact_id: artId,
	          },
        ],
      }));

      let activeRunId: string | undefined;
      const controller = new AbortController();
      try {
        const exposeReservedExport = (runId: string) => {
          activeRunId = runId;
          _RESERVED_RUN_UPLOAD_ABORTS.set(convId, { runId, controller });
          patchConversation(convId, (c) => ({ ...c, run_id: runId }));
          set((s) => ({
            runs_progress: {
              ...s.runs_progress,
              [convId]: initialProgress(runId, "artifact_export"),
            },
          }));
        };
        const { ack, reconcileImmediately, startReplay } = await resolveReservedRunStart({
          request: startArtifactPptxExport({
            artifact: source,
            conversation_id: convId,
          }, controller.signal, exposeReservedExport),
          reservedRunId: () => activeRunId,
          isCurrent: (runId) => (
            get().conversations[convId]?.run_id === runId
            && _RESERVED_RUN_UPLOAD_ABORTS.get(convId)?.runId === runId
          ),
          placeholderMessage: {
            id: placeholderId,
            role: "assistant",
            text: "",
            ts: Date.now(),
            status: "streaming",
            task_type: PPTX_EXPORT_TASK,
            task_payload: taskPayload,
            source_artifact_id: artId,
          },
          progressMode: "artifact_export",
        });
        activeRunId = ack.run_id;
        ensureRunStillOwned(convId, ack.run_id);
        patchConversation(convId, (c) => ({ ...c, run_id: ack.run_id }));
        set((s) => ({
          runs_progress: {
            ...s.runs_progress,
            [convId]: initialProgress(ack.run_id, ack.progress_mode),
          },
        }));
        await waitForRunTerminal({
          convId,
          runId: ack.run_id,
          terminalEvents: ["run.done", "run.error", "run.cancelled"],
          timeoutMs: 30 * 60 * 1000,
          timeoutMessage: "PowerPoint export still running after 30 min",
          closedMessage: "PowerPoint export event stream closed",
          reconcileImmediately,
          startReplay,
        });
        const res = await fetchRunArtifactAfterTerminal(convId, ack.run_id);
        patchConversation(convId, (c) => c.run_id !== ack.run_id
          ? c
          : {
              ...c,
              pending: false,
              run_id: undefined,
              messages: c.messages.map((m) =>
                m.id === placeholderId
                  ? {
		                    ...res.message,
		                    task_type: PPTX_EXPORT_TASK,
		                    task_payload: taskPayload,
		                    source_artifact_id: artId,
		                  }
                  : m
              ),
            }
        );
        if (res.message.download_url) {
          triggerStoreDownload(
            res.message.download_url,
            res.message.download_filename || `${source.name}.pptx`,
          );
          emitArtifactEvent(convId, "artifact.downloaded", artId, {
            format: "pptx",
            run_id: ack.run_id,
          });
        }
        set((s) => {
          if (s.runs_progress[convId]?.run_id !== ack.run_id) return s;
          const next = { ...s.runs_progress };
          delete next[convId];
          return { runs_progress: next };
        });
      } catch (err) {
        const cancellation = runCancellationDisposition(convId, activeRunId, err);
        if (cancellation === "pending") return;
        const cancelled = cancellation === "confirmed";
        const setupError = isSetupError(err) ? err : null;
        patchConversation(convId, (c) => activeRunId && c.run_id !== activeRunId
          ? c
          : ({
              ...c,
              pending: false,
              run_id: undefined,
              messages: c.messages.map((m) =>
            m.id === placeholderId
              ? {
                  ...m,
                  run_id: activeRunId,
                  text: cancelled
                    ? "Run cancelled."
                    : setupError
                    ? setupErrorText(setupError, "API key required — open Settings to paste your OpenRouter key.")
                    : `PowerPoint export failed: ${err instanceof Error ? err.message : "unknown"}`,
	                  status: "error",
	                  task_type: PPTX_EXPORT_TASK,
	                  task_payload: taskPayload,
	                  source_artifact_id: artId,
                  failure: cancelled
                    ? { status: "cancelled", produced_files: [] }
                    : setupError
                    ? undefined
                    : runClientFailure(err, undefined, activeRunId),
                }
              : m
              ),
            })
        );
        set((s) => {
          if (activeRunId && s.runs_progress[convId]?.run_id !== activeRunId) {
            return { settings_open: setupError ? true : s.settings_open };
          }
          const next = { ...s.runs_progress };
          delete next[convId];
          return {
            runs_progress: next,
            settings_open: setupError ? true : s.settings_open,
          };
        });
      } finally {
        cleanupReservedRunOwner(convId, activeRunId);
      }
      } finally {
        releasePptxOperation();
      }
    },

    retryRun: async (
      message_id,
      designer_override,
      video_export_only = false,
      authoring_run_id,
      on_run_started,
    ) => {
      // Locate the failed message + which conversation it lives in.
      // Walk all conversations rather than assuming current, so the
      // user can retry from a previous chat without switching first.
      let convId = "";
      let oldMsg: Message | undefined;
      for (const c of Object.values(get().conversations)) {
        const m = c.messages.find((x) => x.id === message_id);
        if (m) {
          convId = c.id;
          oldMsg = m;
          break;
        }
      }
      if (!convId || !oldMsg) return;
      const old_run_id = runIdFromMessage(oldMsg);

      // Replace the FailureCard with a streaming placeholder so the
      // user sees motion immediately and the rest of the chat stays
      // intact. The placeholder takes a fresh id so React can animate
      // the swap.
      const placeholderId = nextId("msg");
      patchConversation(convId, (c) => ({
        ...c,
        pending: true,
        messages: c.messages.map((m) =>
          m.id === message_id
            ? {
                id: placeholderId,
                role: "assistant",
                text: "",
                ts: Date.now(),
                status: "streaming",
                task_type: oldMsg.task_type,
                task_payload: oldMsg.task_payload,
                source_artifact_id: oldMsg.source_artifact_id,
              }
            : m
        ),
      }));

      let activeRetryRunId: string | undefined;
      const controller = new AbortController();
      try {
        const exposeReservedRetry = (runId: string) => {
          activeRetryRunId = runId;
          _RESERVED_RUN_UPLOAD_ABORTS.set(convId, { runId, controller });
          on_run_started?.(runId);
          patchConversation(convId, (c) => ({ ...c, run_id: runId }));
          set((s) => ({
            runs_progress: {
              ...s.runs_progress,
              [convId]: initialProgress(runId),
            },
          }));
        };
        const resolveRetryStart = (
          request: Promise<GenerateAck>,
          progressMode: string,
        ) => resolveReservedRunStart({
          request,
          reservedRunId: () => activeRetryRunId,
          isCurrent: (runId) => (
            get().conversations[convId]?.run_id === runId
            && _RESERVED_RUN_UPLOAD_ABORTS.get(convId)?.runId === runId
          ),
          placeholderMessage: {
            id: placeholderId,
            role: "assistant",
            text: "",
            ts: Date.now(),
            status: "streaming",
            task_type: oldMsg.task_type,
            task_payload: oldMsg.task_payload,
            source_artifact_id: oldMsg.source_artifact_id,
          },
          progressMode,
        });
        // Step 1 — POST /api/runs/{old_run_id}/retry. Backend resumes a
        // validated author checkpoint in place when possible, otherwise
        // it starts a fresh retry from the original inputs.
        let startResolution: {
          ack: GenerateAck;
          reconcileImmediately: boolean;
          startReplay?: RunStartReplay;
        };
        if (video_export_only) {
          try {
            startResolution = await resolveRetryStart(
              retryVideoExportRequest(
                old_run_id,
                convId,
                controller.signal,
                exposeReservedRetry,
              ),
              "video_export",
            );
          } catch (error) {
            if (!(error instanceof ApiError) || error.status !== 422) {
              throw error;
            }
            startResolution = await resolveRetryStart(
              retryRunRequest(
                authoring_run_id || old_run_id,
                designer_override,
                controller.signal,
                exposeReservedRetry,
              ),
              "generate",
            );
          }
        } else {
          startResolution = await resolveRetryStart(
            retryRunRequest(
              old_run_id,
              designer_override,
              controller.signal,
              exposeReservedRetry,
            ),
            "generate",
          );
        }
        let { ack, reconcileImmediately, startReplay } = startResolution;
        const finishRetry = async (
          currentAck: GenerateAck,
          reconcileNow = false,
          retryStart?: RunStartReplay,
        ) => {
          activeRetryRunId = currentAck.run_id;
          ensureRunStillOwned(convId, currentAck.run_id);
          on_run_started?.(currentAck.run_id);
          patchConversation(convId, (c) => ({ ...c, run_id: currentAck.run_id }));
          set((s) => ({
            runs_progress: {
              ...s.runs_progress,
              [convId]: initialProgress(currentAck.run_id, currentAck.progress_mode),
            },
          }));
          await waitForRunTerminal({
            convId,
            runId: currentAck.run_id,
            terminalEvents: ["run.done", "run.error", "run.cancelled"],
            timeoutMs: 60 * 60 * 1000,
            timeoutMessage: "retry still running after 60 min",
            closedMessage: "event stream closed during retry — backend may have crashed",
            reconcileImmediately: reconcileNow,
            startReplay: retryStart,
          });
	          return fetchRunArtifactAfterTerminal(convId, currentAck.run_id);
        };

        let res = await finishRetry(ack, reconcileImmediately, startReplay);
        if (
          video_export_only
          && !res.artifact
          && res.message.failure?.retry_route === "full_authoring"
        ) {
          startResolution = await resolveRetryStart(
            retryRunRequest(
              authoring_run_id || old_run_id,
              designer_override,
              controller.signal,
              exposeReservedRetry,
            ),
            "generate",
          );
          ({ ack, reconcileImmediately, startReplay } = startResolution);
          res = await finishRetry(ack, reconcileImmediately, startReplay);
        }
        patchConversation(convId, (c) => {
          if (c.run_id !== ack.run_id) return c;
          const messages = c.messages.map((m) =>
            m.id === placeholderId ? res.message : m
          );
          const next: Conversation = {
            ...c,
            messages,
            pending: false,
            run_id: undefined,
          };
          if (!res.artifact) return next;
          return {
            ...next,
            artifacts: { ...c.artifacts, [res.artifact.artifact_id]: res.artifact },
            active_artifact_id: res.artifact.artifact_id,
          };
        });
        set((s) => {
          if (s.runs_progress[convId]?.run_id !== ack.run_id) return s;
          const next = { ...s.runs_progress };
          delete next[convId];
          return { runs_progress: next };
        });
        return ack.run_id;
      } catch (err) {
        const cancellation = runCancellationDisposition(convId, activeRetryRunId, err);
        if (cancellation === "pending") return;
        const cancelled = cancellation === "confirmed";
        const setupError = isSetupError(err) ? err : null;
        patchConversation(convId, (c) => activeRetryRunId && c.run_id !== activeRetryRunId
          ? c
          : ({
              ...c,
              pending: false,
              run_id: undefined,
              messages: c.messages.map((m) =>
            m.id === placeholderId
              ? {
                  ...m,
                  run_id: activeRetryRunId,
                  text: cancelled
                    ? "Run cancelled."
                    : setupError
                    ? setupErrorText(setupError, "API key required — open Settings to paste your key.")
                    : `Retry failed: ${err instanceof Error ? err.message : "unknown"}`,
                  status: "error",
                  failure: cancelled
                    ? {
                        status: "cancelled",
                        produced_files: [],
                        artifact_type: oldMsg?.failure?.artifact_type,
                      }
                    : setupError
                    ? undefined
                    : runClientFailure(
                        err,
                        oldMsg?.failure?.artifact_type,
                        activeRetryRunId,
                      ),
                }
              : m
              ),
            })
        );
        set((s) => {
          if (activeRetryRunId && s.runs_progress[convId]?.run_id !== activeRetryRunId) {
            return { settings_open: setupError ? true : s.settings_open };
          }
          const next = { ...s.runs_progress };
          delete next[convId];
          return {
            runs_progress: next,
            settings_open: setupError ? true : s.settings_open,
          };
        });
      } finally {
        cleanupReservedRunOwner(convId, activeRetryRunId);
      }
    },

	    resumeRun: async (message_id) => {
	      let convId = "";
	      let conv: Conversation | undefined;
	      let failedIndex = -1;
	      let failedMsg: Message | undefined;
      for (const c of Object.values(get().conversations)) {
        const idx = c.messages.findIndex((x) => x.id === message_id);
        if (idx >= 0) {
          convId = c.id;
          conv = c;
          failedIndex = idx;
          failedMsg = c.messages[idx];
          break;
        }
	      }
	      if (!convId || !conv || failedIndex < 0 || !failedMsg) return;

	      const recoverable = resolveRecoverableTask(conv, failedIndex, failedMsg);
	      if (!recoverable) return;
	      if (recoverable.task_type === PPTX_EXPORT_TASK) {
	        const bundleParent = conv.paper_bundle?.kind === "parent"
	          ? conv
	          : conv.paper_bundle?.kind === "child"
	            ? get().conversations[conv.paper_bundle.parent_conversation_id]
	            : undefined;
	        if (
	          bundleParent?.paper_bundle?.kind === "parent"
	          && paperBundleHasActiveTasks(bundleParent.paper_bundle)
	        ) {
	          throw new Error("Wait for Paper All-in-One to finish before exporting PowerPoint.");
	        }
	      }
	      const releasePptxOperation = recoverable.task_type === PPTX_EXPORT_TASK
	        ? acquirePptxOperation(convId, conv)
	        : undefined;

	      const artifactType = recoverable.task_payload?.artifact_type || failedMsg.failure?.artifact_type;
	      const artifactDeliveryRunId = failedMsg.failure?.status === "artifact_delivery_failed"
	        ? failedMsg.failure.run_id || runIdFromMessage(failedMsg)
	        : "";
	      const resumeCanvasPresetId = artifactType === "poster"
	        && typeof recoverable.task_payload?.canvas_preset_id === "string"
	        ? recoverable.task_payload.canvas_preset_id.trim()
	        : "";
	      if (
	        !artifactDeliveryRunId
	        && resumeCanvasPresetId
	        && resumeCanvasPresetId !== "auto"
	      ) {
	        if (get().poster_canvas_presets_status !== "ready") {
	          await get().loadPosterCanvasPresets();
	        }
	        const canvasState = get();
	        if (
	          canvasState.poster_canvas_presets_status !== "ready"
	          || !canvasState.poster_canvas_presets.some(
	            (preset) => preset.id === resumeCanvasPresetId,
	          )
	        ) {
	          set((state) => ({
	            canvas_validation_errors: {
	              ...state.canvas_validation_errors,
	              [convId]: {
	                brief: recoverable.instruction,
	                message: canvasState.poster_canvas_presets_error
	                  || "Canvas preset catalog unavailable. Retry when it can be validated.",
	              },
	            },
	          }));
	          return;
	        }
	      }
	      const placeholderId = nextId("msg");
	      patchConversation(convId, (c) => ({
	        ...c,
	        pending: true,
        messages: c.messages.map((m) =>
          m.id === message_id
            ? {
                id: placeholderId,
                role: "assistant",
	                text: "",
	                ts: Date.now(),
	                status: "streaming",
	                task_type: recoverable.task_type,
	                task_payload: recoverable.task_payload,
	                source_artifact_id: recoverable.task_payload?.source_artifact_id
	                  || recoverable.source_artifact?.artifact_id
	                  || failedMsg.source_artifact_id,
	              }
	            : m
	        ),
	      }));

	      const baselineConv = get().conversations[convId];
	      const sourceConv = baselineConv ?? conv;
	      const memory = buildConversationMemory({
	        ...sourceConv,
	        messages: sourceConv.messages.slice(0, failedIndex),
	      });
	      let activeRunId: string | undefined;
	      const controller = new AbortController();
	      const resolveResumeStart = (
	        request: Promise<GenerateAck>,
	        progressMode: string,
	      ) => resolveReservedRunStart({
	        request,
	        reservedRunId: () => activeRunId,
	        isCurrent: (runId) => (
	          get().conversations[convId]?.run_id === runId
	          && _RESERVED_RUN_UPLOAD_ABORTS.get(convId)?.runId === runId
	        ),
	        placeholderMessage: {
	          id: placeholderId,
	          role: "assistant",
	          text: "",
	          ts: Date.now(),
	          status: "streaming",
	          task_type: recoverable.task_type,
	          task_payload: recoverable.task_payload,
	          source_artifact_id: recoverable.task_payload?.source_artifact_id
	            || recoverable.source_artifact?.artifact_id
	            || failedMsg.source_artifact_id,
	        },
	        progressMode,
	      });

	      try {
	        if (artifactDeliveryRunId) {
	          activeRunId = artifactDeliveryRunId;
	          _RESERVED_RUN_UPLOAD_ABORTS.set(convId, {
	            runId: artifactDeliveryRunId,
	            controller,
	          });
	          patchConversation(convId, (current) => ({
	            ...current,
	            run_id: artifactDeliveryRunId,
	          }));
	          set((state) => ({
	            runs_progress: {
	              ...state.runs_progress,
	              [convId]: initialProgress(
	                artifactDeliveryRunId,
	                recoverable.task_type === PPTX_EXPORT_TASK ? "artifact_export" : undefined,
	              ),
	            },
	          }));
	          const result = await runGenerateAckFlow({
	            convId,
	            placeholderId,
	            ack: {
	              run_id: artifactDeliveryRunId,
	              placeholder_message: failedMsg,
	            },
	            timeoutMs: recoverable.task_type === PPTX_EXPORT_TASK
	              ? 30 * 60 * 1000
	              : 60 * 60 * 1000,
	            timeoutMessage: "artifact delivery recovery timed out",
	            closedMessage: "artifact delivery recovery event stream closed",
	            task_type: recoverable.task_type,
	            task_payload: recoverable.task_payload,
	            source_artifact_id: recoverable.task_payload?.source_artifact_id
	              || recoverable.source_artifact?.artifact_id
	              || failedMsg.source_artifact_id,
	            activateArtifact: recoverable.task_type !== PPTX_EXPORT_TASK,
	            recoverExisting: true,
	            artifactOnlyRecovery: true,
	          });
	          if (result.message.download_url) {
	            triggerStoreDownload(
	              result.message.download_url,
	              result.message.download_filename || "artifact.pptx",
	            );
	          }
	          return;
	        }
	        if (recoverable.task_type === PPTX_EXPORT_TASK) {
	          if (!recoverable.source_artifact) {
	            throw new Error("PowerPoint export source artifact is missing");
	          }
	          const source = cloneArtifact(recoverable.source_artifact);
	          const taskPayload: MessageTaskPayload = {
	            ...(recoverable.task_payload ?? {}),
	            source_artifact_id: source.artifact_id,
	            export_format: "pptx",
	          };
	          const { ack, reconcileImmediately, startReplay } = await resolveResumeStart(
	            startArtifactPptxExport({
	              artifact: source,
	              conversation_id: convId,
	            }, controller.signal, (runId) => {
	              activeRunId = runId;
	              _RESERVED_RUN_UPLOAD_ABORTS.set(convId, { runId, controller });
	              patchConversation(convId, (c) => ({ ...c, run_id: runId }));
	              set((s) => ({
	                runs_progress: {
	                  ...s.runs_progress,
	                  [convId]: initialProgress(runId, "artifact_export"),
	                },
	              }));
	            }),
	            "artifact_export",
	          );
	          activeRunId = ack.run_id;
	          ensureRunStillOwned(convId, ack.run_id);
	          const res = await runGenerateAckFlow({
	            convId,
	            placeholderId,
	            ack,
	            timeoutMs: 30 * 60 * 1000,
	            timeoutMessage: "PowerPoint export still running after 30 min",
	            closedMessage: "PowerPoint export event stream closed",
	            task_type: PPTX_EXPORT_TASK,
	            task_payload: taskPayload,
	            source_artifact_id: source.artifact_id,
	            activateArtifact: false,
	            reconcileImmediately,
	            startReplay,
	          });
	          if (res.message.download_url) {
	            triggerStoreDownload(
	              res.message.download_url,
	              res.message.download_filename || `${source.name}.pptx`,
	            );
            emitArtifactEvent(convId, "artifact.downloaded", source.artifact_id, {
              format: "pptx",
              run_id: ack.run_id,
	              resumed: true,
	            });
	          }
	          return;
	        }

	        if (recoverable.task_type === POSTER_CODE_EDIT_TASK) {
	          if (!recoverable.source_artifact) {
	            throw new Error("Poster revision source artifact is missing");
	          }
	          const source = cloneArtifact(recoverable.source_artifact);
	          const taskPayload: MessageTaskPayload = {
	            ...(recoverable.task_payload ?? {}),
	            artifact_type: "poster",
	            source_artifact_id: source.artifact_id,
	          };
	          const { ack, reconcileImmediately, startReplay } = await resolveResumeStart(
	            startPosterCodeEdit({
	              artifact: source,
	              instruction: recoverable.instruction,
	              conversation_id: convId,
	              conversation_history: memory.history,
	              selection_context: taskPayload.selection_context ?? undefined,
	              palette_id: taskPayload.palette_id,
	            }, controller.signal, (runId) => {
	              activeRunId = runId;
	              _RESERVED_RUN_UPLOAD_ABORTS.set(convId, { runId, controller });
	              patchConversation(convId, (c) => ({ ...c, run_id: runId }));
	              set((s) => ({
	                runs_progress: {
	                  ...s.runs_progress,
	                  [convId]: initialProgress(runId, "poster_code_edit"),
	                },
	              }));
	            }),
	            "poster_code_edit",
	          );
	          activeRunId = ack.run_id;
	          ensureRunStillOwned(convId, ack.run_id);
	          await runGenerateAckFlow({
	            convId,
	            placeholderId,
	            ack,
	            timeoutMs: 60 * 60 * 1000,
	            timeoutMessage: "poster revision still running after 60 min",
	            closedMessage: "poster revision event stream closed during resume",
	            task_type: POSTER_CODE_EDIT_TASK,
	            task_payload: taskPayload,
	            source_artifact_id: source.artifact_id,
	            reconcileImmediately,
	            startReplay,
	          });
	          return;
	        }

	        const payload = recoverable.task_payload ?? {};
	        const baseline = payload.baseline_artifact_id
	          ? sourceConv.artifacts[payload.baseline_artifact_id]
	          : sourceConv.active_artifact_id
	            ? sourceConv.artifacts[sourceConv.active_artifact_id]
	            : undefined;
	        const attachmentRefs = payload.attachment_refs ?? [];
	        const { palette_id: payloadPaletteId, ...payloadWithoutPalette } = payload;
		        const canvasSelection = artifactType !== "poster"
		          ? null
		          : resumeCanvasPresetId
		            ? posterCanvasRequestSelection(
		                get().poster_canvas_presets,
		                resumeCanvasPresetId,
		                payload.template,
		              )
		            : payload.template
		              ? { canvas_preset_id: undefined, template: payload.template }
		              : posterCanvasRequestSelection(
		                  get().poster_canvas_presets,
		                  sourceConv.poster_canvas_preset_id,
		                );
		        const template = artifactType === "poster"
		          ? canvasSelection?.template
		          : payload.template;
	        const taskPayload: MessageTaskPayload = {
	          ...payloadWithoutPalette,
	          artifact_type: artifactType,
	          authoring_max_attempts: payload.authoring_max_attempts,
	          template,
	          canvas_preset_id: canvasSelection?.canvas_preset_id,
	          baseline_artifact_id: baseline?.artifact_id,
	          attachment_refs: attachmentRefs.length ? attachmentRefs : undefined,
	          ...(artifactType === "poster" && payloadPaletteId
	            ? { palette_id: payloadPaletteId }
	            : {}),
	        };
	        const { ack, reconcileImmediately, startReplay } = await resolveResumeStart(
	          startGenerate({
	            brief: recoverable.instruction,
	            attachments: [],
	            attachment_refs: taskPayload.attachment_refs,
	            reference_poster_ref: taskPayload.reference_poster_ref,
	            conversation_id: convId,
	            artifact_type: artifactType,
	            authoring_max_attempts: taskPayload.authoring_max_attempts,
	            template,
	            canvas_preset_id: canvasSelection?.canvas_preset_id,
	            palette_id: artifactType === "poster" ? taskPayload.palette_id : undefined,
	            baseline_artifact: baseline,
	            conversation_history: memory.history,
	            prior_artifacts: memory.artifacts,
	          }, controller.signal, {
	            onReserved: (runId) => {
	              activeRunId = runId;
	              _RESERVED_RUN_UPLOAD_ABORTS.set(convId, { runId, controller });
	              patchConversation(convId, (c) => ({ ...c, run_id: runId }));
	              set((s) => ({
	                runs_progress: {
	                  ...s.runs_progress,
	                  [convId]: initialProgress(runId),
	                },
	              }));
	            },
	          }),
	          "generate",
	        );
	        activeRunId = ack.run_id;
	        ensureRunStillOwned(convId, ack.run_id);
	        await runGenerateAckFlow({
	          convId,
	          placeholderId,
	          ack,
	          timeoutMs: 60 * 60 * 1000,
	          timeoutMessage: "resumed run still running after 60 min",
	          closedMessage: "event stream closed during resume — backend may have crashed",
	          task_type: GENERATE_TASK,
	          task_payload: taskPayload,
	          reconcileImmediately,
	          startReplay,
	        });
	      } catch (err) {
	        const cancellation = runCancellationDisposition(convId, activeRunId, err);
	        if (cancellation === "pending") return;
	        const cancelled = cancellation === "confirmed";
	        const setupError = isSetupError(err) ? err : null;
	        const detail = err instanceof Error ? err.message : "unknown";
	        patchConversation(convId, (c) => activeRunId && c.run_id !== activeRunId
	          ? c
	          : ({
	              ...c,
	              pending: false,
	              run_id: undefined,
	              messages: c.messages.map((m) =>
            m.id === placeholderId
              ? {
                  ...m,
	                  run_id: activeRunId,
	                  text: cancelled
	                    ? "Run cancelled."
	                    : setupError
	                    ? setupErrorText(setupError, "API key required — open Settings to paste your key.")
	                    : `Resume failed: ${detail}`,
	                  status: "error",
	                  task_type: recoverable.task_type,
	                  task_payload: recoverable.task_payload,
	                  source_artifact_id: recoverable.source_artifact?.artifact_id
	                    || recoverable.task_payload?.source_artifact_id
	                    || m.source_artifact_id,
	                  failure: cancelled
	                    ? { status: "cancelled", produced_files: [], artifact_type: artifactType }
	                    : setupError
	                    ? undefined
                    : runClientFailure(err, artifactType, activeRunId),
                }
              : m
	              ),
	            })
	        );
	        set((s) => {
	          if (activeRunId && s.runs_progress[convId]?.run_id !== activeRunId) {
	            return { settings_open: setupError ? true : s.settings_open };
	          }
          const next = { ...s.runs_progress };
          delete next[convId];
          return {
            runs_progress: next,
            settings_open: setupError ? true : s.settings_open,
          };
        });
      } finally {
        cleanupReservedRunOwner(convId, activeRunId);
        releasePptxOperation?.();
      }
    },

    cancelRun: async (conversation_id) => {
      const requestScope = currentDemoUserScope();
      const cid = conversation_id ?? get().current_conversation_id;
      const initialConversation = get().conversations[cid];
      const publicationRunId = initialConversation
        ? activeCandidatePublishMessage(initialConversation)?.run_id
        : undefined;
      const forkRunId = activeAttemptForkOwner(cid)?.runId;
      const derivedCancellation = Promise.all([
        cancelCandidatePublication(cid, requestScope),
        cancelAttemptFork(cid),
      ]);
      const cancelOrdinaryRun = async () => {
      const conv = get().conversations[cid];
      const run_id = conv?.run_id;
      if (!run_id || run_id === publicationRunId || run_id === forkRunId) return;
      const sourceMessage = [...(conv?.messages ?? [])].reverse().find((message) => (
        message.role === "assistant"
        && message.status === "streaming"
        && message.task_type !== CANDIDATE_PUBLISH_TASK
        && message.run_id === run_id
      )) ?? [...(conv?.messages ?? [])].reverse().find((message) => (
        message.role === "assistant"
        && message.status === "streaming"
        && message.task_type !== CANDIDATE_PUBLISH_TASK
        && !message.run_id
      ));
      const currentProgress = get().runs_progress[cid];
      if (
        currentProgress?.run_id === run_id
        && (currentProgress.phase === "done" || currentProgress.phase === "error")
      ) return;

      set((s) => {
        if (
          currentDemoUserScope() !== requestScope
          || s.conversations[cid]?.run_id !== run_id
          || s.runs_progress[cid]?.run_id !== run_id
        ) return s;
        return {
          runs_progress: {
            ...s.runs_progress,
            [cid]: {
              ...s.runs_progress[cid],
              phase: "cancelling",
              label: "Stopping run…",
              cancel_request_in_flight: true,
            },
          },
        };
      });

      const runKey = runOperationKey(cid, run_id);
      const cancelRequestKey = runCancellationRequestKey(requestScope, cid, run_id);
      let request = _RUN_CANCEL_REQUESTS.get(cancelRequestKey);
      if (!request) {
        request = (async () => {
          const controller = new AbortController();
          const timeout = window.setTimeout(
            () => controller.abort(new Error("run cancellation timed out")),
            CANCELLATION_REQUEST_TIMEOUT_MS,
          );
          try {
            return await cancelRunRequest(run_id, controller.signal);
          } finally {
            window.clearTimeout(timeout);
          }
        })();
        _RUN_CANCEL_REQUESTS.set(cancelRequestKey, request);
      }
      let result;
      try {
        result = await request;
      } catch {
        set((s) => {
          const progress = s.runs_progress[cid];
          if (
            currentDemoUserScope() !== requestScope
            || s.conversations[cid]?.run_id !== run_id
            || progress?.run_id !== run_id
          ) return s;
          if (progress.phase === "done" || progress.phase === "error") return s;
          return {
            runs_progress: {
              ...s.runs_progress,
              [cid]: {
                ...progress,
                phase: "cancelling",
                label: "Cancellation not confirmed; backend may still be stopping",
                cancel_request_in_flight: false,
              },
            },
          };
        });
        return;
      } finally {
        if (_RUN_CANCEL_REQUESTS.get(cancelRequestKey) === request) {
          _RUN_CANCEL_REQUESTS.delete(cancelRequestKey);
        }
      }

      if (currentDemoUserScope() !== requestScope) return;

      set((s) => {
        const progress = s.runs_progress[cid];
        if (
          s.conversations[cid]?.run_id !== run_id
          || progress?.run_id !== run_id
        ) return s;
        return {
          runs_progress: {
            ...s.runs_progress,
            [cid]: { ...progress, cancel_request_in_flight: false },
          },
        };
      });

      const confirmedTerminal = result.http_status === 200
        && result.confirmed
        && result.status === "already_terminal";
      const confirmedCancellation = result.http_status === 200
        && result.confirmed
        && (result.status === "cancelled" || result.status === "already_cancelled");
      const cancellationEventObserved = _AUTHORITATIVE_RUN_CANCELLATIONS.has(runKey);
      const latestProgress = get().runs_progress[cid];
      if (
        latestProgress?.run_id === run_id
        && (
          latestProgress.phase === "done"
          || (latestProgress.phase === "error" && !cancellationEventObserved)
        )
      ) return;

      if (!confirmedTerminal && !confirmedCancellation) {
        set((s) => {
          const progress = s.runs_progress[cid];
          if (
            currentDemoUserScope() !== requestScope
            || s.conversations[cid]?.run_id !== run_id
            || progress?.run_id !== run_id
          ) return s;
          return {
            runs_progress: {
              ...s.runs_progress,
              [cid]: {
                ...progress,
                phase: "cancelling",
                label: "Cancellation not confirmed; backend may still be stopping",
              },
            },
          };
        });
        return;
      }

      let confirmedUploadOwner: { runId: string; controller: AbortController } | undefined;
      if (confirmedCancellation) {
        _AUTHORITATIVE_RUN_CANCELLATIONS.add(runKey);
        const uploadOwner = _RESERVED_RUN_UPLOAD_ABORTS.get(cid);
        if (uploadOwner?.runId === run_id) {
          confirmedUploadOwner = uploadOwner;
          if (!uploadOwner.controller.signal.aborted) {
            uploadOwner.controller.abort(new RunWaitCancelledError());
          }
        }
      }

      if (confirmedTerminal) {
        set((s) => {
          const progress = s.runs_progress[cid];
          if (
            currentDemoUserScope() !== requestScope
            || s.conversations[cid]?.run_id !== run_id
            || progress?.run_id !== run_id
          ) return s;
          return {
            runs_progress: {
              ...s.runs_progress,
              [cid]: result.run_state === "completed"
                ? { ...progress, phase: "done", label: "Done." }
                : { ...progress, phase: "error", label: "Run failed." },
            },
          };
        });
        return;
      }

      const waitOwner = _SSE_WAIT_ABORTS.get(cid);
      if (waitOwner?.runId === run_id) {
        waitOwner.abort(new RunWaitCancelledError());
      }
      patchConversation(cid, (c) => {
        if (currentDemoUserScope() !== requestScope || c.run_id !== run_id) return c;
        return {
          ...c,
          pending: false,
          run_id: undefined,
          messages: c.messages.map((m) => {
            if (sourceMessage && m.id === sourceMessage.id && m.status === "streaming") {
              return {
                ...m,
                run_id,
                text: "Run cancelled.",
                status: "error",
                failure: { status: "cancelled", produced_files: [] },
              };
            }
            return m;
          }),
        };
      });
      clearRunProgress(cid, run_id);
      if (!confirmedUploadOwner) {
        _AUTHORITATIVE_RUN_CANCELLATIONS.delete(runKey);
      }
      };
      await Promise.all([derivedCancellation, cancelOrdinaryRun()]);
    },

    // ---- layer ops ----

    selectLayer: (layer_id, mode = "replace") => {
      if (!layer_id) {
        setSelectionState([]);
        return;
      }
      const current = get().selected_layer_ids.length
        ? get().selected_layer_ids
        : get().selected_layer_id
          ? [get().selected_layer_id!]
          : [];
      if (mode === "add") {
        setSelectionState([...current, layer_id]);
      } else if (mode === "toggle") {
        setSelectionState(
          current.includes(layer_id)
            ? current.filter((id) => id !== layer_id)
            : [...current, layer_id]
        );
      } else {
        setSelectionState([layer_id]);
      }
    },

    setSelection: (layer_ids) => setSelectionState(layer_ids),

    clearSelection: () => setSelectionState([]),

    syncLayerInspection: (layer_id, patch) => {
      patchActiveArtifact((art) => ({
        ...art,
        layers: art.layers.map((layer) =>
          layer.layer_id === layer_id ? { ...layer, ...patch } : layer
        ),
      }));
    },

    updateLayer: (layer_id, patch, opts = {}) => {
      // Two-track update: optimistically mutate the artifact in place
      // (so the Sidebar atoms reflect the new value), AND record the
      // patch into the conversation's pending_edits buffer so /api/edits/apply
      // can replay it server-side. We track only fields that round-trip.
      patchActiveArtifact((art) => ({
        ...art,
        layers: art.layers.map((l) =>
          l.layer_id === layer_id
            ? {
                ...l,
                ...patch,
                effects: { ...l.effects, ...(patch.effects ?? {}) },
              }
            : l
        ),
      }), opts);
      const convId = get().current_conversation_id;
      const conv = get().conversations[convId];
      const artId = conv?.active_artifact_id;
      if (!conv || !artId) return;
      // Skip pure UI flags — locked / visible aren't sent server-side.
      const round_trip: Partial<Layer> = {};
      if (patch.text !== undefined) round_trip.text = patch.text;
      if (patch.font_family !== undefined) round_trip.font_family = patch.font_family;
      if (patch.font_size_px !== undefined) round_trip.font_size_px = patch.font_size_px;
      if (patch.font_weight !== undefined) round_trip.font_weight = patch.font_weight;
      if (patch.font_style !== undefined) round_trip.font_style = patch.font_style;
      if (patch.line_height !== undefined) round_trip.line_height = patch.line_height;
      if (patch.letter_spacing !== undefined) round_trip.letter_spacing = patch.letter_spacing;
      if (patch.text_transform !== undefined) round_trip.text_transform = patch.text_transform;
      if (patch.align !== undefined) round_trip.align = patch.align;
      if (patch.bbox !== undefined) round_trip.bbox = patch.bbox;
      if (patch.flow_offset !== undefined) round_trip.flow_offset = patch.flow_offset;
      if (patch.z_index !== undefined) round_trip.z_index = patch.z_index;
      if (patch.effects !== undefined) round_trip.effects = patch.effects;
      if (patch.src !== undefined) round_trip.src = patch.src;
      if (patch.fit !== undefined) round_trip.fit = patch.fit;
      if (patch.object_position !== undefined) round_trip.object_position = patch.object_position;
      if (Object.keys(round_trip).length === 0) return;
      patchConversation(convId, (c) => {
        const allEdits = c.pending_edits ?? {};
        return {
          ...c,
          pending_edits: {
            ...allEdits,
            [artId]: mergeLayerEdit(allEdits[artId], layer_id, round_trip),
          },
        };
      });
      // Manual-save hook — only for native HTML artifacts (poster /
      // landing). Pure layer-based demos have no source HTML file to
      // patch server-side, and decks/videos don't have a layer
      // round-trip; those edits stay local or go through agent re-runs.
      const art = conv.artifacts[artId];
      if (art?.native_file_url && art.native_format === "html") {
        get().scheduleAutoSave();
      }
    },

    recordHtmlLayoutPatch: (patch) => {
      const convId = get().current_conversation_id;
      const conv = get().conversations[convId];
      const artId = conv?.active_artifact_id;
      if (!conv || !artId) return;
      const art = conv.artifacts[artId];
      if (!art?.native_file_url || art.native_format !== "html" || artifactTypeForArtifact(art) !== "poster") {
        return;
      }
      patchConversation(convId, (c) => {
        const allEdits = c.pending_edits ?? {};
        return {
          ...c,
          pending_edits: {
            ...allEdits,
            [artId]: mergeLayoutEdit(allEdits[artId], patch),
          },
        };
      });
      get().scheduleAutoSave();
    },

    replaceActiveArtifactPendingEdits: (edits) => {
      const convId = get().current_conversation_id;
      const conv = get().conversations[convId];
      const artId = conv?.active_artifact_id;
      if (!conv || !artId) return;
      patchConversation(convId, (c) => {
        const next = { ...(c.pending_edits ?? {}) };
        if (edits && hasPendingEditsPayload(edits)) {
          next[artId] = edits;
        } else {
          delete next[artId];
        }
        return { ...c, pending_edits: next };
      });
      const nextEdits = edits && hasPendingEditsPayload(edits);
      set({ autosave_state: nextEdits ? "editing" : "idle" });
    },

    addLayer: (layer) => {
      patchActiveArtifact((art) => ({
        ...art,
        layers: [...art.layers, layer],
      }));
      setSelectionState([layer.layer_id]);
    },

    insertLayers: (layers, options = {}) => {
      if (!layers.length) return;
      const newIds = layers.map((l) => l.layer_id);
      patchActiveArtifact((art) => {
        const frame = activeInsertFrame(art);
        const placement = options.placement ?? (layers.length === 1 ? "single" : "frame-relative");
        const maxZ = Math.max(0, ...art.layers.map((l) => l.z_index));
        let nextLayers = layers.map((l, idx) => ({
          ...cloneLayer(l),
          z_index: maxZ + idx + 1,
          visible: l.visible ?? true,
        }));

        if (placement === "frame-relative") {
          nextLayers = nextLayers.map((l) => ({
            ...l,
            bbox: l.bbox
              ? {
                  x: Math.round(frame.x + l.bbox.x * frame.w),
                  y: Math.round(frame.y + l.bbox.y * frame.h),
                  w: Math.round(l.bbox.w * frame.w),
                  h: Math.round(l.bbox.h * frame.h),
                }
              : l.bbox,
          }));
        }

        const moveGroupTo = (anchor: { x: number; y: number }) => {
          const bounds = boundsOf(nextLayers) ?? nextLayers[0].bbox ?? {
            x: 0,
            y: 0,
            w: Math.min(240, frame.w),
            h: Math.min(120, frame.h),
          };
          const desired = {
            ...bounds,
            x: anchor.x - bounds.w / 2,
            y: anchor.y - bounds.h / 2,
          };
          const clamped = clampBoxToFrame(desired, frame);
          const dx = clamped.x - bounds.x;
          const dy = clamped.y - bounds.y;
          nextLayers = nextLayers.map((l) => ({
            ...l,
            bbox: l.bbox
              ? { ...l.bbox, x: Math.round(l.bbox.x + dx), y: Math.round(l.bbox.y + dy) }
              : l.bbox,
          }));
        };

        if (options.strategy === "point" && options.anchor) {
          moveGroupTo(options.anchor);
        } else if (options.strategy === "center") {
          moveGroupTo({ x: frame.x + frame.w / 2, y: frame.y + frame.h / 2 });
        } else if (placement === "single") {
          const selected = selectedLayers(art).filter((l) => l.bbox);
          const bounds = boundsOf(nextLayers) ?? nextLayers[0].bbox ?? {
            x: 0,
            y: 0,
            w: Math.min(240, frame.w),
            h: Math.min(120, frame.h),
          };
          const anchor =
            selected.length === 1 && selected[0].bbox
              ? { x: selected[0].bbox.x + 24, y: selected[0].bbox.y + 24 }
              : {
                  x: frame.x + (frame.w - bounds.w) / 2,
                  y: frame.y + (frame.h - bounds.h) / 2,
                };
          const clamped = clampBoxToFrame({ ...bounds, ...anchor }, frame);
          const dx = clamped.x - bounds.x;
          const dy = clamped.y - bounds.y;
          nextLayers = nextLayers.map((l) => ({
            ...l,
            bbox: l.bbox
              ? { ...l.bbox, x: Math.round(l.bbox.x + dx), y: Math.round(l.bbox.y + dy) }
              : l.bbox,
          }));
        }

        return { ...art, layers: [...art.layers, ...nextLayers] };
      });
      if (options.select !== false) setSelectionState(newIds);
    },

    removeLayer: (layer_id) => {
      patchActiveArtifact((art) => ({
        ...pruneLayerGroups({
          ...art,
          layers: art.layers.filter((l) => l.layer_id !== layer_id),
        }),
      }));
      setSelectionState(get().selected_layer_ids.filter((id) => id !== layer_id));
    },

    reorderLayer: (layer_id, dir, scope_layer_ids) =>
      patchActiveArtifact((art) => {
        const scope =
          scope_layer_ids && scope_layer_ids.length
            ? new Set(scope_layer_ids)
            : null;
        const sorted = [...art.layers]
          .filter((l) => !scope || scope.has(l.layer_id))
          .sort((a, b) => a.z_index - b.z_index);
        const idx = sorted.findIndex((l) => l.layer_id === layer_id);
        if (idx === -1) return art;
        const swap = dir === "up" ? idx + 1 : idx - 1;
        if (swap < 0 || swap >= sorted.length) return art;
        const a = sorted[idx];
        const b = sorted[swap];
        const aZ = a.z_index;
        const bZ = b.z_index;
        return {
          ...art,
          layers: art.layers.map((l) => {
            if (l.layer_id === a.layer_id) return { ...l, z_index: bZ };
            if (l.layer_id === b.layer_id) return { ...l, z_index: aZ };
            return l;
          }),
        };
      }),

    reorderLayerBlock: (layer_ids, target_layer_ids, position, scope_layer_ids) =>
      patchActiveArtifact((art) => {
        const moving = new Set(layer_ids);
        const targets = new Set(target_layer_ids);
        if (!moving.size || !targets.size) return art;
        if ([...targets].some((id) => moving.has(id))) return art;
        const scope =
          scope_layer_ids && scope_layer_ids.length
            ? new Set(scope_layer_ids)
            : null;
        const display = [...art.layers]
          .filter((l) => !scope || scope.has(l.layer_id))
          .sort((a, b) => b.z_index - a.z_index);
        const block = display.filter((l) => moving.has(l.layer_id));
        if (!block.length) return art;
        const rest = display.filter((l) => !moving.has(l.layer_id));
        const targetIndexes = rest
          .map((l, idx) => (targets.has(l.layer_id) ? idx : -1))
          .filter((idx) => idx >= 0);
        if (!targetIndexes.length) return art;
        const insertIdx =
          position === "before"
            ? Math.min(...targetIndexes)
            : Math.max(...targetIndexes) + 1;
        const nextDisplay = [
          ...rest.slice(0, insertIdx),
          ...block,
          ...rest.slice(insertIdx),
        ];
        const zValues = display.map((l) => l.z_index).sort((a, b) => b - a);
        const zById = new Map(
          nextDisplay.map((l, idx) => [l.layer_id, zValues[idx] ?? l.z_index])
        );
        return {
          ...art,
          layers: art.layers.map((l) =>
            zById.has(l.layer_id) ? { ...l, z_index: zById.get(l.layer_id)! } : l
          ),
        };
      }),

    reorderSelection: (dir) =>
      patchActiveArtifact((art) => {
        const selected = new Set(get().selected_layer_ids);
        if (!selected.size) return art;
        const sorted = [...art.layers].sort((a, b) => a.z_index - b.z_index);
        if (dir === "up") {
          for (let i = sorted.length - 2; i >= 0; i -= 1) {
            if (selected.has(sorted[i].layer_id) && !selected.has(sorted[i + 1].layer_id)) {
              [sorted[i], sorted[i + 1]] = [sorted[i + 1], sorted[i]];
            }
          }
        } else {
          for (let i = 1; i < sorted.length; i += 1) {
            if (selected.has(sorted[i].layer_id) && !selected.has(sorted[i - 1].layer_id)) {
              [sorted[i], sorted[i - 1]] = [sorted[i - 1], sorted[i]];
            }
          }
        }
        const zById = new Map(sorted.map((l, idx) => [l.layer_id, idx + 1]));
        return {
          ...art,
          layers: art.layers.map((l) => ({ ...l, z_index: zById.get(l.layer_id) ?? l.z_index })),
        };
      }),

    toggleLayerProp: (layer_id, prop) =>
      patchActiveArtifact((art) => ({
        ...art,
        layers: art.layers.map((l) =>
          l.layer_id === layer_id
            ? { ...l, [prop]: !(l[prop] ?? prop === "visible") }
            : l
        ),
      })),

    toggleGroupProp: (group_id, prop) =>
      patchActiveArtifact((art) => {
        const children = art.layers.filter((l) => l.group_id === group_id);
        if (!children.length) return art;
        const nextValue =
          prop === "visible"
            ? !children.some((l) => l.visible !== false)
            : !children.every((l) => !!l.locked);
        return {
          ...art,
          layers: art.layers.map((l) =>
            l.group_id === group_id ? { ...l, [prop]: nextValue } : l
          ),
        };
      }),

    deleteGroup: (group_id) => {
      patchActiveArtifact((art) =>
        pruneLayerGroups({
          ...art,
          layers: art.layers.filter((l) => l.group_id !== group_id || l.locked),
        })
      );
      setSelectionState(
        get().selected_layer_ids.filter((id) => {
          const layer = getActiveContext().art?.layers.find((l) => l.layer_id === id);
          return layer?.group_id !== group_id;
        })
      );
    },

    setSelectionLocked: (locked) =>
      patchActiveArtifact((art) => {
        const selected = new Set(get().selected_layer_ids);
        if (!selected.size) return art;
        return {
          ...art,
          layers: art.layers.map((l) =>
            selected.has(l.layer_id) ? { ...l, locked } : l
          ),
        };
      }),

    groupSelection: () => {
      const { art } = getActiveContext();
      if (!art) return;
      const selectedIds = new Set(get().selected_layer_ids);
      const layers = art.layers.filter(
        (l) => selectedIds.has(l.layer_id) && l.bbox && l.kind !== "background"
      );
      if (layers.length < 2) return;
      if (!selectionScopeKey(art, layers)) return;
      const groupLayerIds = new Set(layers.map((l) => l.layer_id));
      const groupId = nextId("grp");
      const group: LayerGroup = { group_id: groupId, name: nextGroupName(art) };
      patchActiveArtifact((current) =>
        pruneLayerGroups({
          ...current,
          layer_groups: [...(current.layer_groups ?? []), group],
          layers: current.layers.map((l) =>
            groupLayerIds.has(l.layer_id) ? { ...l, group_id: groupId } : l
          ),
        })
      );
      setSelectionState(layers.map((l) => l.layer_id));
    },

    ungroupSelection: (group_id) => {
      const { art } = getActiveContext();
      if (!art) return;
      const groupIds = new Set<string>();
      if (group_id) groupIds.add(group_id);
      const selected = new Set(get().selected_layer_ids);
      art.layers.forEach((l) => {
        if (l.group_id && selected.has(l.layer_id)) groupIds.add(l.group_id);
      });
      if (!groupIds.size) return;
      patchActiveArtifact((current) => ({
        ...current,
        layer_groups: (current.layer_groups ?? []).filter((g) => !groupIds.has(g.group_id)),
        layers: current.layers.map((l) =>
          l.group_id && groupIds.has(l.group_id) ? withoutGroupId(l) : l
        ),
      }));
    },

    renameGroup: (group_id, name) => {
      const next = name.trim();
      if (!next) return;
      patchActiveArtifact((art) => ({
        ...art,
        layer_groups: (art.layer_groups ?? []).map((g) =>
          g.group_id === group_id ? { ...g, name: next } : g
        ),
      }));
    },

    updateCanvas: (patch) =>
      patchActiveArtifact((art) => ({
        ...art,
        canvas: { ...art.canvas, ...patch },
      })),

    captureHistorySnapshot: () => {
      const { art } = getActiveContext();
      if (art) recordHistory(art);
    },

    undo: () => {
      const { convId, conv, artId, art } = getActiveContext();
      if (!conv || !artId || !art) return;
      const hist = get().editor_history[artId];
      if (!hist?.past.length) return;
      const previous = cloneArtifact(hist.past[hist.past.length - 1]);
      const rest = hist.past.slice(0, -1);
      const current = cloneArtifact(art);
      const selection = sanitizeSelection(get().selected_layer_ids, previous);
      set((s) => {
        const c = s.conversations[convId];
        if (!c) return s;
        return {
          conversations: {
            ...s.conversations,
            [convId]: {
              ...c,
              updated_at: Date.now(),
              artifacts: { ...c.artifacts, [artId]: previous },
            },
          },
          editor_history: {
            ...s.editor_history,
            [artId]: { past: rest, future: [current, ...(hist.future ?? [])].slice(0, 50) },
          },
          selected_layer_ids: selection,
          selected_layer_id: selection[selection.length - 1] ?? null,
        };
      });
    },

    redo: () => {
      const { convId, conv, artId, art } = getActiveContext();
      if (!conv || !artId || !art) return;
      const hist = get().editor_history[artId];
      if (!hist?.future.length) return;
      const next = cloneArtifact(hist.future[0]);
      const future = hist.future.slice(1);
      const current = cloneArtifact(art);
      const selection = sanitizeSelection(get().selected_layer_ids, next);
      set((s) => {
        const c = s.conversations[convId];
        if (!c) return s;
        return {
          conversations: {
            ...s.conversations,
            [convId]: {
              ...c,
              updated_at: Date.now(),
              artifacts: { ...c.artifacts, [artId]: next },
            },
          },
          editor_history: {
            ...s.editor_history,
            [artId]: { past: [...(hist.past ?? []), current].slice(-50), future },
          },
          selected_layer_ids: selection,
          selected_layer_id: selection[selection.length - 1] ?? null,
        };
      });
    },

    copySelection: () => {
      const { art } = getActiveContext();
      if (!art) return;
      const source = editableSelectedLayers(art);
      const { layers, groups } = normalizeCopiedLayersForGroups(art, source);
      set({ editor_clipboard: layers, editor_clipboard_groups: groups });
    },

    pasteSelection: () => {
      const source = get().editor_clipboard;
      if (!source.length) return;
      const sourceGroups = get().editor_clipboard_groups;
      const groupIdMap = new Map(
        sourceGroups.map((g) => [g.group_id, nextId("grp")])
      );
      const newIds: string[] = [];
      patchActiveArtifact((art) => {
        const maxZ = Math.max(0, ...art.layers.map((l) => l.z_index));
        const copies = source.map((l, idx) => {
          const id = nextId("lyr");
          newIds.push(id);
          return {
            ...cloneLayer(l),
            layer_id: id,
            name: `${l.name} copy`,
            z_index: maxZ + idx + 1,
            group_id: l.group_id ? groupIdMap.get(l.group_id) : undefined,
            bbox: l.bbox
              ? { ...l.bbox, x: l.bbox.x + 24, y: l.bbox.y + 24 }
              : l.bbox,
          };
        });
        const newGroups = sourceGroups.map((g) => ({
          group_id: groupIdMap.get(g.group_id)!,
          name: `${g.name} copy`,
        }));
        return pruneLayerGroups({
          ...art,
          layer_groups: [...(art.layer_groups ?? []), ...newGroups],
          layers: [...art.layers, ...copies],
        });
      });
      setSelectionState(newIds);
    },

    copySelectionStyle: () => {
      const { art } = getActiveContext();
      if (!art) return;
      const layer = selectedLayers(art).find((l) => !l.locked);
      if (!layer) return;
      if (layer.kind === "text") {
        set({ editor_style_clipboard: { kind: "text", patch: textStylePatch(layer) } });
      } else if (layer.kind === "image") {
        set({ editor_style_clipboard: { kind: "image", patch: imageStylePatch(layer) } });
      } else if (layer.kind === "shape" || layer.kind === "background") {
        set({ editor_style_clipboard: { kind: "shape", patch: shapeStylePatch(layer) } });
      }
    },

    pasteSelectionStyle: () => {
      const clip = get().editor_style_clipboard;
      const { art } = getActiveContext();
      if (!clip || !art) return;
      const selected = new Set(get().selected_layer_ids);
      if (!selected.size) return;
      patchActiveArtifact((current) => ({
        ...current,
        layers: current.layers.map((l) => {
          if (!selected.has(l.layer_id) || l.locked) return l;
          const compatible =
            (clip.kind === "text" && l.kind === "text") ||
            (clip.kind === "image" && l.kind === "image") ||
            (clip.kind === "shape" && (l.kind === "shape" || l.kind === "background"));
          if (!compatible) return l;
          return {
            ...l,
            ...clip.patch,
            effects: clip.patch.effects
              ? { ...l.effects, ...clip.patch.effects }
              : l.effects,
          };
        }),
      }));
    },

    updateSelectionStyle: (patch) => {
      const { art } = getActiveContext();
      if (!art) return;
      const selected = new Set(get().selected_layer_ids);
      if (!selected.size) return;
      patchActiveArtifact((current) => ({
        ...current,
        layers: current.layers.map((l) =>
          selected.has(l.layer_id) && !l.locked
            ? {
                ...l,
                ...patch,
                effects: patch.effects ? { ...l.effects, ...patch.effects } : l.effects,
              }
            : l
        ),
      }));
    },

    duplicateSelection: () => {
      const { art } = getActiveContext();
      if (!art) return;
      const source = editableSelectedLayers(art);
      if (!source.length) return;
      const { layers: normalizedSource, groups: sourceGroups } =
        normalizeCopiedLayersForGroups(art, source);
      const groupIdMap = new Map(
        sourceGroups.map((g) => [g.group_id, nextId("grp")])
      );
      const newIds: string[] = [];
      patchActiveArtifact((current) => {
        const maxZ = Math.max(0, ...current.layers.map((l) => l.z_index));
        const copies = normalizedSource.map((l, idx) => {
          const id = nextId("lyr");
          newIds.push(id);
          return {
            ...cloneLayer(l),
            layer_id: id,
            name: `${l.name} copy`,
            z_index: maxZ + idx + 1,
            group_id: l.group_id ? groupIdMap.get(l.group_id) : undefined,
            bbox: l.bbox
              ? { ...l.bbox, x: l.bbox.x + 24, y: l.bbox.y + 24 }
              : l.bbox,
          };
        });
        const newGroups = sourceGroups.map((g) => ({
          group_id: groupIdMap.get(g.group_id)!,
          name: `${g.name} copy`,
        }));
        return pruneLayerGroups({
          ...current,
          layer_groups: [...(current.layer_groups ?? []), ...newGroups],
          layers: [...current.layers, ...copies],
        });
      });
      setSelectionState(newIds);
    },

    deleteSelection: () => {
      const { art } = getActiveContext();
      if (!art) return;
      const selected = new Set(get().selected_layer_ids);
      const deletable = new Set(
        art.layers
          .filter((l) => selected.has(l.layer_id) && !l.locked)
          .map((l) => l.layer_id)
      );
      if (!deletable.size) return;
      patchActiveArtifact((current) =>
        pruneLayerGroups({
          ...current,
          layers: current.layers.filter((l) => !deletable.has(l.layer_id)),
        })
      );
      setSelectionState([]);
    },

    alignSelection: (mode, reference) => {
      const { art } = getActiveContext();
      if (!art) return;
      const layers = editableSelectedLayers(art);
      if (!layers.length) return;
      const target =
        layers.length === 1
          ? reference ?? { x: 0, y: 0, w: art.canvas.w, h: art.canvas.h }
          : boundsOf(layers);
      if (!target) return;
      const selected = new Set(layers.map((l) => l.layer_id));
      patchActiveArtifact((current) => ({
        ...current,
        layers: current.layers.map((l) => {
          if (!selected.has(l.layer_id) || !l.bbox) return l;
          const b = l.bbox;
          const next = { ...b };
          if (mode === "left") next.x = target.x;
          if (mode === "center") next.x = target.x + (target.w - b.w) / 2;
          if (mode === "right") next.x = target.x + target.w - b.w;
          if (mode === "top") next.y = target.y;
          if (mode === "middle") next.y = target.y + (target.h - b.h) / 2;
          if (mode === "bottom") next.y = target.y + target.h - b.h;
          return { ...l, bbox: { ...next, x: Math.round(next.x), y: Math.round(next.y) } };
        }),
      }));
    },

    distributeSelection: (mode) => {
      const { art } = getActiveContext();
      if (!art) return;
      const layers = editableSelectedLayers(art);
      if (layers.length < 3) return;
      const sorted = [...layers].sort((a, b) => {
        const ab = a.bbox!;
        const bb = b.bbox!;
        return mode === "horizontal"
          ? (ab.x + ab.w / 2) - (bb.x + bb.w / 2)
          : (ab.y + ab.h / 2) - (bb.y + bb.h / 2);
      });
      const first = sorted[0].bbox!;
      const last = sorted[sorted.length - 1].bbox!;
      const start = mode === "horizontal" ? first.x + first.w / 2 : first.y + first.h / 2;
      const end = mode === "horizontal" ? last.x + last.w / 2 : last.y + last.h / 2;
      const step = (end - start) / (sorted.length - 1);
      const targetById = new Map<string, number>();
      sorted.forEach((l, idx) => targetById.set(l.layer_id, start + step * idx));
      patchActiveArtifact((current) => ({
        ...current,
        layers: current.layers.map((l) => {
          const center = targetById.get(l.layer_id);
          if (center === undefined || !l.bbox) return l;
          const b = l.bbox;
          return {
            ...l,
            bbox: mode === "horizontal"
              ? { ...b, x: Math.round(center - b.w / 2) }
              : { ...b, y: Math.round(center - b.h / 2) },
          };
        }),
      }));
    },

    nudgeSelection: (dx, dy) => {
      const { art } = getActiveContext();
      if (!art) return;
      const selected = new Set(editableSelectedLayers(art).map((l) => l.layer_id));
      if (!selected.size) return;
      patchActiveArtifact((current) => ({
        ...current,
        layers: current.layers.map((l) =>
          selected.has(l.layer_id) && l.bbox
            ? { ...l, bbox: { ...l.bbox, x: l.bbox.x + dx, y: l.bbox.y + dy } }
            : l
        ),
      }));
    },

    recordArtifactDownloaded: (artifact_id) => {
      emitArtifactEvent(
        get().current_conversation_id,
        "artifact.downloaded",
        artifact_id,
      );
    },

    submitOpenResearchProject: async (artifact_id, options = {}) => {
      const convId = get().current_conversation_id;
      const conv = get().conversations[convId];
      const artId = artifact_id || conv?.active_artifact_id;
      const art = artId ? conv?.artifacts[artId] : undefined;
      if (!conv || !artId || !art || artifactTypeForArtifact(art) !== "poster") return;
      const source = cloneArtifact(art);
      let openResearchRunId = "";
      const setOpenResearch = (openresearch: NonNullable<Artifact["openresearch"]>) => {
        patchConversation(convId, (c) => {
          const current = c.artifacts[artId];
          if (!current) return c;
          return {
            ...c,
            artifacts: {
              ...c.artifacts,
              [artId]: {
                ...current,
                openresearch,
              },
            },
          };
        });
      };

      try {
        const ack = await startOpenResearchProject({
          artifact: source,
          conversation_id: convId,
          ...options,
        });
        openResearchRunId = ack.job_id;
        setOpenResearch({ status: "running", job_id: ack.job_id });
        patchConversation(convId, (c) => ({ ...c, run_id: ack.job_id }));
        set((s) => ({
          runs_progress: {
            ...s.runs_progress,
            [convId]: initialProgress(ack.job_id),
          },
        }));

        await waitForOpenResearchResult(ack.job_id, setOpenResearch);
      } catch (e) {
        setOpenResearch({
          status: "error",
          job_id: openResearchRunId || art.openresearch?.job_id || "",
          error: e instanceof Error ? e.message : String(e),
        });
      } finally {
        patchConversation(convId, (c) =>
          c.run_id === openResearchRunId
            ? { ...c, run_id: undefined }
            : c
        );
        if (openResearchRunId) {
          set((s) => {
            const current = s.runs_progress[convId];
            if (!current || current.run_id !== openResearchRunId) return s;
            const next = { ...s.runs_progress };
            delete next[convId];
            return { runs_progress: next };
          });
        }
      }
    },

    updateVideoSceneDuration: (scene_id, duration_s) => {
      const clean = Math.max(
        VIDEO_SCENE_DURATION_MIN_S,
        Math.min(
          VIDEO_SCENE_DURATION_MAX_S,
          Number.isFinite(duration_s) ? duration_s : VIDEO_SCENE_DURATION_MIN_S,
        ),
      );
      patchActiveArtifact((art) => {
        if (!art.video_project) return art;
        const scenes = art.video_project.scenes.map((scene) =>
          scene.scene_id === scene_id
            ? { ...scene, duration_s: Math.round(clean * 10) / 10 }
            : scene
        );
        return {
          ...art,
          video_project: {
            ...art.video_project,
            scenes,
            duration_s: Math.round(scenes.reduce((sum, scene) => sum + scene.duration_s, 0) * 10) / 10,
          },
        };
      });
    },

    renderActiveVideo: async () => {
      const { convId, artId, art } = getActiveContext();
      if (!convId || !artId || !art || artifactTypeForArtifact(art) !== "video" || !art.video_project) return;
      const releaseDerivedRun = acquireDerivedRunOperation(convId, "video_render");
      const source = cloneArtifact(art);
      let renderRunId = "";
      const controller = new AbortController();
      let preserveCancellationPending = false;
      const setLatestRender = (
        latest_render: NonNullable<Artifact["video_project"]>["latest_render"],
        expectedRunId?: string,
      ) => {
        patchConversation(convId, (c) => {
          if (expectedRunId && c.run_id !== expectedRunId) return c;
          const current = c.artifacts[artId];
          if (!current?.video_project) return c;
          return {
            ...c,
            artifacts: {
              ...c.artifacts,
              [artId]: {
                ...current,
                video_project: {
                  ...current.video_project,
                  latest_render,
                },
              },
            },
          };
        });
      };

      try {
        const { ack, reconcileImmediately, startReplay } = await resolveReservedRunStart({
          request: renderVideoRequest({
            artifact: source,
            conversation_id: convId,
          }, controller.signal, (runId) => {
            renderRunId = runId;
            _RESERVED_RUN_UPLOAD_ABORTS.set(convId, { runId, controller });
            patchConversation(convId, (c) => ({ ...c, run_id: runId }));
            set((s) => ({
              runs_progress: {
                ...s.runs_progress,
                [convId]: initialProgress(runId, "video_render"),
              },
            }));
          }),
          reservedRunId: () => renderRunId || undefined,
          isCurrent: (runId) => (
            get().conversations[convId]?.run_id === runId
            && _RESERVED_RUN_UPLOAD_ABORTS.get(convId)?.runId === runId
          ),
          placeholderMessage: {
            id: nextId("msg"),
            role: "assistant",
            text: "",
            ts: Date.now(),
            status: "streaming",
          },
          progressMode: "video_render",
        });
        renderRunId = ack.run_id;
        ensureRunStillOwned(convId, ack.run_id);
        patchConversation(convId, (c) => ({ ...c, run_id: ack.run_id }));
        set((s) => ({
          runs_progress: {
            ...s.runs_progress,
            [convId]: initialProgress(ack.run_id, ack.progress_mode),
          },
        }));

        await waitForRunTerminal({
          convId,
          runId: ack.run_id,
          terminalEvents: ["run.done", "run.error", "run.cancelled"],
          timeoutMs: 10 * 60 * 1000,
          timeoutMessage: "video render still running after 10 min",
          closedMessage: "video render event stream closed",
          reconcileImmediately,
          startReplay,
        });

        const res = await fetchRunArtifactAfterTerminal(convId, ack.run_id);
        if (res.artifact?.native_file_url) {
          setLatestRender({
            run_id: ack.run_id,
            mp4_url: res.artifact.native_file_url,
            rendered_at: Date.now(),
          }, ack.run_id);
        } else {
          setLatestRender({
            run_id: ack.run_id,
            rendered_at: Date.now(),
            error: res.message.failure?.agent_last_note || res.message.text || "Video render failed",
          }, ack.run_id);
        }
      } catch (err) {
        const cancellation = runCancellationDisposition(convId, renderRunId || undefined, err);
        if (cancellation) {
          preserveCancellationPending = cancellation === "pending";
          return;
        }
        setLatestRender({
          run_id: renderRunId || "local",
          rendered_at: Date.now(),
          error: err instanceof Error ? err.message : "Video render failed",
        }, renderRunId || undefined);
      } finally {
        cleanupReservedRunOwner(convId, renderRunId || undefined);
        releaseDerivedRun();
        if (!preserveCancellationPending) {
          patchConversation(convId, (c) => renderRunId && c.run_id !== renderRunId
            ? c
            : { ...c, run_id: undefined }
          );
          set((s) => {
            if (renderRunId && s.runs_progress[convId]?.run_id !== renderRunId) return s;
            const next = { ...s.runs_progress };
            delete next[convId];
            return { runs_progress: next };
          });
        }
      }
    },

    // ---- edit round-trip ----

    applyEdits: async () => {
      const convId = get().current_conversation_id;
      const conv = get().conversations[convId];
      const artId = conv?.active_artifact_id;
      if (!conv || !artId) return;
      const art = conv.artifacts[artId];
      const edits = conv.pending_edits?.[artId];
      if (!art || !edits || !hasPendingEditsPayload(edits)) return;
      const editsPayload = edits;
      // Round-trip path is HTML-only. Deck edits persist to a derived HTML
      // run; PPTX remains an export format rather than the editable source.
      if (!art.native_file_url) return;
      if (art.native_format && art.native_format !== "html") return;
      const artifactType = artifactTypeForArtifact(art);
      const paletteId = artifactType === "poster"
        ? conv.poster_palette_id?.trim() || undefined
        : undefined;

      set({ pending_apply: true });
      // Drop a streaming placeholder into the chat so the user sees
      // motion (Apply runs ~5–15 s; long enough to need feedback).
      const placeholderId = nextId("msg");
      patchConversation(convId, (c) => ({
        ...c,
        messages: [
          ...c.messages,
          {
            id: placeholderId,
            role: "assistant",
            text: "",
            ts: Date.now(),
            status: "streaming",
          },
        ],
      }));

      try {
        if (artifactType === "poster" && !paletteId && !art.candidate_draft) {
          throw new Error("Select a palette before creating or revising a poster.");
        }
        const res = await applyEditsRequest({
          run_id: runIdFromArtifactId(art.artifact_id),
          conversation_id: convId,
          artifact_type: artifactType,
          ...(paletteId ? { palette_id: paletteId } : {}),
          edits: editsPayload,
        });
        patchConversation(convId, (c) => {
          const messages = c.messages.map((m) =>
            m.id === placeholderId ? { ...res.message, id: placeholderId } : m
          );
          if (!res.artifact) return { ...c, messages };
          // Preserve the parent in the lineage chain. Clear the pending
          // buffer for the *new* artifact (it has no edits yet).
          const cleared_edits = { ...(c.pending_edits ?? {}) };
          delete cleared_edits[artId];
          return {
            ...c,
            messages,
            artifacts: {
              ...c.artifacts,
              [res.artifact.artifact_id]: res.artifact,
            },
            active_artifact_id: res.artifact.artifact_id,
            pending_edits: cleared_edits,
          };
        });
      } catch (err) {
        const setupError = isSetupError(err) ? err : null;
        patchConversation(convId, (c) => ({
          ...c,
          messages: c.messages.map((m) =>
            m.id === placeholderId
              ? {
                  ...m,
                  text: setupError
                    ? setupErrorText(setupError, "API key required — open Settings to paste your OpenRouter key.")
                    : `Apply failed: ${err instanceof Error ? err.message : "unknown"}`,
                  status: "error",
                }
              : m
          ),
        }));
        if (setupError) set({ settings_open: true });
      } finally {
        set({ pending_apply: false });
      }
    },

    discardEdits: () => {
      const convId = get().current_conversation_id;
      const conv = get().conversations[convId];
      const artId = conv?.active_artifact_id;
      if (!conv || !artId) return;
      const art = conv.artifacts[artId];
      const edits = conv.pending_edits?.[artId];
      if (!art || !edits) return;
      // Roll back the in-place layer mutations using the patch keys we
      // recorded. This is best-effort — we restore each touched field
      // to whatever the parent artifact had if the artifact's parent is
      // present. Since we don't keep a snapshot, just clear the buffer
      // and let the next Apply / regenerate refresh from server.
      patchConversation(convId, (c) => {
        const next = { ...(c.pending_edits ?? {}) };
        delete next[artId];
        return { ...c, pending_edits: next };
      });
    },

    // ---- manual save ----

    autosave_state: "idle",
    autosave_last_saved_at: null,
    autosave_error: null,

    scheduleAutoSave: () => {
      if (_autosave_in_flight) {
        _autosave_dirty_after_save = true;
        return;
      }
      set({ autosave_state: "editing" });
    },

    flushAutoSave: async () => {
      if (_autosave_in_flight) return;
      const convId = get().current_conversation_id;
      const conv = get().conversations[convId];
      const artId = conv?.active_artifact_id;
      if (!conv || !artId) return;
      const art = conv.artifacts[artId];
      const edits = conv.pending_edits?.[artId];
      if (!art || !edits || !hasPendingEditsPayload(edits)) {
        // Nothing to save — flip back to idle so the pill clears.
        set({ autosave_state: "idle" });
        return;
      }
      const editsPayload = edits;
      if (art.native_format && art.native_format !== "html") {
        set({ autosave_state: "idle" });
        return;
      }
      const artifactType = artifactTypeForArtifact(art);
      const paletteId = artifactType === "poster"
        ? conv.poster_palette_id?.trim() || undefined
        : undefined;

      _autosave_in_flight = true;
      _autosave_dirty_after_save = false;
      set({ autosave_state: "saving", autosave_error: null });

      try {
        if (artifactType === "poster" && !paletteId && !art.candidate_draft) {
          throw new Error("Select a palette before creating or revising a poster.");
        }
        const res = await applyEditsRequest({
          run_id: runIdFromArtifactId(art.artifact_id),
          conversation_id: convId,
          artifact_type: artifactType,
          ...(paletteId ? { palette_id: paletteId } : {}),
          edits: editsPayload,
        });
        // Success: replace the active artifact with the new run, clear
        // pending_edits for the OLD artifact id (the new artifact has
        // its own clean buffer keyed by its own id), and point the chat
        // card at the saved artifact so Back to Chat -> Open Canvas
        // reopens the edited version.
        patchConversation(convId, (c) => {
          if (!res.artifact) return c;
          const cleared_edits = { ...(c.pending_edits ?? {}) };
          delete cleared_edits[artId];
          const savedArtifactId = res.artifact.artifact_id;
          return {
            ...c,
            messages: c.messages.map((m) =>
              m.artifact_id === artId
                ? {
                    ...m,
                    artifact_id: savedArtifactId,
                    run_id: runIdFromArtifactId(savedArtifactId),
                  }
                : m
            ),
            artifacts: {
              ...c.artifacts,
              [savedArtifactId]: res.artifact,
            },
            active_artifact_id: savedArtifactId,
            pending_edits: cleared_edits,
          };
        });
        if (res.artifact && conv.paper_bundle?.kind === "parent") {
          const savedArtifactId = res.artifact.artifact_id;
          const bundleArtifactType = PAPER_BUNDLE_ARTIFACT_ORDER.find(
            (candidateType) => (
              conv.paper_bundle?.kind === "parent"
              && conv.paper_bundle.tasks[candidateType].artifact_id === artId
            ),
          );
          if (bundleArtifactType) {
            patchConversation(convId, (parent) => {
              if (
                parent.paper_bundle?.kind !== "parent"
                || parent.paper_bundle.tasks[bundleArtifactType].artifact_id !== artId
              ) return parent;
              return {
                ...parent,
                updated_at: Date.now(),
                paper_bundle: {
                  ...parent.paper_bundle,
                  tasks: {
                    ...parent.paper_bundle.tasks,
                    [bundleArtifactType]: {
                      ...parent.paper_bundle.tasks[bundleArtifactType],
                      run_id: runIdFromArtifactId(savedArtifactId),
                      artifact_id: savedArtifactId,
                    },
                  },
                },
              };
            });
          }
        }
        if (res.artifact && conv.paper_bundle?.kind === "child") {
          const parentId = conv.paper_bundle.parent_conversation_id;
          const bundleArtifactType = conv.paper_bundle.artifact_type;
          const savedArtifactId = res.artifact.artifact_id;
          patchConversation(parentId, (parent) => {
            if (parent.paper_bundle?.kind !== "parent") return parent;
            return {
              ...parent,
              updated_at: Date.now(),
              artifacts: {
                ...parent.artifacts,
                [savedArtifactId]: res.artifact!,
              },
              paper_bundle: {
                ...parent.paper_bundle,
                tasks: {
                  ...parent.paper_bundle.tasks,
                  [bundleArtifactType]: {
                    ...parent.paper_bundle.tasks[bundleArtifactType],
                    run_id: runIdFromArtifactId(savedArtifactId),
                    artifact_id: savedArtifactId,
                  },
                },
              },
            };
          });
        }
        set({
          autosave_state: "saved",
          autosave_last_saved_at: Date.now(),
          autosave_error: null,
        });
      } catch (err) {
        const msg = err instanceof Error ? err.message : "save failed";
        set({ autosave_state: "error", autosave_error: msg });
      } finally {
        _autosave_in_flight = false;
        if (_autosave_dirty_after_save) {
          _autosave_dirty_after_save = false;
          set({ autosave_state: "editing" });
        }
      }
    },
  };
}, persistOptions));

// ---- selectors ----

export const useCurrentConversation = (): Conversation | undefined =>
  useApp((s) => s.conversations[s.current_conversation_id]);

export const useMessages = (): Message[] =>
  useApp((s) => s.conversations[s.current_conversation_id]?.messages ?? []);

export const useArtifactById = (artifact_id: string): Artifact | undefined =>
  useApp(
    (s) => s.conversations[s.current_conversation_id]?.artifacts[artifact_id]
  );

export const useActiveArtifact = (): Artifact | null => {
  const c = useCurrentConversation();
  if (!c?.active_artifact_id) return null;
  return c.artifacts[c.active_artifact_id] ?? null;
};

export const useSelectedLayer = (): Layer | null => {
  const art = useActiveArtifact();
  const id = useApp((s) => s.selected_layer_id);
  if (!art || !id) return null;
  return art.layers.find((l) => l.layer_id === id) ?? null;
};

/** Conversations sorted most-recent first, for the history sidebar. */
export const useConversationList = (): Conversation[] =>
  useApp((s) =>
    Object.values(s.conversations)
      .filter((conversation) => conversation.paper_bundle?.kind !== "child")
      .sort((a, b) => b.updated_at - a.updated_at)
      .slice(0, MAX_HISTORY_SIDEBAR_ITEMS)
  );
