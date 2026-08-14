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
const { ProgressCard } = await vite.ssrLoadModule("/src/components/ProgressCard.tsx");
const { initialProgress } = await vite.ssrLoadModule("/src/lib/progress.ts");
const { useApp } = await vite.ssrLoadModule("/src/lib/store.ts");
await vite.close();

test("an in-flight cancellation request disables and relabels the cancel action", () => {
  const storeState = useApp.getState();
  const previousStoreState = {
    current_conversation_id: storeState.current_conversation_id,
    ui_language: storeState.ui_language,
    runs_progress: storeState.runs_progress,
  };
  const progress = initialProgress("run_cancelling");
  progress.phase = "cancelling";
  progress.label = "Stopping run…";
  progress.cancel_request_in_flight = true;

  try {
    Object.assign(storeState, {
      current_conversation_id: "conv_cancelling",
      ui_language: "en",
      runs_progress: { conv_cancelling: progress },
    });

    const markup = renderToStaticMarkup(createElement(ProgressCard, {}));

    assert.match(markup, /<button[^>]*disabled=""[^>]*>/);
    assert.match(markup, /Cancelling…/);
  } finally {
    Object.assign(storeState, previousStoreState);
  }
});

test("an unconfirmed cancellation with no request in flight exposes retry", () => {
  const storeState = useApp.getState();
  const previousStoreState = {
    current_conversation_id: storeState.current_conversation_id,
    ui_language: storeState.ui_language,
    runs_progress: storeState.runs_progress,
  };
  const progress = initialProgress("run_retry_cancel");
  progress.phase = "cancelling";
  progress.label = "Cancellation not confirmed; backend may still be stopping";
  progress.cancel_request_in_flight = false;

  try {
    Object.assign(storeState, {
      current_conversation_id: "conv_retry_cancel",
      ui_language: "en",
      runs_progress: { conv_retry_cancel: progress },
    });

    const markup = renderToStaticMarkup(createElement(ProgressCard, {}));

    assert.match(markup, />Retry cancellation<\/button>/);
    assert.doesNotMatch(markup, /<button[^>]*disabled=""[^>]*>[^<]*Retry cancellation/);
  } finally {
    Object.assign(storeState, previousStoreState);
  }
});
