import assert from "node:assert/strict";
import test from "node:test";

import {
  canSubmitPosterCanvasSelection,
  canvasPickerKeyAction,
  posterCanvasRequestSelection,
  restoredPosterCanvasPresetId,
  validatePosterCanvasCatalog,
} from "../src/lib/poster_canvas_state.ts";
import { translate } from "../src/lib/i18n.ts";
import type {
  Message,
  PosterCanvasPresetCatalog,
} from "../src/lib/types.ts";

const catalog: PosterCanvasPresetCatalog = {
  version: 1,
  kind: "poster_canvas_presets",
  default_preset_id: "cvpr-landscape",
  presets: [
    { id: "auto", label: "Auto · Prompt first", template: null, canvas: null },
    {
      id: "cvpr-landscape",
      label: "2:1 Wide",
      template: "cvpr-landscape",
      canvas: { w_px: 3072, h_px: 1536, dpi: 150, aspect_ratio: "2:1", color_mode: "RGB" },
    },
    {
      id: "academic-landscape-5x3",
      label: "5:3 Landscape",
      template: "academic-landscape-5x3",
      canvas: { w_px: 2560, h_px: 1536, dpi: 150, aspect_ratio: "5:3", color_mode: "RGB" },
    },
    {
      id: "academic-landscape-1.4",
      label: "1.4:1 Landscape",
      template: "academic-landscape-1.4",
      canvas: { w_px: 2150, h_px: 1536, dpi: 150, aspect_ratio: "1.4:1", color_mode: "RGB" },
    },
    {
      id: "poster-classic-4x3",
      label: "4:3 Landscape",
      template: "poster-classic-4x3",
      canvas: { w_px: 2048, h_px: 1536, dpi: 300, aspect_ratio: "4:3", color_mode: "RGB" },
    },
    {
      id: "neurips-portrait",
      label: "3:4 Portrait",
      template: "neurips-portrait",
      canvas: { w_px: 1536, h_px: 2048, dpi: 300, aspect_ratio: "3:4", color_mode: "RGB" },
    },
  ],
};

test("accepts only the complete ordered server-backed Poster canvas catalog", () => {
  const validated = validatePosterCanvasCatalog(catalog);
  assert.deepEqual(
    validated.map((preset) => [
      preset.id,
      preset.label,
      preset.template,
      preset.canvas?.w_px ?? null,
      preset.canvas?.h_px ?? null,
    ]),
    [
      ["auto", "Auto · Prompt first", null, null, null],
      ["cvpr-landscape", "2:1 Wide", "cvpr-landscape", 3072, 1536],
      ["academic-landscape-5x3", "5:3 Landscape", "academic-landscape-5x3", 2560, 1536],
      ["academic-landscape-1.4", "1.4:1 Landscape", "academic-landscape-1.4", 2150, 1536],
      ["poster-classic-4x3", "4:3 Landscape", "poster-classic-4x3", 2048, 1536],
      ["neurips-portrait", "3:4 Portrait", "neurips-portrait", 1536, 2048],
    ],
  );

  const inconsistent = structuredClone(catalog);
  inconsistent.presets[3]!.canvas!.w_px = 2149;
  assert.throws(
    () => validatePosterCanvasCatalog(inconsistent),
    /canvas preset catalog/i,
  );
});

test("restores explicit Auto instead of a stale historical canvas preset", () => {
  const historical: Message = {
    id: "msg_old",
    role: "user",
    text: "Create a poster",
    ts: 1,
    task_payload: {
      artifact_type: "poster",
      template: "poster-classic-4x3",
      canvas_preset_id: "poster-classic-4x3",
    },
  };
  assert.equal(restoredPosterCanvasPresetId([historical], null), "auto");
  assert.equal(restoredPosterCanvasPresetId([historical], "auto"), "auto");
  assert.equal(
    restoredPosterCanvasPresetId([historical], undefined),
    "poster-classic-4x3",
  );
});

test("keeps Auto usable while explicit canvas presets require a ready catalog", () => {
  assert.equal(canSubmitPosterCanvasSelection("idle", [], "auto"), true);
  assert.equal(canSubmitPosterCanvasSelection("loading", [], "auto"), true);
  assert.equal(canSubmitPosterCanvasSelection("error", [], "auto"), true);
  assert.equal(
    canSubmitPosterCanvasSelection("loading", catalog.presets, "poster-classic-4x3"),
    false,
  );
  assert.equal(
    canSubmitPosterCanvasSelection("ready", catalog.presets, "poster-classic-4x3"),
    true,
  );
  assert.equal(canSubmitPosterCanvasSelection("ready", catalog.presets, "missing"), false);
});

test("resolves one request snapshot for ordinary, bundle, and resume paths", () => {
  assert.deepEqual(
    posterCanvasRequestSelection([], "auto", "cvpr-landscape"),
    { canvas_preset_id: "auto", template: undefined },
  );
  assert.deepEqual(
    posterCanvasRequestSelection(catalog.presets, "poster-classic-4x3"),
    {
      canvas_preset_id: "poster-classic-4x3",
      template: "poster-classic-4x3",
    },
  );
  assert.deepEqual(
    posterCanvasRequestSelection(
      catalog.presets,
      "poster-classic-4x3",
      "cvpr-landscape",
    ),
    {
      canvas_preset_id: "poster-classic-4x3",
      template: "poster-classic-4x3",
    },
  );
  assert.deepEqual(
    posterCanvasRequestSelection([], "poster-classic-4x3", "poster-classic-4x3"),
    {
      canvas_preset_id: "poster-classic-4x3",
      template: "poster-classic-4x3",
    },
  );
});

test("maps listbox keyboard input to wrapping, boundary, select, and close actions", () => {
  assert.deepEqual(canvasPickerKeyAction("ArrowDown", 5, 6), { kind: "focus", index: 0 });
  assert.deepEqual(canvasPickerKeyAction("ArrowUp", 0, 6), { kind: "focus", index: 5 });
  assert.deepEqual(canvasPickerKeyAction("Home", 4, 6), { kind: "focus", index: 0 });
  assert.deepEqual(canvasPickerKeyAction("End", 1, 6), { kind: "focus", index: 5 });
  assert.deepEqual(canvasPickerKeyAction("Enter", 3, 6), { kind: "select", index: 3 });
  assert.deepEqual(canvasPickerKeyAction("Escape", 3, 6), { kind: "close" });
});

test("provides canvas picker labels in English, Chinese, and Korean", () => {
  assert.equal(translate("en", "Canvas"), "Canvas");
  assert.equal(translate("zh", "Canvas"), "画布");
  assert.equal(translate("ko", "Canvas"), "캔버스");
  assert.equal(translate("zh", "Auto · Prompt first"), "自动 · 优先遵循提示词");
  assert.equal(translate("ko", "3:4 Portrait"), "3:4 세로형");
});
