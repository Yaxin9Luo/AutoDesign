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

Resolve a Python 3 launcher first: try `python3`, then `python`; on Windows also
try `py -3`. Use the first command whose `--version` reports Python 3 in place
of `<PYTHON3>` below. Run
`<PYTHON3> "$SKILL_ROOT/scripts/ppt_harness.py" --help` for exact flags.

1. Run `doctor`, then `init --run-dir "$RUN" --source "$PAPER"`. The first
   online run installs artifact-hash-locked browser and native-PPT dependencies
   in a versioned, content-verified user cache; later runs can reuse them
   offline. A missing or changed runtime is blocked, not a reason to skip QA.
2. Use `evidence --query "..."`, `visuals`, and rendered paper pages. Query
   evidence for identity, problem, gap, contributions, method, results, and
   conclusions. Query ablations, robustness, qualitative evidence, and
   limitations only when the paper actually reports them. A fresh host VLM or
   subagent must inspect PDF visual candidates
   against their captions before using them; bind the complete hash-matched JSON
   with `bind-visuals`. Uncertain candidates stay unused.
3. After the evidence queries, write a complete role/evidence story-plan JSON
   and run `plan --brief "..." --story-plan "$STORY_PLAN"`. It has exactly one
   entry per slide with `slide_id`, a role valid for that narrative slot, and
   one or more real `evidence_refs`; the harness validates role-to-evidence
   compatibility before the immutable hash is written. The problem -> method ->
   evidence -> takeaway backbone stays ordered. Conditional experimental slots
   without source support must use an allowed same-phase source-backed role such
   as method detail, results deep dive, evidence analysis, case analysis, or
   scope and boundaries; never invent a robustness, ablation, qualitative, or
   limitations section just to fill the arc. If the story plan is omitted,
   deterministic semantic role scoring selects evidence and performs the same
   evidence-conditioned substitutions; it never assigns by extraction order.
   Ablation requires explicit ablation terminology, or a component/variant
   comparison with a measured effect; bare words such as `without` or `variant`
   do not qualify. Robustness and qualitative roles likewise require explicit
   terminology or a condition/case plus an observed result. A two-value numeric
   comparison can provide the observed result when the component operation or
   shifted condition is explicit. Reject negated, not-provided, or future-work
   statements before evaluating positive signals: stating that an experiment is
   absent is not evidence that it was performed. That fallback needs
   role-distinctive evidence with a unique winning margin; ambiguous or generic
   overlap stops and requests `--story-plan`. Paper decks default to exactly 18
   slides. A count attached to the requested deck/slides/presentation, including
   Chinese numerals through sixty, overrides the default; source metadata such
   as “12-page manuscript”, “25-page article”, or “30-page PDF” does not. Build
   one research argument, not disconnected summary cards. Pass an explicit
   visual-allocation JSON to `plan`; keep each visual within its permitted role
   and reuse limit.
4. Run `begin`. Author the returned `artifact/deck.html` yourself. The HTML is
   canonical and the immutable plan is snapshotted under artifact provenance.
   Match every slide's ordered role, section, assertion, and evidence refs to
   that plan: the visible `h1` must equal the planned assertion, every visible
   text/table source list must equal the planned evidence refs, and the speaker
   note must equal the planned note intent. Stage each planned figure with
   `stage-visual`. Use the exact
   tagged-DOM contract so native text, tables, images,
   shapes, and speaker notes can become editable PowerPoint objects. Use only
   local, regular, non-linked source-grounded paper assets; never invent paper
   facts, links, logos, figures, or measurements. Bind every visible text box,
   native table, and speaker-note statement to real evidence IDs.
5. Run `validate`. Static gates run before rendering. The harness then audits
   every slide in a network-denied browser, verifies that the authored slide
   roots actually compute to 1920x1080 before any audit isolation CSS, makes a
   contact sheet,
   verifies an exact-page-count PDF, exports `deck.pptx`, reopens its required
   native text/table/image counts, native rect/ellipse/line counts and types,
   and exact notes, and, when LibreOffice is
   available, renders and compares
   the PowerPoint with the canonical HTML. A screenshot-only deck does not pass.
6. Run `review-context`. Give the contact sheet and every individual preview to
   a fresh host VLM or fresh subagent that did not author the attempt. The
   reviewer must inspect every slide, cite localized repairs, and return the
   exact hash-bound schema in [review-rubric.md](references/review-rubric.md).
   Record it with `record-review`; never self-certify from source code.
7. If review fails, run `begin` again and repair only the reported slides. The
   harness allows at most three attempts. If it passes, run `finalize`. Deliver
   only the allowlisted, verified `final/deck.html`, `final/deck.pptx`,
   `final/deck.pdf`, `final/notes.json`, local assets, and provenance. Extra,
   symlinked, hardlinked, or unreviewed files are never delivered.

Run `resume --run-dir "$RUN"` before every continuation after interruption. It
verifies the installed `skill_root`, snapshotted instructions, source, artifact,
preview, review hashes, the minimum score for persisted passing reviews, and
single-link final files. A scaffold, fallback, stale review, failed export,
or visually unreviewed deck is not a completed deliverable.

## Design standard

Use a formal academic light theme with a restrained palette, serif display
hierarchy, sans-serif labels, generous but purposeful whitespace, and native
evidence. Prefer diagrams, plots, tables, equations, and paper figures over
decorative stock imagery. Reject dark dashboards, repeated card grids,
gradients, sparse slogan slides, oversized titles, tiny captions, and generic
AI-marketing language. Keep references style-only; never copy their content.
