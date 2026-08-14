"""Pydantic runtime models for AutoDesign.

This branch removes the SFT/RL training trace product surface. The
models below are the engineering primitives tools, agents, the web shim, and
the CLI pass around while producing design artifacts. Durable preference
signals now live in design-session events instead of model-decision traces.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .util.english_text import is_substantially_english

from .quality_assets import VisualProfileId


LandingStyle = Literal[
    "minimalist", "editorial", "neubrutalism",
    "glassmorphism", "claymorphism", "liquid-glass",
]


class DesignSystem(BaseModel):
    """Landing-specific design-system selector (v1.0 #8.5).

    One of the six bundled styles. The HTML renderer loads the matching
    `assets/design-systems/<style>.css` at render time and inlines it into
    the output HTML, so the file stays self-contained after distribution.
    """
    style: LandingStyle = "minimalist"
    accent_color: str | None = None      # override the style's --ld-accent token
    font_pairing: str | None = None      # free-text planner hint, not enforced
    # v1.3 tri-state — None = auto (nav rendered when section_count >= 4),
    # True/False = explicit opt-in/out. Renderer at render time.
    show_nav: bool | None = None


DeckExportMode = Literal["html", "hybrid", "visual", "editable"]
HtmlFrameKind = Literal["canvas", "slide", "section", "scene"]
HtmlBlockKind = Literal[
    "text", "image", "table", "metric", "quote", "shape", "caption",
    "chart", "embed", "group",
]
DeckHtmlBlockKind = Literal[
    "text", "image", "table", "metric", "quote", "shape", "caption",
]
DeckHtmlLayout = Literal[
    "full_bleed_cover",
    "editorial_split",
    "visual_grid",
    "metric_cards",
    "comparison",
    "timeline",
    "process_flow",
    "closing_action",
]
HtmlFrameRenderMode = Literal["scene_graph", "authored_html"]
VideoVoicePreset = Literal["male", "female"]
VideoSubtitleFormat = Literal["srt", "vtt"]
KOKORO_VOICE_BY_PRESET: dict[VideoVoicePreset, str] = {
    "male": "am_michael",
    "female": "af_heart",
}
PosterSizePreset = Literal[
    "a0_portrait",
    "a0_landscape",
    "a1_portrait",
    "a1_landscape",
    "36x48_portrait",
    "36x48_landscape",
    "42x48_portrait",
    "42x48_landscape",
    "conference-poster-portrait",
    "academic-wide-2x1",
    "academic-landscape-1.414",
    "neurips-portrait",
    "cvpr-landscape",
    "icml-portrait",
    "custom",
]
PosterSizeSource = Literal["user", "template", "brief", "fallback", "inferred", "custom"]


class PosterSizeMetadata(BaseModel):
    """Physical poster size metadata for HTML/PDF export.

    Pixel canvas remains authoritative on DesignSpec.canvas; this model tells
    browser/PDF export what physical page size that pixel canvas represents.
    """
    preset: PosterSizePreset | None = None
    width_mm: float | None = None
    height_mm: float | None = None
    width_in: float | None = None
    height_in: float | None = None
    orientation: Literal["portrait", "landscape", "square", "custom"] | None = None
    source: PosterSizeSource | None = None
    label: str | None = None

    @model_validator(mode="after")
    def _positive_physical_size(self) -> "PosterSizeMetadata":
        for name in ("width_mm", "height_mm", "width_in", "height_in"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive when supplied")
        return self


class DeckHtmlBlock(BaseModel):
    """Structured HTML-first deck block.

    The planner describes slide content semantically; the HTML deck renderer
    turns these blocks into editable `.od-layer` DOM. `layer_id` connects image
    and table blocks to generated/ingested assets in `ctx.state.rendered_layers`.
    Text block `style` may carry native typography controls:
    font_family/font_size_px/font_weight/font_style/line_height/letter_spacing/
    text_transform/fill/align.
    """
    block_id: str
    kind: DeckHtmlBlockKind
    role: str | None = None
    layer_id: str | None = None
    text: str | None = None
    title: str | None = None
    items: list[str] = Field(default_factory=list)
    bbox: dict[str, int] | None = None
    src_path: str | None = None
    prompt: str | None = None
    aspect_ratio: str | None = None
    rows: list[list[str]] | None = None
    headers: list[str] | None = None
    caption: str | None = None
    col_highlight_rule: list[str] | None = None
    style: dict[str, Any] = Field(default_factory=dict)
    slot_id: str | None = None
    panel_role: str | None = None
    layout_archetype: str | None = None
    source: str | None = None
    source_id: str | None = None
    evidence_quote: str | None = None
    evidence_source: str | None = None
    covers: list[str] = Field(default_factory=list)


class DeckHtmlSlide(BaseModel):
    slide_id: str
    title: str | None = None
    subtitle: str | None = None
    layout: DeckHtmlLayout = "editorial_split"
    blocks: list[DeckHtmlBlock] = Field(default_factory=list)
    speaker_notes: str | None = None
    style: dict[str, Any] = Field(default_factory=dict)
    layout_plan: dict[str, Any] | None = None


class DeckHtmlSpec(BaseModel):
    title: str | None = None
    theme: dict[str, Any] = Field(default_factory=dict)
    slides: list[DeckHtmlSlide] = Field(default_factory=list)


class FrameSlot(BaseModel):
    """Named region in a frame-level spatial storyboard."""
    slot_id: str
    role: str
    bbox: dict[str, int]
    required: bool = False
    content_policy: str | None = None
    max_text_words: int | None = None
    min_visual_area_ratio: float | None = None
    parent_slot_id: str | None = None
    panel_job: str | None = None
    text_budget: str | None = None
    visual_ids: list[str] = Field(default_factory=list)
    space_fill_policy: str | None = None


class FrameLayoutPlan(BaseModel):
    """Frame-level layout skeleton used before concrete blocks are placed."""
    archetype: str
    margin_px: int | None = None
    gutter_px: int | None = None
    slots: list[FrameSlot] = Field(default_factory=list)
    notes: str | None = None


class HtmlBlock(BaseModel):
    """Canonical HTML artifact block shared by poster, deck, landing, and video.

    This is the v1 bridge scene-graph node. Existing legacy structures still
    load, but new planners should prefer `DesignSpec.html_artifact`.
    """
    block_id: str
    kind: HtmlBlockKind
    role: str | None = None
    layer_id: str | None = None
    text: str | None = None
    title: str | None = None
    items: list[str] = Field(default_factory=list)
    bbox: dict[str, int] | None = None
    src_path: str | None = None
    prompt: str | None = None
    aspect_ratio: str | None = None
    image_size: str | None = None
    rows: list[list[str]] | None = None
    headers: list[str] | None = None
    caption: str | None = None
    href: str | None = None
    variant: str | None = None
    editable: bool = True
    source: str | None = None
    source_id: str | None = None
    source_text: str | None = None
    evidence_quote: str | None = None
    evidence_source: str | None = None
    slot_id: str | None = None
    panel_role: str | None = None
    is_identity_asset: bool | None = None
    identity_asset_id: str | None = None
    identity_asset_role: str | None = None
    identity_entity_name: str | None = None
    identity_required_to_place: bool | None = None
    identity_allowed_to_place: bool | None = None
    identity_primary: bool | None = None
    identity_asset_intent: str | None = None
    identity_group: str | None = None
    canonical_entity_key: str | None = None
    asset_id: str | None = None
    asset_type: str | None = None
    covers: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    style: dict[str, Any] = Field(default_factory=dict)
    children: list["HtmlBlock"] = Field(default_factory=list)


class HtmlFrame(BaseModel):
    """One canvas/slide/section/scene in the canonical HTML artifact graph."""
    frame_id: str
    kind: HtmlFrameKind
    role: str | None = None
    title: str | None = None
    subtitle: str | None = None
    layout: str | None = None
    bbox: dict[str, int] | None = None
    duration_s: float | None = None
    transition: str | None = None
    speaker_notes: str | None = None
    source: str | None = None
    layout_plan: FrameLayoutPlan | None = None
    render_mode: HtmlFrameRenderMode | None = None
    authored_body_html: str | None = None
    authored_css: str | None = None
    poster_size: PosterSizeMetadata | None = None
    style: dict[str, Any] = Field(default_factory=dict)
    blocks: list[HtmlBlock] = Field(default_factory=list)


class HtmlArtifactSpec(BaseModel):
    """Canonical HTML-first scene graph.

    Frame meaning is target-specific: poster = one canvas, deck = slides,
    landing = sections, video = timeline scenes.
    """
    title: str | None = None
    target: str | None = None
    theme: dict[str, Any] = Field(default_factory=dict)
    frames: list[HtmlFrame] = Field(default_factory=list)


class VideoSceneContract(BaseModel):
    """One timed, narrated scene in an HTML-first conference video."""

    scene_id: str
    title: str
    start_s: float = Field(ge=0)
    duration_s: float = Field(ge=1)
    narration_text: str = Field(min_length=1)

    @field_validator("narration_text")
    @classmethod
    def _english_narration_required(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not is_substantially_english(cleaned):
            raise ValueError("narration_text must be substantially English narration")
        return cleaned


class VideoVoiceMetadata(BaseModel):
    """Deterministic local TTS selection recorded with every delivery."""

    preset: VideoVoicePreset
    engine: Literal["kokoro"] = "kokoro"
    kokoro_voice_id: str
    mapping_version: Literal["kokoro-v1"] = "kokoro-v1"
    language: Literal["en"] = "en"


VIDEO_MIN_DURATION_S = 300
VIDEO_MAX_DURATION_S = 600
VIDEO_MEDIA_DURATION_TOLERANCE_S = 0.5


class VideoMediaProbe(BaseModel):
    """Normalized ffprobe evidence for an accepted MP4."""

    video_codec: Literal["h264"]
    pixel_format: Literal["yuv420p"]
    audio_codec: Literal["aac"]
    width: Literal[1920]
    height: Literal[1080]
    fps: Literal[30]
    duration_s: float = Field(
        ge=VIDEO_MIN_DURATION_S - VIDEO_MEDIA_DURATION_TOLERANCE_S,
        le=VIDEO_MAX_DURATION_S + VIDEO_MEDIA_DURATION_TOLERANCE_S,
    )
    video_stream_duration_s: float | None = None
    audio_stream_duration_s: float | None = None
    video_frame_count: int | None = None
    subtitle_codec: Literal["mov_text"] | None = None
    subtitle_forced: bool | None = None


class VideoDeliveryContract(BaseModel):
    """Paper-to-video delivery contract for a conference-length MP4."""

    target_duration_s: int = Field(
        default=360,
        ge=VIDEO_MIN_DURATION_S,
        le=VIDEO_MAX_DURATION_S,
    )
    width: Literal[1920] = 1920
    height: Literal[1080] = 1080
    fps: Literal[30] = 30
    scene_count_min: Literal[10] = 10
    scene_count_max: Literal[14] = 14
    narration_language: Literal["en"] = "en"
    subtitle_formats: list[VideoSubtitleFormat] = Field(
        default_factory=lambda: ["srt", "vtt"]
    )
    source_format: Literal["html-first-hyperframes"] = "html-first-hyperframes"
    voice_preset: VideoVoicePreset = "female"
    voice: VideoVoiceMetadata | None = None
    scenes: list[VideoSceneContract]

    @model_validator(mode="after")
    def _validate_delivery(self) -> "VideoDeliveryContract":
        if not self.scene_count_min <= len(self.scenes) <= self.scene_count_max:
            raise ValueError("video delivery requires 10-14 scenes")
        if len({scene.scene_id for scene in self.scenes}) != len(self.scenes):
            raise ValueError("video scene_id values must be unique")
        ordered = sorted(self.scenes, key=lambda scene: scene.start_s)
        if ordered != self.scenes:
            raise ValueError("video scenes must be ordered by start_s")
        timeline_end = max(scene.start_s + scene.duration_s for scene in self.scenes)
        if not VIDEO_MIN_DURATION_S <= timeline_end <= VIDEO_MAX_DURATION_S:
            raise ValueError(
                "video scene timeline must end within 300-600 seconds"
            )
        if abs(timeline_end - self.target_duration_s) > 2:
            raise ValueError("video scene timeline must match target_duration_s within 2 seconds")

        expected_voice = VideoVoiceMetadata(
            preset=self.voice_preset,
            kokoro_voice_id=KOKORO_VOICE_BY_PRESET[self.voice_preset],
        )
        if self.voice is None:
            self.voice = expected_voice
        elif self.voice != expected_voice:
            raise ValueError("voice metadata must match the deterministic Kokoro preset mapping")
        if self.subtitle_formats != ["srt", "vtt"]:
            raise ValueError("canonical video subtitles must include SRT and VTT")
        return self


class ArtifactType(str, Enum):
    """What kind of design artifact is being produced in the current session slot.

    Drives renderer selection and prompts the planner with artifact-specific
    layout guidance. A chat session may contain multiple artifacts (mix of
    types); the `switch_artifact_type` tool changes this mid-session.
    """
    POSTER = "poster"       # vertical / horizontal, absolutely-positioned layers
    DECK = "deck"           # N slides, HTML-first editable frames
    LANDING = "landing"     # self-contained HTML one-pager with flow layout
    VIDEO = "video"         # HTML-first HyperFrames timeline with validated MP4 delivery


AttemptSafetyState = Literal["ready", "ready_with_warnings", "blocked"]
AttemptSelectionState = Literal[
    "requested",
    "terminating",
    "promoting",
    "delivering",
    "complete",
    "failed",
]


class AttemptIssue(BaseModel):
    issue_id: str
    message: str


class AttemptCandidate(BaseModel):
    schema_version: Literal[1] = 1
    candidate_id: str
    run_id: str
    artifact_type: ArtifactType
    attempt: int = Field(ge=1)
    max_attempts: int = Field(ge=1)
    created_at: str
    source_relative_path: str
    preview_relative_paths: list[str] = Field(default_factory=list)
    dependency_relative_paths: list[str] = Field(default_factory=list)
    browser_resource_relative_paths: list[str] | None = None
    source_sha256: str
    dependency_fingerprint: str
    safety_state: AttemptSafetyState
    hard_blockers: list[AttemptIssue] = Field(default_factory=list)
    warnings: list[AttemptIssue] = Field(default_factory=list)
    validation_summary_relative_path: str
    previous_candidate_id: str | None = None
    repair_source_attempt: int | None = None


class AttemptCandidateIndex(BaseModel):
    schema_version: Literal[1] = 1
    run_id: str
    candidate_ids: list[str] = Field(default_factory=list)
    manifest_relative_paths: list[str] = Field(default_factory=list)
    updated_at: str


class AttemptSelectionJournal(BaseModel):
    schema_version: Literal[1] = 1
    run_id: str
    candidate_id: str
    candidate_sha256: str
    source_attempt: int = Field(ge=1)
    idempotency_key: str
    state: AttemptSelectionState
    artifact_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    updated_at: str


Status = Literal["ok", "error"]
ErrorCategory = Literal[
    "validation",        # tool_args failed pydantic / schema validation
    "safety_filter",     # NBP / Anthropic safety filter rejected the request
    "api",               # upstream API error (network / 5xx / auth)
    "timeout",           # call exceeded its budget
    "not_found",         # referenced ID / asset doesn't exist
    "unsupported_format",  # ingest_document on an unrecognized file type
    "parse_error",       # critic / ingest model output failed to parse
    "provider_unavailable",  # v2.7.5 — model id is broken at the provider
                             # (404 / no-endpoints-for-modality / unlisted);
                             # distinct from `api` so the planner can pivot
                             # rather than retry the same call.
    "unknown",
]
LayerKind = Literal[
    "background",    # full-canvas raster (poster/deck only)
    "text",          # rendered text layer (poster) OR inline HTML text (landing)
    "brand_asset",   # legacy brand/identity imagery kind
    "group",         # organisational grouping (unused in v1.0)
    "section",       # landing section container (v1.0 #8)
    "image",         # NBP-generated inline image inside a landing section (v1.0 #8.75)
    "slide",         # deck slide container: children hold text/image elements (v1.0 #7)
    "table",         # v1.2 paper2any: structured data (rows/headers) — renderers
                     # produce native PPTX / HTML tables instead of cropped images.
                     # src_path holds a PIL-drawn PNG fallback for PSD/SVG paths.
    "shape",         # poster/deck vector decoration or panel chrome; supported by
                     # HTML/SVG composite paths and html_artifact adapters.
    "cta",           # v1.3 landing call-to-action button — renders as <a role="button">
                     # with href + variant. Per-design-system styling via .ld-cta--*.
    "callout",       # v2.6 deck annotation: highlight a sub-region of a sibling
                     # picture/table, optional text label + arrow connector.
                     # Slide-only (kind="slide" parent). Renderer adds shapes
                     # ON TOP of the anchor's bbox via python-pptx primitives.
]
Verdict = Literal["pass", "revise", "fail"]
Severity = Literal["blocker", "major", "minor"]
DesignFeedbackSeverity = Literal["blocker", "high", "medium", "low"]
# v2.8.1 — slide archetype taxonomy (Phase 1 lands the first 4; the rest are
# placeholders so v2.8.2 / v2.8.3 can ship without re-touching the schema).
# Default value on `LayerNode.archetype` is `"evidence_snapshot"` because that
# label routes through the renderer's existing inline default path — every
# pre-v2.8.1 deck still renders byte-for-byte unchanged.
SlideArchetype = Literal[
    # Phase 1 (v2.8.1)
    "cover_editorial",
    "evidence_snapshot",
    "takeaway_list",
    "thanks_qa",
    # Phase 2 (v2.8.2 — placeholders, fall through to default render)
    "pipeline_horizontal",
    "tension_two_column",
    "section_divider",
    # Phase 3 (v2.8.3 — placeholders, fall through to default render)
    "cover_technical",
    "residual_stack_vertical",
    "conflict_vs_cooperation",
]
IssueCategory = Literal[
    "typography", "composition", "brand",
    "legibility", "cultural", "artifact",
    # v1.0 #8.5-fix: landing critique often flags text-content concerns that
    # don't fit the poster-visual vocabulary — "copy" covers headline/body
    # wording quality, "content" covers section balance / length / pacing.
    "copy", "content",
    # v2.7 — fabricated number / unverifiable claim in body. Emitted by the
    # critic when a kind="text" body layer contains numeric tokens but no
    # `evidence_quote`, OR when the rendered slide contains "[?]" markers
    # left by the composite-stage provenance validator.
    "provenance",
]


class SafeZone(BaseModel):
    """Top-left origin, pixel units. Used for both bbox and reserved regions."""
    x: int
    y: int
    w: int
    h: int
    purpose: Literal["title", "subtitle", "stamp", "logo", "body"] | None = None

    @field_validator("w", "h")
    @classmethod
    def _positive_dim(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("w/h must be positive")
        return v

    @field_validator("x", "y")
    @classmethod
    def _nonneg_pos(cls, v: int) -> int:
        if v < 0:
            raise ValueError("x/y must be >= 0")
        return v


class TextEffect(BaseModel):
    stroke: dict[str, Any] | None = None     # {color: "#hex", width: int}
    shadow: dict[str, Any] | None = None     # {color: "#hex", dx: int, dy: int, blur: int}
    fill: str = "#000000"


class LayerNode(BaseModel):
    """Polymorphic layer descriptor. Fields populated depend on `kind`.

    `bbox` is optional as of v1.0 #8: poster/deck layers use pixel coords;
    landing layers (kind="section" and their text children) use flow layout
    with no pixel bbox.
    """
    layer_id: str
    name: str
    kind: LayerKind
    z_index: int
    bbox: SafeZone | None = None

    # text-only
    text: str | None = None
    font_family: str | None = None
    font_size_px: int | None = None
    font_weight: int | None = None
    font_style: Literal["normal", "italic"] | None = None
    line_height: float | None = None
    letter_spacing: float | None = None
    text_transform: Literal["none", "uppercase"] | None = None
    align: Literal["left", "center", "right"] | None = None
    effects: TextEffect | None = None

    # background-only
    prompt: str | None = None
    aspect_ratio: str | None = None
    image_size: str | None = None

    # any
    src_path: str | None = None
    children: list["LayerNode"] = Field(default_factory=list)

    # v1.2 paper2any — table layers (kind="table") carry structured data
    # that renderers turn into native PPTX tables / HTML <table> elements.
    # `headers` is optional (first data row doubles as header when absent).
    # `caption` sits above or below the table depending on the renderer.
    # `src_path` is a PIL-rendered PNG fallback used by PSD/SVG paths that
    # don't have a live-table primitive.
    # `col_highlight_rule` — one entry per column, "max"/"min"/"" —
    # renderers bold the winning row per column. Enables "highlight
    # LongCat-Next's winning metrics" without the planner duplicating
    # every cell as bold/non-bold.
    rows: list[list[str]] | None = None
    headers: list[str] | None = None
    caption: str | None = None
    col_highlight_rule: list[str] | None = None

    # v1.3 landing interactivity — cta-only
    href: str | None = None
    variant: Literal["primary", "secondary", "ghost"] | None = None

    # v2.3 deck speaker notes — slide-only (kind="slide"); ignored on other kinds.
    # Populates `slide.notes_slide.notes_text_frame.text` in the PPTX renderer,
    # so the notes show in PowerPoint / Keynote presenter view but not on slides.
    # v2.7.2 — read by `slide_id` (this LayerNode), NEVER by enumerate index.
    # Cloud Design dogfood (2026-04-26) showed an off-by-one cascade when
    # slides were inserted post-notes-generation; binding by id eliminates it.
    speaker_notes: str | None = None

    # v2.7.2 deck section number — slide-only (kind="slide"). Optional label
    # like "§1", "§2.1", "§3" prepended to the slide title at render time.
    # Designer does NOT need to keep this stable across iterations: the
    # composite stage runs `apply_section_policy` (default = "renumber")
    # to assign monotonic section numbers in slide order before a deck renderer
    # sees the spec. Set `SECTION_NUMBER_POLICY=preserve` to opt out
    # of renumbering, or `="strip"` to drop the field entirely.
    section_number: str | None = None

    # Deck semantics — slide-only (kind="slide"); consumed by legacy
    # layer-based compatibility exports and HTML adapters.
    role: Literal[
        "cover", "content", "content_with_figure",
        "content_with_table", "section_divider", "closing",
    ] | None = None

    # v2.8.1 deck archetype — slide-only (kind="slide"); selects the layout
    # function in `tools/archetypes/`. The renderer dispatches on this field
    # before falling through to the default inline `_render_slide` path.
    # Default = "evidence_snapshot" because that label is wired to the
    # default-render fallback (zero behavior change for pre-v2.8.1 decks).
    archetype: SlideArchetype = "evidence_snapshot"

    # Deprecated legacy deck field retained only so prior serialized layer-graph
    # runs can be read and exported. New deck generation uses `html_artifact`
    # blocks and never emits it.
    template_slot: str | None = None

    # v2.6 callout — kind="callout" only; child of a slide alongside the
    # picture/table it annotates. Renderer overlays a shape (rectangle
    # highlight, ellipse circle, or textbox label) on top of the anchor's
    # bbox. Optional thin connector line from label to highlight. Used
    # for "presentation-native" emphasis on dense paper figures (e.g.
    # highlight the winning row of a benchmark table, label a panel of
    # an ablation grid).
    anchor_layer_id: str | None = None
                                  # references a sibling kind="image" /
                                  # "table" on the same slide; renderer
                                  # reads that shape's bbox to scope the
                                  # callout's coordinate space.
    callout_style: Literal["highlight", "label", "circle"] | None = None
                                  # highlight = rectangle outline only
                                  # label     = text box with thin border
                                  # circle    = ellipse outline
    callout_text: str | None = None  # label content (label style only)
    callout_region: SafeZone | None = None
                                  # sub-region within anchor.bbox to point
                                  # at, in image-pixel coords (top-left
                                  # origin relative to the anchor's bbox
                                  # top-left). If None, callout points at
                                  # the anchor's full bbox.
    arrow: bool = False           # if True + style=label, draw a thin
                                  # connector from label center to the
                                  # callout_region's nearest edge.

    # v2.7 Provenance — text-only; required when `text` contains a
    # significant numeric token (>=4 digits, decimal, or K/M/T/B/% suffix).
    # `evidence_quote` MUST be a verbatim substring of the ingested source
    # text; the composite-stage auditor (autodesign/util/provenance.py)
    # rejects bullets that fail substring match. `evidence_source` is a
    # free-form trace hint ("ingest_table_06 row LongCat-Next" /
    # "ingest_p_12") used only for human inspection — not validated.
    # Motivation: 2026-04-25 longcat-next dogfood produced 9 fabricated
    # numbers across slides 4/6/8/9 (e.g. "PSNR 28.5 → 22.1 dB" with
    # paper-real values being 20.88/21.86/30.52/18.16). Pure prompt rules
    # backfired — the model met "number + named rival" by inventing
    # numbers. Machine-checkable provenance is the gate.
    evidence_quote: str | None = None
    evidence_source: str | None = None

    # v2.8.0 Claim graph coverage — slide-only (kind="slide"). Holds the
    # ClaimGraph node ids (T*/M*/E*/I*) that this slide presents to the
    # audience. Populated by the planner when `Brief.claim_graph` is
    # non-None; consumed by the v2.7.3 critic's `claim_coverage` rule to
    # detect missing tensions / mechanisms / evidence. Empty list = no
    # coverage claimed (the v2.7.3 baseline behavior — critic skips
    # claim_coverage when the deck-wide union of `covers` is empty).
    covers: list[str] = Field(default_factory=list)


# v2.8.0 — Claim graph nodes.
#
# When the input is a paper PDF, the `ClaimGraphExtractor` sub-agent runs
# between the enhancer and the planner and emits one `ClaimGraph` capturing
# the paper's argumentative arc (thesis → tensions → mechanisms → evidence
# → implications). The planner then orders slides along that arc instead of
# walking paper chapter order, and the critic uses it to flag uncovered
# nodes (`category="claim_coverage"`).
#
# Hard provenance rule: every `EvidenceNode.raw_quote` MUST be a verbatim
# substring of the paper raw_text. `autodesign/util/claim_graph_validator.py`
# enforces this at extraction time AND `autodesign/util/provenance.py`
# re-checks it during composite. Fabricated quotes drop the whole graph
# back to None and the planner degrades to v2.7.3 chapter-order behavior.


class TensionNode(BaseModel):
    """One unresolved-question / paradox the paper sets up.

    Examples: "understanding-generation conflict", "dual bottleneck in
    diffusion samplers". `evidence_anchor` is a free-form pointer ("fig 7",
    "section 3.2") used by the planner to label the tension on slide.
    """
    id: str
    name: str
    description: str
    evidence_anchor: str | None = None


class MechanismNode(BaseModel):
    """A mechanism / method / paradigm the paper introduces.

    `resolves` lists tension ids the mechanism is claimed to address. Used
    by the critic to detect "mechanism without a tension" (orphan slide)
    and "tension without a mechanism" (uncovered tension).
    """
    id: str
    name: str
    resolves: list[str] = Field(default_factory=list)
    description: str


class EvidenceNode(BaseModel):
    """One concrete result / number / table cell from the paper.

    `raw_quote` MUST be a verbatim substring of the paper raw_text. The
    extractor is told to delete any evidence whose quote it cannot ground;
    the validator double-checks. `supports` references mechanism ids.
    """
    id: str
    metric: str
    source: str
    raw_quote: str
    supports: list[str] = Field(default_factory=list)


class ImplicationNode(BaseModel):
    """A downstream consequence / takeaway the paper draws.

    `derives_from` references mechanism + evidence ids the implication is
    grounded in. Implication slides are usually the closing 1-2 slides of
    the talk arc.
    """
    id: str
    description: str
    derives_from: list[str] = Field(default_factory=list)


class ClaimGraph(BaseModel):
    """Paper-as-argument graph; consumed by planner + critic when present.

    Construction is owned by `autodesign/agents/claim_graph_extractor.py`.
    Validation (substring + ref integrity) is owned by
    `autodesign/util/claim_graph_validator.py`.
    """
    paper_title: str
    paper_anchor: str
    thesis: str
    tensions: list[TensionNode] = Field(default_factory=list)
    mechanisms: list[MechanismNode] = Field(default_factory=list)
    evidence: list[EvidenceNode] = Field(default_factory=list)
    implications: list[ImplicationNode] = Field(default_factory=list)


class DeckPlanOutlineItem(BaseModel):
    """One planned slide in the backend deck planning layer."""
    slide_index: int
    title: str
    role: str = "content"
    chapter: str = ""
    communication_job: str = ""
    assertion_title: str = ""
    scope: str = ""
    layout_family: str = ""
    content: str = ""
    visual_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    speaker_note: str | None = None
    speaker_note_intent: str = ""


class DeckPlan(BaseModel):
    """Structured deck length + outline plan.

    `slide_count=None` means the plan is still pending/refinement-only and
    should not enforce an exact count yet.
    """
    artifact_type: Literal["deck"] = "deck"
    deck_subtype: str = "general"
    talk_profile: Literal[
        "short_overview",
        "standard_conference",
        "full_formal",
    ] = "standard_conference"
    slide_count: int | None = None
    count_range: list[int] = Field(default_factory=list)
    lock_level: Literal["hard", "soft", "advisory"] = "advisory"
    status: Literal["pending", "explicit", "refined", "fallback"] = "pending"
    density_budget: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    source: str = "unknown"
    outline: list[DeckPlanOutlineItem] = Field(default_factory=list)
    document_signals: dict[str, Any] = Field(default_factory=dict)


class Brief(BaseModel):
    """v2.8.0 — typed envelope for the planner's input.

    Until v2.8.0 the brief travelled as raw `str` (+ a separate `attachments`
    list passed alongside it through `runner.py`). The new `Brief` model
    mirrors that shape so the runner / planner can carry the optional
    `claim_graph` extracted before designer.start without inventing yet
    another out-of-band channel. Existing code paths still pass the brief
    string verbatim into `DesignerLoop.run`; `Brief` is a typed reference
    object the runner stores alongside `ctx.state` for tools that need it
    (designer prompt + critic).

    `attachments` carries the resolved file paths (str so the model is
    JSON-serialisable; runner converts from Path).
    """
    text: str
    attachments: list[str] = Field(default_factory=list)
    template: str | None = None
    claim_graph: ClaimGraph | None = None


class DesignSpec(BaseModel):
    brief: str
    artifact_type: ArtifactType = ArtifactType.POSTER
    visual_profile: VisualProfileId | None = None
    canvas: dict[str, Any]                   # {w_px, h_px, dpi, aspect_ratio, color_mode:"RGB"}
    palette: list[str] = Field(default_factory=list)
    color_system: dict[str, Any] = Field(default_factory=dict)
    typography: dict[str, str] = Field(default_factory=dict)
    mood: list[str] = Field(default_factory=list)
    composition_notes: str = ""
    layer_graph: list[LayerNode] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
    design_system: DesignSystem | None = None  # landing-only (v1.0 #8.5)
    deck_export_mode: DeckExportMode = "html"  # deck-only; html is the default
    deck_plan_override_reason: str | None = None  # deck-only; records intentional count mismatch
    deck_html: DeckHtmlSpec | None = None  # deck-only HTML-first source
    html_artifact: HtmlArtifactSpec | None = None  # canonical HTML-first scene graph

    @model_validator(mode="after")
    def _canvas_required_keys(self) -> "DesignSpec":
        for k in ("w_px", "h_px"):
            if k not in self.canvas:
                raise ValueError(f"canvas missing required key: {k}")
        return self


class ThinkingBlockRecord(BaseModel):
    """One extended-thinking block captured from Claude's response.

    Anthropic returns two sub-types:
      - `thinking`: plain CoT text + opaque `signature` for verification
      - `redacted_thinking`: encrypted (text unavailable) + opaque `data`
        which we map onto `signature` to keep the record shape uniform.

    Both must round-trip back verbatim on the next turn or Anthropic 400s,
    so signatures are persisted even though we never interpret them.
    """
    thinking: str = ""              # empty when is_redacted=True
    signature: str = ""              # Anthropic-issued; opaque to us
    is_redacted: bool = False


class LegacyCritiqueIssue(BaseModel):
    """Legacy single-call critic issue (pre v2.7.3). Retained because old
    code and smoke fixtures may still construct `CritiqueResult.issues`.
    New code uses `CritiqueIssue` (the v2.7.3 sub-agent shape) below.
    """
    severity: Severity
    layer_id: str | None = None
    category: IssueCategory
    description: str
    suggested_fix: str

    @field_validator("category", mode="before")
    @classmethod
    def _coerce_unknown_category(cls, v: Any) -> Any:
        """Map unknown category strings to "artifact" rather than failing
        the whole CritiqueResult payload.

        Motivated by 2026-04-25 dogfood: Qwen-VL-Max returned
        `category="per-slide density"` (a sensible label, just not in our
        Literal). Pydantic's literal_error bubbled up, the entire critique
        was rejected, and the run terminated with reward=0.0 even though
        the deck itself was fine. Soft-coercing keeps the critic's signal
        usable while the rest of the schema stays strict.
        """
        if isinstance(v, str):
            allowed = {
                "typography", "composition", "brand", "legibility",
                "cultural", "artifact", "copy", "content",
                "provenance",  # v2.7
            }
            if v not in allowed:
                # Best-effort substring match before falling back to "artifact"
                # so "design_system_drift" still routes to "brand", etc.
                lower = v.lower()
                for cand in ("typography", "composition", "brand", "legibility",
                             "cultural", "copy", "content", "provenance"):
                    if cand in lower:
                        return cand
                return "artifact"
        return v


class CritiqueResult(BaseModel):
    """Legacy critic result (pre v2.7.3 inline-tool path).

    The runner / planner now consumes `CritiqueReport` produced by
    `agents.critic_agent.CriticAgent`; this class survives for older fixtures
    and utility code that still use its `verdict` / `score` shape.
    """
    iteration: int
    verdict: Verdict
    score: float
    issues: list[LegacyCritiqueIssue] = Field(default_factory=list)
    rationale: str = ""

    @field_validator("score")
    @classmethod
    def _score_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("score must be in [0, 1]")
        return v


# v2.7.3 — vision critic sub-agent shape.
#
# The inline `Critic` class is gone; `agents.critic_agent.CriticAgent` now
# spawns its own LLM loop, sees slide PNGs (vision for ALL artifact types,
# not just poster), and emits one of these as the terminal `report_verdict`
# tool call. The planner consumes the JSON via the unchanged `critique`
# tool signature — see `tools/critique_tool.py`.
#
# Severity vocab is intentionally distinct from the legacy `Severity`
# (`major`/`minor`) because the sub-agent operates closer to the human
# code-review vocabulary (`high`/`medium`/`low`) and we don't want to
# silently overload the legacy enum.
CritiqueIssueSeverity = Literal["blocker", "high", "medium", "low"]
CritiqueIssueCategory = Literal[
    "provenance",          # number / quote / paper terminology not in source
    "claim_coverage",      # key paper claim not represented (v2.8.0 will wire)
    "visual_hierarchy",    # title vs body vs caption sizing / contrast
    "typography",          # font choice, leading, punctuation
    "layout",              # shape overlap / out-of-bounds / cramped slots
    "narrative_flow",      # slide ordering / transitions / arc
    "factual_error",       # asserts something the paper does not support
]
CritiqueVerdict = Literal["pass", "revise", "fail"]
CritiqueRepairTool = Literal[
    "propose_design_spec",
    "edit_layer",
    "render_text_layer",
    "generate_image",
    "composite",
    "none",
]
FeedbackStage = Literal[
    "content_strategy",
    "visual_curation",
    "layout_storyboard",
    "typography_system",
    "rendering_export",
]
FeedbackRepairRoute = Literal[
    "local_refine",
    "pivot_layout_archetype",
    "revise_content_strategy",
    "revise_visual_curation",
    "revise_typography_system",
    "revise_authored_html",
    "shrink_text",
    "swap_visual",
    "resize_visual",
    "adjust_size_or_archetype",
    "none",
]


class CritiqueIssue(BaseModel):
    """One concrete issue raised by the v2.7.3 vision critic sub-agent."""
    issue_id: str | None = None
    slide_id: str | None = None
    layer_ids: list[str] = Field(default_factory=list)
    severity: CritiqueIssueSeverity
    category: CritiqueIssueCategory
    description: str
    target: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    suggested_action: str = ""
    repair_tool: CritiqueRepairTool | None = None
    stage: FeedbackStage | None = None
    repair_route: FeedbackRepairRoute | None = None
    confidence: float | None = None
    evidence_paper_anchor: str | None = None

    @field_validator("confidence")
    @classmethod
    def _confidence_range(cls, v: float | None) -> float | None:
        if v is not None and not 0.0 <= v <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        return v


class CritiqueReport(BaseModel):
    """Terminal output of `CriticAgent.critique`. Embedded verbatim into
    the planner-facing `critique` tool_result.payload so the planner can
    decide between finalize / propose_design_spec / abort."""
    score: float
    verdict: CritiqueVerdict
    issues: list[CritiqueIssue] = Field(default_factory=list)
    summary: str = ""
    iteration: int = 1
    dimension_scores: dict[str, float] = Field(default_factory=dict)
    review_coverage: dict[str, Any] = Field(default_factory=dict)

    @field_validator("score")
    @classmethod
    def _score_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("score must be in [0, 1]")
        return v

    @field_validator("dimension_scores")
    @classmethod
    def _dimension_score_range(cls, v: dict[str, float]) -> dict[str, float]:
        for name, score in v.items():
            if not 0.0 <= float(score) <= 1.0:
                raise ValueError(f"dimension_scores[{name!r}] must be in [0, 1]")
        return v

    @model_validator(mode="after")
    def _pass_requires_high_score_and_no_blockers(self) -> "CritiqueReport":
        if self.verdict == "pass":
            if self.score < 0.75:
                raise ValueError("pass verdict requires score >= 0.75")
            blockers = [
                issue for issue in self.issues
                if issue.severity == "blocker"
            ]
            if blockers:
                raise ValueError("pass verdict cannot include blocker issues")
        return self


class DesignFeedbackFinding(BaseModel):
    """One normalized environment finding emitted by composite/finalize.

    Sources may start as layout grounding issues, deterministic quality lint,
    paper-density audits, deck export warnings, or other harness checks. This
    shape is the planner-facing contract, so the runner can reason about
    blockers without knowing every legacy payload key.
    """
    id: str
    source: str
    severity: DesignFeedbackSeverity
    artifact_type: str
    message: str
    target: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    suggested_action: str = ""
    stage: FeedbackStage | None = None
    repair_route: FeedbackRepairRoute | None = None
    repairable: bool = True


class DesignFeedback(BaseModel):
    """Normalized design-environment feedback for the latest composite pass."""
    artifact_type: str
    iteration: int = 0
    findings: list[DesignFeedbackFinding] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    has_blocking_findings: bool = False

    @model_validator(mode="after")
    def _derive_counts(self) -> "DesignFeedback":
        counts = {
            "total": len(self.findings),
            "blocker": sum(1 for finding in self.findings if finding.severity == "blocker"),
            "high": sum(1 for finding in self.findings if finding.severity == "high"),
            "medium": sum(1 for finding in self.findings if finding.severity == "medium"),
            "low": sum(1 for finding in self.findings if finding.severity == "low"),
        }
        self.counts = counts
        self.has_blocking_findings = bool(
            any(finding.severity == "blocker" for finding in self.findings)
        )
        return self


class CompositionArtifacts(BaseModel):
    """Runtime model used by composite tool to track local file paths
    inside ctx.state. Product artifacts live on disk under
    out/runs/<run_id>/ and are surfaced by API/CLI result models."""
    psd_path: str | None = None
    svg_path: str | None = None
    html_path: str | None = None
    deck_html_path: str | None = None
    html_artifact_path: str | None = None
    pdf_path: str | None = None
    pptx_path: str | None = None
    preview_path: str | None = None
    layer_manifest: list[dict[str, Any]] = Field(default_factory=list)


class ToolResultRecord(BaseModel):
    """Runtime tool result passed back into the planner loop."""
    status: Status
    error_message: str | None = None
    error_category: ErrorCategory | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


TerminalStatus = Literal["pass", "revise", "fail", "max_turns", "abort"]


class RunResult(BaseModel):
    """Runtime summary returned by `PipelineRunner.run`."""
    run_id: str
    run_dir: str
    artifact_type: str
    terminal_status: TerminalStatus
    critic_verdict: CritiqueVerdict | None = None
    critic_score: float | None = None
    n_layers: int = 0
    n_critiques: int = 0
    finalize_notes: str = ""
    wall_s: float = 0.0
    designer_model: str = ""
    planner_model: str = ""
    critic_model: str = ""
    image_model: str = ""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_create_tokens: int = 0
    selected_skills: list[str] = Field(default_factory=list)
    visual_reference: dict[str, Any] = Field(default_factory=dict)
    canvas_plan: dict[str, Any] = Field(default_factory=dict)
    deck_plan: dict[str, Any] = Field(default_factory=dict)
    # v design-memory: compact style fingerprint extracted from DesignSpec.
    # palette, typography, layout density, figure source, design system.
    # Empty dict when no DesignSpec was produced (abort / max_turns runs).
    style_snapshot: dict[str, Any] = Field(default_factory=dict)


class ApplyEditsResult(BaseModel):
    """Runtime summary returned by `apply_edits`."""
    run_id: str
    run_dir: str
    parent_run_id: str | None = None
    restored_layer_ids: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    artifact_type: str = "poster"   # "poster" | "landing" — filled by apply_edits()
