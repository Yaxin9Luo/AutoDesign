import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

class MemoryStorage implements Storage {
  private readonly values = new Map<string, string>();
  get length() { return this.values.size; }
  clear() { this.values.clear(); }
  getItem(key: string) { return this.values.get(key) ?? null; }
  key(index: number) { return [...this.values.keys()][index] ?? null; }
  removeItem(key: string) { this.values.delete(key); }
  setItem(key: string, value: string) { this.values.set(key, value); }
}

Object.assign(globalThis, {
  window: globalThis,
  localStorage: new MemoryStorage(),
  document: { cookie: "" },
});

const vite = await createServer({
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true },
  appType: "custom",
});
const {
  candidatePublicationIsActive,
  useApp,
} = await vite.ssrLoadModule("/src/lib/store.ts");
const { initialProgress } = await vite.ssrLoadModule("/src/lib/progress.ts");
const { AttemptInspector } = await vite.ssrLoadModule("/src/components/AttemptInspector.tsx");
const {
  CompactAttemptDock,
  compactAttemptErrorForRun,
  settleCompactAttemptAction,
} = await vite.ssrLoadModule("/src/components/CompactAttemptDock.tsx");
const { VideoTimelineBar } = await vite.ssrLoadModule("/src/components/canvas/VideoTimelineBar.tsx");
await vite.close();

import type { Artifact, Conversation } from "../src/lib/types.ts";

const conversation = (id: string, artifact: Artifact): Conversation => ({
  id,
  title: id,
  created_at: 1,
  updated_at: 1,
  messages: [],
  artifacts: { [artifact.artifact_id]: artifact },
  active_artifact_id: artifact.artifact_id,
  pending: true,
  run_id: "run_cancelling",
});

test("candidate actions stay disabled while the conversation run is cancelling", () => {
  const draft: Artifact = {
    artifact_id: "art_candidate_draft",
    name: "Candidate draft",
    artifact_type: "landing",
    canvas: { w: 1440, h: 900 },
    layers: [],
    candidate_draft: true,
    attempt_lineage: {
      source_run_id: "run_attempts",
      source_attempt: 1,
      source_candidate_id: "candidate-current",
      source_candidate_sha256: "a".repeat(64),
    },
  };
  const progress = initialProgress("run_cancelling", "attempt_publish");
  progress.phase = "cancelling";
  const candidateState = {
    current_conversation_id: "candidate",
    conversations: { candidate: conversation("candidate", draft) },
    runs_progress: { candidate: progress },
    run_attempts: {
      run_attempts: {
        run_id: "run_attempts",
        candidates: [{
          candidate_id: "candidate-other",
          run_id: "run_attempts",
          artifact_type: "landing",
          attempt: 2,
          max_attempts: 3,
          created_at: "2026-08-03T00:00:00Z",
          source_sha256: "b".repeat(64),
          safety_state: "ready",
          hard_blockers: [],
          warnings: [],
          source_url: "/candidate.html",
          preview_urls: [],
        }],
        selection_phase: "idle",
        loading: false,
      },
    },
  };
  Object.assign(useApp.getState(), candidateState);
  Object.assign(useApp.getInitialState(), candidateState);
  assert.equal(useApp.getState().run_attempts.run_attempts?.candidates.length, 1);

  const markup = renderToStaticMarkup(
    createElement(AttemptInspector, { variant: "panel", runId: "run_attempts" }),
  );

  assert.match(markup, /<button[^>]*disabled=""[^>]*>[\s\S]*?Publish as new final<\/button>/);
  assert.match(markup, /<button[^>]*disabled=""[^>]*>[\s\S]*?Use this attempt<\/button>/);
});

test("compact attempt actions stay disabled while their bundle is cancelling", () => {
  const candidate = {
    candidate_id: "candidate-ready",
    run_id: "run_attempts_compact",
    artifact_type: "poster",
    attempt: 1,
    max_attempts: 4,
    created_at: "2026-08-03T00:00:00Z",
    source_sha256: "a".repeat(64),
    safety_state: "ready",
    hard_blockers: [],
    warnings: [],
    source_url: "/candidate.html",
    preview_urls: [],
  };
  const compactState = {
    run_attempts: {
      run_attempts_compact: {
        run_id: "run_attempts_compact",
        candidates: [candidate],
        selection_phase: "idle",
        loading: false,
      },
    },
  };
  Object.assign(useApp.getState(), compactState);
  Object.assign(useApp.getInitialState(), compactState);

  const markup = renderToStaticMarkup(createElement(CompactAttemptDock, {
    runId: "run_attempts_compact",
    conversationId: "paper-child",
    pending: true,
    finalized: false,
    actionsDisabled: true,
  }));

  assert.match(
    markup,
    /<button[^>]*disabled=""[^>]*>[\s\S]*?Ready<\/span><\/button>/,
  );
});

test("compact attempt actions observe an active derived candidate publication", () => {
  const runId = "run_attempts_compact_publishing";
  const candidate = {
    candidate_id: "candidate-ready",
    run_id: runId,
    artifact_type: "poster",
    attempt: 1,
    max_attempts: 4,
    created_at: "2026-08-05T00:00:00Z",
    source_sha256: "a".repeat(64),
    safety_state: "ready" as const,
    hard_blockers: [],
    warnings: [],
    source_url: "/candidate.html",
    preview_urls: [],
  };
  const publishProgress = initialProgress("run_candidate_publish", "attempt_publish");
  publishProgress.phase = "running";
  const compactState = {
    run_attempts: {
      [runId]: {
        run_id: runId,
        candidates: [candidate],
        selection_phase: "idle" as const,
        loading: false,
      },
    },
    runs_progress: {
      "derived-child:candidate-publish": publishProgress,
    },
  };
  Object.assign(useApp.getState(), compactState);
  Object.assign(useApp.getInitialState(), compactState);

  const markup = renderToStaticMarkup(createElement(CompactAttemptDock, {
    runId,
    conversationId: "derived-child",
    pending: true,
    finalized: false,
    actionsDisabled: false,
  }));

  assert.match(
    markup,
    /<button[^>]*disabled=""[^>]*>[\s\S]*?Ready<\/span><\/button>/,
  );
});

test("externally rerendered SSR snapshots reflect pre-ack ownership", () => {
  const runId = "run_pre_ack_reactive";
  const conversationId = "conversation_pre_ack_reactive";
  const artifact: Artifact = {
    artifact_id: "art_pre_ack_reactive",
    name: "Poster source",
    artifact_type: "poster",
    canvas: { w: 1600, h: 900 },
    layers: [],
  };
  const candidate = {
    candidate_id: "candidate-pre-ack-reactive",
    run_id: runId,
    artifact_type: "poster",
    attempt: 1,
    max_attempts: 4,
    created_at: "2026-08-05T00:00:00Z",
    source_sha256: "d".repeat(64),
    safety_state: "ready" as const,
    hard_blockers: [],
    warnings: [],
    source_url: "/candidate.html",
    preview_urls: [],
  };
  const initial = {
    mode: "chat" as const,
    ui_language: "en" as const,
    current_conversation_id: conversationId,
    conversations: {
      [conversationId]: {
        ...conversation(conversationId, artifact),
        pending: true,
        run_id: runId,
      },
    },
    runs_progress: {},
    candidate_publication_owners: {},
    run_attempts: {
      [runId]: {
        run_id: runId,
        candidates: [candidate],
        selection_phase: "idle" as const,
        loading: false,
      },
    },
  };
  useApp.setState(initial);
  Object.assign(useApp.getInitialState(), initial);

  const snapshots: Array<{
    active: boolean;
    inspectorDisabled: boolean;
    dockDisabled: boolean;
  }> = [];
  const renderSnapshot = () => {
    Object.assign(useApp.getInitialState(), useApp.getState());
    const inspector = renderToStaticMarkup(createElement(AttemptInspector, {
      variant: "panel",
      runId,
    }));
    const dock = renderToStaticMarkup(createElement(CompactAttemptDock, {
      runId,
      conversationId,
      pending: true,
      finalized: false,
      actionsDisabled: false,
    }));
    const inspectorButton = inspector.match(
      /<button([^>]*)>Use this attempt<\/button>/,
    )?.[1] ?? "";
    const dockButton = dock.match(
      /<button([^>]*)>[\s\S]*?Ready<\/span><\/button>/,
    )?.[1] ?? "";
    snapshots.push({
      active: candidatePublicationIsActive(useApp.getState(), conversationId),
      inspectorDisabled: /disabled=""/.test(inspectorButton),
      dockDisabled: /disabled=""/.test(dockButton),
    });
  };

  renderSnapshot();
  const unsubscribe = useApp.subscribe(renderSnapshot);
  const token = Symbol("pre-ack-reactive");
  useApp.setState({
    candidate_publication_owners: {
      [conversationId]: {
        token,
        operationConversationId: `${conversationId}:candidate-publish`,
      },
    },
  });
  useApp.setState({ candidate_publication_owners: {} });
  unsubscribe();

  assert.deepEqual(snapshots, [
    { active: false, inspectorDisabled: false, dockDisabled: false },
    { active: true, inspectorDisabled: true, dockDisabled: true },
    { active: false, inspectorDisabled: false, dockDisabled: false },
  ]);
  assert.equal(useApp.getState().conversations[conversationId].run_id, runId);
  assert.equal(useApp.getState().conversations[conversationId].messages.length, 0);
  assert.deepEqual(useApp.getState().runs_progress, {});
});

test("blocked compact attempts present a neutral attempt label", () => {
  const candidate = {
    candidate_id: "candidate-blocked",
    run_id: "run_attempts_compact_blocked",
    artifact_type: "poster",
    attempt: 1,
    max_attempts: 4,
    created_at: "2026-08-04T00:00:00Z",
    source_sha256: "a".repeat(64),
    safety_state: "blocked" as const,
    hard_blockers: [{ issue_id: "overflow", message: "Canvas repair required" }],
    warnings: [],
    source_url: "/candidate.html",
    preview_urls: [],
  };
  const compactState = {
    run_attempts: {
      run_attempts_compact_blocked: {
        run_id: "run_attempts_compact_blocked",
        candidates: [candidate],
        selection_phase: "idle" as const,
        loading: false,
      },
    },
  };
  Object.assign(useApp.getState(), compactState);
  Object.assign(useApp.getInitialState(), compactState);

  const markup = renderToStaticMarkup(createElement(CompactAttemptDock, {
    runId: "run_attempts_compact_blocked",
    conversationId: "paper-child",
    pending: true,
    finalized: false,
    actionsDisabled: false,
  }));

  assert.match(markup, /<button[^>]*>[\s\S]*?Attempt 1<\/span><\/button>/);
  assert.doesNotMatch(markup, /Fix in Canvas · generation continues|red-/);
});

test("Video compact attempts distinguish a source draft from final delivery", () => {
  const candidate = {
    candidate_id: "candidate-video",
    run_id: "run_attempts_compact_video",
    artifact_type: "video",
    attempt: 1,
    max_attempts: 4,
    created_at: "2026-08-04T00:00:00Z",
    source_sha256: "a".repeat(64),
    safety_state: "ready" as const,
    hard_blockers: [],
    warnings: [],
    source_url: "/candidate.html",
    preview_urls: [],
  };
  const compactState = {
    run_attempts: {
      run_attempts_compact_video: {
        run_id: "run_attempts_compact_video",
        candidates: [candidate],
        selection_phase: "idle" as const,
        loading: false,
      },
    },
  };
  Object.assign(useApp.getState(), compactState);
  Object.assign(useApp.getInitialState(), compactState);

  const markup = renderToStaticMarkup(createElement(CompactAttemptDock, {
    runId: "run_attempts_compact_video",
    conversationId: "paper-child",
    pending: true,
    finalized: false,
    actionsDisabled: false,
  }));

  assert.match(markup, /Source ready/);
  assert.doesNotMatch(markup, /<span>Ready<\/span>/);
});

test("blocked compact attempts retain the Korean Attempt label", () => {
  const candidate = {
    candidate_id: "candidate-blocked-ko",
    run_id: "run_attempts_compact_blocked_ko",
    artifact_type: "poster",
    attempt: 1,
    max_attempts: 4,
    created_at: "2026-08-04T00:00:00Z",
    source_sha256: "a".repeat(64),
    safety_state: "blocked" as const,
    hard_blockers: [{ issue_id: "overflow", message: "Canvas repair required" }],
    warnings: [],
    source_url: "/candidate.html",
    preview_urls: [],
  };
  const compactState = {
    ui_language: "ko" as const,
    run_attempts: {
      run_attempts_compact_blocked_ko: {
        run_id: "run_attempts_compact_blocked_ko",
        candidates: [candidate],
        selection_phase: "idle" as const,
        loading: false,
      },
    },
  };
  Object.assign(useApp.getState(), compactState);
  Object.assign(useApp.getInitialState(), compactState);

  const markup = renderToStaticMarkup(createElement(CompactAttemptDock, {
    runId: "run_attempts_compact_blocked_ko",
    conversationId: "paper-child",
    pending: true,
    finalized: false,
    actionsDisabled: false,
  }));

  assert.match(markup, /시도 1/);
  Object.assign(useApp.getState(), { ui_language: "en" });
  Object.assign(useApp.getInitialState(), { ui_language: "en" });
});

test("compact attempt action failures are captured instead of rejected", async () => {
  const expected = new Error("attempt state changed during request");
  const failure = await settleCompactAttemptAction(async () => {
    throw expected;
  });
  assert.equal(failure, expected);
});

test("compact attempt errors do not carry into a replacement run", () => {
  const error = { runId: "old-run", message: "old failure" };
  assert.equal(compactAttemptErrorForRun(error, "old-run"), "old failure");
  assert.equal(compactAttemptErrorForRun(error, "replacement-run"), null);
});

test("compact attempt failures keep availability separate from localized diagnostics", () => {
  const translations = {
    en: { summary: "Attempt action failed. Please retry.", ready: "Ready" },
    zh: { summary: "操作失败，请重试。", ready: "就绪" },
    ko: { summary: "작업에 실패했습니다. 다시 시도해 주세요.", ready: "준비됨" },
  } as const;

  for (const [language, expected] of Object.entries(translations)) {
    const runId = `run_attempt_failure_${language}`;
    const failureState = {
      ui_language: language,
      runs_progress: {},
      run_attempts: {
        [runId]: {
          run_id: runId,
          candidates: [{
            candidate_id: "candidate-failed",
            run_id: runId,
            artifact_type: "poster",
            attempt: 1,
            max_attempts: 4,
            created_at: "2026-08-05T00:00:00Z",
            source_sha256: "a".repeat(64),
            safety_state: "ready" as const,
            hard_blockers: [],
            warnings: [],
            source_url: "/candidate.html",
            preview_urls: [],
          }],
          selection_phase: "failed" as const,
          selection: {
            candidate_id: "candidate-failed",
            source_attempt: 1,
            state: "failed" as const,
            error_message: "promotion lease expired",
          },
          loading: false,
        },
      },
    };
    Object.assign(useApp.getState(), failureState);
    Object.assign(useApp.getInitialState(), failureState);
    const markup = renderToStaticMarkup(createElement(CompactAttemptDock, {
      runId,
      conversationId: `failure-child-${language}`,
      pending: false,
      finalized: false,
      actionsDisabled: false,
    }));
    const diagnostic = markup.match(/<details[^>]*>[\s\S]*?<\/details>/)?.[0] ?? "";

    assert.match(markup, new RegExp(expected.ready));
    assert.match(diagnostic, new RegExp(expected.summary));
    assert.match(diagnostic, /promotion lease expired/);
    assert.doesNotMatch(diagnostic, /red-/);
  }

  Object.assign(useApp.getState(), { ui_language: "en" });
  Object.assign(useApp.getInitialState(), { ui_language: "en" });
});

test("video rendering stays disabled while its run is cancelling", () => {
  const video: Artifact = {
    artifact_id: "art_video",
    name: "Video",
    artifact_type: "video",
    canvas: { w: 1920, h: 1080 },
    layers: [{
      layer_id: "frame-1",
      name: "Frame 1",
      kind: "background",
      z_index: 0,
      bbox: { x: 0, y: 0, w: 1920, h: 1080 },
    }],
    video_project: {
      duration_s: 5,
      fps: 30,
      scenes: [{
        scene_id: "scene-1",
        name: "Scene 1",
        frame_layer_id: "frame-1",
        duration_s: 5,
        transition: "cut",
      }],
    },
  };
  const progress = initialProgress("run_cancelling", "video_render");
  progress.phase = "cancelling";
  const videoState = {
    current_conversation_id: "video",
    conversations: { video: conversation("video", video) },
    runs_progress: { video: progress },
  };
  Object.assign(useApp.getState(), videoState);
  Object.assign(useApp.getInitialState(), videoState);

  const markup = renderToStaticMarkup(createElement(VideoTimelineBar, { art: video }));

  assert.match(markup, /<button[^>]*disabled=""[^>]*>[\s\S]*?Rendering<\/button>/);
});
