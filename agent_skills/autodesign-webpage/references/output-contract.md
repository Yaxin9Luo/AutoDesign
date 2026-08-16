# Research webpage output contract

Use this contract literally. Persisted run contracts use `format_version: 1`;
the transient PDF visual-review batch uses the exact schema shown below. The
harness rejects unknown, incomplete, stale, symlinked, or hash-drifted inputs.

## Plan

`plan.json` has this exact top-level shape:

```json
{
  "format_version": 1,
  "artifact_type": "research_webpage",
  "brief": "Create an editorial research project page for a technical audience.",
  "title_claim_id": "claim-title",
  "thesis_claim_id": "claim-thesis",
  "sections": [
    {"id": "identity", "role": "identity", "claim_ids": ["claim-title", "claim-thesis"]},
    {"id": "abstract", "role": "abstract", "claim_ids": ["claim-abstract"]},
    {"id": "method", "role": "method", "claim_ids": ["claim-method"]},
    {"id": "evidence", "role": "evidence", "claim_ids": ["claim-method"]},
    {"id": "results", "role": "results", "claim_ids": ["claim-results"]},
    {"id": "limitations", "role": "limitations", "claim_ids": ["claim-limitations"]},
    {"id": "resources", "role": "resources", "claim_ids": ["claim-paper-url"]},
    {"id": "citation", "role": "citation", "claim_ids": []}
  ],
  "visual_allocations": [{"visual_id": "vis-001", "role": "overview"}],
  "interactions": [{
    "id": "inspect-method",
    "kind": "inspect",
    "claim_ids": ["claim-method"],
    "visual_ids": ["vis-001"],
    "control_id": "inspect-method-control",
    "target_id": "method-figure",
    "state_attribute": "aria-pressed"
  }],
  "resource_links": [{
    "label": "Paper",
    "url": "https://source-provided.example/paper",
    "source_ids": ["ev-042"]
  }],
  "missing_metadata": ["code_url", "data_url", "license"],
  "max_attempts": 4
}
```

The eight roles are required once each, in the shown order. Section IDs,
interaction IDs, controls, and targets are stable HTML identifiers. The plan
needs at least one `inspect` or `compare` interaction bound to planned claims or
visuals. `navigate` may supplement it. Valid state attributes are
`aria-current`, `aria-expanded`, `aria-pressed`, `aria-selected`, `data-active`,
and `data-state`. `max_attempts` is 1-6.

Copy the user's actual request into `brief`; do not silently replace its
language, audience, emphasis, or visual constraints with a generic default.

Every HTTPS resource URL must occur verbatim in its cited evidence. Omit an
unsupported URL and declare the relevant missing field; do not invent a
placeholder. Allowed missing fields are authors, affiliations, venue, date,
paper URL, code URL, data URL, citation, and license.

## PDF visual review

Only PDF-extracted candidates need this step. Build the payload from current
`source_manifest.json`, `source_visuals.json`, the candidate visual bytes, and
caption evidence. Repeat their exact SHA-256 values:

```json
{
  "reviewer_mode": "fresh_host_vlm",
  "source_manifest_sha256": "...",
  "source_visuals_sha256": "...",
  "matches": [{
    "visual_id": "vis-001",
    "visual_sha256": "...",
    "caption_evidence_id": "ev-042",
    "caption_evidence_sha256": "...",
    "confidence": 0.93,
    "allowed_content_roles": ["overview", "method"]
  }]
}
```

Use the exact `reviewer_mode` accepted by `_portable.py`; generate the payload
from the current catalog rather than copying this example. Confidence must be a
finite number from 0.8 through 1. An explicit attached asset is already eligible.

## HTML and asset closure

The canonical artifact is `attempts/<id>/artifact/index.html` plus local files
under the same `artifact/` directory. `index.html` is the only allowed HTML
document: do not ship linked or unlinked `.html`/`.htm` sidecars.

- Use HTML5 doctype, `html[lang]`, viewport metadata, one `main`, one visible
  `h1`, a labeled `nav`, and a skip link to `#main`.
- Mark the eight narrative containers with `data-section-role`. Bind the H1 and
  thesis to the plan and keep at least half of each visible within the initial
  1440 x 1000 desktop viewport. Mark every visible source claim with one `data-claim-id`; after
  whitespace normalization its visible text must exactly equal that source-map
  claim's text.
- Write each unavailable field in visible native text and tag it with
  `data-missing-metadata`. The marker set must exactly equal the plan.
- Mark a staged evidence image with `data-source-id` on the `img`, `source`, or
  its containing figure. Preserve the staged bytes exactly and provide useful
  alt text and a source-grounded caption.
- Implement every interaction with a native `button` or `a`,
  `data-interaction-id`, `aria-controls`, an accessible name, and the planned
  observable state attribute. Its target must contain a bound claim or visual.
  Activation must also visibly change the target's text, geometry, or computed
  presentation; changing only ARIA/data attributes is a no-op and fails. At
  least one planned control must remain visible, enabled, unclipped, and at
  least 24 x 24 CSS pixels at 390 px width, and its activation must still
  produce the planned visible target change there.
- Keep core research content and evidence visible when JavaScript is disabled.
  JavaScript may annotate, compare, filter, or focus evidence; it may not fetch
  data, hide the only copy of evidence, or gate reading.
- Use only local CSS, JS, fonts, images, media, and downloads. Remote assets,
  data URLs, `@import`, iframes, objects, embeds, base tags, forms, meta refresh,
  network APIs, broken fragments, positive tabindex, and unlisted external
  links are forbidden.
- JavaScript navigation (`location`, `window.open`) is forbidden. Browser QA
  tracks timers for 2.5 seconds and fails closed if delayed work does not settle
  or attempts a blocked request; do not use persistent or long-delay timers.
- Provide a browser-measurable visible `:focus-visible` treatment, a mobile breakpoint, and effective
  `prefers-reduced-motion: reduce` overrides for every active motion mode.
- Use 3-8 restrained functional inline SVG icons. Name interactive icons; hide
  decorative ones from accessibility APIs.

## Source map

`claims.json` is either a claim list or `{ "claims": [...] }`. Each claim uses:

```json
{
  "id": "claim-results",
  "text": "The paper reports 91% evidence coverage.",
  "source_ids": ["ev-017"],
  "quote": false
}
```

Use only evidence IDs from `evidence/evidence.jsonl`. Direct quotes and numbers
must pass the portable grounding rules. Every planned/mapped claim must be
rendered in native HTML. Formula-derived values additionally require the
portable formula fields and source-grounded operands.

## Browser and delivery outputs

A passing attempt contains:

```text
attempts/<id>/
  artifact/
    index.html
    assets/...
    webpage-validation.json
    browser-audit.json
    interaction-audit.json
  provenance/source-map.json
  qa/
    deterministic.json
    review-context.json
    semantic-review.json
    previews/desktop.png
    previews/mobile.png
```

The final directory contains the exact reviewed artifact closure,
`provenance/source-map.json`, and `delivery-manifest.json`. Screenshot hashes
bind the semantic review but previews remain in attempt QA. Never manually copy
an attempt into `final/`. Use the actual `attempt_id` returned by each `begin`
command; repair attempts are not necessarily `01`.
