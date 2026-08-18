# Poster output, DOM, QA, and delivery contract

Follow this contract literally. The Agent authors and repairs `poster.html`;
scripts validate and render it but never rewrite its HTML, CSS, layout, or
content. Use the active immutable plan and staged assets from
`authoring-context.json`; the exact plan schema lives in
[agent-first-source.md](agent-first-source.md).

## Authored HTML

Create the requested `attempts/NN/artifact/poster.html` with:

- `<!doctype html>` and no script, iframe, form control, remote asset, hotlink,
  data URL, event-handler attribute, CSS import, duplicate HTML attribute, or
  symlinked dependency. CSS generated content must be empty; every visible word
  belongs in native HTML text.
- Exactly one
  `<main class="paper-poster" data-autodesign-artifact="poster">`. Its
  `data-canvas-width`, `data-canvas-height`, `data-print-width-mm`, and
  `data-print-height-mm` equal the attempt-bound plan.
- One `@page { size: WIDTHmm HEIGHTmm; margin: 0; }` rule and print CSS that
  preserves the physical canvas.
- Exactly one `<header data-role="identity-header">`, containing exactly one
  non-empty `data-identity="title"`, `authors`, and `institutions` field. Bind
  each to evidence IDs. Put no logo, badge, icon, QR code, venue/year, link,
  claim, subtitle, or fourth row in the header.
- Native sections with meaningful headings and `data-section-role`, covering
  problem/context, method, evidence/results, and takeaway/limitations. Bind
  every section/article with `data-source-ids`.
- Every direct visible claim in exactly one `data-claim-id` element with the
  exact `data-source-ids` set from its source-map entry.
- Each source image marked with its `data-source-id` and exact staged local
  path. Put source evidence and its explanatory native readout as adjacent
  direct children of one `.source-flow-unit`; both carry intersecting evidence
  `data-source-ids`. Preserve the readable crop, labels, axes, legends, and
  table structure. A native summary may explain source evidence; it must not replace
  it.
- An exact dependency closure: every non-HTML artifact sidecar is referenced;
  every reference is local, present, contained, non-symlinked, and an approved
  image or font. SVGs are self-contained and non-executable.
- Substantial editable native text, at least one native HTML table with real
  cells, and SVG labels kept as text. Never flatten the Poster or a table into
  a screenshot.

## Typography and composition gates

Use poster-scale type: title at least 56 px, author/institution at least 28 px,
section headings at least 36 px, and body, list, and native table text at least
24 px. The DOM audit independently blocks native table text below 24 px.

Build a dense, readable conference Poster rather than a landing page. Default
landscape work normally uses three editorial columns with one to three
normal-flow sections per column. Avoid giant empty regions, clipped figures,
tiny paper screenshots, repeated figure/table labels, heavy full-cell grids,
decorative filler, gratuitous cards, gradients, and arbitrary section colors.
Use a restrained academic palette and let source evidence carry the visual
weight.

## Claim/source map

Before validation, write an object containing `claims`; each item has exact
keys `id`, `text`, and `source_ids`:

```json
{"claims":[{"id":"claim-method-01","source_ids":["paper-sec-method"],"text":"The routing stage uses two passes."}]}
```

Every claim ID appears exactly once in visible HTML. Visible text equals the
mapped text, and its evidence-ID set matches exactly. Every visible number is
source-grounded or explicitly derived under the source-grounding contract.

## Strictly read-only DOM audit

Run `dom-audit` before final validation. It may read the attempt snapshot and
authored artifact, measure screen/print DOM, and write only:

- `qa/dom-audit.json`;
- `qa/previews/dom-screen.png`; and
- `qa/previews/dom-print.png`.

The report binds the attempt context, plan, catalog, screenshots, and artifact
tree. `artifact_tree_sha256_before` must equal
`artifact_tree_sha256_after`, and `artifact_unchanged` must be true. The audit
may report overflow, clipping, overlap, viewport escape, blank bands, sparse
oversized panels, low effective image resolution, table overflow/small text,
source-flow defects, screen/print mismatch, or template boxiness. It never
moves elements, resizes panels, shrinks fonts, injects CSS, or accepts a repair.
The Agent interprets findings, preserves design intent, and edits only a new or
active authoring attempt.

## Validation and immutable QA

`validate` uses the same DOM engine after static safety and authoring checks.
It requires a nonblank fixed canvas, no overflow/clipping/missing asset,
network-denied browser execution, no console/request error, exact local closure,
and typography/source bindings. It then writes and probes `artifact/poster.pdf`
as exactly one page at the planned physical size, rasterizes that exact PDF to
`qa/previews/poster-print.png`, and sets `artifact/preview.png` to the PDF
raster. The screen and PDF-raster previews are both mandatory fresh-review
frames.

A runtime or failed deterministic check stays on the current attempt: repair
the active authored HTML, let the harness replace only its generated render/QA
outputs, and rerun. Once deterministic QA passes and `review-context` binds the
artifact, never modify that attempt's artifact or QA bytes. A semantic repair
creates the next route-authorized attempt and keeps every previous attempt,
catalog, plan, review, and receipt byte intact.

## Final closure

Finalization promotes only one attempt with passing deterministic QA and a
complete fresh semantic pass. The exact reviewed closure is:

- `poster.html`;
- one-page `poster.pdf`;
- PDF-raster `preview.png`;
- every validated HTML-referenced `assets/**` image/font;
- `provenance/source-map.json`; and
- `delivery-manifest.json`.

A preview, scaffold, fallback PDF, failed attempt, or cherry-picked subset is
not a deliverable.
