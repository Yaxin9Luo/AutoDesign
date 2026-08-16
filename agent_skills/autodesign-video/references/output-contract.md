# Conference-video output contract

## Plan

The canonical plan is one JSON object:

```json
{
  "format_version": 1,
  "artifact_type": "video",
  "width": 1920,
  "height": 1080,
  "fps": 30,
  "scene_count": 12,
  "duration_s": 360,
  "voice_id": "af_heart",
  "language": "en",
  "max_attempts": 4,
  "scenes": [
    {
      "scene_id": "scene_01",
      "title": "The research question",
      "role": "opening",
      "start_s": 0,
      "duration_s": 24,
      "narration": "Complete English narration for this scene.",
      "source_ids": ["ev-001"],
      "visual_ids": [],
      "visual_role": "overview",
      "title_claim_id": "claim-scene-01-title",
      "narration_claim_id": "claim-scene-01-narration",
      "visible_claim_ids": ["claim-scene-01-title"]
    }
  ]
}
```

Scenes are unique and contiguous from zero. Their durations sum exactly to the
video duration. Default to 12 scenes and 360 seconds. Only an explicit user
request may select 10–14 scenes or 300–600 seconds. The canvas and frame rate
are always 1920×1080 at 30 fps. Narration is English and grounded; it is not
slide copy read aloud. The separately supplied claims list is mandatory and
non-empty. Every scene title and narration equals its named claim text exactly,
the two claims cite exactly that scene's source IDs, and every visible numeric
fact appears in a named `visible_claim_ids` claim. The saved `RUN/plan.json` is
canonical: validation and delivery use those exact bytes, not a reserialized
copy.

## Editable HyperFrames project

The project contains `index.html`, `hyperframes.json`, and local assets. The
JSON entry is `index.html`. HTML has exactly one root with
`data-composition-id`, `data-start="0"`, `data-duration`, `data-width="1920"`,
and `data-height="1080"`. A static project declares `data-no-timeline`; an
animated project registers one deterministic seekable `window.__timelines`
entry. Never use `requestAnimationFrame` or wall-clock state.

Every planned scene is a direct `<section id="scene_XX" class="clip">` with
exact `data-start`, `data-duration`, `data-source-ids`, `data-narration`,
`data-title-claim-id`, `data-narration-claim-id`, and `data-claim-ids`.
The visible heading carries the exact `data-claim-id`. `data-hf-clip` is
optional metadata and never replaces the literal `clip` class. Source images
are regular local files with `data-source-id` matching the authorized visual
catalog and its SHA-256. The actual image `data-source-id` set inside each scene
equals that scene's canonical `visual_ids` exactly; images may not be omitted,
moved to another scene, or added without a plan binding. Scene allocations
retain each visual's eligibility, allowed content roles, and reuse limit, and
pass the shared visual-plan validator. Preserve native text, SVG text, tables,
and equations.

Include exactly one narration element before delivery:

```html
<audio class="clip" src="assets/narration.wav"
       data-start="0" data-duration="360"
       data-track-index="2" data-media-start="0"></audio>
```

Include an accessible `data-subtitle-toggle` control with `aria-pressed` and a
local subtitle overlay. The overlay has
`data-subtitle-source="narration/subtitles.en.vtt"`, and its local
`data-subtitle-cue` elements exactly match the generated VTT cues. Captions are
selectable in the MP4 and toggleable in the editable project. They must not be
burned in or forced. Default the HTML overlay off so the video remains
intentionally composed without captions.

Reject duplicate attributes, every inline `on*` event handler, meta refresh,
remote URLs, protocol-relative URLs, data/blob/javascript URLs, remote fonts,
scripts, styles, media, iframes, executable downloads, dynamic `new Image`,
`fetch`, XHR, WebSocket, EventSource, `sendBeacon`, dynamic imports, and CSS
URLs that are absolute, encoded escapes, or traverse `..`. Every project path
stays inside the project and is neither a symlink nor hard link. Rendering runs
without provider credentials. Before TTS or any other generated output is
written, the complete existing project tree and pipeline-owned `narration`,
`assets`, `renders`, and `frames` paths are checked for linked ancestors,
linked targets, and containment escapes. A real Chromium preflight blocks non-project
requests and enumerates every visible enabled native or ARIA control, including
buttons, supported inputs, selects, textareas, summaries, anchors, and custom
roles. It operates each one and records a unique identity, operation, and
result. After initial load, each operation, and the complete sequence, it waits
at least 500 ms and fails closed on pending timers or late requests,
navigations, popups, and page errors. It clicks the subtitle control twice,
proving `aria-pressed`, computed `display`/`visibility`, effective opacity
through all ancestors, clipped viewport intersection, and nonzero painted
bounds transition off → on → off against cues from the locally generated VTT.

## Non-negotiable delivery order

1. Deterministic structural HTML, timeline, source, and local-path validation.
   This stage allows the pipeline-owned `assets/narration.wav` to be absent. It
   never runs full media lint and never creates placeholder audio.
2. Exact HyperFrames 0.7.86 invokes local Kokoro per scene. Measure every WAV;
   conservatively refit at no more than 1.30× or route overlong narration to an
   authoring repair. Mix measured speech at planned scene starts into one
   full-duration 24 kHz mono PCM narration WAV.
3. Write the English transcript, SRT, VTT, per-scene timing, voice ID, speed,
   engine, and optional-caption metadata.
4. Launch the exact installed HyperFrames browser in strict offline mode. Reject
   network attempts or page errors; click the subtitle toggle twice and bind
   every overlay cue to the generated VTT hash.
5. Run the real complete `hyperframes lint` only after the referenced narration
   WAV exists and is hash-bound.
6. Run exactly `hyperframes render --fps 30 --resolution landscape --strict
   --no-best-effort --output <fresh-path> .`. Only that fresh, nonempty result
   may become the delivery video.
7. Mux the SRT as non-forced English `mov_text` without re-rendering video or
   audio. ffprobe then requires H.264/yuv420p video, AAC audio, 1920×1080,
   exactly 30 fps, planned duration, and the selectable English subtitle track.
8. Extract six spread-out representative frames and one 3×2 contact sheet.
   Bind every hash into deterministic QA and a fresh host-VLM review.

Nonzero TTS/probe execution, missing tools, corrupt caches, and timeouts are
runtime failures: repair setup and resume the same attempt. Invalid authored
HTML, full-lint findings, render-content failures, overlong narration, and
media-contract failures are authoring repairs. Every lint/render failure first
reruns the browser/runtime doctor; a corrupt browser or runtime routes to
same-attempt runtime recovery. Never resend a deterministic setup failure to
the authoring model. Never accept an older MP4 after a failed render. Runtime
diagnostics persist in the active attempt until a successful delivery clears
them; they are never published as final artifacts.

## Required delivery closure

The selected attempt retains and hash-binds:

- editable `index.html`, `hyperframes.json`, and every local asset;
- `conference-video.mp4` produced from the fresh HyperFrames render;
- `assets/narration.wav` and per-scene narration text/WAV files;
- `narration/transcript.en.txt`, `subtitles.en.srt`, `subtitles.en.vtt`,
  `timing.json`, and `voice-and-subtitles.json`;
- `media_probe.json`, six representative frames, `contact-sheet.png`,
  `video-source-map.json`, and `delivery-report.json`;
- attempt-level deterministic report, semantic review/context, provenance
  source map, and final exact-set `delivery-manifest.json` in the run state.

Finalization is forbidden unless every delivered byte, preview, source map,
review context, and reviewer verdict still matches its recorded hash. Before
passing semantic review, all seven rubric dimensions must be present, finite,
and at least 4/5; record, resume, and finalize all revalidate that threshold and
the current rubric binding. Before publishing, the project must equal the
plan-derived allowlist exactly. Hidden
files, `.env`, logs, render scratch files, and unreferenced assets are rejected,
not copied. Published reports contain only stable project-relative paths, never
machine-local absolute paths. Allowlisted files are copied into a same-parent
staging directory and atomically promoted only after the staged set is complete. Because portable
run creation pre-creates an empty `artifact/`, promotion first atomically moves
that placeholder to a same-parent empty backup, then renames the complete stage
into the now-absent destination; this works on Windows, where replacing an
existing directory is forbidden. Matching interrupted stages and empty backups
are recovered or cleaned without following links. A failed copy leaves the live
artifact empty and an exact retry remains idempotent.
