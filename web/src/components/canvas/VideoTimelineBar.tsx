import { useMemo } from "react";
import { useApp } from "@/lib/store";
import type { Artifact, Layer } from "@/lib/types";
import {
  detectSlideFrames,
  layerIntersectsFrame,
  type SlideFrame,
} from "@/lib/slide_frames";
import { I } from "../icons";
import {
  VIDEO_SCENE_DURATION_MAX_S,
  VIDEO_SCENE_DURATION_MIN_S,
} from "@/lib/presets";

const clamp01 = (n: number) => Math.max(0, Math.min(1, n));

function rgbaFromHex(hex: string, opacity: number) {
  const clean = hex.trim().replace("#", "");
  if (!/^[0-9a-f]{6}$/i.test(clean)) return `rgba(23, 19, 15, ${opacity})`;
  const n = parseInt(clean, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${opacity})`;
}

function layerShadowCss(layer: Layer) {
  const s = layer.shadow;
  return s ? `${s.dx}px ${s.dy}px ${s.blur}px ${rgbaFromHex(s.color, clamp01(s.opacity))}` : undefined;
}

function objectPositionCss(layer: Layer) {
  const pos = layer.object_position ?? { x: 0.5, y: 0.5 };
  return `${Math.round(clamp01(pos.x) * 100)}% ${Math.round(clamp01(pos.y) * 100)}%`;
}

export function VideoTimelineBar({ art }: { art: Artifact }) {
  const frames = useMemo(() => detectSlideFrames(art), [art]);
  const activeIdx = useApp((s) => s.active_slide_idx);
  const setActiveSlideIdx = useApp((s) => s.setActiveSlideIdx);
  const updateSceneDuration = useApp((s) => s.updateVideoSceneDuration);
  const renderActiveVideo = useApp((s) => s.renderActiveVideo);
  const currentConversationId = useApp((s) => s.current_conversation_id);
  const progress = useApp((s) =>
    currentConversationId ? s.runs_progress[currentConversationId] : undefined
  );
  const project = art.video_project;
  if (!project || frames.length < 1 || project.scenes.length < 1) return null;

  const scenes = project.scenes;
  const safeActive = Math.min(activeIdx, scenes.length - 1, frames.length - 1);
  const activeScene = scenes[safeActive];
  const totalDuration = scenes.reduce((sum, scene) => sum + scene.duration_s, 0);
  const rendering = progress?.phase === "queued"
    || progress?.phase === "running"
    || progress?.phase === "cancelling";
  const latest = project.latest_render;

  return (
    <div className="flex h-[168px] shrink-0 items-stretch gap-3 border-t border-ink-300/60 bg-surface-raised px-4 py-3">
      <div className="flex w-[170px] shrink-0 flex-col justify-between border-r border-ink-300/60 pr-3">
        <div>
          <div
            className="text-[10px] font-medium uppercase text-ink-500"
            style={{ letterSpacing: "0.16em" }}
          >
            Video scenes
          </div>
          <div className="mt-1 font-display text-[18px] leading-none text-ink-900" style={{ fontVariationSettings: '"opsz" 36' }}>
            {formatSeconds(totalDuration)}
          </div>
          <div className="mt-1 text-[11px] text-ink-500">
            {scenes.length} scenes · {project.fps} fps
          </div>
        </div>
        {activeScene && (
          <label className="block">
            <span className="mb-1 block text-[10px] uppercase text-ink-500" style={{ letterSpacing: "0.12em" }}>
              Active duration
            </span>
            <div className="flex items-center gap-1.5">
              <input
                type="number"
                min={VIDEO_SCENE_DURATION_MIN_S}
                max={VIDEO_SCENE_DURATION_MAX_S}
                step={0.1}
                value={activeScene.duration_s}
                onChange={(e) => updateSceneDuration(activeScene.scene_id, Number(e.target.value))}
                className="field-input h-7 w-20 px-2 text-[12px]"
              />
              <span className="text-[11px] text-ink-500">sec</span>
            </div>
          </label>
        )}
      </div>

      <div className="flex min-w-0 flex-1 gap-2 overflow-x-auto pb-1">
        {frames.map((frame, idx) => {
          const scene = scenes[idx];
          const active = idx === safeActive;
          return (
            <button
              key={frame.layer_id}
              type="button"
              onClick={() => setActiveSlideIdx(idx)}
              className={`group flex w-[148px] shrink-0 flex-col rounded-md border bg-paper p-1.5 text-left transition ${
                active
                  ? "border-accent shadow-sm ring-1 ring-accent/50"
                  : "border-ink-300/70 hover:-translate-y-px hover:border-ink-700 hover:shadow-soft"
              }`}
              title={scene?.name ?? `Scene ${idx + 1}`}
            >
              <FrameThumbnail art={art} frame={frame} active={active} />
              <div className="mt-1.5 flex min-w-0 items-center justify-between gap-2">
                <span className="min-w-0 truncate text-[11px] font-medium text-ink-800">
                  {scene?.name ?? `Scene ${idx + 1}`}
                </span>
                <span className="shrink-0 tabular text-[10px] text-ink-500">
                  {formatSeconds(scene?.duration_s ?? 0)}
                </span>
              </div>
            </button>
          );
        })}
      </div>

      <div className="flex w-[260px] shrink-0 flex-col justify-between border-l border-ink-300/60 pl-3">
        <button
          type="button"
          onClick={() => {
            void renderActiveVideo();
          }}
          disabled={rendering}
          className="inline-flex h-9 items-center justify-center gap-2 rounded-md bg-ink-900 px-3 text-[11px] font-medium uppercase text-ink-50 transition hover:bg-ink-700 disabled:cursor-wait disabled:opacity-55"
          style={{ letterSpacing: "0.13em" }}
        >
          <I.Video width={14} height={14} />
          {rendering ? "Rendering" : "Render MP4"}
        </button>
        <div className="min-h-[48px]">
          {rendering && (
            <div className="rounded-md border border-accent/30 bg-accent-soft px-2.5 py-2 text-[11px] text-accent-deep">
              {progress?.label ?? "Rendering MP4..."}
            </div>
          )}
          {!rendering && latest?.error && (
            <div className="line-clamp-3 rounded-md border border-red-200 bg-red-50 px-2.5 py-2 text-[11px] leading-snug text-red-700">
              {latest.error}
            </div>
          )}
          {!rendering && latest?.mp4_url && (
            <div className="flex items-center gap-2">
              <video
                src={latest.mp4_url}
                controls
                className="h-[58px] w-[104px] shrink-0 rounded-md border border-ink-300/70 bg-black object-cover"
              >
                {latest.subtitle_url && (
                  <track kind="subtitles" srcLang="en" label="English" src={latest.subtitle_url} />
                )}
              </video>
              <div className="min-w-0 text-[11px] leading-snug">
                <div className="truncate text-ink-700">
                  Latest render
                </div>
                <a
                  href={latest.mp4_url}
                  download
                  className="inline-flex items-center gap-1 text-accent-deep underline-offset-2 hover:underline"
                >
                  Download MP4
                  <I.ArrowRight width={11} height={11} />
                </a>
              </div>
            </div>
          )}
          {!rendering && !latest && (
            <div className="rounded-md border border-ink-300/70 bg-vellum px-2.5 py-2 text-[11px] leading-snug text-ink-500">
              No render yet.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function FrameThumbnail({
  art,
  frame,
  active,
}: {
  art: Artifact;
  frame: SlideFrame;
  active: boolean;
}) {
  const width = 136;
  const height = Math.round(width * (frame.bbox.h / frame.bbox.w));
  const scale = width / frame.bbox.w;
  const layers = art.layers
    .filter((l) => l.visible !== false && layerIntersectsFrame(l, frame))
    .sort((a, b) => a.z_index - b.z_index);

  return (
    <div
      className={`relative overflow-hidden rounded border ${active ? "border-accent/40" : "border-ink-300/70"}`}
      style={{ width, height, background: art.canvas.background ?? "#fff" }}
    >
      <div
        className="absolute left-0 top-0 origin-top-left overflow-hidden"
        style={{
          width: frame.bbox.w,
          height: frame.bbox.h,
          transform: `scale(${scale})`,
          background: art.canvas.background ?? "#fff",
        }}
      >
        {layers.map((layer) => (
          <ThumbLayer key={layer.layer_id} layer={layer} frame={frame} />
        ))}
      </div>
      <span
        className={`absolute left-1 top-1 rounded-sm px-1.5 py-0.5 font-mono text-[9px] tabular-nums ${
          active ? "bg-accent text-ink-50" : "bg-white/85 text-ink-700"
        }`}
      >
        {String(frame.idx + 1).padStart(2, "0")}
      </span>
    </div>
  );
}

function ThumbLayer({ layer, frame }: { layer: Layer; frame: SlideFrame }) {
  if (!layer.bbox) return null;
  const style: React.CSSProperties = {
    position: "absolute",
    left: layer.bbox.x - frame.bbox.x,
    top: layer.bbox.y - frame.bbox.y,
    width: layer.bbox.w,
    height: layer.bbox.h,
  };

  if (layer.kind === "text") {
    return (
      <div
        style={{
          ...style,
          fontFamily: layer.font_family,
          fontSize: layer.font_size_px,
          fontWeight: layer.font_weight,
          fontStyle: layer.font_style ?? "normal",
          lineHeight: layer.line_height,
          letterSpacing: layer.letter_spacing ? `${layer.letter_spacing}px` : undefined,
          textAlign: layer.align,
          textTransform: layer.text_transform === "uppercase" ? "uppercase" : "none",
          color: layer.effects?.fill,
          whiteSpace: "pre-wrap",
        }}
      >
        {layer.text}
      </div>
    );
  }

  if (layer.kind === "shape" || layer.kind === "background") {
    return (
      <div
        style={{
          ...style,
          background: layer.fill_color,
          borderRadius: layer.shape_kind === "ellipse" ? "9999px" : (layer.corner_radius ?? 0),
          opacity: layer.opacity ?? 1,
          boxShadow: layerShadowCss(layer),
          border:
            layer.stroke_color && (layer.stroke_width ?? 0) > 0
              ? `${layer.stroke_width}px ${layer.stroke_dash ?? "solid"} ${layer.stroke_color}`
              : undefined,
        }}
      />
    );
  }

  if (layer.kind === "image") {
    return (
      <img
        src={layer.src}
        alt={layer.name}
        style={{
          ...style,
          objectFit: layer.fit ?? "cover",
          objectPosition: objectPositionCss(layer),
          borderRadius: layer.corner_radius ?? 0,
          opacity: layer.opacity ?? 1,
          boxShadow: layerShadowCss(layer),
        }}
      />
    );
  }

  return null;
}

function formatSeconds(sec: number) {
  return `${Number.isFinite(sec) ? sec.toFixed(sec % 1 === 0 ? 0 : 1) : "0"}s`;
}
