---
name: autodesign-webpage
description: Use when turning a paper, preprint, or manuscript into a responsive research project webpage, especially when the page must remain source-grounded, editable, local, accessible, and independently reviewed in a real browser.
---

# AutoDesign Webpage

Build an editorial research page from the user's paper. The installed Skill is
read-only; put every mutable file in a user-selected output directory. Do not use
the AutoDesign repository, package, or server.

## Set up the run

Resolve `SKILL_ROOT` to the directory containing this `SKILL.md`, then use:

```bash
HARNESS="$SKILL_ROOT/scripts/webpage_harness.py"
python3 "$HARNESS" doctor
python3 "$HARNESS" init --run-dir "$RUN_DIR" --source "$PAPER"
STATUS_JSON="$(python3 "$HARNESS" status --run-dir "$RUN_DIR")"
printf '%s\n' "$STATUS_JSON"
ATTEMPT_ID="$(printf '%s' "$STATUS_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["active_attempt"] or "")')"
```

Pass each user-supplied figure with `--asset`; pass visual references only with
`--reference`. Never write into `$SKILL_ROOT`. For a PDF, treat missing Poppler
or browser runtime as blocked, fix the environment, and resume the same run.

Read [source-grounding.md](references/source-grounding.md),
[output-contract.md](references/output-contract.md), and
[review-rubric.md](references/review-rubric.md) before planning.

## Plan from evidence

Read `evidence/evidence.jsonl`, `evidence/source_manifest.json`, and
`evidence/source_visuals.json`. Use stable evidence IDs; never invent claims,
links, metrics, authors, affiliations, venues, or project metadata.

For PDF-extracted visuals marked `review_required`, inspect the actual visual
and its candidate caption with fresh host vision. Write the exact hash-bound
review JSON described in the output contract, then run:

```bash
python3 "$HARNESS" bind-visuals --run-dir "$RUN_DIR" --review-json visual-review.json
```

Create `plan.json` outside the Skill. Give the first viewport the paper identity
and thesis, then abstract, method, evidence, results, limitations, resources,
and citation. Add at least one source-bound `inspect` or `compare` interaction;
navigation alone is not meaningful. Copy the user's actual request into
`plan.brief`, including its language, audience, emphasis, and visual constraints.
Declare unavailable metadata truthfully.

```bash
python3 "$HARNESS" plan --run-dir "$RUN_DIR" --plan-json plan.json
```

## Author one bounded attempt

```bash
ATTEMPT_ID="$(python3 "$HARNESS" begin --run-dir "$RUN_DIR" | python3 -c 'import json,sys; print(json.load(sys.stdin)["attempt_id"])')"
python3 "$HARNESS" stage-visual --run-dir "$RUN_DIR" --attempt "$ATTEMPT_ID" --visual-id vis-001
```

Author `$RUN_DIR/attempts/$ATTEMPT_ID/artifact/index.html` with editable native
HTML/CSS. Keep the research narrative visible without JavaScript. Use local
files only; use inline SVG only for restrained functional icons. Bind visible
claims with `data-claim-id`, and make the normalized visible text of each
binding exactly match that claim's source-map text. Bind source visuals with
`data-source-id`, sections with `data-section-role`, and interactions with the
IDs and accessible state in the plan. Copy no text, logos, claims, figures,
links, or codes from style references.

Write a source-map claims JSON, then validate:

```bash
python3 "$HARNESS" source-map --run-dir "$RUN_DIR" --attempt "$ATTEMPT_ID" --claims-json claims.json
python3 "$HARNESS" validate --run-dir "$RUN_DIR" --attempt "$ATTEMPT_ID"
```

Validation is deterministic-first and renders desktop/mobile screenshots in a
pinned offline Chromium runtime. It also checks keyboard activation, no-JS core
content, reduced motion, internal links, local asset closure, source hashes, and
responsive geometry. Do not waive a failing check. Begin the next attempt and
repair only reported regions; preserve every prior attempt. Every repair must
capture the new `attempt_id` returned by `begin` into `ATTEMPT_ID` before any
stage, source-map, validate, review, or finalize command. After resuming an
in-progress run, recover `ATTEMPT_ID` from the `active_attempt` returned by
`status` as shown above. Stop when the plan's bounded attempt budget is
exhausted.

## Review the rendered page

After deterministic validation passes:

```bash
python3 "$HARNESS" review-context --run-dir "$RUN_DIR" --attempt "$ATTEMPT_ID" > review-context.json
```

Ask a fresh vision-capable subagent to inspect both bound screenshots, the
source map, and the rubric. If subagents are unavailable, use a fresh host-VLM
pass and record that mode honestly. The reviewer must echo every binding from
the generated context. It must reject generic marketing language, decorative
interaction, hidden evidence, invented links, weak mobile hierarchy, and visual
AI slop.

```bash
python3 "$HARNESS" record-review --run-dir "$RUN_DIR" --attempt "$ATTEMPT_ID" --review-json review.json
python3 "$HARNESS" finalize --run-dir "$RUN_DIR" --attempt "$ATTEMPT_ID"
```

Finalize only a passing, hash-current review. Deliver `$RUN_DIR/final/`; never
present an attempt directory as final. A `needs_visual_review` delivery must be
labeled as such, never as verified.
