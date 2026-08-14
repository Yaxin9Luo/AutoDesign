import { useMemo, useState } from "react";
import type { RefObject } from "react";
import { useApp } from "@/lib/store";
import type { Artifact, Layer } from "@/lib/types";
import { ResizeHandle } from "../ResizeHandle";
import {
  detectSlideFrames,
  layerIntersectsFrame,
  type SlideFrame,
} from "@/lib/slide_frames";
import { I } from "../icons";

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

interface Props {
  art: Artifact;
  // Kept for backward-compat with Canvas.tsx callsite. Single-slide
  // canvas no longer scrolls between slides — the store's active idx
  // does the switching now — so neither field is read in the body.
  scale?: number;
  scrollRef?: RefObject<HTMLDivElement>;
}

const VERTICAL_PADDING = 24;

export function LayerDeckNavBar(_props: Props) {
  const { art } = _props;
  const frames = useMemo(() => detectSlideFrames(art), [art]);
  const activeIdx = useApp((s) => s.active_slide_idx);
  const setActiveSlideIdx = useApp((s) => s.setActiveSlideIdx);
  const barHeight = useApp((s) => s.deck_navbar_height);
  const setDeckNavBarHeight = useApp((s) => s.setDeckNavBarHeight);
  const addSlideAfter = useApp((s) => s.addSlideAfter);
  const duplicateActiveSlide = useApp((s) => s.duplicateActiveSlide);
  const deleteActiveSlide = useApp((s) => s.deleteActiveSlide);
  const moveActiveSlide = useApp((s) => s.moveActiveSlide);
  const moveActiveSlideToIndex = useApp((s) => s.moveActiveSlideToIndex);
  const [draggingIdx, setDraggingIdx] = useState<number | null>(null);
  const [dragOverIdx, setDragOverIdx] = useState<number | null>(null);

  if (frames.length < 2) return null;

  const first = frames[0].bbox;
  const thumbH = Math.max(40, barHeight - VERTICAL_PADDING);
  const thumbW = Math.round(thumbH * (first.w / first.h));
  const thumbScale = thumbW / first.w;
  const safeActive = Math.min(activeIdx, frames.length - 1);

  const goTo = (frame: SlideFrame) => {
    setActiveSlideIdx(frame.idx);
  };

  return (
    <div
      className="relative flex shrink-0 items-center gap-2 overflow-x-auto border-b border-ink-300/60 bg-surface-raised px-4"
      style={{
        height: barHeight,
        paddingTop: VERTICAL_PADDING / 2,
        paddingBottom: VERTICAL_PADDING / 2,
      }}
    >
      <span
        className="shrink-0 self-center pr-2 text-[10px] font-medium uppercase text-ink-500"
        style={{ letterSpacing: "0.16em" }}
      >
        {frames.length} slides
      </span>
      <span
        className="mr-1 w-px shrink-0 self-center bg-ink-300"
        style={{ height: thumbH }}
      />
      <div className="flex shrink-0 items-center gap-1 self-center">
        <SlideToolButton title="Add slide after" onClick={addSlideAfter}>
          <I.Plus width={13} height={13} />
        </SlideToolButton>
        <SlideToolButton title="Duplicate slide" onClick={duplicateActiveSlide}>
          <I.Duplicate width={13} height={13} />
        </SlideToolButton>
        <SlideToolButton
          title="Move slide left"
          disabled={safeActive === 0}
          onClick={() => moveActiveSlide("up")}
        >
          <I.ChevronUp width={13} height={13} />
        </SlideToolButton>
        <SlideToolButton
          title="Move slide right"
          disabled={safeActive === frames.length - 1}
          onClick={() => moveActiveSlide("down")}
        >
          <I.ChevronDown width={13} height={13} />
        </SlideToolButton>
        <SlideToolButton
          title="Delete slide"
          disabled={frames.length <= 1}
          onClick={deleteActiveSlide}
        >
          <I.Trash width={13} height={13} />
        </SlideToolButton>
      </div>
      <span
        className="mx-1 w-px shrink-0 self-center bg-ink-300"
        style={{ height: thumbH }}
      />
      {frames.map((frame) => (
        <LayerSlideThumb
          key={frame.layer_id}
          art={art}
          frame={frame}
          active={safeActive === frame.idx}
          dragging={draggingIdx === frame.idx}
          dropTarget={dragOverIdx === frame.idx && draggingIdx !== frame.idx}
          width={thumbW}
          height={thumbH}
          scale={thumbScale}
          onClick={() => goTo(frame)}
          onDragStart={() => {
            setDraggingIdx(frame.idx);
            setActiveSlideIdx(frame.idx);
          }}
          onDragOver={(e) => {
            if (draggingIdx === null || draggingIdx === frame.idx) return;
            e.preventDefault();
            setDragOverIdx(frame.idx);
          }}
          onDrop={(e) => {
            e.preventDefault();
            if (draggingIdx === null || draggingIdx === frame.idx) return;
            setActiveSlideIdx(draggingIdx);
            moveActiveSlideToIndex(frame.idx);
            setDraggingIdx(null);
            setDragOverIdx(null);
          }}
          onDragEnd={() => {
            setDraggingIdx(null);
            setDragOverIdx(null);
          }}
        />
      ))}
      <ResizeHandle
        side="bottom"
        getCurrentSize={() => useApp.getState().deck_navbar_height}
        setSize={setDeckNavBarHeight}
      />
    </div>
  );
}

function SlideToolButton({
  title,
  disabled,
  onClick,
  children,
}: {
  title: string;
  disabled?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      title={title}
      disabled={disabled}
      onClick={onClick}
      className="icon-btn h-7 w-7 text-[10px] font-medium"
    >
      {children}
    </button>
  );
}


function LayerSlideThumb({
  art,
  frame,
  active,
  dragging,
  dropTarget,
  width,
  height,
  scale,
  onClick,
  onDragStart,
  onDragOver,
  onDrop,
  onDragEnd,
}: {
  art: Artifact;
  frame: SlideFrame;
  active: boolean;
  dragging: boolean;
  dropTarget: boolean;
  width: number;
  height: number;
  scale: number;
  onClick: () => void;
  onDragStart: () => void;
  onDragOver: (e: React.DragEvent<HTMLButtonElement>) => void;
  onDrop: (e: React.DragEvent<HTMLButtonElement>) => void;
  onDragEnd: () => void;
}) {
  const layers = art.layers
    .filter((l) => l.visible !== false && layerIntersectsFrame(l, frame))
    .sort((a, b) => a.z_index - b.z_index);

  return (
    <button
      draggable
      onClick={onClick}
      onDragStart={onDragStart}
      onDragOver={onDragOver}
      onDrop={onDrop}
      onDragEnd={onDragEnd}
      aria-label={`Slide ${frame.idx + 1}`}
      className={`group relative shrink-0 overflow-hidden rounded-md border bg-white transition ${
        active
          ? "border-accent shadow-sm ring-1 ring-accent/50"
          : "border-ink-300/70 hover:-translate-y-px hover:border-ink-700 hover:shadow-soft"
      } ${dragging ? "opacity-50" : ""} ${dropTarget ? "ring-2 ring-accent" : ""}`}
      style={{ width, height }}
      title={`Slide ${frame.idx + 1}`}
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
        className={`absolute left-1.5 top-1.5 rounded-sm px-1.5 py-0.5 font-mono text-[10px] tabular-nums ${
          active
            ? "bg-accent text-ink-50"
            : "bg-white/80 text-ink-700 backdrop-blur-sm group-hover:bg-white"
        }`}
      >
        {String(frame.idx + 1).padStart(2, "0")}
      </span>
    </button>
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
          letterSpacing: layer.letter_spacing
            ? `${layer.letter_spacing}px`
            : undefined,
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
