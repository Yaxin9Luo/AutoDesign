import assert from "node:assert/strict";
import test from "node:test";

import { formatElapsedSince } from "../src/lib/elapsed.ts";
import { applyEvent, initialProgress } from "../src/lib/progress.ts";

for (const event of [
  "designer_author.attempt_start",
  "slides_author.attempt_start",
  "landing_author.attempt_start",
  "video_author.attempt_start",
]) {
  test(`tracks ${event} for Paper All-in-One progress`, () => {
    const progress = applyEvent(initialProgress("run"), {
      event,
      attempt: 2,
      max_attempts: 12,
    });

    assert.equal(progress.counts.attempts, 2);
    assert.equal(progress.counts.max_attempts, 12);
  });
}

test("attempt progress never moves backwards on replayed events", () => {
  const latest = applyEvent(initialProgress("run"), {
    event: "video_author.attempt_start",
    attempt: 3,
    max_attempts: 12,
  });
  const replayed = applyEvent(latest, {
    event: "video_author.attempt_start",
    attempt: 2,
    max_attempts: 10,
  });

  assert.equal(replayed.counts.attempts, 3);
  assert.equal(replayed.counts.max_attempts, 12);
});

test("formats elapsed bundle time across the minute boundary", () => {
  assert.equal(formatElapsedSince(10_000, 15_000), "5s");
  assert.equal(formatElapsedSince(10_000, 75_000), "1m 05s");
  assert.equal(formatElapsedSince(15_000, 10_000), "0s");
});
