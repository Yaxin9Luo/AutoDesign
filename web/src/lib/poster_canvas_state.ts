import type {
  Message,
  PosterCanvasPreset,
  PosterCanvasPresetCatalog,
} from "./types";

const EXPECTED_PRESETS = [
  ["auto", "Auto · Prompt first", null, null, null],
  ["cvpr-landscape", "2:1 Wide", "cvpr-landscape", 3072, 1536],
  ["academic-landscape-5x3", "5:3 Landscape", "academic-landscape-5x3", 2560, 1536],
  ["academic-landscape-1.4", "1.4:1 Landscape", "academic-landscape-1.4", 2150, 1536],
  ["poster-classic-4x3", "4:3 Landscape", "poster-classic-4x3", 2048, 1536],
  ["neurips-portrait", "3:4 Portrait", "neurips-portrait", 1536, 2048],
] as const;

const catalogError = (): Error => new Error(
  "Poster canvas preset catalog is missing or inconsistent.",
);

export function canSubmitPosterCanvasSelection(
  status: "idle" | "loading" | "ready" | "error",
  presets: PosterCanvasPreset[],
  presetId: string,
): boolean {
  return presetId === "auto"
    || (status === "ready" && presets.some((preset) => preset.id === presetId));
}

export function validatePosterCanvasCatalog(
  raw: unknown,
): PosterCanvasPreset[] {
  if (!raw || typeof raw !== "object") throw catalogError();
  const catalog = raw as Partial<PosterCanvasPresetCatalog>;
  if (
    catalog.version !== 1
    || catalog.kind !== "poster_canvas_presets"
    || catalog.default_preset_id !== "cvpr-landscape"
    || !Array.isArray(catalog.presets)
    || catalog.presets.length !== EXPECTED_PRESETS.length
  ) {
    throw catalogError();
  }
  EXPECTED_PRESETS.forEach(([id, label, template, width, height], index) => {
    const preset = catalog.presets![index];
    if (
      !preset
      || preset.id !== id
      || preset.label !== label
      || preset.template !== template
    ) {
      throw catalogError();
    }
    if (width === null || height === null) {
      if (preset.canvas !== null) throw catalogError();
      return;
    }
    if (
      !preset.canvas
      || preset.canvas.w_px !== width
      || preset.canvas.h_px !== height
      || !Number.isFinite(preset.canvas.dpi)
      || typeof preset.canvas.aspect_ratio !== "string"
      || preset.canvas.color_mode !== "RGB"
    ) {
      throw catalogError();
    }
  });
  return catalog.presets;
}

export function restoredPosterCanvasPresetId(
  messages: Message[],
  stored: unknown,
): string {
  if (stored === null) return "auto";
  if (typeof stored === "string" && stored.trim()) return stored.trim();
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const payload = messages[index]?.task_payload;
    if (payload?.artifact_type !== "poster") continue;
    if (payload.canvas_preset_id === null) return "auto";
    if (typeof payload.canvas_preset_id === "string" && payload.canvas_preset_id.trim()) {
      return payload.canvas_preset_id.trim();
    }
  }
  return "auto";
}

export function templateForCanvasPreset(
  presets: PosterCanvasPreset[],
  presetId: string,
): string | undefined {
  const preset = presets.find((candidate) => candidate.id === presetId);
  if (!preset) throw new Error(`Unknown Poster canvas preset: ${presetId}`);
  return preset.template ?? undefined;
}

export function posterCanvasRequestSelection(
  presets: PosterCanvasPreset[],
  presetId: string | null | undefined,
  persistedTemplate?: string,
): { canvas_preset_id: string; template: string | undefined } {
  const canvas_preset_id = presetId?.trim() || "auto";
  if (canvas_preset_id === "auto") {
    return { canvas_preset_id, template: undefined };
  }
  const preset = presets.find((candidate) => candidate.id === canvas_preset_id);
  return {
    canvas_preset_id,
    template: preset
      ? preset.template ?? undefined
      : persistedTemplate ?? templateForCanvasPreset(presets, canvas_preset_id),
  };
}

export type CanvasPickerKeyAction =
  | { kind: "focus"; index: number }
  | { kind: "select"; index: number }
  | { kind: "close" }
  | { kind: "none" };

export function canvasPickerKeyAction(
  key: string,
  index: number,
  count: number,
): CanvasPickerKeyAction {
  if (key === "Escape") return { kind: "close" };
  if (key === "Enter" || key === " ") return { kind: "select", index };
  if (count <= 0) return { kind: "none" };
  if (key === "Home") return { kind: "focus", index: 0 };
  if (key === "End") return { kind: "focus", index: count - 1 };
  if (key === "ArrowDown" || key === "ArrowRight") {
    return { kind: "focus", index: (index + 1) % count };
  }
  if (key === "ArrowUp" || key === "ArrowLeft") {
    return { kind: "focus", index: (index - 1 + count) % count };
  }
  return { kind: "none" };
}

const CANVAS_VALIDATION_CODES = new Set([
  "canvas_preset_mismatch",
  "canvas_preset_not_supported_for_artifact",
  "conflicting_canvas_directives",
  "invalid_canvas_ratio",
  "unknown_canvas_preset",
  "unknown_canvas_template",
]);

export function isCanvasValidationError(error: unknown): error is Error & {
  status: 422;
  code: string;
} {
  if (!(error instanceof Error)) return false;
  const candidate = error as Error & { status?: unknown; code?: unknown };
  return candidate.status === 422
    && typeof candidate.code === "string"
    && CANVAS_VALIDATION_CODES.has(candidate.code);
}
