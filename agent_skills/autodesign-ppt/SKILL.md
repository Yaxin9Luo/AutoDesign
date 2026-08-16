---
name: autodesign-ppt
description: Use when a user asks to turn a paper, preprint, or manuscript into an editable, source-grounded conference slide deck, PowerPoint, research talk, seminar deck, or paper presentation.
---

# AutoDesign PPT

Create the deck with the host coding agent. Use the bundled harness for source
evidence, durable attempts, deterministic QA, editable PowerPoint export, and
truthful delivery. This Skill does not require AutoDesign, a server, or an API
judge.

## Read first

Read [source-grounding.md](references/source-grounding.md),
[output-contract.md](references/output-contract.md), and
[review-rubric.md](references/review-rubric.md) completely. Resolve `SKILL_ROOT`
to this installed Skill folder. Put the run in a user-selected output directory outside
the Skill. Never write output, caches, or run state into the installed package.

## Execute

Run `python "$SKILL_ROOT/scripts/ppt_harness.py" --help` for exact flags.

1. Run `doctor`, then `init --run-dir "$RUN" --source "$PAPER"`. The first
   online run installs exact-pinned browser and native-PPT dependencies in a
   versioned user cache; later runs can reuse them offline. A missing runtime is
   blocked, not a reason to skip QA.
2. Use `evidence --query "..."`, `visuals`, and rendered paper pages. Query
   evidence for identity, problem, gap, contributions,
   method, results, ablations, qualitative evidence, limitations, and
   conclusions. A fresh host VLM or subagent must inspect PDF visual candidates
   against their captions before using them; bind the complete hash-matched JSON
   with `bind-visuals`. Uncertain candidates stay unused.
3. Run `plan --brief "..."`. Paper decks default to exactly 18 slides. An
   explicit user slide count overrides the default in both standalone Deck and
   One-Paper-to-All use. Build one research argument, not 18 disconnected
   summary cards. Pass an explicit visual-allocation JSON to `plan`; keep each
   visual within its permitted role and reuse limit.
4. Run `begin`. Author the returned `artifact/deck.html` yourself. The HTML is
   canonical. Stage each planned figure with `stage-visual`. Use the exact
   tagged-DOM contract so native text, tables, images,
   shapes, and speaker notes can become editable PowerPoint objects. Use only
   local, source-grounded paper assets; never invent paper facts, links, logos,
   figures, or measurements.
5. Run `validate`. Static gates run before rendering. The harness then audits
   every 1920x1080 slide in a network-denied browser, makes a contact sheet,
   verifies an exact-page-count PDF, exports `deck.pptx`, reopens its native
   structure and notes, and, when LibreOffice is available, renders and compares
   the PowerPoint with the canonical HTML. A screenshot-only deck does not pass.
6. Run `review-context`. Give the contact sheet and every individual preview to
   a fresh host VLM or fresh subagent that did not author the attempt. The
   reviewer must inspect every slide, cite localized repairs, and return the
   exact hash-bound schema in [review-rubric.md](references/review-rubric.md).
   Record it with `record-review`; never self-certify from source code.
7. If review fails, run `begin` again and repair only the reported slides. The
   harness allows at most three attempts. If it passes, run `finalize`. Deliver
   only the verified `final/deck.html`, `final/deck.pptx`, `final/deck.pdf`,
   `final/notes.json`, local assets, and provenance.

Run `resume --run-dir "$RUN"` before every continuation after interruption. It
verifies the installed `skill_root`, snapshotted instructions, source, artifact,
preview, and review hashes. A scaffold, fallback, stale review, failed export,
or visually unreviewed deck is not a completed deliverable.

## Design standard

Use a formal academic light theme with a restrained palette, serif display
hierarchy, sans-serif labels, generous but purposeful whitespace, and native
evidence. Prefer diagrams, plots, tables, equations, and paper figures over
decorative stock imagery. Reject dark dashboards, repeated card grids,
gradients, sparse slogan slides, oversized titles, tiny captions, and generic
AI-marketing language. Keep references style-only; never copy their content.
