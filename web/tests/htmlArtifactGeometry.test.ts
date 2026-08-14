import assert from "node:assert/strict";
import test from "node:test";

import { resolveDeckViewportSize } from "../src/lib/html_artifact_geometry.ts";

test("uses the deck contract dimensions instead of the stacked artifact canvas", () => {
  assert.deepEqual(
    resolveDeckViewportSize(
      { w: 2000, h: 13920 },
      { w: 1920, h: 1080 },
      { w: 2000, h: 13920 },
    ),
    { w: 1920, h: 1080 },
  );
});

test("falls back to the rendered slide before the artifact canvas", () => {
  assert.deepEqual(
    resolveDeckViewportSize(
      { w: 2000, h: 13920 },
      { w: 0, h: 0 },
      { w: 1920, h: 1080 },
    ),
    { w: 1920, h: 1080 },
  );
});

test("uses the artifact canvas only when no complete slide size exists", () => {
  assert.deepEqual(
    resolveDeckViewportSize(
      { w: 1920, h: 1080 },
      { w: 1920, h: 0 },
      { w: 0, h: 1080 },
    ),
    { w: 1920, h: 1080 },
  );
});
