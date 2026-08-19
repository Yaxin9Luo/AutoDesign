---
name: autodesign-poster
description: Use when a user asks to turn a paper, preprint, or manuscript into an editable, source-grounded academic conference poster, including PDF export or style-reference adaptation.
---

# AutoDesign Poster

Create the Poster with the host coding Agent. The Agent reads the paper, chooses
evidence, authors HTML/CSS, and makes every semantic repair. Bundled scripts
render, crop, hash, version, measure, validate, and package the result.

Resolve `SKILL_ROOT` to this installed Skill folder. Put run state, browser
cache, and generated files in a user-selected output directory. Never write
generated artifacts, caches, or run state inside this Skill directory.

Resolve `<PY>` to the first available launcher in this order: `python3`,
`python`, then Windows `py -3`. Substitute the selected launcher verbatim;
do not assume it is one shell token. Use
`<PY> "$SKILL_ROOT/scripts/poster_harness.py" --help` for exact flags.

## Read at the point of use

- Before source selection, read
  [agent-first-source.md](references/agent-first-source.md). It is the visual
  evidence authority for Poster v2.
- Before authoring or validation, read
  [output-contract.md](references/output-contract.md).
- Before either fresh review, read
  [review-rubric.md](references/review-rubric.md).

## Invariants

- PDF and complete page renders are the primary semantic surface. pdfimages is
  discovery-only and never authorizes evidence.
- The host Agent or a fresh vision-capable subagent owns semantic selection.
  There is no mandatory image-count quota.
- Scripts never edit the Poster. In particular, DOM audit reports defects but
  never changes authored HTML, CSS, layout, or content.
- Render the primary `.paper-poster` canvas as opaque pure white in both screen
  and print media so white-background paper crops remain visually integrated.
  Do not use a canvas gradient, image, tint, dark theme, transparent root, or
  root/ancestor paint effect that changes white; restrained light fills remain
  available inside local sections and panels.
- Fresh source review must pass before `plan`. The artifact reviewer may
  escalate a repair route but never downgrade it.

## Workflow

Run every command as `<PY> "$SKILL_ROOT/scripts/poster_harness.py" COMMAND ...`.
Each successful command prints JSON; retain its IDs, hashes, relative paths,
active revisions, and `next_action`.

1. Run `doctor` with an external `--cache-root`. If Chromium is absent and the
   user permits the download, rerun it with `--install-browser`.
2. Run `init --run-dir "$RUN" --source "$PAPER"`. Add only optional style
   references with repeated `--reference`; they can never become paper
   evidence.
3. Run `inspect-source --run-dir "$RUN"`. Read the returned immutable PDF
   directly and inspect every complete returned page when direct PDF vision is
   unavailable. Query grounded text with `evidence` as needed.
4. For each important complete region, write the canonical request from the
   source guide and run `crop-source`. Inspect every crop at useful zoom, then
   run `list-source-assets`; extraction hints remain untrusted.
5. Write the exact selection object, run `source-review-context`, give its
   bound previews and source pages to a fresh reviewer, then run
   `record-source-review`. Repair and repeat until a passing review commits an
   immutable catalog revision.
6. Write the exact revision-bound plan from the source guide and run `plan`.
   Use only reviewed assets, permitted roles, and catalog reuse limits.
7. Run `begin-attempt`. Read the returned attempt ID, revision IDs, staged
   assets, and `authoring-context.json`. Author its requested `poster.html` and
   claim/source map yourself. Preserve each source visual and its evidence-bound
   native readout as direct siblings in one `.source-flow-unit`. Never guess an
   attempt number.
8. Run the strictly read-only `dom-audit --attempt "$ATTEMPT"`; repair the
   authored Poster yourself if it reports findings.
9. Run `validate --attempt "$ATTEMPT" --source-map "$SOURCE_MAP"`. Continue
   only after all HTML, DOM, local-dependency, preview, and one-page PDF gates
   pass.
10. Run `review-context`, then give every bound screen/PDF frame, source map,
    evidence, and rubric to a fresh Agent that did not author the attempt. Run
    `record-review` with its complete canonical review.
11. On failure, follow the recorded route. A layout repair starts a new
    `begin-attempt`; content or source repair first uses `reopen-curation`, then
    commits the required new plan or source-review/catalog and plan revisions.
12. On pass, run `finalize --attempt "$ATTEMPT"` and deliver the complete
    verified `final/` closure, not selected files from it.

After interruption, run `resume --run-dir "$RUN"`, take the active attempt and
revision IDs from its JSON, and perform only its named safe next action:

- `author` means run `begin-attempt` and author the attempt it returns. This
  starts the first attempt, starts the next layout-repair attempt, or returns
  the active authoring attempt as appropriate.
- `retry_current_attempt` means run `begin-attempt` to recover the same active
  attempt without consuming a new semantic attempt.
- `reopen_curation` means submit the bound content/source reopen request before
  any new plan, source review, or attempt.
- Other actions name the command/stage to run using the active IDs in JSON.

A blocked, stale, failed, or unreviewed run is not a finished Poster.

## Compatibility and quality boundary

`diagnose-v1` is read-only. It reports legacy metadata but does not load,
execute, migrate, or continue the old snapshot; continue v1 only with its exact
prior package, or start a new v2 run.

This portable Skill does not replace the full AutoDesign Harness. The Harness
has stronger orchestration, assistance, and quality-control loops. Use this
Skill for convenient standalone Agent workflows while continuing to improve it
toward Harness quality.
