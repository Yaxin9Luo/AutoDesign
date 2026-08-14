# General HTML-First Deck

Use as the visual substrate for new HTML-first decks, including paper and report decks. Paper provenance and report structure remain owned by their higher-priority companion skills. Existing-presentation beautification stays isolated in `deck.ppt_beautify`. Author editable `html_artifact` slide frames; `deck.html` remains the editable export source.

## Stage: enhance

Classify audience, scenario, talk length, tone, and primary source type before expanding the brief. Preserve scenario obligations: a pitch needs a clear ask, a technical deck needs architecture or flow, and a weekly update needs metrics and risks. Presenter, talk, and workshop requests require speaker notes on every substantive slide. Keep the visual direction provisional until planning.

## Stage: plan

Build a varied slide arc with slots, grouped blocks, stable ids, and source-aware imagery. Read `layout`, `scenario`, `theme`, and `image-policy` as needed. Read `academic_deck_taste_v1` before choosing the theme and visual rhythm for an academic or paper deck. Academic decks default to exactly 18 slides; an explicit user count, range, or request not to use a fixed count overrides that default. A full formal academic talk uses 20-26 slides.

Keep authoring instructions out of visible slide blocks. Put narrative intent, delivery cues, and presenter guidance only in `frame.speaker_notes`; never render labels such as `Intent:`, `Design note:`, or `Speaker note:`. Every metric or takeaway card must contain a meaningful metric/claim and local context, not an ordinal placeholder such as `1`, `2`, or `3` alone.

For academic decks, use role-aware density: 30-65 words for outline/checkpoints, 45-100 for problem/context, 55-140 for method/algorithm, 45-110 for results/analysis, and 20-60 for closing. The deterministic hard floor remains 30 words for substantive slides. Treat original source visuals, native HTML tables, source-verifiable equations, and editable mechanism diagrams as visual evidence units. Keep text, tables, formula labels, diagram labels, and callouts as native HTML or SVG text.

Use a formal academic visual system: white or near-white canvas, near-black ink, thin neutral rules, one restrained accent, serif main hierarchy, and sans-serif small labels. Preserve supplied palette metadata but use it as an accent source, not as a grid of colored surfaces. Prefer flat editorial compositions and large evidence areas. Reject gradients, dark defaults, visible playback controls, dashboard card grids, nested panels, decorative chrome, and oversized sparse slogans.

Keyboard and hash navigation are required, but controls stay visually absent. Put speaker guidance in `data-speaker-notes` with `[Sources]` and `[Talk]`; never display it on the slide. A paginated deck may show one active slide at a time as long as every slide remains a complete 1920x1080 logical frame for audit and export.

## Stage: critique

Check scenario coverage, layout rhythm, per-slide word density, visual-unit slide coverage, editable structure, and whether source evidence was displaced by decorative imagery. Speaker notes must be spoken prompts, not essays or visible slide copy. A text-only deck is acceptable only when the user explicitly requests it.

## Stage: repair

Change storyboard and slots before color or type polish; split cramped slides instead of shrinking text. Restore missing visual evidence units with editable tables, equations, or mechanism diagrams when source figures alone cannot cover the planned slide count. Read `layout` and `agent-flow`. For academic deck repairs, read `academic_deck_taste_v1` when the output drifts toward dark defaults, text walls, sparse unrelated imagery, or generic framework-demo styling.
