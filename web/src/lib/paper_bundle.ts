import {
  PAPER_BUNDLE_PROMPTS_V1,
  PAPER_BUNDLE_PROMPT_VERSION,
  PAPER_POSTER_TEMPLATE,
} from "./presets.ts";
import type {
  ArtifactType,
  Attachment,
  PaperBundleChildState,
  PaperBundleParentState,
  PaperBundleStatus,
  PaperBundleTask,
  PaperBundleTaskMap,
} from "./types.ts";

export const PAPER_BUNDLE_ARTIFACT_ORDER = Object.freeze([
  "poster",
  "deck",
  "landing",
  "video",
] as const satisfies readonly ArtifactType[]);

export interface PaperBundleRequestSpec {
  brief: string;
  attachments: Attachment[];
  conversation_id: string;
  artifact_type: ArtifactType;
  palette_id?: string;
  template?: string;
}

export function paperBundleChildConversationId(
  parentConversationId: string,
  artifactType: ArtifactType,
): string {
  return `${parentConversationId}:paper-bundle:${artifactType}`;
}

export function createInitialPaperBundleTasks(
  parentConversationId: string,
  startedAt?: number,
): PaperBundleTaskMap {
  const task = <TArtifactType extends ArtifactType>(
    artifactType: TArtifactType,
  ): PaperBundleTask<TArtifactType> => ({
    artifact_type: artifactType,
    child_conversation_id: paperBundleChildConversationId(
      parentConversationId,
      artifactType,
    ),
    status: "pending",
    ...(typeof startedAt === "number" ? { started_at: startedAt } : {}),
  });

  return {
    poster: task("poster"),
    deck: task("deck"),
    landing: task("landing"),
    video: task("video"),
  };
}

export function createPaperBundleParentState(
  parentConversationId: string,
  sourceName: string,
  startedAt?: number,
): PaperBundleParentState {
  return {
    kind: "parent",
    prompt_version: PAPER_BUNDLE_PROMPT_VERSION,
    source_name: sourceName,
    tasks: createInitialPaperBundleTasks(parentConversationId, startedAt),
  };
}

export function createPaperBundleChildState(
  parentConversationId: string,
  artifactType: ArtifactType,
): PaperBundleChildState {
  return {
    kind: "child",
    parent_conversation_id: parentConversationId,
    artifact_type: artifactType,
  };
}

export function derivePaperBundleStatus(
  tasks: PaperBundleTaskMap,
): PaperBundleStatus {
  const statuses = PAPER_BUNDLE_ARTIFACT_ORDER.map(
    (artifactType) => tasks[artifactType].status,
  );

  if (statuses.every((status) => status === "complete")) return "complete";
  if (statuses.some((status) => (
    status === "pending" || status === "uploading" || status === "running"
  ))) {
    return "running";
  }
  if (statuses.some((status) => status === "cancelling")) return "cancelling";
  if (statuses.some((status) => status === "complete")) return "partial";
  if (statuses.every((status) => status === "cancelled")) return "cancelled";
  return "failed";
}

export function paperBundleBlocksPptxExport(
  bundle: PaperBundleParentState,
): boolean {
  if (
    bundle.backend_state === "reserved"
    || bundle.backend_state === "running"
    || bundle.backend_state === "cancelling"
  ) {
    return true;
  }
  const status = derivePaperBundleStatus(bundle.tasks);
  return status === "running" || status === "cancelling";
}

export function paperBundleBlocksAttemptActions(
  bundle: PaperBundleParentState,
  runId: string,
): boolean {
  const matchingTask = Object.values(bundle.tasks).find((task) => (
    task.run_id === runId || task.authoring_run_id === runId
  ));
  if (!matchingTask) return false;
  if (
    bundle.backend_state === "cancelled"
    || bundle.backend_state === "completed"
    || bundle.backend_state === "partial"
    || bundle.backend_state === "failed"
  ) {
    return false;
  }
  return bundle.backend_state === "cancelling"
    || matchingTask.status === "cancelling";
}

export function resolvedCompletedTaskError(
  _hasFailure: boolean,
  _incomingError?: string,
  _previousSameRunError?: string,
): string | undefined {
  return undefined;
}

export function createPaperBundleRequestSpecs(
  parentConversationId: string,
  attachments: Attachment[],
  posterPaletteId?: string | null,
): PaperBundleRequestSpec[] {
  return PAPER_BUNDLE_ARTIFACT_ORDER.map((artifactType) => ({
    brief: PAPER_BUNDLE_PROMPTS_V1[artifactType],
    attachments,
    conversation_id: paperBundleChildConversationId(
      parentConversationId,
      artifactType,
    ),
    artifact_type: artifactType,
    ...(artifactType === "poster"
      ? {
          template: PAPER_POSTER_TEMPLATE,
          ...(posterPaletteId ? { palette_id: posterPaletteId } : {}),
        }
      : {}),
  }));
}
