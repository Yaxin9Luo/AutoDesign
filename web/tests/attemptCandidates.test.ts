import assert from "node:assert/strict";
import test from "node:test";

import {
  candidateAction,
  isAttemptDraftEditing,
  normalizeRunAttemptState,
} from "../src/lib/attempt_candidates.ts";

const candidate = {
  candidate_id: "candidate-1",
  run_id: "run-1",
  artifact_type: "landing",
  attempt: 1,
  max_attempts: 4,
  created_at: "2026-07-29T00:00:00Z",
  source_sha256: "a".repeat(64),
  safety_state: "ready",
  hard_blockers: [],
  warnings: [],
  source_url: "/api/files/runs/run-1/attempt-1.html",
  preview_urls: ["/api/files/runs/run-1/attempt-1.png"],
} as const;

test("normalizes candidates and preserves the durable selection phase", () => {
  const state = normalizeRunAttemptState({
    run_id: "run-1",
    candidates: [candidate],
    selection: {
      candidate_id: "candidate-1",
      source_attempt: 1,
      state: "delivering",
    },
  });

  assert.equal(state.candidates.length, 1);
  assert.equal(state.selection_phase, "delivering");
  assert.equal(state.selection?.candidate_id, "candidate-1");
});

test("rejects malformed candidate URLs and hashes", () => {
  const state = normalizeRunAttemptState({
    run_id: "run-1",
    candidates: [
      { ...candidate, source_url: "file:///tmp/leak.html" },
      { ...candidate, source_sha256: "short" },
    ],
  });
  assert.deepEqual(state.candidates, []);
});

test("marks only the selected candidate as current", () => {
  assert.equal(
    candidateAction(candidate, "complete", true, "candidate-1"),
    "current",
  );
  assert.equal(
    candidateAction(
      { ...candidate, candidate_id: "candidate-2", attempt: 2 },
      "complete",
      true,
      "candidate-1",
    ),
    "open",
  );
});

test("blocked candidates open in Canvas instead of bypassing safety", () => {
  assert.equal(
    candidateAction(
      { ...candidate, safety_state: "blocked" },
      "idle",
      false,
    ),
    "fix",
  );
});

test("an attempt draft is editing only while its Canvas is open", () => {
  assert.equal(
    isAttemptDraftEditing("chat", "candidate-1", "candidate-1"),
    false,
  );
  assert.equal(
    isAttemptDraftEditing("canvas", "candidate-1", "candidate-1"),
    true,
  );
  assert.equal(
    isAttemptDraftEditing("canvas", "candidate-2", "candidate-1"),
    false,
  );
});
