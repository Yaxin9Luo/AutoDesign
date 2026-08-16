# PPT output contract

Follow this contract literally. The host authors content; the harness validates,
renders, exports, binds review evidence, and finalizes.

## Plan and narrative

Paper decks default to exactly 18 slides unless the user explicitly requests a
different deck or presentation count from 1 through 60. Arabic and common
Chinese numerals are accepted. The count must be syntactically attached to the
requested deck, slides, presentation, or a direct resize command such as “Make
it 14 pages.” Source metadata such as “12-page manuscript”, “25-page article”,
or “30-page PDF” is not a deck count. The default arc is:

1. cover;
2. roadmap;
3. problem;
4. motivation;
5. prior-work gap;
6. contributions;
7. method overview;
8. core mechanism;
9. algorithm, objective, or architecture;
10. experimental setup;
11. primary results;
12. robustness;
13. ablation;
14. qualitative evidence;
15. limitations;
16. implications;
17. takeaways;
18. closing and discussion.

Combine adjacent roles when the user asks for fewer slides. Add evidence or
mechanism deep dives before takeaways when the user asks for more. Do not pad
with section dividers or split one sentence across multiple slides.

After querying evidence, the host should pass `plan --story-plan PATH` with a
version-1 JSON object containing exactly `format_version` and `slides`. `slides`
contains exactly one object per requested slide, in order, with only:

- `slide_id`: contiguous `slide-01` through `slide-N`;
- `role`: the corresponding role in the academic arc;
- `evidence_refs`: a non-empty, unique list of real evidence IDs.

The harness rejects unknown evidence, wrong roles, wrong order, and count
mismatches before hashing the immutable plan. If no host story plan is passed,
the deterministic fallback scores role-distinctive evidence concepts for each
slide. A match must clear the minimum and beat the runner-up by a fixed margin;
generic words such as “method” or “result” cannot stand in for missing ablation
evidence. It does not rotate through extraction order. If a role has no unique,
distinctive match, planning stops and requests an explicit story plan.

If source visuals are needed, write a JSON list and pass it to
`plan --visual-allocations`:

```json
[
  {"visual_id": "visual-explicit-001", "role": "method", "slide_id": "slide-08"}
]
```

The visual must be eligible for that role, stay within its catalog reuse limit,
and appear on the allocated slide. Run `stage-visual` after `begin`; do not copy
an unapproved evidence file into the artifact by hand.

`plan` writes an immutable hash binding. `begin` snapshots its exact bytes to
`artifact/provenance/plan.json`. Every authored slide must preserve the plan's
ordered ID, index, role, chapter, assertion title, and evidence refs. Validation
and review bind that snapshot; repairs use a new attempt rather than rewriting
it.

The visible `h1` text equals the planned assertion title. Source-derived title
anchors stop at sentence punctuation and are bounded by words and grapheme
clusters, so an unspaced CJK paragraph cannot become a giant heading. Emoji
modifiers, ZWJ sequences, and regional-indicator flag pairs stay intact at the
boundary. Every visible native text element and table cites exactly the slide's
planned evidence refs, in plan order. Speaker notes equal the plan's complete
note intent, not merely its
`[Sources]` prefix. These native claims and notes are the canonical source-map
inputs; changing their text or evidence IDs without a new plan fails validation.

## Canonical HTML

Write `artifact/deck.html` with:

- `<!doctype html>` and exactly one
  `<main data-autodesign-artifact-root="deck">`;
- root `data-slide-count`, `data-width="1920"`, and `data-height="1080"`;
- exactly the planned number of
  `<section class="deck-slide">` elements, identified uniquely and contiguously
  as `slide-01` through `slide-N` in both `id` and `data-slide-id`;
- per-slide `data-slide-index`, `data-slide-role`, `data-section`,
  `data-assertion-title`, `data-source-ids`, `data-width="1920"`,
  `data-height="1080"`, and `data-speaker-notes`;
- notes in the exact shape
  `[Sources] ev-001, ev-002 [Talk] What the presenter should explain.`;
- CSS that fixes every slide to 1920x1080 with no overflow or clipping. The
  browser checks the actual computed width, height, and layout box of every
  authored slide root before slide-isolation CSS is applied; a dummy rule or
  wrapper with the right dimensions cannot satisfy this gate;
- ArrowLeft and ArrowRight keyboard navigation plus stable `#slide-01` hash
  navigation. Navigation controls may stay invisible in the rendered slide;
- only local regular-file dependencies. No remote URL, hotlink, iframe, web
  font, network fetch, data URL, event-handler attribute, author-written script,
  symlink, hardlink, CSS-generated text, or hidden URL-bearing attribute. The
  sole script is the audited navigation snippet below.

Use this exact navigation element; any other script is rejected:

```html
<script data-autodesign-navigation>(()=>{const s=[...document.querySelectorAll('.deck-slide')];const i=()=>Math.max(0,s.findIndex(x=>'#'+x.id===location.hash));const g=n=>{const x=s[Math.min(s.length-1,Math.max(0,n))];if(x){location.hash=x.id;x.scrollIntoView({block:'start'})}};addEventListener('keydown',e=>{if(e.key==='ArrowLeft'){e.preventDefault();g(i()-1)}else if(e.key==='ArrowRight'){e.preventDefault();g(i()+1)}});addEventListener('hashchange',()=>{const x=s[i()];if(x)x.scrollIntoView({block:'start'})})})();</script>
```

Use assertion-led titles that state the slide's point. Cover and roadmap titles
may be neutral; evidence slides must not use labels such as "Results" as the
entire title. Use the paper's exact title, authors, and affiliations only when
the source contains them.

## Native editable PowerPoint tags

Every visible piece of text and every delivery-critical table, figure, or shape
must carry one of these explicit tags:

```html
<h1 data-pptx-kind="text"
    data-pptx-x="120" data-pptx-y="80"
    data-pptx-w="1680" data-pptx-h="140"
    data-font-size="54" data-font-family="Palatino Linotype"
    data-color="#171717" data-bold="true"
    data-claim-id="claim-07-title"
    data-source-ids="ev-021">An assertion title</h1>

<img data-pptx-kind="image"
     data-pptx-x="980" data-pptx-y="250"
     data-pptx-w="760" data-pptx-h="560"
     data-source-ids="visual-explicit-001"
     src="assets/method.png" alt="Source method diagram">

<table data-pptx-kind="table"
       data-pptx-x="920" data-pptx-y="260"
       data-pptx-w="820" data-pptx-h="500"
       data-font-size="20" data-source-ids="ev-044">
  <tr><th>Method</th><th>Score</th></tr>
  <tr><td>Baseline</td><td>72.1</td></tr>
</table>

<div data-pptx-kind="shape" data-shape="rect"
     data-pptx-x="120" data-pptx-y="490"
     data-pptx-w="760" data-pptx-h="10"
     data-fill="#6B3FA0"></div>
```

Coordinates are in the 1920x1080 canonical pixel space. Width and height must
be positive and stay inside the canvas. Supported kinds are `text`, `image`,
`table`, and `shape`; shapes support rectangle, ellipse, and line. Text supports
font family, font size, color, fill, bold, italic, horizontal alignment, and
vertical alignment. All visible text must be tagged; untagged slide text would be
rasterized into the decorative background and is therefore a hard failure.

Every visible native text element and table needs real `ev-*` source IDs; each
slide also needs at least one stable `data-claim-id`. Speaker-note `[Sources]`
must exactly match the slide evidence refs and the `[Talk]` statement is included
in the source map. Each source image needs its visual source ID. Keep table cells
native; never use a screenshot of a table, equation label, or final text. A
near-full-slide image without editable overlays is rejected.

The exporter creates a text-free background from the canonical browser render
only to preserve CSS decoration. It then lays native PowerPoint text, tables,
images, and shapes over that background. The harness reopens the result and
checks slide size, exact notes, per-slide native text/table/image counts, exact
native rect/ellipse/line counts and types, and OOXML integrity against a
contract derived from the plan-bound canonical DOM. Deleting a required native
object, including a decorative native rectangle, fails reopen validation even
if the text-free background screenshot still looks correct.

## Visual and content rules

- Treat reference images as style-only. Never copy reference text, figures,
  tables, logos, QR codes, claims, or links.
- Use PDF-extracted visuals only after a fresh host VLM binds the exact visual
  hash to caption evidence. Obey eligibility, content role, and reuse limits.
- Use 28-34 px body text and 44-64 px assertion titles as working targets; 14 px
  is the deterministic lower boundary, not a design target.
- Prefer one primary composition per slide. Do not repeat the same title-slide
  layout or identical card grid across the deck.
- Preserve scale labels, legends, axes, table headers, units, and uncertainty.
- Do not claim causal, comparative, or numeric findings beyond cited evidence.

## Deterministic delivery

The reviewed artifact set contains only `deck.html`, `deck.pdf`, `deck.pptx`,
`notes.json`, `provenance/plan.json`, and the exact local dependency closure.
Unexpected files and directories, symlinks, hardlinks, and non-regular files are
rejected. Browser QA must pass separately for every slide and the contact sheet.
The PDF page count must equal the plan.
The PPTX must reopen at 13.333333x7.5 inches with every slide, note, editable
text layer, table, image, and shape present. When a local office renderer exists,
all pages must rasterize and pass both pixel-similarity and foreground-edge
recall against canonical HTML. A page-count-only or blank render cannot pass.

Finalization adds the exact reviewed source map and a hash manifest. It refuses
stale artifacts, stale previews, partial reviews, and unreviewed frames. Every
final file, including the manifest, must retain link count one; resume rejects
an external hardlink created after finalization. A persisted `pass` review is
rechecked against the bound minimum score of 4 on every resume and finalize.

The pinned PowerPoint runtime is installed without bytecode and run with Python
bytecode writes disabled. Any `.pyc`, `.pyo`, or `__pycache__` appearing in the
runtime cache invalidates it rather than being ignored by the content hash.
