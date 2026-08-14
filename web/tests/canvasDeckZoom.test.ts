import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  CANVAS_LAYER_ORDER,
  CANVAS_MAX_ZOOM,
  CANVAS_ZOOM_PRESETS,
  clampCanvasZoomPercent,
  supportsCanvasZoom,
} from "../src/lib/canvas_zoom.ts";

test("HTML decks use the scalable canvas path through 300 percent", () => {
  assert.equal(supportsCanvasZoom("deck", "html"), true);
  assert.equal(supportsCanvasZoom("poster", "html"), true);
  assert.equal(supportsCanvasZoom("landing", "html"), false);
  assert.equal(supportsCanvasZoom("deck", "pptx"), false);
  assert.equal(CANVAS_MAX_ZOOM, 3);
  assert.ok(CANVAS_ZOOM_PRESETS.includes(50));
  assert.ok(CANVAS_ZOOM_PRESETS.includes(100));
  assert.ok(CANVAS_ZOOM_PRESETS.includes(300));
  assert.equal(clampCanvasZoomPercent(500), 300);
  assert.equal(clampCanvasZoomPercent(49.6), 50);
});

test("canvas toolbar and zoom menu stay above Deck navigation", () => {
  assert.ok(CANVAS_LAYER_ORDER.toolbar > CANVAS_LAYER_ORDER.deckNavigation);
  assert.ok(CANVAS_LAYER_ORDER.menu > CANVAS_LAYER_ORDER.toolbar);
});

test("HTML decks use the unified geometry-aware canvas viewer", async () => {
  const canvasSource = await readFile(
    fileURLToPath(new URL("../src/components/Canvas.tsx", import.meta.url)),
    "utf8",
  );

  assert.match(canvasSource, /\{isZoomableHtml \? \(/);
  assert.match(canvasSource, /resolveDeckViewportSize\(/);
  assert.match(canvasSource, /artType !== "poster"/);
  assert.doesNotMatch(canvasSource, /ZoomableDeckArtifactView/);
});

test("canvas toolbar prioritizes an explicit save action over static status badges", async () => {
  const canvasSource = await readFile(
    fileURLToPath(new URL("../src/components/Canvas.tsx", import.meta.url)),
    "utf8",
  );
  const manualSaveStart = canvasSource.indexOf("function ManualSaveControl");
  const manualSaveEnd = canvasSource.indexOf("function formatTime", manualSaveStart);
  const titleClusterStart = canvasSource.indexOf("const titleCluster =");
  const zoomClusterStart = canvasSource.indexOf("const zoomCluster =", titleClusterStart);

  assert.ok(manualSaveStart >= 0);
  assert.ok(manualSaveEnd > manualSaveStart);
  assert.ok(titleClusterStart >= 0);
  assert.ok(zoomClusterStart > titleClusterStart);

  const manualSaveSource = canvasSource.slice(manualSaveStart, manualSaveEnd);
  const titleClusterSource = canvasSource.slice(titleClusterStart, zoomClusterStart);

  assert.match(manualSaveSource, /t\("Save changes \(⌘S\)"\)/);
  assert.match(manualSaveSource, /<span>\{label\}<\/span>/);
  assert.match(manualSaveSource, /inline-flex h-7 items-center gap-1\.5 rounded-md border px-2\.5/);
  assert.match(titleClusterSource, /<ManualSaveControl hasUnsaved=\{hasUnsaved\} \/>/);
  assert.doesNotMatch(titleClusterSource, /native_format/);
  assert.doesNotMatch(titleClusterSource, /CanvasValidationPill/);
});
