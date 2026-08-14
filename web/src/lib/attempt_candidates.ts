import type { AppMode, ArtifactType, Conversation } from "./types";

export type AttemptSafetyState = "ready" | "ready_with_warnings" | "blocked";
export type AttemptSelectionPhase =
  | "idle"
  | "requested"
  | "terminating"
  | "promoting"
  | "delivering"
  | "complete"
  | "failed";

export interface AttemptIssue {
  issue_id: string;
  message: string;
}

export interface AttemptCandidateSummary {
  candidate_id: string;
  run_id: string;
  artifact_type: ArtifactType;
  attempt: number;
  max_attempts: number;
  created_at: string;
  source_sha256: string;
  safety_state: AttemptSafetyState;
  hard_blockers: AttemptIssue[];
  warnings: AttemptIssue[];
  source_url: string;
  preview_urls: string[];
}

export interface AttemptSelectionStatus {
  candidate_id: string;
  source_attempt: number;
  state: Exclude<AttemptSelectionPhase, "idle">;
  artifact_id?: string | null;
  error_code?: string | null;
  error_message?: string | null;
}

export interface RunAttemptState {
  run_id: string;
  candidates: AttemptCandidateSummary[];
  active_attempt?: number;
  final_candidate_id?: string;
  selection_phase: AttemptSelectionPhase;
  selection?: AttemptSelectionStatus;
  loading: boolean;
  error?: string;
}

const ARTIFACT_TYPES = new Set<ArtifactType>([
  "poster",
  "deck",
  "landing",
  "video",
]);
const SAFETY_STATES = new Set<AttemptSafetyState>([
  "ready",
  "ready_with_warnings",
  "blocked",
]);
const SELECTION_PHASES = new Set<AttemptSelectionPhase>([
  "idle",
  "requested",
  "terminating",
  "promoting",
  "delivering",
  "complete",
  "failed",
]);

function issue(value: unknown): AttemptIssue | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  if (typeof raw.issue_id !== "string" || typeof raw.message !== "string") return null;
  return { issue_id: raw.issue_id, message: raw.message };
}

export function normalizeAttemptCandidate(value: unknown): AttemptCandidateSummary | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  if (
    typeof raw.candidate_id !== "string"
    || typeof raw.run_id !== "string"
    || !ARTIFACT_TYPES.has(raw.artifact_type as ArtifactType)
    || !Number.isInteger(raw.attempt)
    || !Number.isInteger(raw.max_attempts)
    || typeof raw.created_at !== "string"
    || typeof raw.source_sha256 !== "string"
    || raw.source_sha256.length !== 64
    || !SAFETY_STATES.has(raw.safety_state as AttemptSafetyState)
    || typeof raw.source_url !== "string"
    || !raw.source_url.startsWith("/api/files/runs/")
  ) return null;
  const previewUrls = Array.isArray(raw.preview_urls)
    ? raw.preview_urls.filter(
      (item): item is string => typeof item === "string" && item.startsWith("/api/files/runs/"),
    )
    : [];
  return {
    candidate_id: raw.candidate_id,
    run_id: raw.run_id,
    artifact_type: raw.artifact_type as ArtifactType,
    attempt: raw.attempt as number,
    max_attempts: raw.max_attempts as number,
    created_at: raw.created_at,
    source_sha256: raw.source_sha256,
    safety_state: raw.safety_state as AttemptSafetyState,
    hard_blockers: Array.isArray(raw.hard_blockers)
      ? raw.hard_blockers.map(issue).filter((item): item is AttemptIssue => item !== null)
      : [],
    warnings: Array.isArray(raw.warnings)
      ? raw.warnings.map(issue).filter((item): item is AttemptIssue => item !== null)
      : [],
    source_url: raw.source_url,
    preview_urls: previewUrls,
  };
}

export function normalizeRunAttemptState(value: unknown): RunAttemptState {
  if (!value || typeof value !== "object") {
    throw new Error("Invalid attempt response.");
  }
  const raw = value as Record<string, unknown>;
  if (typeof raw.run_id !== "string" || !raw.run_id) {
    throw new Error("Attempt response is missing run_id.");
  }
  const candidates = Array.isArray(raw.candidates)
    ? raw.candidates
      .map(normalizeAttemptCandidate)
      .filter((candidate): candidate is AttemptCandidateSummary => candidate !== null)
      .sort((a, b) => a.attempt - b.attempt)
    : [];
  const selectionRaw = raw.selection && typeof raw.selection === "object"
    ? raw.selection as Record<string, unknown>
    : null;
  const phase = selectionRaw && SELECTION_PHASES.has(selectionRaw.state as AttemptSelectionPhase)
    ? selectionRaw.state as AttemptSelectionPhase
    : "idle";
  const selection = selectionRaw && phase !== "idle"
    && typeof selectionRaw.candidate_id === "string"
    && Number.isInteger(selectionRaw.source_attempt)
    ? {
      candidate_id: selectionRaw.candidate_id,
      source_attempt: selectionRaw.source_attempt as number,
      state: phase as AttemptSelectionStatus["state"],
      artifact_id: typeof selectionRaw.artifact_id === "string" ? selectionRaw.artifact_id : null,
      error_code: typeof selectionRaw.error_code === "string" ? selectionRaw.error_code : null,
      error_message: typeof selectionRaw.error_message === "string" ? selectionRaw.error_message : null,
    }
    : undefined;
  return {
    run_id: raw.run_id,
    candidates,
    selection_phase: phase,
    selection,
    final_candidate_id: selection?.state === "complete" ? selection.candidate_id : undefined,
    loading: false,
  };
}

function runIdFromArtifactId(value?: string | null): string | undefined {
  return value?.startsWith("art_") ? value.slice(4) || undefined : undefined;
}

export function attemptRunIdForConversation(
  conversation: Conversation,
): string | undefined {
  const active = conversation.active_artifact_id
    ? conversation.artifacts[conversation.active_artifact_id]
    : undefined;
  const draftSource = active?.attempt_lineage?.source_run_id;
  if (draftSource) return draftSource;
  const published = runIdFromArtifactId(conversation.published_artifact_id);
  if (published) return published;
  const completed = [...conversation.messages].reverse().find(
    (message) => message.status === "done" && message.run_id,
  )?.run_id;
  return completed || conversation.run_id;
}

export function candidateAction(
  candidate: AttemptCandidateSummary,
  selectionPhase: AttemptSelectionPhase,
  finalized: boolean,
  selectedCandidateId?: string,
): "select" | "fix" | "open" | "current" | "disabled" {
  if (
    selectionPhase === "complete"
    && candidate.candidate_id === selectedCandidateId
  ) return "current";
  if (selectionPhase === "complete") {
    return candidate.safety_state === "blocked" ? "fix" : "open";
  }
  if (selectionPhase !== "idle" && selectionPhase !== "failed") return "disabled";
  if (candidate.safety_state === "blocked") return "fix";
  return finalized ? "open" : "select";
}

export function isAttemptDraftEditing(
  mode: AppMode,
  candidateId: string,
  activeDraftCandidateId?: string,
): boolean {
  return mode === "canvas" && candidateId === activeDraftCandidateId;
}
