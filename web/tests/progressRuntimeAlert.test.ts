import assert from "node:assert/strict";
import test from "node:test";

import { applyEvent, initialProgress } from "../src/lib/progress.ts";

const REGION_ERROR =
  "PermissionDeniedError: Error code: 403 - {'error': {'message': 'This model is not available in your region.', 'code': 403}}";

test("keeps and classifies the complete provider error for a runtime alert", () => {
  const progress = applyEvent(initialProgress("run"), {
    event: "prompt.enhance.error",
    error: REGION_ERROR,
  });

  assert.deepEqual(progress.runtime_alert, {
    status_code: 403,
    title: "Model unavailable from the current network or region",
    message: "This model is not available in your region.",
    hint: "Check your VPN, network, account access, or provider route, then retry.",
    technical_detail: REGION_ERROR,
  });
  assert.equal(progress.recent[0]?.detail, "This model is not available in your region.");
});

test("explains paper memory degradation as a consequence of the upstream API error", () => {
  const withApiError = applyEvent(initialProgress("run"), {
    event: "paper_memory_agent.api_error",
    turn: 1,
    error: REGION_ERROR,
  });
  const degraded = applyEvent(withApiError, {
    event: "paper_memory_agent.degraded",
    reason: "no_valid_dossier",
  });

  assert.equal(withApiError.recent[0]?.label, "Paper memory API error · turn 1");
  assert.equal(withApiError.recent[0]?.detail, "This model is not available in your region.");
  assert.equal(
    degraded.recent[0]?.detail,
    "Paper memory was skipped because the upstream model request failed.",
  );
  assert.equal(degraded.runtime_alert?.technical_detail, REGION_ERROR);
});

test("classifies common provider failures without discarding their raw details", () => {
  const cases = [
    {
      error: "AuthenticationError: Error code: 401 - invalid API key",
      status: 401,
      title: "Provider authentication failed",
    },
    {
      error: "RateLimitError: Error code: 429 - too many requests",
      status: 429,
      title: "Provider rate limit reached",
    },
    {
      error: "InternalServerError: Error code: 503 - service unavailable",
      status: 503,
      title: "Provider service is temporarily unavailable",
    },
    {
      error: "APITimeoutError: request timed out while waiting for the provider",
      status: undefined,
      title: "Provider request timed out",
    },
  ] as const;

  for (const item of cases) {
    const progress = applyEvent(initialProgress("run"), {
      event: "prompt.enhance.error",
      error: item.error,
    });
    assert.equal(progress.runtime_alert?.status_code, item.status);
    assert.equal(progress.runtime_alert?.title, item.title);
    assert.equal(progress.runtime_alert?.technical_detail, item.error);
  }
});
