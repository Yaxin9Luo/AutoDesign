import assert from "node:assert/strict";
import test from "node:test";

import {
  PAPER_BUNDLE_ARTIFACT_ORDER,
  createPaperBundleChildState,
  createPaperBundleParentState,
  createInitialPaperBundleTasks,
  createPaperBundleRequestSpecs,
  derivePaperBundleStatus,
  paperBundleChildConversationId,
  resolvedCompletedTaskError,
} from "../src/lib/paper_bundle.ts";
import {
  DENSE_PAPER_POSTER_PROMPT,
  PAPER_BUNDLE_PROMPTS_V1,
  PAPER_BUNDLE_PROMPT_VERSION,
  VIDEO_ARTIFACT_DESCRIPTION,
  VIDEO_SCENE_DURATION_MAX_S,
} from "../src/lib/presets.ts";
import { translate } from "../src/lib/i18n.ts";
import type {
  Attachment,
  PaperBundleTaskStatus,
} from "../src/lib/types.ts";

const DECK_PROMPT = "Create a polished 16:9 academic conference slide deck in standalone HTML. Build a coherent research talk narrative from motivation through method, evidence, analysis, limitations, and takeaways; make substantive slides information-rich and use original paper figures, native tables, equations, and editable diagrams as visual evidence.";
const LANDING_PROMPT = "Create a polished interactive academic paper landing page in standalone HTML. Make the paper identity, method, evidence, and results immediately understandable; use many eligible original paper figures with local interpretations, restrained inline SVG icons, meaningful source-grounded interactions, responsive layout, and subtle motion with reduced-motion support.";
const VIDEO_PROMPT = "Create a rigorous 5–10 minute academic conference video, choosing the duration to match the paper's complexity, with English narration, English subtitles, and extensive use of original paper visuals.";

test("uses a stable artifact order and four unique child conversation IDs", () => {
  assert.deepEqual(PAPER_BUNDLE_ARTIFACT_ORDER, [
    "poster",
    "deck",
    "landing",
    "video",
  ]);

  const childIds = PAPER_BUNDLE_ARTIFACT_ORDER.map((artifactType) =>
    paperBundleChildConversationId("conv_parent", artifactType),
  );

  assert.equal(new Set(childIds).size, 4);
  assert.deepEqual(
    createInitialPaperBundleTasks("conv_parent"),
    Object.fromEntries(PAPER_BUNDLE_ARTIFACT_ORDER.map((artifactType, index) => [
      artifactType,
      {
        artifact_type: artifactType,
        child_conversation_id: childIds[index],
        status: "pending",
      },
    ])),
  );
});

test("uses one discriminated parent or child state without stored aggregate status", () => {
  const parent = createPaperBundleParentState(
    "conv_parent",
    "paper.pdf",
  );

  assert.equal(parent.kind, "parent");
  assert.equal(parent.prompt_version, 1);
  assert.equal(parent.source_name, "paper.pdf");
  assert.deepEqual(
    parent.tasks,
    createInitialPaperBundleTasks("conv_parent"),
  );
  assert.equal("status" in parent, false);

  const child = createPaperBundleChildState("conv_parent", "deck");
  assert.deepEqual(child, {
    kind: "child",
    parent_conversation_id: "conv_parent",
    artifact_type: "deck",
  });
  assert.equal("tasks" in child, false);
});

test("pins version one to the exact four paper prompts", () => {
  assert.equal(PAPER_BUNDLE_PROMPT_VERSION, 1);
  assert.equal(PAPER_BUNDLE_PROMPTS_V1.poster, DENSE_PAPER_POSTER_PROMPT);
  assert.equal(PAPER_BUNDLE_PROMPTS_V1.deck, DECK_PROMPT);
  assert.equal(PAPER_BUNDLE_PROMPTS_V1.landing, LANDING_PROMPT);
  assert.equal(PAPER_BUNDLE_PROMPTS_V1.video, VIDEO_PROMPT);
});

test("describes adaptive video length in every supported East Asian locale", () => {
  assert.equal(VIDEO_SCENE_DURATION_MAX_S, 600);
  assert.equal(VIDEO_ARTIFACT_DESCRIPTION, "MP4 · 5–10 min · narrated + subtitles");
  assert.equal(
    translate("zh", VIDEO_ARTIFACT_DESCRIPTION),
    "MP4 · 5–10 分钟 · 旁白与字幕",
  );
  assert.equal(
    translate("ko", VIDEO_ARTIFACT_DESCRIPTION),
    "MP4 · 5–10분 · 내레이션 + 자막",
  );
});

test("builds requests with shared attachments and poster-only options", () => {
  const paper: Attachment = {
    id: "paper",
    name: "paper.pdf",
    size: 100,
    kind: "pdf",
    role: "content",
  };
  const attachments = [paper];
  const requests = createPaperBundleRequestSpecs(
    "conv_parent",
    attachments,
    "academic_blue",
  );

  assert.deepEqual(
    requests.map((request) => request.artifact_type),
    PAPER_BUNDLE_ARTIFACT_ORDER,
  );
  for (const request of requests) {
    assert.equal(request.attachments, attachments);
    assert.equal(request.attachments[0], paper);
    assert.equal(request.brief, PAPER_BUNDLE_PROMPTS_V1[request.artifact_type]);
    assert.equal(
      request.conversation_id,
      paperBundleChildConversationId("conv_parent", request.artifact_type),
    );
  }

  const poster = requests[0];
  assert.equal(poster.canvas_preset_id, "auto");
  assert.equal("template" in poster, false);
  assert.equal(poster.palette_id, "academic_blue");
  for (const request of requests.slice(1)) {
    assert.equal("template" in request, false);
    assert.equal("palette_id" in request, false);
  }
});

test("Paper All-in-One preserves Auto or an explicit Poster canvas selection only on its Poster child", () => {
  const paper: Attachment = {
    id: "paper",
    name: "paper.pdf",
    size: 100,
    kind: "pdf",
    role: "content",
  };
  const auto = createPaperBundleRequestSpecs(
    "conv_auto",
    [paper],
    "academic_blue",
    "auto",
    undefined,
  );
  assert.equal(auto[0].canvas_preset_id, "auto");
  assert.equal("template" in auto[0], false);

  const explicit = createPaperBundleRequestSpecs(
    "conv_4x3",
    [paper],
    "academic_blue",
    "poster-classic-4x3",
    "poster-classic-4x3",
  );
  assert.equal(explicit[0].canvas_preset_id, "poster-classic-4x3");
  assert.equal(explicit[0].template, "poster-classic-4x3");

  for (const child of [...auto.slice(1), ...explicit.slice(1)]) {
    assert.equal("canvas_preset_id" in child, false);
    assert.equal("template" in child, false);
  }
});

function tasksWithStatuses(statuses: PaperBundleTaskStatus[]) {
  const tasks = createInitialPaperBundleTasks("conv_parent");
  PAPER_BUNDLE_ARTIFACT_ORDER.forEach((artifactType, index) => {
    tasks[artifactType] = { ...tasks[artifactType], status: statuses[index] };
  });
  return tasks;
}

test("derives every overall bundle status", () => {
  assert.equal(
    derivePaperBundleStatus(tasksWithStatuses(["complete", "running", "pending", "failed"])),
    "running",
  );
  assert.equal(
    derivePaperBundleStatus(tasksWithStatuses(["complete", "complete", "complete", "complete"])),
    "complete",
  );
  assert.equal(
    derivePaperBundleStatus(tasksWithStatuses(["complete", "failed", "cancelled", "failed"])),
    "partial",
  );
  assert.equal(
    derivePaperBundleStatus(tasksWithStatuses(["failed", "failed", "cancelled", "failed"])),
    "failed",
  );
  assert.equal(
    derivePaperBundleStatus(tasksWithStatuses(["cancelled", "cancelled", "cancelled", "cancelled"])),
    "cancelled",
  );
  assert.equal(
    derivePaperBundleStatus(tasksWithStatuses(["complete", "uploading", "pending", "failed"])),
    "running",
  );
  assert.equal(
    derivePaperBundleStatus(tasksWithStatuses(["complete", "cancelling", "cancelling", "failed"])),
    "cancelling",
  );
});

test("clears a stale same-run error when the recovered artifact is a clean success", () => {
  assert.equal(
    resolvedCompletedTaskError(false, undefined, "stale soft-accept diagnostic"),
    undefined,
  );
  assert.equal(
    resolvedCompletedTaskError(true, "current quality warning", "stale warning"),
    undefined,
  );
});
