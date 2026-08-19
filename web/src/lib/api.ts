/**
 * Single seam between the UI and the Python backend.
 *
 * The frontend types in `types.ts` mirror the wire shapes in
 * scripts/web_server.py. Multipart upload because PDF attachments need
 * to ride along (paper2poster).
 *
 * The dev server (`vite.config.ts`) proxies /api/* to FastAPI on :8000.
 * To run the backend in another terminal:
 *   uv run uvicorn scripts.web_server:app --reload --port 8000
 */

import type {
  Artifact,
  ArtifactAsset,
  ArtifactType,
  Attachment,
  BackendInfo,
  Canvas,
  Conversation,
  HarnessMatrix,
  Message,
  OpenResearchArtifactState,
  PendingEditsPayload,
  PosterCanvasPresetCatalog,
  PosterPaletteCatalog,
  PosterSelectionContext,
  RecoverableTaskType,
  MessageTaskPayload,
} from "./types";
import { configHeaders, keyHeaders, setDemoMode, type ApiConfig } from "./api_settings";
import {
  normalizeRunAttemptState,
  type RunAttemptState,
} from "./attempt_candidates";

/** Typed error so the store can distinguish "needs API key" (412) from
 *  generic 5xx and pop the Settings drawer instead of just toasting an
 *  error. The backend returns `{ code: "no_api_key", message: "..." }`
 *  for that specific case. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  constructor(status: number, message: string, code: string | null = null) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export type RunStartReplay = (signal?: AbortSignal) => Promise<GenerateAck>;

export class RunStartAmbiguousError extends Error {
  readonly runId: string;
  readonly retryStart: RunStartReplay;
  constructor(runId: string, cause: unknown, retryStart: RunStartReplay) {
    super(
      cause instanceof Error
        ? `Run ${runId} may have started, but its acknowledgement was lost: ${cause.message}`
        : `Run ${runId} may have started, but its acknowledgement was lost.`,
      { cause },
    );
    this.runId = runId;
    this.retryStart = retryStart;
  }
}

export type RunStatusErrorKind =
  | "unauthorized"
  | "forbidden"
  | "not_found"
  | "invalid_response"
  | "request_rejected";

export class RunStatusError extends ApiError {
  readonly kind: RunStatusErrorKind;
  readonly retryable: boolean;
  constructor(
    status: number,
    message: string,
    kind: RunStatusErrorKind,
    retryable: boolean,
    code: string | null = null,
  ) {
    super(status, message, code);
    this.kind = kind;
    this.retryable = retryable;
  }
}

function ambiguousStartFailure(
  runId: string,
  signal: AbortSignal | undefined,
  error: unknown,
  retryStart: RunStartReplay,
): never {
  if (signal?.aborted) throw error;
  throw new RunStartAmbiguousError(runId, error, retryStart);
}

const RUN_START_ACK_TIMEOUT_MS = 60 * 1000;

async function postReservedRunStart(
  runId: string,
  startToken: string,
  signal?: AbortSignal,
  parse: (value: unknown) => GenerateAck = (value) => value as GenerateAck,
): Promise<GenerateAck> {
  const retryStart: RunStartReplay = (retrySignal) => postReservedRunStart(
    runId,
    startToken,
    retrySignal,
    parse,
  );
  const requestController = new AbortController();
  const abortRequest = () => requestController.abort(signal?.reason);
  if (signal?.aborted) {
    abortRequest();
  } else {
    signal?.addEventListener("abort", abortRequest, { once: true });
  }
  const requestTimeout = globalThis.setTimeout(
    () => requestController.abort(new Error("Run start acknowledgement timed out.")),
    RUN_START_ACK_TIMEOUT_MS,
  );
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/start`, {
      method: "POST",
      headers: {
        "X-Autodesign-Upload-Token": startToken,
        ...keyHeaders(),
      },
      signal: requestController.signal,
    });
    if (!response.ok) throw await asApiError(response, "run start failed");
    return parse(await response.json());
  } catch (error) {
    if (error instanceof ApiError) throw error;
    return ambiguousStartFailure(runId, signal, error, retryStart);
  } finally {
    globalThis.clearTimeout(requestTimeout);
    signal?.removeEventListener("abort", abortRequest);
  }
}

async function asApiError(res: Response, label: string): Promise<ApiError> {
  let detail: unknown = null;
  try {
    detail = await res.json();
  } catch {
    /* fall back below */
  }
  if (detail && typeof detail === "object" && "detail" in detail) {
    const d = (detail as { detail: unknown }).detail;
    if (typeof d === "string") {
      return new ApiError(res.status, `${label} ${res.status}: ${d}`);
    }
    if (d && typeof d === "object") {
      const obj = d as {
        code?: string;
        message?: string;
        findings?: Array<{ message?: unknown }>;
      };
      const findings = Array.isArray(obj.findings)
        ? obj.findings
          .map((finding) => (
            typeof finding?.message === "string" ? finding.message.trim() : ""
          ))
          .filter(Boolean)
        : [];
      return new ApiError(
        res.status,
        obj.message ?? (findings.join(" · ") || `${label} ${res.status}`),
        obj.code ?? null,
      );
    }
  }
  return new ApiError(
    res.status,
    `${label} ${res.status}: ${res.statusText || "unknown"}`,
  );
}

/** Returned by /api/health. `needs_setup` is true when the backend
 *  booted without any .env credential — the frontend should pop the
 *  Settings drawer on first send. */
export interface HealthInfo extends BackendInfo {
  needs_setup: boolean;
}

/**
 * One-shot probe of the FastAPI shim. Used at boot to populate
 * `AppState.backend_info` so the empty-hero footer can name the actual
 * designer model.
 */
export async function fetchHealth(): Promise<HealthInfo | null> {
  try {
    // Send credential + model-override headers so the response reflects
    // what the user picked in Settings, not just the backend's .env.
    const res = await fetch("/api/health", { headers: keyHeaders() });
    if (!res.ok) return null;
    const j = (await res.json()) as Partial<HealthInfo> & {
      status?: string;
      models?: Record<string, string>;
    };
    setDemoMode(!!j.demo_mode);
    if (!j.designer_model || !j.image_model) return null;
    return {
      designer_model: j.designer_model,
      image_model: j.image_model,
      models: j.models ?? {
        designer: j.designer_model,
        image: j.image_model,
      },
      backend_profile: j.backend_profile,
      demo_mode: !!j.demo_mode,
      public_user_isolation: !!j.public_user_isolation,
      user_isolation: !!j.user_isolation,
      demo: j.demo,
      needs_setup: !!j.needs_setup,
    };
  } catch {
    return null;
  }
}

export async function fetchPosterPalettes(): Promise<PosterPaletteCatalog> {
  const res = await fetch("/api/palettes?artifact_type=poster", {
    headers: keyHeaders(),
  });
  if (!res.ok) throw await asApiError(res, "/api/palettes");
  return (await res.json()) as PosterPaletteCatalog;
}

export async function fetchPosterCanvasPresets(): Promise<PosterCanvasPresetCatalog> {
  const res = await fetch("/api/canvas-presets?artifact_type=poster", {
    headers: keyHeaders(),
  });
  if (!res.ok) throw await asApiError(res, "/api/canvas-presets");
  return (await res.json()) as PosterCanvasPresetCatalog;
}

export interface CodingAgentSmokeResponse {
  ok: boolean;
  status: "passed" | "failed" | "timeout" | "missing_command" | "disabled" | string;
  auth_status: "verified" | "not_verified" | "unavailable" | string;
  command_detected: boolean;
  command_source?: string;
  command?: string;
  binary?: string;
  binary_version?: string;
  capabilities?: Record<string, boolean>;
  rejected_candidates?: Array<{
    binary: string;
    source: string;
    version: string;
    missing: string[];
  }>;
  harness: string;
  model?: string | null;
  timeout_s: number;
  elapsed_s: number;
  reason: string;
  returncode?: number | null;
  timed_out?: boolean;
  stdout_excerpt?: string;
  stderr_excerpt?: string;
}

export async function testCodingAgent(
  cfg?: ApiConfig,
  opts: { timeout_s?: number } = {},
): Promise<CodingAgentSmokeResponse> {
  const res = await fetch("/api/coding-agent/smoke", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...keyHeaders(),
      ...(cfg ? configHeaders(cfg) : {}),
    },
    body: JSON.stringify({ timeout_s: opts.timeout_s ?? 60 }),
  });
  if (!res.ok) {
    throw await asApiError(res, "/api/coding-agent/smoke");
  }
  return (await res.json()) as CodingAgentSmokeResponse;
}

export interface StartHarnessMatrixRequest {
  paper_path: string;
  prompt: string;
  template: string;
  harnesses: Array<{ id: string; model?: string }>;
  attempts: number;
  timeout_s: number;
  concurrency: "by_harness";
  reuse_ingest_run?: string | null;
}

export async function startHarnessMatrix(req: StartHarnessMatrixRequest): Promise<HarnessMatrix> {
  const res = await fetch("/api/harness-matrix", {
    method: "POST",
    headers: {
      ...keyHeaders(),
      "Content-Type": "application/json",
    },
    body: JSON.stringify(req),
  });
  if (!res.ok) throw await asApiError(res, "harness matrix start failed");
  const j = (await res.json()) as { matrix: HarnessMatrix };
  return j.matrix;
}

export async function fetchHarnessMatrix(matrix_id: string): Promise<HarnessMatrix> {
  const res = await fetch(`/api/harness-matrix/${encodeURIComponent(matrix_id)}`, {
    headers: keyHeaders(),
  });
  if (!res.ok) throw await asApiError(res, "harness matrix fetch failed");
  return (await res.json()) as HarnessMatrix;
}

export async function cancelHarnessMatrix(matrix_id: string): Promise<HarnessMatrix> {
  const res = await fetch(`/api/harness-matrix/${encodeURIComponent(matrix_id)}/cancel`, {
    method: "POST",
    headers: keyHeaders(),
  });
  if (!res.ok) throw await asApiError(res, "harness matrix cancel failed");
  const j = (await res.json()) as { matrix: HarnessMatrix };
  return j.matrix;
}

// ---------- /api/harness/login (Connect account) ----------

export interface HarnessLoginState {
  login_id: string;
  harness: string;
  status: "starting" | "awaiting_user" | "success" | "failed" | "cancelled" | string;
  url: string;
  message: string;
  lines: string[];
  returncode: number | null;
}

export interface HarnessAuthStatus {
  harness: string;
  available: boolean;
  logged_in: boolean;
  account: string | null;
  config_dir?: string;
  message?: string;
}

export async function startHarnessLogin(harness: string): Promise<HarnessLoginState> {
  const res = await fetch("/api/harness/login", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...keyHeaders() },
    body: JSON.stringify({ harness }),
  });
  if (!res.ok) throw await asApiError(res, "/api/harness/login");
  return (await res.json()) as HarnessLoginState;
}

export async function cancelHarnessLogin(login_id: string): Promise<HarnessLoginState> {
  const res = await fetch(`/api/harness/login/${encodeURIComponent(login_id)}/cancel`, {
    method: "POST",
    headers: keyHeaders(),
  });
  if (!res.ok) throw await asApiError(res, "/api/harness/login/cancel");
  return (await res.json()) as HarnessLoginState;
}

export async function fetchHarnessAuthStatus(harness: string): Promise<HarnessAuthStatus> {
  const res = await fetch(`/api/harness/auth-status?harness=${encodeURIComponent(harness)}`, {
    headers: keyHeaders(),
  });
  if (!res.ok) throw await asApiError(res, "/api/harness/auth-status");
  return (await res.json()) as HarnessAuthStatus;
}

/** SSE endpoint for a login session — consume with `new EventSource(url)`. */
export function harnessLoginEventsUrl(login_id: string): string {
  return `/api/harness/login/${encodeURIComponent(login_id)}/events`;
}

// ---------- /api/history ----------

export interface ServerHistoryArtifactPreview {
  artifact_id: string;
  name: string;
  artifact_type: ArtifactType;
  canvas: Canvas;
  preview_url?: string;
}

export interface ServerHistoryLastRun {
  run_id: string;
  status?: Message["status"];
  artifact_id?: string;
}

export interface ServerHistoryConversationSummary {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
  message_count: number;
  artifacts: Record<string, ServerHistoryArtifactPreview>;
  active_artifact_id: string | null;
  poster_palette_id?: string | null;
  poster_canvas_preset_id?: string | null;
  pending?: boolean;
  run_id?: string;
  pending_artifact_type?: ArtifactType;
  pending_task_type?: RecoverableTaskType;
  pending_task_payload?: MessageTaskPayload;
  last_run?: ServerHistoryLastRun;
}

export interface ServerHistoryResponse {
  conversations: Record<string, ServerHistoryConversationSummary>;
  imported_runs: number;
  user_isolated: boolean;
  request_scope: string;
}

export interface ServerHistoryOptions {
  limit?: number;
  include_design_sessions?: boolean;
}

export async function fetchServerHistory(
  options: ServerHistoryOptions = {},
): Promise<ServerHistoryResponse> {
  const params = new URLSearchParams();
  if (options.limit) params.set("limit", String(options.limit));
  if (options.include_design_sessions) {
    params.set("include_design_sessions", "true");
  }
  const path = `/api/history${params.size ? `?${params}` : ""}`;
  const headers = keyHeaders();
  const requestScope = headers["X-Demo-User"];
  const res = await fetch(path, { headers });
  if (!res.ok) {
    throw await asApiError(res, path);
  }
  const j = (await res.json()) as Partial<ServerHistoryResponse>;
  return {
    conversations: j.conversations ?? {},
    imported_runs: Number(j.imported_runs ?? 0),
    user_isolated: !!j.user_isolated,
    request_scope: requestScope,
  };
}

export interface ServerHistoryConversationResponse {
  conversation: Conversation;
  user_isolated: boolean;
  request_scope: string;
}

export async function fetchServerHistoryConversation(
  conversation_id: string,
  options: Pick<ServerHistoryOptions, "include_design_sessions"> = {},
): Promise<ServerHistoryConversationResponse> {
  const params = new URLSearchParams();
  if (options.include_design_sessions) {
    params.set("include_design_sessions", "true");
  }
  const path = `/api/history/conversations/${encodeURIComponent(conversation_id)}${
    params.size ? `?${params}` : ""
  }`;
  const headers = keyHeaders();
  const requestScope = headers["X-Demo-User"];
  const res = await fetch(path, { headers });
  if (!res.ok) {
    throw await asApiError(res, path);
  }
  const j = (await res.json()) as Partial<ServerHistoryConversationResponse>;
  if (!j.conversation) {
    throw new ApiError(res.status, `${path} returned no conversation`);
  }
  return {
    conversation: j.conversation,
    user_isolated: !!j.user_isolated,
    request_scope: requestScope,
  };
}

export async function saveServerConversation(
  conversation: Conversation,
): Promise<void> {
  const res = await fetch("/api/history/conversation", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...keyHeaders(),
    },
    body: JSON.stringify({ conversation }),
  });
  if (!res.ok) {
    throw await asApiError(res, "/api/history/conversation");
  }
}

export async function deleteServerConversation(
  conversation_id: string,
): Promise<void> {
  const res = await fetch(`/api/history/conversations/${conversation_id}`, {
    method: "DELETE",
    headers: keyHeaders(),
  });
  if (!res.ok) {
    throw await asApiError(res, `/api/history/conversations/${conversation_id}`);
  }
}

export interface GenerateRequest {
  brief: string;
  attachments: Attachment[];
  conversation_id?: string;
  /** Pre-selected artifact type from the quick-action pills. */
  artifact_type?: ArtifactType;
  palette_id?: string;
  /** Optional runner canvas preset, e.g. cvpr-landscape for paper posters. */
  template?: string;
  /** Explicit client picker snapshot. `auto` intentionally carries no template. */
  canvas_preset_id?: string;
  /** External authoring attempt allowance for this invocation. */
  authoring_max_attempts?: number;
  /** Persisted attachment metadata from a prior user turn. Used by
   *  Resume when the browser no longer has File handles after a backend
   *  restart. */
  attachment_refs?: Array<Pick<Attachment, "name" | "size" | "kind" | "role">>;
  /** Persisted metadata for recovering the separate style-reference upload. */
  reference_poster_ref?: Pick<Attachment, "name" | "size" | "kind" | "role" | "reference_handle">;
  /** When the user has manually edited an artifact and is asking for
   *  further refinements, the edited artifact is sent as the new baseline. */
  baseline_artifact?: Artifact;
  /** Last N turns of *this* conversation. The backend stitches these
   *  into the brief preamble so the designer has continuity ("now make
   *  slides for the same content" actually works). */
  conversation_history?: Array<{
    role: "user" | "assistant";
    text: string;
    artifact_id?: string;
  }>;
  /** Compact summaries of artifacts already produced in this thread.
   *  Lets the designer reuse style/canvas across turns. */
  prior_artifacts?: Array<{
    artifact_id: string;
    name: string;
    type: ArtifactType;
    canvas: { w: number; h: number };
    native_format?: string;
  }>;
}

export interface GenerateResponse {
  message: Message;
  artifact: Artifact | null;
}

/** v0.3 wire shape — /api/generate now returns immediately with run_id +
 *  a streaming placeholder. Final artifact fetched via
 *  fetchRunArtifact(run_id) once SSE delivers `run.done`. */
export interface GenerateAck {
  run_id: string;
  placeholder_message: Message;
  progress_mode?: string | null;
  reference_poster_handle?: string | null;
  start_token?: string | null;
}

export interface RunInputSlot {
  name: string;
  role: "attachment" | "reference_poster";
  sha256: string;
  size: number;
}

export interface RunReserveResponse {
  run_id: string;
  upload_token: string;
  input_slots: RunInputSlot[];
  request_digest: string;
  run_state: string;
  expires_at: number;
  reused: boolean;
}

export interface RunCancelResponse {
  http_status: number;
  run_id: string;
  status: "cancelled" | "already_cancelled" | "already_terminal" | "cancellation_pending";
  run_state: string;
  confirmed: boolean;
  terminated_pids: number[];
  surviving_pids: number[];
}

export type RunLifecycleState =
  | "reserved"
  | "uploading"
  | "queued"
  | "running"
  | "completing"
  | "completed"
  | "cancelling"
  | "cancelled"
  | "failed";

export interface RunStatusResponse {
  run_id: string;
  run_state: RunLifecycleState;
  revision: number;
  publishable: boolean;
  cancellation_pending: string | null;
  worker_pid: number | null;
  terminal_event: string | null;
}

export type PaperBundleBackendState =
  | "reserved"
  | "running"
  | "cancelling"
  | "cancelled"
  | "completed"
  | "partial"
  | "failed";

export type PaperBundleChildState =
  | "reserved"
  | "uploading"
  | "queued"
  | "running"
  | "completing"
  | "completed"
  | "cancelling"
  | "cancelled"
  | "failed";

export interface PaperBundleReserveRequest {
  brief: string;
  artifact_type: ArtifactType;
  conversation_id: string;
  input_slots: RunInputSlot[];
  palette_id?: string | null;
  baseline_artifact?: string | null;
  conversation_history?: string | null;
  prior_artifacts?: string | null;
  template?: string | null;
  canvas_preset_id?: string | null;
  authoring_max_attempts?: number | null;
}

export interface PaperBundleCreateRequest {
  job_id: string;
  conversation_id: string;
  source_name: string;
  prompt_version: string;
  children: { [TArtifactType in ArtifactType]: PaperBundleReserveRequest & {
    artifact_type: TArtifactType;
  } };
}

export interface PaperBundleInputSlotDescriptor {
  name: string;
  expected_sha256: string;
  expected_size: number;
}

export interface PaperBundleChildDescriptor {
  run_id: string;
  artifact_type: ArtifactType;
  conversation_id: string;
  input_slots: PaperBundleInputSlotDescriptor[];
  upload_token?: string;
  request_digest: string;
  expires_at: number;
  state: PaperBundleChildState;
  terminal: boolean;
  process_free: boolean;
}

export interface PaperBundlePublicationDescriptor {
  source_run_id: string;
  publication_run_id: string;
  artifact_id: string;
  source_attempt: number;
  source_candidate_id: string;
  source_candidate_sha256: string;
  generation: number;
  published_at: number;
}

export interface PaperBundleJobResponse {
  schema_version: 1 | 2;
  job_id: string;
  owner_id: string;
  conversation_id: string;
  source_name: string;
  prompt_version: string;
  state: PaperBundleBackendState;
  children: { [TArtifactType in ArtifactType]: PaperBundleChildDescriptor & {
    artifact_type: TArtifactType;
  } };
  request_digest: string;
  revision: number;
  created_at: number;
  updated_at: number;
  terminal: boolean;
  terminal_at: number | null;
  cancel_requested: boolean;
  cancel_requested_at: number | null;
  completed_children: ArtifactType[];
  publications: Partial<Record<ArtifactType, PaperBundlePublicationDescriptor>>;
}

export interface PaperBundleCreateResponse extends PaperBundleJobResponse {
  reused: boolean;
}

export interface PaperBundleCancelResponse {
  http_status: number;
  job_id: string;
  owner_id?: string;
  state: PaperBundleBackendState;
  status: "cancelled" | "already_cancelled" | "already_terminal" | "cancellation_pending";
  confirmed: boolean;
  children: Partial<Record<ArtifactType, PaperBundleChildDescriptor>>;
  publications?: Partial<Record<ArtifactType, PaperBundlePublicationDescriptor>>;
  revision?: number;
  pending_creation?: boolean;
  factory_quiesced?: boolean;
}

const RUN_CANCEL_STATUSES: ReadonlySet<RunCancelResponse["status"]> = new Set([
  "cancelled",
  "already_cancelled",
  "already_terminal",
  "cancellation_pending",
]);
const RUN_LIFECYCLE_STATES: ReadonlySet<RunLifecycleState> = new Set([
  "reserved",
  "uploading",
  "queued",
  "running",
  "completing",
  "completed",
  "cancelling",
  "cancelled",
  "failed",
]);

function isPidList(value: unknown): value is number[] {
  return Array.isArray(value)
    && value.every((pid) => Number.isInteger(pid) && pid >= 0);
}

const PAPER_BUNDLE_ARTIFACT_TYPES = [
  "poster",
  "deck",
  "landing",
  "video",
] as const satisfies readonly ArtifactType[];
const PAPER_BUNDLE_PARENT_STATES: ReadonlySet<PaperBundleBackendState> = new Set([
  "reserved",
  "running",
  "cancelling",
  "cancelled",
  "completed",
  "partial",
  "failed",
]);
const PAPER_BUNDLE_PARENT_TERMINAL_STATES: ReadonlySet<PaperBundleBackendState> = new Set([
  "cancelled",
  "completed",
  "partial",
  "failed",
]);
const PAPER_BUNDLE_CHILD_STATES: ReadonlySet<PaperBundleChildState> = new Set([
  "reserved",
  "uploading",
  "queued",
  "running",
  "completing",
  "completed",
  "cancelling",
  "cancelled",
  "failed",
]);
const PAPER_BUNDLE_CHILD_TERMINAL_STATES: ReadonlySet<PaperBundleChildState> = new Set([
  "completed",
  "cancelled",
  "failed",
]);
const PAPER_BUNDLE_CANCEL_STATUSES: ReadonlySet<PaperBundleCancelResponse["status"]> = new Set([
  "cancelled",
  "already_cancelled",
  "already_terminal",
  "cancellation_pending",
]);
const SAFE_IDENTIFIER_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const PAPER_BUNDLE_PUBLICATION_FIELDS = new Set([
  "source_run_id",
  "publication_run_id",
  "artifact_id",
  "source_attempt",
  "source_candidate_id",
  "source_candidate_sha256",
  "generation",
  "published_at",
]);

function isObject(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isNullableFiniteNumber(value: unknown): value is number | null {
  return value === null || isFiniteNumber(value);
}

function parsePaperBundleChild(
  value: unknown,
  artifactType: ArtifactType,
  requireUploadToken: boolean,
  label: string,
): PaperBundleChildDescriptor {
  if (!isObject(value)) throw new ApiError(502, label);
  const state = typeof value.state === "string"
    && PAPER_BUNDLE_CHILD_STATES.has(value.state as PaperBundleChildState)
    ? value.state as PaperBundleChildState
    : null;
  const terminal = typeof value.terminal === "boolean" ? value.terminal : null;
  const slots = Array.isArray(value.input_slots) ? value.input_slots : null;
  const parsedSlots = slots?.map((slot) => {
    if (
      !isObject(slot)
      || typeof slot.name !== "string"
      || !SAFE_IDENTIFIER_PATTERN.test(slot.name)
      || typeof slot.expected_sha256 !== "string"
      || !SHA256_PATTERN.test(slot.expected_sha256)
      || !Number.isInteger(slot.expected_size)
      || (slot.expected_size as number) < 0
    ) {
      throw new ApiError(502, label);
    }
    return {
      name: slot.name,
      expected_sha256: slot.expected_sha256,
      expected_size: slot.expected_size as number,
    };
  });
  const uploadToken = typeof value.upload_token === "string"
    && value.upload_token.length > 0
    ? value.upload_token
    : undefined;
  if (
    typeof value.run_id !== "string"
    || !SAFE_IDENTIFIER_PATTERN.test(value.run_id)
    || value.artifact_type !== artifactType
    || typeof value.conversation_id !== "string"
    || value.conversation_id.length === 0
    || !parsedSlots
    || parsedSlots.length === 0
    || new Set(parsedSlots.map((slot) => slot.name)).size !== parsedSlots.length
    || (requireUploadToken ? !uploadToken : value.upload_token !== undefined)
    || typeof value.request_digest !== "string"
    || !SHA256_PATTERN.test(value.request_digest)
    || !isFiniteNumber(value.expires_at)
    || !state
    || terminal === null
    || terminal !== PAPER_BUNDLE_CHILD_TERMINAL_STATES.has(state)
    || typeof value.process_free !== "boolean"
  ) {
    throw new ApiError(502, label);
  }
  return {
    run_id: value.run_id,
    artifact_type: artifactType,
    conversation_id: value.conversation_id,
    input_slots: parsedSlots,
    ...(uploadToken ? { upload_token: uploadToken } : {}),
    request_digest: value.request_digest,
    expires_at: value.expires_at,
    state,
    terminal,
    process_free: value.process_free,
  };
}

function parsePaperBundleChildren(
  value: unknown,
  requireUploadToken: boolean,
  allowEmpty: boolean,
  label: string,
): PaperBundleJobResponse["children"] | Partial<Record<ArtifactType, PaperBundleChildDescriptor>> {
  if (!isObject(value)) throw new ApiError(502, label);
  const keys = Object.keys(value);
  if (allowEmpty && keys.length === 0) return {};
  if (
    keys.length !== PAPER_BUNDLE_ARTIFACT_TYPES.length
    || PAPER_BUNDLE_ARTIFACT_TYPES.some((artifactType) => !(artifactType in value))
  ) {
    throw new ApiError(502, label);
  }
  const children = Object.fromEntries(PAPER_BUNDLE_ARTIFACT_TYPES.map((artifactType) => [
    artifactType,
    parsePaperBundleChild(value[artifactType], artifactType, requireUploadToken, label),
  ])) as PaperBundleJobResponse["children"];
  const runIds = Object.values(children).map((child) => child.run_id);
  if (new Set(runIds).size !== runIds.length) throw new ApiError(502, label);
  return children;
}

function parsePaperBundlePublications(
  value: unknown,
  schemaVersion: 1 | 2,
  label: string,
): Partial<Record<ArtifactType, PaperBundlePublicationDescriptor>> {
  if (schemaVersion === 1) {
    if (value !== undefined) throw new ApiError(502, label);
    return {};
  }
  if (!isObject(value)) throw new ApiError(502, label);
  const publications: Partial<Record<ArtifactType, PaperBundlePublicationDescriptor>> = {};
  for (const [artifactType, raw] of Object.entries(value)) {
    if (
      !PAPER_BUNDLE_ARTIFACT_TYPES.includes(artifactType as ArtifactType)
      || !isObject(raw)
      || Object.keys(raw).length !== PAPER_BUNDLE_PUBLICATION_FIELDS.size
      || Object.keys(raw).some((field) => !PAPER_BUNDLE_PUBLICATION_FIELDS.has(field))
      || typeof raw.source_run_id !== "string"
      || !SAFE_IDENTIFIER_PATTERN.test(raw.source_run_id)
      || typeof raw.publication_run_id !== "string"
      || !SAFE_IDENTIFIER_PATTERN.test(raw.publication_run_id)
      || raw.publication_run_id === raw.source_run_id
      || typeof raw.artifact_id !== "string"
      || !SAFE_IDENTIFIER_PATTERN.test(raw.artifact_id)
      || !Number.isInteger(raw.source_attempt)
      || (raw.source_attempt as number) <= 0
      || typeof raw.source_candidate_id !== "string"
      || !SAFE_IDENTIFIER_PATTERN.test(raw.source_candidate_id)
      || typeof raw.source_candidate_sha256 !== "string"
      || !SHA256_PATTERN.test(raw.source_candidate_sha256)
      || !Number.isInteger(raw.generation)
      || (raw.generation as number) <= 0
      || !isFiniteNumber(raw.published_at)
    ) {
      throw new ApiError(502, label);
    }
    publications[artifactType as ArtifactType] = {
      source_run_id: raw.source_run_id,
      publication_run_id: raw.publication_run_id,
      artifact_id: raw.artifact_id,
      source_attempt: raw.source_attempt as number,
      source_candidate_id: raw.source_candidate_id,
      source_candidate_sha256: raw.source_candidate_sha256,
      generation: raw.generation as number,
      published_at: raw.published_at,
    };
  }
  return publications;
}

function parsePaperBundleJob(
  value: unknown,
  requireUploadToken: boolean,
  label = "Invalid paper bundle response.",
): PaperBundleJobResponse {
  if (!isObject(value)) throw new ApiError(502, label);
  const schemaVersion = value.schema_version === 1 || value.schema_version === 2
    ? value.schema_version
    : null;
  const state = typeof value.state === "string"
    && PAPER_BUNDLE_PARENT_STATES.has(value.state as PaperBundleBackendState)
    ? value.state as PaperBundleBackendState
    : null;
  const terminal = typeof value.terminal === "boolean" ? value.terminal : null;
  const completedChildren = Array.isArray(value.completed_children)
    && value.completed_children.every((artifactType) => (
      typeof artifactType === "string"
      && PAPER_BUNDLE_ARTIFACT_TYPES.includes(artifactType as ArtifactType)
    ))
    ? value.completed_children as ArtifactType[]
    : null;
  if (
    !schemaVersion
    || typeof value.job_id !== "string"
    || !SAFE_IDENTIFIER_PATTERN.test(value.job_id)
    || typeof value.owner_id !== "string"
    || value.owner_id.length === 0
    || typeof value.conversation_id !== "string"
    || value.conversation_id.length === 0
    || typeof value.source_name !== "string"
    || value.source_name.length === 0
    || typeof value.prompt_version !== "string"
    || value.prompt_version.length === 0
    || !state
    || typeof value.request_digest !== "string"
    || !SHA256_PATTERN.test(value.request_digest)
    || !Number.isInteger(value.revision)
    || (value.revision as number) < 0
    || !isFiniteNumber(value.created_at)
    || !isFiniteNumber(value.updated_at)
    || value.updated_at < value.created_at
    || terminal === null
    || terminal !== PAPER_BUNDLE_PARENT_TERMINAL_STATES.has(state)
    || !isNullableFiniteNumber(value.terminal_at)
    || terminal !== (value.terminal_at !== null)
    || typeof value.cancel_requested !== "boolean"
    || !isNullableFiniteNumber(value.cancel_requested_at)
    || value.cancel_requested !== (value.cancel_requested_at !== null)
    || !completedChildren
    || new Set(completedChildren).size !== completedChildren.length
  ) {
    throw new ApiError(502, label);
  }
  const children = parsePaperBundleChildren(
    value.children,
    requireUploadToken,
    false,
    label,
  ) as PaperBundleJobResponse["children"];
  const publications = parsePaperBundlePublications(
    value.publications,
    schemaVersion,
    label,
  );
  const createdAt = value.created_at as number;
  const updatedAt = value.updated_at as number;
  const publicationValues = Object.values(publications);
  if (
    publicationValues.some((publication) => (
      publication.published_at < createdAt
      || publication.published_at > updatedAt
    ))
    || new Set(publicationValues.map((publication) => publication.publication_run_id)).size
      !== publicationValues.length
    || new Set(publicationValues.map((publication) => publication.artifact_id)).size
      !== publicationValues.length
    || PAPER_BUNDLE_ARTIFACT_TYPES.some((artifactType) => {
      const publication = publications[artifactType];
      if (!publication) return false;
      const source = children[artifactType];
      return publication.source_run_id !== source.run_id
        || (source.state !== "completed" && source.state !== "failed")
        || !source.terminal
        || !source.process_free;
    })
  ) {
    throw new ApiError(502, label);
  }
  if (
    terminal
    && PAPER_BUNDLE_ARTIFACT_TYPES.some((artifactType) => (
      !children[artifactType].terminal || !children[artifactType].process_free
    ))
  ) {
    throw new ApiError(502, label);
  }
  const completedFromChildren = PAPER_BUNDLE_ARTIFACT_TYPES.filter(
    (artifactType) => (
      publications[artifactType] !== undefined
      || (
        children[artifactType].state === "completed"
        && children[artifactType].terminal
        && children[artifactType].process_free
      )
    ),
  );
  if (
    completedFromChildren.length !== completedChildren.length
    || completedFromChildren.some((artifactType) => !completedChildren.includes(artifactType))
  ) {
    throw new ApiError(502, label);
  }
  const childStates = new Set(
    PAPER_BUNDLE_ARTIFACT_TYPES.map((artifactType) => children[artifactType].state),
  );
  if (
    (state === "cancelling" && !value.cancel_requested)
    || (state === "completed" && completedChildren.length !== PAPER_BUNDLE_ARTIFACT_TYPES.length)
    || (
      state === "partial"
      && (completedChildren.length === 0
        || completedChildren.length === PAPER_BUNDLE_ARTIFACT_TYPES.length)
    )
    || (state === "failed" && (
      completedChildren.length > 0
      || (childStates.size === 1 && childStates.has("cancelled"))
    ))
    || (state === "cancelled" && !value.cancel_requested && (
      childStates.size !== 1 || !childStates.has("cancelled")
    ))
  ) {
    throw new ApiError(502, label);
  }
  return {
    schema_version: schemaVersion,
    job_id: value.job_id,
    owner_id: value.owner_id,
    conversation_id: value.conversation_id,
    source_name: value.source_name,
    prompt_version: value.prompt_version,
    state,
    children,
    request_digest: value.request_digest,
    revision: value.revision as number,
    created_at: value.created_at,
    updated_at: value.updated_at,
    terminal,
    terminal_at: value.terminal_at,
    cancel_requested: value.cancel_requested,
    cancel_requested_at: value.cancel_requested_at,
    completed_children: completedChildren,
    publications,
  };
}

function parseRunCancelResponse(
  value: unknown,
  requestedRunId: string,
  httpStatus: number,
): RunCancelResponse {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new ApiError(httpStatus, "Invalid run cancellation response.");
  }
  const body = value as Record<string, unknown>;
  const status = typeof body.status === "string"
    && RUN_CANCEL_STATUSES.has(body.status as RunCancelResponse["status"])
    ? body.status as RunCancelResponse["status"]
    : null;
  if (
    body.run_id !== requestedRunId
    || typeof body.confirmed !== "boolean"
    || !status
    || typeof body.run_state !== "string"
    || !isPidList(body.terminated_pids)
    || !isPidList(body.surviving_pids)
    || (
      (status === "cancelled" || status === "already_cancelled")
      && body.run_state !== "cancelled"
    )
    || (
      status === "already_terminal"
      && body.run_state !== "completed"
      && body.run_state !== "failed"
    )
    || (
      body.confirmed
      && status !== "cancellation_pending"
      && body.surviving_pids.length > 0
    )
    || (
      status === "cancellation_pending"
      && (body.confirmed || body.run_state !== "cancelling")
    )
  ) {
    throw new ApiError(httpStatus, "Invalid run cancellation response.");
  }
  return {
    http_status: httpStatus,
    run_id: requestedRunId,
    status,
    run_state: body.run_state,
    confirmed: body.confirmed,
    terminated_pids: body.terminated_pids,
    surviving_pids: body.surviving_pids,
  };
}

export interface StartGenerateOptions {
  /** Keep legacy multipart behavior unless the ordinary Run flow opts in. */
  reserveUploads?: boolean;
  onReserved?: (run_id: string) => void;
}

interface PreparedRunInput {
  attachment: Attachment & { file: File };
  slot: RunInputSlot;
}

function safeUploadSuffix(name: string): string {
  const match = name.match(/(\.[a-zA-Z0-9]{1,12})$/);
  return match ? match[1].toLowerCase() : "";
}

async function sha256Hex(file: File): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export interface PreparedPaperBundleInput {
  file: File;
  slot: RunInputSlot;
}

export async function preparePaperBundleInput(file: File): Promise<PreparedPaperBundleInput> {
  return {
    file,
    slot: {
      name: `attachment-0${safeUploadSuffix(file.name)}`,
      role: "attachment",
      sha256: await sha256Hex(file),
      size: file.size,
    },
  };
}

async function prepareRunInputs(attachments: Attachment[]): Promise<PreparedRunInput[]> {
  const prepared: PreparedRunInput[] = [];
  let attachmentIndex = 0;
  for (const attachment of attachments) {
    if (!attachment.file) continue;
    const role = attachment.role === "style_reference"
      ? "reference_poster" as const
      : "attachment" as const;
    const name = role === "reference_poster"
      ? `reference-poster${safeUploadSuffix(attachment.name)}`
      : `attachment-${attachmentIndex++}${safeUploadSuffix(attachment.name)}`;
    prepared.push({
      attachment: attachment as Attachment & { file: File },
      slot: {
        name,
        role,
        sha256: await sha256Hex(attachment.file),
        size: attachment.file.size,
      },
    });
  }
  return prepared;
}

async function startReservedGenerate(
  req: GenerateRequest,
  signal: AbortSignal | undefined,
  options: StartGenerateOptions,
): Promise<GenerateAck> {
  const inputs = await prepareRunInputs(req.attachments);
  const reserve = await fetch("/api/runs/reserve", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": crypto.randomUUID(),
      ...keyHeaders(),
    },
    body: JSON.stringify({
      brief: req.brief,
      artifact_type: req.artifact_type ?? null,
      palette_id: req.palette_id ?? null,
      baseline_artifact: req.baseline_artifact ? JSON.stringify(req.baseline_artifact) : null,
      conversation_history: req.conversation_history
        ? JSON.stringify(req.conversation_history)
        : null,
      prior_artifacts: req.prior_artifacts ? JSON.stringify(req.prior_artifacts) : null,
      conversation_id: req.conversation_id ?? null,
      template: req.template ?? null,
      canvas_preset_id: req.canvas_preset_id ?? null,
      authoring_max_attempts: req.authoring_max_attempts ?? null,
      input_slots: inputs.map(({ slot }) => slot),
    }),
    signal,
  });
  if (!reserve.ok) throw await asApiError(reserve, "/api/runs/reserve");
  const reservation = (await reserve.json()) as RunReserveResponse;
  options.onReserved?.(reservation.run_id);

  for (const { attachment, slot } of inputs) {
    const upload = await fetch(
      `/api/runs/${encodeURIComponent(reservation.run_id)}/inputs/${encodeURIComponent(slot.name)}`,
      {
        method: "PUT",
        headers: {
          "X-Autodesign-Upload-Token": reservation.upload_token,
          ...keyHeaders(),
        },
        body: attachment.file,
        signal,
      },
    );
    if (!upload.ok) throw await asApiError(upload, "run input upload failed");
  }

  return postReservedRunStart(reservation.run_id, reservation.upload_token, signal);
}

export async function startGenerate(
  req: GenerateRequest,
  signal?: AbortSignal,
  options: StartGenerateOptions = {},
): Promise<GenerateAck> {
  if (options.reserveUploads) {
    return startReservedGenerate(req, signal, options);
  }
  const fd = new FormData();
  fd.append("brief", req.brief);
  if (req.conversation_id) fd.append("conversation_id", req.conversation_id);
  if (req.artifact_type) fd.append("artifact_type", req.artifact_type);
  if (req.palette_id) fd.append("palette_id", req.palette_id);
  if (req.template) fd.append("template", req.template);
  if (req.canvas_preset_id) fd.append("canvas_preset_id", req.canvas_preset_id);
  if (req.authoring_max_attempts !== undefined) {
    fd.append("authoring_max_attempts", String(req.authoring_max_attempts));
  }
  if (req.baseline_artifact) {
    fd.append("baseline_artifact", JSON.stringify(req.baseline_artifact));
  }
  if (req.conversation_history && req.conversation_history.length > 0) {
    fd.append("conversation_history", JSON.stringify(req.conversation_history));
  }
  if (req.prior_artifacts && req.prior_artifacts.length > 0) {
    fd.append("prior_artifacts", JSON.stringify(req.prior_artifacts));
  }
  if (req.attachment_refs && req.attachment_refs.length > 0) {
    fd.append("attachment_refs", JSON.stringify(req.attachment_refs));
  }
  if (req.reference_poster_ref) {
    fd.append("reference_poster_ref", JSON.stringify([req.reference_poster_ref]));
  }
  for (const a of req.attachments) {
    if (!a.file) continue;
    if (a.role === "style_reference") {
      fd.set("reference_poster", a.file, a.name);
    } else {
      fd.append("files", a.file, a.name);
    }
  }

  const res = await fetch("/api/generate", {
    method: "POST",
    body: fd,
    headers: {
      ...keyHeaders(),
      "X-Autodesign-Reserve-Only": "true",
    },
    signal,
  });
  if (!res.ok) {
    throw await asApiError(res, "/api/generate");
  }
  const ack = (await res.json()) as GenerateAck;
  return startAcknowledgedRun(ack, signal, options.onReserved);
}

async function startAcknowledgedRun(
  ack: GenerateAck,
  signal?: AbortSignal,
  onReserved?: (run_id: string) => void,
): Promise<GenerateAck> {
  onReserved?.(ack.run_id);
  if (!ack.start_token) return ack;
  return postReservedRunStart(ack.run_id, ack.start_token, signal);
}

export async function cancelRunRequest(
  run_id: string,
  signal?: AbortSignal,
): Promise<RunCancelResponse> {
  const res = await fetch(`/api/runs/${encodeURIComponent(run_id)}/cancel`, {
    method: "POST",
    headers: keyHeaders(),
    signal,
  });
  if (!res.ok) throw await asApiError(res, "run cancel failed");
  return parseRunCancelResponse(await res.json(), run_id, res.status);
}

export async function fetchRunStatus(
  run_id: string,
  signal?: AbortSignal,
): Promise<RunStatusResponse> {
  const res = await fetch(`/api/runs/${encodeURIComponent(run_id)}/status`, {
    headers: keyHeaders(),
    signal,
  });
  if (!res.ok) {
    const error = await asApiError(res, "run status failed");
    const kind: RunStatusErrorKind = res.status === 401
      ? "unauthorized"
      : res.status === 403
        ? "forbidden"
        : res.status === 404
          ? "not_found"
          : "request_rejected";
    const retryable = res.status >= 500
      || res.status === 408
      || res.status === 425
      || res.status === 429;
    throw new RunStatusError(res.status, error.message, kind, retryable, error.code);
  }
  let value: unknown;
  try {
    value = await res.json();
  } catch {
    throw new RunStatusError(
      res.status,
      "Invalid run status response.",
      "invalid_response",
      false,
    );
  }
  const workerPid = isObject(value) ? value.worker_pid : undefined;
  if (
    !isObject(value)
    || value.run_id !== run_id
    || typeof value.run_state !== "string"
    || !RUN_LIFECYCLE_STATES.has(value.run_state as RunLifecycleState)
    || !Number.isInteger(value.revision)
    || (value.revision as number) < 0
    || typeof value.publishable !== "boolean"
    || !(value.cancellation_pending === null || typeof value.cancellation_pending === "string")
    || !(workerPid === null || (
      typeof workerPid === "number"
      && Number.isInteger(workerPid)
      && workerPid > 0
    ))
    || !(value.terminal_event === null || typeof value.terminal_event === "string")
  ) {
    throw new RunStatusError(
      res.status,
      "Invalid run status response.",
      "invalid_response",
      false,
    );
  }
  return {
    run_id: value.run_id as string,
    run_state: value.run_state as RunLifecycleState,
    revision: value.revision as number,
    publishable: value.publishable as boolean,
    cancellation_pending: value.cancellation_pending as string | null,
    worker_pid: workerPid,
    terminal_event: value.terminal_event as string | null,
  };
}

export async function createPaperBundle(
  request: PaperBundleCreateRequest,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<PaperBundleCreateResponse> {
  const res = await fetch("/api/paper-bundles", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
      ...keyHeaders(),
    },
    body: JSON.stringify(request),
    signal,
  });
  if (!res.ok) throw await asApiError(res, "paper bundle create failed");
  const value = await res.json() as unknown;
  const parsed = parsePaperBundleJob(value, true);
  const childrenMatchRequest = PAPER_BUNDLE_ARTIFACT_TYPES.every((artifactType) => {
    const expected = request.children[artifactType];
    const actual = parsed.children[artifactType];
    return actual.conversation_id === expected.conversation_id
      && actual.input_slots.length === expected.input_slots.length
      && expected.input_slots.every((slot, index) => {
        const echoed = actual.input_slots[index];
        return echoed.name === slot.name
          && echoed.expected_sha256 === slot.sha256
          && echoed.expected_size === slot.size;
      });
  });
  if (
    !isObject(value)
    || typeof value.reused !== "boolean"
    || parsed.job_id !== request.job_id
    || parsed.conversation_id !== request.conversation_id
    || !childrenMatchRequest
  ) {
    throw new ApiError(502, "Invalid paper bundle response.");
  }
  return { ...parsed, reused: value.reused };
}

export async function listPaperBundles(): Promise<PaperBundleJobResponse[]> {
  const res = await fetch("/api/paper-bundles", { headers: keyHeaders() });
  if (!res.ok) throw await asApiError(res, "paper bundle list failed");
  const value = await res.json() as unknown;
  if (!Array.isArray(value)) {
    throw new ApiError(502, "Invalid paper bundle response.");
  }
  return value.map((record) => parsePaperBundleJob(record, false));
}

export async function getPaperBundle(
  jobId: string,
  signal?: AbortSignal,
): Promise<PaperBundleJobResponse> {
  const res = await fetch(`/api/paper-bundles/${encodeURIComponent(jobId)}`, {
    headers: keyHeaders(),
    signal,
  });
  if (!res.ok) throw await asApiError(res, "paper bundle fetch failed");
  const parsed = parsePaperBundleJob(await res.json(), false);
  if (parsed.job_id !== jobId) {
    throw new ApiError(502, "Invalid paper bundle response.");
  }
  return parsed;
}

export async function uploadReservedRunInput(
  child: PaperBundleChildDescriptor,
  slotName: string,
  file: File,
  signal?: AbortSignal,
): Promise<void> {
  if (!child.upload_token) {
    throw new ApiError(409, "Paper bundle upload token is unavailable.");
  }
  const expected = child.input_slots.find((slot) => slot.name === slotName);
  if (!expected || expected.expected_size !== file.size) {
    throw new ApiError(409, "Paper bundle input does not match its reservation.");
  }
  const res = await fetch(
    `/api/runs/${encodeURIComponent(child.run_id)}/inputs/${encodeURIComponent(slotName)}`,
    {
      method: "PUT",
      headers: {
        "X-Autodesign-Upload-Token": child.upload_token,
        ...keyHeaders(),
      },
      body: file,
      signal,
    },
  );
  if (!res.ok) throw await asApiError(res, "run input upload failed");
  const value = await res.json() as unknown;
  if (
    !isObject(value)
    || value.run_id !== child.run_id
    || value.slot !== slotName
    || value.sha256 !== expected.expected_sha256
    || value.size !== expected.expected_size
    || typeof value.run_state !== "string"
    || typeof value.idempotent !== "boolean"
  ) {
    throw new ApiError(502, "Invalid run input upload response.");
  }
}

export async function startReservedRun(
  child: PaperBundleChildDescriptor,
  signal?: AbortSignal,
): Promise<GenerateAck> {
  if (!child.upload_token) {
    throw new ApiError(409, "Paper bundle upload token is unavailable.");
  }
  return postReservedRunStart(child.run_id, child.upload_token, signal, (value) => {
    if (
      !isObject(value)
      || value.run_id !== child.run_id
      || !isObject(value.placeholder_message)
      || typeof value.placeholder_message.id !== "string"
      || value.placeholder_message.role !== "assistant"
      || typeof value.placeholder_message.text !== "string"
      || !isFiniteNumber(value.placeholder_message.ts)
    ) {
      throw new ApiError(502, "Invalid run start response.");
    }
    return value as unknown as GenerateAck;
  });
}

export async function cancelPaperBundleRequest(
  jobId: string,
  signal?: AbortSignal,
): Promise<PaperBundleCancelResponse> {
  const res = await fetch(`/api/paper-bundles/${encodeURIComponent(jobId)}/cancel`, {
    method: "POST",
    headers: keyHeaders(),
    signal,
  });
  if (!res.ok && res.status !== 202) {
    throw await asApiError(res, "paper bundle cancel failed");
  }
  const value = await res.json() as unknown;
  const label = "Invalid paper bundle cancellation response.";
  if (!isObject(value)) throw new ApiError(502, label);
  const state = typeof value.state === "string"
    && PAPER_BUNDLE_PARENT_STATES.has(value.state as PaperBundleBackendState)
    ? value.state as PaperBundleBackendState
    : null;
  const status = typeof value.status === "string"
    && PAPER_BUNDLE_CANCEL_STATUSES.has(value.status as PaperBundleCancelResponse["status"])
    ? value.status as PaperBundleCancelResponse["status"]
    : null;
  const confirmed = typeof value.confirmed === "boolean" ? value.confirmed : null;
  if (value.job_id !== jobId || !state || !status || confirmed === null) {
    throw new ApiError(502, label);
  }
  let children: Partial<Record<ArtifactType, PaperBundleChildDescriptor>>;
  let revision: number | undefined;
  let ownerId: string | undefined;
  let pendingCreation: boolean | undefined;
  let factoryQuiesced: boolean | undefined;
  let publications: PaperBundleCancelResponse["publications"];
  if (value.schema_version !== undefined) {
    const record = parsePaperBundleJob(value, false, label);
    children = record.children;
    publications = record.publications;
    revision = record.revision;
    ownerId = record.owner_id;
  } else {
    children = parsePaperBundleChildren(value.children, false, true, label);
    if (value.owner_id !== undefined) {
      if (typeof value.owner_id !== "string" || value.owner_id.length === 0) {
        throw new ApiError(502, label);
      }
      ownerId = value.owner_id;
    }
    if (value.pending_creation !== undefined) {
      if (typeof value.pending_creation !== "boolean") throw new ApiError(502, label);
      pendingCreation = value.pending_creation;
    }
    if (value.factory_quiesced !== undefined) {
      if (typeof value.factory_quiesced !== "boolean") throw new ApiError(502, label);
      factoryQuiesced = value.factory_quiesced;
    }
  }
  const childValues = Object.values(children);
  const childrenAreQuiescent = (
    childValues.length === PAPER_BUNDLE_ARTIFACT_TYPES.length
    && childValues.every((child) => child.terminal && child.process_free)
  );
  const creationTombstoneIsQuiescent = (
    childValues.length === 0
    && pendingCreation === true
    && factoryQuiesced === true
    && ownerId !== undefined
  );
  const confirmationIsQuiescent = childrenAreQuiescent || creationTombstoneIsQuiescent;
  const valid = confirmed
    ? (
        res.status === 200
        && status !== "cancellation_pending"
        && PAPER_BUNDLE_PARENT_TERMINAL_STATES.has(state)
        && confirmationIsQuiescent
        && (
          (status === "cancelled" || status === "already_cancelled")
            ? state === "cancelled"
            : status === "already_terminal" && state !== "cancelled"
        )
      )
    : (
        res.status === 202
        && status === "cancellation_pending"
        && state === "cancelling"
      );
  if (!valid) throw new ApiError(502, label);
  return {
    http_status: res.status,
    job_id: jobId,
    ...(ownerId === undefined ? {} : { owner_id: ownerId }),
    state,
    status,
    confirmed,
    children,
    ...(publications === undefined ? {} : { publications }),
    ...(revision === undefined ? {} : { revision }),
    ...(pendingCreation === undefined ? {} : { pending_creation: pendingCreation }),
    ...(factoryQuiesced === undefined ? {} : { factory_quiesced: factoryQuiesced }),
  };
}

export interface PosterCodeEditRequest {
  artifact: Artifact;
  instruction: string;
  conversation_id?: string;
  palette_id?: string;
  conversation_history?: GenerateRequest["conversation_history"];
  selection_context?: PosterSelectionContext;
}

export async function startPosterCodeEdit(
  req: PosterCodeEditRequest,
  signal?: AbortSignal,
  onReserved?: (run_id: string) => void,
): Promise<GenerateAck> {
  const res = await fetch("/api/code-edit/poster", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Autodesign-Reserve-Only": "true",
      ...keyHeaders(),
    },
    body: JSON.stringify({
      artifact: req.artifact,
      instruction: req.instruction,
      conversation_id: req.conversation_id,
      palette_id: req.palette_id,
      conversation_history: req.conversation_history ?? [],
      source_run_id: runIdFromArtifactId(req.artifact.artifact_id),
      selection_context: req.selection_context ?? null,
    }),
    signal,
  });
  if (!res.ok) {
    throw await asApiError(res, "/api/code-edit/poster");
  }
  return startAcknowledgedRun(
    (await res.json()) as GenerateAck,
    signal,
    onReserved,
  );
}

export async function fetchRunArtifact(
  run_id: string,
  signal?: AbortSignal,
): Promise<GenerateResponse> {
  const res = await fetch(`/api/runs/${run_id}/artifact`, {
    headers: keyHeaders(),
    signal,
  });
  if (!res.ok) {
    throw await asApiError(res, `/api/runs/${run_id}/artifact`);
  }
  return (await res.json()) as GenerateResponse;
}

export async function fetchRunAttempts(
  run_id: string,
  signal?: AbortSignal,
): Promise<RunAttemptState> {
  const res = await fetch(
    `/api/runs/${encodeURIComponent(run_id)}/attempts`,
    { headers: keyHeaders(), signal },
  );
  if (!res.ok) throw await asApiError(res, "attempt history");
  return normalizeRunAttemptState(await res.json());
}

export async function selectRunAttempt(
  run_id: string,
  attempt: number,
  expected_candidate_sha256: string,
  idempotency_key: string,
): Promise<RunAttemptState> {
  const res = await fetch(
    `/api/runs/${encodeURIComponent(run_id)}/attempts/${attempt}/select`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", ...keyHeaders() },
      body: JSON.stringify({ expected_candidate_sha256, idempotency_key }),
    },
  );
  if (!res.ok) throw await asApiError(res, "attempt selection");
  return normalizeRunAttemptState(await res.json());
}

export async function publishRunAttempt(
  run_id: string,
  attempt: number,
  expected_candidate_sha256: string,
  idempotency_key: string,
  conversation_id?: string,
  signal?: AbortSignal,
  onReserved?: (run_id: string) => void,
): Promise<GenerateAck> {
  const res = await fetch(
    `/api/runs/${encodeURIComponent(run_id)}/attempts/${attempt}/publish`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Autodesign-Reserve-Only": "true",
        ...keyHeaders(),
      },
      body: JSON.stringify({
        conversation_id: conversation_id || null,
        expected_candidate_sha256,
        idempotency_key,
      }),
      signal,
    },
  );
  if (!res.ok) throw await asApiError(res, "attempt publication");
  return startAcknowledgedRun(
    (await res.json()) as GenerateAck,
    signal,
    onReserved,
  );
}

export async function forkRunAttempt(
  run_id: string,
  attempt: number,
  conversation_id?: string,
  signal?: AbortSignal,
  onReserved?: (run_id: string) => void,
): Promise<GenerateAck> {
  const res = await fetch(
    `/api/runs/${encodeURIComponent(run_id)}/attempts/${attempt}/fork`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Autodesign-Reserve-Only": "true",
        ...keyHeaders(),
      },
      body: JSON.stringify({ conversation_id: conversation_id || null }),
      signal,
    },
  );
  if (!res.ok) throw await asApiError(res, "attempt fork");
  return startAcknowledgedRun(
    (await res.json()) as GenerateAck,
    signal,
    onReserved,
  );
}

export async function publishCandidateDraft(
  artifact_id: string,
  conversation_id?: string,
  signal?: AbortSignal,
  onReserved?: (run_id: string) => void,
): Promise<GenerateAck> {
  const res = await fetch(
    `/api/artifacts/${encodeURIComponent(artifact_id)}/publish-candidate-draft`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Autodesign-Reserve-Only": "true",
        ...keyHeaders(),
      },
      body: JSON.stringify({ conversation_id: conversation_id || null }),
      signal,
    },
  );
  if (!res.ok) throw await asApiError(res, "candidate draft publish");
  return startAcknowledgedRun(
    (await res.json()) as GenerateAck,
    signal,
    onReserved,
  );
}

// ---------- /api/openresearch/projects ----------

export interface OpenResearchProjectRequest {
  artifact: Artifact;
  conversation_id?: string;
  org_id?: string;
  paper_id?: string;
  paper_url?: string;
  repo_full_name?: string;
  agent_prompt?: string;
}

export interface OpenResearchProjectAck {
  job_id: string;
  status: "running";
}

export interface OpenResearchProjectResult extends OpenResearchArtifactState {
  source_run_id: string;
  artifact_id: string;
  details?: Record<string, unknown>;
}

export async function startOpenResearchProject(
  req: OpenResearchProjectRequest,
): Promise<OpenResearchProjectAck> {
  const res = await fetch("/api/openresearch/projects", {
    method: "POST",
    headers: {
      ...keyHeaders(),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      artifact: req.artifact,
      artifact_id: req.artifact.artifact_id,
      source_run_id: runIdFromArtifactId(req.artifact.artifact_id),
      conversation_id: req.conversation_id,
      org_id: req.org_id,
      paper_id: req.paper_id,
      paper_url: req.paper_url,
      repo_full_name: req.repo_full_name,
      agent_prompt: req.agent_prompt,
    }),
  });
  if (!res.ok) {
    throw await asApiError(res, "/api/openresearch/projects");
  }
  return (await res.json()) as OpenResearchProjectAck;
}

export async function fetchOpenResearchProject(job_id: string): Promise<OpenResearchProjectResult> {
  const res = await fetch(`/api/openresearch/projects/${job_id}`, {
    headers: keyHeaders(),
  });
  if (!res.ok) {
    throw await asApiError(res, `/api/openresearch/projects/${job_id}`);
  }
  return (await res.json()) as OpenResearchProjectResult;
}

// ---------- /api/assets/upload ----------

export interface EditorAssetUploadResponse {
  url: string;
  filename: string;
  content_type: string;
  size: number;
}

export async function uploadEditorAsset(
  file: File,
): Promise<EditorAssetUploadResponse> {
  const fd = new FormData();
  fd.append("file", file, file.name || "image");
  const res = await fetch("/api/assets/upload", {
    method: "POST",
    body: fd,
    headers: keyHeaders(),
  });
  if (!res.ok) {
    throw await asApiError(res, "/api/assets/upload");
  }
  return (await res.json()) as EditorAssetUploadResponse;
}

export async function fetchArtifactAssets(artifact_id: string): Promise<ArtifactAsset[]> {
  const res = await fetch(`/api/artifacts/${encodeURIComponent(artifact_id)}/assets`, {
    headers: keyHeaders(),
  });
  if (!res.ok) {
    throw await asApiError(res, `/api/artifacts/${artifact_id}/assets`);
  }
  return (await res.json()) as ArtifactAsset[];
}

// ---------- /api/artifacts/export ----------

export type ArtifactExportFormat =
  | "pdf"
  | "pptx"
  | "original_html"
  | "standalone_html";

export interface ArtifactExportRequest {
  artifact: Artifact;
  format: ArtifactExportFormat;
}

export interface ArtifactPptxExportRequest {
  artifact: Artifact;
  conversation_id?: string;
}

export interface ArtifactExportResponse {
  url: string;
  filename: string;
  format: string;
  mime_type: string;
}

export async function exportArtifactRequest(
  req: ArtifactExportRequest,
): Promise<ArtifactExportResponse> {
  const res = await fetch("/api/artifacts/export", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...keyHeaders(),
    },
    body: JSON.stringify(req),
  });
  if (!res.ok) {
    throw await asApiError(res, "/api/artifacts/export");
  }
  return (await res.json()) as ArtifactExportResponse;
}

export async function startArtifactPptxExport(
  req: ArtifactPptxExportRequest,
  signal?: AbortSignal,
  onReserved?: (run_id: string) => void,
): Promise<GenerateAck> {
  const res = await fetch("/api/artifacts/export/pptx-run", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Autodesign-Reserve-Only": "true",
      ...keyHeaders(),
    },
    body: JSON.stringify(req),
    signal,
  });
  if (!res.ok) {
    throw await asApiError(res, "/api/artifacts/export/pptx-run");
  }
  return startAcknowledgedRun(
    (await res.json()) as GenerateAck,
    signal,
    onReserved,
  );
}

// ---------- Phase progress ----------
// Moved to `lib/progress.ts` in the rich-progress refactor.

// ---------- /api/edits/apply ----------

export interface ApplyEditsRequest {
  /** The source artifact's run_id (extracted from artifact_id "art_<id>"). */
  run_id: string;
  conversation_id?: string;
  artifact_type: ArtifactType;
  palette_id?: string;
  /** Either the legacy layer-id-keyed patch map, or the structured
   *  `{ layers, layout }` payload used by native HTML flow edits. */
  edits: PendingEditsPayload;
}

export async function applyEditsRequest(
  req: ApplyEditsRequest
): Promise<GenerateResponse> {
  const fd = new FormData();
  fd.append("run_id", req.run_id);
  if (req.conversation_id) fd.append("conversation_id", req.conversation_id);
  fd.append("artifact_type", req.artifact_type);
  if (req.palette_id) fd.append("palette_id", req.palette_id);
  fd.append("edits_json", JSON.stringify(req.edits));

  const res = await fetch("/api/edits/apply", {
    method: "POST",
    body: fd,
    headers: keyHeaders(),
  });
  if (!res.ok) {
    throw await asApiError(res, "/api/edits/apply");
  }
  return (await res.json()) as GenerateResponse;
}

// ---------- /api/video/render ----------

export interface RenderVideoRequest {
  artifact: Artifact;
  conversation_id?: string;
}

export async function renderVideoRequest(
  req: RenderVideoRequest,
  signal?: AbortSignal,
  onReserved?: (run_id: string) => void,
): Promise<GenerateAck> {
  const res = await fetch("/api/video/render", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Autodesign-Reserve-Only": "true",
      ...keyHeaders(),
    },
    body: JSON.stringify(req),
    signal,
  });
  if (!res.ok) {
    throw await asApiError(res, "/api/video/render");
  }
  return startAcknowledgedRun(
    (await res.json()) as GenerateAck,
    signal,
    onReserved,
  );
}

// ---------- /api/design-events ----------

export type DesignEventName =
  | "artifact.opened"
  | "artifact.downloaded"
  | "openresearch.project_requested"
  | "openresearch.project_ready"
  | "openresearch.project_failed";

export interface DesignEventRequest {
  conversation_id: string;
  event: DesignEventName;
  run_id?: string;
  artifact_id?: string;
  data?: Record<string, unknown>;
}

export async function sendDesignEvent(req: DesignEventRequest): Promise<void> {
  const res = await fetch("/api/design-events", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...keyHeaders(),
    },
    body: JSON.stringify(req),
  });
  if (!res.ok) {
    throw await asApiError(res, "/api/design-events");
  }
}

/** "art_20260504-114432-abcd1234" → "20260504-114432-abcd1234". */
export function runIdFromArtifactId(artifact_id: string): string {
  return artifact_id.startsWith("art_") ? artifact_id.slice(4) : artifact_id;
}

/** "msg_20260504-114432-abcd1234" → "20260504-114432-abcd1234". Used by
 *  the FailureCard's Retry CTA to find the source run on the backend. */
export function runIdFromMessageId(message_id: string): string {
  return message_id.startsWith("msg_") ? message_id.slice(4) : message_id;
}

const RUN_ID_RE = /^\d{8}-\d{6}-[a-f0-9]{8}$/i;

/** Prefer the explicit backend `run_id`, but recover old persisted
 *  FailureCard messages whose id was accidentally replaced with a
 *  frontend placeholder. */
export function runIdFromMessage(
  message: Pick<Message, "id" | "run_id" | "text">,
): string {
  if (message.run_id) return message.run_id;
  const fromId = runIdFromMessageId(message.id);
  if (RUN_ID_RE.test(fromId)) return fromId;
  const match = message.text.match(/out\/runs\/(\d{8}-\d{6}-[a-f0-9]{8})\//i);
  return match?.[1] ?? fromId;
}

// ---------- /api/runs/{run_id}/retry ----------

/** Continues a failed run from a validated checkpoint when available,
 *  otherwise starts a fresh retry from its original inputs. Returns a
 *  `GenerateAck` shaped like /api/generate; checkpoint resumes may keep
 *  the existing run id. */
export async function retryRunRequest(
  run_id: string,
  designer_override?: string,
  signal?: AbortSignal,
  onReserved?: (run_id: string) => void,
): Promise<GenerateAck> {
  const fd = new FormData();
  if (designer_override) fd.append("designer_override", designer_override);
  const res = await fetch(`/api/runs/${run_id}/retry`, {
    method: "POST",
    body: fd,
    headers: {
      ...keyHeaders(),
      "X-Autodesign-Reserve-Only": "true",
    },
    signal,
  });
  if (!res.ok) {
    throw await asApiError(res, `/api/runs/${run_id}/retry`);
  }
  return startAcknowledgedRun(
    (await res.json()) as GenerateAck,
    signal,
    onReserved,
  );
}

export async function retryVideoExportRequest(
  run_id: string,
  conversation_id: string,
  signal?: AbortSignal,
  onReserved?: (run_id: string) => void,
): Promise<GenerateAck> {
  const fd = new FormData();
  fd.append("conversation_id", conversation_id);
  const res = await fetch(`/api/runs/${run_id}/retry-video-export`, {
    method: "POST",
    body: fd,
    headers: {
      ...keyHeaders(),
      "X-Autodesign-Reserve-Only": "true",
    },
    signal,
  });
  if (!res.ok) {
    throw await asApiError(res, `/api/runs/${run_id}/retry-video-export`);
  }
  return startAcknowledgedRun(
    (await res.json()) as GenerateAck,
    signal,
    onReserved,
  );
}
