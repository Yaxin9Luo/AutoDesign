# Fresh poster review rubric

Perform this review only after deterministic validation passes. The reviewer
must be a fresh host VLM or fresh subagent that did not author the attempt. It
must inspect both the screen preview and PDF-raster preview listed by
`review-context`, read the relevant source
map and evidence, and judge the rendered poster rather than nominal HTML/CSS.
No external judge API is required.

## Blockers

Return a failing verdict when any of these is visible or evidenced:

- the title, author, institution, claim, number, figure, table, or conclusion is
  unsupported, wrong, or assigned to the wrong source;
- reference content leaked into the target: wording, logo, QR code, link,
  figure, table, claim, or recognizable identity treatment used as content;
- a key method or result is absent, the research arc is misleading, or the
  conclusion outruns the evidence;
- important text is illegible at full-poster view, overlaps, clips, or falls
  outside the canvas; a figure loses labels/legend/axes needed to understand it;
- the output is a sparse landing page, a wallpaper of tiny paper screenshots,
  or a flattened poster rather than editable native structure;
- the identity header contains anything beyond title, authors, and
  institutions;
- the poster cannot be presented as the planned one-page physical PDF.

## Dimension scores

Score every dimension from 1 to 5. Use 3 for competent but clearly improvable,
4 for presentation-ready, and 5 only for unusually strong work.

1. `poster_impact`: At thumbnail distance, the research identity, main idea,
   and decisive result are immediately recognizable. Composition feels like a
   conference poster, not a generic dashboard or web page.
2. `information_architecture`: Reading order is obvious; problem → method →
   evidence → takeaway has meaningful grouping, proportional panel space, and
   no dead zones, repeated boilerplate, or stranded bottom strips.
3. `evidence_use`: Original figures/tables and native synthesis are correctly
   selected, readable, source-bound, and integrated into the argument. Claims
   stay within the paper's facts and limitations.
4. `human_effort_saved`: The poster is substantially ready for a real author to
   present, not merely a scaffold requiring wholesale rewriting, rearrangement,
   or asset replacement.
5. `typography_craft`: Type hierarchy, wrapping, line length, density, table
   craft, alignment, rhythm, and contrast remain readable and deliberate at the
   full board scale.
6. `originality_anti_template`: The visual system reflects the target research
   and any permitted style geometry without AI-slop gradients, gratuitous
   cards, random colors, fake branding, repeated decoration, or copied reference
   content.
7. `editability_export`: Text, tables, and SVG labels remain native; local
   assets are sharp and correctly cropped; preview and one-page PDF faithfully
   represent the same poster.

A `pass` requires zero blockers and an average score of at least 3.75. Use
`fail` for actionable quality or correctness defects. Use
`needs_visual_review` only when the reviewer truly cannot inspect the bound
preview; it is not a pass.

## Exact review object

Copy every binding field exactly from `review-context.json`. Do not omit or add
keys.

```json
{
  "format_version": 1,
  "attempt_id": "01",
  "review_context_sha256": "COPY_CONTEXT_SHA256",
  "artifact_hashes": {},
  "preview_hashes": {
    "poster_pdf": "COPY_PDF_PREVIEW_SHA256",
    "poster_screen": "COPY_SCREEN_PREVIEW_SHA256"
  },
  "reviewed_frame_ids": ["poster_pdf", "poster_screen"],
  "source_manifest_sha256": "COPY_SOURCE_MANIFEST_SHA256",
  "source_map_sha256": "COPY_SOURCE_MAP_SHA256",
  "rubric_sha256": "COPY_RUBRIC_SHA256",
  "reviewer_mode": "fresh_host_vlm",
  "dimension_scores": {
    "poster_impact": 4,
    "information_architecture": 4,
    "evidence_use": 4,
    "human_effort_saved": 4,
    "typography_craft": 4,
    "originality_anti_template": 4,
    "editability_export": 4
  },
  "blockers": [],
  "localized_repairs": [],
  "verdict": "pass",
  "complete": true
}
```

`artifact_hashes`, `preview_hashes`, and `reviewed_frame_ids` must be the exact
complete values from context. For a failed review, make each repair local and
verifiable, for example: identify the panel, observed problem, source evidence
to preserve, and desired visible correction. Never respond with a generic
request to “make it more polished.”
