import assert from "node:assert/strict";
import test from "node:test";

import {
  canonicalDeckFrameIds,
  deckAccessibilityState,
  deckIndexFromHash,
  deckIndexForKey,
  deckPresentationMode,
  deckProgress,
} from "../src/lib/deck_navigation.ts";

const frameIds = ["slide_01", "slide_02", "slide_03"];

test("restores a slide index from an encoded frame hash", () => {
  assert.equal(deckIndexFromHash("#slide_02", frameIds), 1);
  assert.equal(deckIndexFromHash("#missing", frameIds), -1);
  assert.equal(deckIndexFromHash("#%E0%A4%A", frameIds), -1);
});

test("maps presenter keys to bounded slide indexes", () => {
  assert.equal(deckIndexForKey("ArrowRight", 0, 3), 1);
  assert.equal(deckIndexForKey("PageDown", 2, 3), 2);
  assert.equal(deckIndexForKey("ArrowLeft", 0, 3), 0);
  assert.equal(deckIndexForKey("Home", 2, 3), 0);
  assert.equal(deckIndexForKey("End", 0, 3), 2);
  assert.equal(deckIndexForKey("Escape", 1, 3), null);
});

test("derives trusted UI progress from the active slide", () => {
  const progress = deckProgress(1, 3);

  assert.equal(progress.current, 2);
  assert.equal(progress.total, 3);
  assert.equal(progress.label, "2 / 3");
  assert.ok(Math.abs(progress.percent - (200 / 3)) < Number.EPSILON * 100);
});

test("derives stable canonical slide identities with declared precedence", () => {
  assert.deepEqual(
    canonicalDeckFrameIds([
      { dataFrameId: " frame-a ", id: "ignored", dataSlideIndex: "9" },
      { id: "authored-id", dataSlideIndex: "8" },
      { dataSlideIndex: "7" },
      {},
      { id: "authored-id" },
      { dataFrameId: "" },
      { id: "authored-id__2" },
    ]),
    [
      "frame-a",
      "authored-id",
      "7",
      "slide_4",
      "authored-id__2",
      "slide_6",
      "authored-id__2__2",
    ],
  );
});

test("classifies stacked and script-driven player decks from one scan", () => {
  const sharedOffsetParent = {};
  const painted = {
    display: "block",
    visibility: "visible",
    opacity: 1,
    width: 1280,
    height: 720,
    position: "static",
    offsetTop: 0,
    offsetLeft: 0,
    offsetParent: sharedOffsetParent,
  };
  const hiddenVariants = [
    { ...painted, display: "none" },
    { ...painted, visibility: "hidden" },
    { ...painted, opacity: 0 },
    { ...painted, width: 0 },
  ];

  assert.equal(deckPresentationMode([painted, painted, painted]), "stacked");
  for (const hidden of hiddenVariants) {
    assert.equal(deckPresentationMode([painted, hidden, hidden]), "player");
  }

  const transformedOverlay = {
    ...painted,
    position: "absolute",
  };
  assert.equal(
    deckPresentationMode([
      transformedOverlay,
      { ...transformedOverlay, transform: "matrix(1, 0, 0, 1, -3840, 0)" },
      { ...transformedOverlay, transform: "matrix(1, 0, 0, 1, -3840, 0)" },
    ]),
    "player",
  );

  assert.equal(
    deckPresentationMode([
      transformedOverlay,
      { ...transformedOverlay, offsetTop: 720 },
      { ...transformedOverlay, offsetTop: 1440 },
    ]),
    "stacked",
  );

  const fixedOverlay = {
    ...transformedOverlay,
    position: "fixed",
    offsetParent: null,
  };
  assert.equal(
    deckPresentationMode([fixedOverlay, fixedOverlay, fixedOverlay]),
    "player",
  );

  assert.equal(
    deckPresentationMode([
      transformedOverlay,
      { ...transformedOverlay, offsetParent: {} },
      { ...transformedOverlay, offsetParent: {} },
    ]),
    "stacked",
  );
});

test("plans accessible player state without falsely hiding stacked slides", () => {
  assert.deepEqual(deckAccessibilityState("player", 1, 3), [
    { ariaCurrent: null, ariaHidden: "true" },
    { ariaCurrent: "page", ariaHidden: "false" },
    { ariaCurrent: null, ariaHidden: "true" },
  ]);
  assert.deepEqual(deckAccessibilityState("stacked", 1, 3), [
    { ariaCurrent: null, ariaHidden: null },
    { ariaCurrent: "page", ariaHidden: null },
    { ariaCurrent: null, ariaHidden: null },
  ]);
});
