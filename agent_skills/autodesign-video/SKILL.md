---
name: autodesign-video
description: Create a narrated, subtitled, source-grounded conference video from a research paper. Use when a user asks to turn a paper, preprint, or manuscript into a research presentation video for a conference or project release.
---

# AutoDesign Video

Create the video with the host coding agent. Use the bundled harness for
evidence, durable attempts, exact HyperFrames rendering, local Kokoro
narration, optional subtitles, media QA, and hash-bound delivery. This Skill
does not require the AutoDesign repository, server, or an API judge.

## Read first

Read [source-grounding.md](references/source-grounding.md),
[output-contract.md](references/output-contract.md), and
[review-rubric.md](references/review-rubric.md) completely. Resolve
`SKILL_ROOT` to this installed Skill folder. Put the run in a user-selected output directory outside the Skill. Never write output, caches, models, voices,
browsers, virtual environments, or `node_modules` inside the installed Skill.

## Execute

Run `python "$SKILL_ROOT/scripts/video_harness.py" --help` and
`python "$SKILL_ROOT/scripts/setup_video.py" --help` for exact flags.

1. Run `doctor`. If it reports a missing runtime, run `setup`. Setup requires
   Node 22+, npm, ffmpeg, ffprobe, and Python 3.10–3.12. It installs exact
   `hyperframes@0.7.86`, its rendering browser, `kokoro-onnx==0.5.0`, and
   `soundfile==0.14.0` in an atomic versioned user cache. It prefetches and
   SHA-256 verifies the exact Kokoro model and voice blobs. Never substitute a
   global or newer HyperFrames version. Setup does not modify the Skill or
   global packages; `setup_video.py remove` deletes only this version's locked
   cache and is safe to repeat.
2. Run `init RUN`, then `evidence RUN PAPER`. Pass user-provided content images
   with `--asset` and style references with `--reference`. Read
   `evidence/evidence.jsonl`, the rendered paper pages, and
   `evidence/source_visuals.json`. A fresh host VLM or subagent must inspect PDF
   visual candidates against their captions before `bind-visuals`. Uncertain
   visuals stay unused. References are style-only; never copy their content.
3. Author and save a complete `plan.json`, then run `plan RUN plan.json`.
   Default to 12 scenes and 360 seconds. Honor explicit user overrides only
   within 10–14 scenes and 300–600 seconds. Build one spoken research argument:
   question, gap, contribution, method, strongest evidence, analysis,
   limitations, implications, and closing. Every scene needs English
   narration, contiguous timing, evidence IDs, and any approved visual IDs.
4. Run `begin-attempt RUN`. Author a local-only editable HyperFrames project in
   a work folder for that attempt, not in its reserved `artifact/` directory.
   Follow the exact DOM and security contract in
   [output-contract.md](references/output-contract.md). Use native HTML/CSS/SVG
   text, real paper figures, deterministic seekable timelines, one composition
   root, literal `class="clip"` scenes, literal narration audio, and a working
   subtitle toggle. Do not add remote resources or runtime network behavior.
5. Run `validate PROJECT plan.json --run RUN`. This is structural validation;
   it intentionally does not require narration audio yet and never creates a
   placeholder. Fix every reported source, timing, local-asset, or HTML error.
6. Write a claims JSON list whose visible claims cite actual evidence IDs. Run
   `deliver PROJECT plan.json --run RUN --attempt ID --claims claims.json`.
   The harness enforces this order: structural validation; per-scene
   HyperFrames/Kokoro TTS and timed WAV mix; transcript/SRT/VTT and metadata;
   full real HyperFrames lint; strict real HyperFrames render; subtitle mux;
   exact ffprobe; representative frames and contact sheet. ffmpeg may mix audio,
   mux subtitles, and extract frames; it must never replace HyperFrames as the
   final renderer. A stale or invalid MP4 is deleted and cannot pass.
7. Run `review-context RUN ID`. Give the exact MP4, narration WAV, contact sheet,
   and all six individual frames to a fresh vision- and audio-capable host or subagent that
   did not author the project. The reviewer must inspect every frame, listen to
   the narration, and return the exact
   hash-bound schema in [review-rubric.md](references/review-rubric.md). Record
   it with `record-review`; never infer visual quality from HTML or self-certify.
8. If an authoring gate fails, start the next bounded attempt and repair the
   localized findings. If setup, TTS execution, ffmpeg, ffprobe, or another
   deterministic runtime stage fails, repair the runtime and resume the same
   attempt; do not spend an authoring attempt on it. On a passing review, run
   `finalize RUN ID` and deliver the complete `final/` directory together with
   its run-level QA record.

Run `resume RUN` before every continuation after interruption. It verifies the
installed Skill snapshot, source, attempt artifacts, previews, reviews, and
final delivery hashes. A scaffold, silent video, burned-in-only subtitle,
plain-ffmpeg slideshow, stale render, failed lint/probe, partial review, or
fallback file is not success.

## Design standard

Make a conference film, not a narrated slide dump. Use editorial scene
composition, restrained academic typography, purposeful motion, direct visual
continuity, readable source figures, and evidence-led narration. Keep captions
optional and default them off in the authored player. Avoid repeated cards,
gratuitous gradients, decorative stock media, fake logos, tiny figures, dense
paper paragraphs, constant motion, and generic AI-marketing language.
