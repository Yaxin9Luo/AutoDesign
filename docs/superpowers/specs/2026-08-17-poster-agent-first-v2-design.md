# Poster Agent-First PDF Ingestion v2 Design

## Status

Approved architecture for the first quality-focused iteration after the public
Agent Skills launch. This document covers `autodesign-poster` only.
Implementation has not started.

The earlier experimental branch `codex/agent-first-pdf-skills` is a source of
reviewable ideas and tests, not a branch to merge wholesale. It predates the
current public `main`. Any reusable source-curation code must be ported
surgically onto current `main`, reviewed again, and proven by new tests.

## Problem

The released Poster Skill is portable and deterministic, but its source-image
workflow is catalog-first:

```text
immutable PDF
  -> Poppler page renders and object-level pdfimages candidates
  -> a filtered visual catalog
  -> a fixed poster plan
  -> later attempts repair the same selected evidence
```

This makes a lossy extraction catalog the boundary of the Agent's reasoning.
The host Agent may recognize that a candidate is only a fragment of a compound
figure, that the central method figure is missing, or that an unreadable crop
should be replaced, but the current run contract cannot safely register a new
PDF-region asset and revise the plan.

The result is not mainly a prompt or visual-rubric problem. It is an upstream
information and control problem: the Agent sees too little source evidence and
cannot revise the frozen evidence decision.

## Goal

Make the source PDF the primary semantic surface, let the host Agent iteratively
select and verify important paper evidence, and retain the deterministic safety
of a standalone installable Skill.

The target is a Poster Skill that:

1. lets Codex, Claude Code, DeepSeek Harness, or another capable host inspect
   the original PDF directly;
2. falls back to complete, hash-bound page renders when native PDF vision is
   unavailable;
3. lets the Agent select a page and normalized crop without being restricted
   by `pdfimages` output;
4. records every source derivation, review, catalog revision, plan revision,
   attempt, and final artifact immutably;
5. internalizes useful deterministic Poster checks from the full Harness; and
6. keeps the Agent, not a script, responsible for semantic selection and design
   edits.

## Non-goals

- Do not copy the AutoDesign server, `PipelineRunner`, queues, SSE, Web UI,
  provider configuration, or API judges into the Skill.
- Do not require a locally running AutoDesign service.
- Do not make `pdfimages`, an automatic ranking heuristic, or a fixed image
  count the final authority on source importance.
- Do not let deterministic tools rewrite Poster HTML, CSS, layout, or content.
- Do not add automatic vector reconstruction, table OCR, or a template
  generator in this iteration.
- Do not weaken the existing one-page PDF, editable HTML, provenance, browser,
  network, typography, or final-publication contracts.
- Do not change PPT, Webpage, or Video in this spec. Their Agent-first work
  follows only after Poster passes the real acceptance matrix.

## Core Boundary

The Agent owns decisions. Scripts own reproducibility and measurement.

| Capability | Owner |
| --- | --- |
| Read and interpret the paper | Host Agent |
| Decide which figures and results matter | Host Agent |
| Choose page and bounding box | Host Agent |
| Judge whether a crop is complete and useful | Fresh Agent review |
| Author and repair Poster HTML/CSS | Host Agent |
| Render pages and crop pixels | Deterministic scripts |
| Hash, register, version, and verify assets | Deterministic scripts |
| Measure DOM geometry and report defects | Read-only DOM audit |
| Modify layout in response to audit findings | Host Agent |

Page rendering and cropping are representation utilities, not semantic
substitutes for the Agent.

## Architecture

```text
immutable source.pdf
  -> Host Agent reads PDF
  -> complete page renders / zoomed inspection as fallback
  -> Agent selects page + normalized bbox
  -> deterministic crop from a verified page
  -> append-only crop receipt + provenance
  -> fresh source-curation review
  -> immutable catalog revision
  -> immutable Poster plan revision
  -> authoring attempt bound to both revisions
  -> read-only DOM audit + deterministic validation
  -> semantic artifact review
       -> layout_repair
       -> content_replan
       -> source_reingest
  -> reviewed final delivery
```

### Capability fallback

Use the strongest available source-reading path in this order:

1. Inspect `RUN/input/source.pdf` directly with the host Agent.
2. Inspect every complete hash-bound page PNG under
   `RUN/evidence/pages/`.
3. Ask a fresh vision-capable subagent to inspect those exact pages when the
   host supports subagents but the main Agent lacks vision.
4. Stop as blocked or ask the user for preselected source evidence when no
   vision-capable path exists.

Never silently promote object-level `pdfimages` output to a semantic
replacement. Keep it as an explicitly untrusted hint manifest.

## Portable Package Boundary

The installed Poster Skill remains self-contained:

```text
agent_skills/autodesign-poster/
  SKILL.md
  scripts/
    poster_harness.py
    poster_dom_audit.py
    _portable.py
    portable_png.py
    setup_browser.py
  references/
    agent-first-source.md
    output-contract.md
    ...
```

The final names may reuse existing files where that keeps the package smaller,
but the boundaries remain distinct:

- shared portable state, hashing, revision, and crop functions stay free of
  Poster-specific policy;
- Poster orchestration and DOM checks stay Poster-specific;
- the installed package imports no AutoDesign product module at runtime; and
- every mutable run file stays outside the installed Skill tree.

Candidate shared code from the stale experimental branch must pass a current-
main diff review before porting. Candidate DOM algorithms may be adapted from
`autodesign/tools/paper_poster_renderer.py` and
`autodesign/util/poster_gate_audit.py`, but only their read-only measurement and
finding logic is eligible. Auto-fit, font shrinking, panel expansion, generated
CSS, and any other mutation path are excluded.

## Versioned Run Data

New Agent-first runs use run format version 2. Version-1 runs are never silently
migrated or mutated by v2 code.

Run format 2 is an explicit initialization opt-in. The shared portable core is
vendored into all four Skill packages, but its default initialization behavior
remains version 1 until an artifact harness opts in. In this iteration only
`autodesign-poster` requests version 2; PPT, Webpage, and Video retain their
released version-1 behavior and tests.

```text
RUN/
  input/
    source.pdf
  evidence/
    pages/page-0001.png
    page-manifest.json
    pdfimages-hints.json
  source-assets/
    files/asset-0001.png
    receipts/asset-0001.json
  source-reviews/
    review-0001/context.json
    review-0001/review.json
  curations/
    001/catalog.json
    001/COMMIT.json
    002/catalog.json
    002/COMMIT.json
  plans/
    001/poster-plan.json
    001/COMMIT.json
    002/poster-plan.json
    002/COMMIT.json
  attempts/
    01/attempt-context.json
    01/catalog-snapshot.json
    01/plan-snapshot.json
    01/artifact/
    01/qa/dom-audit.json
    01/qa/deterministic.json
    01/qa/semantic-review.json
    02/...
  final/
  run.json
  events.jsonl
```

### Invariants

- The source PDF and page renders are immutable and hash-bound.
- Every crop binds the source PDF hash, page hash, page number, page geometry,
  normalized bbox, renderer identity, DPI, and output hash.
- Repeating the same crop request is idempotent and returns the same asset.
- Different crop requests create new append-only assets and receipts.
- A catalog revision is immutable after a passing source review.
- A plan revision references exactly one catalog revision and only reviewed
  assets.
- An attempt snapshots exact source, catalog, and plan hashes.
- Later revisions never alter how an older attempt validates.
- Final delivery comes from one exact attempt that passed deterministic and
  semantic review.

Catalog and plan commits use sibling staging, a canonical `COMMIT.json`, atomic
promotion, and a compare-and-set update of `run.json`. Resume may complete an
unambiguous prepared commit or discard incomplete staging, but it must fail
closed on conflicting bytes or parents.

## Command Contract

Keep existing stable Poster commands where their semantics remain valid and
add one shared Agent-first vocabulary:

```text
poster_harness.py inspect-source
poster_harness.py crop-source
poster_harness.py list-source-assets
poster_harness.py source-review-context
poster_harness.py record-source-review
poster_harness.py reopen-curation
poster_harness.py dom-audit
poster_harness.py diagnose-v1
```

For a v2 run, `init` prepares the immutable PDF and page manifest; `evidence`
returns grounded text, claims, pages, and untrusted extraction hints without
freezing a visual whitelist; `plan` commits a numbered plan revision;
`begin-attempt`, `validate`, `review-context`, `record-review`, `finalize`, and
`resume` operate on revision-bound attempts. The legacy `bind-visuals` command
is invalid for v2 because its one-time catalog semantics contradict iterative
source curation. No two commands may perform the same state transition under
different undocumented rules.

All commands:

- print one JSON result to stdout;
- return nonzero for blocked, failed, corrupt, or incomplete work;
- persist only relative run paths, never machine-specific absolute paths;
- reject symlinks, hardlinks, containment escapes, and noncanonical request
  bytes where relevant; and
- create no bytecode or cache inside the installed Skill.

## Source Curation

### Crop request

The Agent selects a one-based page and normalized top-left-origin bbox:

```json
{
  "page": 7,
  "bbox": [0.12, 0.18, 0.84, 0.71],
  "kind": "figure",
  "poster_role": "method-overview",
  "supports_claims": ["claim-method-01"],
  "why_essential": "The paper's principal system diagram"
}
```

The crop command verifies the exact page manifest and performs no semantic
approval. Arbitrary scratch images cannot be imported as paper evidence.

### Selection policy

Remove the current visual-count floor as a deterministic eligibility gate.
Prefer fewer complete, important, readable source visuals over a larger set of
fragments or decorative screenshots.

Every proposed asset records:

- source page and bbox;
- proposed Poster role;
- evidence or claim IDs;
- why the visual is essential;
- intended reuse limit; and
- crop-integrity status.

The selected set must cover the central method and primary result when the
paper contains them. A missing category may be marked `not_applicable` only
with a non-empty, source-grounded rationale. Four to seven distinct visual
regions is a design suggestion for a typical landscape Poster, not a pass/fail
quota.

### Fresh source review

Before committing a catalog revision, create a hash-bound review context over
the exact source, pages, crop receipts, previews, selected asset set, proposed
roles, and evidence IDs.

Prefer a fresh vision-capable subagent when the host supports one. Otherwise
the host Agent performs an explicit separate review pass and records
`reviewer_kind: host_fresh_pass`. The deterministic script validates the review
schema and bindings; it does not manufacture a semantic score.

The review checks:

- importance to the paper's thesis;
- crop completeness;
- caption and claim match;
- label, axis, legend, and table readability;
- duplicate or ornamental content;
- central-method and primary-result coverage; and
- whether the selected visual deserves its proposed Poster area.

A passing review commits a new immutable catalog revision. A failing review
keeps the run in source curation and names localized repairs.

## Poster Planning and Attempts

The Poster plan binds an exact catalog revision and assigns reviewed assets to
semantic sections. It must describe the Poster thesis, section arc, claim IDs,
visual roles, source-flow relationships, intended area, and reuse limits.

The plan must not reconstruct a source figure and then treat the reconstruction
as source evidence. Native HTML diagrams and tables may explain a method, but
the plan preserves the original source visual when it is essential evidence.

`begin-attempt` freezes:

- source-manifest hash;
- catalog revision and hash;
- plan revision and hash;
- exact authorized asset IDs and hashes;
- parent attempt and supersession record, when present.

Source curation and planning do not consume an artifact attempt. The attempt
budget advances only when a new authoring attempt begins.

## Read-Only DOM Audit

Expose a Poster-only command:

```text
poster_harness.py dom-audit --run-dir RUN --attempt 01
```

It may write screenshots, metrics, and `qa/dom-audit.json`; it must never write
or rewrite files under `attempts/01/artifact/`.

The audit reuses one internal engine for the standalone command and the DOM
portion of final `validate`. It measures:

- rendered text nodes and client rectangles;
- computed typography and visibility;
- scroll overflow, clipping, and viewport escape;
- element and panel overlap;
- canvas, column, panel, and lower-edge fill;
- internal blank bands and sparse oversized panels;
- image displayed area and effective resolution;
- native table width, font size, and overflow;
- source-flow gutter and sibling relationships;
- screen/print canvas parity; and
- high-confidence boxiness or template-pattern signals.

Each finding contains a stable code, `block_id`, severity, geometry evidence,
and suggested repair route. The report includes artifact hashes captured before
and after the audit. A regression test requires those hashes and bytes to be
identical.

The audit is not an auto-layout engine. It does not move elements, expand
panels, shrink fonts, inject CSS, or accept its own repairs. The Agent reads the
report, decides whether the proposed repair preserves design intent, edits the
Poster, and reruns the audit.

## Review and Repair Routing

Artifact review has three ordered semantic repair routes:

```text
layout_repair < content_replan < source_reingest
```

- `layout_repair`: retain catalog and plan; begin the next authoring attempt.
- `content_replan`: retain catalog; commit a new plan revision before the next
  attempt.
- `source_reingest`: return to the PDF, create append-only replacement crops,
  pass a fresh source review, commit a new catalog and plan, then begin the next
  attempt. Previous crops remain immutable but need not appear in the new
  catalog.

The Agent may escalate a proposed route but may not downgrade it. If any
finding says a key visual is missing, wrong, incomplete, fragmentary, or
unreadable, `layout_repair` is invalid. When several findings coexist, the
strongest required route wins.

Deterministic environment failures such as missing Poppler, browser startup,
or export runtime errors remain on the same attempt and do not consume a new
semantic attempt.

`resume` reports exactly one safe next action, including source inspection,
source review, planning, authoring, DOM audit, validation, semantic review,
reopen, finalize, or complete.

## Version-1 Compatibility

V2 detects the run format before loading a Skill snapshot. Mutating v2 commands
reject v1 runs. `diagnose-v1` may read old manifests and events as data but
never imports or executes the old run snapshot. Continuing a v1 run requires
its exact prior Skill package; otherwise the user starts a new v2 run.

## Testing Strategy

Use RED-GREEN-REFACTOR. Do not weaken a source or delivery gate merely to make
an old fixture pass.

### Shared deterministic tests

1. Direct-PDF and full-page fallback expose the same immutable source identity.
2. The Agent can create a valid crop absent from `pdfimages` hints.
3. Invalid page, bbox, page hash, source drift, symlink, hardlink, or containment
   escape fails without outside mutation.
4. Crop registration is deterministic, append-only, crash-atomic, and
   idempotent.
5. Source review binds the exact context and cannot approve changed assets.
6. Catalog and plan revisions are immutable and recover correctly across every
   transaction crash window.
7. Attempt 01 remains bound to catalog/plan revision 1 after Attempt 02 uses
   revision 2.
8. Curation and re-planning do not consume artifact attempts.
9. Route escalation is accepted and route downgrade is rejected.
10. Resume returns the exact next safe action for every persisted boundary.

### Poster-specific deterministic tests

1. A reviewed complete figure crop can satisfy method/result coverage even
   when object-level extraction produced only fragments.
2. No fixed visual-count floor forces unrelated images into a Poster.
3. Only assets referenced by the attempt's plan are staged.
4. Missing or unreadable key evidence requires `source_reingest`.
5. DOM fixtures detect clipping, overlap, blank bands, low effective image
   resolution, table overflow, source-flow defects, and screen/print mismatch.
6. `dom-audit` changes only QA outputs and leaves every artifact byte unchanged.
7. Existing one-page physical PDF, editable HTML, typography, network, source
   map, and exact publication tests remain green.

### Real acceptance matrix

Test at least three papers:

1. a method-architecture-heavy paper;
2. a result/table/curve-heavy paper; and
3. a paper whose important figure is missing or fragmented in `pdfimages` and
   therefore requires direct page inspection and a new crop.

Run the installed Skill outside the repository in a native host conversation.
Codex is the first stabilization host. After the behavior is stable, run at
least one representative DeepSeek Harness pass. Preserve commands, run state,
trajectory, revision history, reviews, rendered Poster, editable HTML, and PDF.

Before generation, a human records the must-have visual list for each test
paper so the test cannot redefine importance after seeing the output. Hard
acceptance requirements:

- zero invented claims, numbers, logos, or paper evidence;
- at least 80% recall against a predeclared human list of must-have source
  visuals, with no major wrong visual;
- at least one correct complete PDF-region visual that object-level extraction
  did not provide correctly;
- central method and primary result present or explicitly rejected with a
  source-grounded rationale;
- a deliberately bad fragment routes to `source_reingest` and succeeds through
  a new catalog/plan revision;
- old attempts remain intact;
- one-page physical PDF and editable HTML pass all existing delivery gates;
- no clipped, overlapping, or unreadable body content; and
- DOM audit proves artifact-byte immutability.

Blindly compare each final Poster with the full AutoDesign Harness output while
hiding the generator identity. Score evidence selection, information hierarchy,
typography, visual balance, professionalism, anti-template quality, and
editability on the same finite 1-5 rubric. For each paper, divide the Skill's
seven-dimension mean by the Harness mean; the median ratio across the three
papers must be at least 0.75. A missing or wrong must-have visual is a hard
failure and cannot be averaged away by other scores.

## Implementation Sequence

1. Port and re-review the shared v2 source-curation substrate onto current
   public `main` with its deterministic tests.
2. Integrate the v2 commands and state transitions into Poster only.
3. Update Poster `SKILL.md` and focused references using progressive
   disclosure; keep operational instructions concise.
4. Internalize the standalone read-only Poster DOM audit and bind it to
   `validate`.
5. Run deterministic, crash-recovery, package, checksum-install, and read-only
   installed-package verification.
6. Run the three-paper real acceptance matrix and obtain a fresh independent
   review.
7. Only after Poster passes, write a separate design and implementation plan
   for PPT, then Webpage, then Video.

Each implementation slice is committed and independently reviewable. No step
may merge unrelated stale-branch history or publish before its applicable
verification gates pass.

## Approved Decisions

- Use Scheme B: Agent-first PDF ingestion plus revisioned planning.
- Deliver Poster first; follow with PPT, Webpage, and Video only after Poster
  proves the architecture.
- Treat the PDF or complete page renders as the primary semantic surface.
- Keep `pdfimages` as hints only.
- Let the Agent choose semantic importance and crop regions.
- Keep crop, hash, receipt, state, and deterministic validation in scripts.
- Internalize useful full-Harness algorithms when they remain portable.
- Keep the DOM tool strictly read-only; the Agent alone edits the Poster.
- Allow repair-route escalation and forbid downgrade.
- Require a three-paper blind acceptance matrix rather than a single showcase.
