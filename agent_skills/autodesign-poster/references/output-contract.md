# Poster output contract

Follow this contract literally. The harness accepts only exact schemas and
never repairs authoring content for you.

## Plan

Write one JSON object:

```json
{
  "format_version": 1,
  "artifact_type": "poster",
  "preset": "cvpr-landscape",
  "canvas": {"width_px": 3072, "height_px": 1536},
  "print": {"width_mm": 2133.6, "height_mm": 1066.8},
  "narrative": [
    {"role": "problem", "purpose": "Frame the paper's research problem."},
    {"role": "method", "purpose": "Explain the central mechanism."},
    {"role": "evidence", "purpose": "Show the decisive measured evidence."},
    {"role": "takeaway", "purpose": "State a bounded conclusion and limitation."}
  ],
  "visual_allocations": [
    {"visual_id": "vis-001", "role": "method"}
  ],
  "style_reference_ids": [],
  "max_attempts": 4
}
```

Supported named presets are `cvpr-landscape`, `a0-landscape`, `a0-portrait`,
`36x48-landscape`, and `36x48-portrait`. Omit `preset`, `canvas`, and `print` to
use the CVPR default. Use `preset: "custom"` only for an explicit user size;
canvas and physical print aspect ratios must agree. Do not silently change a
requested size. `max_attempts` is 1–8.

Allocate only content-eligible visuals and allowed roles from
`evidence/source_visuals.json`. A style-reference ID cannot also be content.
Style references remain in evidence context and are never staged into the
artifact.

## Authoring HTML

Create one standalone `artifact/poster.html` with:

- `<!doctype html>` and no script, iframe, remote asset, hotlink, data URL,
  event-handler attribute, CSS import, or symlinked dependency;
- exactly one root
  `<main class="paper-poster" data-autodesign-artifact="poster">`;
- root `data-canvas-width`, `data-canvas-height`, `data-print-width-mm`, and
  `data-print-height-mm` values equal to the saved plan;
- an explicit `@page { size: WIDTHmm HEIGHTmm; margin: 0; }` rule and print CSS
  that preserves the physical canvas;
- exactly one `<header data-role="identity-header">`, containing exactly one
  non-empty `data-identity="title"`, `authors`, and `institutions` field. Give
  every identity field evidence IDs. Put no logo, badge, icon, QR code,
  venue/year, link, claim, subtitle, or fourth row in the header;
- body sections marked with `data-section-role` and a meaningful native heading.
  Cover problem/context, method, evidence/results, and takeaway/limitations;
- each visible direct claim inside exactly one element with
  `data-claim-id="..."` and the exact whitespace/comma-separated
  `data-source-ids="..."` set from its source-map entry;
- every section/article grounded with `data-source-ids`; every source image
  identified with `data-source-id` and loaded from its staged local path;
- an exact dependency closure: every non-HTML file in the attempt artifact
  directory is referenced by the HTML, and every referenced file is local,
  present, non-symlinked, and contained in that directory;
- substantial editable HTML text, at least one native HTML table with real
  cells, and SVG text kept as text. Never flatten the poster or a table into a
  screenshot;
- explicit poster-scale CSS: title at least 56 px, author/institution text at
  least 28 px, section headings at least 36 px, and body/list/table text at
  least 24 px;
- a compact, readable conference-poster hierarchy. Default posters use three
  editorial columns with one to three normal-flow sections per column. Do not
  create landing-page cards, giant empty regions, clipped figures, repeated
  `Fig. N`/`Table N` clutter, heavy full-cell table grids, or decorative filler.

Use a restrained academic palette, neutral panel interiors, a white or
near-white identity area with one deliberate accent treatment, and no gradients
or arbitrary multicolor section system. Source figures carry evidence; native
readouts and tables carry synthesis.

## Claim source map

Before `validate`, write an exact object:

```json
{
  "claims": [
    {
      "id": "claim-method-01",
      "text": "The routing stage uses two passes.",
      "source_ids": ["paper-sec-004"]
    }
  ]
}
```

Every claim ID must appear exactly once in visible HTML. The visible element
must contain the mapped text and the same evidence-ID set. Every visible number
must be source-grounded or explicitly derived under the source-grounding
contract. Do not use source IDs as decoration or proof without reading them.

## Deterministic acceptance and delivery

`validate` first checks document safety, fixed canvas, print page, identity,
native editability, exact source bindings, local dependency closure, research
arc, and typography. Only a static pass may launch pinned Chromium. Browser QA
must show a nonblank canvas with no overflow, clipping, missing asset, console
error, or blocked request. The harness then writes and probes
`artifact/poster.pdf`; it must be exactly one page at the planned physical size.

The reviewed artifact set is exact and immutable:

- `artifact/poster.html`
- `artifact/poster.pdf`
- `artifact/preview.png`
- any staged files below `artifact/assets/`

Finalization atomically promotes a passing, independently reviewed attempt and
adds `provenance/source-map.json`. A preview alone, HTML scaffold, fallback PDF,
or file produced by a failed attempt is not success.
