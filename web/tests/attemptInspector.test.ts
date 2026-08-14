import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const vite = await createServer({
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true },
  appType: "custom",
});
const {
  AttemptInspector,
  AttemptDiagnostics,
  attemptFinalizationPrompt,
  reduceAttemptActionDiagnostic,
} = await vite.ssrLoadModule(
  "/src/components/AttemptInspector.tsx",
);
const { Chat } = await vite.ssrLoadModule("/src/components/Chat.tsx");
const { Sidebar } = await vite.ssrLoadModule("/src/components/sidebar/Sidebar.tsx");
const { useApp } = await vite.ssrLoadModule("/src/lib/store.ts");
const { initialProgress } = await vite.ssrLoadModule("/src/lib/progress.ts");
await vite.close();

const candidates = [
  {
    candidate_id: "poster-attempt-01",
    run_id: "run-1",
    artifact_type: "poster",
    attempt: 1,
    max_attempts: 4,
    created_at: "2026-07-30T01:00:00Z",
    source_sha256: "a".repeat(64),
    safety_state: "blocked",
    hard_blockers: [{
      issue_id: "paper_poster_html_editorial_flow_shape_failed",
      message: "Poster validation finding",
    }],
    warnings: [],
    source_url: "/api/files/runs/run-1/attempt-01/poster.html",
    preview_urls: ["/api/files/runs/run-1/attempt-01/poster.png"],
  },
  {
    candidate_id: "poster-attempt-02",
    run_id: "run-1",
    artifact_type: "poster",
    attempt: 2,
    max_attempts: 4,
    created_at: "2026-07-30T01:05:00Z",
    source_sha256: "b".repeat(64),
    safety_state: "ready",
    hard_blockers: [],
    warnings: [],
    source_url: "/api/files/runs/run-1/attempt-02/poster.html",
    preview_urls: ["/api/files/runs/run-1/attempt-02/poster.png"],
  },
];

function renderInspector(
  variant: "rail" | "panel",
  language: "en" | "zh" | "ko" = "en",
) {
  const store = useApp.getState();
  Object.assign(store, {
    mode: "chat",
    ui_language: language,
    current_conversation_id: "conv-1",
    conversations: {
      "conv-1": {
        id: "conv-1",
        title: "Poster",
        created_at: 1,
        updated_at: 1,
        messages: [{
          id: "msg-run-1",
          role: "assistant",
          text: "Generating poster",
          ts: 1,
          run_id: "run-1",
          status: "streaming",
        }],
        artifacts: {},
        active_artifact_id: null,
        poster_palette_id: null,
        run_id: "run-1",
        pending: true,
      },
    },
    run_attempts: {
      "run-1": {
        run_id: "run-1",
        candidates,
        active_attempt: 3,
        selection_phase: "idle",
        loading: false,
      },
    },
  });
  return renderToStaticMarkup(
    createElement(AttemptInspector, { runId: "run-1", variant }),
  );
}

function renderDraftPublisher(
  stateKind: "ready" | "blocked" | "missing" | "candidate-missing" | "loading" | "error" | "publishing",
) {
  const store = useApp.getState();
  const sourceRunId = "run-publish-source";
  const candidateId = "poster-attempt-target";
  const draft = {
    artifact_id: "attempt-draft-target",
    name: "Poster attempt",
    artifact_type: "poster",
    canvas: { w: 1600, h: 900 },
    layers: [],
    candidate_draft: true,
    attempt_lineage: {
      source_run_id: sourceRunId,
      source_attempt: 2,
      source_candidate_id: candidateId,
    },
  };
  const exactCandidate = {
    ...candidates[1],
    candidate_id: candidateId,
    run_id: sourceRunId,
    safety_state: stateKind === "blocked" ? "blocked" : "ready",
  };
  const attemptState = stateKind === "missing"
    ? undefined
    : {
        run_id: sourceRunId,
        candidates: stateKind === "error"
          || stateKind === "loading"
          || stateKind === "ready"
          || stateKind === "blocked"
          || stateKind === "publishing"
          ? [exactCandidate]
          : [{ ...candidates[1], run_id: sourceRunId }],
        selection_phase: "idle",
        loading: stateKind === "loading",
        ...(stateKind === "error" ? { error: "attempt lookup failed" } : {}),
      };
  const publishProgress = initialProgress("run-candidate-publish", "attempt_publish");
  publishProgress.phase = "running";
  Object.assign(store, {
    mode: "canvas",
    ui_language: "en",
    current_conversation_id: "conv-publish",
    conversations: {
      "conv-publish": {
        id: "conv-publish",
        title: "Poster",
        created_at: 1,
        updated_at: 1,
        messages: [],
        artifacts: { [draft.artifact_id]: draft },
        active_artifact_id: draft.artifact_id,
        pending: true,
        run_id: sourceRunId,
      },
    },
    run_attempts: attemptState ? { [sourceRunId]: attemptState } : {},
    runs_progress: stateKind === "publishing"
      ? { "conv-publish:candidate-publish": publishProgress }
      : {},
  });
  return renderToStaticMarkup(
    createElement(AttemptInspector, { runId: sourceRunId, variant: "panel" }),
  );
}

function hasEnabledPublish(markup: string) {
  const button = markup.match(/<button[^>]*>Publish as new final<\/button>/)?.[0];
  return Boolean(button && !/disabled=""/.test(button));
}

function renderSelectionFailure(language: "en" | "zh" | "ko") {
  const runId = "run-selection-failure";
  const candidate = {
    ...candidates[1],
    run_id: runId,
  };
  const store = useApp.getState();
  Object.assign(store, {
    mode: "chat",
    ui_language: language,
    current_conversation_id: "conv-selection-failure",
    conversations: {
      "conv-selection-failure": {
        id: "conv-selection-failure",
        title: "Poster",
        created_at: 1,
        updated_at: 1,
        messages: [],
        artifacts: {},
        active_artifact_id: null,
        poster_palette_id: null,
        run_id: runId,
        pending: false,
      },
    },
    run_attempts: {
      [runId]: {
        run_id: runId,
        candidates: [candidate],
        selection_phase: "failed",
        selection: {
          candidate_id: candidate.candidate_id,
          source_attempt: candidate.attempt,
          state: "failed",
          error_message: "promotion lease expired",
        },
        loading: false,
      },
    },
    runs_progress: {},
  });
  return renderToStaticMarkup(
    createElement(AttemptInspector, { runId, variant: "panel" }),
  );
}

function renderNoOutputFailure() {
  const runId = "run-no-output-failure";
  const store = useApp.getState();
  Object.assign(store, {
    mode: "chat",
    ui_language: "en",
    current_conversation_id: "conv-no-output-failure",
    conversations: {
      "conv-no-output-failure": {
        id: "conv-no-output-failure",
        title: "Poster",
        created_at: 1,
        updated_at: 1,
        messages: [],
        artifacts: {},
        active_artifact_id: null,
        poster_palette_id: null,
        run_id: runId,
        pending: false,
      },
    },
    run_attempts: {
      [runId]: {
        run_id: runId,
        candidates: [],
        selection_phase: "idle",
        loading: false,
        error: "No attempt output is available.",
      },
    },
    runs_progress: {},
  });
  return renderToStaticMarkup(
    createElement(AttemptInspector, { runId, variant: "panel" }),
  );
}

function renderCachedCandidateError() {
  renderInspector("panel");
  const store = useApp.getState();
  Object.assign(store, {
    conversations: {
      ...store.conversations,
      "conv-1": {
        ...store.conversations["conv-1"],
        pending: false,
      },
    },
    run_attempts: {
      "run-1": {
        ...store.run_attempts["run-1"],
        error: "Attempt refresh temporarily unavailable.",
      },
    },
  });
  return renderToStaticMarkup(
    createElement(AttemptInspector, { runId: "run-1", variant: "panel" }),
  );
}

function renderPublishedLineageError(
  lineageKind: "published" | "wrong-source" | "missing-identity" | "draft",
) {
  const runId = "run-final-artifact-error";
  const artifactId = "art-final-artifact-error";
  const isDraft = lineageKind === "draft";
  const store = useApp.getState();
  Object.assign(store, {
    mode: "chat",
    ui_language: "en",
    current_conversation_id: "conv-final-artifact-error",
    conversations: {
      "conv-final-artifact-error": {
        id: "conv-final-artifact-error",
        title: "Poster",
        created_at: 1,
        updated_at: 1,
        messages: [{
          id: "msg-final-artifact-error",
          role: "assistant",
          text: "Published poster",
          ts: 1,
          run_id: "run-derived-publication",
          artifact_id: artifactId,
          status: "done",
          task_type: "candidate_publish",
          task_payload: {
            artifact_type: "poster",
            source_artifact_id: "art-source-draft",
            source_run_id: runId,
            source_candidate_id: "poster-attempt-02",
          },
        }],
        artifacts: {
          [artifactId]: {
            artifact_id: artifactId,
            name: "Published poster",
            artifact_type: "poster",
            canvas: { w: 1600, h: 900 },
            layers: [],
            candidate_draft: isDraft,
            attempt_lineage: {
              source_run_id: lineageKind === "wrong-source" ? "run-other" : runId,
              source_attempt: 2,
              source_candidate_id: "poster-attempt-02",
              source_candidate_sha256: "c".repeat(64),
              status: isDraft ? "draft" : "published",
              published_version_id: isDraft ? undefined : "version-published-02",
            },
          },
        },
        active_artifact_id: artifactId,
        published_artifact_id: lineageKind === "missing-identity" ? undefined : artifactId,
        poster_palette_id: null,
        pending: false,
      },
    },
    run_attempts: {
      [runId]: {
        run_id: runId,
        candidates: [],
        selection_phase: "idle",
        loading: false,
        error: "Attempt refresh failed after publication.",
      },
    },
    runs_progress: {},
  });
  return renderToStaticMarkup(
    createElement(AttemptInspector, { runId, variant: "panel" }),
  );
}

test("attempt inspector presents design versions without internal safety language", () => {
  const markup = renderInspector("panel");

  assert.match(markup, /Attempt 1/);
  assert.match(markup, /Attempt 2/);
  assert.ok(markup.indexOf("Attempt 1") < markup.indexOf("Attempt 2"));
  assert.match(markup, />Preview</);
  assert.match(markup, /Fix in Canvas · generation continues/);
  assert.match(markup, /Use this attempt/);
  assert.doesNotMatch(
    markup,
    /Blocked|Poster validation finding|paper_poster_html_editorial_flow_shape_failed/,
  );
});

test("attempt inspector publishes repaired drafts once the exact candidate is loaded", () => {
  const availability = Object.fromEntries(
    (["ready", "blocked", "missing", "candidate-missing", "loading", "error", "publishing"] as const)
      .map((stateKind) => [stateKind, hasEnabledPublish(renderDraftPublisher(stateKind))]),
  );

  assert.deepEqual(availability, {
    ready: true,
    blocked: true,
    missing: false,
    "candidate-missing": false,
    loading: false,
    error: false,
    publishing: false,
  });
  assert.doesNotMatch(renderDraftPublisher("blocked"), /title="Fix in Canvas"/);
});

test("selection failure with an available candidate is neutral, localized, and retryable", () => {
  const copies = {
    en: {
      summary: "Automatic finalization stopped; attempt kept",
      retry: "Retry finalization",
    },
    zh: {
      summary: "自动完成流程已停止；尝试版本已保留",
      retry: "重试完成流程",
    },
    ko: {
      summary: "자동 마무리는 중단됐지만 시도 버전은 그대로 보관했습니다",
      retry: "최종화 재시도",
    },
  } as const;

  for (const language of ["en", "zh", "ko"] as const) {
    const markup = renderSelectionFailure(language);
    const summary = markup.match(/<summary[^>]*>([^<]+)<\/summary>/)?.[1];
    const retryButton = markup.match(
      new RegExp(`<button[^>]*>${copies[language].retry}<\\/button>`),
    )?.[0];

    assert.equal(summary, copies[language].summary);
    assert.doesNotMatch(summary ?? "", /failed/i);
    assert.match(markup, /<details/);
    assert.match(markup, /promotion lease expired/);
    assert.doesNotMatch(markup, /role="alert"|\bred-/);
    assert.ok(retryButton && !/disabled=""/.test(retryButton));
  }
});

test("a reachable no-output attempt error remains a red alert", () => {
  const markup = renderNoOutputFailure();

  assert.match(markup, /role="alert"/);
  assert.match(markup, /\bborder-red-|\bbg-red-|\btext-red-/);
  assert.match(markup, /No attempt output is available\./);
});

test("cached candidates keep refresh diagnostics neutral and ordinary actions enabled", () => {
  const markup = renderCachedCandidateError();
  const actionButtons = [...markup.matchAll(
    /<button([^>]*)>(?:Fix in Canvas · generation continues|Use this attempt)<\/button>/g,
  )];

  assert.match(markup, /<details/);
  assert.match(markup, /Automatic finalization stopped; attempt kept/);
  assert.match(markup, /Attempt refresh temporarily unavailable\./);
  assert.doesNotMatch(markup, /role="alert"|\bborder-red-|\bbg-red-|\btext-red-/);
  assert.ok(actionButtons.length > 0);
  assert.ok(actionButtons.every((match) => !/disabled=""/.test(match[1])));

  const draftMarkup = renderDraftPublisher("error");
  assert.equal(hasEnabledPublish(draftMarkup), false);
  assert.match(draftMarkup, /<details/);
  assert.match(draftMarkup, /attempt lookup failed/);
  assert.doesNotMatch(draftMarkup, /role="alert"|\bborder-red-|\bbg-red-|\btext-red-/);
});

test("exact published lineage keeps a transient refresh diagnostic neutral", () => {
  const markup = renderPublishedLineageError("published");

  assert.match(markup, /<details/);
  assert.match(markup, /Automatic finalization stopped; attempt kept/);
  assert.match(markup, /Attempt refresh failed after publication\./);
  assert.doesNotMatch(markup, /role="alert"|\bborder-red-|\bbg-red-|\btext-red-/);
});

test("wrong, missing, and draft publication lineage remain genuine no-output errors", () => {
  for (const lineageKind of ["wrong-source", "missing-identity", "draft"] as const) {
    const markup = renderPublishedLineageError(lineageKind);

    assert.match(markup, /role="alert"/);
    assert.match(markup, /\bborder-red-|\bbg-red-|\btext-red-/);
    assert.match(markup, /Attempt refresh failed after publication\./);
    assert.doesNotMatch(markup, /<details/);
  }
});

test("state and matching action diagnostics coexist and follow action lifecycle", () => {
  const failed = reduceAttemptActionDiagnostic(null, {
    type: "failed",
    runId: "run-diagnostics",
    key: "open:attempt-02",
    message: "Opening the attempt failed.",
  });
  const markup = renderToStaticMarkup(createElement(AttemptDiagnostics, {
    runId: "run-diagnostics",
    language: "en",
    stateError: "Attempt refresh also failed.",
    actionError: failed,
    hasUsableOutput: true,
  }));

  assert.match(markup, /<details/);
  assert.match(markup, /Attempt refresh also failed\./);
  assert.match(markup, /Opening the attempt failed\./);
  assert.doesNotMatch(markup, /role="alert"|\bborder-red-|\bbg-red-|\btext-red-/);

  const unrelatedSuccess = reduceAttemptActionDiagnostic(failed, {
    type: "succeeded",
    runId: "run-diagnostics",
    key: "open:attempt-01",
  });
  assert.deepEqual(unrelatedSuccess, failed);
  assert.equal(reduceAttemptActionDiagnostic(failed, {
    type: "succeeded",
    runId: "run-diagnostics",
    key: "open:attempt-02",
  }), null);
  assert.equal(reduceAttemptActionDiagnostic(failed, {
    type: "run_changed",
    runId: "run-replacement",
  }), null);
});

test("rail exposes collapse and responsive sheet controls", () => {
  const markup = renderInspector("rail");

  assert.match(markup, /data-attempt-inspector="rail"/);
  assert.match(markup, /aria-label="Collapse attempts"/);
  assert.match(markup, />Attempts 2</);
  assert.doesNotMatch(
    markup,
    /Minimum delivery checks passed|Warnings|Blocked/,
  );
});

test("full chat places attempts in the right inspector instead of the bottom dock", () => {
  renderInspector("panel");

  const markup = renderToStaticMarkup(
    createElement(Chat, { variant: "full" }),
  );

  assert.match(markup, /data-attempt-inspector="rail"/);
  assert.doesNotMatch(markup, /data-attempt-dock="bottom"/);
  const workspace = markup.match(
    /<div data-chat-workspace="true" class="([^"]+)"/,
  );
  assert.ok(workspace);
  assert.doesNotMatch(workspace[1], /\bz-\d+\b/);
});

test("rail controls have focused Chinese and Korean translations", () => {
  assert.match(
    renderInspector("rail", "zh"),
    /aria-label="收起尝试版本"/,
  );
  assert.match(
    renderInspector("rail", "ko"),
    /aria-label="시도 버전 접기"/,
  );
});

test("candidate drafts open with Attempts in the existing Canvas sidebar", () => {
  renderInspector("panel");
  const store = useApp.getState();
  const conversation = store.conversations["conv-1"];
  const draft = {
    artifact_id: "attempt-draft-01",
    name: "Poster attempt 1",
    artifact_type: "poster",
    canvas: { w: 1600, h: 900 },
    layers: [],
    native_format: "html",
    native_file_url: "/api/files/runs/run-1/attempt-01/poster.html",
    candidate_draft: true,
    attempt_lineage: {
      source_run_id: "run-1",
      source_attempt: 1,
      source_candidate_id: "poster-attempt-01",
      source_candidate_sha256: "a".repeat(64),
    },
  };
  Object.assign(store, {
    mode: "canvas",
    selected_layer_id: null,
    selected_layer_ids: [],
    conversations: {
      ...store.conversations,
      "conv-1": {
        ...conversation,
        artifacts: { [draft.artifact_id]: draft },
        active_artifact_id: draft.artifact_id,
      },
    },
  });

  const markup = renderToStaticMarkup(createElement(Sidebar));

  assert.match(markup, />Attempts</);
  assert.match(markup, />Preview</);
  assert.doesNotMatch(markup, /Blocked|Poster validation finding/);
});

test("attempt confirmation is destructive only while the exact source is active", () => {
  assert.equal(
    typeof attemptFinalizationPrompt,
    "function",
    "AttemptInspector and CompactAttemptDock need one shared source-aware prompt",
  );

  assert.deepEqual(
    attemptFinalizationPrompt("en", 2, true),
    {
      title: "Stop generating and finalize Attempt 2?",
      detail: "The active attempt will be terminated and discarded. Completed attempts remain available after finalization.",
    },
  );
  const neutralCopies = {
    en: "Finalize Attempt 2?",
    zh: "将尝试版本 2 设为最终版本？",
    ko: "시도 2를 최종본으로 만들까요?",
  } as const;
  for (const language of ["en", "zh", "ko"] as const) {
    const prompt = attemptFinalizationPrompt(language, 2, false);
    assert.equal(prompt.title, neutralCopies[language]);
    assert.doesNotMatch(prompt.title, /stop|停止|중단/i);
    assert.doesNotMatch(prompt.detail, /terminated|discarded|终止|丢弃|종료|폐기/i);
  }
});
