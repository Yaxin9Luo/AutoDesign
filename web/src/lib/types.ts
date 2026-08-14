/**
 * Frontend types — kept intentionally close to `autodesign/schema.py`
 * (LayerNode / DesignSpec / SafeZone) so the future API integration is
 * a JSON-pass-through, not a translation layer.
 *
 * Subset chosen for the v1 canvas editor; fields the renderer ignores
 * are simply absent here, not renamed.
 */

export type ArtifactType = "poster" | "deck" | "landing" | "video";

export type LayerKind =
  | "background"
  | "text"
  | "image"
  | "shape"
  | "section"; // landing only — flow layout, no bbox

export type ShapeKind = "rect" | "ellipse" | "line" | "arrow";

export type Align = "left" | "center" | "right";

export interface Bbox {
  /** top-left origin, pixel units (matches schema.py SafeZone) */
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface TextEffect {
  fill?: string;
  stroke?: { color: string; width: number };
  shadow?: { color: string; dx: number; dy: number; blur: number };
}

export interface LayerShadow {
  color: string;
  dx: number;
  dy: number;
  blur: number;
  opacity: number;
}

export interface Layer {
  layer_id: string;
  name: string;
  kind: LayerKind;
  z_index: number;
  bbox?: Bbox;
  visible?: boolean;
  locked?: boolean;
  group_id?: string;
  slot_id?: string;
  panel_role?: string;
  layout_archetype?: string;

  // text
  text?: string;
  font_family?: string;
  font_size_px?: number;
  font_weight?: number; // 300..900
  font_style?: "normal" | "italic";
  line_height?: number; // unitless, e.g. 1.2
  letter_spacing?: number; // px
  align?: Align;
  text_transform?: "none" | "uppercase";
  list_style?: "none" | "bullet";
  effects?: TextEffect;

  // image
  src?: string;
  fit?: "cover" | "contain" | "fill";
  object_position?: { x: number; y: number };
  flow_offset?: { dx: number; dy: number };
  corner_radius?: number;
  opacity?: number; // 0..1

  // background / shape
  fill_color?: string;
  shape_kind?: ShapeKind;
  stroke_color?: string;
  stroke_width?: number;
  stroke_dash?: "solid" | "dashed" | "dotted";
  shadow?: LayerShadow;
}

export interface Canvas {
  w: number;
  h: number;
  background?: string;
}

export interface LayerGroup {
  group_id: string;
  name: string;
}

export interface VideoScene {
  scene_id: string;
  name: string;
  frame_layer_id: string;
  duration_s: number;
  transition: "cut" | "fade" | "wipe";
}

export interface VideoRenderInfo {
  run_id: string;
  mp4_url?: string;
  subtitle_url?: string;
  rendered_at: number;
  error?: string;
}

export interface VideoProject {
  duration_s: number;
  fps: number;
  scenes: VideoScene[];
  latest_render?: VideoRenderInfo;
}

export type NativeFormat = "svg" | "html" | "pptx" | "mp4" | "png";
export type ViewFormat = "svg" | "html" | "mp4" | "png";

export interface OpenResearchArtifactState {
  status: "running" | "submitted" | "error";
  job_id: string;
  result_url?: string | null;
  api_log_url?: string | null;
  project_id?: string | null;
  project_url?: string | null;
  org_id?: string | null;
  paper_id?: string | null;
  repo_full_name?: string | null;
  gui_submitter_status?: string | null;
  gui_submitter_reason?: string | null;
  gui_submitter_error?: string | null;
  gui_submitter_session_url?: string | null;
  agent_prompt_url?: string | null;
  submitter_log_url?: string | null;
  latest_report_id?: string | null;
  latest_report_url?: string | null;
  error?: string | null;
}

export interface Artifact {
  artifact_id: string;
  name: string;
  artifact_type: ArtifactType;
  canvas: Canvas;
  canvas_plan?: Record<string, unknown>;
  deck_plan?: Record<string, unknown>;
  layers: Layer[];
  layer_groups?: LayerGroup[];
  video_project?: VideoProject;
  /** lineage — tracks edits like the Python `parent_run_id` chain */
  parent_artifact_id?: string;
  /** Path 1 — direct render of the agent's actual file. When set, the
   *  canvas embeds this URL directly (SVG inline, HTML iframe, PPTX
   *  download) instead of iterating over `layers`. */
  native_file_url?: string;
  native_format?: NativeFormat;
  view_file_url?: string;
  view_format?: ViewFormat;
  download_url?: string;
  pdf_url?: string;
  downloads?: Record<string, string>;
  /** PNG render of the artifact (vision-critic preview). Used as the
   *  thumbnail in chat cards; falls back to native_file_url if absent. */
  preview_url?: string;
  /** Viewport-sized thumbnail for compact cards. The full-page QA preview
   *  remains available through preview_url. */
  card_preview_url?: string;
  /** Delivery quality is independent from publishability. A degraded final is
   *  still openable/downloadable, but carries refinement diagnostics. */
  quality_status?: "ready" | "ready_with_warnings";
  quality_diagnostics?: string[];
  openresearch?: OpenResearchArtifactState | null;
  candidate_draft?: boolean;
  attempt_lineage?: {
    materialization_version?: number;
    source_run_id: string;
    source_attempt: number;
    source_candidate_id: string;
    source_candidate_sha256: string;
    published_artifact_id_at_fork?: string | null;
    poster_palette_id?: string;
    status?: "draft" | "published";
    edited_at?: string;
    published_version_id?: string;
  };
}

export interface ArtifactAsset {
  asset_id: string;
  name: string;
  kind: "figure" | "table" | "image";
  url: string;
  filename: string;
  run_id: string;
  source: string;
  size: number;
}

export type HtmlLayoutPatch =
  | {
      kind: "section_height";
      section_id: string;
      height_px: number;
    }
  | {
      kind: "section_size";
      section_id: string;
      width_px?: number;
      height_px?: number;
      offset_x_px?: number;
      offset_y_px?: number;
    }
  | {
      kind: "poster_style";
      scope: "global" | "section";
      section_id?: string;
      styles: {
        accent?: string;
        accent2?: string;
        background?: string;
        ink?: string;
      };
    }
  | {
      kind: "section_order";
      columns: Array<{ column_id: string; section_ids: string[] }>;
    }
  | {
      kind: "column_widths";
      columns_id: string;
      widths: number[];
    }
  | {
      kind: "dom_delete";
      target_id?: string;
      target_kind?: "text" | "image" | "section" | "layer";
      block_id?: string;
      selector?: string;
      label?: string;
    };

export interface PendingArtifactEdits {
  layers?: Record<string, Partial<Layer>>;
  layout?: HtmlLayoutPatch[];
}

export type PendingEditsPayload = PendingArtifactEdits | Record<string, Partial<Layer>>;

export type PosterAreaSelectionKind = "element" | "region" | "drawing";

export interface PosterDrawingPath {
  points: Array<{ x: number; y: number }>;
  color?: string;
  width_px?: number;
}

export interface PosterAreaSelectionItem {
  selection_id: string;
  kind: PosterAreaSelectionKind;
  label: string;
  instruction?: string;
  rect: Bbox;
  selector?: string;
  block_id?: string;
  text_excerpt?: string;
  html_excerpt?: string;
  nearby_headings?: string[];
  drawing_paths?: PosterDrawingPath[];
}

export interface PosterSelectionContext {
  kind: PosterAreaSelectionKind | "multi";
  rect: Bbox;
  instruction?: string;
  selector?: string;
  block_id?: string;
  text_excerpt?: string;
  html_excerpt?: string;
  nearby_headings?: string[];
  drawing_paths?: PosterDrawingPath[];
  items?: PosterAreaSelectionItem[];
}

export interface PosterSelectionSummary {
  kind: "area_selection";
  count: number;
  labels: string[];
  item_kinds: PosterAreaSelectionKind[];
  area_instructions?: Array<{ index: number; label: string; instruction: string }>;
}

// ---------- Chat ----------

export type Role = "user" | "assistant";

export interface Attachment {
  id: string;
  name: string;
  size: number;
  kind: "pdf" | "image" | "doc" | "other";
  /** Style references are uploaded through a separate backend field and are
   * never ingested as paper content/evidence. */
  role?: "content" | "style_reference";
  /** Opaque backend handle used to retry the exact historical style reference. */
  reference_handle?: string;
  /** The actual File handle — kept on the client only, used to build
   *  multipart uploads. Not serialized into chat history (would be huge
   *  and stale by re-render). */
  file?: File;
}

/** Structured failure metadata, mirrored from `Failure` in
 *  scripts/web_server.py. Set on assistant messages whose run did not
 *  produce an artifact (max_turns / cancelled / runtime error). The
 *  FailureCard component renders from this — `text` stays for clients
 *  that don't know about the structured shape. */
export interface MessageFailure {
  status: string; // "max_turns" | "cancelled" | "error" | terminal_status name
  /** Frontend recovery diagnostics for failures tied to a durable run. */
  run_id?: string;
  phase?: string; // "ingest" | "planning" | "authoring" | "rendering"
  error_code?: string;
  error_message?: string;
  error_detail?: string;
  resume_available?: boolean;
  resume_from_attempt?: number;
  next_attempt?: number;
  retry_route?: "full_authoring" | "export_only" | "setup_required" | "none";
  parent_run_id?: string;
  agent_last_note?: string;
  pointer_cleanup_warnings?: string[];
  produced_files: string[];
  /** Frontend-only recovery hint for synthetic failures such as an SSE
   *  disconnect after a backend restart. Lets Resume re-run the same
   *  brief with the original artifact type. */
  artifact_type?: ArtifactType;
  /** Backend's hint for a Retry CTA — when set, a one-click "Retry with
   *  X" makes sense; when null, fall back to a generic Retry. */
  suggested_designer?: string;
  /** Deprecated compatibility alias for `suggested_designer`. */
  suggested_planner?: string;
  elapsed_ms?: number;
  /** Vision critic's verdict and score for degraded runs. Populated
   *  when the agent self-graded the artifact as sub-pass (fail / revise)
   *  but still emitted rendered files — drives the quality-warning
   *  banner above the artifact card. */
  critic_verdict?: string;
  critic_score?: number;
}

export type RecoverableTaskType =
  | "generate"
  | "poster_code_edit"
  | "artifact_export_pptx"
  | "candidate_publish";

export interface PosterPalette {
  id: string;
  name: string;
  roles: {
    background: string;
    text: string;
    primary: string;
    secondary: string;
    accent: string;
    header_text: string;
    bar: string;
  };
}

export interface PosterPaletteCatalog {
  version: number;
  kind: "academic_poster_color_palettes";
  palettes: PosterPalette[];
}

export interface MessageTaskPayload {
  artifact_type?: ArtifactType;
  palette_id?: string;
  template?: string;
  authoring_max_attempts?: number;
  baseline_artifact_id?: string;
  source_artifact_id?: string;
  source_run_id?: string;
  source_candidate_id?: string;
  selection_context?: PosterSelectionContext | null;
  export_format?: "pptx";
  attachment_refs?: Array<{
    name: string;
    size: number;
    kind: Attachment["kind"];
    role?: Attachment["role"];
  }>;
  reference_poster_ref?: {
    name: string;
    size: number;
    kind: Attachment["kind"];
    role?: Attachment["role"];
    reference_handle?: string;
  };
}

export interface Message {
  id: string;
  role: Role;
  text: string;
  ts: number;
  /** Backend run that produced this assistant message. Prefer this over
   *  parsing `id`; older messages may not have it. */
  run_id?: string;
  attachments?: Attachment[];
  artifact_id?: string; // assistant message that produced an artifact
  status?: "streaming" | "done" | "error";
  failure?: MessageFailure;
  selection_summary?: PosterSelectionSummary;
  download_url?: string;
  download_filename?: string;
  download_mime_type?: string;
  task_type?: RecoverableTaskType;
  task_payload?: MessageTaskPayload;
  source_artifact_id?: string;
}

// ---------- Conversation ----------

export type PaperBundleTaskStatus =
  | "pending"
  | "uploading"
  | "running"
  | "cancelling"
  | "complete"
  | "failed"
  | "cancelled";

export type PaperBundleStatus =
  | "running"
  | "cancelling"
  | "complete"
  | "partial"
  | "failed"
  | "cancelled";

export type PaperBundleBackendState =
  | "reserved"
  | "running"
  | "cancelling"
  | "cancelled"
  | "completed"
  | "partial"
  | "failed";

export interface PaperBundleTask<TArtifactType extends ArtifactType = ArtifactType> {
  artifact_type: TArtifactType;
  child_conversation_id: string;
  status: PaperBundleTaskStatus;
  run_id?: string;
  authoring_run_id?: string;
  artifact_id?: string;
  error?: string;
  started_at?: number;
  finished_at?: number;
  attempts?: number;
  max_attempts?: number;
  terminal?: boolean;
  process_free?: boolean;
}

export type PaperBundleTaskMap = {
  [TArtifactType in ArtifactType]: PaperBundleTask<TArtifactType>;
};

export interface PaperBundleParentState {
  kind: "parent";
  prompt_version: 1;
  source_name: string;
  tasks: PaperBundleTaskMap;
  job_id?: string;
  revision?: number;
  backend_state?: PaperBundleBackendState;
  cancel_error?: string;
  cancel_request_in_flight?: boolean;
}

export interface PaperBundleChildState {
  kind: "child";
  parent_conversation_id: string;
  artifact_type: ArtifactType;
}

export type PaperBundleState = PaperBundleParentState | PaperBundleChildState;

export interface HistoryLastRun {
  run_id: string;
  status?: Message["status"];
  artifact_id?: string;
}

/** A single chat thread. Users can have multiple, switched from the
 *  history rail. Each conversation owns its own run lifecycle so two
 *  conversations can be generating in parallel — clicking around the
 *  history sidebar never interrupts a run, and each tab keeps its own
 *  SSE stream alive in the store. */
export interface Conversation {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
  messages: Message[];
  artifacts: Record<string, Artifact>;
  active_artifact_id: string | null;
  /** Last published artifact remains stable while an attempt fork is edited. */
  published_artifact_id?: string | null;
  poster_palette_id?: string | null;
  paper_bundle?: PaperBundleState;
  /** Per-artifact uncommitted edit buffer. Keyed by artifact_id, then
   *  layer_id. Cleared after a successful Apply or when the user
   *  switches to a different artifact. Patches accumulate across UI
   *  changes so the user can tweak multiple layers before committing. */
  pending_edits?: Record<string, PendingEditsPayload>;
  /** True while a generate is in flight in this conversation. The chat
   *  composer's Send button stays disabled only for this conversation
   *  (other conversations can still send in parallel). */
  pending?: boolean;
  /** The currently-running run_id, if any. Saved alongside the
   *  conversation so a Cancel button knows what to cancel and the
   *  ProgressCard can read state for *this* conversation specifically.
   *  See `RunProgress` for the full shape; defined in lib/progress.ts. */
  run_id?: string;
  /** Client-only marker for a compact server history row that needs a
   *  full conversation fetch before it can be edited or rendered. */
  history_summary?: boolean;
  /** Server conversation id used to hydrate a compact row remapped into a
   *  Paper All-in-One child conversation. */
  history_source_id?: string;
  /** Message count retained for a compact server history row. */
  history_message_count?: number;
  /** Last run metadata retained for compact history reconciliation. */
  history_last_run?: HistoryLastRun;
}

// ---------- App state ----------

export type AppMode = "chat" | "canvas";

/** Snapshot of which models the backend has wired for each agent
 *  role. Populated by GET /api/health. Each role can run a different
 *  model — that's the whole point of the per-agent override panel. */
export interface BackendInfo {
  /** Top-level kept for legacy callers; same value as `models.designer`. */
  designer_model: string;
  /** Same as `models.image`. */
  image_model: string;
  /** Per-agent map. Keys mirror autodesign's role names. New agents
   *  added on the backend will appear here automatically — the UI
   *  iterates this dict rather than hard-coding a list. */
  models: Record<string, string>;
  demo_mode?: boolean;
  public_user_isolation?: boolean;
  user_isolation?: boolean;
  demo?: {
    artifact_type: "poster";
    template: string;
    daily_limit: number;
    concurrency: number;
    queue_max: number;
    run_ttl_hours: number;
    max_pdf_bytes: number;
    settings_locked: boolean;
    openresearch_enabled: boolean;
    requires_low_privilege_user: boolean;
  };
  backend_profile?: {
    paper_poster?: BackendPaperPosterProfile;
    code_editor?: BackendCodeEditorProfile;
    harness_capabilities?: Record<string, BackendHarnessCapability>;
    openresearch?: BackendOpenResearchProfile;
    environment?: BackendEnvironmentProfile;
  };
}

export interface BackendEnvironmentProfile {
  video: {
    ready: boolean;
    missing: string[];
    repair: string;
    node: {
      available: boolean;
      compatible: boolean;
      binary: string;
      version: string;
    };
    hyperframes: {
      available: boolean;
      binary: string;
      version: string;
      source: string;
    };
    ffmpeg: { available: boolean; binary: string };
    ffprobe: { available: boolean; binary: string };
  };
  coding_agent: {
    harness: string;
    ready: boolean;
    binary: string;
    binary_source: string;
    version: string;
    capabilities: Record<string, boolean>;
    missing: string[];
    rejected_candidates: Array<{
      binary: string;
      source: string;
      version: string;
      missing: string[];
    }>;
  };
}

export interface BackendPaperPosterProfile {
  template: string;
  designer_author: string;
  designer_author_harness: string;
  designer_author_model?: string | null;
  designer_author_cmd: string;
  designer_author_cmd_available?: boolean;
  designer_author_cmd_source?: string;
  designer_author_cmd_message?: string;
  designer_author_timeout_s: number;
  designer_author_max_attempts: number;
}

export interface BackendCodeEditorProfile {
  available?: boolean;
  harness: string;
  model?: string | null;
  cmd: string;
  cmd_source?: string;
  command_detected?: boolean;
  auth_status?: "not_verified" | "verified" | "unavailable" | string;
  auth_message?: string;
  message?: string;
  timeout_s: number;
  max_attempts: number;
}

export interface BackendHarnessCapability {
  id: string;
  binary: string;
  binary_source: string;
  available: boolean;
  model_selection_mode: string;
  supports_hard_model_arg: boolean;
  notes: string;
  surfaces: Record<string, { model: string; cmd: string }>;
}

export interface BackendOpenResearchProfile {
  submitter: string;
  submitter_cmd: string;
  submitter_cmd_available: boolean;
  submitter_cmd_source: string;
  submitter_cmd_message: string;
  submitter_timeout_s: number;
  org_id_configured: boolean;
  repo_configured: boolean;
  api_url: string;
  api_token_configured: boolean;
}

export interface HarnessMatrixRow {
  matrix_id: string;
  paper_id?: string;
  paper_path?: string;
  template?: string;
  harness: string;
  requested_model?: string;
  effective_model_note?: string;
  model_selection_mode?: string;
  attempt_budget?: number;
  timeout_s?: number;
  status: string;
  outcome_class?: string;
  fallback_type?: string;
  fallback_manifest?: string;
  quality_status?: string;
  source_reason?: string;
  terminal_status?: string;
  primary_blocker?: string;
  run_id?: string;
  run_dir?: string;
  attempts_seen?: number;
  wall_seconds?: number | null;
  returncode?: number | null;
  process_id?: number | null;
  process_group_id?: number | null;
  final_html?: string;
  preview_png?: string;
  final_html_url?: string;
  preview_url?: string;
  report_path?: string;
  last_process_reason?: string;
  hard_issue_ids?: string[];
  stdout_tail?: string;
  stderr_tail?: string;
}

export interface HarnessMatrix {
  matrix_id: string;
  created_at?: string;
  updated_at?: string;
  status: string;
  paper_id?: string;
  paper_path?: string;
  prompt_chars?: number;
  template?: string;
  attempts?: number;
  timeout_s?: number;
  concurrency?: string;
  reuse_ingest_run?: string;
  matrix_dir?: string;
  report_path?: string;
  strict_success?: boolean;
  hard_failure_count?: number;
  summary?: {
    total_cells?: number;
    terminal_cells?: number;
    strict_success_count?: number;
    usable_count?: number;
    hard_failure_count?: number;
    outcome_counts?: Record<string, number>;
  };
  rows: HarnessMatrixRow[];
  error?: string;
}

export interface AppState {
  mode: AppMode;
  conversations: Record<string, Conversation>;
  current_conversation_id: string;
  selected_layer_id: string | null;
  selected_layer_ids: string[];
  /** Pre-selected artifact type from a quick-action card; cleared after first send. */
  intent_type: ArtifactType | null;
  /** Null until the first successful /api/health response. */
  backend_info: BackendInfo | null;
}
