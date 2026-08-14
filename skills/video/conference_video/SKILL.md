# Conference Paper Video

Use for a narrated conference video derived from an academic paper. The source is an editable HyperFrames HTML timeline; the MP4 is a validated delivery, not the authoring source.

## Stage: enhance

Expand the paper into a 10-14 scene English narrative lasting 300-600 seconds. Choose the target duration from the paper's complexity, evidence density, and explanation needs; preserve an already-selected target during repair or resume. Preserve the paper's claims, terminology, figures, tables, and provenance.

## Stage: plan

Create ordered `html_artifact` scene frames with explicit durations and English `speaker_notes`. Use 1920x1080 at 30 fps. Select `male` or `female`; the runtime records the corresponding deterministic Kokoro voice id. Read `delivery_contract` before authoring. Write local-only HyperFrames HTML with native text and local source figures. Use exactly one `data-composition-id` root; static scenes must set `data-no-timeline`, while animated work must register a seekable `window.__timelines` entry. Every timed scene and narration audio element must have literal `class="clip"`; `data-hf-clip` alone is invalid. Do not use `requestAnimationFrame`, remote scripts, styles, fonts, images, media, iframes, data URLs, or network APIs. Keep all animation deterministic and seekable.

Keep all 10-14 scene sections directly in `project/index.html`; do not move them into `data-composition-src` files to address file-size or track-density warnings. Those lint warnings are advisory, while the ordered root scenes are part of the strict delivery contract. Use renderer-supported system fonts, or provide a local `@font-face` source for every custom font.

## Stage: critique

Reject placeholders, external assets, missing narration, fewer than 10 or more than 14 scenes, timelines outside 300-600 seconds, lint errors, render errors, stale MP4 files, and incomplete media probes.

## Stage: repair

Repair every blocking contract failure without restructuring valid root scenes in response to advisory lint warnings. Then require `index.html`, English transcript, English SRT and VTT, Kokoro voice metadata, a fresh MP4, ffprobe evidence, and `delivery_manifest.json`. Whisper may compare rendered speech with the canonical transcript as optional QA, but delivery cannot depend on Whisper.
