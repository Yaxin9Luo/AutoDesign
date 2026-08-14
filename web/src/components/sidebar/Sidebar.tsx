import { useEffect, useRef, useState } from "react";
import {
  artifactTypeForArtifact,
  useActiveArtifact,
  useApp,
  useCurrentConversation,
  useSelectedLayer,
} from "@/lib/store";
import { attemptRunIdForConversation } from "@/lib/attempt_candidates";
import { translate } from "@/lib/i18n";
import { encodePaperAssetDrag, PAPER_ASSET_DRAG_MIME } from "@/lib/paper_asset_drag";
import type { InsertPlacementMode } from "@/lib/store";
import { fetchArtifactAssets, uploadEditorAsset } from "@/lib/api";
import type { Align, Artifact, ArtifactAsset, Layer, LayerGroup, ShapeKind } from "@/lib/types";
import { isClientDemoArtifact, nextId } from "@/lib/mock";
import { detectSlideFrames } from "@/lib/slide_frames";
import { I } from "../icons";
import { ResizeHandle } from "../ResizeHandle";
import { ArtifactDownloadMenu } from "../ArtifactDownloadMenu";
import { AttemptInspector } from "../AttemptInspector";
import {
  ColorField,
  Field,
  NumberField,
  PanelSection,
  SegGroup,
  SelectField,
  SliderField,
} from "./atoms";

type Tab = "design" | "layers" | "insert" | "attempts";
type RelBox = { x: number; y: number; w: number; h: number };
type CropPoint = { x: number; y: number };

const DEFAULT_SHADOW = {
  color: "#17130f",
  dx: 0,
  dy: 12,
  blur: 28,
  opacity: 0.18,
};

const SHADOW_PRESETS = {
  none: null,
  soft: DEFAULT_SHADOW,
  lift: { color: "#17130f", dx: 0, dy: 18, blur: 42, opacity: 0.24 },
} as const;

const clamp01 = (n: number) => Math.max(0, Math.min(1, n));

function layerCenterInBox(layer: Layer, box: { x: number; y: number; w: number; h: number }) {
  const b = layer.bbox;
  if (!b) return false;
  if (layer.kind === "background" && (b.w > box.w * 1.05 || b.h > box.h * 1.05)) {
    return false;
  }
  const cx = b.x + b.w / 2;
  const cy = b.y + b.h / 2;
  return cx >= box.x && cx <= box.x + box.w && cy >= box.y && cy <= box.y + box.h;
}

function assetDisplayName(asset: ArtifactAsset, t: (text: string) => string) {
  return asset.name
    .replace(/^Figure crop\b/, t("Figure crop"))
    .replace(/^Table crop\b/, t("Table crop"))
    .replace(/^Paper crop\b/, t("Paper crop"))
    .replace(/^Figure\b/, t("Figure"))
    .replace(/^Table\b/, t("Table"));
}

function activeFrameForArtifact(art: Artifact | null, activeSlideIdx: number) {
  if (!art) return null;
  const frames = detectSlideFrames(art);
  if (frames.length < 2) return null;
  return frames[Math.min(Math.max(0, activeSlideIdx), frames.length - 1)] ?? null;
}

function frameForLayer(art: Artifact | null, layer: Layer) {
  if (!art) return null;
  const frames = detectSlideFrames(art);
  if (frames.length < 2) return null;
  return frames.find((frame) => layerCenterInBox(layer, frame.bbox)) ?? null;
}

export function Sidebar() {
  const layer = useSelectedLayer();
  const art = useActiveArtifact();
  const conversation = useCurrentConversation();
  const attemptRunId = conversation
    ? attemptRunIdForConversation(conversation)
    : undefined;
  const [tab, setTab] = useState<Tab>(() => (
    art?.candidate_draft && attemptRunId ? "attempts" : "design"
  ));
  const selectedIds = useApp((s) => s.selected_layer_ids);
  const isNative = !!art?.native_file_url;
  const artType = art ? artifactTypeForArtifact(art) : null;
  // Native artifacts come in two shapes: editable (HTML — has a parsed
  // layer manifest, full editor available) and read-only (PPTX — no
  // manifest, download-only panel). The pure-mock path keeps working
  // for legacy fixtures that have layers but no native_file_url.
  const hasLayers = (art?.layers?.length ?? 0) > 0;
  const isEditableHtml =
    !!art?.native_file_url
    && (artType === "poster" || artType === "deck" || artType === "landing")
    && (art.view_format ?? art.native_format) === "html";
  const showFullEditor = artType !== "video" && (!isNative || hasLayers || isEditableHtml);
  const setSidebarWidth = useApp((s) => s.setSidebarWidth);
  const cancelPendingInsert = useApp((s) => s.cancelPendingInsert);
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  const switchTab = (next: Tab) => {
    if (next !== tab) cancelPendingInsert();
    setTab(next);
  };

  useEffect(() => {
    if (art?.candidate_draft && attemptRunId) setTab("attempts");
  }, [art?.artifact_id, attemptRunId]);

  useEffect(() => {
    if (!attemptRunId) {
      setTab((current) => current === "attempts" ? "design" : current);
    }
  }, [attemptRunId]);

  return (
    <aside className="app-panel relative flex h-full min-h-0 w-full shrink-0 flex-col border-l">
      <ResizeHandle
        side="left"
        getCurrentSize={() => useApp.getState().properties_sidebar_width}
        setSize={(px) => setSidebarWidth("properties", px)}
      />
      {(showFullEditor || attemptRunId) && (
        <div className="relative flex items-center border-b border-ink-300/55 bg-surface-raised/45">
          {showFullEditor && (
            <>
              <TabBtn active={tab === "design"} onClick={() => switchTab("design")}>
                {t("Design")}
              </TabBtn>
              <TabBtn active={tab === "layers"} onClick={() => switchTab("layers")}>
                {t("Layers")}
              </TabBtn>
              <TabBtn active={tab === "insert"} onClick={() => switchTab("insert")}>
                {t("Insert")}
              </TabBtn>
            </>
          )}
          {attemptRunId && (
            <TabBtn active={tab === "attempts"} onClick={() => switchTab("attempts")}>
              {t("Attempts")}
            </TabBtn>
          )}
        </div>
      )}
      <div className="flex-1 overflow-y-auto">
        {tab === "attempts" && attemptRunId ? (
          <AttemptInspector runId={attemptRunId} variant="panel" />
        ) : showFullEditor ? (
          <>
            {isNative && <NativeEditingHint />}
            {tab === "design" &&
              (selectedIds.length > 1 ? (
                <>
                  <MultiSelectionProperties />
                  <PaperAssetsSection />
                </>
              ) : layer ? (
                <PropertiesForLayer layer={layer} />
              ) : (
                <>
                  <CanvasProperties />
                  <PaperAssetsSection />
                </>
              ))}
            {tab === "layers" && <LayersList />}
            {tab === "insert" && <InsertPanel onInserted={() => setTab("design")} />}
          </>
        ) : (
          <NativeArtifactPanel />
        )}
      </div>
    </aside>
  );
}

/** Inspector hint shown above the editor tabs when the artifact is a
 *  parsed-from-HTML native file. Edits stay pending until the canvas
 *  toolbar Save button or Cmd+S commits them. */
function NativeEditingHint() {
  const art = useActiveArtifact();
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  if (!art) return null;
  return (
    <div className="border-b border-ink-300/55 bg-accent-soft/70 px-4 py-3">
      <div className="flex items-start gap-2.5 text-[11.5px] leading-relaxed text-ink-700">
        <span className="mt-1 inline-block h-1 w-1 shrink-0 rounded-full bg-accent" />
        <div>
          <span className="font-medium text-accent-deep">
            {translate(language, "Editing the agent's {format}.", { format: art.native_format?.toUpperCase() ?? "HTML" })}
          </span>{" "}
          {t("Click text to edit, or click images to move/replace them. Use Save or Cmd+S when ready.")}
        </div>
      </div>
    </div>
  );
}

function NativeArtifactPanel() {
  const art = useActiveArtifact();
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  if (!art) return null;
  const artType = artifactTypeForArtifact(art);
  const title =
    artType === "deck"
      ? "Slide deck"
      : artType === "poster"
        ? "HTML poster"
        : artType === "landing"
          ? "Landing page"
          : "Native artifact";
  // Reached only for native artifacts that don't have a parsed layer
  // manifest. Be explicit so users don't think poster edits are blocked.
  return (
    <PanelSection title={title}>
      {art.native_format === "html" && artType === "deck" ? (
        <p className="text-[12.5px] leading-relaxed text-ink-700">
          {t("This deck is an HTML-first slide canvas. Edit layers here, use chat for structural changes, or export a PPTX from Download.")}
        </p>
      ) : art.native_format === "html" && artType === "poster" ? (
        <p className="text-[12.5px] leading-relaxed text-ink-700">
          {t("This poster is an HTML paper poster. Click editable poster text on the canvas, or use chat for larger structural revisions.")}
        </p>
      ) : artType === "deck" ? (
        <p className="text-[12.5px] leading-relaxed text-ink-700">
          {t(art.native_format === "pptx"
            ? "This deck is a PowerPoint file. Download it to edit in PowerPoint or Keynote."
            : "Decks are HTML-first, editable slide canvases by default. This artifact does not expose parsed layers; use chat for structural changes, or export a PPTX from Download.")}
        </p>
      ) : (
        <p className="text-[12.5px] leading-relaxed text-ink-700">
          {t("This artifact does not expose a parsed layer manifest. Use chat for structural changes, or download the source file.")}
        </p>
      )}
      <div className="mt-3">
        <ArtifactDownloadMenu
          artifact={art}
          className="inline-flex items-center gap-1.5 rounded-md bg-ink-900 px-3 py-1.5 text-[11px] font-medium uppercase text-ink-50 hover:bg-ink-700"
        />
      </div>
      <div className="mt-5 rounded-md border border-ink-300/70 bg-vellum p-3.5 leading-relaxed">
        <div className="mb-1 font-display text-[15px] text-ink-900" style={{ fontVariationSettings: '"opsz" 36' }}>
          {art.name}
        </div>
        <div className="tabular text-[11px] uppercase text-ink-500" style={{ letterSpacing: "0.12em" }}>
          {artifactTypeForArtifact(art)} · {art.canvas.w}×{art.canvas.h} · {art.native_format}
        </div>
      </div>
    </PanelSection>
  );
}

function TabBtn({
  active,
  children,
  onClick,
}: {
  active: boolean;
  children: React.ReactNode;
  onClick: () => void;
}) {
  return (
    <button onClick={onClick} className={`tab-btn ${active ? "tab-btn-active" : ""}`}>
      {children}
    </button>
  );
}

// ============ Design tab ============

function PropertiesForLayer({ layer }: { layer: Layer }) {
  const language = useApp((s) => s.ui_language);
  let body;
  if (layer.kind === "text") body = <TextProperties layer={layer} />;
  else if (layer.kind === "image") body = <ImageProperties layer={layer} />;
  else if (layer.kind === "shape" || layer.kind === "background")
    body = <ShapeProperties layer={layer} />;
  else body = (
    <div className="px-4 py-6 text-sm text-ink-500">
      {translate(language, "No editable properties for this layer kind.")}
    </div>
  );
  return (
    <>
      {body}
      <PaperAssetsSection selectedImageLayer={layer.kind === "image" ? layer : undefined} />
    </>
  );
}

function MultiSelectionProperties() {
  const art = useActiveArtifact();
  const selectedIds = useApp((s) => s.selected_layer_ids);
  const align = useApp((s) => s.alignSelection);
  const distribute = useApp((s) => s.distributeSelection);
  const duplicate = useApp((s) => s.duplicateSelection);
  const remove = useApp((s) => s.deleteSelection);
  const lock = useApp((s) => s.setSelectionLocked);
  const clear = useApp((s) => s.clearSelection);
  const updateStyle = useApp((s) => s.updateSelectionStyle);
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  const selected = art?.layers.filter((l) => selectedIds.includes(l.layer_id)) ?? [];
  const unlocked = selected.filter((l) => !l.locked);
  const allLocked = selected.length > 0 && selected.every((l) => l.locked);
  const first = unlocked[0] ?? selected[0];
  const allText = unlocked.length > 0 && unlocked.every((l) => l.kind === "text");
  const allShape =
    unlocked.length > 0 &&
    unlocked.every((l) => l.kind === "shape" || l.kind === "background");
  const shapeImageMix =
    unlocked.length > 0 &&
    unlocked.every((l) => l.kind === "shape" || l.kind === "background" || l.kind === "image");
  return (
    <>
      <PanelSection title={`${selected.length} ${t("selected")}`}>
        <p className="text-[12.5px] leading-relaxed text-ink-600">
          {t("Batch edit layout with alignment, distribution, duplication, locking, and deletion.")}
        </p>
        <div className="grid grid-cols-2 gap-2">
          <button className="field-input text-left text-[12px]" onClick={duplicate} disabled={!unlocked.length}>
            {t("Duplicate")}
          </button>
          <button className="field-input text-left text-[12px]" onClick={() => lock(!allLocked)} disabled={!selected.length}>
            {allLocked ? t("Unlock") : t("Lock")}
          </button>
          <button className="field-input text-left text-[12px]" onClick={remove} disabled={!unlocked.length}>
            {t("Delete")}
          </button>
          <button className="field-input text-left text-[12px]" onClick={clear}>
            {t("Clear")}
          </button>
        </div>
      </PanelSection>
      <PanelSection title="Align">
        <div className="grid grid-cols-3 gap-2">
          {[
            ["Left", "left"],
            ["Center", "center"],
            ["Right", "right"],
            ["Top", "top"],
            ["Middle", "middle"],
            ["Bottom", "bottom"],
          ].map(([label, mode]) => (
            <button
              key={mode}
              className="rounded-md border border-ink-300/70 bg-surface-raised px-2 py-2 text-[11px] text-ink-700 transition hover:border-ink-700 hover:text-ink-900"
              onClick={() => align(mode as any)}
              disabled={!unlocked.length}
            >
              {t(label)}
            </button>
          ))}
        </div>
      </PanelSection>
      <PanelSection title="Distribute">
        <div className="grid grid-cols-2 gap-2">
          <button
            className="rounded-md border border-ink-300/70 bg-surface-raised px-2 py-2 text-[11px] text-ink-700 transition hover:border-ink-700 hover:text-ink-900 disabled:opacity-40"
            onClick={() => distribute("horizontal")}
            disabled={selected.length < 3}
          >
            {t("Horizontal")}
          </button>
          <button
            className="rounded-md border border-ink-300/70 bg-surface-raised px-2 py-2 text-[11px] text-ink-700 transition hover:border-ink-700 hover:text-ink-900 disabled:opacity-40"
            onClick={() => distribute("vertical")}
            disabled={selected.length < 3}
          >
            {t("Vertical")}
          </button>
        </div>
      </PanelSection>
      {first && (allText || allShape || shapeImageMix) && (
        <PanelSection title="Batch style">
          {allText && (
            <>
              <Field label="Color">
                <ColorField
                  value={first.effects?.fill}
                  onChange={(v) => updateStyle({ effects: { fill: v } })}
                />
              </Field>
              <div className="grid grid-cols-2 gap-2">
                <Field label="Size">
                  <NumberField
                    value={first.font_size_px}
                    onChange={(v) => updateStyle({ font_size_px: v })}
                    min={8}
                    max={400}
                    suffix="px"
                  />
                </Field>
                <Field label="Weight">
                  <SelectField
                    value={String(first.font_weight ?? 400)}
                    onChange={(v) => updateStyle({ font_weight: parseInt(v, 10) })}
                    options={[
                      { value: "300", label: "Light" },
                      { value: "400", label: "Regular" },
                      { value: "500", label: "Medium" },
                      { value: "600", label: "Semibold" },
                      { value: "700", label: "Bold" },
                      { value: "800", label: "Black" },
                    ]}
                  />
                </Field>
              </div>
              <Field label="Alignment">
                <SegGroup
                  value={first.align}
                  onChange={(v) => updateStyle({ align: v as Align })}
                  options={[
                    { value: "left", icon: <I.AlignLeft /> },
                    { value: "center", icon: <I.AlignCenter /> },
                    { value: "right", icon: <I.AlignRight /> },
                  ]}
                />
              </Field>
              <div className="grid grid-cols-3 gap-2">
                <button
                  type="button"
                  onClick={() => updateStyle({ font_style: first.font_style === "italic" ? "normal" : "italic" })}
                  className={`field-input text-center text-[12px] ${first.font_style === "italic" ? "bg-ink-900 text-white" : ""}`}
                >
                  {t("Italic")}
                </button>
                <button
                  type="button"
                  onClick={() => updateStyle({ text_transform: first.text_transform === "uppercase" ? "none" : "uppercase" })}
                  className={`field-input text-center text-[12px] ${first.text_transform === "uppercase" ? "bg-ink-900 text-white" : ""}`}
                >
                  {t("Upper")}
                </button>
                <button
                  type="button"
                  onClick={() => updateStyle({ list_style: first.list_style === "bullet" ? "none" : "bullet" })}
                  className={`field-input text-center text-[12px] ${first.list_style === "bullet" ? "bg-ink-900 text-white" : ""}`}
                >
                  {t("Bullet")}
                </button>
              </div>
            </>
          )}
          {allShape && (
            <>
              <Field label="Fill">
                <ColorField
                  value={first.fill_color}
                  onChange={(v) => updateStyle({ fill_color: v })}
                />
              </Field>
              <Field label="Stroke">
                <ColorField
                  value={first.stroke_color}
                  onChange={(v) => updateStyle({ stroke_color: v })}
                />
              </Field>
              <Field label="Stroke width">
                <NumberField
                  value={first.stroke_width}
                  onChange={(v) => updateStyle({ stroke_width: v })}
                  min={0}
                  suffix="px"
                />
              </Field>
              <Field label="Stroke dash">
                <SegGroup
                  value={first.stroke_dash ?? "solid"}
                  onChange={(v) => updateStyle({ stroke_dash: v as Layer["stroke_dash"] })}
                  options={[
                    { value: "solid", label: "Solid" },
                    { value: "dashed", label: "Dash" },
                    { value: "dotted", label: "Dot" },
                  ]}
                />
              </Field>
            </>
          )}
          {shapeImageMix && (
            <>
              <Field label="Corner radius">
                <NumberField
                  value={first.corner_radius}
                  onChange={(v) => updateStyle({ corner_radius: v })}
                  min={0}
                  suffix="px"
                />
              </Field>
              <Field label={translate(language, "Opacity {value}%", { value: Math.round((first.opacity ?? 1) * 100) })}>
                <SliderField
                  value={first.opacity ?? 1}
                  onChange={(v) => updateStyle({ opacity: v })}
                  min={0}
                  max={1}
                  step={0.01}
                />
              </Field>
              <ShadowControls
                shadow={first.shadow}
                onChange={(shadow) => updateStyle({ shadow })}
              />
            </>
          )}
        </PanelSection>
      )}
    </>
  );
}

function TextProperties({ layer }: { layer: Layer }) {
  const updateLayer = useApp((s) => s.updateLayer);
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  const fontOptions = [
    { value: "Inter", label: "Inter" },
    { value: "Playfair Display", label: "Playfair Display" },
    { value: "JetBrains Mono", label: "JetBrains Mono" },
    { value: "IBM Plex Sans", label: "IBM Plex Sans" },
    { value: "Noto Sans SC", label: "Noto Sans SC" },
    { value: "Noto Serif SC", label: "Noto Serif SC" },
    { value: "ui-sans-serif", label: "System Sans" },
    { value: "ui-serif", label: "System Serif" },
  ];
  if (layer.font_family && !fontOptions.some((option) => option.value === layer.font_family)) {
    fontOptions.unshift({ value: layer.font_family, label: layer.font_family });
  }
  return (
    <>
      <PanelSection title="Content">
        <textarea
          value={layer.text ?? ""}
          onChange={(e) => updateLayer(layer.layer_id, { text: e.target.value })}
          rows={3}
          className="field-input resize-none"
        />
      </PanelSection>

      <PanelSection title="Typography">
        <Field label="Font">
          <SelectField
            value={layer.font_family}
            onChange={(v) => updateLayer(layer.layer_id, { font_family: v })}
            options={fontOptions}
          />
        </Field>

        <div className="grid grid-cols-2 gap-2">
          <Field label="Size">
            <NumberField
              value={layer.font_size_px}
              onChange={(v) => updateLayer(layer.layer_id, { font_size_px: v })}
              min={8}
              max={400}
              suffix="px"
            />
          </Field>
          <Field label="Weight">
            <SelectField
              value={String(layer.font_weight ?? 400)}
              onChange={(v) =>
                updateLayer(layer.layer_id, { font_weight: parseInt(v, 10) })
              }
              options={[
                { value: "300", label: "Light" },
                { value: "400", label: "Regular" },
                { value: "500", label: "Medium" },
                { value: "600", label: "Semibold" },
                { value: "700", label: "Bold" },
                { value: "800", label: "Black" },
              ]}
            />
          </Field>
        </div>

        <div className="grid grid-cols-3 gap-2">
          <button
            type="button"
            onClick={() =>
              updateLayer(layer.layer_id, {
                font_weight: (layer.font_weight ?? 400) >= 700 ? 400 : 700,
              })
            }
            className={`field-input text-center text-[12px] ${
              (layer.font_weight ?? 400) >= 700 ? "bg-ink-900 text-white" : ""
            }`}
          >
            {t("Bold")}
          </button>
          <button
            type="button"
            onClick={() =>
              updateLayer(layer.layer_id, {
                font_style: layer.font_style === "italic" ? "normal" : "italic",
              })
            }
            className={`field-input text-center text-[12px] ${
              layer.font_style === "italic" ? "bg-ink-900 text-white" : ""
            }`}
          >
            {t("Italic")}
          </button>
          <button
            type="button"
            onClick={() =>
              updateLayer(layer.layer_id, {
                text_transform:
                  layer.text_transform === "uppercase" ? "none" : "uppercase",
              })
            }
            className={`field-input text-center text-[12px] ${
              layer.text_transform === "uppercase" ? "bg-ink-900 text-white" : ""
            }`}
          >
            {t("Upper")}
          </button>
        </div>

        <Field label="Color">
          <ColorField
            value={layer.effects?.fill}
            onChange={(v) =>
              updateLayer(layer.layer_id, { effects: { ...layer.effects, fill: v } })
            }
          />
        </Field>

        <Field label="List">
          <SegGroup
            value={layer.list_style ?? "none"}
            onChange={(v) =>
              updateLayer(layer.layer_id, { list_style: v as Layer["list_style"] })
            }
            options={[
              { value: "none", label: "None" },
              { value: "bullet", label: "Bullet" },
            ]}
          />
        </Field>

        <Field label="Alignment">
          <SegGroup
            value={layer.align}
            onChange={(v) =>
              updateLayer(layer.layer_id, { align: v as Layer["align"] })
            }
            options={[
              { value: "left", icon: <I.AlignLeft /> },
              { value: "center", icon: <I.AlignCenter /> },
              { value: "right", icon: <I.AlignRight /> },
            ]}
          />
        </Field>

        <div className="grid grid-cols-2 gap-2">
          <Field label="Line height">
            <NumberField
              value={layer.line_height}
              onChange={(v) => updateLayer(layer.layer_id, { line_height: v })}
              min={0.6}
              max={3}
              step={0.05}
            />
          </Field>
          <Field label="Tracking">
            <NumberField
              value={layer.letter_spacing}
              onChange={(v) =>
                updateLayer(layer.layer_id, { letter_spacing: v })
              }
              min={-10}
              max={40}
              suffix="px"
            />
          </Field>
        </div>
      </PanelSection>

      <PositionSection layer={layer} />
    </>
  );
}

function ImageProperties({ layer }: { layer: Layer }) {
  const art = useActiveArtifact();
  const updateLayer = useApp((s) => s.updateLayer);
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const replaceWithFile = async (file: File | undefined) => {
    if (!file) return;
    setStatus(t("Uploading..."));
    try {
      const asset = await uploadEditorAsset(file);
      const isHtmlPoster =
        art?.native_format === "html"
        && artifactTypeForArtifact(art) === "poster";
      if (isHtmlPoster) {
        window.dispatchEvent(
          new CustomEvent("paper-asset:replace-selected", {
            detail: {
              url: asset.url,
              name: asset.filename || file.name,
              layer_id: layer.layer_id,
            },
          }),
        );
      } else {
        updateLayer(layer.layer_id, { src: asset.url });
      }
      setStatus(t("Image replaced"));
    } catch (e) {
      setStatus(e instanceof Error ? e.message : t("Upload failed"));
    }
  };
  return (
    <>
      <PanelSection title="Image">
        <input
          ref={inputRef}
          type="file"
          accept="image/png,image/jpeg,image/webp,image/gif"
          className="hidden"
          onChange={(e) => {
            replaceWithFile(e.target.files?.[0]);
            e.currentTarget.value = "";
          }}
        />
        <button
          type="button"
          onClick={() => inputRef.current?.click()}
          className="flex w-full items-center justify-center gap-2 rounded-md border border-dashed border-ink-300 bg-vellum px-3 py-2.5 text-[12px] font-medium text-ink-700 transition hover:border-ink-700 hover:bg-surface-raised hover:text-ink-900"
        >
          <I.Image width={14} height={14} />
          {t("Replace image")}
        </button>
        {status && <p className="text-[11px] text-ink-500">{status}</p>}
        <Field label="Source URL">
          <input
            type="text"
            value={layer.src ?? ""}
            onChange={(e) => updateLayer(layer.layer_id, { src: e.target.value })}
            className="field-input"
          />
        </Field>
        <Field label="Fit">
          <SegGroup
            value={layer.fit ?? "cover"}
            onChange={(v) =>
              updateLayer(layer.layer_id, { fit: v as Layer["fit"] })
            }
            options={[
              { value: "cover", label: "Cover" },
              { value: "contain", label: "Contain" },
              { value: "fill", label: "Fill" },
            ]}
          />
        </Field>
        {(layer.fit ?? "cover") === "cover" && (
          <Field label="Crop position">
            <CropPositionControl
              value={layer.object_position ?? { x: 0.5, y: 0.5 }}
              onChange={(object_position, opts) =>
                updateLayer(layer.layer_id, { object_position }, opts)
              }
            />
          </Field>
        )}
        <button
          type="button"
          onClick={() => updateLayer(layer.layer_id, { object_position: { x: 0.5, y: 0.5 } })}
          className="field-input text-left text-[12px]"
        >
          {t("Reset crop")}
        </button>
        <Field label="Corner radius">
          <NumberField
            value={layer.corner_radius}
            onChange={(v) => updateLayer(layer.layer_id, { corner_radius: v })}
            min={0}
            suffix="px"
          />
        </Field>
        <Field label={translate(language, "Opacity {value}%", { value: Math.round((layer.opacity ?? 1) * 100) })}>
          <SliderField
            value={layer.opacity ?? 1}
            onChange={(v) => updateLayer(layer.layer_id, { opacity: v })}
            min={0}
            max={1}
            step={0.01}
          />
        </Field>
        <ShadowControls
          shadow={layer.shadow}
          onChange={(shadow) => updateLayer(layer.layer_id, { shadow })}
        />
      </PanelSection>
      <PositionSection layer={layer} />
    </>
  );
}

function PaperAssetsSection({
  selectedImageLayer,
}: {
  selectedImageLayer?: Layer;
}) {
  const art = useActiveArtifact();
  const updateLayer = useApp((s) => s.updateLayer);
  const selectedPaperAsset = useApp((s) => s.selected_paper_asset);
  const setSelectedPaperAsset = useApp((s) => s.setSelectedPaperAsset);
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  const [assets, setAssets] = useState<ArtifactAsset[]>([]);
  const [assetStatus, setAssetStatus] = useState<"idle" | "loading" | "error">("idle");
  const isPoster = art ? artifactTypeForArtifact(art) === "poster" : false;
  const isClientDemoPoster = isPoster && isClientDemoArtifact(art);
  const isHtmlPoster =
    !!art?.native_file_url &&
    art.native_format === "html" &&
    artifactTypeForArtifact(art) === "poster";

  useEffect(() => {
    if (!art?.artifact_id || !isPoster || isClientDemoPoster) {
      setAssets([]);
      setAssetStatus("idle");
      if (isClientDemoPoster) setSelectedPaperAsset(null);
      return;
    }
    let cancelled = false;
    setAssetStatus("loading");
    fetchArtifactAssets(art.artifact_id)
      .then((items) => {
        if (cancelled) return;
        setAssets(items);
        setAssetStatus("idle");
        if (
          selectedPaperAsset &&
          !items.some((asset) => asset.asset_id === selectedPaperAsset.asset_id)
        ) {
          setSelectedPaperAsset(null);
        }
      })
      .catch(() => {
        if (cancelled) return;
        setAssets([]);
        setAssetStatus("error");
      });
    return () => {
      cancelled = true;
    };
  }, [art?.artifact_id, isClientDemoPoster, isPoster, setSelectedPaperAsset]);

  if (!isPoster) return null;

  const replaceWithAsset = (asset: ArtifactAsset) => {
    setSelectedPaperAsset(asset);
    if (!selectedImageLayer) return;
    if (isHtmlPoster) {
      window.dispatchEvent(
        new CustomEvent("paper-asset:replace-selected", {
          detail: { ...asset, layer_id: selectedImageLayer.layer_id },
        }),
      );
      return;
    }
    updateLayer(selectedImageLayer.layer_id, { src: asset.url });
  };

  return (
    <PanelSection
      title="Assets"
      right={
        assets.length > 0 ? (
          <span className="tabular text-[10px] font-medium text-ink-500">
            {assets.length}
          </span>
        ) : null
      }
    >
      <p className="text-[12px] leading-relaxed text-ink-500">
        {selectedPaperAsset
          ? selectedImageLayer
            ? t("Selected asset is ready. Use Replace on the canvas or below.")
            : t("Now click a poster image, then press Replace on the canvas.")
          : t("Click an asset to select it, or drag it onto a poster image.")}
      </p>
      {selectedPaperAsset && (
        <div className="rounded-md border border-accent/30 bg-accent-soft/70 px-2.5 py-2">
          <div className="flex items-center justify-between gap-2">
            <div className="min-w-0">
              <div className="truncate text-[11.5px] font-medium text-ink-900">
                {assetDisplayName(selectedPaperAsset, t)}
              </div>
              <div className="mt-0.5 text-[9px] uppercase tracking-[0.14em] text-accent-deep">
                {t("Selected asset")}
              </div>
            </div>
            <button
              type="button"
              disabled={!selectedImageLayer}
              onClick={() => replaceWithAsset(selectedPaperAsset)}
              className="shrink-0 rounded-sm bg-ink-900 px-2.5 py-1.5 text-[9.5px] font-medium uppercase text-ink-50 transition hover:bg-ink-700 disabled:cursor-not-allowed disabled:opacity-35"
              style={{ letterSpacing: "0.12em" }}
            >
              {t("Replace selected image")}
            </button>
          </div>
        </div>
      )}
      {assetStatus === "loading" && (
        <p className="text-[12px] text-ink-500">{t("Loading assets...")}</p>
      )}
      {assetStatus === "error" && (
        <p className="text-[12px] text-red-700">{t("Could not load paper assets.")}</p>
      )}
      {assetStatus !== "loading" && assets.length === 0 && (
        <p className="text-[12px] leading-relaxed text-ink-500">
          {t("No paper assets found for this poster.")}
        </p>
      )}
      {assets.length > 0 && (
        <div className="grid max-h-72 grid-cols-2 gap-2 overflow-y-auto pr-1">
          {assets.map((asset) => {
            const active = selectedPaperAsset?.asset_id === asset.asset_id;
            const current = selectedImageLayer?.src === asset.url;
            const label = assetDisplayName(asset, t);
            const kindLabel =
              asset.kind === "figure"
                ? "Figure"
                : asset.kind === "table"
                  ? "Table"
                  : "Image asset";
            return (
              <button
                type="button"
                key={asset.asset_id}
                title={`${t("Use this asset")}: ${label}`}
                draggable
                onDragStart={(e) => {
                  setSelectedPaperAsset(asset);
                  e.dataTransfer.effectAllowed = "copy";
                  e.dataTransfer.setData(PAPER_ASSET_DRAG_MIME, encodePaperAssetDrag(asset));
                  e.dataTransfer.setData("text/uri-list", asset.url);
                  e.dataTransfer.setData("text/plain", asset.url);
                }}
                onClick={() => setSelectedPaperAsset(asset)}
                className={[
                  "group rounded-md border bg-surface-raised p-1.5 text-left transition",
                  active
                    ? "border-accent shadow-[0_0_0_1px_rgba(211,96,51,0.4)]"
                    : current
                      ? "border-sky-400/80 shadow-[0_0_0_1px_rgba(14,165,233,0.25)]"
                      : "border-ink-300/70 hover:border-accent/70",
                ].join(" ")}
              >
                <span className="flex h-20 items-center justify-center overflow-hidden rounded bg-vellum">
                  <img
                    src={asset.url}
                    alt={label}
                    className="max-h-full max-w-full object-contain"
                    loading="lazy"
                  />
                </span>
                <span className="mt-1.5 block truncate text-[11px] font-medium text-ink-900">
                  {label}
                </span>
                <span className="mt-0.5 block text-[9px] uppercase tracking-[0.14em] text-ink-500">
                  {current ? t("Current image") : t(kindLabel)}
                </span>
              </button>
            );
          })}
        </div>
      )}
    </PanelSection>
  );
}

function ShapeProperties({ layer }: { layer: Layer }) {
  const updateLayer = useApp((s) => s.updateLayer);
  const language = useApp((s) => s.ui_language);
  return (
    <>
      <PanelSection title="Shape">
        <Field label="Fill">
          <ColorField
            value={layer.fill_color}
            onChange={(v) => updateLayer(layer.layer_id, { fill_color: v })}
          />
        </Field>
        <Field label="Stroke">
          <ColorField
            value={layer.stroke_color}
            onChange={(v) => updateLayer(layer.layer_id, { stroke_color: v })}
          />
        </Field>
        <Field label="Stroke width">
          <NumberField
            value={layer.stroke_width}
            onChange={(v) => updateLayer(layer.layer_id, { stroke_width: v })}
            min={0}
            suffix="px"
          />
        </Field>
        <Field label="Stroke dash">
          <SegGroup
            value={layer.stroke_dash ?? "solid"}
            onChange={(v) =>
              updateLayer(layer.layer_id, { stroke_dash: v as Layer["stroke_dash"] })
            }
            options={[
              { value: "solid", label: "Solid" },
              { value: "dashed", label: "Dash" },
              { value: "dotted", label: "Dot" },
            ]}
          />
        </Field>
        <Field label="Corner radius">
          <NumberField
            value={layer.corner_radius}
            onChange={(v) => updateLayer(layer.layer_id, { corner_radius: v })}
            min={0}
            suffix="px"
          />
        </Field>
        <Field label={translate(language, "Opacity {value}%", { value: Math.round((layer.opacity ?? 1) * 100) })}>
          <SliderField
            value={layer.opacity ?? 1}
            onChange={(v) => updateLayer(layer.layer_id, { opacity: v })}
            min={0}
            max={1}
            step={0.01}
          />
        </Field>
        <ShadowControls
          shadow={layer.shadow}
          onChange={(shadow) => updateLayer(layer.layer_id, { shadow })}
        />
      </PanelSection>
      <PositionSection layer={layer} />
    </>
  );
}

function CropPositionControl({
  value,
  onChange,
}: {
  value: CropPoint;
  onChange: (value: CropPoint, opts?: { history?: boolean }) => void;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const setFromPointer = (
    clientX: number,
    clientY: number,
    opts?: { history?: boolean },
  ) => {
    const rect = ref.current?.getBoundingClientRect();
    if (!rect) return;
    onChange({
      x: clamp01((clientX - rect.left) / rect.width),
      y: clamp01((clientY - rect.top) / rect.height),
    }, opts);
  };
  const start = (e: React.MouseEvent<HTMLDivElement>) => {
    e.preventDefault();
    let recorded = false;
    const updateFromPointer = (clientX: number, clientY: number) => {
      setFromPointer(clientX, clientY, recorded ? { history: false } : undefined);
      recorded = true;
    };
    updateFromPointer(e.clientX, e.clientY);
    const move = (ev: MouseEvent) => updateFromPointer(ev.clientX, ev.clientY);
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };
  return (
    <div className="space-y-2">
      <div
        ref={ref}
        onMouseDown={start}
        className="relative h-28 cursor-crosshair overflow-hidden rounded-md border border-ink-300/70 bg-[linear-gradient(90deg,rgba(23,19,15,0.08)_1px,transparent_1px),linear-gradient(rgba(23,19,15,0.08)_1px,transparent_1px)]"
        style={{ backgroundSize: "25% 25%" }}
      >
        <div
          className="absolute h-3 w-3 -translate-x-1/2 -translate-y-1/2 rounded-full border border-white bg-accent shadow-sm"
          style={{ left: `${clamp01(value.x) * 100}%`, top: `${clamp01(value.y) * 100}%` }}
        />
      </div>
      <div className="grid grid-cols-2 gap-2">
        <Field label={`X ${Math.round(clamp01(value.x) * 100)}%`}>
          <SliderField
            value={clamp01(value.x)}
            onChange={(x) => onChange({ ...value, x: clamp01(x) })}
            min={0}
            max={1}
            step={0.01}
          />
        </Field>
        <Field label={`Y ${Math.round(clamp01(value.y) * 100)}%`}>
          <SliderField
            value={clamp01(value.y)}
            onChange={(y) => onChange({ ...value, y: clamp01(y) })}
            min={0}
            max={1}
            step={0.01}
          />
        </Field>
      </div>
    </div>
  );
}

function ShadowControls({
  shadow,
  onChange,
}: {
  shadow: Layer["shadow"];
  onChange: (shadow: Layer["shadow"]) => void;
}) {
  const language = useApp((s) => s.ui_language);
  const current = shadow ?? DEFAULT_SHADOW;
  return (
    <div className="space-y-3">
      <Field label="Shadow">
        <SegGroup
          value={shadow ? "custom" : "none"}
          onChange={(v) => {
            if (v === "none") onChange(undefined);
            if (v === "soft") onChange({ ...SHADOW_PRESETS.soft });
            if (v === "lift") onChange({ ...SHADOW_PRESETS.lift });
          }}
          options={[
            { value: "none", label: "Off" },
            { value: "soft", label: "Soft" },
            { value: "lift", label: "Lift" },
            { value: "custom", label: "Custom" },
          ]}
        />
      </Field>
      {shadow && (
        <>
          <Field label="Shadow color">
            <ColorField
              value={current.color}
              onChange={(color) => onChange({ ...current, color })}
            />
          </Field>
          <div className="grid grid-cols-3 gap-2">
            <Field label="X">
              <NumberField
                value={current.dx}
                onChange={(dx) => onChange({ ...current, dx })}
                min={-80}
                max={80}
                suffix="px"
              />
            </Field>
            <Field label="Y">
              <NumberField
                value={current.dy}
                onChange={(dy) => onChange({ ...current, dy })}
                min={-80}
                max={80}
                suffix="px"
              />
            </Field>
            <Field label="Blur">
              <NumberField
                value={current.blur}
                onChange={(blur) => onChange({ ...current, blur })}
                min={0}
                max={120}
                suffix="px"
              />
            </Field>
          </div>
          <Field label={translate(language, "Shadow opacity {value}%", { value: Math.round(clamp01(current.opacity) * 100) })}>
            <SliderField
              value={clamp01(current.opacity)}
              onChange={(opacity) => onChange({ ...current, opacity })}
              min={0}
              max={1}
              step={0.01}
            />
          </Field>
        </>
      )}
    </div>
  );
}

function PositionSection({ layer }: { layer: Layer }) {
  const art = useActiveArtifact();
  const activeSlideIdx = useApp((s) => s.active_slide_idx);
  const updateLayer = useApp((s) => s.updateLayer);
  if (!layer.bbox) return null;
  const { x, y, w, h } = layer.bbox;
  const coordinateFrame = frameForLayer(art, layer) ?? activeFrameForArtifact(art, activeSlideIdx);
  const origin = coordinateFrame?.bbox ?? { x: 0, y: 0 };
  const localX = x - origin.x;
  const localY = y - origin.y;
  const set = (patch: Partial<{ x: number; y: number; w: number; h: number }>) => {
    const next = { ...layer.bbox! };
    if (patch.x !== undefined) next.x = origin.x + patch.x;
    if (patch.y !== undefined) next.y = origin.y + patch.y;
    if (patch.w !== undefined) next.w = patch.w;
    if (patch.h !== undefined) next.h = patch.h;
    updateLayer(layer.layer_id, { bbox: next });
  };
  return (
    <PanelSection title="Position">
      <div className="grid grid-cols-2 gap-2">
        <Field label="X">
          <NumberField value={localX} onChange={(v) => set({ x: v })} suffix="px" />
        </Field>
        <Field label="Y">
          <NumberField value={localY} onChange={(v) => set({ y: v })} suffix="px" />
        </Field>
        <Field label="Width">
          <NumberField value={w} onChange={(v) => set({ w: v })} suffix="px" />
        </Field>
        <Field label="Height">
          <NumberField value={h} onChange={(v) => set({ h: v })} suffix="px" />
        </Field>
      </div>
    </PanelSection>
  );
}

function CanvasProperties() {
  const art = useActiveArtifact();
  const activeSlideIdx = useApp((s) => s.active_slide_idx);
  const updateCanvas = useApp((s) => s.updateCanvas);
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
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  const viewToggles: Array<[string, boolean, () => void]> = [
    ["Grid", grid, toggleGrid],
    ["Rulers", rulers, toggleRulers],
    ["Safe margins", safe, toggleSafe],
    ["Smart guides", smart, toggleSmart],
  ];
  if (!art) return null;
  const artType = artifactTypeForArtifact(art);
  const activeFrame = activeFrameForArtifact(art, activeSlideIdx);
  const frameLabel = artType === "video" ? "Scene" : "Slide";
  const nativeHtmlCanvas = !!art.native_file_url && art.native_format === "html";
  return (
    <>
      <PanelSection title={activeFrame ? frameLabel : "Canvas"}>
        <p className="text-[12.5px] italic leading-relaxed text-ink-500">
          {activeFrame
            ? translate(language, "Nothing selected. Click any layer in this {frame} to edit it.", { frame: t(frameLabel).toLowerCase() })
            : t("Nothing selected. Click any layer on the canvas to edit it, or adjust the canvas below.")}
        </p>
        <div className="grid grid-cols-2 gap-2">
          <Field label="Width">
            {activeFrame || nativeHtmlCanvas ? (
              <ReadOnlyNumber value={activeFrame?.bbox.w ?? art.canvas.w} suffix="px" />
            ) : (
              <NumberField
                value={art.canvas.w}
                onChange={(v) => updateCanvas({ w: v })}
                suffix="px"
              />
            )}
          </Field>
          <Field label="Height">
            {activeFrame || nativeHtmlCanvas ? (
              <ReadOnlyNumber value={activeFrame?.bbox.h ?? art.canvas.h} suffix="px" />
            ) : (
              <NumberField
                value={art.canvas.h}
                onChange={(v) => updateCanvas({ h: v })}
                suffix="px"
              />
            )}
          </Field>
        </div>
        <Field label="Background">
          <ColorField
            value={art.canvas.background}
            onChange={(v) => updateCanvas({ background: v })}
          />
        </Field>
      </PanelSection>
      <PanelSection title="View">
        <div className="grid grid-cols-2 gap-2">
          {viewToggles.map(([label, active, action]) => (
            <button
              key={label}
              type="button"
              onClick={action}
              className={`rounded-md border px-2 py-2 text-left text-[11px] font-medium transition ${
                active
                  ? "border-ink-900 bg-ink-900 text-white"
                  : "border-ink-300/70 bg-surface-raised text-ink-700 hover:border-ink-700 hover:text-ink-900"
              }`}
            >
              {t(label)}
              <span className={`block text-[10px] ${active ? "text-ink-200" : "text-ink-500"}`}>
                {active ? t("On") : t("Off state")}
              </span>
            </button>
          ))}
        </div>
        <Field label="Grid size">
          <SegGroup
            value={String(gridSize)}
            onChange={(v) => setGridSize(parseInt(v, 10))}
            options={[4, 8, 12, 16, 24].map((n) => ({ value: String(n), label: String(n) }))}
          />
        </Field>
        <Field label="Safe margin">
          <SegGroup
            value={String(Math.round(safePct * 100))}
            onChange={(v) => setSafePct(parseInt(v, 10) / 100)}
            options={[4, 6, 8, 10].map((n) => ({ value: String(n), label: `${n}%` }))}
          />
        </Field>
      </PanelSection>
      {!activeFrame && (
        <PanelSection title="Presets">
          <div className="grid grid-cols-2 gap-2">
            {[
              { label: "3:4 Poster", w: 1200, h: 1600 },
              { label: "16:9 Slide", w: 1920, h: 1080 },
              { label: "1:1 Square", w: 1080, h: 1080 },
              { label: "9:16 Story", w: 1080, h: 1920 },
            ].map((p) => (
              <button
                key={p.label}
                onClick={() => updateCanvas({ w: p.w, h: p.h })}
                className="rounded-md border border-ink-300/70 bg-surface-raised px-2 py-2 text-[11px] font-medium text-ink-700 transition hover:border-ink-700 hover:text-ink-900"
              >
                {p.label}
                <span className="tabular block text-[10px] text-ink-500">
                  {p.w}×{p.h}
                </span>
              </button>
            ))}
          </div>
        </PanelSection>
      )}
    </>
  );
}

function ReadOnlyNumber({ value, suffix }: { value: number; suffix?: string }) {
  return (
    <div className="field-input relative tabular pr-7 text-ink-600">
      {value}
      {suffix && (
        <span className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-[11px] text-ink-500">
          {suffix}
        </span>
      )}
    </div>
  );
}

// ============ Layers tab ============

function LayersList() {
  const art = useActiveArtifact();
  const selectedIds = useApp((s) => s.selected_layer_ids);
  const selectLayer = useApp((s) => s.selectLayer);
  const setSelection = useApp((s) => s.setSelection);
  const updateLayer = useApp((s) => s.updateLayer);
  const groupSelection = useApp((s) => s.groupSelection);
  const ungroupSelection = useApp((s) => s.ungroupSelection);
  const renameGroup = useApp((s) => s.renameGroup);
  const reorderLayer = useApp((s) => s.reorderLayer);
  const reorderLayerBlock = useApp((s) => s.reorderLayerBlock);
  const toggleLayerProp = useApp((s) => s.toggleLayerProp);
  const toggleGroupProp = useApp((s) => s.toggleGroupProp);
  const removeLayer = useApp((s) => s.removeLayer);
  const deleteSelection = useApp((s) => s.deleteSelection);
  const deleteGroup = useApp((s) => s.deleteGroup);
  const collapsedGroups = useApp((s) => s.layer_group_collapsed);
  const setGroupCollapsed = useApp((s) => s.setLayerGroupCollapsed);
  const activeSlideIdx = useApp((s) => s.active_slide_idx);
  const setActiveSlideIdx = useApp((s) => s.setActiveSlideIdx);
  const addSlideAfter = useApp((s) => s.addSlideAfter);
  const duplicateActiveSlide = useApp((s) => s.duplicateActiveSlide);
  const deleteActiveSlide = useApp((s) => s.deleteActiveSlide);
  const moveActiveSlide = useApp((s) => s.moveActiveSlide);
  const [renaming, setRenaming] = useState<{ kind: "layer" | "group"; id: string } | null>(null);
  const [draftName, setDraftName] = useState("");
  const [dragging, setDragging] = useState<{
    kind: "layer" | "group";
    ids: string[];
    groupId: string | null;
  } | null>(null);
  const [dropTarget, setDropTarget] = useState<{
    key: string;
    ids: string[];
    position: "before" | "after";
    scopeIds: string[];
  } | null>(null);
  if (!art) return null;
  const sorted = [...art.layers].sort((a, b) => b.z_index - a.z_index);
  const slideFrames = detectSlideFrames(art);
  const isVideo = artifactTypeForArtifact(art) === "video";
  const frameLabel = isVideo ? "Scene" : "Slide";
  const groupById = new Map((art.layer_groups ?? []).map((g) => [g.group_id, g]));
  const layerCenterInFrame = (l: Layer, frame: (typeof slideFrames)[number]) => {
    const b = l.bbox;
    if (!b) return false;
    if (l.kind === "background" && (b.w > frame.bbox.w * 1.05 || b.h > frame.bbox.h * 1.05)) {
      return false;
    }
    const cx = b.x + b.w / 2;
    const cy = b.y + b.h / 2;
    return (
      cx >= frame.bbox.x &&
      cx <= frame.bbox.x + frame.bbox.w &&
      cy >= frame.bbox.y &&
      cy <= frame.bbox.y + frame.bbox.h
    );
  };
  const scopeKey = (l: Layer) => {
    if (slideFrames.length >= 2) {
      const frame = slideFrames.find((f) => layerCenterInFrame(l, f));
      return frame ? `slide:${frame.idx}` : "canvas";
    }
    return "canvas";
  };
  const selectedLayer = selectedIds.length === 1
    ? art.layers.find((l) => l.layer_id === selectedIds[0])
    : null;
  const selectedScopeLayers = selectedLayer
    ? slideFrames.length >= 2
      ? (() => {
          const frame = slideFrames.find((f) => layerCenterInFrame(selectedLayer, f));
          return frame
            ? sorted.filter((l) => layerCenterInFrame(l, frame))
            : sorted.filter((l) => !slideFrames.some((f) => layerCenterInFrame(l, f)));
        })()
      : sorted
    : [];
  const selectedScopeIds = selectedScopeLayers.map((l) => l.layer_id);
  const selectedDisplayIdx = selectedScopeIds.indexOf(selectedIds[0] ?? "");
  const canMoveForward = selectedDisplayIdx > 0;
  const canMoveBackward =
    selectedDisplayIdx >= 0 && selectedDisplayIdx < selectedScopeIds.length - 1;
  const selectedLayers = art.layers.filter((l) => selectedIds.includes(l.layer_id));
  const selectedGroupIds = Array.from(
    new Set(selectedLayers.map((l) => l.group_id).filter(Boolean) as string[])
  );
  const canGroup =
    selectedLayers.length >= 2 &&
    selectedLayers.every((l) => l.bbox && l.kind !== "background") &&
    new Set(selectedLayers.map(scopeKey)).size === 1 &&
    !(selectedGroupIds.length === 1 && selectedLayers.every((l) => l.group_id === selectedGroupIds[0]));
  const canUngroup = selectedGroupIds.length > 0;
  const beginRenameLayer = (l: Layer) => {
    setRenaming({ kind: "layer", id: l.layer_id });
    setDraftName(l.name || l.layer_id);
  };
  const beginRenameGroup = (g: LayerGroup) => {
    setRenaming({ kind: "group", id: g.group_id });
    setDraftName(g.name || g.group_id);
  };
  const commitRename = () => {
    if (!renaming) return;
    const next = draftName.trim();
    if (next) {
      if (renaming.kind === "layer") updateLayer(renaming.id, { name: next });
      else renameGroup(renaming.id, next);
    }
    setRenaming(null);
    setDraftName("");
  };
  const cancelRename = () => {
    setRenaming(null);
    setDraftName("");
  };
  const selectIds = (ids: string[], toggle: boolean) => {
    if (!toggle) {
      setSelection(ids);
      return;
    }
    const selected = new Set(selectedIds);
    const allSelected = ids.every((id) => selected.has(id));
    if (allSelected) setSelection(selectedIds.filter((id) => !ids.includes(id)));
    else setSelection([...selectedIds, ...ids.filter((id) => !selected.has(id))]);
  };
  const canDropOn = (
    targetKind: "layer" | "group",
    targetGroupId: string | null,
    targetIds: string[]
  ) => {
    if (!dragging) return false;
    if (targetIds.some((id) => dragging.ids.includes(id))) return false;
    if (dragging.kind === "group") return targetKind === "group" || targetGroupId === null;
    if (dragging.groupId) return targetKind === "layer" && targetGroupId === dragging.groupId;
    return targetKind === "group" || (targetKind === "layer" && targetGroupId === null);
  };
  const rowDropHandlers = (
    key: string,
    targetKind: "layer" | "group",
    targetGroupId: string | null,
    targetIds: string[],
    scopeIds: string[]
  ) => ({
    onDragOver: (e: React.DragEvent) => {
      if (!canDropOn(targetKind, targetGroupId, targetIds)) return;
      e.preventDefault();
      const rect = e.currentTarget.getBoundingClientRect();
      const position = e.clientY < rect.top + rect.height / 2 ? "before" : "after";
      setDropTarget({ key, ids: targetIds, position, scopeIds });
    },
    onDragLeave: () => {
      if (dropTarget?.key === key) setDropTarget(null);
    },
    onDrop: (e: React.DragEvent) => {
      e.preventDefault();
      if (!dragging || !dropTarget || dropTarget.key !== key) return;
      reorderLayerBlock(dragging.ids, dropTarget.ids, dropTarget.position, dropTarget.scopeIds);
      setDragging(null);
      setDropTarget(null);
    },
  });
  const dropClass = (key: string) =>
    dropTarget?.key === key
      ? dropTarget.position === "before"
        ? "before:absolute before:left-1 before:right-1 before:top-0 before:h-px before:bg-accent"
        : "after:absolute after:bottom-0 after:left-1 after:right-1 after:h-px after:bg-accent"
      : "";
  const renderRenameInput = (selected: boolean) => (
    <input
      value={draftName}
      autoFocus
      onClick={(e) => e.stopPropagation()}
      onDoubleClick={(e) => e.stopPropagation()}
      onChange={(e) => setDraftName(e.target.value)}
      onBlur={commitRename}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          e.currentTarget.blur();
        }
        if (e.key === "Escape") {
          e.preventDefault();
          cancelRename();
        }
      }}
      className={`min-w-0 flex-1 rounded-sm border px-1 py-0.5 text-xs outline-none ${
        selected
          ? "border-white/20 bg-white/10 text-white"
          : "border-ink-300 bg-white text-ink-900"
      }`}
    />
  );
  const renderLayer = (l: Layer, scopeIds: string[], indent = false, groupId: string | null = null) => {
    const selected = selectedIds.includes(l.layer_id);
    const isRenaming = renaming?.kind === "layer" && renaming.id === l.layer_id;
    const key = `layer:${l.layer_id}`;
    return (
      <div
        key={l.layer_id}
        draggable
        onDragStart={(e) => {
          e.stopPropagation();
          setDragging({ kind: "layer", ids: [l.layer_id], groupId });
          e.dataTransfer.effectAllowed = "move";
        }}
        onDragEnd={() => {
          setDragging(null);
          setDropTarget(null);
        }}
        {...rowDropHandlers(key, "layer", groupId, [l.layer_id], scopeIds)}
        onClick={(e) => selectLayer(l.layer_id, e.shiftKey ? "toggle" : "replace")}
        onDoubleClick={(e) => {
          e.stopPropagation();
          beginRenameLayer(l);
        }}
        className={`group relative flex items-center gap-1.5 rounded-md px-2 py-1.5 text-xs transition ${dropClass(key)} ${
          selected
            ? "bg-ink-900 text-white shadow-[inset_3px_0_0_#0ea5e9] ring-1 ring-sky-400/70"
            : "text-ink-700 hover:bg-vellum"
        } ${indent ? "ml-5" : ""}`}
      >
        <span className={`flex h-5 w-5 shrink-0 items-center justify-center ${selected ? "text-ink-300" : "text-ink-500"}`}>
          {l.kind === "text" && <I.Type width={12} height={12} />}
          {l.kind === "image" && <I.Image width={12} height={12} />}
          {(l.kind === "shape" || l.kind === "background" || l.kind === "section") && (
            <I.Square width={12} height={12} />
          )}
        </span>
        {isRenaming ? renderRenameInput(selected) : (
          <span className="flex-1 truncate">{l.name}</span>
        )}
        {!isRenaming && (
          <button
            onClick={(e) => {
              e.stopPropagation();
              beginRenameLayer(l);
            }}
            className="opacity-0 transition group-hover:opacity-100"
            title="Rename layer"
          >
            <I.Edit width={12} height={12} />
          </button>
        )}
        <button
          onClick={(e) => {
            e.stopPropagation();
            toggleLayerProp(l.layer_id, "visible");
          }}
          className="opacity-0 transition group-hover:opacity-100"
          title={l.visible === false ? "Show" : "Hide"}
        >
          {l.visible === false ? (
            <I.EyeOff width={12} height={12} />
          ) : (
            <I.Eye width={12} height={12} />
          )}
        </button>
        <button
          onClick={(e) => {
            e.stopPropagation();
            toggleLayerProp(l.layer_id, "locked");
          }}
          className="opacity-0 transition group-hover:opacity-100"
          title={l.locked ? "Unlock" : "Lock"}
        >
          {l.locked ? (
            <I.Lock width={12} height={12} />
          ) : (
            <I.Unlock width={12} height={12} />
          )}
        </button>
      </div>
    );
  };
  const renderGroup = (group: LayerGroup, layers: Layer[], scopeIds: string[]) => {
    const childIds = layers.map((l) => l.layer_id);
    const allSelected = childIds.length > 0 && childIds.every((id) => selectedIds.includes(id));
    const collapsed = collapsedGroups[group.group_id] ?? false;
    const visible = layers.some((l) => l.visible !== false);
    const locked = layers.every((l) => l.locked);
    const isRenaming = renaming?.kind === "group" && renaming.id === group.group_id;
    const key = `group:${group.group_id}`;
    return (
      <div key={group.group_id} className="space-y-0.5">
        <div
          draggable
          onDragStart={(e) => {
            e.stopPropagation();
            setDragging({ kind: "group", ids: childIds, groupId: group.group_id });
            e.dataTransfer.effectAllowed = "move";
          }}
          onDragEnd={() => {
            setDragging(null);
            setDropTarget(null);
          }}
          {...rowDropHandlers(key, "group", group.group_id, childIds, scopeIds)}
          onClick={(e) => selectIds(childIds, e.shiftKey)}
          onDoubleClick={(e) => {
            e.stopPropagation();
            beginRenameGroup(group);
          }}
          className={`group relative flex items-center gap-1.5 rounded-md px-2 py-1.5 text-xs transition ${dropClass(key)} ${
            allSelected ? "bg-ink-900 text-white" : "bg-vellum/70 text-ink-800 hover:bg-vellum"
          }`}
        >
          <button
            onClick={(e) => {
              e.stopPropagation();
              setGroupCollapsed(group.group_id, !collapsed);
            }}
            className={allSelected ? "text-ink-300" : "text-ink-500"}
            title={collapsed ? "Expand group" : "Collapse group"}
          >
            {collapsed ? <I.ChevronUp width={12} height={12} /> : <I.ChevronDown width={12} height={12} />}
          </button>
          <span className={`flex h-5 w-5 shrink-0 items-center justify-center ${allSelected ? "text-ink-300" : "text-ink-500"}`}>
            <I.Layout width={12} height={12} />
          </span>
          {isRenaming ? renderRenameInput(allSelected) : (
            <span className="flex-1 truncate">{group.name}</span>
          )}
          <span className={`tabular text-[10px] ${allSelected ? "text-ink-300" : "text-ink-500"}`}>
            {layers.length}
          </span>
          {!isRenaming && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                beginRenameGroup(group);
              }}
              className="opacity-0 transition group-hover:opacity-100"
              title="Rename group"
            >
              <I.Edit width={12} height={12} />
            </button>
          )}
          <button
            onClick={(e) => {
              e.stopPropagation();
              toggleGroupProp(group.group_id, "visible");
            }}
            className="opacity-0 transition group-hover:opacity-100"
            title={visible ? "Hide group" : "Show group"}
          >
            {visible ? <I.Eye width={12} height={12} /> : <I.EyeOff width={12} height={12} />}
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              toggleGroupProp(group.group_id, "locked");
            }}
            className="opacity-0 transition group-hover:opacity-100"
            title={locked ? "Unlock group" : "Lock group"}
          >
            {locked ? <I.Lock width={12} height={12} /> : <I.Unlock width={12} height={12} />}
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              ungroupSelection(group.group_id);
            }}
            className="opacity-0 transition group-hover:opacity-100"
            title="Ungroup"
          >
            <I.X width={12} height={12} />
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              deleteGroup(group.group_id);
            }}
            className="opacity-0 transition hover:text-red-600 group-hover:opacity-100"
            title="Delete group layers"
          >
            <I.Trash width={12} height={12} />
          </button>
        </div>
        {!collapsed && (
          <div className="space-y-0.5">
            {layers.map((l) => renderLayer(l, childIds, true, group.group_id))}
          </div>
        )}
      </div>
    );
  };
  const renderScope = (layers: Layer[]) => {
    const scopeIds = layers.map((l) => l.layer_id);
    const rows: Array<
      | { kind: "group"; topZ: number; group: LayerGroup; layers: Layer[] }
      | { kind: "layer"; topZ: number; layer: Layer }
    > = [];
    const seenGroups = new Set<string>();
    for (const layer of layers) {
      if (layer.group_id && groupById.has(layer.group_id)) {
        if (seenGroups.has(layer.group_id)) continue;
        seenGroups.add(layer.group_id);
        const groupLayers = layers.filter((l) => l.group_id === layer.group_id);
        rows.push({
          kind: "group",
          topZ: Math.max(...groupLayers.map((l) => l.z_index)),
          group: groupById.get(layer.group_id)!,
          layers: groupLayers,
        });
      } else {
        rows.push({ kind: "layer", topZ: layer.z_index, layer });
      }
    }
    return rows
      .sort((a, b) => b.topZ - a.topZ)
      .map((row) =>
        row.kind === "group"
          ? renderGroup(row.group, row.layers, scopeIds)
          : renderLayer(row.layer, scopeIds, false, null)
      );
  };
  const activateSlideAnd = (idx: number, action: () => void) => {
    setActiveSlideIdx(idx);
    action();
  };

  return (
    <PanelSection title={`${art.layers.length} Layer${art.layers.length === 1 ? "" : "s"}`}>
      <div className="-mx-1 space-y-0.5">
        {slideFrames.length >= 2 ? (
          <>
            {slideFrames.map((frame) => {
              const layers = sorted.filter((l) => layerCenterInFrame(l, frame));
              const active = frame.idx === activeSlideIdx;
              return (
                <details
                  key={frame.layer_id}
                  open={active}
                  className={`rounded-md border bg-vellum/40 transition ${
                    active ? "border-ink-900/50 shadow-[inset_0_0_0_1px_rgba(23,19,15,0.08)]" : "border-ink-300/50"
                  }`}
                >
                  <summary
                    className={`flex cursor-pointer items-center justify-between gap-2 px-2 py-1.5 text-[10px] font-medium uppercase ${
                      active ? "text-ink-900" : "text-ink-500"
                    }`}
                    style={{ letterSpacing: "0.14em" }}
                    onClick={() => setActiveSlideIdx(frame.idx)}
                  >
                    <span>{frameLabel} {frame.idx + 1} · {layers.length}</span>
                    <span className="flex items-center gap-1">
                      {active && <span className="text-[9px] text-accent-deep">Active</span>}
                      {!isVideo && (
                        <>
                          <button
                            className="icon-btn h-5 w-5"
                            title="Add slide after"
                            onClick={(e) => {
                              e.stopPropagation();
                              activateSlideAnd(frame.idx, addSlideAfter);
                            }}
                          >
                            <I.Plus width={10} height={10} />
                          </button>
                          <button
                            className="icon-btn h-5 w-5"
                            title="Duplicate slide"
                            onClick={(e) => {
                              e.stopPropagation();
                              activateSlideAnd(frame.idx, duplicateActiveSlide);
                            }}
                          >
                            <I.Duplicate width={10} height={10} />
                          </button>
                          <button
                            className="icon-btn h-5 w-5"
                            title="Move slide left"
                            disabled={frame.idx === 0}
                            onClick={(e) => {
                              e.stopPropagation();
                              activateSlideAnd(frame.idx, () => moveActiveSlide("up"));
                            }}
                          >
                            <I.ChevronUp width={10} height={10} />
                          </button>
                          <button
                            className="icon-btn h-5 w-5"
                            title="Move slide right"
                            disabled={frame.idx === slideFrames.length - 1}
                            onClick={(e) => {
                              e.stopPropagation();
                              activateSlideAnd(frame.idx, () => moveActiveSlide("down"));
                            }}
                          >
                            <I.ChevronDown width={10} height={10} />
                          </button>
                          <button
                            className="icon-btn h-5 w-5 hover:text-red-600"
                            title="Delete slide"
                            disabled={slideFrames.length <= 1}
                            onClick={(e) => {
                              e.stopPropagation();
                              activateSlideAnd(frame.idx, deleteActiveSlide);
                            }}
                          >
                            <I.Trash width={10} height={10} />
                          </button>
                        </>
                      )}
                    </span>
                  </summary>
                  <div className="space-y-0.5 px-1 pb-1">{renderScope(layers)}</div>
                </details>
              );
            })}
            {renderScope(sorted.filter((l) => !slideFrames.some((f) => layerCenterInFrame(l, f))))}
          </>
        ) : (
          renderScope(sorted)
        )}
      </div>
      {selectedIds.length > 0 && (
        <div className="mt-3 flex items-center gap-1 border-t border-ink-300/60 pt-3">
          {selectedIds.length === 1 && (
            <>
              <button
                onClick={() => selectLayer(selectedScopeIds[selectedDisplayIdx - 1], "replace")}
                className="icon-btn"
                disabled={!canMoveForward}
                title="Select previous layer"
              >
                <I.ChevronUp />
              </button>
              <button
                onClick={() => selectLayer(selectedScopeIds[selectedDisplayIdx + 1], "replace")}
                className="icon-btn"
                disabled={!canMoveBackward}
                title="Select next layer"
              >
                <I.ChevronDown />
              </button>
              <span className="tabular px-1 text-[10px] text-ink-500">
                {selectedDisplayIdx >= 0 ? `${selectedDisplayIdx + 1}/${selectedScopeIds.length}` : ""}
              </span>
            </>
          )}
          <span className="flex-1" />
          <button
            onClick={groupSelection}
            className="field-input h-7 px-2 text-[10px]"
            disabled={!canGroup}
            title="Group selection"
          >
            Group
          </button>
          <button
            onClick={() => ungroupSelection()}
            className="field-input h-7 px-2 text-[10px]"
            disabled={!canUngroup}
            title="Ungroup selection"
          >
            Ungroup
          </button>
          <button
            onClick={() => reorderLayer(selectedIds[0], "up", selectedScopeIds)}
            className="icon-btn"
            disabled={selectedIds.length !== 1 || !canMoveForward}
            title="Bring forward"
          >
            <I.ChevronUp />
          </button>
          <button
            onClick={() => reorderLayer(selectedIds[0], "down", selectedScopeIds)}
            className="icon-btn"
            disabled={selectedIds.length !== 1 || !canMoveBackward}
            title="Send backward"
          >
            <I.ChevronDown />
          </button>
          <button
            onClick={() => selectedIds.length === 1 ? removeLayer(selectedIds[0]) : deleteSelection()}
            className="icon-btn hover:text-red-600"
            title="Delete"
          >
            <I.Trash />
          </button>
        </div>
      )}
    </PanelSection>
  );
}

// ============ Insert tab ============

function InsertPanel({ onInserted }: { onInserted: () => void }) {
  const insertLayers = useApp((s) => s.insertLayers);
  const placementMode = useApp((s) => s.insert_placement_mode);
  const setPlacementMode = useApp((s) => s.setInsertPlacementMode);
  const pendingInsert = useApp((s) => s.pending_insert);
  const setPendingInsert = useApp((s) => s.setPendingInsert);
  const cancelPendingInsert = useApp((s) => s.cancelPendingInsert);
  const updateLayer = useApp((s) => s.updateLayer);
  const selectedLayer = useSelectedLayer();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [imageUrl, setImageUrl] = useState("");
  const [mediaStatus, setMediaStatus] = useState<string | null>(null);

  const placeOrInsert = (
    layers: Layer[],
    placement: "single" | "frame-relative",
    supportedMode: InsertPlacementMode = placementMode,
  ) => {
    if (supportedMode === "click-to-place") {
      setPendingInsert(layers, { placement });
      setMediaStatus(null);
      return;
    }
    insertLayers(layers, {
      placement,
      strategy: supportedMode === "center" ? "center" : "near-selection",
    });
    onInserted();
  };

  const insertSingle = (layer: Layer) => {
    placeOrInsert([layer], "single");
  };

  const insertBlock = (layers: Layer[]) => {
    placeOrInsert(layers, "frame-relative");
  };

  const replaceOrInsertImage = (src: string, name = "Image") => {
    if (selectedLayer?.kind === "image") {
      updateLayer(selectedLayer.layer_id, { src });
      onInserted();
      return;
    }
    insertLayers([imageLayer(name, { x: 0, y: 0, w: 360, h: 240 }, src)], {
      placement: "single",
      strategy: placementMode === "center" ? "center" : "near-selection",
    });
    onInserted();
  };

  const handleImageFile = async (file: File | undefined) => {
    if (!file) return;
    setMediaStatus("Uploading...");
    try {
      const asset = await uploadEditorAsset(file);
      replaceOrInsertImage(asset.url, file.name.replace(/\.[^.]+$/, "") || "Image");
      setMediaStatus("Image added");
    } catch (e) {
      setMediaStatus(e instanceof Error ? e.message : "Upload failed");
    }
  };

  const pasteImage = async () => {
    if (!navigator.clipboard?.read) {
      setMediaStatus("Clipboard image paste is not available in this browser.");
      return;
    }
    try {
      const items = await navigator.clipboard.read();
      for (const item of items) {
        const type = item.types.find((t) => t.startsWith("image/"));
        if (!type) continue;
        const blob = await item.getType(type);
        await handleImageFile(new File([blob], "pasted-image", { type }));
        return;
      }
      setMediaStatus("No image found on clipboard.");
    } catch (e) {
      setMediaStatus(e instanceof Error ? e.message : "Could not read clipboard.");
    }
  };

  const insertImageUrl = () => {
    const url = imageUrl.trim();
    if (!url) return;
    replaceOrInsertImage(url, "Image URL");
    setImageUrl("");
  };

  const insertShape = (shape: ShapeKind) => {
    insertSingle({
      layer_id: nextId("lyr"),
      name: shape[0].toUpperCase() + shape.slice(1),
      kind: "shape",
      shape_kind: shape,
      z_index: 1,
      bbox: { x: 0, y: 0, w: shape === "line" || shape === "arrow" ? 260 : 180, h: shape === "line" || shape === "arrow" ? 4 : 180 },
      fill_color: shape === "line" || shape === "arrow" ? "transparent" : "#1f1f1f",
      stroke_color:
        shape === "line" || shape === "arrow" ? "#1f1f1f" : undefined,
      stroke_width: shape === "line" || shape === "arrow" ? 4 : 0,
      visible: true,
    });
  };

  return (
    <>
      <PanelSection
        title="Placement"
        right={
          pendingInsert ? (
            <button
              type="button"
              onClick={cancelPendingInsert}
              className="text-[10px] font-medium uppercase text-ink-500 transition hover:text-ink-900"
              style={{ letterSpacing: "0.12em" }}
            >
              Cancel
            </button>
          ) : null
        }
      >
        <SegGroup
          value={placementMode}
          onChange={(v) => setPlacementMode(v as InsertPlacementMode)}
          options={[
            { value: "near-selection", label: "Near" },
            { value: "center", label: "Center" },
            { value: "click-to-place", label: "Place" },
          ]}
        />
        <p className="text-[11px] text-ink-500">
          {pendingInsert
            ? "Click the canvas to place the pending insert. Press Esc to cancel."
            : placementMode === "click-to-place"
              ? "Choose an insert, then click the canvas to place it."
              : placementMode === "center"
                ? "New items are centered in the active frame."
                : "New items appear near the selection or frame center."}
        </p>
      </PanelSection>

      <PanelSection title="Text presets">
        <div className="grid grid-cols-2 gap-2">
          {TEXT_PRESETS.map((preset) => (
            <InsertTile
              key={preset.name}
              label={preset.name}
              sublabel={preset.preview}
              icon={<I.Type width={14} height={14} />}
              onClick={() => insertSingle(textLayer(preset.name, preset.text, { x: 0, y: 0, w: preset.w, h: preset.h }, preset.style))}
            />
          ))}
        </div>
      </PanelSection>

      <PanelSection title="Pitch blocks">
        <div className="grid grid-cols-2 gap-2">
          {PITCH_BLOCKS.map((block) => (
            <InsertTile
              key={block.name}
              label={block.name}
              sublabel={block.sublabel}
              icon={block.icon}
              onClick={() => insertBlock(block.layers())}
            />
          ))}
        </div>
      </PanelSection>

      <PanelSection title="Shapes">
        <div className="grid grid-cols-2 gap-2">
          {(
            [
              { kind: "rect" as const, label: "Rectangle", icon: <I.Square /> },
              { kind: "ellipse" as const, label: "Ellipse", icon: <I.Circle /> },
              { kind: "line" as const, label: "Line", icon: <I.Line /> },
              { kind: "arrow" as const, label: "Arrow", icon: <I.Arrow /> },
            ]
          ).map((s) => (
            <button
              key={s.kind}
              onClick={() => insertShape(s.kind)}
              className="flex flex-col items-center justify-center gap-2 rounded-md border border-ink-300/70 bg-surface-raised px-2 py-3 text-[10.5px] font-medium uppercase text-ink-700 transition hover:border-ink-700 hover:text-ink-900"
              style={{ letterSpacing: "0.1em" }}
            >
              {s.icon}
              <span>{s.label}</span>
            </button>
          ))}
        </div>
      </PanelSection>

      <PanelSection title="Media">
        <input
          ref={fileInputRef}
          type="file"
          accept="image/png,image/jpeg,image/webp,image/gif"
          className="hidden"
          onChange={(e) => {
            handleImageFile(e.target.files?.[0]);
            e.currentTarget.value = "";
          }}
        />
        <div className="grid grid-cols-2 gap-2">
          <InsertTile
            label="Upload image"
            sublabel="PNG/JPEG/WebP/GIF"
            icon={<I.Image width={14} height={14} />}
            onClick={() => fileInputRef.current?.click()}
          />
          <InsertTile
            label="Paste image"
            sublabel="From clipboard"
            icon={<I.Edit width={14} height={14} />}
            onClick={pasteImage}
          />
          <InsertTile
            label="Image frame"
            sublabel="Placeholder"
            icon={<I.Square width={14} height={14} />}
            onClick={() => insertSingle(imageLayer("Image frame", { x: 0, y: 0, w: 360, h: 240 }, "https://placehold.co/720x480/f5f0e6/2b2924?text=Image"))}
          />
          <InsertTile
            label="URL image"
            sublabel="Use field below"
            icon={<I.ArrowRight width={14} height={14} />}
            onClick={insertImageUrl}
          />
        </div>
        <div className="flex gap-2">
          <input
            type="url"
            value={imageUrl}
            onChange={(e) => setImageUrl(e.target.value)}
            placeholder="https://..."
            className="field-input min-w-0 flex-1"
          />
          <button type="button" className="field-input w-auto px-3 text-[12px]" onClick={insertImageUrl}>
            Add
          </button>
        </div>
        {mediaStatus && <p className="text-[11px] text-ink-500">{mediaStatus}</p>}
      </PanelSection>

      <PanelSection title="Symbols">
        <div className="grid grid-cols-3 gap-2">
          {SYMBOLS.map((symbol) => (
            <button
              key={symbol.name}
              type="button"
              onClick={() => insertSingle(imageLayer(symbol.name, { x: 0, y: 0, w: 96, h: 96 }, svgDataUrl(symbol.svg), "contain"))}
              className="flex h-16 flex-col items-center justify-center gap-1 rounded-md border border-ink-300/70 bg-surface-raised px-2 text-[10px] font-medium text-ink-700 transition hover:border-ink-700 hover:text-ink-900"
            >
              <span className="text-lg leading-none">{symbol.glyph}</span>
              <span className="truncate">{symbol.name}</span>
            </button>
          ))}
        </div>
      </PanelSection>
    </>
  );
}

function InsertTile({
  label,
  sublabel,
  icon,
  onClick,
}: {
  label: string;
  sublabel: string;
  icon: React.ReactNode;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex min-h-[72px] flex-col items-start justify-between rounded-md border border-ink-300/70 bg-surface-raised px-2.5 py-2 text-left text-ink-700 transition hover:border-ink-700 hover:text-ink-900"
    >
      <span className="text-ink-500">{icon}</span>
      <span>
        <span className="block text-[11px] font-medium">{label}</span>
        <span className="block truncate text-[9.5px] uppercase text-ink-500" style={{ letterSpacing: "0.08em" }}>
          {sublabel}
        </span>
      </span>
    </button>
  );
}

const TEXT_PRESETS = [
  { name: "Title", preview: "48 / bold", text: "Slide title", w: 560, h: 76, style: { font_size_px: 52, font_weight: 700, line_height: 1.05 } },
  { name: "Subtitle", preview: "28 / medium", text: "Concise supporting line", w: 620, h: 58, style: { font_size_px: 28, font_weight: 500, line_height: 1.18 } },
  { name: "Body", preview: "18 / regular", text: "Body copy for explanation or notes.", w: 520, h: 86, style: { font_size_px: 18, font_weight: 400, line_height: 1.45 } },
  { name: "Caption", preview: "13 / muted", text: "Caption or source note", w: 360, h: 36, style: { font_size_px: 13, font_weight: 400, line_height: 1.25, effects: { fill: "#6d665d" } } },
  { name: "Metric", preview: "64 / number", text: "90%", w: 220, h: 88, style: { font_size_px: 64, font_weight: 700, line_height: 1, effects: { fill: "#176448" } } },
  { name: "Label", preview: "12 / tracked", text: "SECTION LABEL", w: 260, h: 32, style: { font_size_px: 12, font_weight: 600, line_height: 1.1, letter_spacing: 3, effects: { fill: "#6d665d" } } },
  { name: "Quote", preview: "34 / serif", text: "“A crisp takeaway belongs here.”", w: 620, h: 106, style: { font_family: "Playfair Display", font_size_px: 34, font_weight: 600, line_height: 1.15 } },
] as const;

function textLayer(
  name: string,
  text: string,
  bbox: RelBox,
  style: Partial<Layer> = {},
): Layer {
  return {
    layer_id: nextId("lyr"),
    name,
    kind: "text",
    z_index: 1,
    bbox,
    text,
    font_family: style.font_family ?? "Inter",
    font_size_px: style.font_size_px ?? 24,
    font_weight: style.font_weight ?? 500,
    font_style: style.font_style ?? "normal",
    line_height: style.line_height ?? 1.2,
    letter_spacing: style.letter_spacing,
    align: style.align ?? "left",
    text_transform: style.text_transform ?? "none",
    list_style: style.list_style ?? "none",
    effects: style.effects ?? { fill: "#1f1f1f" },
    visible: true,
  };
}

function shapeLayer(
  name: string,
  bbox: RelBox,
  patch: Partial<Layer> = {},
): Layer {
  return {
    layer_id: nextId("lyr"),
    name,
    kind: "shape",
    shape_kind: "rect",
    z_index: 1,
    bbox,
    fill_color: "#f4efe5",
    stroke_color: "#d9d1c3",
    stroke_width: 1,
    stroke_dash: "solid",
    corner_radius: 18,
    opacity: 1,
    visible: true,
    ...patch,
  };
}

function imageLayer(
  name: string,
  bbox: RelBox,
  src: string,
  fit: Layer["fit"] = "cover",
): Layer {
  return {
    layer_id: nextId("lyr"),
    name,
    kind: "image",
    z_index: 1,
    bbox,
    src,
    fit,
    object_position: { x: 0.5, y: 0.5 },
    corner_radius: 16,
    opacity: 1,
    visible: true,
  };
}

const PITCH_BLOCKS = [
  {
    name: "Metric card",
    sublabel: "Label / number / note",
    icon: <I.Layout width={14} height={14} />,
    layers: () => [
      shapeLayer("Metric card bg", { x: 0.1, y: 0.16, w: 0.32, h: 0.26 }, { fill_color: "#fbf7ec" }),
      textLayer("Metric label", "RELIABILITY", { x: 0.13, y: 0.2, w: 0.22, h: 0.04 }, { font_size_px: 12, font_weight: 700, letter_spacing: 2.4, effects: { fill: "#6d665d" } }),
      textLayer("Metric value", "90%", { x: 0.13, y: 0.25, w: 0.18, h: 0.1 }, { font_size_px: 54, font_weight: 700, line_height: 1, effects: { fill: "#176448" } }),
      textLayer("Metric note", "Recovered with visible checkpoints.", { x: 0.13, y: 0.35, w: 0.24, h: 0.06 }, { font_size_px: 14, font_weight: 400, line_height: 1.25, effects: { fill: "#4a453f" } }),
    ],
  },
  {
    name: "Comparison row",
    sublabel: "Before / after",
    icon: <I.ArrowRight width={14} height={14} />,
    layers: () => [
      shapeLayer("Before card", { x: 0.1, y: 0.2, w: 0.33, h: 0.24 }, { fill_color: "#f6eee3" }),
      shapeLayer("After card", { x: 0.57, y: 0.2, w: 0.33, h: 0.24 }, { fill_color: "#edf4ee" }),
      textLayer("Before label", "Before", { x: 0.13, y: 0.24, w: 0.18, h: 0.05 }, { font_size_px: 18, font_weight: 700 }),
      textLayer("After label", "After", { x: 0.6, y: 0.24, w: 0.18, h: 0.05 }, { font_size_px: 18, font_weight: 700, effects: { fill: "#176448" } }),
      textLayer("Before note", "Manual inspection hides failure points.", { x: 0.13, y: 0.32, w: 0.24, h: 0.07 }, { font_size_px: 14, line_height: 1.25 }),
      textLayer("After note", "Visible traces make recovery routine.", { x: 0.6, y: 0.32, w: 0.24, h: 0.07 }, { font_size_px: 14, line_height: 1.25 }),
    ],
  },
  {
    name: "Callout",
    sublabel: "Accent note",
    icon: <I.SparkleQuiet width={14} height={14} />,
    layers: () => [
      shapeLayer("Callout bg", { x: 0.12, y: 0.2, w: 0.58, h: 0.18 }, { fill_color: "#f8f2e8", corner_radius: 14 }),
      shapeLayer("Callout accent", { x: 0.12, y: 0.2, w: 0.012, h: 0.18 }, { fill_color: "#176448", stroke_width: 0, corner_radius: 8 }),
      textLayer("Callout title", "Key takeaway", { x: 0.16, y: 0.23, w: 0.32, h: 0.05 }, { font_size_px: 20, font_weight: 700 }),
      textLayer("Callout body", "Make the system state visible before asking people to trust it.", { x: 0.16, y: 0.3, w: 0.42, h: 0.06 }, { font_size_px: 15, line_height: 1.3 }),
    ],
  },
  {
    name: "Quote block",
    sublabel: "Editorial pull quote",
    icon: <I.Asterism width={14} height={14} />,
    layers: () => [
      textLayer("Quote mark", "“", { x: 0.11, y: 0.15, w: 0.08, h: 0.12 }, { font_family: "Playfair Display", font_size_px: 84, line_height: 0.9, effects: { fill: "#c3b7a5" } }),
      textLayer("Quote", "Reliable agents need visible checkpoints.", { x: 0.18, y: 0.18, w: 0.58, h: 0.13 }, { font_family: "Playfair Display", font_size_px: 34, font_weight: 600, line_height: 1.08 }),
      textLayer("Quote source", "Design principle", { x: 0.18, y: 0.34, w: 0.26, h: 0.04 }, { font_size_px: 12, font_weight: 700, letter_spacing: 2, effects: { fill: "#6d665d" } }),
    ],
  },
  {
    name: "Chart bars",
    sublabel: "Two data bars",
    icon: <I.AlignLeft width={14} height={14} />,
    layers: () => [
      textLayer("Chart title", "Before / After", { x: 0.12, y: 0.15, w: 0.3, h: 0.05 }, { font_size_px: 22, font_weight: 700 }),
      textLayer("Before metric", "22%", { x: 0.28, y: 0.25, w: 0.12, h: 0.06 }, { font_size_px: 28, font_weight: 700, align: "right", effects: { fill: "#92342e" } }),
      shapeLayer("Before bar", { x: 0.18, y: 0.31, w: 0.22, h: 0.045 }, { fill_color: "#92342e", stroke_width: 0, corner_radius: 0 }),
      textLayer("After metric", "90%", { x: 0.62, y: 0.39, w: 0.12, h: 0.06 }, { font_size_px: 28, font_weight: 700, align: "right", effects: { fill: "#176448" } }),
      shapeLayer("After bar", { x: 0.18, y: 0.45, w: 0.56, h: 0.045 }, { fill_color: "#176448", stroke_width: 0, corner_radius: 0 }),
    ],
  },
  {
    name: "Button / Chip",
    sublabel: "CTA + status",
    icon: <I.Check width={14} height={14} />,
    layers: () => [
      shapeLayer("Button", { x: 0.12, y: 0.22, w: 0.22, h: 0.075 }, { fill_color: "#15110d", stroke_width: 0, corner_radius: 999 }),
      textLayer("Button label", "Open trace", { x: 0.15, y: 0.242, w: 0.16, h: 0.035 }, { font_size_px: 15, font_weight: 700, align: "center", effects: { fill: "#ffffff" } }),
      shapeLayer("Chip", { x: 0.38, y: 0.22, w: 0.18, h: 0.075 }, { fill_color: "#e8e0d3", stroke_width: 0, corner_radius: 999 }),
      textLayer("Chip label", "RECOVERED", { x: 0.4, y: 0.244, w: 0.14, h: 0.03 }, { font_size_px: 12, font_weight: 700, align: "center", letter_spacing: 1.4, effects: { fill: "#5f564b" } }),
    ],
  },
] as const;

const SYMBOLS = [
  { name: "Check", glyph: "✓", svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96"><path d="M22 50l17 17 36-40" fill="none" stroke="#176448" stroke-width="10" stroke-linecap="round" stroke-linejoin="round"/></svg>' },
  { name: "Arrow right", glyph: "→", svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96"><path d="M18 48h56M52 26l22 22-22 22" fill="none" stroke="#1f1f1f" stroke-width="8" stroke-linecap="round" stroke-linejoin="round"/></svg>' },
  { name: "Star", glyph: "★", svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96"><path d="M48 13l10 24 26 2-20 17 6 26-22-14-22 14 6-26-20-17 26-2z" fill="#c7a34b"/></svg>' },
  { name: "Plus", glyph: "+", svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96"><path d="M48 18v60M18 48h60" fill="none" stroke="#1f1f1f" stroke-width="9" stroke-linecap="round"/></svg>' },
  { name: "Quote", glyph: "“", svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96"><text x="18" y="70" font-size="76" font-family="Georgia,serif" fill="#92342e">“</text><text x="48" y="70" font-size="76" font-family="Georgia,serif" fill="#92342e">”</text></svg>' },
  { name: "Play", glyph: "▶", svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96"><circle cx="48" cy="48" r="36" fill="#15110d"/><path d="M40 31l26 17-26 17z" fill="#fff"/></svg>' },
] as const;

function svgDataUrl(svg: string) {
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}
