import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { RefObject } from "react";
import {
  artifactTypeForArtifact,
  hasPendingEditsPayload,
  useActiveArtifact,
  useApp,
  type PendingInsert,
} from "@/lib/store";
import type { Artifact, ArtifactAsset, Bbox, Layer } from "@/lib/types";
import {
  openResearchNeedsPaperId,
  openResearchResultHref,
  openResearchStatusLabel,
  openResearchStatusMessage,
  openResearchSubmitOptionsFromPaperInput,
} from "@/lib/openresearch";
import { I } from "./icons";
import { nextId } from "@/lib/mock";
import { InPlaceEditor } from "./canvas/InPlaceEditor";
import { HtmlAreaRevisionEditor } from "./canvas/HtmlAreaRevisionEditor";
import { HtmlFlowLayoutEditor } from "./canvas/HtmlFlowLayoutEditor";
import { PosterStyleEditor } from "./canvas/PosterStyleEditor";
import { findInjectedStyle } from "./canvas/styleTagCompatibility";
import { DeckNavBar } from "./canvas/DeckNavBar";
import { LayerDeckNavBar } from "./canvas/LayerDeckNavBar";
import { VideoTimelineBar } from "./canvas/VideoTimelineBar";
import { ArtifactDownloadMenu } from "./ArtifactDownloadMenu";
import { detectSlideFrames, layerIntersectsFrame } from "@/lib/slide_frames";
import { resolveDeckViewportSize } from "@/lib/html_artifact_geometry";
import {
  DECK_RUNTIME_ROOT_ATTR,
  DECK_RUNTIME_SLIDE_ATTR,
  findDeckSlides,
  markDeckArtifactRoot,
} from "@/lib/deck_navigation";
import { translate } from "@/lib/i18n";
import { hasPaperAssetDrag, readPaperAssetDrag } from "@/lib/paper_asset_drag";
import { paperBundleBlocksPptxExport } from "@/lib/paper_bundle";
import {
  CANVAS_LAYER_ORDER,
  CANVAS_MAX_ZOOM,
  CANVAS_MIN_ZOOM,
  CANVAS_ZOOM_PRESETS,
  clampCanvasZoomPercent,
  supportsCanvasZoom,
} from "@/lib/canvas_zoom";

type SnapGuide = { axis: "x" | "y"; pos: number };
type Marquee = { x: number; y: number; w: number; h: number };

const SELECTION_BLUE = "#0ea5e9";
const SELECTION_BLUE_DARK = "#0369a1";
const SELECTION_HALO = "rgba(255, 255, 255, 0.96)";
export { findDeckArtifactRoot } from "@/lib/deck_navigation";

const clamp01 = (n: number) => Math.max(0, Math.min(1, n));

function rgbaFromHex(hex: string, opacity: number) {
  const clean = hex.trim().replace("#", "");
  if (!/^[0-9a-f]{6}$/i.test(clean)) return `rgba(23, 19, 15, ${opacity})`;
  const n = parseInt(clean, 16);
  const r = (n >> 16) & 255;
  const g = (n >> 8) & 255;
  const b = n & 255;
  return `rgba(${r}, ${g}, ${b}, ${opacity})`;
}

function layerShadowCss(layer: Layer) {
  const s = layer.shadow;
  if (!s) return undefined;
  return `${s.dx}px ${s.dy}px ${s.blur}px ${rgbaFromHex(s.color, clamp01(s.opacity))}`;
}

function textShadowCss(layer: Layer) {
  const s = layer.effects?.shadow;
  if (!s) return undefined;
  return `${s.dx}px ${s.dy}px ${s.blur}px ${s.color}`;
}

function objectPositionCss(layer: Layer) {
  const pos = layer.object_position ?? { x: 0.5, y: 0.5 };
  return `${Math.round(clamp01(pos.x) * 100)}% ${Math.round(clamp01(pos.y) * 100)}%`;
}

export function Canvas() {
  const art = useActiveArtifact();
  const paperBundlePptxExportDisabled = useApp((s) => {
    const current = s.conversations[s.current_conversation_id];
    const parent = current?.paper_bundle?.kind === "parent"
      ? current
      : current?.paper_bundle?.kind === "child"
        ? s.conversations[current.paper_bundle.parent_conversation_id]
        : undefined;
    return parent?.paper_bundle?.kind === "parent"
      && paperBundleBlocksPptxExport(parent.paper_bundle);
  });
  const selectedIds = useApp((s) => s.selected_layer_ids);
  const selectLayer = useApp((s) => s.selectLayer);
  const setSelection = useApp((s) => s.setSelection);
  const clearSelection = useApp((s) => s.clearSelection);
  const gridVisible = useApp((s) => s.grid_visible);
  const rulersVisible = useApp((s) => s.rulers_visible);
  const safeMarginsVisible = useApp((s) => s.safe_margins_visible);
  const pendingInsert = useApp((s) => s.pending_insert);
  const commitPendingInsert = useApp((s) => s.commitPendingInsert);
  const cancelPendingInsert = useApp((s) => s.cancelPendingInsert);
  const insertLayers = useApp((s) => s.insertLayers);
  const updateLayer = useApp((s) => s.updateLayer);
  const updateCanvas = useApp((s) => s.updateCanvas);
  const areaEditActive = useApp((s) => s.area_revision_active);
  const setAreaEditActive = useApp((s) => s.setAreaRevisionActive);
  const clearAreaRevisionItems = useApp((s) => s.clearAreaRevisionItems);
  const setSelectedPaperAsset = useApp((s) => s.setSelectedPaperAsset);
  const artType = art ? artifactTypeForArtifact(art) : null;
  const [styleEditActive, setStyleEditActive] = useState(false);

  const containerRef = useRef<HTMLDivElement | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);
  const [scale, setScale] = useState(0.5);
  const [auto, setAuto] = useState(true);
  const [fitSize, setFitSize] = useState<{ w: number; h: number } | null>(null);
  const [snapGuides, setSnapGuides] = useState<SnapGuide[]>([]);
  const [marquee, setMarquee] = useState<Marquee | null>(null);
  const [pendingPoint, setPendingPoint] = useState<{ x: number; y: number } | null>(null);
  const smartGuidesVisible = useApp((s) => s.smart_guides_visible);
  const isZoomableHtmlArtifact =
    !!art &&
    !!art.native_file_url &&
    supportsCanvasZoom(artType, art.view_format ?? art.native_format);

  const syncHtmlContentSize = useCallback((size: { w: number; h: number }) => {
    setFitSize(size);
    if (!isZoomableHtmlArtifact || !art || artType !== "poster") return;
    if (art.canvas.w === size.w && art.canvas.h === size.h) return;
    updateCanvas({ w: size.w, h: size.h });
  }, [art, artType, isZoomableHtmlArtifact, updateCanvas]);

  // Detect editable-deck slide frames (single-slide rendering when the
  // artifact is a layer-mode deck with ≥2 detected slide rectangles).
  const slideFrames = useMemo(
    () => (art ? detectSlideFrames(art) : []),
    [art]
  );
  const activeSlideIdx = useApp((s) => s.active_slide_idx);
  const activeFrame =
    slideFrames.length >= 2
      ? slideFrames[Math.min(activeSlideIdx, slideFrames.length - 1)]
      : null;
  const artifactLayers = useMemo(
    () => (Array.isArray(art?.layers) ? art.layers : []),
    [art?.layers],
  );
  // Layer-mode deck navbar is absolutely positioned over the scroll
  // container so canvas-content scroll never moves it. Video uses the
  // timeline bar instead, even though it also has detected scene frames.
  const showSlideNavBar = artType !== "video" && slideFrames.length >= 2;
  const navBarHeight = useApp((s) => s.deck_navbar_height);

  // Stage dims drive both the fit calc and the rendered canvas size.
  // In slide mode → the active frame's bbox; otherwise → the full art.
  const stageW = activeFrame?.bbox.w ?? art?.canvas.w ?? 0;
  const stageH = activeFrame?.bbox.h ?? art?.canvas.h ?? 0;

  const visibleStageLayers = useMemo(
    () =>
      art
        ? (activeFrame
            ? artifactLayers.filter((l) => layerIntersectsFrame(l, activeFrame))
            : artifactLayers
          ).filter((l) => l.visible !== false && l.bbox)
        : [],
    [art, activeFrame, artifactLayers]
  );

  const activeReference = activeFrame?.bbox ?? (art ? { x: 0, y: 0, w: art.canvas.w, h: art.canvas.h } : undefined);

  useLayoutEffect(() => {
    if (!auto || !art || !containerRef.current) return;
    const fit = () => {
      const el = containerRef.current!;
      // The stage wrapper uses p-14 (112px total). Leave a small
      // extra buffer so "Fit" means the entire canvas is visible
      // without needing to scroll one last sliver.
      const padding = 128;
      const target = activeFrame
        ? { w: stageW, h: stageH }
        : (fitSize ?? art.canvas);
      const sx = (el.clientWidth - padding) / target.w;
      const sy = (el.clientHeight - padding) / target.h;
      setScale(Math.max(CANVAS_MIN_ZOOM, Math.min(CANVAS_MAX_ZOOM, Math.min(sx, sy))));
    };
    fit();
    const obs = new ResizeObserver(fit);
    obs.observe(containerRef.current);
    return () => obs.disconnect();
  }, [auto, art?.canvas.w, art?.canvas.h, fitSize?.w, fitSize?.h, stageW, stageH, activeFrame]);

  useEffect(() => {
    setAuto(true);
    setFitSize(null);
    setSnapGuides([]);
    setMarquee(null);
    setPendingPoint(null);
    clearAreaRevisionItems();
    setSelectedPaperAsset(null);
    setAreaEditActive(false);
    setStyleEditActive(false);
    cancelPendingInsert();
    clearSelection();
    // Snap back to slide 1 whenever a different artifact comes in,
    // otherwise the user lands on a stale slide index from the prior
    // deck (or worse, an out-of-range one).
    useApp.getState().setActiveSlideIdx(0);
  }, [
    art?.artifact_id,
    art?.native_file_url,
    clearAreaRevisionItems,
    clearSelection,
    cancelPendingInsert,
    setSelectedPaperAsset,
    setAreaEditActive,
  ]);

  useEffect(() => {
    if (!pendingInsert) setPendingPoint(null);
  }, [pendingInsert]);

  // Jumping between slides should bring the new slide's top into view —
  // otherwise the prior slide's scroll position lingers and you land
  // mid-slide.
  useEffect(() => {
    containerRef.current?.scrollTo({ top: 0, left: 0 });
  }, [activeSlideIdx]);

  useCanvasShortcuts({
    art,
    activeFrame: activeReference,
    visibleLayers: visibleStageLayers,
  });

  if (!art) {
    return (
      <div className="canvas-grid-bg flex h-full w-full items-center justify-center text-[13px] italic text-ink-500">
        No artifact yet — generate one from the chat.
      </div>
    );
  }

  const viewUrl = art.view_file_url ?? art.native_file_url;
  const viewFormat = art.view_format ?? art.native_format;
  const isNative = !!viewUrl;
  const isHtml = isNative && viewFormat === "html";
  const isZoomableHtml = isNative && supportsCanvasZoom(artType, viewFormat);
  const rulerW = rulersVisible && activeReference ? 34 : 0;
  const rulerH = rulersVisible && activeReference ? 24 : 0;
  const stagePointFromEvent = (e: React.MouseEvent<HTMLElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    return {
      x: (activeReference?.x ?? 0) + (e.clientX - rect.left) / scale,
      y: (activeReference?.y ?? 0) + (e.clientY - rect.top) / scale,
    };
  };
  const paperAssetLayerAt = (asset: ArtifactAsset, point: { x: number; y: number }): Layer => {
    const maxW = Math.max(120, Math.min(360, (activeReference?.w ?? art.canvas.w) * 0.28));
    const maxH = Math.max(90, Math.min(260, (activeReference?.h ?? art.canvas.h) * 0.24));
    return {
      layer_id: nextId("paper_asset"),
      name: asset.name,
      kind: "image",
      z_index: Math.max(0, ...art.layers.map((l) => l.z_index)) + 1,
      bbox: {
        x: Math.round(point.x - maxW / 2),
        y: Math.round(point.y - maxH / 2),
        w: Math.round(maxW),
        h: Math.round(maxH),
      },
      src: asset.url,
      fit: "contain",
      opacity: 1,
    };
  };
  const imageLayerAtPoint = (point: { x: number; y: number }): Layer | null =>
    visibleStageLayers
      .slice()
      .sort((a, b) => b.z_index - a.z_index)
      .find((layer) => {
        if (layer.kind !== "image" || layer.locked || !layer.bbox) return false;
        const { x, y, w, h } = layer.bbox;
        return point.x >= x && point.x <= x + w && point.y >= y && point.y <= y + h;
      }) ?? null;
  const onPaperAssetDragOver = (e: React.DragEvent<HTMLElement>) => {
    if (!hasPaperAssetDrag(e.dataTransfer)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  };
  const onPaperAssetDrop = (e: React.DragEvent<HTMLElement>) => {
    const asset = readPaperAssetDrag(e.dataTransfer);
    if (!asset || !activeReference) return;
    e.preventDefault();
    e.stopPropagation();
    setSelectedPaperAsset(asset);
    const point = stagePointFromEvent(e);
    const target = imageLayerAtPoint(point);
    if (target) {
      updateLayer(target.layer_id, { src: asset.url, fit: target.fit ?? "contain" });
      selectLayer(target.layer_id, "replace");
      return;
    }
    insertLayers([paperAssetLayerAt(asset, point)], {
      placement: "single",
      strategy: "point",
      anchor: point,
    });
  };

  return (
    <div className="canvas-grid-bg relative flex h-full w-full flex-col">
      <CanvasToolbar
        scale={scale}
        setScale={(v) => {
          setAuto(false);
          setScale(v);
        }}
        onFit={() => setAuto(true)}
        hideZoom={isNative && !isZoomableHtml}
        showDesignTools={!isNative}
        activeFrame={activeReference}
        areaEditActive={areaEditActive}
        setAreaEditActive={(active) => {
          setAreaEditActive(active);
          if (active) setStyleEditActive(false);
        }}
        styleEditActive={styleEditActive}
        setStyleEditActive={(active) => {
          setStyleEditActive(active);
          if (active) setAreaEditActive(false);
        }}
        pptxExportDisabled={paperBundlePptxExportDisabled}
      />
      {isZoomableHtml ? (
        <ZoomableHtmlArtifactView
          art={art}
          url={viewUrl!}
          scale={scale}
          containerRef={containerRef}
          onContentSize={syncHtmlContentSize}
          areaEditActive={areaEditActive}
          styleEditActive={styleEditActive}
        />
      ) : isHtml ? (
        <RawHtmlArtifactView art={art} url={viewUrl!} />
      ) : isNative ? (
        <NativeArtifactView
          art={art}
          pptxExportDisabled={paperBundlePptxExportDisabled}
        />
      ) : (
        <div className="relative flex min-h-0 flex-1 flex-col">
          <div
            ref={containerRef}
            className="relative min-h-0 flex-1 overflow-auto"
            style={{ paddingTop: showSlideNavBar ? navBarHeight : 0 }}
            onMouseDown={(e) => {
              // click on empty area deselects
              if (e.target === e.currentTarget) clearSelection();
            }}
          >
            <div
              className="flex min-h-full min-w-full p-14"
              style={{
                alignItems: "safe center",
                justifyContent: "safe center",
              }}
            >
              <div
                className="relative"
                style={{
                  width: stageW * scale + rulerW,
                  height: stageH * scale + rulerH,
                }}
              >
                {rulersVisible && activeReference && (
                  <RulerOverlay frame={activeReference} scale={scale} />
                )}
                <div
                  className={`absolute shadow-page overflow-hidden ${pendingInsert ? "cursor-crosshair" : ""}`}
                  style={{
                    left: rulerW,
                    top: rulerH,
                    width: stageW * scale,
                    height: stageH * scale,
                    background: art.canvas.background,
                  }}
                  onMouseMove={(e) => {
                    if (pendingInsert && activeReference) setPendingPoint(stagePointFromEvent(e));
                  }}
                  onMouseLeave={() => setPendingPoint(null)}
                  onDragOver={onPaperAssetDragOver}
                  onDrop={onPaperAssetDrop}
                  onMouseDownCapture={(e) => {
                    if (!pendingInsert || !activeReference || e.button !== 0) return;
                    e.preventDefault();
                    e.stopPropagation();
                    commitPendingInsert(stagePointFromEvent(e));
                  }}
                  onMouseDown={(e) => {
                    if (e.target === e.currentTarget) clearSelection();
                  }}
                >
                  <div
                    ref={contentRef}
                    className="absolute left-0 top-0 origin-top-left"
                    style={{
                      width: art.canvas.w,
                      height: art.canvas.h,
                      // In slide mode, translate(-Fx, -Fy) (in pre-scale
                      // units) parks the active frame's top-left at the
                      // stage's (0,0). overflow:hidden on the parent
                      // clips everything outside the active slide.
                      transform: activeFrame
                        ? `scale(${scale}) translate(${-activeFrame.bbox.x}px, ${-activeFrame.bbox.y}px)`
                        : `scale(${scale})`,
                      background: art.canvas.background,
                    }}
                    onMouseDown={(e) => {
                      if (e.target !== e.currentTarget) return;
                      beginMarqueeSelection({
                        event: e,
                        contentEl: contentRef.current,
                        scale,
                        activeFrame: activeReference ?? null,
                        visibleLayers: visibleStageLayers,
                        setMarquee,
                        setSelection,
                        clearSelection,
                      });
                    }}
                  >
                    {gridVisible && activeReference && (
                      <CanvasGridOverlay frame={activeReference} scale={scale} />
                    )}
                    {safeMarginsVisible && activeReference && (
                      <SafeMarginOverlay frame={activeReference} scale={scale} />
                    )}
                    {visibleStageLayers
                      .slice()
                      .sort((a, b) => a.z_index - b.z_index)
                      .map((l) => (
                        <LayerView
                          key={l.layer_id}
                          layer={l}
                          art={art}
                          selected={selectedIds.includes(l.layer_id)}
                          selectedIds={selectedIds}
                          visibleLayers={visibleStageLayers}
                          activeFrame={activeReference}
                          smartGuides={smartGuidesVisible}
                          scale={scale}
                          onSelect={(mode) => selectLayer(l.layer_id, mode)}
                          setSnapGuides={setSnapGuides}
                        />
                      ))}
                    <SelectionOverlay
                      layers={visibleStageLayers.filter((l) => selectedIds.includes(l.layer_id))}
                      scale={scale}
                      visibleLayers={visibleStageLayers}
                      activeFrame={activeReference}
                      smartGuides={smartGuidesVisible}
                      setSnapGuides={setSnapGuides}
                    />
                    <SnapGuides guides={snapGuides} scale={scale} />
                    {marquee && <MarqueeRect rect={marquee} scale={scale} />}
                  </div>
                  {pendingInsert && activeReference && (
                    <PendingInsertPreview
                      pending={pendingInsert}
                      point={pendingPoint}
                      frame={activeReference}
                      scale={scale}
                    />
                  )}
                </div>
              </div>
            </div>
          </div>
          {artType === "video" && art.video_project ? (
            <VideoTimelineBar art={art} />
          ) : null}
          {showSlideNavBar && (
            <div className="absolute left-0 right-0 top-0 z-30">
              <LayerDeckNavBar art={art} scale={scale} scrollRef={containerRef} />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ManualSaveControl({ hasUnsaved }: { hasUnsaved: boolean }) {
  const state = useApp((s) => s.autosave_state);
  const last = useApp((s) => s.autosave_last_saved_at);
  const error = useApp((s) => s.autosave_error);
  const flush = useApp((s) => s.flushAutoSave);
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);

  // After "saved" lingers for 2.5 s, idle back to keep the toolbar quiet.
  useEffect(() => {
    if (state !== "saved") return;
    const t = window.setTimeout(() => {
      // Only flip back if still "saved" (a new edit may have moved on).
      const cur = useApp.getState().autosave_state;
      if (cur === "saved") useApp.setState({ autosave_state: "idle" });
    }, 2500);
    return () => window.clearTimeout(t);
  }, [state]);

  const disabled = state === "saving" || (!hasUnsaved && state !== "error");
  const title =
    state === "saving"
      ? t("Saving changes")
      : state === "error"
        ? error ?? t("Save failed")
        : hasUnsaved
          ? t("Save changes (⌘S)")
          : last
            ? `${t("Saved")} ${formatTime(last)}`
            : t("No unsaved changes");
  const label =
    state === "saving"
      ? t("Saving")
      : state === "saved"
        ? t("Saved")
        : state === "error"
          ? t("Save failed")
          : t("Save changes (⌘S)");

  return (
    <span className="ml-1 inline-flex items-center">
      <button
        type="button"
        onClick={() => void flush()}
        disabled={disabled}
        title={title}
        className={`inline-flex h-7 items-center gap-1.5 rounded-md border px-2.5 text-[10px] font-medium uppercase transition ${
          hasUnsaved
            ? "border-ink-900 bg-ink-900 text-paper hover:bg-ink-800"
            : state === "error"
              ? "border-amber-700/50 bg-amber-50 text-amber-900 hover:bg-amber-100"
              : "border-ink-300 bg-paper text-ink-500"
        } shrink-0 disabled:cursor-default disabled:opacity-60`}
        style={{ letterSpacing: "0.11em" }}
      >
        {state === "saving" ? (
          <I.Refresh width={13} height={13} className="animate-spin" />
        ) : state === "saved" ? (
          <I.Check width={13} height={13} />
        ) : state === "error" ? (
          <I.Alert width={13} height={13} />
        ) : (
          <I.Save width={13} height={13} />
        )}
        <span>{label}</span>
      </button>
    </span>
  );
}

function formatTime(ms: number): string {
  const d = new Date(ms);
  const h = d.getHours();
  const m = d.getMinutes().toString().padStart(2, "0");
  const ampm = h >= 12 ? "PM" : "AM";
  const h12 = h % 12 === 0 ? 12 : h % 12;
  return `${h12}:${m} ${ampm}`;
}

function NativeArtifactView({
  art,
  pptxExportDisabled,
}: {
  art: Artifact;
  pptxExportDisabled: boolean;
}) {
  const url = art.native_file_url!;
  const fmt = art.native_format;

  if (fmt === "svg") {
    return (
      <div className="flex flex-1 items-center justify-center overflow-auto p-14">
        <img
          src={url}
          alt={art.name}
          className="max-h-full max-w-full shadow-page"
          style={{ background: art.canvas.background }}
        />
      </div>
    );
  }

  if (fmt === "pptx") {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-5 text-ink-500">
        <I.Deck width={44} height={44} strokeWidth={1.1} />
        <div className="text-center">
          <div className="font-display text-[18px] text-ink-900" style={{ fontVariationSettings: '"opsz" 72' }}>
            {art.name}
          </div>
          <div className="mt-1.5 text-[11px] uppercase text-ink-500" style={{ letterSpacing: "0.16em" }}>
            PPTX preview not available
          </div>
        </div>
        <ArtifactDownloadMenu
          artifact={art}
          label="Download"
          pptxExportDisabled={pptxExportDisabled}
          className="inline-flex items-center gap-1.5 rounded-sm bg-ink-900 px-3.5 py-2 text-[10px] font-medium uppercase text-ink-50 hover:bg-ink-700"
        />
      </div>
    );
  }

  if (fmt === "mp4") {
    return (
      <div className="flex flex-1 items-center justify-center overflow-auto bg-paper p-8">
        <video
          src={url}
          controls
          playsInline
          poster={art.preview_url}
          className="max-h-full max-w-[1600px] shadow-page"
          // Native aspect lock; the file is 1920×1080 from the renderer.
          style={{ aspectRatio: `${art.canvas.w} / ${art.canvas.h}` }}
        >
          {art.downloads?.vtt && (
            <track kind="subtitles" srcLang="en" label="English" src={art.downloads.vtt} />
          )}
        </video>
      </div>
    );
  }

  return (
    <div className="flex flex-1 items-center justify-center text-[13px] italic text-ink-500">
      Unknown native format.
    </div>
  );
}

/**
 * Renders the HTML artifact in an iframe and bolts the in-place editor
 * onto it. The iframe is same-origin (Vite proxies /api/files/runs/*)
 * so the parent React app can reach `iframe.contentDocument` directly
 * — no postMessage shim needed. Keyed by `native_file_url` so the
 * iframe re-mounts cleanly after each successful save (which
 * yields a NEW run dir with a NEW URL); React unmounts the prior
 * iframe + editor and remounts both, which re-runs all listeners
 * against the freshly-rendered DOM.
 */
function RawHtmlArtifactView({ art, url }: { art: Artifact; url: string }) {
  const [iframeEl, setIframeEl] = useState<HTMLIFrameElement | null>(null);
  const artType = artifactTypeForArtifact(art);
  return (
    <div className="relative flex flex-1 flex-col">
      {artType === "deck" && <DeckNavBar iframe={iframeEl} />}
      <div className="relative flex min-h-0 flex-1 items-stretch justify-center overflow-auto p-8">
        <iframe
          key={url}
          ref={setIframeEl}
          src={url}
          title={art.name}
          // See ZoomableHtmlArtifactView for the sandbox rationale.
          sandbox="allow-same-origin"
          className="h-full w-full max-w-[1440px] border border-ink-300/70 bg-white shadow-page"
        />
        <InPlaceEditor iframe={iframeEl} />
      </div>
    </div>
  );
}

function ZoomableHtmlArtifactView({
  art,
  url,
  scale,
  containerRef,
  onContentSize,
  areaEditActive,
  styleEditActive,
}: {
  art: Artifact;
  url: string;
  scale: number;
  containerRef: RefObject<HTMLDivElement>;
  onContentSize: (size: { w: number; h: number }) => void;
  areaEditActive: boolean;
  styleEditActive: boolean;
}) {
  const [iframeEl, setIframeEl] = useState<HTMLIFrameElement | null>(null);
  const artType = artifactTypeForArtifact(art);
  const [frameSize, setFrameSize] = useState(() => ({
    w: art.canvas.w,
    h: art.canvas.h,
  }));

  useEffect(() => {
    const size = { w: art.canvas.w, h: art.canvas.h };
    setFrameSize(size);
    onContentSize(size);
  }, [art.artifact_id, art.canvas.w, art.canvas.h, onContentSize]);

  const readHtmlCanvasSize = useCallback((doc: Document) => {
    if (artType === "deck") {
      const deckRoot = markDeckArtifactRoot(doc);
      const firstSlide = findDeckSlides(doc)[0];
      return resolveDeckViewportSize(
        art.canvas,
        {
          w: Number(deckRoot?.dataset.w),
          h: Number(deckRoot?.dataset.h),
        },
        {
          w: firstSlide?.offsetWidth ?? 0,
          h: firstSlide?.offsetHeight ?? 0,
        },
      );
    }
    const canvas = doc.querySelector<HTMLElement>(".canvas, .paper-poster");
    const dataW = Number(canvas?.dataset.w);
    const dataH = Number(canvas?.dataset.h);
    const w = Number.isFinite(dataW) && dataW > 0
      ? dataW
      : canvas?.offsetWidth || art.canvas.w;
    const h = Number.isFinite(dataH) && dataH > 0
      ? dataH
      : canvas?.offsetHeight || art.canvas.h;
    return { w: Math.ceil(w), h: Math.ceil(h) };
  }, [art.canvas.h, art.canvas.w, artType]);

  const normalizeHtmlDocument = useCallback((doc: Document, size: { w: number; h: number }) => {
    if (!doc.head || !doc.body) return;
    const styleId = "autodesign-web-poster-editor-frame";
    let style = findInjectedStyle(
      doc,
      styleId,
      "designanything-web-poster-editor-frame",
    );
    if (!style) {
      style = doc.createElement("style");
      style.id = styleId;
      doc.head.appendChild(style);
    }
    // The generated standalone poster HTML has its own body padding,
    // dark page background, and centered document layout. In the web
    // editor, the parent owns the page chrome and zoom. Flatten the
    // iframe to the poster .canvas itself so Fit always targets the
    // actual editable artwork, not the standalone document shell.
    style.textContent = artType === "deck" ? `
      html, body {
        width: ${size.w}px !important;
        min-width: ${size.w}px !important;
        min-height: ${size.h}px !important;
        margin: 0 !important;
        padding: 0 !important;
        display: block !important;
        overflow-x: hidden !important;
        background: transparent !important;
        box-sizing: border-box !important;
      }
      [${DECK_RUNTIME_ROOT_ATTR}] {
        display: block !important;
        width: ${size.w}px !important;
        margin: 0 !important;
        padding: 0 !important;
        gap: 0 !important;
        transform: none !important;
      }
      [${DECK_RUNTIME_SLIDE_ATTR}] {
        width: ${size.w}px !important;
        height: ${size.h}px !important;
        margin: 0 !important;
        box-shadow: none !important;
        scroll-margin-top: 0 !important;
      }
      .kbd-help { display: none !important; }
    ` : `
      html, body {
        width: ${size.w}px !important;
        height: ${size.h}px !important;
        min-width: ${size.w}px !important;
        min-height: ${size.h}px !important;
        margin: 0 !important;
        padding: 0 !important;
        display: block !important;
        overflow: hidden !important;
        background: transparent !important;
        box-sizing: border-box !important;
      }
      .canvas {
        width: ${size.w}px !important;
        height: ${size.h}px !important;
        margin: 0 !important;
        box-shadow: none !important;
        overflow: hidden !important;
      }
      .ld-toolbar,
      .ld-modal-backdrop {
        display: none !important;
      }
    `;
  }, [artType]);

  const updateFrameSize = useCallback(() => {
    if (!iframeEl?.contentDocument) return;
    const doc = iframeEl.contentDocument;
    const next = readHtmlCanvasSize(doc);
    normalizeHtmlDocument(doc, next);
    setFrameSize((prev) => (prev.w === next.w && prev.h === next.h ? prev : next));
    onContentSize(next);
  }, [iframeEl, normalizeHtmlDocument, onContentSize, readHtmlCanvasSize]);

  useEffect(() => {
    if (!iframeEl) return;
    iframeEl.addEventListener("load", updateFrameSize);
    if (iframeEl.contentDocument?.readyState === "complete") updateFrameSize();
    return () => iframeEl.removeEventListener("load", updateFrameSize);
  }, [iframeEl, updateFrameSize]);

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      {artType === "deck" && <DeckNavBar iframe={iframeEl} />}
      <div ref={containerRef} data-canvas-scroll className="relative min-h-0 flex-1 overflow-auto">
        <div
          className="flex min-h-full min-w-full p-14"
          style={{
            alignItems: "safe center",
            justifyContent: "safe center",
          }}
        >
          <div
            className="relative shadow-page"
            style={{
              width: frameSize.w * scale,
              height: frameSize.h * scale,
            }}
          >
            <iframe
              key={url}
              ref={setIframeEl}
              src={url}
              title={art.name}
              // `sandbox=""` (max sandbox) gives the iframe an opaque
              // origin — that blocks the parent React app from reading
              // `iframe.contentDocument` (everything is null), which is
              // exactly what InPlaceEditor needs to wire up listeners.
              // `allow-same-origin` is the relaxation that keeps scripts
              // disabled (we don't want the agent's pre-bundled ld-toolbar
              // JS competing with our floating toolbar) but DOES let the
              // parent reach into the document tree.
              sandbox="allow-same-origin"
              className="absolute left-0 top-0 border border-ink-300/70 bg-white"
              style={{
                width: frameSize.w,
                height: frameSize.h,
                transform: `scale(${scale})`,
                transformOrigin: "top left",
              }}
            />
          </div>
        </div>
        {!areaEditActive && <InPlaceEditor iframe={iframeEl} scale={scale} />}
        {artType === "poster" && !areaEditActive && (
          <HtmlFlowLayoutEditor iframe={iframeEl} scale={scale} />
        )}
        {artType === "poster" && (
          <HtmlAreaRevisionEditor
            iframe={iframeEl}
            active={areaEditActive}
            scale={scale}
          />
        )}
        {artType === "poster" && (
          <PosterStyleEditor
            iframe={iframeEl}
            active={styleEditActive && !areaEditActive}
          />
        )}
      </div>
    </div>
  );
}

function CanvasToolbar({
  scale,
  setScale,
  onFit,
  hideZoom,
  showDesignTools,
  activeFrame,
  areaEditActive,
  setAreaEditActive,
  styleEditActive,
  setStyleEditActive,
  pptxExportDisabled,
}: {
  scale: number;
  setScale: (n: number) => void;
  onFit: () => void;
  hideZoom?: boolean;
  showDesignTools?: boolean;
  activeFrame?: Bbox;
  areaEditActive: boolean;
  setAreaEditActive: (active: boolean) => void;
  styleEditActive: boolean;
  setStyleEditActive: (active: boolean) => void;
  pptxExportDisabled: boolean;
}) {
  const art = useActiveArtifact();
  const enterChat = useApp((s) => s.enterChat);
  const toggleProperties = useApp((s) => s.togglePropertiesSidebar);
  const properties_open = useApp((s) => s.properties_sidebar_open);
  const designFocus = useApp((s) => s.design_focus_mode);
  const toggleDesignFocus = useApp((s) => s.toggleDesignFocusMode);
  const submitOpenResearchProject = useApp((s) => s.submitOpenResearchProject);
  const currentConversation = useApp((s) => s.conversations[s.current_conversation_id]);
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  const [paperIdDraft, setPaperIdDraft] = useState("");
  const layerCount = Array.isArray(art?.layers) ? art.layers.length : 0;
  const artType = art ? artifactTypeForArtifact(art) : null;
  const isEditableHtml =
    !!art
    && (artType === "poster" || artType === "deck" || artType === "landing")
    && !!art.native_file_url
    && (art.view_format ?? art.native_format) === "html";
  const isEditable =
    !!art
    && artType !== "video"
    && (!art.native_file_url || layerCount > 0 || isEditableHtml);
  const canEditArea =
    !!art
    && artType === "poster"
    && !!art.native_file_url
    && (art.view_format ?? art.native_format) === "html";
  const showManualSave =
    !!art?.native_file_url && art.native_format === "html" && (layerCount > 0 || isEditableHtml);
  const activeEdits =
    art?.artifact_id ? currentConversation?.pending_edits?.[art.artifact_id] : undefined;
  const hasUnsaved = hasPendingEditsPayload(activeEdits);
  const research = art?.openresearch;
  const researchHref = openResearchResultHref(research);
  const researchLabel = t(openResearchStatusLabel(research));
  const researchMessage = openResearchStatusMessage(research);
  const needsPaperId = openResearchNeedsPaperId(research);
  const paperIdValue = paperIdDraft.trim();
  const submitPaperIdRetry = () => {
    if (!art || !paperIdValue || research?.status === "running") return;
    void submitOpenResearchProject(
      art.artifact_id,
      openResearchSubmitOptionsFromPaperInput(paperIdValue),
    );
  };
  const titleCluster = (
    <div className="flex min-w-0 flex-1 items-center gap-3 overflow-hidden">
      <button
        onClick={enterChat}
        className="group inline-flex shrink-0 items-center gap-1.5 rounded-md text-ink-500 transition hover:text-ink-900"
      >
        <I.ArrowLeft width={13} height={13} className="transition group-hover:-translate-x-0.5" />
        <span className="eyebrow whitespace-nowrap">{t("Back to chat")}</span>
      </button>
      <span className="mx-1 h-4 w-px bg-ink-300" />
      <div className="flex min-w-0 items-baseline gap-2.5 overflow-hidden">
        <span className="eyebrow shrink-0">{t(isEditable ? "Editing" : "Preview")}</span>
        <span
          className="min-w-0 truncate font-display text-[15px] text-ink-900"
          style={{ fontVariationSettings: '"opsz" 36' }}
        >
          {art?.name}
        </span>
        <span className="tabular shrink-0 whitespace-nowrap text-[10px] uppercase text-ink-500" style={{ letterSpacing: "0.14em" }}>
          {art?.canvas.w} × {art?.canvas.h}
        </span>
        {showManualSave && <ManualSaveControl hasUnsaved={hasUnsaved} />}
      </div>
    </div>
  );

  const zoomCluster = (
    <div className="flex shrink-0 items-center gap-2">
      {!hideZoom && (
        <>
          <button
            onClick={() => setScale(Math.max(CANVAS_MIN_ZOOM, scale - 0.1))}
            className="icon-btn h-7 w-7"
            title={t("Zoom out")}
          >
            <I.ChevronDown width={13} height={13} />
          </button>
          <ZoomMenu scale={scale} setScale={setScale} onFit={onFit} />
          <button
            onClick={() => setScale(Math.min(CANVAS_MAX_ZOOM, scale + 0.1))}
            className="icon-btn h-7 w-7"
            title={t("Zoom in")}
          >
            <I.ChevronUp width={13} height={13} />
          </button>
          <span className="mx-1 h-4 w-px bg-ink-300" />
        </>
      )}
      <button
        onClick={toggleProperties}
        className="icon-btn"
        title={properties_open ? t("Hide properties") : t("Show properties")}
      >
        <I.PanelRight />
      </button>
      {canEditArea && (
        <button
          onClick={() => setAreaEditActive(!areaEditActive)}
          className={`inline-flex h-7 items-center gap-1.5 rounded-md border px-2.5 text-[10px] font-medium uppercase transition ${
            areaEditActive
              ? "border-accent bg-accent text-white hover:bg-accent-deep"
              : "border-ink-300/75 bg-paper/80 text-ink-700 hover:border-ink-500 hover:bg-white hover:text-ink-900"
          }`}
          style={{ letterSpacing: "0.14em" }}
          title={t("Select a poster area and describe the change.")}
        >
          <I.Focus width={12} height={12} />
          {t("Edit Area")}
        </button>
      )}
      {canEditArea && (
        <button
          onClick={() => setStyleEditActive(!styleEditActive)}
          className={`inline-flex h-7 items-center gap-1.5 rounded-md border px-2.5 text-[10px] font-medium uppercase transition ${
            styleEditActive
              ? "border-accent bg-accent text-white hover:bg-accent-deep"
              : "border-ink-300/75 bg-paper/80 text-ink-700 hover:border-ink-500 hover:bg-white hover:text-ink-900"
          }`}
          style={{ letterSpacing: "0.14em" }}
          title={t("Adjust poster or panel colors.")}
        >
          <I.Paintbrush width={12} height={12} />
          {t("Style")}
        </button>
      )}
      {art && (
        <ArtifactDownloadMenu
          artifact={art}
          compact
          pptxExportDisabled={pptxExportDisabled}
          className="inline-flex h-7 items-center gap-1.5 rounded-md border border-ink-300/75 bg-paper/80 px-2.5 text-[10px] font-medium uppercase text-ink-700 transition hover:border-ink-500 hover:bg-white hover:text-ink-900"
        />
      )}
      {art && artType === "poster" && (
        <>
          {needsPaperId && (
            <div
              className="flex h-8 max-w-[310px] items-center gap-1.5 rounded-md border border-amber-700/25 bg-amber-50 px-2 text-amber-900"
              title={researchMessage || t("OpenResearch needs a paper id")}
            >
              <I.Alert width={12} height={12} className="shrink-0" />
              <input
                value={paperIdDraft}
                onChange={(e) => setPaperIdDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    submitPaperIdRetry();
                  }
                }}
                placeholder={t("arXiv ID or URL")}
                className="h-6 min-w-0 flex-1 bg-transparent text-[11px] text-ink-900 outline-none placeholder:text-amber-900/55"
              />
              <button
                onClick={submitPaperIdRetry}
                disabled={!paperIdValue}
                className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-sm border border-amber-700/30 bg-amber-100 text-amber-900 transition hover:bg-amber-200 disabled:cursor-not-allowed disabled:opacity-50"
                title={t("Retry OpenResearch")}
              >
                <I.Refresh width={11} height={11} />
              </button>
            </div>
          )}
          {research?.status === "error" && !needsPaperId && researchMessage && (
            <span
              className="inline-flex h-8 max-w-[260px] items-center gap-1.5 truncate rounded-md border border-amber-700/25 bg-amber-50 px-2 text-[11px] text-amber-900"
              title={researchMessage}
            >
              <I.Alert width={12} height={12} className="shrink-0" />
              <span className="truncate">{researchMessage}</span>
            </span>
          )}
          {research?.status !== "running" && research?.status !== "error" && researchHref ? (
          <a
            href={researchHref}
            target="_blank"
            rel="noreferrer"
            className="inline-flex h-7 items-center gap-1.5 rounded-md border border-ink-300/75 bg-paper/80 px-2.5 text-[10px] font-medium uppercase text-ink-700 transition hover:border-ink-500 hover:bg-white hover:text-ink-900"
            style={{ letterSpacing: "0.14em" }}
            title={research?.gui_submitter_status === "submitted" ? t("Open OpenResearch session") : t("Open OpenResearch project")}
          >
            <I.Report width={12} height={12} />
            {researchLabel}
          </a>
        ) : (
          <button
            onClick={() => void submitOpenResearchProject(art.artifact_id)}
            disabled={research?.status === "running" || needsPaperId}
            className={`inline-flex h-7 items-center gap-1.5 rounded-md border border-ink-300/75 bg-paper/80 px-2.5 text-[10px] font-medium uppercase text-ink-700 transition hover:border-ink-500 hover:bg-white hover:text-ink-900 disabled:opacity-60 ${research?.status === "running" ? "disabled:cursor-wait" : ""} ${needsPaperId ? "disabled:cursor-not-allowed" : ""} ${research?.status === "error" ? "text-amber-700" : ""}`}
            style={{ letterSpacing: "0.14em" }}
            title={
              research?.status === "running"
                ? t("OpenResearch project is submitting")
                : researchMessage || t("Submit to OpenResearch")
            }
          >
            <I.Report width={12} height={12} />
            {researchLabel}
          </button>
        )}
        </>
      )}
      <button
        onClick={toggleDesignFocus}
        className={`icon-btn ${designFocus ? "bg-ink-900 text-white hover:bg-ink-900 hover:text-white" : ""}`}
        title={designFocus ? t("Exit focus design mode") : t("Focus design mode")}
      >
        <I.Focus />
      </button>
    </div>
  );

  if (showDesignTools) {
    return (
      <div
        className="relative flex h-[80px] shrink-0 flex-col border-b border-ink-300/50 bg-surface-raised/82 backdrop-blur-md"
        style={{ zIndex: CANVAS_LAYER_ORDER.toolbar }}
      >
        <div className="flex min-h-10 items-center justify-between gap-3 border-b border-ink-200/70 px-4">
          {titleCluster}
          {zoomCluster}
        </div>
        <div className="flex min-h-10 items-center overflow-x-auto px-4">
          <EditorToolbar activeFrame={activeFrame} />
        </div>
      </div>
    );
  }

  return (
    <div
      className="relative flex h-12 items-center justify-between border-b border-ink-300/50 bg-surface-raised/82 px-4 backdrop-blur-md"
      style={{ zIndex: CANVAS_LAYER_ORDER.toolbar }}
    >
      {titleCluster}
      {zoomCluster}
    </div>
  );
}

/**
 * Figma-style zoom dropdown. Click the percentage to open a menu of
 * presets (25 / 50 / 75 / 100 / 125 / 150 / 200 / 300 + Fit). Editable input
 * at the top accepts an arbitrary percent (clamped to 2–300%). The
 * value re-syncs from `scale` whenever the menu closes / reopens.
 */
function ZoomMenu({
  scale,
  setScale,
  onFit,
}: {
  scale: number;
  setScale: (n: number) => void;
  onFit: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const wrapRef = useRef<HTMLDivElement | null>(null);

  // Close on outside click + Escape.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Re-seed the editable input each time the menu opens.
  useEffect(() => {
    if (open) setDraft(String(Math.round(scale * 100)));
  }, [open, scale]);

  const currentPct = Math.round(scale * 100);

  const apply = (pct: number) => {
    const clamped = clampCanvasZoomPercent(pct);
    setScale(clamped / 100);
    setOpen(false);
  };

  const commitDraft = () => {
    const n = parseInt(draft, 10);
    if (Number.isFinite(n)) apply(n);
    else setOpen(false);
  };

  return (
    <div ref={wrapRef} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        className="tabular inline-flex items-center gap-1 rounded-sm px-2 py-1 text-[11px] font-medium text-ink-500 transition hover:bg-ink-100 hover:text-ink-900"
        title="Zoom"
        style={{ letterSpacing: "0.05em", minWidth: "56px" }}
      >
        <span>{currentPct}%</span>
        <I.ChevronDown width={11} height={11} className="opacity-60" />
      </button>

      {open && (
        <div
          className="absolute right-0 top-[calc(100%+4px)] w-40 overflow-hidden rounded-md border border-ink-300/70 bg-surface-raised py-1 shadow-soft"
          style={{ zIndex: CANVAS_LAYER_ORDER.menu }}
        >
          {/* Free-text % input */}
          <div className="flex items-center gap-1 px-2 pb-1 pt-0.5">
            <input
              autoFocus
              type="text"
              inputMode="numeric"
              value={draft}
              onChange={(e) => setDraft(e.target.value.replace(/[^\d]/g, ""))}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  commitDraft();
                }
              }}
              className="tabular w-full rounded border border-ink-300/70 bg-paper px-2 py-1 text-[11px] text-ink-900 outline-none focus:border-accent"
            />
            <span className="text-[10px] text-ink-500">%</span>
          </div>
          <div className="my-1 border-t border-ink-200/70" />
          {CANVAS_ZOOM_PRESETS.map((pct) => (
            <button
              key={pct}
              onClick={() => apply(pct)}
              className={`tabular flex w-full items-center justify-between px-3 py-1 text-[11px] transition hover:bg-ink-100 ${
                currentPct === pct ? "text-ink-900 font-medium" : "text-ink-700"
              }`}
            >
              <span>{pct}%</span>
              {currentPct === pct && (
                <I.Check width={11} height={11} className="text-accent" />
              )}
            </button>
          ))}
          <div className="my-1 border-t border-ink-200/70" />
          <button
            onClick={() => {
              onFit();
              setOpen(false);
            }}
            className="flex w-full items-center justify-between px-3 py-1 text-[11px] text-ink-700 transition hover:bg-ink-100"
            title="Auto-scale until canvas fits viewport"
          >
            <span>Fit to viewport</span>
            <span className="font-mono text-[9px] text-ink-400">⇧0</span>
          </button>
        </div>
      )}
    </div>
  );
}

const SNAP_THRESHOLD = 6;
const MIN_BOX_SIZE = 8;

function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName.toLowerCase();
  return (
    tag === "input" ||
    tag === "textarea" ||
    tag === "select" ||
    target.isContentEditable
  );
}

function layerBounds(layers: Layer[]): Bbox | null {
  const boxes = layers.map((l) => l.bbox).filter(Boolean) as Bbox[];
  if (!boxes.length) return null;
  const x1 = Math.min(...boxes.map((b) => b.x));
  const y1 = Math.min(...boxes.map((b) => b.y));
  const x2 = Math.max(...boxes.map((b) => b.x + b.w));
  const y2 = Math.max(...boxes.map((b) => b.y + b.h));
  return { x: x1, y: y1, w: x2 - x1, h: y2 - y1 };
}

function normalizeRect(a: { x: number; y: number }, b: { x: number; y: number }): Marquee {
  const x = Math.min(a.x, b.x);
  const y = Math.min(a.y, b.y);
  return {
    x,
    y,
    w: Math.abs(b.x - a.x),
    h: Math.abs(b.y - a.y),
  };
}

function rectsIntersect(a: Bbox | Marquee, b: Bbox | Marquee): boolean {
  return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
}

function artPoint(event: MouseEvent | React.MouseEvent, contentEl: HTMLElement | null, scale: number) {
  if (!contentEl) return { x: 0, y: 0 };
  const rect = contentEl.getBoundingClientRect();
  return {
    x: (event.clientX - rect.left) / scale,
    y: (event.clientY - rect.top) / scale,
  };
}

function snapBox(
  box: Bbox,
  movingIds: Set<string>,
  visibleLayers: Layer[],
  reference?: Bbox,
): { box: Bbox; guides: SnapGuide[] } {
  const xGuides: number[] = [];
  const yGuides: number[] = [];
  if (reference) {
    xGuides.push(reference.x, reference.x + reference.w / 2, reference.x + reference.w);
    yGuides.push(reference.y, reference.y + reference.h / 2, reference.y + reference.h);
  }
  for (const l of visibleLayers) {
    if (!l.bbox || movingIds.has(l.layer_id)) continue;
    const b = l.bbox;
    xGuides.push(b.x, b.x + b.w / 2, b.x + b.w);
    yGuides.push(b.y, b.y + b.h / 2, b.y + b.h);
  }
  const xEdges = [box.x, box.x + box.w / 2, box.x + box.w];
  const yEdges = [box.y, box.y + box.h / 2, box.y + box.h];
  let bestX: { delta: number; pos: number } | null = null;
  let bestY: { delta: number; pos: number } | null = null;
  for (const guide of xGuides) {
    for (const edge of xEdges) {
      const delta = guide - edge;
      if (Math.abs(delta) <= SNAP_THRESHOLD && (!bestX || Math.abs(delta) < Math.abs(bestX.delta))) {
        bestX = { delta, pos: guide };
      }
    }
  }
  for (const guide of yGuides) {
    for (const edge of yEdges) {
      const delta = guide - edge;
      if (Math.abs(delta) <= SNAP_THRESHOLD && (!bestY || Math.abs(delta) < Math.abs(bestY.delta))) {
        bestY = { delta, pos: guide };
      }
    }
  }
  const snapped = {
    ...box,
    x: bestX ? box.x + bestX.delta : box.x,
    y: bestY ? box.y + bestY.delta : box.y,
  };
  return {
    box: snapped,
    guides: [
      ...(bestX ? [{ axis: "x" as const, pos: bestX.pos }] : []),
      ...(bestY ? [{ axis: "y" as const, pos: bestY.pos }] : []),
    ],
  };
}

function beginMarqueeSelection({
  event,
  contentEl,
  scale,
  activeFrame,
  visibleLayers,
  setMarquee,
  setSelection,
  clearSelection,
}: {
  event: React.MouseEvent;
  contentEl: HTMLElement | null;
  scale: number;
  activeFrame: Bbox | null;
  visibleLayers: Layer[];
  setMarquee: (m: Marquee | null) => void;
  setSelection: (ids: string[]) => void;
  clearSelection: () => void;
}) {
  event.stopPropagation();
  const additive = event.shiftKey;
  const start = artPoint(event, contentEl, scale);
  if (!additive) clearSelection();
  const onMove = (ev: MouseEvent) => {
    const current = artPoint(ev, contentEl, scale);
    setMarquee(normalizeRect(start, current));
  };
  const onUp = (ev: MouseEvent) => {
    const current = artPoint(ev, contentEl, scale);
    const rect = normalizeRect(start, current);
    const picked = visibleLayers
      .filter((l) => {
        if (!l.bbox || l.locked || l.kind === "background") return false;
        if (activeFrame && !layerIntersectsFrame(l, { idx: 0, layer_id: "", bbox: activeFrame })) return false;
        return rectsIntersect(l.bbox, rect);
      })
      .map((l) => l.layer_id);
    const base = additive ? useApp.getState().selected_layer_ids : [];
    setSelection([...base, ...picked]);
    setMarquee(null);
    window.removeEventListener("mousemove", onMove);
    window.removeEventListener("mouseup", onUp);
  };
  window.addEventListener("mousemove", onMove);
  window.addEventListener("mouseup", onUp);
}

function useCanvasShortcuts({
  art,
  activeFrame,
  visibleLayers,
}: {
  art: Artifact | null;
  activeFrame?: Bbox;
  visibleLayers: Layer[];
}) {
  useEffect(() => {
    if (!art || art.native_file_url) return;
    const onKey = (e: KeyboardEvent) => {
      if (isTypingTarget(e.target)) return;
      const meta = e.metaKey || e.ctrlKey;
      const key = e.key.toLowerCase();
      const s = useApp.getState();
      if (meta && key === "z") {
        e.preventDefault();
        if (e.shiftKey) s.redo();
        else s.undo();
        return;
      }
      if ((meta && key === "y") || (meta && e.shiftKey && key === "z")) {
        e.preventDefault();
        s.redo();
        return;
      }
      if (meta && key === "d") {
        e.preventDefault();
        s.duplicateSelection();
        return;
      }
      if (meta && key === "g") {
        e.preventDefault();
        if (e.shiftKey) s.ungroupSelection();
        else s.groupSelection();
        return;
      }
      if (meta && key === "c") {
        e.preventDefault();
        if (e.altKey) s.copySelectionStyle();
        else s.copySelection();
        return;
      }
      if (meta && key === "v") {
        e.preventDefault();
        if (e.altKey) s.pasteSelectionStyle();
        else s.pasteSelection();
        return;
      }
      if (meta && key === "a") {
        e.preventDefault();
        const ids = visibleLayers
          .filter((l) => l.bbox && !l.locked && l.kind !== "background")
          .map((l) => l.layer_id);
        s.setSelection(ids);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        if (s.pending_insert) {
          s.cancelPendingInsert();
          return;
        }
        s.clearSelection();
        return;
      }
      if (e.key === "Delete" || e.key === "Backspace") {
        e.preventDefault();
        s.deleteSelection();
        return;
      }
      const step = e.shiftKey ? 10 : 1;
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        s.nudgeSelection(-step, 0);
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        s.nudgeSelection(step, 0);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        s.nudgeSelection(0, -step);
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        s.nudgeSelection(0, step);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [art, activeFrame, visibleLayers]);
}

function EditorToolbar({ activeFrame }: { activeFrame?: Bbox }) {
  const art = useActiveArtifact();
  const selectedIds = useApp((s) => s.selected_layer_ids);
  const history = useApp((s) =>
    art ? s.editor_history[art.artifact_id] ?? { past: [], future: [] } : { past: [], future: [] }
  );
  const clipboardCount = useApp((s) => s.editor_clipboard.length);
  const styleClipboard = useApp((s) => s.editor_style_clipboard);
  const undo = useApp((s) => s.undo);
  const redo = useApp((s) => s.redo);
  const duplicate = useApp((s) => s.duplicateSelection);
  const remove = useApp((s) => s.deleteSelection);
  const copy = useApp((s) => s.copySelection);
  const paste = useApp((s) => s.pasteSelection);
  const copyStyle = useApp((s) => s.copySelectionStyle);
  const pasteStyle = useApp((s) => s.pasteSelectionStyle);
  const reorder = useApp((s) => s.reorderSelection);
  const lock = useApp((s) => s.setSelectionLocked);
  const align = useApp((s) => s.alignSelection);
  const distribute = useApp((s) => s.distributeSelection);
  const updateStyle = useApp((s) => s.updateSelectionStyle);
  const rememberColor = useApp((s) => s.rememberColor);

  const selectedLayers = art?.layers.filter((l) => selectedIds.includes(l.layer_id)) ?? [];
  const selectedCount = selectedLayers.length;
  const anyUnlocked = selectedLayers.some((l) => !l.locked);
  const allLocked = selectedLayers.length > 0 && selectedLayers.every((l) => l.locked);
  const editableSelected = selectedLayers.filter((l) => !l.locked);
  const allText = editableSelected.length > 0 && editableSelected.every((l) => l.kind === "text");
  const firstText = allText ? editableSelected[0] : null;
  const textColor = firstText?.effects?.fill ?? "#17130f";
  const textWeight = firstText?.font_weight ?? 400;
  const textSize = firstText?.font_size_px ?? 24;

  return (
    <div className="flex items-center gap-1">
      <ToolbarButton title="Undo" disabled={!history.past.length} onClick={undo}>
        <I.Undo width={13} height={13} />
      </ToolbarButton>
      <ToolbarButton title="Redo" disabled={!history.future.length} onClick={redo}>
        <I.Redo width={13} height={13} />
      </ToolbarButton>
      <span className="mx-1 h-4 w-px bg-ink-300" />
      <ToolbarButton title="Copy" disabled={!anyUnlocked} onClick={copy}>
        <I.Copy width={13} height={13} />
      </ToolbarButton>
      <ToolbarButton title="Paste" disabled={!clipboardCount} onClick={paste}>
        <I.Clipboard width={13} height={13} />
      </ToolbarButton>
      <ToolbarButton title="Copy style" disabled={!anyUnlocked} onClick={copyStyle}>
        <I.Paintbrush width={13} height={13} />
      </ToolbarButton>
      <ToolbarButton title="Paste style" disabled={!selectedCount || !styleClipboard} onClick={pasteStyle}>
        <I.Paintbrush width={13} height={13} className="-scale-x-100" />
      </ToolbarButton>
      {allText && firstText && (
        <>
          <span className="mx-1 h-4 w-px bg-ink-300" />
          <ToolbarButton
            title="Bold"
            active={textWeight >= 700}
            onClick={() => updateStyle({ font_weight: textWeight >= 700 ? 400 : 700 })}
          >
            <I.Bold width={13} height={13} />
          </ToolbarButton>
          <ToolbarButton
            title="Italic"
            active={firstText.font_style === "italic"}
            onClick={() => updateStyle({ font_style: firstText.font_style === "italic" ? "normal" : "italic" })}
          >
            <I.Italic width={13} height={13} />
          </ToolbarButton>
          <ToolbarButton
            title="Decrease font size"
            onClick={() => updateStyle({ font_size_px: Math.max(8, textSize - 2) })}
          >
            A-
          </ToolbarButton>
          <ToolbarButton
            title="Increase font size"
            onClick={() => updateStyle({ font_size_px: Math.min(400, textSize + 2) })}
          >
            A+
          </ToolbarButton>
          <label
            title="Text color"
            className="relative inline-flex h-7 min-w-7 cursor-pointer items-center justify-center rounded-md px-1.5 transition hover:bg-ink-100"
          >
            <span className="h-3.5 w-3.5 rounded-full border border-ink-300" style={{ background: textColor }} />
            <input
              type="color"
              value={/^#[0-9a-f]{6}$/i.test(textColor) ? textColor : "#17130f"}
              onChange={(e) => {
                rememberColor(e.target.value);
                updateStyle({ effects: { fill: e.target.value } });
              }}
              className="absolute inset-0 h-full w-full cursor-pointer opacity-0"
            />
          </label>
          <ToolbarButton title="Align left" active={firstText.align === "left"} onClick={() => updateStyle({ align: "left" })}>
            <I.AlignLeft width={13} height={13} />
          </ToolbarButton>
          <ToolbarButton title="Align center" active={firstText.align === "center"} onClick={() => updateStyle({ align: "center" })}>
            <I.AlignCenter width={13} height={13} />
          </ToolbarButton>
          <ToolbarButton title="Align right" active={firstText.align === "right"} onClick={() => updateStyle({ align: "right" })}>
            <I.AlignRight width={13} height={13} />
          </ToolbarButton>
          <ToolbarButton
            title="Uppercase"
            active={firstText.text_transform === "uppercase"}
            onClick={() => updateStyle({ text_transform: firstText.text_transform === "uppercase" ? "none" : "uppercase" })}
          >
            TT
          </ToolbarButton>
          <ToolbarButton
            title="Bullet list"
            active={firstText.list_style === "bullet"}
            onClick={() => updateStyle({ list_style: firstText.list_style === "bullet" ? "none" : "bullet" })}
          >
            List
          </ToolbarButton>
        </>
      )}
      <span className="mx-1 h-4 w-px bg-ink-300" />
      <ToolbarButton title="Duplicate" disabled={!anyUnlocked} onClick={duplicate}>
        <I.Duplicate width={13} height={13} />
      </ToolbarButton>
      <ToolbarButton title="Delete" disabled={!anyUnlocked} onClick={remove}><I.Trash width={13} height={13} /></ToolbarButton>
      <ToolbarButton title={allLocked ? "Unlock" : "Lock"} disabled={!selectedCount} onClick={() => lock(!allLocked)}>
        {allLocked ? <I.Unlock width={13} height={13} /> : <I.Lock width={13} height={13} />}
      </ToolbarButton>
      <ToolbarButton title="Bring forward" disabled={!selectedCount} onClick={() => reorder("up")}><I.ChevronUp width={13} height={13} /></ToolbarButton>
      <ToolbarButton title="Send backward" disabled={!selectedCount} onClick={() => reorder("down")}><I.ChevronDown width={13} height={13} /></ToolbarButton>
      <ToolbarMenu
        label="Align"
        disabled={!anyUnlocked}
        items={[
          ["Left", () => align("left", activeFrame)],
          ["Center", () => align("center", activeFrame)],
          ["Right", () => align("right", activeFrame)],
          ["Top", () => align("top", activeFrame)],
          ["Middle", () => align("middle", activeFrame)],
          ["Bottom", () => align("bottom", activeFrame)],
        ]}
      />
      <ToolbarMenu
        label="Dist"
        disabled={selectedCount < 3}
        items={[
          ["Horizontal", () => distribute("horizontal")],
          ["Vertical", () => distribute("vertical")],
        ]}
      />
      <ViewMenu />
      {selectedCount > 1 && (
        <span className="ml-1 rounded-sm bg-ink-100 px-1.5 py-0.5 text-[10px] font-medium text-ink-600">
          {selectedCount}
        </span>
      )}
      <span className="mx-1 h-4 w-px bg-ink-300" />
    </div>
  );
}

function ToolbarButton({
  title,
  disabled,
  active,
  onClick,
  children,
}: {
  title: string;
  disabled?: boolean;
  active?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      title={title}
      disabled={disabled}
      onClick={onClick}
      className={`icon-btn h-7 min-w-7 px-1.5 text-[10.5px] font-medium ${active ? "icon-btn-active" : ""}`}
    >
      {children}
    </button>
  );
}

function ToolbarMenu({
  label,
  disabled,
  items,
}: {
  label: string;
  disabled?: boolean;
  items: Array<[string, () => void]>;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", close);
    return () => window.removeEventListener("mousedown", close);
  }, [open]);
  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((v) => !v)}
        className="inline-flex h-7 items-center gap-1 rounded-md px-2 text-[10.5px] font-medium text-ink-500 transition hover:bg-ink-100 hover:text-ink-900 disabled:cursor-not-allowed disabled:opacity-40"
      >
        {label}
        <I.ChevronDown width={10} height={10} />
      </button>
      {open && (
        <div className="absolute right-0 top-[calc(100%+4px)] z-50 min-w-32 overflow-hidden rounded-md border border-ink-300/70 bg-surface-raised py-1 shadow-soft">
          {items.map(([name, action]) => (
            <button
              key={name}
              type="button"
              onMouseDown={(e) => {
                e.preventDefault();
                action();
                setOpen(false);
              }}
              className="block w-full px-3 py-1.5 text-left text-[11px] text-ink-700 transition hover:bg-ink-100 hover:text-ink-900"
            >
              {name}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function ViewMenu() {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  const grid = useApp((s) => s.grid_visible);
  const rulers = useApp((s) => s.rulers_visible);
  const safe = useApp((s) => s.safe_margins_visible);
  const smart = useApp((s) => s.smart_guides_visible);
  const gridSize = useApp((s) => s.grid_size_px);
  const safePct = useApp((s) => s.safe_margin_pct);
  const toggleGrid = useApp((s) => s.toggleGrid);
  const toggleRulers = useApp((s) => s.toggleRulers);
  const toggleSafe = useApp((s) => s.toggleSafeMargins);
  const toggleSmart = useApp((s) => s.toggleSmartGuides);
  const setGridSize = useApp((s) => s.setGridSize);
  const setSafePct = useApp((s) => s.setSafeMarginPct);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", close);
    return () => window.removeEventListener("mousedown", close);
  }, [open]);

  const toggleItems: Array<[string, boolean, () => void]> = [
    ["Grid", grid, toggleGrid],
    ["Rulers", rulers, toggleRulers],
    ["Safe margins", safe, toggleSafe],
    ["Smart guides", smart, toggleSmart],
  ];

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="inline-flex h-7 items-center gap-1 rounded-md px-2 text-[10.5px] font-medium text-ink-500 transition hover:bg-ink-100 hover:text-ink-900"
      >
        View
        <I.ChevronDown width={10} height={10} />
      </button>
      {open && (
        <div className="absolute right-0 top-[calc(100%+4px)] z-50 w-52 overflow-hidden rounded-md border border-ink-300/70 bg-surface-raised py-2 shadow-soft">
          {toggleItems.map(([name, active, action]) => (
            <button
              key={name}
              type="button"
              onMouseDown={(e) => {
                e.preventDefault();
                action();
              }}
              className="flex w-full items-center justify-between px-3 py-1.5 text-left text-[11px] text-ink-700 transition hover:bg-ink-100 hover:text-ink-900"
            >
              <span>{name}</span>
              <span className="tabular text-[10px] text-ink-500">{active ? "On" : "Off"}</span>
            </button>
          ))}
          <div className="my-1 border-t border-ink-200/70" />
          <div className="px-3 py-1 text-[10px] uppercase text-ink-500" style={{ letterSpacing: "0.12em" }}>Grid size</div>
          <div className="grid grid-cols-5 gap-1 px-3 pb-2">
            {[4, 8, 12, 16, 24].map((n) => (
              <button
                key={n}
                type="button"
                onMouseDown={(e) => {
                  e.preventDefault();
                  setGridSize(n);
                }}
                className={`rounded border px-1.5 py-1 text-[10px] ${
                  gridSize === n
                    ? "border-ink-900 bg-ink-900 text-white"
                    : "border-ink-300/70 text-ink-600 hover:border-ink-700"
                }`}
              >
                {n}
              </button>
            ))}
          </div>
          <div className="px-3 py-1 text-[10px] uppercase text-ink-500" style={{ letterSpacing: "0.12em" }}>Safe margin</div>
          <div className="grid grid-cols-4 gap-1 px-3">
            {[0.04, 0.06, 0.08, 0.1].map((n) => (
              <button
                key={n}
                type="button"
                onMouseDown={(e) => {
                  e.preventDefault();
                  setSafePct(n);
                }}
                className={`rounded border px-1.5 py-1 text-[10px] ${
                  Math.abs(safePct - n) < 0.001
                    ? "border-ink-900 bg-ink-900 text-white"
                    : "border-ink-300/70 text-ink-600 hover:border-ink-700"
                }`}
              >
                {Math.round(n * 100)}%
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function SelectionOverlay({
  layers,
  scale,
  visibleLayers,
  activeFrame,
  smartGuides,
  setSnapGuides,
}: {
  layers: Layer[];
  scale: number;
  visibleLayers: Layer[];
  activeFrame?: Bbox;
  smartGuides: boolean;
  setSnapGuides: (guides: SnapGuide[]) => void;
}) {
  const updateLayer = useApp((s) => s.updateLayer);
  const editable = layers.filter((l) => l.bbox && !l.locked);
  const box = layerBounds(layers.filter((l) => l.bbox));
  if (!box) return null;
  const canResize = editable.length > 0;
  const handleSize = Math.max(12, 14 / scale);
  const borderWidth = Math.max(2, 2.8 / scale);
  const outlineOffset = Math.max(4, 4 / scale);
  const haloWidth = Math.max(7, 6 / scale);
  const handles: Array<{ id: ResizeDir; x: number; y: number; cursor: string }> = [
    { id: "nw", x: 0, y: 0, cursor: "nwse-resize" },
    { id: "n", x: 0.5, y: 0, cursor: "ns-resize" },
    { id: "ne", x: 1, y: 0, cursor: "nesw-resize" },
    { id: "e", x: 1, y: 0.5, cursor: "ew-resize" },
    { id: "se", x: 1, y: 1, cursor: "nwse-resize" },
    { id: "s", x: 0.5, y: 1, cursor: "ns-resize" },
    { id: "sw", x: 0, y: 1, cursor: "nesw-resize" },
    { id: "w", x: 0, y: 0.5, cursor: "ew-resize" },
  ];

  const startResize = (dir: ResizeDir, e: React.MouseEvent) => {
    if (!canResize) return;
    e.stopPropagation();
    e.preventDefault();
    const resizable = editable;
    const startBox = layerBounds(resizable);
    if (!startBox) return;
    const movingIds = new Set(resizable.map((l) => l.layer_id));
    const origins = new Map(resizable.map((l) => [l.layer_id, l.bbox!]));
    const startX = e.clientX;
    const startY = e.clientY;
    const aspect = startBox.w / Math.max(1, startBox.h);
    let recorded = false;

    const onMove = (ev: MouseEvent) => {
      const dx = (ev.clientX - startX) / scale;
      const dy = (ev.clientY - startY) / scale;
      if (!recorded && (Math.abs(dx) > 0.5 || Math.abs(dy) > 0.5)) {
        useApp.getState().captureHistorySnapshot();
        recorded = true;
      }
      if (!recorded) return;
      const next = resizeBox(startBox, dir, dx, dy, ev.shiftKey, aspect);
      const snapped = smartGuides
        ? snapBox(next, movingIds, visibleLayers, activeFrame)
        : { box: next, guides: [] };
      setSnapGuides(smartGuides ? snapped.guides : []);
      const sx = snapped.box.w / Math.max(1, startBox.w);
      const sy = snapped.box.h / Math.max(1, startBox.h);
      for (const [id, b] of origins) {
        updateLayer(
          id,
          {
            bbox: {
              x: Math.round(snapped.box.x + (b.x - startBox.x) * sx),
              y: Math.round(snapped.box.y + (b.y - startBox.y) * sy),
              w: Math.max(MIN_BOX_SIZE, Math.round(b.w * sx)),
              h: Math.max(MIN_BOX_SIZE, Math.round(b.h * sy)),
            },
          },
          { history: false },
        );
      }
    };
    const onUp = () => {
      setSnapGuides([]);
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };

  return (
    <div
      className="pointer-events-none absolute z-40"
      style={{
        left: box.x,
        top: box.y,
        width: box.w,
        height: box.h,
        outline: `${borderWidth}px solid ${SELECTION_BLUE}`,
        outlineOffset,
        boxShadow: [
          `0 0 0 ${haloWidth}px ${SELECTION_HALO}`,
          `0 0 0 ${haloWidth + borderWidth}px rgba(14, 165, 233, 0.36)`,
          `0 18px 70px rgba(3, 105, 161, 0.22)`,
        ].join(", "),
      }}
    >
      {canResize &&
        handles.map((h) => (
          <button
            key={h.id}
            type="button"
            aria-label={`Resize ${h.id}`}
            onMouseDown={(e) => startResize(h.id, e)}
            className="pointer-events-auto absolute rounded-sm border-2 border-white shadow-md"
            style={{
              width: handleSize,
              height: handleSize,
              left: `calc(${h.x * 100}% - ${handleSize / 2}px)`,
              top: `calc(${h.y * 100}% - ${handleSize / 2}px)`,
              cursor: h.cursor,
              background: SELECTION_BLUE,
              boxShadow: `0 0 0 ${Math.max(2, 2 / scale)}px rgba(3, 105, 161, 0.35)`,
            }}
          />
        ))}
    </div>
  );
}

type ResizeDir = "nw" | "n" | "ne" | "e" | "se" | "s" | "sw" | "w";

function resizeBox(box: Bbox, dir: ResizeDir, dx: number, dy: number, keepRatio: boolean, aspect: number): Bbox {
  let x = box.x;
  let y = box.y;
  let w = box.w;
  let h = box.h;
  if (dir.includes("e")) w = box.w + dx;
  if (dir.includes("s")) h = box.h + dy;
  if (dir.includes("w")) {
    x = box.x + dx;
    w = box.w - dx;
  }
  if (dir.includes("n")) {
    y = box.y + dy;
    h = box.h - dy;
  }
  w = Math.max(MIN_BOX_SIZE, w);
  h = Math.max(MIN_BOX_SIZE, h);
  if (keepRatio && dir.length === 2) {
    if (Math.abs(dx) > Math.abs(dy)) h = w / aspect;
    else w = h * aspect;
    if (dir.includes("w")) x = box.x + box.w - w;
    if (dir.includes("n")) y = box.y + box.h - h;
  }
  return { x, y, w, h };
}

function SnapGuides({ guides, scale }: { guides: SnapGuide[]; scale: number }) {
  if (!guides.length) return null;
  const width = Math.max(1, 1 / scale);
  return (
    <>
      {guides.map((g, idx) => (
        <div
          key={`${g.axis}-${g.pos}-${idx}`}
          className="pointer-events-none absolute z-50 bg-accent"
          style={
            g.axis === "x"
              ? { left: g.pos, top: -100000, width, height: 200000 }
              : { left: -100000, top: g.pos, width: 200000, height: width }
          }
        />
      ))}
    </>
  );
}

function CanvasGridOverlay({ frame, scale }: { frame: Bbox; scale: number }) {
  const gridSize = useApp((s) => s.grid_size_px);
  const majorEvery = useApp((s) => s.grid_major_every);
  const minor = Math.max(4, gridSize);
  const major = minor * Math.max(1, majorEvery);
  const stroke = Math.max(0.5, 1 / scale);
  const majorStroke = Math.max(0.75, 1.25 / scale);
  return (
    <div
      className="pointer-events-none absolute z-0"
      style={{
        left: frame.x,
        top: frame.y,
        width: frame.w,
        height: frame.h,
        backgroundImage: [
          `linear-gradient(to right, rgba(24, 20, 16, 0.04) ${stroke}px, transparent ${stroke}px)`,
          `linear-gradient(to bottom, rgba(24, 20, 16, 0.04) ${stroke}px, transparent ${stroke}px)`,
          `linear-gradient(to right, rgba(23, 100, 72, 0.09) ${majorStroke}px, transparent ${majorStroke}px)`,
          `linear-gradient(to bottom, rgba(23, 100, 72, 0.09) ${majorStroke}px, transparent ${majorStroke}px)`,
        ].join(", "),
        backgroundSize: `${minor}px ${minor}px, ${minor}px ${minor}px, ${major}px ${major}px, ${major}px ${major}px`,
      }}
    />
  );
}

function SafeMarginOverlay({ frame, scale }: { frame: Bbox; scale: number }) {
  const pct = useApp((s) => s.safe_margin_pct);
  const mx = Math.round(frame.w * pct);
  const my = Math.round(frame.h * pct);
  return (
    <div
      className="pointer-events-none absolute z-10"
      style={{
        left: frame.x + mx,
        top: frame.y + my,
        width: Math.max(0, frame.w - mx * 2),
        height: Math.max(0, frame.h - my * 2),
        border: `${Math.max(0.75, 0.8 / scale)}px dashed rgba(146, 52, 46, 0.38)`,
        boxShadow: `0 0 0 ${Math.max(0.75, 0.8 / scale)}px rgba(255,255,255,0.18) inset`,
      }}
    />
  );
}

function RulerOverlay({ frame, scale }: { frame: Bbox; scale: number }) {
  const gridSize = useApp((s) => s.grid_size_px);
  const majorEvery = useApp((s) => s.grid_major_every);
  const major = gridSize * Math.max(1, majorEvery);
  const ticksX: number[] = [];
  const ticksY: number[] = [];
  for (let x = 0; x <= frame.w; x += major) ticksX.push(x);
  for (let y = 0; y <= frame.h; y += major) ticksY.push(y);
  const rulerH = 24;
  const rulerW = 34;
  const showLabels = scale >= 0.35;
  return (
    <div className="pointer-events-none absolute inset-0 z-[60] text-[10px] text-ink-500">
      <div
        className="absolute top-0 border-b border-ink-300/70 bg-paper/90 backdrop-blur-sm"
        style={{ left: rulerW, width: frame.w * scale, height: rulerH }}
      >
        {ticksX.map((x) => (
          <div
            key={`x-${x}`}
            className="absolute bottom-0 border-l border-ink-400/70"
            style={{ left: x * scale, height: x === 0 ? rulerH : 9 }}
          >
            {showLabels && x % (major * 2) === 0 && (
              <span className="absolute left-1 top-1 tabular">{x}</span>
            )}
          </div>
        ))}
      </div>
      <div
        className="absolute left-0 border-r border-ink-300/70 bg-paper/90 backdrop-blur-sm"
        style={{ top: rulerH, width: rulerW, height: frame.h * scale }}
      >
        {ticksY.map((y) => (
          <div
            key={`y-${y}`}
            className="absolute right-0 border-t border-ink-400/70"
            style={{ top: y * scale, width: y === 0 ? rulerW : 9 }}
          >
            {showLabels && y % (major * 2) === 0 && (
              <span
                className="absolute left-1 top-1 tabular"
                style={{ writingMode: "vertical-rl" }}
              >
                {y}
              </span>
            )}
          </div>
        ))}
      </div>
      <div
        className="absolute left-0 top-0 border-b border-r border-ink-300/70 bg-paper/95"
        style={{ width: rulerW, height: rulerH }}
      />
    </div>
  );
}

function pendingInsertBounds(pending: PendingInsert, frame: Bbox): Bbox | null {
  const layers = pending.layers.map((l) => {
    if (pending.placement !== "frame-relative" || !l.bbox) return l;
    return {
      ...l,
      bbox: {
        x: frame.x + l.bbox.x * frame.w,
        y: frame.y + l.bbox.y * frame.h,
        w: l.bbox.w * frame.w,
        h: l.bbox.h * frame.h,
      },
    };
  });
  return layerBounds(layers);
}

function PendingInsertPreview({
  pending,
  point,
  frame,
  scale,
}: {
  pending: PendingInsert;
  point: { x: number; y: number } | null;
  frame: Bbox;
  scale: number;
}) {
  const bounds = pendingInsertBounds(pending, frame);
  if (!bounds) return null;
  const anchor = point ?? {
    x: frame.x + frame.w / 2,
    y: frame.y + frame.h / 2,
  };
  const w = bounds.w * scale;
  const h = bounds.h * scale;
  return (
    <div
      className="pointer-events-none absolute z-[70] rounded-sm border border-dashed border-accent bg-accent/10 shadow-[0_0_0_1px_rgba(255,255,255,0.75)_inset]"
      style={{
        left: (anchor.x - frame.x) * scale - w / 2,
        top: (anchor.y - frame.y) * scale - h / 2,
        width: w,
        height: h,
      }}
    >
      <div className="absolute left-1 top-1 rounded-sm bg-ink-900 px-1.5 py-0.5 text-[10px] font-medium text-white">
        Click to place
      </div>
    </div>
  );
}

function MarqueeRect({ rect, scale }: { rect: Marquee; scale: number }) {
  return (
    <div
      className="pointer-events-none absolute z-50 border border-accent bg-accent/10"
      style={{
        left: rect.x,
        top: rect.y,
        width: rect.w,
        height: rect.h,
        borderWidth: Math.max(1, 1 / scale),
      }}
    />
  );
}

function LayerView({
  layer,
  art,
  selected,
  selectedIds,
  visibleLayers,
  activeFrame,
  smartGuides,
  scale,
  onSelect,
  setSnapGuides,
}: {
  layer: Layer;
  art: Artifact;
  selected: boolean;
  selectedIds: string[];
  visibleLayers: Layer[];
  activeFrame?: Bbox;
  smartGuides: boolean;
  scale: number;
  onSelect: (mode: "replace" | "toggle") => void;
  setSnapGuides: (guides: SnapGuide[]) => void;
}) {
  const updateLayer = useApp((s) => s.updateLayer);
  // Inline text-edit state. Entered via double-click on a text layer;
  // committed on blur or Enter; reverted on Escape.
  const [editing, setEditing] = useState(false);
  const [cropEditing, setCropEditing] = useState(false);
  const textElRef = useRef<HTMLDivElement | null>(null);

  // Seed the contentEditable's DOM imperatively when entering edit mode
  // (React stops rendering its children while editing, so the DOM is
  // the source of truth) + focus and select all so the user can just
  // start typing.
  useEffect(() => {
    if (!editing || !textElRef.current) return;
    const el = textElRef.current;
    el.innerText = layer.text ?? "";
    el.focus();
    const range = document.createRange();
    range.selectNodeContents(el);
    const sel = window.getSelection();
    sel?.removeAllRanges();
    sel?.addRange(range);
  }, [editing, layer.text]);

  // If the user clicks elsewhere and the layer goes unselected, drop
  // edit mode too. Prevents a stuck "still editing but invisible" state.
  useEffect(() => {
    if (!selected) {
      setEditing(false);
      setCropEditing(false);
    }
  }, [selected]);

  if (layer.visible === false) return null;
  const { bbox } = layer;
  if (!bbox) return null;

  const interactive = !layer.locked;
  const layerDomAttrs = {
    "data-layer-id": layer.layer_id,
    "data-layer-kind": layer.kind,
  };
  const selectedOutlineWidth = Math.max(2, 2.4 / scale);
  const selectedOutlineOffset = Math.max(3, 3 / scale);

  const baseStyle: React.CSSProperties = {
    position: "absolute",
    left: bbox.x,
    top: bbox.y,
    width: bbox.w,
    height: bbox.h,
    pointerEvents: interactive ? undefined : "none",
    cursor: interactive ? "pointer" : "default",
    outline: selected ? `${selectedOutlineWidth}px solid ${SELECTION_BLUE}` : undefined,
    outlineOffset: selected ? selectedOutlineOffset : undefined,
  };

  // Draggable position update — disabled while editing text so the
  // user's mouse selects characters instead of repositioning the layer.
  const onMouseDown = (e: React.MouseEvent) => {
    if (!interactive || editing) return;
    e.stopPropagation();
    if (e.shiftKey) {
      onSelect("toggle");
      return;
    }
    if (!selected) onSelect("replace");
    const ids = selected ? selectedIds : [layer.layer_id];
    const movingIds = new Set(ids);
    const movingLayers = art.layers.filter(
      (l) => movingIds.has(l.layer_id) && l.bbox && !l.locked
    );
    const group = layerBounds(movingLayers);
    if (!group) return;
    const startX = e.clientX;
    const startY = e.clientY;
    const origins = new Map(
      movingLayers.map((l) => [l.layer_id, l.bbox!])
    );
    let recorded = false;

    const move = (ev: MouseEvent) => {
      const dx = (ev.clientX - startX) / scale;
      const dy = (ev.clientY - startY) / scale;
      if (!recorded && (Math.abs(dx) > 0.5 || Math.abs(dy) > 0.5)) {
        useApp.getState().captureHistorySnapshot();
        recorded = true;
      }
      if (!recorded) return;
      const desired = { ...group, x: group.x + dx, y: group.y + dy };
      const snapped = smartGuides
        ? snapBox(desired, movingIds, visibleLayers, activeFrame)
        : { box: desired, guides: [] };
      setSnapGuides(smartGuides ? snapped.guides : []);
      const finalDx = snapped.box.x - group.x;
      const finalDy = snapped.box.y - group.y;
      for (const [id, b] of origins) {
        updateLayer(
          id,
          { bbox: { ...b, x: Math.round(b.x + finalDx), y: Math.round(b.y + finalDy) } },
          { history: false },
        );
      }
    };
    const up = () => {
      setSnapGuides([]);
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };

  const onImageMouseDown = (e: React.MouseEvent) => {
    const canCrop = layer.kind === "image" && (layer.fit ?? "cover") === "cover";
    if (!canCrop || !selected || (!cropEditing && !e.altKey)) {
      onMouseDown(e);
      return;
    }
    e.preventDefault();
    e.stopPropagation();
    const startX = e.clientX;
    const startY = e.clientY;
    const start = layer.object_position ?? { x: 0.5, y: 0.5 };
    let recorded = false;
    const move = (ev: MouseEvent) => {
      const dx = (ev.clientX - startX) / Math.max(1, bbox.w * scale);
      const dy = (ev.clientY - startY) / Math.max(1, bbox.h * scale);
      if (!recorded && (Math.abs(dx) > 0.002 || Math.abs(dy) > 0.002)) {
        useApp.getState().captureHistorySnapshot();
        recorded = true;
      }
      if (!recorded) return;
      updateLayer(
        layer.layer_id,
        { object_position: { x: clamp01(start.x + dx), y: clamp01(start.y + dy) } },
        { history: false },
      );
    };
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };

  const commitTextEdit = () => {
    const el = textElRef.current;
    if (!el) {
      setEditing(false);
      return;
    }
    const newText = el.innerText;
    if (newText !== (layer.text ?? "")) {
      updateLayer(layer.layer_id, { text: newText });
    }
    setEditing(false);
  };

  const cancelTextEdit = () => {
    // Revert by leaving editing mode — React re-renders with the
    // original layer.text as children, blowing away DOM mutations.
    setEditing(false);
  };

  if (layer.kind === "background" || layer.kind === "shape") {
    const shape = layer.shape_kind ?? "rect";
    const isEllipse = shape === "ellipse";
    const isLineLike = shape === "line" || shape === "arrow";
    if (isLineLike) {
      const strokeWidth = Math.max(1, layer.stroke_width ?? Math.max(1, bbox.h));
      const strokeColor = layer.stroke_color ?? layer.fill_color ?? "#17130f";
      return (
        <div
          {...layerDomAttrs}
          style={{
            ...baseStyle,
            opacity: layer.opacity ?? 1,
            boxShadow: layerShadowCss(layer),
          }}
          onMouseDown={onMouseDown}
        >
          <div
            style={{
              position: "absolute",
              left: 0,
              right: shape === "arrow" ? strokeWidth * 1.75 : 0,
              top: "50%",
              borderTop: `${strokeWidth}px ${layer.stroke_dash ?? "solid"} ${strokeColor}`,
              transform: "translateY(-50%)",
            }}
          />
          {shape === "arrow" && (
            <div
              style={{
                position: "absolute",
                right: 0,
                top: "50%",
                width: strokeWidth * 2.5,
                height: strokeWidth * 2.5,
                borderTop: `${strokeWidth}px ${layer.stroke_dash ?? "solid"} ${strokeColor}`,
                borderRight: `${strokeWidth}px ${layer.stroke_dash ?? "solid"} ${strokeColor}`,
                transform: "translateY(-50%) rotate(45deg)",
                transformOrigin: "center",
              }}
            />
          )}
        </div>
      );
    }
    return (
      <div
        {...layerDomAttrs}
        style={{
          ...baseStyle,
          background: layer.fill_color,
          borderRadius: isEllipse ? "9999px" : (layer.corner_radius ?? 0),
          opacity: layer.opacity ?? 1,
          boxShadow: layerShadowCss(layer),
          border:
            layer.stroke_color && (layer.stroke_width ?? 0) > 0
              ? `${layer.stroke_width}px ${layer.stroke_dash ?? "solid"} ${layer.stroke_color}`
              : undefined,
        }}
        onMouseDown={onMouseDown}
      />
    );
  }

  if (layer.kind === "text") {
    return (
      <div
        {...layerDomAttrs}
        ref={textElRef}
        contentEditable={editing}
        suppressContentEditableWarning
        spellCheck={editing}
        style={{
          ...baseStyle,
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
          textShadow: textShadowCss(layer),
          color: layer.effects?.fill,
          whiteSpace: "pre-wrap",
          padding: 2,
          // Visual cues: thicker outline + text caret while editing.
          cursor: editing ? "text" : (interactive ? "pointer" : "default"),
          outline: editing
            ? `${Math.max(2, 2.4 / scale)}px solid ${SELECTION_BLUE_DARK}`
            : selected
              ? `${selectedOutlineWidth}px solid ${SELECTION_BLUE}`
              : undefined,
          outlineOffset: editing ? selectedOutlineOffset : selected ? selectedOutlineOffset : undefined,
        }}
        onMouseDown={(e) => {
          if (editing) {
            // Let the browser handle caret placement; don't drag/reselect.
            e.stopPropagation();
            return;
          }
          onMouseDown(e);
        }}
        onDoubleClick={(e) => {
          if (!interactive) return;
          e.stopPropagation();
          if (!selected) onSelect("replace");
          setEditing(true);
        }}
        onBlur={editing ? commitTextEdit : undefined}
        onKeyDown={(e) => {
          if (!editing) return;
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            commitTextEdit();
          } else if (e.key === "Escape") {
            e.preventDefault();
            cancelTextEdit();
          }
          // Stop propagation so parent shortcuts (e.g. Backspace =
          // delete layer) don't fire while we're typing.
          e.stopPropagation();
        }}
      >
        {/* Children only when NOT editing — DOM owns the content during
            edit, set imperatively in the useEffect above. */}
        {!editing &&
          (layer.list_style === "bullet" ? (
            <ul style={{ margin: 0, paddingLeft: "1.15em" }}>
              {(layer.text ?? "")
                .split("\n")
                .filter((line) => line.trim().length)
                .map((line, idx) => (
                  <li key={`${layer.layer_id}-li-${idx}`}>{line}</li>
                ))}
            </ul>
          ) : (
            layer.text ?? ""
          ))}
      </div>
    );
  }

  if (layer.kind === "image") {
    return (
      <img
        {...layerDomAttrs}
        src={layer.src}
        alt={layer.name}
        style={{
          ...baseStyle,
          objectFit: layer.fit ?? "cover",
          objectPosition: objectPositionCss(layer),
          borderRadius: layer.corner_radius ?? 0,
          opacity: layer.opacity ?? 1,
          boxShadow: layerShadowCss(layer),
          cursor: cropEditing ? "move" : baseStyle.cursor,
          outline: cropEditing
            ? `${Math.max(2, 2.4 / scale)}px dashed ${SELECTION_BLUE_DARK}`
            : baseStyle.outline,
        }}
        onMouseDown={onImageMouseDown}
        onDoubleClick={(e) => {
          if (!interactive || (layer.fit ?? "cover") !== "cover") return;
          e.stopPropagation();
          if (!selected) onSelect("replace");
          setCropEditing((v) => !v);
        }}
      />
    );
  }

  return null;
}

/**
 * Drag handler that listens to the synthetic event LayerView fires.
 * Mounted once at the top of Canvas mode.
 */
export function CanvasDragBridge() {
  const updateLayer = useApp((s) => s.updateLayer);
  useEffect(() => {
    const onDrag = (e: Event) => {
      const ev = e as CustomEvent<{ layer_id: string; x: number; y: number }>;
      const { layer_id, x, y } = ev.detail;
      const s = useApp.getState();
      const c = s.conversations[s.current_conversation_id];
      const id = c?.active_artifact_id;
      if (!c || !id) return;
      const layer = c.artifacts[id]?.layers.find(
        (l) => l.layer_id === layer_id
      );
      if (!layer || !layer.bbox) return;
      updateLayer(layer_id, {
        bbox: { ...layer.bbox, x, y },
      });
    };
    window.addEventListener("layer:drag", onDrag);
    return () => window.removeEventListener("layer:drag", onDrag);
  }, [updateLayer]);
  return null;
}
