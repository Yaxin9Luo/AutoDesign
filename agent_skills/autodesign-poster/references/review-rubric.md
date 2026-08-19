# Fresh Poster reviews and repair routes

There are two separate semantic reviews. Source review happens before planning
and uses the exact schema in
[agent-first-source.md](agent-first-source.md). Artifact review happens only
after deterministic validation passes. Neither review may be authored by the
Agent pass that made the decisions being reviewed.

## Artifact-review procedure

Give a fresh host VLM or fresh subagent all screen and PDF-raster frames listed
by `review-context`, plus its exact source map, evidence, plan/catalog snapshot,
and this rubric. Judge the rendered Poster, not nominal HTML/CSS. Inspect every
bound frame; no external judge API is required.

Fail when any identity, claim, number, figure, table, or conclusion is
unsupported or wrong; reference content leaks into the target; the central
method/result or research arc is missing or misleading; a selected source
visual is fragmentary, wrong, unreadable, detached from its readout, or replaced
by reconstruction; text/evidence clips, overlaps, escapes, or becomes
illegible; the primary Poster canvas is transparent, tinted, dark, altered by
root/ancestor paint effects, or uses a background image/gradient that clashes
with white-background paper evidence;
the composition is a sparse landing page, screenshot wallpaper, or flattened
image; the header contains more than title/authors/institutions; or
the planned editable one-page physical PDF cannot be presented.

Score every dimension from 1 through 5. Use 3 for competent but improvable, 4
for presentation-ready, and 5 only for unusually strong work:

1. `poster_impact`
2. `information_architecture`
3. `evidence_use`
4. `human_effort_saved`
5. `typography_craft`
6. `originality_anti_template`
7. `editability_export`

A pass requires zero blockers, all bound frames reviewed, and an average score
of at least 3.75.

## Exact artifact-review schema

Copy all binding values exactly from `review-context.json`. A pass uses
`repair_route:null` and no route findings:

```json
{
  "artifact_hashes": {
    "artifact/poster.html": "COPY_HASH",
    "artifact/poster.pdf": "COPY_HASH",
    "artifact/preview.png": "COPY_HASH"
  },
  "attempt_id": "COPY_ACTIVE_ATTEMPT_ID",
  "blockers": [],
  "complete": true,
  "dimension_scores": {
    "editability_export": 4,
    "evidence_use": 4,
    "human_effort_saved": 4,
    "information_architecture": 4,
    "originality_anti_template": 4,
    "poster_impact": 4,
    "typography_craft": 4
  },
  "format_version": 1,
  "localized_repairs": [],
  "preview_hashes": {
    "poster_pdf": "COPY_HASH",
    "poster_screen": "COPY_HASH"
  },
  "repair_route": null,
  "review_context_sha256": "COPY_CONTEXT_SHA256",
  "reviewed_frame_ids": [
    "poster_pdf",
    "poster_screen"
  ],
  "reviewer_mode": "fresh_subagent",
  "route_findings": [],
  "rubric_sha256": "COPY_RUBRIC_SHA256",
  "source_manifest_sha256": "COPY_SOURCE_MANIFEST_SHA256",
  "source_map_sha256": "COPY_SOURCE_MAP_SHA256",
  "verdict": "pass"
}
```

For a failure, `repair_route` is non-null and at least one bound blocker or route
finding is present. Each route finding has exactly `finding_id`, `code`,
`minimum_route`, `block_id`, and `message`. Each localized repair identifies a
specific panel/element, observed defect, source evidence to preserve, and a
visible testable correction. Never request only “more polish.”

## Exact repair-route table

Route order is `layout_repair < content_replan < source_reingest`. The chosen
route must be at least the strongest minimum below.

| Finding code | Minimum route |
| --- | --- |
| `dom_overflow` | `layout_repair` |
| `dom_clipping` | `layout_repair` |
| `dom_overlap` | `layout_repair` |
| `dom_blank_band` | `layout_repair` |
| `typography` | `layout_repair` |
| `visual_balance` | `layout_repair` |
| `narrative_hierarchy` | `content_replan` |
| `claim_selection` | `content_replan` |
| `section_allocation` | `content_replan` |
| `evidence_area_mismatch` | `content_replan` |
| `key_visual_missing` | `source_reingest` |
| `wrong_visual` | `source_reingest` |
| `incomplete_crop` | `source_reingest` |
| `fragmentary_crop` | `source_reingest` |
| `unreadable_source_visual` | `source_reingest` |
| `caption_claim_mismatch` | `source_reingest` |
| `poster-dom-root-overflow` | `layout_repair` |
| `poster-dom-text-clipping` | `layout_repair` |
| `poster-dom-text-overlap` | `layout_repair` |
| `poster-dom-viewport-escape` | `layout_repair` |
| `poster-dom-blank-band` | `layout_repair` |
| `poster-dom-sparse-oversized-panel` | `layout_repair` |
| `poster-dom-image-low-effective-resolution` | `layout_repair` |
| `poster-dom-table-overflow` | `layout_repair` |
| `poster-dom-table-text-small` | `layout_repair` |
| `poster-dom-source-flow-gutter` | `layout_repair` |
| `poster-dom-source-flow-sibling` | `layout_repair` |
| `poster-dom-screen-print-mismatch` | `layout_repair` |
| `poster-dom-canvas-background` | `layout_repair` |
| `poster-dom-template-boxiness` | `layout_repair` |

The reviewer may escalate but never downgrade. Multiple findings take the
strongest minimum route.

## Route actions

- `layout_repair`: keep the catalog and plan; start the next authoring attempt
  and repair HTML/CSS there.
- `content_replan`: keep the catalog; submit the canonical `reopen-curation`
  request, commit a new plan revision, then start the next attempt.
- `source_reingest`: submit the reopen request, return to PDF/pages, create
  append-only replacement crops, pass a fresh source review, commit a new
  catalog and plan, then start the next attempt.

## Exact reopen request

For the latter two routes, copy current values from the persisted review and
`resume` into this exact reopen schema:

```json
{
  "attempt_id": "COPY_ACTIVE_ATTEMPT_ID",
  "expected_curation_revision": 2,
  "expected_plan_revision": 3,
  "finding_ids": [
    "COPY_FINDING_ID"
  ],
  "reason": "Repair the bound semantic finding before the next attempt.",
  "repair_route": "content_replan",
  "run_format_version": 2,
  "semantic_review_sha256": "COPY_SEMANTIC_REVIEW_SHA256"
}
```

Browser startup, Poppler, export, or other environment/runtime failures are not
semantic findings. Retry the current attempt and do not consume a new semantic
attempt or invent a repair route.
