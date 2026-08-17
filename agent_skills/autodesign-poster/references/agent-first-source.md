# Agent-first source curation

Treat the paper, not an extraction folder, as the source of truth. Let the host
Agent decide what matters; use the bundled portable code only to render, crop,
hash, register, review-bind, and version those decisions.

## Inspect before selecting

Use this order:

1. Inspect the immutable `RUN/input/source.pdf` directly.
2. If direct PDF vision is unavailable or unclear, inspect every complete,
   hash-bound page under `RUN/evidence/pages/`.
3. If the host lacks vision but supports subagents, give those exact pages to a
   fresh vision-capable subagent.
4. If no vision-capable path exists, stop and ask for source evidence. Do not
   infer important visuals from filenames or extraction order.

Treat `pdfimages` output as untrusted discovery hints only. Never select a hint
as evidence until an exact region has a registered crop receipt.

## Register exact PDF regions

Use normalized `[left, top, right, bottom]` coordinates with a top-left origin.
Values stay in `[0, 1]`; right and bottom are exclusive. Bind the request to the
exact source, page manifest, page number, and page hash returned by source
inspection:

```json
{
  "run_format_version": 2,
  "source_sha256": "COPY_SOURCE_SHA256",
  "page_manifest_sha256": "COPY_PAGE_MANIFEST_SHA256",
  "page": 7,
  "page_sha256": "COPY_PAGE_SHA256",
  "bbox_normalized": [0.12, 0.18, 0.84, 0.71],
  "role": "method-overview",
  "claim": "The paper's principal system diagram.",
  "max_reuse": 1
}
```

Inspect the resulting crop at useful zoom. Re-crop incomplete panels, clipped
captions, unreadable axes, detached legends, or fragments that lose necessary
context. Never import a scratch image, regenerated figure, or arbitrary local
file as paper evidence.

## Curate, then review freshly

Select only registered crops. Assign structural roles, reuse limits, and
`essential` or `supporting` importance. Cover both `central_method` and
`primary_result` with bound asset and evidence IDs. Use `not_applicable` only
when the source itself supports a non-empty rationale and at least one evidence
ID; attach no asset IDs to that status.

Give the immutable review context and its complete copied preview set to a
reviewer that did not make the selection. Prefer a fresh vision-capable
subagent; otherwise perform a deliberate separate host pass and record
`host_fresh_pass`. Score all seven bound dimensions:

- importance;
- crop completeness;
- caption and claim match;
- label, axis, legend, and table readability;
- duplicate or ornamental content;
- central-method and primary-result coverage;
- fitness for the proposed Poster area.

Pass only with integer scores of 4 or 5 in every dimension and no blocker. On
failure, bind each finding to a selected asset or source-story category and
name a localized repair. Keep curating until a fresh passing review commits an
immutable catalog revision.

Prefer a small set of complete, decisive, readable regions over many fragments.
Do not impose a fixed image count, promote decorative filler, or invent a visual
to satisfy a quota.
