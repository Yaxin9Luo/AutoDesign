# Research webpage review rubric

Review the rendered artifact, not the author's explanation. Prefer a fresh
vision-capable subagent with no authoring history. Inspect both `desktop` and
`mobile` screenshots in `review-context.json`, then compare the visible claims
and visuals with the source map and evidence. Apply the user's hash-bound
`rubric.brief`; do not replace it with generic design preferences. Do not call
an external judge API.

## Immediate blockers

Fail before scoring when any of these is visible or evidenced:

- invented, contradicted, or materially distorted paper claims, metrics,
  authors, affiliations, venue, links, citation, or project metadata;
- a copied reference logo, text, claim, figure, link, or QR/scannable code;
- missing/incorrect source visual, unreadable evidence, hidden core content, or
  a decorative interaction unrelated to the cited research;
- clipped, overlapping, horizontally overflowing, or unusably small content in
  either required viewport;
- a transparent, tinted, dark, gradient, or image-backed primary research
  canvas instead of opaque pure white on `html`, `body`, and `main`, including
  drift introduced by responsive CSS, runtime JavaScript, opacity, filters,
  masks, blending, or clipping on those surfaces or an ancestor wrapping
  `main`;
- generic product-marketing copy, repetitive cards, decorative gradients,
  excessive pills, arbitrary glow, stock SaaS composition, or other AI-slop;
- a broken primary navigation/control, absent keyboard affordance, or an
  interaction whose initial state is the only way to access evidence;
- an interaction that changes only ARIA/data state without a visible target
  change, no usable `inspect`/`compare` control at 390 px, a control skipped by
  sequential keyboard focus, an invisible keyboard focus treatment, or a
  title/thesis pushed below the initial desktop viewport;
- visible numeric/URL/formula assertions outside exact source bindings, section
  claim IDs that differ from the plan, generated pseudo-element prose, or a
  thesis marker that is not itself the exact thesis claim, including equivalent
  text or claim mutation injected after load;
- a full narrative claim repeated across sections, or a `data-claim-ref` that
  repeats claim text, points anywhere except the claim-owning section, or drifts
  from its declared section after load;
- paint-transparent, background-matched, clipped, masked, transformed-away,
  effectively transparent, or collapsed evidence in either the no-JS or
  JavaScript-enabled state;
- duplicate attributes, inline event handlers, any additional HTML sidecar,
  unreachable/scratch/hardlinked artifact files, delayed/persistent timers,
  animation frames or Web Animations in default or reduced motion, or scripted
  navigation/egress paths;
- incomplete screenshot set or any context/hash mismatch.

## Dimensions

Score every dimension from 1 to 5. Use 3 for acceptable but visibly improvable,
4 for publication-ready, and 5 only for unusually strong work.

### `source_fidelity`

Claims, numbers, links, labels, and visual interpretations match the cited
paper evidence. Missing metadata is stated without apology or invention.

### `research_narrative`

The opening identifies the paper and thesis immediately. Abstract, method,
evidence, results, limitations, resources, and citation form one deliberate
research story rather than a generic landing-page funnel.

### `visual_hierarchy`

Section rhythm, scale, whitespace, alignment, and evidence placement guide a
reader through the argument. Desktop and mobile each feel intentionally
composed; neither is merely a shrunken version of the other. Judge the primary
canvas separately from permissible local light section/card fills, controls,
code blocks, and browser chrome.

### `typography`

Body copy, headings, labels, tables, captions, and links are readable and
coherent. Line lengths, contrast, density, and type scale suit research reading.

### `evidence_use`

Figures, tables, captions, and quantitative results do explanatory work. They
are legible, correctly framed, source-bound, and not decorative thumbnails.

### `interaction_utility`

Inspect/compare behavior helps a reader understand source evidence. Controls
have clear names and state; the static document remains complete without the
interaction.

### `accessibility_responsive`

Reading order, focus, controls, links, alt text, reduced motion, and desktop/
mobile layouts are usable. Do not infer a pass from the desktop screenshot.

### `anti_slop`

The page avoids formulaic AI aesthetics and empty hype. Its visual language is
specific to the paper, editorially restrained, and consistent across sections.

## Pass policy

A passing review requires:

- no blockers;
- every dimension present and at least 3;
- mean score at least 4.0;
- `source_fidelity` and `anti_slop` at least 4.

Use `needs_visual_review` only when the host truly cannot inspect images. Do not
use it to waive weak work or missing screenshots.

## Hash-bound review JSON

Copy the following binding fields exactly from `review-context.json`:

```json
{
  "format_version": 1,
  "attempt_id": "<actual attempt_id from begin>",
  "review_context_sha256": "...",
  "artifact_hashes": {},
  "preview_hashes": {"desktop": "...", "mobile": "..."},
  "reviewed_frame_ids": ["desktop", "mobile"],
  "source_manifest_sha256": "...",
  "rubric_sha256": "...",
  "source_map_sha256": "...",
  "reviewer_mode": "fresh_subagent",
  "dimension_scores": {
    "source_fidelity": 4,
    "research_narrative": 4,
    "visual_hierarchy": 4,
    "typography": 4,
    "evidence_use": 4,
    "interaction_utility": 4,
    "accessibility_responsive": 4,
    "anti_slop": 4
  },
  "blockers": [],
  "localized_repairs": [],
  "verdict": "pass",
  "complete": true
}
```

For a failure, name concrete regions and fixes, for example:

```json
{
  "blockers": ["mobile results table clips its final metric column"],
  "localized_repairs": [
    {"region": "#results table", "issue": "mobile overflow", "repair": "use a labeled native overflow wrapper and keep headers visible"}
  ],
  "verdict": "fail"
}
```

Start a new bounded attempt for repairs. Do not mutate a deterministically
recorded attempt or reuse its stale review context.
