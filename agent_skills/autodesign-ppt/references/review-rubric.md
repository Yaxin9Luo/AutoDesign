# PPT review rubric

Review only after deterministic validation passes. Use a fresh host-VLM pass or
a fresh subagent that did not author the deck. Inspect the contact sheet first,
then every individual slide preview at full size. HTML source is supporting
evidence, not a substitute for looking.

## Required dimensions

Score every dimension from 1 to 5:

- `source_fidelity`: claims, numbers, captions, identity, limits, and visual
  interpretations match the cited paper evidence;
- `narrative_coherence`: the talk has a clear problem -> gap -> method -> evidence
  -> limitation -> takeaway argument, with no redundant or missing bridge;
- `visual_hierarchy`: each slide has one obvious assertion and a deliberate
  reading order;
- `typography_legibility`: text, axes, legends, equations, and citations remain
  readable at presentation distance;
- `layout_composition`: no clipping, collisions, accidental emptiness, repeated
  card grids, or mechanically templated compositions;
- `evidence_communication`: figures and tables are explained, not pasted; the
  viewer can understand what evidence supports the title;
- `speaker_notes`: every note tells the presenter what to say and lists the
  evidence used on that slide.

A passing deck has no blocker and every score is at least 4. Use 5 only when the
dimension is publication-ready. The reviewer must list concrete slide IDs for
every repair. "Improve design," "make it clearer," and global restyling without
localized evidence are invalid repair instructions.

## Hard blockers

- invented or unsupported paper content;
- incorrect visual-caption association or misleading crop;
- missing, duplicated, or out-of-order slide;
- a slide not inspected by the reviewer;
- illegible, clipped, overlapping, or blank content;
- visible remote/missing asset or runtime error;
- screenshot-only text/table content in the PPTX;
- missing speaker notes or source IDs;
- PPTX reopen/render failure, wrong PDF page count, or stale review binding;
- generic AI-marketing copy, decorative filler, or repeated template grids that
  displace the paper's evidence.

## Hash-bound review object

Copy binding fields exactly from `qa/review-context.json` and write:

```json
{
  "format_version": 1,
  "attempt_id": "01",
  "review_context_sha256": "...",
  "artifact_hashes": {"artifact/deck.html": "..."},
  "preview_hashes": {"contact-sheet": "...", "slide-01": "..."},
  "reviewed_frame_ids": ["contact-sheet", "slide-01"],
  "source_manifest_sha256": "...",
  "rubric_sha256": "...",
  "source_map_sha256": "...",
  "reviewer_mode": "fresh_subagent",
  "dimension_scores": {
    "source_fidelity": 4,
    "narrative_coherence": 4,
    "visual_hierarchy": 4,
    "typography_legibility": 4,
    "layout_composition": 4,
    "evidence_communication": 4,
    "speaker_notes": 4
  },
  "blockers": [],
  "localized_repairs": [],
  "verdict": "pass",
  "complete": true
}
```

`reviewed_frame_ids` must equal the sorted complete frame set from the context;
the abbreviated example above is not a valid 18-slide review. If the host has
no vision, use `needs_visual_review`; never record an unperformed review as a
pass. A failed review starts a new attempt and never mutates the reviewed one.
