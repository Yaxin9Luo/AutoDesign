import assert from "node:assert/strict";
import test from "node:test";

import { restoredPosterPaletteId } from "../src/lib/poster_palette_state.ts";
import type { Message } from "../src/lib/types.ts";

const historicalPosterMessage: Message = {
  id: "msg_poster",
  role: "user",
  text: "Create a poster",
  ts: 1,
  task_payload: {
    artifact_type: "poster",
    palette_id: "retired_palette",
  },
};

test("explicit palette invalidation does not restore a historical task palette", () => {
  assert.equal(
    restoredPosterPaletteId([historicalPosterMessage], null),
    null,
  );
  assert.equal(
    restoredPosterPaletteId([historicalPosterMessage], undefined),
    "retired_palette",
  );
});
