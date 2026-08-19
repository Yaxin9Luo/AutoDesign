# Fresh vision review contract

Review the MP4, narration WAV, contact sheet, and all six individual
representative frames from the exact deterministic-passed attempt. Listen to
the narration and inspect every frame. Also read every hash-bound path returned
under `review_materials`: the normalized source text, evidence JSONL, and
attempt source map. Use those materials to check what each title, visible
number, narrated claim, and source visual actually says. The reviewer must be a fresh
vision- and audio-capable host context or fresh subagent that did not author the video.
HTML, source code, filenames, and the author's own description are not visual
or audio evidence, but the bound source/evidence files are required semantic
evidence.

Score every dimension from 1 through 5:

1. `research_story_and_source_fidelity` — the film communicates the paper's
   actual question, contribution, method, evidence, limitations, and conclusion
   without invented claims.
2. `scene_composition_and_visual_hierarchy` — each sampled scene has a clear
   focal point, balanced composition, readable type, intentional density, and
   a clean white primary canvas that integrates white-background paper figures.
3. `figure_legibility_and_evidence_use` — source visuals remain interpretable,
   properly cropped, meaningfully annotated, and connected to narration.
4. `motion_continuity_and_seekability` — progression feels coherent and motion
   supports comprehension rather than decorating every element.
5. `narration_pacing_and_audio_quality` — spoken text is intelligible, natural,
   complete, and aligned with scene timing.
6. `subtitle_readability_and_optional_playback` — captions are accurate and
   readable when enabled, but the composition also works with them disabled.
7. `conference_readiness_and_low_ai_aesthetic` — the result feels like a
   deliberate research film, not a template, dashboard, or AI slideshow.

Any invented/unbound claim, unsafe asset, unreadable evidence, nondeterministic
motion, clipped narration, forced/burned-in-only subtitles, or invalid media
contract is a blocker. A transparent, tinted, dark, gradient, or image
composition/scene root is also a blocker, as is primary-root opacity, filtering,
masking, blending, clipping, interaction-driven canvas mutation, or an animated
project whose active scenes cannot be deterministically sought and checked. A
`data-no-timeline` marker is invalid when a player/timeline registry exists or
when a composition/scene root has an active CSS animation, transition, or Web
Animation; local descendant motion remains allowed.
This does not prohibit purposeful player chrome, subtitles, overlays, or
restrained light local panels. Localized repairs name scene IDs and concrete
changes.
For `verdict: "pass"`, every dimension must independently score at least 4/5;
an average score cannot compensate for a weaker dimension. Missing, Boolean,
NaN, infinite, or out-of-range scores are invalid.

Return this exact JSON shape, copying every hash from `review-context` without
alteration:

```json
{
  "format_version": 1,
  "attempt_id": "01",
  "review_context_sha256": "...",
  "artifact_hashes": {"artifact/conference-video.mp4": "..."},
  "preview_hashes": {"contact_sheet": "...", "frame_01": "..."},
  "reviewed_frame_ids": ["contact_sheet", "frame_01"],
  "source_manifest_sha256": "...",
  "source_map_sha256": "...",
  "rubric_sha256": "...",
  "reviewer_mode": "fresh_host_vlm",
  "dimension_scores": {
    "research_story_and_source_fidelity": 5,
    "scene_composition_and_visual_hierarchy": 5,
    "figure_legibility_and_evidence_use": 5,
    "motion_continuity_and_seekability": 5,
    "narration_pacing_and_audio_quality": 5,
    "subtitle_readability_and_optional_playback": 5,
    "conference_readiness_and_low_ai_aesthetic": 5
  },
  "blockers": [],
  "localized_repairs": [],
  "verdict": "pass",
  "complete": true
}
```

`reviewed_frame_ids` must equal the complete sorted preview-ID set from the
context. A passing verdict requires no blockers. Use `needs_visual_review` only
when actual visual inspection was impossible, never as a substitute for a
partial review. Stale hashes, skipped frames, missing dimensions, a self-review,
or a review from another attempt are rejected. `record-review`, `resume`, and
`finalize` each revalidate the bound rubric hash, complete score vector, and
per-dimension passing threshold before advancing.
