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
const { PaperBundleCard } = await vite.ssrLoadModule(
  "/src/components/PaperBundleCard.tsx",
);
const { createPaperBundleParentState } = await vite.ssrLoadModule(
  "/src/lib/paper_bundle.ts",
);
const { applyEvent, initialProgress } = await vite.ssrLoadModule(
  "/src/lib/progress.ts",
);
const { useApp } = await vite.ssrLoadModule("/src/lib/store.ts");
await vite.close();

test("paper bundle cards show elapsed time immediately and attempts after they start", () => {
  const originalDateNow = Date.now;
  const now = 100_000;
  const storeState = useApp.getState();
  const previousStoreState = {
    current_conversation_id: storeState.current_conversation_id,
    ui_language: storeState.ui_language,
    conversations: storeState.conversations,
    runs_progress: storeState.runs_progress,
    run_attempts: storeState.run_attempts,
  };
  Date.now = () => now;

  try {
    const parentId = "conv_parent";
    const bundle = createPaperBundleParentState(parentId, "paper.pdf");
    const progresses = {};

    for (const task of Object.values(bundle.tasks)) {
      task.status = "running";
      const progress = initialProgress(`run_${task.artifact_type}`);
      progress.started_at = now - 65_000;
      progresses[task.child_conversation_id] = task.artifact_type === "deck"
        ? applyEvent(progress, {
            event: "slides_author.attempt_start",
            attempt: 2,
            max_attempts: 12,
          })
        : progress;
    }

    Object.assign(storeState, {
      current_conversation_id: parentId,
      ui_language: "en",
      conversations: {
        [parentId]: {
          id: parentId,
          title: "paper.pdf",
          created_at: now,
          updated_at: now,
          messages: [],
          artifacts: {},
          active_artifact_id: null,
          poster_palette_id: null,
          paper_bundle: bundle,
          pending: true,
        },
      },
      runs_progress: progresses,
    });

    const markup = renderToStaticMarkup(
      createElement(PaperBundleCard, { bundle }),
    );

    assert.doesNotMatch(markup, /Attempt 0/);
    assert.match(markup, /Attempt 2 of 12/);
    assert.equal(markup.match(/1m 05s/g)?.length, 4);
    assert.equal(markup.match(/title="Cancel this task"/g)?.length, 4);
  } finally {
    Object.assign(storeState, previousStoreState);
    Date.now = originalDateNow;
  }
});

test("failed bundle task waits for persisted attempts before showing a failure", () => {
  const storeState = useApp.getState();
  const previousStoreState = {
    current_conversation_id: storeState.current_conversation_id,
    ui_language: storeState.ui_language,
    conversations: storeState.conversations,
    runs_progress: storeState.runs_progress,
    run_attempts: storeState.run_attempts,
  };
  const parentId = "conv_attempt_hydration";
  const runId = "run_attempt_hydration";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  bundle.tasks.deck = {
    ...bundle.tasks.deck,
    status: "failed",
    run_id: runId,
    max_attempts: 4,
    error: "terminal transport message",
  };
  bundle.backend_state = "failed";

  try {
    Object.assign(storeState, {
      current_conversation_id: parentId,
      ui_language: "en",
      conversations: {
        [parentId]: {
          id: parentId,
          title: "paper.pdf",
          created_at: 1,
          updated_at: 1,
          messages: [],
          artifacts: {},
          active_artifact_id: null,
          poster_palette_id: null,
          paper_bundle: bundle,
          pending: true,
        },
      },
      runs_progress: {},
      run_attempts: {},
    });
    const loadingMarkup = renderToStaticMarkup(
      createElement(PaperBundleCard, { bundle }),
    );
    assert.match(loadingMarkup, /Checking attempts/);
    assert.doesNotMatch(loadingMarkup, />Failed</);
    assert.doesNotMatch(loadingMarkup, /Retry/);

    storeState.run_attempts = {
      [runId]: {
        run_id: runId,
        candidates: [],
        selection_phase: "idle",
        loading: true,
      },
    };
    const inFlightMarkup = renderToStaticMarkup(
      createElement(PaperBundleCard, { bundle }),
    );
    assert.match(inFlightMarkup, /Checking attempts/);
    assert.doesNotMatch(inFlightMarkup, />Failed</);
    assert.doesNotMatch(inFlightMarkup, /Retry/);

    storeState.run_attempts = {
      [runId]: {
        run_id: runId,
        candidates: [],
        selection_phase: "idle",
        loading: false,
        error: "Attempt history request timed out.",
      },
    };
    const retryHistoryMarkup = renderToStaticMarkup(
      createElement(PaperBundleCard, { bundle }),
    );
    assert.match(retryHistoryMarkup, /Attempt history unavailable/);
    assert.match(retryHistoryMarkup, /Retry attempts/);
    assert.doesNotMatch(retryHistoryMarkup, />Failed</);
    assert.doesNotMatch(retryHistoryMarkup, />Retry</);

    storeState.run_attempts = {
      [runId]: {
        run_id: runId,
        candidates: [],
        selection_phase: "idle",
        loading: false,
      },
    };
    const loadedMarkup = renderToStaticMarkup(
      createElement(PaperBundleCard, { bundle }),
    );
    assert.doesNotMatch(loadedMarkup, /Checking attempts/);
    assert.match(loadedMarkup, />Failed</);
    assert.match(loadedMarkup, /Retry/);
  } finally {
    Object.assign(storeState, previousStoreState);
  }
});

test("ready bundle cards retain their final elapsed time and attempts", () => {
  const originalDateNow = Date.now;
  const now = 200_000;
  const storeState = useApp.getState();
  const previousStoreState = {
    current_conversation_id: storeState.current_conversation_id,
    ui_language: storeState.ui_language,
    conversations: storeState.conversations,
    runs_progress: storeState.runs_progress,
  };
  Date.now = () => now;

  try {
    const parentId = "conv_ready";
    const bundle = createPaperBundleParentState(parentId, "ready.pdf");
    bundle.tasks.deck = {
      ...bundle.tasks.deck,
      status: "complete",
      attempts: 3,
      max_attempts: 12,
      started_at: now - 65_000,
      finished_at: now,
    };

    Object.assign(storeState, {
      current_conversation_id: parentId,
      ui_language: "en",
      conversations: {
        [parentId]: {
          id: parentId,
          title: "ready.pdf",
          created_at: now,
          updated_at: now,
          messages: [],
          artifacts: {},
          active_artifact_id: null,
          poster_palette_id: null,
          paper_bundle: bundle,
          pending: false,
        },
      },
      runs_progress: {},
    });

    const markup = renderToStaticMarkup(
      createElement(PaperBundleCard, { bundle }),
    );

    assert.match(markup, /Ready/);
    assert.match(markup, /Attempt 3 of 12/);
    assert.match(markup, /1m 05s/);
  } finally {
    Object.assign(storeState, previousStoreState);
    Date.now = originalDateNow;
  }
});

test("a finalized degraded artifact stays available without being labelled failed or clean", () => {
  const storeState = useApp.getState();
  const previousStoreState = {
    current_conversation_id: storeState.current_conversation_id,
    ui_language: storeState.ui_language,
    conversations: storeState.conversations,
    runs_progress: storeState.runs_progress,
    run_attempts: storeState.run_attempts,
  };

  try {
    const parentId = "conv_degraded_final";
    const artifactId = "art_degraded_deck";
    const bundle = createPaperBundleParentState(parentId, "paper.pdf");
    bundle.tasks.deck = {
      ...bundle.tasks.deck,
      status: "complete",
      artifact_id: artifactId,
    };

    Object.assign(storeState, {
      current_conversation_id: parentId,
      ui_language: "en",
      conversations: {
        [parentId]: {
          id: parentId,
          title: "paper.pdf",
          created_at: 1,
          updated_at: 2,
          messages: [],
          artifacts: {
            [artifactId]: {
              artifact_id: artifactId,
              name: "Deck with refinement notes",
              artifact_type: "deck",
              canvas: { w: 1920, h: 1080 },
              layers: [],
              native_file_url: "/fixtures/degraded-deck.html",
              native_format: "html",
              preview_url: "/fixtures/degraded-deck.png",
              quality_status: "ready_with_warnings",
              quality_diagnostics: ["One caption is clipped."],
            },
          },
          active_artifact_id: null,
          paper_bundle: bundle,
          pending: false,
        },
      },
      runs_progress: {},
      run_attempts: {},
    });

    const markup = renderToStaticMarkup(createElement(PaperBundleCard, { bundle }));
    assert.match(markup, />Needs refinement<\/span>/);
    assert.match(markup, /One caption is clipped\./);
    assert.match(markup, />Open<\/button>/);
    assert.match(markup, />Download<\/button>/);
    assert.doesNotMatch(markup, />Failed<\/span>/);
  } finally {
    Object.assign(storeState, previousStoreState);
  }
});

test("failed bundle tasks keep available attempts and accessible localized diagnostics", () => {
  const storeState = useApp.getState();
  const previousStoreState = {
    current_conversation_id: storeState.current_conversation_id,
    ui_language: storeState.ui_language,
    conversations: storeState.conversations,
    runs_progress: storeState.runs_progress,
    run_attempts: storeState.run_attempts,
  };

  try {
    const parentId = "conv_partial";
    const bundle = createPaperBundleParentState(parentId, "partial.pdf");
    bundle.tasks.poster.status = "complete";
    bundle.tasks.deck.status = "complete";
    bundle.tasks.landing.status = "complete";
    bundle.tasks.video = {
      ...bundle.tasks.video,
      status: "failed",
      run_id: "run_failed_video",
      error: "Video runtime was unavailable.",
    };

    Object.assign(storeState, {
      current_conversation_id: parentId,
      ui_language: "en",
      conversations: {
        [parentId]: {
          id: parentId,
          title: "partial.pdf",
          created_at: 1,
          updated_at: 2,
          messages: [],
          artifacts: {},
          active_artifact_id: null,
          poster_palette_id: null,
          paper_bundle: bundle,
          pending: false,
        },
      },
      runs_progress: {},
      run_attempts: {
        run_failed_video: {
          run_id: "run_failed_video",
          candidates: [{
            candidate_id: "video-attempt-02",
            run_id: "run_failed_video",
            artifact_type: "video",
            attempt: 2,
            max_attempts: 4,
            created_at: "2026-07-29T00:00:00Z",
            source_sha256: "a".repeat(64),
            safety_state: "ready_with_warnings",
            hard_blockers: [],
            warnings: [],
            source_url: "/api/files/runs/run_failed_video/video.html",
            preview_urls: [],
          }],
          selection_phase: "idle",
          loading: false,
        },
      },
    });

    const markup = renderToStaticMarkup(
      createElement(PaperBundleCard, { bundle }),
    );

    assert.match(markup, /Partially complete/);
    assert.equal(markup.match(/title="Retry this task"/g)?.length, 1);
    assert.doesNotMatch(markup, /title="Cancel this task"/);
    assert.match(markup, />Available<\/span>/);
    assert.match(markup, /title="Finalize Attempt 2\?"/);
    assert.doesNotMatch(markup, /Stop generating/);

    const translations = {
      en: {
        available: "Available",
        summary: "Attempt finalization failed",
        prompt: "Finalize Attempt 2?",
      },
      zh: {
        available: "可用",
        summary: "尝试版本完成失败",
        prompt: "将尝试版本 2 设为最终版本？",
      },
      ko: {
        available: "사용 가능",
        summary: "시도 버전 마무리 실패",
        prompt: "시도 2를 최종본으로 만들까요?",
      },
    } as const;
    for (const language of ["en", "zh", "ko"] as const) {
      storeState.ui_language = language;
      const localizedMarkup = renderToStaticMarkup(
        createElement(PaperBundleCard, { bundle }),
      );
      const diagnostic = localizedMarkup.match(
        /<details[^>]*>[\s\S]*?<\/details>/,
      )?.[0] ?? "";
      assert.match(localizedMarkup, new RegExp(translations[language].available));
      assert.match(localizedMarkup, new RegExp(
        `title="${translations[language].prompt.replace(/[?]/g, "\\?")}"`,
      ));
      assert.match(diagnostic, new RegExp(translations[language].summary));
      assert.match(diagnostic, /Video runtime was unavailable/);
      assert.doesNotMatch(diagnostic, /red-/);
    }
  } finally {
    Object.assign(storeState, previousStoreState);
  }
});

test("failed bundle tasks with only blocked drafts ask for refinement instead of claiming availability", () => {
  const storeState = useApp.getState();
  const previousStoreState = {
    current_conversation_id: storeState.current_conversation_id,
    ui_language: storeState.ui_language,
    conversations: storeState.conversations,
    runs_progress: storeState.runs_progress,
    run_attempts: storeState.run_attempts,
  };

  try {
    const parentId = "conv_blocked_landing";
    const runId = "run_blocked_landing";
    const bundle = createPaperBundleParentState(parentId, "blocked.pdf");
    bundle.tasks.poster.status = "complete";
    bundle.tasks.deck.status = "complete";
    bundle.tasks.video.status = "complete";
    bundle.tasks.landing = {
      ...bundle.tasks.landing,
      status: "failed",
      run_id: runId,
      authoring_run_id: runId,
      error: "Run finished without producing the requested artifact.",
    };

    const candidate = (attempt: number, issueId: string, message: string) => ({
      candidate_id: `landing-attempt-${attempt}`,
      run_id: runId,
      artifact_type: "landing" as const,
      attempt,
      max_attempts: 3,
      created_at: `2026-08-06T00:0${attempt}:00Z`,
      source_sha256: String(attempt).repeat(64),
      safety_state: "blocked" as const,
      hard_blockers: [{ issue_id: issueId, message }],
      warnings: [],
      source_url: `/api/files/runs/${runId}/attempt-${attempt}/landing.html`,
      preview_urls: [],
    });

    Object.assign(storeState, {
      current_conversation_id: parentId,
      ui_language: "en",
      conversations: {
        [parentId]: {
          id: parentId,
          title: "blocked.pdf",
          created_at: 1,
          updated_at: 2,
          messages: [],
          artifacts: {},
          active_artifact_id: null,
          poster_palette_id: null,
          paper_bundle: bundle,
          pending: false,
        },
      },
      runs_progress: {},
      run_attempts: {
        [runId]: {
          run_id: runId,
          candidates: [
            candidate(1, "landing_clipped_text", "Core text is clipped."),
            candidate(2, "landing_clipped_text", "Core text is clipped."),
            candidate(3, "landing_horizontal_overflow", "Mobile layout overflows by 1463px."),
          ],
          selection_phase: "idle",
          loading: false,
        },
      },
    });

    const translations = {
      en: {
        status: "Needs refinement",
        summary: "Attempts need refinement",
        available: "Available",
        finalizationFailed: "Attempt finalization failed",
      },
      zh: {
        status: "需要继续修复",
        summary: "尝试版本需要继续修复",
        available: "可用",
        finalizationFailed: "尝试版本完成失败",
      },
      ko: {
        status: "추가 수정 필요",
        summary: "시도 버전에 추가 수정이 필요합니다",
        available: "사용 가능",
        finalizationFailed: "시도 버전 마무리 실패",
      },
    } as const;
    for (const language of ["en", "zh", "ko"] as const) {
      storeState.ui_language = language;
      const markup = renderToStaticMarkup(
        createElement(PaperBundleCard, { bundle }),
      );
      const expected = translations[language];
      assert.match(markup, new RegExp(expected.status));
      assert.match(markup, new RegExp(expected.summary));
      assert.match(markup, /Core text is clipped/);
      assert.equal(markup.match(/Core text is clipped/g)?.length, 1);
      assert.match(markup, /Mobile layout overflows by 1463px/);
      assert.match(markup, /bg-amber-50 text-amber-900/);
      assert.doesNotMatch(markup, /bg-red-50 text-red-800/);
      assert.doesNotMatch(markup, /role="alert"/);
      assert.doesNotMatch(markup, new RegExp(`>${expected.available}<`));
      assert.doesNotMatch(markup, new RegExp(expected.finalizationFailed));
      assert.doesNotMatch(markup, /Finalize Attempt/);
    }
  } finally {
    Object.assign(storeState, previousStoreState);
  }
});

test("backend-managed bundle exposes uploading/cancelling states without legacy retry", () => {
  const storeState = useApp.getState();
  const previousStoreState = {
    current_conversation_id: storeState.current_conversation_id,
    ui_language: storeState.ui_language,
    conversations: storeState.conversations,
    runs_progress: storeState.runs_progress,
  };

  try {
    const parentId = "conv_backend_bundle";
    const bundle = createPaperBundleParentState(parentId, "paper.pdf");
    bundle.job_id = "job_bundle";
    bundle.backend_state = "cancelling";
    bundle.tasks.poster.status = "uploading";
    bundle.tasks.deck.status = "cancelling";
    bundle.tasks.landing = {
      ...bundle.tasks.landing,
      status: "failed",
      run_id: "run_landing",
    };
    bundle.tasks.video.status = "complete";
    bundle.cancel_request_in_flight = true;
    Object.assign(storeState, {
      current_conversation_id: parentId,
      ui_language: "en",
      conversations: {
        [parentId]: {
          id: parentId,
          title: "paper.pdf",
          created_at: 1,
          updated_at: 2,
          messages: [],
          artifacts: {},
          active_artifact_id: null,
          paper_bundle: bundle,
          pending: true,
        },
      },
      runs_progress: {},
    });

    const markup = renderToStaticMarkup(createElement(PaperBundleCard, { bundle }));

    assert.match(markup, /Uploading/);
    assert.match(markup, /Cancelling/);
    assert.match(markup, /disabled=""/);
    assert.doesNotMatch(markup, />Retry</);

    bundle.cancel_error = "Cancellation not confirmed; backend may still be stopping.";
    bundle.cancel_request_in_flight = false;
    const retryMarkup = renderToStaticMarkup(createElement(PaperBundleCard, { bundle }));
    assert.match(retryMarkup, /Retry cancellation/);
    assert.doesNotMatch(retryMarkup, /disabled=""/);
  } finally {
    Object.assign(storeState, previousStoreState);
  }
});

test("backend nonterminal state keeps Cancel All available despite stale failed tasks", () => {
  const storeState = useApp.getState();
  const previousStoreState = {
    current_conversation_id: storeState.current_conversation_id,
    ui_language: storeState.ui_language,
    conversations: storeState.conversations,
    runs_progress: storeState.runs_progress,
  };

  try {
    const parentId = "conv_backend_running";
    const bundle = createPaperBundleParentState(parentId, "paper.pdf");
    bundle.job_id = "job_bundle";
    bundle.backend_state = "running";
    for (const task of Object.values(bundle.tasks)) {
      task.status = "failed";
      task.run_id = `run_${task.artifact_type}`;
    }
    Object.assign(storeState, {
      current_conversation_id: parentId,
      ui_language: "en",
      conversations: {
        [parentId]: {
          id: parentId,
          title: "paper.pdf",
          created_at: 1,
          updated_at: 2,
          messages: [],
          artifacts: {},
          active_artifact_id: null,
          paper_bundle: bundle,
          pending: false,
        },
      },
      runs_progress: {},
    });

    const markup = renderToStaticMarkup(createElement(PaperBundleCard, { bundle }));
    assert.match(markup, /Cancel all running tasks/);

    bundle.backend_state = "cancelling";
    bundle.cancel_error = "Cancellation not confirmed; backend may still be stopping.";
    const retryMarkup = renderToStaticMarkup(createElement(PaperBundleCard, { bundle }));
    assert.match(retryMarkup, /Retry cancellation/);

    bundle.backend_state = "cancelled";
    bundle.cancel_error = undefined;
    const terminalMarkup = renderToStaticMarkup(createElement(PaperBundleCard, { bundle }));
    assert.match(terminalMarkup, /Cancelled/);
    assert.doesNotMatch(terminalMarkup, /Cancel all running tasks/);
  } finally {
    Object.assign(storeState, previousStoreState);
  }
});

test("single-child cancellation disables only that task's compact attempt actions", () => {
  const storeState = useApp.getState();
  const previousStoreState = {
    current_conversation_id: storeState.current_conversation_id,
    ui_language: storeState.ui_language,
    conversations: storeState.conversations,
    runs_progress: storeState.runs_progress,
    run_attempts: storeState.run_attempts,
  };
  try {
    const parentId = "conv_child_cancelling";
    const bundle = createPaperBundleParentState(parentId, "paper.pdf");
    bundle.job_id = "job_bundle";
    bundle.backend_state = "running";
    bundle.tasks.poster = {
      ...bundle.tasks.poster,
      status: "cancelling",
      run_id: "poster_attempt_run",
      authoring_run_id: "poster_attempt_run",
    };
    Object.assign(storeState, {
      current_conversation_id: parentId,
      ui_language: "en",
      conversations: {
        [parentId]: {
          id: parentId,
          title: "paper.pdf",
          created_at: 1,
          updated_at: 2,
          messages: [],
          artifacts: {},
          active_artifact_id: null,
          paper_bundle: bundle,
          pending: true,
        },
      },
      runs_progress: {},
      run_attempts: {
        poster_attempt_run: {
          run_id: "poster_attempt_run",
          candidates: [{
            candidate_id: "poster-attempt-1",
            run_id: "poster_attempt_run",
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
          }],
          selection_phase: "idle",
          loading: false,
        },
      },
    });

    const markup = renderToStaticMarkup(createElement(PaperBundleCard, { bundle }));
    assert.match(
      markup,
      /<button[^>]*disabled=""[^>]*>[\s\S]*?Ready<\/span><\/button>/,
    );
  } finally {
    Object.assign(storeState, previousStoreState);
  }
});

test("landing bundle preview uses a dedicated card thumbnail without cropping the QA image", () => {
  const storeState = useApp.getState();
  const previousStoreState = {
    current_conversation_id: storeState.current_conversation_id,
    ui_language: storeState.ui_language,
    conversations: storeState.conversations,
    runs_progress: storeState.runs_progress,
    run_attempts: storeState.run_attempts,
  };

  try {
    const parentId = "conv_tall_landing_preview";
    const bundle = createPaperBundleParentState(parentId, "paper.pdf");
    const artifactTypes = ["poster", "deck", "landing", "video"];
    const artifacts = {};
    for (const artifactType of artifactTypes) {
      const artifactId = `art_${artifactType}`;
      bundle.tasks[artifactType].status = "complete";
      bundle.tasks[artifactType].artifact_id = artifactId;
      artifacts[artifactId] = {
        artifact_id: artifactId,
        name: `${artifactType} artifact`,
        artifact_type: artifactType,
        canvas: artifactType === "landing"
          ? { w: 831, h: 4096 }
          : { w: 1920, h: 1080 },
        layers: [],
        native_file_url: `/fixtures/${artifactType}.html`,
        native_format: artifactType === "video" ? "mp4" : "html",
        preview_url: `/fixtures/${artifactType}-preview${
          artifactType === "landing" ? "-831x4096" : ""
        }.png`,
        card_preview_url: artifactType === "landing"
          ? "/fixtures/landing-card-preview-1440x900.png"
          : undefined,
      };
    }

    Object.assign(storeState, {
      current_conversation_id: parentId,
      ui_language: "en",
      conversations: {
        [parentId]: {
          id: parentId,
          title: "paper.pdf",
          created_at: 1,
          updated_at: 2,
          messages: [],
          artifacts,
          active_artifact_id: null,
          paper_bundle: bundle,
          pending: false,
        },
      },
      runs_progress: {},
      run_attempts: {},
    });

    const markup = renderToStaticMarkup(createElement(PaperBundleCard, { bundle }));

    assert.match(
      markup,
      /class="[^"]*object-contain[^"]*" src="\/fixtures\/landing-card-preview-1440x900\.png"/,
    );
    assert.match(
      markup,
      /class="[^"]*object-contain[^"]*" src="\/fixtures\/poster-preview\.png"/,
    );
    assert.match(
      markup,
      /class="[^"]*object-contain[^"]*" src="\/fixtures\/deck-preview\.png"/,
    );
    assert.match(markup, /<video class="[^"]*object-contain[^"]*"/);
    assert.doesNotMatch(
      markup,
      /src="\/fixtures\/landing-preview-831x4096\.png"/,
    );
  } finally {
    Object.assign(storeState, previousStoreState);
  }
});
