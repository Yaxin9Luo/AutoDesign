"""Tool registry — name → (JSON schema, handler).

Schemas exposed verbatim to the Anthropic tool-use API. Handlers signature:
    fn(args: dict, *, ctx: ToolContext) -> ToolResultRecord
"""

from __future__ import annotations

from ._contract import ToolContext, ToolHandler  # re-export
from .apply_design_ops import apply_design_ops
from .composite import composite
from .critique_tool import critique
from .discover_paper_resources import discover_paper_resources
from .edit_layer import edit_layer
from .export_video import export_video
from .fetch_brand_asset import fetch_brand_asset
from .finalize import finalize
from .generate_background import generate_background
from .generate_image import generate_image
from .generate_visual_reference import generate_visual_reference
from .ingest_document import ingest_document
from .propose_paper_poster_html import propose_paper_poster_html
from .propose_design_spec import propose_design_spec
from .render_text_layer import render_text_layer
from .read_skill_resource import read_skill_resource
from .retrieve_paper_context import retrieve_paper_context
from .switch_artifact_type import switch_artifact_type


TOOL_SCHEMAS: list[dict] = [
    {
        "name": "switch_artifact_type",
        "description": (
            "Declare what kind of design artifact this session is producing: "
            "'poster' (fixed-canvas HTML-first artifact with editable DOM panels), 'deck' (N-slide "
            "HTML-first presentation with editable slide frames), or 'landing' (self-contained HTML "
            "one-pager with semantic sections). Call this AT THE START of a "
            "new artifact (first turn, or mid-session when the user asks for a "
            "different artifact type) before the proposal tool. Default is "
            "'poster' if you skip this; but calling explicitly is recommended "
            "so the runtime state records the artifact type explicitly. "
            "Returns a tool result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["poster", "deck", "landing"],
                    "description": (
                        "The artifact type. 'poster' = vertical/horizontal "
                        "fixed-canvas HTML-first artifact with editable DOM panels. 'deck' = N editable "
                        "HTML slide frames. 'landing' = single "
                        "self-contained HTML page with flow layout."
                    ),
                },
            },
            "required": ["type"],
        },
    },
    {
        "name": "ingest_document",
        "description": (
            "Read user-provided source files (PDF papers, markdown notes, or "
            "image files) and extract a structured manifest and source assets "
            "for HTML-first authoring. Call this FIRST when the brief prologue "
            "mentions 'Attached files:'; for academic paper posters, call "
            "propose_paper_poster_html next so the returned title, sections, "
            "figures, tables, and recommended text units drive the HTML you "
            "write next. PDF "
            "figures are extracted (via pymupdf page-render + Claude vision "
            "bbox location) and PRE-REGISTERED in rendered_layers with stable "
            "layer_ids — reference them as source-backed figure/table subjects "
            "in authored HTML with data-source-id/data-layer-id, or as image "
            "children in a DesignSpec when using the legacy path; composite hydrates src_path "
            "automatically. Standalone .png/.jpg files get a passthrough "
            "layer_id. Markdown ingestion returns raw text + any resolved "
            "relative image refs. PDF ingestion also returns recommended_figures "
            "and recommended_text_units for paper-poster planning. Returns a tool result; artifacts are the "
            "extracted / copied image PNG paths."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Absolute or ~-prefixed paths to ingest. Supported "
                        "extensions: .pdf (academic papers / reports, "
                        "Anthropic native document vision), .md/.markdown/.txt "
                        "(markdown or plain text, embedded image refs copied), "
                        ".png/.jpg/.jpeg/.webp (single-image passthrough)."
                    ),
                },
            },
            "required": ["file_paths"],
        },
    },
    {
        "name": "retrieve_paper_context",
        "description": (
            "Retrieve source-backed paper snippets from paper_memory. Use this "
            "after PDF ingest to ground captions, figure/table readouts, result "
            "bands, native tables, limitations, and short takeaways for "
            "paper-poster panels. Do not use retrieval to expand prose as the "
            "primary fill. "
            "Default hybrid mode first uses the LLM-curated paper-memory dossier "
            "when available, then falls back to deterministic lexical/BM25-style "
            "lookup over canonical chunks. It returns quotes, pages, categories, "
            "evidence_kind, safe_to_quote, source ids, and poster-copy suggestions. "
            "Use needs:['expanded_text'] or expand_evidence_refs:true when the "
            "same dossier query should include raw chunk text for its evidence refs. "
            "Use returned quotes/pages as evidence in the next "
            "propose_paper_poster_html or propose_design_spec call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language query for the paper evidence to retrieve.",
                },
                "panel_role": {
                    "type": "string",
                    "description": "Optional target panel role such as method_pipeline, results_table, ablation_analysis, limitations_future, or takeaway.",
                },
                "source_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional source ids to prioritize/filter, such as ingest_fig_03, ingest_table_02, section:3, or page:5.",
                },
                "categories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional chunk categories to filter, e.g. method_unit, result_unit, numeric_claim, table_row, figure_caption, limitation_unit, takeaway_unit.",
                },
                "evidence_kind": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional evidence kinds to filter: verbatim, extracted, derived_summary, or normalized_table.",
                },
                "safe_to_quote": {
                    "type": "boolean",
                    "description": "Optional filter. true returns only directly citable verbatim/extracted chunks; false returns planning summaries/table normalizations.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["hybrid", "curated", "raw"],
                    "description": "Default hybrid. curated requires dossier results; raw bypasses the LLM-curated dossier and uses canonical lexical retrieval.",
                },
                "needs": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["copy", "quote", "visual", "table", "limitation", "takeaway", "expanded_text"]},
                    "description": "Optional intent hints for curated retrieval. Use expanded_text when the same dossier query should include raw source chunk text for matched evidence refs.",
                },
                "expand_evidence_refs": {
                    "type": "boolean",
                    "description": "When true, dossier-backed results include chunk_text on returned evidence refs. Equivalent to needs:['expanded_text'] for curated results.",
                },
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional number of snippets to return. Omit to return all matching snippets.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "discover_paper_resources",
        "description": (
            "Discover external resources for an attached paper project page. "
            "Call after ingest_document and before propose_design_spec when "
            "building a paper website/project page. The tool extracts URLs "
            "from the PDF text, then conservatively searches public APIs for "
            "arXiv/PDF, GitHub/code, Hugging Face models/datasets/spaces, "
            "demos, blogs, Twitter/X, model weights, and hardware/interface "
            "docs. It returns exact resource_chips with hrefs/icons for the "
            "page. Do not invent unavailable links; use only returned links "
            "or links from the user/source."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Optional paper title override. Defaults to the ingested manifest title.",
                },
                "authors": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional author list override.",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 24,
                    "description": "Maximum resources to return. Default 12.",
                },
                "search_web": {
                    "type": "boolean",
                    "description": (
                        "Default true. Set false only for offline tests; "
                        "source-extracted URLs are still returned."
                    ),
                },
            },
        },
    },
    {
        "name": "propose_paper_poster_html",
        "description": (
            "HTML-first path for academic paper posters. After ingest_document "
            "returns poster_content_brief/poster_plan_contract, submit the "
            "poster body as constrained HTML/CSS instead of hand-writing a "
            "large DesignSpec JSON. The runtime infers editable blocks, source "
            "bindings, and measured bboxes, compiles an authored_html "
            "DesignSpec, then runs the same paper-poster validation as "
            "propose_design_spec. Use this as the preferred dogfood paper "
            "poster authoring tool. The compiler is fail-closed for ignored "
            "slot/lane contracts; out-of-canvas blocks, source-visual, "
            "native-unit, and density gaps receive one planner retry, then "
            "render as P0 repair diagnostics so dogfood still has a visual "
            "candidate. Text-collision warnings are carried forward to "
            "authored preflight/composite repair. In dogfood paper "
            "poster mode, if poster_plan_contract.reference_profile is "
            "`conference_editorial_flow` or editorial_flow_contract is present, "
            "author one compact identity header plus exactly three "
            "`.poster-column` columns containing several normal-flow "
            "`.poster-section` blocks. Do not submit the old `.poster-grid`, "
            "six `.flow-panel` cards, child lanes, or `panel_content_plan` in "
            "that editorial mode. When a non-empty "
            "poster_plan_contract.layout_slot_contract is present for legacy "
            "fixed-panel mode, it is a "
            "semantic panel roster and capacity guide, not a rectangle-copy "
            "instruction. Author the fixed board as CSS grid/flex or normal "
            "document flow with visible panel roots carrying matching "
            "data-slot-id/data-panel-role. Do not copy slot bboxes into "
            "position:absolute/left/top CSS. For panels with source visuals, "
            "make each source visual/table plus its explanation a separate "
            "direct-child `.figure-flow-unit` or `.source-flow-unit`. Inside "
            "that unit, the bound figure/table and local reading-note/readout "
            "text should be direct siblings, with float/shape-outside so the "
            "text wraps around that one source visual like a document layout. "
            "Local readouts must not be visible paper captions and must not "
            "start with `Fig. N`, `Figure N`, or `Table N`; keep those labels "
            "in metadata instead. "
            "Conference posters should be visual/table-first: use ingested "
            "figures, tables, benchmark crops, and native result structures as "
            "the panel subjects, with prose only as concise explanation. "
            "For `ingest_table_*`, the source evidence is the original PDF table "
            "crop; native tables may only add smaller distilled summaries/readouts. "
            "Panel fill should come from scaling/adding source visuals, enlarging "
            "native tables, compact comparison tables, figure labels, and concise visual interpretation; "
            "do not treat extra prose as the primary fill mechanism. Do not make a large "
            "Core contributions section as pure prose or mini-card text; merge it into "
            "Motivation or back it with a source/native evidence unit and local readout. Native units, when useful, "
            "should be real native table rows, compact comparison tables, figure labels, method notes, "
            "source-grounded bullets, or compact pipeline-stage DOM, not bare prose in a native lane; do not use `.model-card` or "
            "`.flow-box` as default filler. child_lanes[] are content "
            "and capacity hints only; never turn them into slot-local absolute "
            "descendants. When poster_plan_contract.authored_html_skeleton is "
            "present, use its html/css as the starter DOM/CSS and "
            "replace the comments with authored content before calling this tool. "
            "The title_meta slot is paper identity only: exactly three visible "
            "text rows for title, authors, and school/institution/company "
            "names. Do not add a fourth header/meta/subtitle row, side identity rail, "
            "venue text, citation/contact text, project/code/resource links, logos, icons, QR codes, badges, thesis text, right-side summaries, taglines, or "
            "generic source-backed-poster descriptors in the header. Never put "
            "pipeline/process labels such as 'Paper poster', 'authored HTML', "
            "or 'no generated evidence imagery' in visible poster text. "
            "When a legacy fixed-panel designer_authoring_blueprint is present, "
            "copy its panel target_words, required_content_modes, content roles, "
            "and html_recipe into your panel_content_plan and then into the DOM; the "
            "first planner candidate should already be visual-rich enough to finalize "
            "without assuming a later post-pass will add missing information. "
            "Only submit `panel_content_plan` when the contract has a non-empty "
            "layout_slot_contract.panel_content_plan_contract: one entry per "
            "required slot explaining what that box will contain, including "
            "claim, target_words, text_units, native_units, source visual plan, "
            "local_explanation_plan, source_refs/evidence_refs, and takeaway. Dogfood rejects authored HTML that skips "
            "this per-panel content plan or under-budgets panel information. "
            "Do not reuse the same claim wording across panels; each panel must "
            "carry distinct source-grounded facts, numbers, caveats, or mechanisms. "
            "Do not solve density by placing every claim in an identical bordered "
            "rectangle. Use natural academic-poster composition: filled panel "
            "interiors, continuous edited prose, inline labels, annotated figures, "
            "compact native tables, source-grounded bullets, concise visual interpretation, and thin separators. Repeated "
            "small card/tile grids are a style defect unless they are real table "
            "rows or a very small number of high-value callouts. "
            "For `conference_editorial_flow`, author a real conference poster "
            "editorial flow: section bars, large source figures/tables, native "
            "equations/tables, compact comparison table rows, and concise readouts arranged in "
            "three vertical columns. For legacy `research_synthesis_dense`, author the fixed CVPR-style 84x42 in "
            "landscape board: a compact full-width identity header above three "
            "human poster columns, with exactly six substantive main panels "
            "(two stacked panels per column). Title/header/footer strips "
            "do not count, and 2x1 compression, 3x3, 4x2, 4x3, or canvas "
            "auto-expansion are density/layout failures. Every large main "
            "panel must combine at least two real content modes from source-backed "
            "text, readable source visual/figure, native table/result structure, "
            "and local explanation/takeaway prose. A single source image or a single "
            "prose block is not enough. Before submitting HTML, run a mental "
            "text-fit pass: every flow panel must have enough line-height, "
            "padding, and source figure/table size for its visible content. "
            "Use edited continuous copy and native rows, not absolute-positioned "
            "overlaps. "
            "Do not place poster copy in bottom-edge or one-pixel-height boxes; "
            "footer/citation text needs a real reserved strip. "
            "poster_plan_contract.visual_density_contract is binding: keep Times "
            "New Roman visibly dark, use the restrained palette only, and fill "
            "pale lanes with native rows/cards/tables/readouts/callouts instead of "
            "hairline text or empty cream boxes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "html": {
                    "type": "string",
                    "description": (
                        "Body-internal poster DOM or a <main class=\"paper-poster\"> "
                        "root. Use semantic panels with data-panel-role, native "
                        "text/table markup, and source figures/tables/images with data-source-id "
                        "or data-layer-id. Place poster_plan_contract.selected_visuals "
                        "up to the required count before lower-priority prose. Do "
                        "not label text-bearing cards, comparison tables, tags, citations, "
                        "or provenance blocks as data-block-kind='shape'; use shape "
                        "only for decorative rules/backgrounds. "
                        "Do not place tall full-page/body-text PDF crops as source "
                        "figures; use sub-panels, native tables, or tighter chart "
                        "crops. This exception does not apply to bound "
                        "`ingest_table_*` source table crops, which must remain "
                        "original PDF crops when used as source evidence. Large "
                        "panels must have filled interiors, not blank "
                        "lower halves or side columns. If poster_plan_contract has "
                        "layout_slot_contract.slots, every required slot must appear "
                        "as a visible panel root with the same slot_id and panel role. "
                        "Use CSS grid/flex on the poster body to arrange those roots; "
                        "do not author explicit slot-local left/top/width/height lanes. "
                        "For source figures/tables/images, create a local <figure> wrapper "
                        "with data-source-id/data-layer-id and let surrounding text "
                        "wrap with float/shape-outside when the figure is not ultra-wide. "
                        "An `ingest_table_*` source must be represented as the original "
                        "PDF table crop, not a native-only subset. "
                        "Preserve the source aspect ratio with contain sizing; do not "
                        "stretch or cover-crop. "
                        "Visible text must be final poster copy, not planning notes: "
                        "do not include phrases such as 'caption must', '4 cards', "
                        "'model-card with fields', lane names, source-id instructions, "
                        "or repeated filler definitions. "
                        "If poster_plan_contract.authored_html_skeleton.html is "
                        "available, start from that flow skeleton when useful and fill "
                        "the panel bodies; do not convert it into absolute lanes. Respect "
                        "poster_plan_contract.designer_authoring_blueprint: every "
                        "non-meta panel must realize its content roles in visible DOM, "
                        "including native table/result/pipeline rows or bound source visuals "
                        "where requested, before calling this tool. Do not leave "
                        "skeleton lane comments, empty divs, or natural-height "
                        "tables as placeholders for later repair. Respect "
                        "visual_density_contract minimum visible font/ink/rule "
                        "requirements; a low-contrast or mostly blank panel is "
                        "not acceptable even when the DOM has words. "
                        "Do not give inline emphasis "
                        "spans data-block-id; keep them as "
                        "plain inline markup. Do not include scripts, remote assets, "
                        "or bitmap text."
                    ),
                },
                "panel_content_plan": {
                    "oneOf": [
                        {"type": "object"},
                        {"type": "array", "items": {"type": "object"}},
                    ],
                    "description": (
                        "Required in dogfood when poster_plan_contract has "
                        "layout_slot_contract.panel_content_plan_contract. "
                        "Do not provide this field for conference_editorial_flow. "
                        "Use {panels:[...]} with one entry per required slot "
                        "except meta/footer slots. Each panel entry must include "
                        "slot_id, panel_role, claim, target_words, text_units "
                        "(source-backed bullets/fragments), native_units "
                        "(tables/compact comparison rows/pipeline boxes/formulas/method notes/source-grounded bullets), "
                        "visual_plan for any required source ids, local_explanation_plan, "
                        "source_refs/evidence_refs for factual claims and numbers, "
                        "and takeaway. Treat target_words as a concise readout/explanation "
                        "budget, not a request for long body prose. The plan should "
                        "fill every panel with readable figures/tables/native result "
                        "structures plus enough short text that the reader can "
                        "understand what each visual proves. "
                        "Use poster_plan_contract.designer_authoring_blueprint.panels "
                        "as the minimum panel roster: preserve its target_words, "
                        "required_content_modes, content roles, and visual_ids, "
                        "then replace generic instructions with paper-specific "
                        "source-backed claims. "
                        "For each contract-defined large main panel, list at least two content modes "
                        "among source-backed text, visual/figure, native table/result "
                        "structure, and local explanation/takeaway prose; title/header/footer "
                        "strips are excluded from this count. Include a compact "
                        "lane budget for each panel so body copy, captions, and "
                        "tables fit without overlap, hidden overflow, or bottom-edge "
                        "one-pixel text boxes. "
                        "Use distinct text units per slot; do not copy the same "
                        "method definition into benchmark, analysis, and synthesis panels."
                    ),
                },
                "css": {
                    "type": "string",
                    "description": (
                        "Scoped static CSS for the poster body. No @import, "
                        "remote URLs, scripts, or style tags. For paper posters, "
                        "include explicit fit rules: box-sizing:border-box, declared "
                        "panel/lane dimensions, measured line-height, wrapping for "
                        "headings/body/captions/takeaways, and no hidden overflow used "
                        "to mask clipping. Source visuals should use aspect-ratio max "
                        "boxes with object-fit:contain; ordinary figures must not be "
                        "vertically centered in otherwise empty lanes."
                    ),
                },
                "browser_flow": {
                    "type": "boolean",
                    "description": (
                        "Set true for academic paper posters authored as browser-native "
                        "normal flow. When true, use CSS grid/flex only for the outer "
                        "poster/panel grid. Inside every source figure/table panel, the "
                        "panel root should contain one direct-child `.figure-flow-unit` "
                        "or `.source-flow-unit` per source asset. Inside each unit, put "
                        "the bound figure/table and its local explanatory text as direct "
                        "siblings, with float/shape-outside for wrap; the readout should fill "
                        "the side wrap space without crossing into the image/table. Do not use media-grid, "
                        "side-stack, two-up, or separate image/text wrappers inside a source "
                        "flow unit. For "
                        "ingest_table_* source crops, treat the asset as the paper's "
                        "original PDF table crop and keep the exact source id on a "
                        "floated figure/table wrapper, preferably with "
                        "data-block-kind=\"table\"; native tables may summarize but "
                        "must not replace, fully duplicate, or drop the bound source table. Multi-figure "
                        "panels must split sources into multiple flow units; do not group "
                        "source assets in support strips, figure strips, or one shared text flow."
                    ),
                },
                "designer_owned_css": {
                    "type": "boolean",
                    "description": (
                        "Set true when the designer's CSS owns panel interior layout. "
                        "Do not rely on compiler-generated child bboxes or a later post-pass."
                    ),
                },
                "archetype": {
                    "type": "string",
                    "description": "Reference-like layout archetype, e.g. dense_evidence_wall or horizontal_dense_paper.",
                },
                "canvas": {
                    "type": "object",
                    "description": "Optional canvas override; normally omit and use canvas_plan/poster_plan_contract.",
                },
                "title": {"type": "string"},
                "brief": {"type": "string"},
                "palette": {"type": "array", "items": {"type": "string"}},
                "typography": {"type": "object"},
                "mood": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["html", "css"],
        },
    },
    {
        "name": "propose_design_spec",
        "description": (
            "Submit the initial DesignSpec for this brief. Call this AFTER "
            "switch_artifact_type (or as the first tool if you're defaulting "
            "to poster). The runner validates and stores the spec; subsequent "
            "tool calls operate on this spec. Re-call to revise. "
            "If `design_spec.artifact_type` is omitted, it falls back to the "
            "value set by switch_artifact_type (default: 'poster'). "
            "Returns a tool result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "design_spec": {
                    "type": "object",
                    "description": (
                        "Full DesignSpec JSON. Required keys: brief, canvas "
                        "({w_px, h_px, dpi, aspect_ratio, color_mode}), "
                        "visual_profile (one of the five quality profiles), palette "
                        "(list of hex strings or OKLch tokens), typography ({title_font, "
                        "subtitle_font, ...}), mood (list[str]), composition_notes "
                        "(str), html_artifact (canonical frames/blocks scene graph), "
                        "optional deck_plan_override_reason for intentional deck "
                        "count deviations, and optional legacy layer_graph/deck_html fallbacks. "
                        "For dogfood academic paper posters, do not call this tool with "
                        "empty args; submit an authored_html html_artifact frame with "
                        "source-backed panels, selected visual IDs, dense editable text, "
                        "and visible DOM/CSS layout. Do not rely on legacy layer_graph-only "
                        "fallbacks for paper posters. Prefer omitting legacy layer_graph "
                        "fallbacks when authored_html carries the poster; if you include "
                        "text layers, every text/chip/card/model-card layer must contain "
                        "real non-empty text. Every panel/group must have filled interiors, "
                        "every text/caption block must wrap inside a real bbox with enough "
                        "height, and every source-image bbox is a max aspect-ratio box "
                        "rather than a centered whitespace placeholder."
                    ),
                },
            },
            "required": ["design_spec"],
        },
    },
    {
        "name": "generate_background",
        "description": (
            "Generate a TEXT-FREE full-canvas poster background raster via "
            "the configured image backend. Use this for non-paper / creative "
            "posters only. Academic paper posters should normally skip this "
            "tool and use a native white or cream editorial canvas with "
            "ingested paper figures/tables as the visual content. "
            "The prompt MUST describe a scene with NO text, characters, lettering, "
            "symbols, or logos — a separate text rendering pass overlays editable "
            "type. Returns a tool result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "layer_id": {"type": "string"},
                "prompt": {
                    "type": "string",
                    "description": (
                        "Scene-only description. The pipeline will append "
                        "'No text, no characters, no lettering, no symbols, no "
                        "logos, no watermarks.' if you don't include it yourself."
                    ),
                },
                "aspect_ratio": {
                    "type": "string",
                    "enum": ["1:1", "3:4", "4:3", "16:9", "9:16",
                             "2:3", "3:2", "4:5", "5:4", "21:9"],
                },
                "image_size": {"type": "string", "enum": ["1K", "2K"]},
                "safe_zones": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "integer"},
                            "y": {"type": "integer"},
                            "w": {"type": "integer"},
                            "h": {"type": "integer"},
                            "purpose": {"type": "string"},
                        },
                        "required": ["x", "y", "w", "h", "purpose"],
                    },
                    "description": (
                        "Regions reserved for later text overlay. Bias the prompt "
                        "to keep these areas low-detail (e.g. 'centered subject "
                        "leaving top 30% as calm sky')."
                    ),
                },
                "allow_paper_background": {
                    "type": "boolean",
                    "description": (
                        "Default false. Set true only when the user explicitly "
                        "asked for generated background art on an academic "
                        "paper poster."
                    ),
                },
            },
            "required": ["layer_id", "prompt", "aspect_ratio", "image_size"],
        },
    },
    {
        "name": "generate_image",
        "description": (
            "Generate an inline image for a landing-page section or a deck "
            "slide via the configured image backend. Use this for landing hero "
            "visuals, feature-card icons/art, deck cover backgrounds, and deck "
            "content imagery. "
            "Images are TEXT-FREE — the renderer overlays real HTML text. "
            "Output is stored as a `kind: \"image\"` layer with a PNG file; "
            "you then reference it in a section or slide `children` list so "
            "the renderer emits it in the HTML frame / PPTX export. "
            "NOT for poster backgrounds. Paper posters use ingested figures / "
            "tables and a neutral canvas; non-paper poster backgrounds use "
            "generate_background with safe_zones. "
            "Prompting tip: use the per-style imagery-prompt prefix from the "
            "design-system guide (e.g. 'soft 3D clay render, pastel palette' "
            "for claymorphism) so all images on the same landing feel "
            "stylistically coherent. "
            "Returns a tool result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "layer_id": {"type": "string"},
                "name": {
                    "type": "string",
                    "description": "Semantic name: 'hero_image' | 'feature_1_icon' | 'cta_banner' etc.",
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "Scene-only description (no text, no logos). Include "
                        "the design-system style prefix. Example: 'soft 3D clay "
                        "render of a warm milk tea in a glass cup, pastel "
                        "off-white background, gentle top-light, rounded forms, "
                        "peaceful handcrafted feel'."
                    ),
                },
                "aspect_ratio": {
                    "type": "string",
                    "enum": ["1:1", "3:4", "4:3", "16:9", "9:16",
                             "2:3", "3:2", "4:5", "5:4", "21:9"],
                    "description": (
                        "Typical picks: '16:9' or '3:2' for hero, '1:1' for "
                        "feature icons, '4:3' for mid-section banners."
                    ),
                },
                "image_size": {
                    "type": "string", "enum": ["1K", "2K"],
                    "description": "'1K' for feature icons (cheaper); '2K' for hero (higher quality).",
                },
                "z_index": {"type": "integer"},
            },
            "required": ["layer_id", "name", "prompt", "aspect_ratio", "image_size"],
        },
    },
    {
        "name": "generate_visual_reference",
        "description": (
            "Generate high-fidelity visual reference images from the latest "
            "composite preview using image-conditioned generation, then "
            "interpret those references into a compact style_anchor and "
            "editable-spec revision guidance. Call AFTER an initial composite "
            "and BEFORE critique/finalize for substantial new deck, poster, "
            "or landing artifacts. The reference image is advisory only: keep "
            "final text native/editable and preserve source-grounded assets. "
            "If the configured image backend does not support reference images, "
            "the tool records a typed attempt error and the planner should "
            "continue with the normal editable pipeline."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "max_items": {
                    "type": "integer",
                    "description": (
                        "Deck only: maximum slide references to generate. "
                        "Default 6. Poster/landing always generate one full "
                        "artifact reference."
                    ),
                },
                "image_size": {
                    "type": "string",
                    "enum": ["1K", "2K"],
                    "description": "Reference generation quality. Default 2K.",
                },
            },
        },
    },
    {
        "name": "render_text_layer",
        "description": (
            "Rasterize a text run into a transparent RGBA PNG sized to the canvas. "
            "Pillow only. Supports stroke and shadow effects. "
            "Returns a tool result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "layer_id": {"type": "string"},
                "name": {
                    "type": "string",
                    "description": "Semantic name: 'title' | 'subtitle' | 'stamp' | 'tagline' | …",
                },
                "text": {"type": "string"},
                "font_family": {
                    "type": "string",
                    "description": (
                        "One of: 'NotoSansSC-Bold' (default for Latin/subtitle), "
                        "'NotoSerifSC-Bold' (default for Chinese title). "
                        "Unknown families fall back to NotoSansSC-Bold with a warning."
                    ),
                },
                "font_size_px": {"type": "integer"},
                "fill": {"type": "string", "description": "Hex color, e.g. '#1a0f0a'"},
                "bbox": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "w": {"type": "integer"},
                        "h": {"type": "integer"},
                    },
                    "required": ["x", "y", "w", "h"],
                    "description": "Top-left origin, pixel coords on full canvas.",
                },
                "align": {"type": "string", "enum": ["left", "center", "right"]},
                "z_index": {"type": "integer"},
                "effects": {
                    "type": "object",
                    "properties": {
                        "stroke": {
                            "type": "object",
                            "properties": {
                                "color": {"type": "string"},
                                "width": {"type": "integer"},
                            },
                        },
                        "shadow": {
                            "type": "object",
                            "properties": {
                                "color": {"type": "string"},
                                "dx": {"type": "integer"},
                                "dy": {"type": "integer"},
                                "blur": {"type": "integer"},
                            },
                        },
                    },
                },
            },
            "required": ["layer_id", "name", "text", "font_family",
                         "font_size_px", "fill", "bbox"],
        },
    },
    {
        "name": "edit_layer",
        "description": (
            "Apply a targeted subset-diff to an existing TEXT layer — tweak its "
            "text/font/color/size/bbox/effects without re-declaring the whole "
            "LayerNode or re-rendering the rest of the artifact. Handler reads "
            "the current layer from ctx.state.rendered_layers[layer_id], merges "
            "`diff` (nested merge for bbox + effects, replace otherwise), and "
            "overwrites the layer's PNG in place. No implicit composite — call "
            "`composite` after batching all your edits. "
            "For HTML-first artifacts, this also accepts editable "
            "html_artifact block ids and updates the block via design ops; "
            "the next composite renders the updated HTML block. "
            "Text layers only: for non-paper poster backgrounds call "
            "generate_background with the same layer_id; for brand assets "
            "call fetch_brand_asset. "
            "Use this for 'make the title bigger', 'try red', 'move the stamp "
            "down 40px', 'bolder shadow' — anything that tweaks one layer. "
            "Returns a tool result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "layer_id": {
                    "type": "string",
                    "description": (
                        "ID of the existing text layer to edit. Must match a "
                        "layer_id present in ctx.state.rendered_layers or an "
                        "editable html_artifact block_id for HTML-first artifacts."
                    ),
                },
                "diff": {
                    "type": "object",
                    "description": (
                        "Subset of editable fields. Only provide what you want "
                        "to change; other fields keep their current value."
                    ),
                    "properties": {
                        "text": {"type": "string"},
                        "font_family": {
                            "type": "string",
                            "description": (
                                "'NotoSansSC-Bold' or 'NotoSerifSC-Bold' "
                                "(unknown families fall back to NotoSansSC-Bold)."
                            ),
                        },
                        "font_size_px": {"type": "integer"},
                        "fill": {"type": "string", "description": "Hex color"},
                        "bbox": {
                            "type": "object",
                            "description": (
                                "Partial bbox update. Fields you omit keep "
                                "their existing value."
                            ),
                            "properties": {
                                "x": {"type": "integer"},
                                "y": {"type": "integer"},
                                "w": {"type": "integer"},
                                "h": {"type": "integer"},
                            },
                        },
                        "align": {
                            "type": "string",
                            "enum": ["left", "center", "right"],
                        },
                        "z_index": {"type": "integer"},
                        "effects": {
                            "type": "object",
                            "description": (
                                "Partial effects update. Nested merge: shadow "
                                "+ stroke sub-objects are replaced whole."
                            ),
                            "properties": {
                                "stroke": {"type": "object"},
                                "shadow": {"type": "object"},
                            },
                        },
                    },
                    "additionalProperties": False,
                },
            },
            "required": ["layer_id", "diff"],
        },
    },
    {
        "name": "apply_design_ops",
        "description": (
            "Apply a batch of targeted design operations to the current "
            "DesignSpec. Use this to repair concrete design_feedback findings "
            "without rewriting the whole spec. Every op MUST include "
            "`finding_id` (or 'manual' outside feedback repair). The batch is "
            "atomic: if any op fails validation, the DesignSpec and rendered "
            "layers are left unchanged. For poster text layers, text/bbox "
            "changes re-render the affected PNGs; deck and landing changes "
            "update the spec and take effect on the next composite. No implicit "
            "composite or finalize — call composite after the ops. For "
            "HTML-first posters/decks/landing pages, prefer html_* ops with "
            "`block_id` for text, caption, image, table, shape, and group "
            "blocks. If a target id is visible in the DesignSpec's "
            "html_artifact.frames[].blocks but not in legacy layer_graph, use "
            "`html_set_block_bbox`, `html_move_block`, `html_resize_block`, "
            "`html_replace_text`, or `html_set_block_style`, not legacy "
            "set_bbox/move_layer/resize_layer/replace_text. Legacy op names "
            "will be aliased only when the id exists as an HTML block and no "
            "legacy layer has that id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ops": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {
                                "type": "string",
                                "enum": [
                                    "add_layer", "delete_layer", "set_bbox",
                                    "move_layer", "resize_layer",
                                    "replace_text", "set_slide_role",
                                    "anchor_callout",
                                    "html_add_block", "html_delete_block",
                                    "html_set_block_bbox", "html_move_block",
                                    "html_resize_block", "html_realize_block_bbox",
                                    "html_realize_all_text_bboxes",
                                    "html_auto_repair_feedback",
                                    "html_bind_all_images_to_blocks",
                                    "html_replace_text", "html_set_authored_css",
                                    "html_set_block_style", "html_set_block_role",
                                    "html_set_block_source",
                                    "html_assign_block_to_slot", "html_resize_slot",
                                    "html_split_slot", "html_wrap_block_in_group",
                                ],
                            },
                            "finding_id": {
                                "type": "string",
                                "description": "DesignFeedbackFinding.id that triggered this op, or 'manual'.",
                            },
                            "layer_id": {"type": "string"},
                            "parent_layer_id": {
                                "type": "string",
                                "description": "For add_layer, append to this node's children. Omit to add top-level.",
                            },
                            "layer": {
                                "type": "object",
                                "description": "Full LayerNode JSON for add_layer.",
                            },
                            "frame_id": {
                                "type": "string",
                                "description": "Target HtmlFrame for html_add_block when no parent_block_id is provided.",
                            },
                            "block_id": {
                                "type": "string",
                                "description": "Target HtmlBlock id for html_* ops.",
                            },
                            "block_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Optional block ids for html_realize_all_text_bboxes.",
                            },
                            "parent_block_id": {
                                "type": "string",
                                "description": "For html_add_block, append to this group block's children.",
                            },
                            "slot_id": {
                                "type": "string",
                                "description": "Storyboard slot id for slot-aware html repair ops.",
                            },
                            "group_block_id": {
                                "type": "string",
                                "description": "Group/panel block id for slot-aware html repair ops.",
                            },
                            "block": {
                                "type": "object",
                                "description": "Full HtmlBlock JSON for html_add_block.",
                            },
                            "new_slot": {
                                "type": "object",
                                "description": "Full FrameSlot JSON for html_split_slot.",
                            },
                            "move_block_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Block ids to move into a newly split slot.",
                            },
                            "scale_children": {
                                "type": "boolean",
                                "description": "For html_resize_slot, scale child bboxes with the panel. Defaults true.",
                            },
                            "content_policy": {"type": ["string", "null"]},
                            "max_text_words": {"type": ["integer", "null"]},
                            "min_visual_area_ratio": {"type": ["number", "null"]},
                            "parent_slot_id": {"type": ["string", "null"]},
                            "panel_job": {"type": ["string", "null"]},
                            "text_budget": {"type": ["string", "null"]},
                            "visual_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "space_fill_policy": {"type": ["string", "null"]},
                            "required": {"type": "boolean"},
                            "bbox": {
                                "type": "object",
                                "description": "Full or partial bbox for set_bbox; fields omitted keep current bbox when merge=true.",
                                "properties": {
                                    "x": {"type": "integer"},
                                    "y": {"type": "integer"},
                                    "w": {"type": "integer"},
                                    "h": {"type": "integer"},
                                },
                            },
                            "merge": {"type": "boolean"},
                            "dx": {"type": "integer"},
                            "dy": {"type": "integer"},
                            "w": {"type": "integer"},
                            "h": {"type": "integer"},
                            "text": {"type": "string"},
                            "style": {
                                "type": ["object", "string"],
                                "description": "Partial HtmlBlock.style update for html_set_block_style; CSS declaration strings are accepted.",
                            },
                            "css": {
                                "type": "string",
                                "description": "Full authored_css replacement for html_set_authored_css.",
                            },
                            "authored_css": {
                                "type": "string",
                                "description": "Alias for css in html_set_authored_css.",
                            },
                            "role": {
                                "type": ["string", "null"],
                                "description": "Layer slide role or arbitrary HtmlBlock semantic role.",
                            },
                            "source": {"type": ["string", "null"]},
                            "source_id": {"type": ["string", "null"]},
                            "source_text": {"type": ["string", "null"]},
                            "evidence_quote": {"type": ["string", "null"]},
                            "evidence_source": {"type": ["string", "null"]},
                            "provenance": {"type": ["object", "null"]},
                            "src_path": {"type": ["string", "null"]},
                            "anchor_layer_id": {"type": "string"},
                            "callout_region": {
                                "type": ["object", "null"],
                                "properties": {
                                    "x": {"type": "integer"},
                                    "y": {"type": "integer"},
                                    "w": {"type": "integer"},
                                    "h": {"type": "integer"},
                                },
                            },
                        },
                        "required": ["op", "finding_id"],
                    },
                },
                "notes": {"type": "string"},
            },
            "required": ["ops"],
        },
    },
    {
        "name": "fetch_brand_asset",
        "description": (
            "Resolve a user-uploaded/fetched academic identity or brand asset. "
            "First checks ctx.state.academic_identity_assets from ingest_document "
            "and returns the matched rendered_layer_id/local path. Optionally "
            "downloads a raster PNG/JPEG/WebP from an explicit https source_url "
            "on an allowlisted academic/official domain; this is not open web "
            "search and does not accept free-form logo queries. "
            "For missing logos, fall back to editable text badges."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "asset_id": {
                    "type": "string",
                    "description": "Identity asset id, rendered layer id, or entity label to resolve.",
                },
                "entity_name": {
                    "type": "string",
                    "description": "Entity name when fetching from an explicit URL.",
                },
                "role": {
                    "type": "string",
                    "enum": ["venue", "institution", "lab", "company", "project"],
                    "description": "Academic identity role. Default: project.",
                },
                "source_url": {
                    "type": "string",
                    "description": "Optional explicit https raster logo URL on an allowlisted official/academic domain.",
                },
            },
            "required": ["asset_id"],
        },
    },
    {
        "name": "composite",
        "description": (
            "Combine all currently rendered layers into PSD (psd-tools, pixel "
            "layers with semantic names + 'text' group), SVG (real <text> "
            "elements + base64-embedded background + subsetted-WOFF2 fonts in "
            "@font-face), and a flattened preview PNG. Reads layers from runner "
            "state — no need to pass layer_graph again. "
            "Returns a tool result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "critique",
        "description": (
            "Spawn the v2.7.3 vision critic sub-agent. The sub-agent runs its "
            "own loop with vision capability for ALL artifact types (deck "
            "slides / landing preview / poster preview), pulls relevant paper "
            "passages on demand, reads latest design_feedback, and emits a "
            "structured CritiqueReport with verdict (pass/revise/fail), score, "
            "and a list of typed, repair-oriented issues. "
            "The full report JSON lands in tool_result.payload. Call AT MOST "
            "max_critique_iters times per run. On verdict='revise', address "
            "the listed issues via propose_design_spec or render_text_layer "
            "and call composite again; on verdict='fail', call finalize. "
            "Do NOT regenerate the background unless an issue with category "
            "'layout' and severity 'blocker' points at the background itself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "preview_path": {
                    "type": "string",
                    "description": (
                        "Optional override; defaults to the last composite "
                        "preview.png. Per-slide PNGs are auto-discovered for "
                        "decks under composites/iter_NN/slides/."
                    ),
                },
            },
        },
    },
    {
        "name": "export_video",
        "description": (
            "Deliver a conference-length HTML-first HyperFrames video from the "
            "current run's composited artifact. Requires 10-14 narrated scene frames "
            "covering 300-600 seconds, with duration chosen for the paper's complexity. "
            "The tool writes English transcript, SRT, VTT, "
            "and deterministic Kokoro voice metadata, authors local-only index.html, "
            "synthesizes local Kokoro narration, runs strict HyperFrames lint/render, "
            "and accepts only a fresh ffprobe-verified "
            "H.264/yuv420p/AAC 1920x1080 30fps MP4. Composer, lint, render, stale-output, "
            "or media-contract failure returns a tool error; placeholders are never success."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "video_id": {
                    "type": "string",
                    "description": (
                        "Short slug for the output directory, e.g. "
                        "'longcat-next-conference'. Defaults to run_id + '-video'."
                    ),
                },
                "style_prompt": {
                    "type": "string",
                    "description": (
                        "Free-text style brief for DESIGN.md (colors, typography, "
                        "motion register). If omitted, derived from design_spec.mood "
                        "and palette. Example: 'Deep ink background, restrained cyan "
                        "and violet signals, precise editorial type, and motion that "
                        "feels like tokens aligning into a single stream.'"
                    ),
                },
                "tone": {
                    "type": "string",
                    "enum": ["academic", "product", "editorial", "technical"],
                    "description": (
                        "Visual and copy register. 'academic' = conference-talk "
                        "restraint (paper-textured canvas, serif headlines). "
                        "'product' = marketing landing feel. "
                        "'editorial' = magazine / long-read. "
                        "'technical' = dark-mode, mono type, data-dense. "
                        "Default: 'academic'."
                    ),
                },
                "duration_s": {
                    "type": "integer",
                    "minimum": 300,
                    "maximum": 600,
                    "default": 360,
                    "description": (
                        "Target video duration in seconds. Choose 300-600 based on "
                        "paper complexity; default 360 only when no target is supplied."
                    ),
                },
                "n_scenes": {
                    "type": "integer",
                    "minimum": 10,
                    "maximum": 14,
                    "default": 12,
                    "description": (
                        "Number of timed, English-narrated scenes. Default 12. "
                        "The HTML-first scene graph remains authoritative."
                    ),
                },
                "voice_preset": {
                    "type": "string",
                    "enum": ["male", "female"],
                    "default": "female",
                    "description": (
                        "Deterministic Kokoro voice preset recorded in delivery metadata."
                    ),
                },
                "include_source_page": {
                    "type": "boolean",
                    "description": (
                        "If true, copy the composited landing page HTML "
                        "to assets/source.html so the closing scene can show a "
                        "miniature paper-preview iframe. Default: false."
                    ),
                },
            },
        },
    },
    {
        "name": "read_skill_resource",
        "description": (
            "Read the complete content of a selected runtime skill resource for the "
            "current workflow stage. Only resources listed in the active skill catalog "
            "are available. Returns a tool result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "skill_id": {
                    "type": "string",
                    "description": "Selected runtime skill id from the active catalog.",
                },
                "resource_id": {
                    "type": "string",
                    "description": "Resource id from the active runtime skill catalog.",
                },
            },
            "required": ["skill_id", "resource_id"],
        },
    },
    {
        "name": "finalize",
        "description": (
            "Signal that the design is done. The runner returns a RunResult "
            "summarizing status, artifact location, critic score, layer count, "
            "and wall time. In POSTER_HARNESS_MODE=dogfood, academic paper "
            "posters must have a critic pass for the latest composite before "
            "finalize. Returns a tool result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "notes": {
                    "type": "string",
                    "description": "Optional final notes recorded in the RunResult.",
                },
            },
        },
    },
]


TOOL_HANDLERS: dict[str, ToolHandler] = {
    "switch_artifact_type": switch_artifact_type,
    "ingest_document": ingest_document,
    "retrieve_paper_context": retrieve_paper_context,
    "discover_paper_resources": discover_paper_resources,
    "propose_paper_poster_html": propose_paper_poster_html,
    "propose_design_spec": propose_design_spec,
    "generate_background": generate_background,
    "generate_image": generate_image,
    "generate_visual_reference": generate_visual_reference,
    "render_text_layer": render_text_layer,
    "read_skill_resource": read_skill_resource,
    "edit_layer": edit_layer,
    "apply_design_ops": apply_design_ops,
    "fetch_brand_asset": fetch_brand_asset,
    "composite": composite,
    "critique": critique,
    "export_video": export_video,
    "finalize": finalize,
}


__all__ = ["TOOL_SCHEMAS", "TOOL_HANDLERS", "ToolContext"]
