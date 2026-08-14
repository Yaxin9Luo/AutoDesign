import type { ArtifactType } from "./types";

export const CANVAS_MIN_ZOOM = 0.02;
export const CANVAS_MAX_ZOOM = 3;
export const CANVAS_LAYER_ORDER = Object.freeze({
  deckNavigation: 20,
  toolbar: 30,
  menu: 40,
});
export const CANVAS_ZOOM_PRESETS = Object.freeze([
  25,
  50,
  75,
  100,
  125,
  150,
  200,
  300,
]);

export function clampCanvasZoomPercent(percent: number): number {
  return Math.max(
    CANVAS_MIN_ZOOM * 100,
    Math.min(CANVAS_MAX_ZOOM * 100, Math.round(percent)),
  );
}

export function supportsCanvasZoom(
  artifactType: ArtifactType | null,
  viewFormat?: string | null,
): boolean {
  return viewFormat === "html"
    && (artifactType === "poster" || artifactType === "deck");
}
