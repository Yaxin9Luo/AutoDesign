# Agent-first source curation and immutable planning

Treat the paper, not an extraction directory, as the source of truth. The host
Agent decides what matters. Portable scripts only render, crop, hash, bind,
review-record, and version the Agent's decisions.

Request-bearing files must use the shared stored UTF-8 JSON form: keys sorted
recursively, two-space indentation, and exactly one trailing LF. The examples
below are byte-exact. Replace every example hash, ID, page, region, and claim
with values from this run without changing the serialization.

## Inspect the whole paper first

Use this order:

1. Read `RUN/input/source.pdf` directly.
2. If direct PDF vision is unavailable or unclear, inspect every complete,
   hash-bound PNG returned by `inspect-source` under `RUN/evidence/pages/`.
3. If the host lacks vision, give those exact pages to a fresh vision-capable
   subagent.
4. If no vision path exists, stop as blocked or ask the user for preselected
   regions. Do not infer importance from filenames or extraction order.

Object-level pdfimages entries are `untrusted` hints. They can suggest a page
to inspect, but never make a region eligible and never limit what the Agent may
crop. Prefer a few complete, decisive, readable regions over fragments or
ornament; four to seven may be useful design guidance for a typical landscape
Poster, but there is no numeric pass/fail quota.

## Exact crop request

Copy the three hashes from `inspect-source`. Coordinates are normalized
`[left,top,right,bottom]`, top-left origin, with right and bottom exclusive.
The page is one-based. `role` must be one of `method`, `overview`,
`method-overview`, `result`, `primary-result`, `comparison`, `context`, or
`supporting`.

```json
{
  "bbox_normalized": [
    0.12,
    0.18,
    0.84,
    0.71
  ],
  "claim": "The crop shows the complete principal system diagram.",
  "max_reuse": 1,
  "page": 7,
  "page_manifest_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "page_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "role": "method-overview",
  "run_format_version": 2,
  "source_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

Run `crop-source`, inspect the returned crop at useful zoom, and recrop when a
compound panel, caption context, label, axis, legend, or table row is missing or
unreadable. An identical request is idempotent. Any changed region or semantic
request creates a new append-only asset and receipt. Never import a scratch
image, reconstruction, or arbitrary local file as paper evidence.

## Exact source selection

Select only registered crops. `roles` are the allowed plan roles;
`max_reuse` is the maximum number of plan allocations for that asset;
`importance` is `essential` or `supporting`. Bind both source-story categories
to real evidence IDs returned by `evidence`.

```json
{
  "assets": [
    {
      "asset_id": "src-method-example",
      "importance": "essential",
      "max_reuse": 1,
      "roles": [
        "method-overview",
        "method"
      ]
    },
    {
      "asset_id": "src-result-example",
      "importance": "essential",
      "max_reuse": 2,
      "roles": [
        "primary-result",
        "result"
      ]
    }
  ],
  "run_format_version": 2,
  "source_story": {
    "central_method": {
      "asset_ids": [
        "src-method-example"
      ],
      "evidence_ids": [
        "paper-sec-method"
      ],
      "rationale": "The complete system diagram explains the central method.",
      "status": "covered"
    },
    "primary_result": {
      "asset_ids": [
        "src-result-example"
      ],
      "evidence_ids": [
        "paper-sec-results"
      ],
      "rationale": "The result crop contains the decisive measured comparison.",
      "status": "covered"
    }
  }
}
```

Use `not_applicable` only when the paper itself supports a non-empty rationale
and at least one bound evidence ID; its `asset_ids` must be empty. A zero-visual
plan is valid only when both `central_method` and `primary_result` have a fresh
reviewed `not_applicable` decision.

## Exact source review

Run `source-review-context` with the selection. Give its exact context, copied
crop previews, PDF/pages, and evidence to a reviewer that did not make the
selection. Prefer `fresh_subagent`; otherwise conduct a deliberately separate
host pass and use `host_fresh_pass`.

```json
{
  "asset_findings": [],
  "blockers": [],
  "complete": true,
  "coverage_findings": [],
  "dimension_scores": {
    "caption_claim_match": 4,
    "crop_completeness": 4,
    "duplicate_or_ornamental_content": 4,
    "importance": 4,
    "label_axis_legend_readability": 4,
    "method_result_coverage": 4,
    "poster_area_fit": 4
  },
  "localized_repairs": [],
  "reviewer_kind": "fresh_subagent",
  "run_format_version": 2,
  "source_review_context_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
  "verdict": "pass"
}
```

A pass requires integer 4 or 5 in all seven dimensions and no blocker. For a
failure, use these exact item schemas: asset finding
`{"asset_id":"...","dimension":"crop_completeness","finding":"..."}`;
coverage finding
`{"evidence_ids":["..."],"finding":"...","story_key":"central_method"}`;
blocker `{"code":"fragmentary_crop","finding":"..."}`; localized repair
`{"instruction":"...","target":"asset:src-..."}`. A failed review must
contain at least one bound finding or blocker. Repair the selection/crops and
create a new fresh context; only a pass commits an immutable catalog revision.

## Exact immutable plan

The plan has a closed schema. `thesis` states the source-grounded Poster
argument. Every claim ID belongs to exactly one narrative section. Every visual
allocation uses claims owned by its `intended_area.section_role`, declares a
`primary` or `supporting` source-flow relationship, stays within relative area
`(0,1]`, uses a reviewed role, and stays within the catalog's `max_reuse`.

```json
{
  "artifact_type": "poster",
  "canvas": {
    "height_px": 1536,
    "width_px": 3072
  },
  "format_version": 1,
  "max_attempts": 4,
  "narrative": [
    {
      "claim_ids": [
        "claim-problem-01"
      ],
      "purpose": "Frame the source-grounded research problem.",
      "role": "problem"
    },
    {
      "claim_ids": [
        "claim-method-01"
      ],
      "purpose": "Explain the central mechanism with the original method evidence.",
      "role": "method"
    },
    {
      "claim_ids": [
        "claim-result-01"
      ],
      "purpose": "Show the decisive measured result and comparison.",
      "role": "evidence"
    },
    {
      "claim_ids": [
        "claim-takeaway-01"
      ],
      "purpose": "State the bounded conclusion and limitation.",
      "role": "takeaway"
    }
  ],
  "no_visual_fallback": null,
  "preset": "cvpr-landscape",
  "print": {
    "height_mm": 1066.8,
    "width_mm": 2133.6
  },
  "style_reference_ids": [],
  "thesis": "The method makes the paper core contribution visible and the measured result bounds the takeaway.",
  "visual_allocations": [
    {
      "claim_ids": [
        "claim-method-01"
      ],
      "intended_area": {
        "relative_area": 0.48,
        "section_role": "method"
      },
      "role": "method-overview",
      "source_flow_relationship": "primary",
      "visual_id": "src-method-example"
    },
    {
      "claim_ids": [
        "claim-result-01"
      ],
      "intended_area": {
        "relative_area": 0.42,
        "section_role": "evidence"
      },
      "role": "primary-result",
      "source_flow_relationship": "primary",
      "visual_id": "src-result-example"
    }
  ]
}
```

Named presets are `cvpr-landscape`, `a0-landscape`, `a0-portrait`,
`36x48-landscape`, and `36x48-portrait`; their canvas and physical dimensions
must match exactly. Use `custom` only for an explicit user size with matching
canvas/print aspect ratios. `max_attempts` is 1 through 8.

Optional style-reference IDs must point only to inputs supplied as style
references. They transfer geometry, spacing, palette, and typography only;
never copy their identity, wording, claims, logos, QR codes, links, figures, or
tables. A native explanatory diagram/table may supplement a reviewed source
visual but never replace essential original evidence.

## Revision and recovery rules

- A passing source review commits one immutable catalog revision.
- `plan` commits one immutable plan revision bound to that catalog.
- `begin-attempt` snapshots the exact source, catalog, plan, authorized asset
  hashes, receipts, parent attempt, and supersession prefix.
- Later catalog/plan revisions never change an older attempt. Do not edit an
  old attempt to make a new repair look successful.
- Obtain every active revision and attempt ID from command JSON or `resume`.
- Runtime failures retry the same attempt. Semantic repairs follow the routes
  in [review-rubric.md](review-rubric.md).
