/**
 * Slide-frame detection for the layer-mode (editable) deck artifact.
 *
 * "Slide frames" are the rectangular shape/background layers that
 * constitute each slide's bounding box — typically large rectangles
 * with ~16:9 aspect, sorted top-to-bottom. We detect them heuristically
 * from the layer manifest so neither the agent nor the editor has to
 * carry an explicit `slide_index` on every layer.
 *
 * Shared between LayerDeckNavBar (renders thumbnails) and Canvas
 * (single-slide rendering — only show layers belonging to the active
 * slide).
 */

import type { Artifact, Layer } from "./types";

export interface SlideFrame {
  idx: number;
  layer_id: string;
  bbox: { x: number; y: number; w: number; h: number };
}

export function detectSlideFrames(art: Artifact): SlideFrame[] {
  if ((art.artifact_type !== "deck" && art.artifact_type !== "video") || art.native_file_url) {
    return [];
  }
  const layers = Array.isArray(art.layers) ? art.layers : [];

  if (art.artifact_type === "video" && art.video_project?.scenes?.length) {
    const byId = new Map(layers.map((l) => [l.layer_id, l]));
    return art.video_project.scenes
      .map((scene, idx) => {
        const layer = byId.get(scene.frame_layer_id);
        if (!layer?.bbox) return null;
        return {
          idx,
          layer_id: layer.layer_id,
          bbox: layer.bbox,
        } satisfies SlideFrame;
      })
      .filter((frame): frame is SlideFrame => frame !== null);
  }

  const minW = art.canvas.w * 0.65;
  const maxW = art.canvas.w;
  return layers
    .filter((l) => {
      const b = l.bbox;
      if (!b || b.w <= 0 || b.h <= 0) return false;
      if (l.kind !== "shape" && l.kind !== "background") return false;
      if (b.w < minW || b.w > maxW) return false;
      if (b.h > art.canvas.h * 0.5) return false;
      const aspect = b.w / b.h;
      return aspect >= 1.45 && aspect <= 1.95;
    })
    .sort((a, b) => (a.bbox!.y - b.bbox!.y) || (a.z_index - b.z_index))
    .map((l, idx) => ({
      idx,
      layer_id: l.layer_id,
      bbox: l.bbox!,
    }));
}

export function layerIntersectsFrame(layer: Layer, frame: SlideFrame): boolean {
  const b = layer.bbox;
  if (!b) return false;
  // Kill the page-spanning background layer leaking into a slide thumb.
  if (layer.kind === "background" && b.w > frame.bbox.w * 1.1) return false;
  return (
    b.x < frame.bbox.x + frame.bbox.w &&
    b.x + b.w > frame.bbox.x &&
    b.y < frame.bbox.y + frame.bbox.h &&
    b.y + b.h > frame.bbox.y
  );
}
