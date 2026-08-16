---
name: autodesign-poster
description: Use when a user asks to turn a paper, preprint, or manuscript into an editable, source-grounded academic conference poster, including PDF export or style-reference adaptation.
---

# AutoDesign Poster

Create the poster with the host coding agent. Use the bundled harness for
evidence, state, deterministic QA, rendering, and delivery; do not substitute a
fallback file for a failed run.

## Read first

Read [source-grounding.md](references/source-grounding.md),
[output-contract.md](references/output-contract.md), and
[review-rubric.md](references/review-rubric.md) completely. Resolve `SKILL_ROOT`
to this installed Skill folder. Put run state and generated files in a
user-selected output directory. Never write generated artifacts, caches, or run
state inside this Skill directory.

## Execute

Run `python "$SKILL_ROOT/scripts/poster_harness.py" --help` for exact flags.

1. Run `doctor --install-browser`, then `init --run-dir "$RUN" --source "$PAPER"`.
   Add user-supplied content images with `--asset`; add poster references with
   `--reference`. References are style-only: never copy their text, claims,
   logos, QR codes, links, figures, tables, or assets.
2. Query `evidence` repeatedly for identity, problem, method, evidence,
   limitations, and conclusions. For PDF visuals, inspect rendered source pages
   and candidate crops with the host VLM (or a fresh subagent), then submit a
   hash-bound authorization using `bind-visuals`. Do not authorize an uncertain
   visual.
3. Write `plan.json` from the output contract and run `plan`. Honor an explicit
   user size. Otherwise use the 3072×1536 CVPR 84×42-inch default. Plan a dense
   problem → method → evidence → takeaway story before styling.
4. Run `begin-attempt`. Read its `authoring-context.json`; author the requested
   `poster.html` yourself using only staged content visuals and grounded text.
   Preserve native HTML text/tables and SVG text. Keep the header to title,
   authors, and institutions only.
5. Write the exact claim/source map, then run `validate`. Static gates run before
   the pinned, network-denied browser. Continue only when HTML, geometry,
   dependency closure, preview, and the exactly-one-page PDF all pass.
6. Run `review-context`. Give its preview and contract to a fresh host-VLM or
   fresh subagent that did not author the attempt. Record its complete,
   hash-bound review with `record-review`; never self-certify semantic quality.
7. On failure, use only localized reviewer findings, run `begin-attempt`, and
   repair within the plan's bounded attempt budget. On pass, run `finalize`.
   Deliver only `final/poster.html`, `final/poster.pdf`, `final/preview.png`, and
   `final/provenance/source-map.json`.

Run `resume --run-dir "$RUN"` after interruption or before every continuation;
it verifies the installed `skill_root`, hashes, and next safe action. A blocked,
failed, stale, or visually unreviewed attempt is not a finished poster.
