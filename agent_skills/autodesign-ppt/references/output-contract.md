# PPT output contract

Follow this contract literally. The host authors content; the harness validates,
renders, exports, binds review evidence, and finalizes.

## Plan and narrative

Paper decks default to exactly 18 slides unless the user explicitly requests a
different count from 1 through 60. The default arc is:

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
- CSS that fixes every slide to 1920x1080 with no overflow or clipping;
- ArrowLeft and ArrowRight keyboard navigation plus stable `#slide-01` hash
  navigation. Navigation controls may stay invisible in the rendered slide;
- only local regular-file dependencies. No remote URL, hotlink, iframe, web
  font, network fetch, data URL, event-handler attribute, author-written script,
  or symlink. The sole script is the audited navigation snippet below.

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

Each slide needs native editable text and at least one `data-claim-id` bound to
real `ev-*` source IDs. Each source image needs its visual source ID. Keep table
cells native; never use a screenshot of a table, equation label, or final text.
A near-full-slide image without editable overlays is rejected.

The exporter creates a text-free background from the canonical browser render
only to preserve CSS decoration. It then lays native PowerPoint text, tables,
images, and shapes over that background. The harness reopens the result and
checks slide size, notes, object types, counts, and OOXML integrity.

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

The reviewed artifact set contains `deck.html`, `deck.pdf`, `deck.pptx`,
`notes.json`, and local artifact dependencies. Browser QA must pass separately
for every slide and the contact sheet. The PDF page count must equal the plan.
The PPTX must reopen at 13.333333x7.5 inches with every slide, note, editable
text layer, table, image, and shape present. When a local office renderer exists,
all pages must render and remain acceptably similar to canonical HTML.

Finalization adds the exact reviewed source map and a hash manifest. It refuses
stale artifacts, stale previews, partial reviews, and unreviewed frames.
