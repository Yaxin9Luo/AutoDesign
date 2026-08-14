"""Deliver a strict HTML-first HyperFrames conference video.

The tool scaffolds source assets, calls the single-turn composer, runs local
lint/render commands, and accepts only a fresh ffprobe-validated MP4 together
with canonical English narration and subtitle artifacts.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import wave
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any

from ._contract import ToolContext, obs_error, obs_ok
from ..attempt_candidates import (
    SecureRunMemberSnapshot,
    secure_run_member_access,
    update_video_delivery_pointer,
)
from ..agents.hyperframes_composer import (
    HyperFramesComposer,
    authored_video_local_asset_paths,
    load_composer_system_prompt,
    validate_authored_video_html,
)
from ..schema import (
    CompositionArtifacts,
    KOKORO_VOICE_BY_PRESET,
    ToolResultRecord,
    VideoDeliveryContract,
    VideoMediaProbe,
)
from ..process_supervision import (
    ProcessLedger,
    process_identity,
    spawn_registered_process,
    terminate_process_identities,
)
from ..run_control import CancellationToken, RunCancelled
from ..util.io import atomic_write_json, sha256_file
from ..util.design_spec_fingerprint import design_spec_sha256
from ..util.logging import log


_REPO_ROOT = Path(__file__).resolve().parents[2]
_HYPERFRAMES_BIN = (
    _REPO_ROOT / "runtime" / "video" / "node_modules" / ".bin" / "hyperframes"
)
_DEVELOPMENT_HYPERFRAMES_BIN = (
    _REPO_ROOT / "web" / "node_modules" / ".bin" / "hyperframes"
)
_KOKORO_CACHE_ASSETS = {
    "models/kokoro-v1.0.onnx": "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
    "voices/voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}
_MINIMUM_SPOKEN_WPM = 90
_MINIMUM_SPEECH_COVERAGE_RATIO = 0.72
_SUBTITLE_MAX_LINE_CHARS = 42
_SUBTITLE_MAX_CUE_DURATION_S = 7.0
_SUBTITLE_MAX_READING_CHARS_PER_S = 20.0
_SUBTITLE_HARD_MAX_READING_CHARS_PER_S = 24.0
_VIDEO_DURATION_TOLERANCE_S = 0.5
_SPOKEN_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*")


def _rendered_duration_contract_error(
    *,
    observed_duration_s: float,
    authored_timeline_s: float,
    selected_target_duration_s: float,
) -> str | None:
    if abs(observed_duration_s - authored_timeline_s) > _VIDEO_DURATION_TOLERANCE_S:
        return (
            f"rendered duration {observed_duration_s:.3f}s does not match "
            f"the authored timeline {authored_timeline_s:.3f}s within "
            f"{_VIDEO_DURATION_TOLERANCE_S:.1f}s"
        )
    if abs(observed_duration_s - selected_target_duration_s) > _VIDEO_DURATION_TOLERANCE_S:
        return (
            f"rendered duration {observed_duration_s:.3f}s does not match "
            f"the selected target {selected_target_duration_s:.3f}s within "
            f"{_VIDEO_DURATION_TOLERANCE_S:.1f}s"
        )
    return None


def resolve_hyperframes_binary() -> Path:
    configured = str(os.getenv("AUTODESIGN_HYPERFRAMES_BIN") or "").strip()
    candidates = [
        Path(configured).expanduser() if configured else None,
        _HYPERFRAMES_BIN,
        _DEVELOPMENT_HYPERFRAMES_BIN,
    ]
    binary = next(
        (candidate for candidate in candidates if candidate and candidate.is_file()),
        None,
    )
    if binary is None:
        raise FileNotFoundError(
            "pinned HyperFrames CLI is missing; run `autodesign setup`"
        )
    return binary


def _hyperframes_command(*args: str) -> list[str]:
    return [str(resolve_hyperframes_binary()), *args]


def _hyperframes_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["HYPERFRAMES_PYTHON"] = sys.executable
    return env


def _run_video_process(
    command: list[str],
    *,
    role: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float,
    process_ledger: ProcessLedger | None = None,
    cancellation_token: CancellationToken | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one media command with durable run ownership when available."""
    token = cancellation_token or CancellationToken.never()
    token.raise_if_cancelled(f"video.{role}.before_spawn")
    if process_ledger is None:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    else:
        process = spawn_registered_process(
            process_ledger,
            command,
            role=role,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        )
    try:
        stdout_bytes, stderr_bytes = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if process_ledger is None:
            process.kill()
        else:
            snapshot = process_ledger.read()
            record = next(
                (
                    item
                    for item in snapshot.processes
                    if item.identity.pid == process.pid
                ),
                None,
            )
            identity = record.identity if record is not None else process_identity(process.pid)
            terminate_process_identities(
                (identity,),
                root_pid=process.pid,
                grace_s=0.5,
                owner_nonces=(record.nonce,) if record is not None else (),
            )
        stdout_bytes, stderr_bytes = process.communicate()
        raise subprocess.TimeoutExpired(
            command,
            timeout,
            output=stdout_bytes,
            stderr=stderr_bytes,
        ) from exc
    token.raise_if_cancelled(f"video.{role}.after_exit")
    return subprocess.CompletedProcess(
        command,
        int(process.returncode or 0),
        (stdout_bytes or b"").decode("utf-8", errors="replace"),
        (stderr_bytes or b"").decode("utf-8", errors="replace"),
    )


# ---------------------------------------------------------------------------
# CLAUDE.md / AGENTS.md boilerplate — always identical across projects.
# Encodes the HyperFrames framework rules the AI composer must follow.
# ---------------------------------------------------------------------------

_CLAUDE_MD = """\
# HyperFrames Composition Project

## Skills — USE THESE FIRST

**Always invoke the relevant skill before writing or modifying compositions.**
Skills encode framework-specific patterns (e.g., `window.__timelines`
registration, `data-*` attribute semantics, shader-compatible CSS rules) that
are NOT in generic web docs. Skipping them produces broken compositions.

| Skill                      | Command                   | When to use                                                                               |
| -------------------------- | ------------------------- | ----------------------------------------------------------------------------------------- |
| **hyperframes**            | `/hyperframes`            | Creating or editing HTML compositions, captions, TTS, audio-reactive animation           |
| **hyperframes-cli**        | `/hyperframes-cli`        | CLI commands: init, lint, preview, render, transcribe, tts                                |
| **hyperframes-registry**   | `/hyperframes-registry`   | Installing blocks and components via `hyperframes add`                                    |
| **website-to-hyperframes** | `/website-to-hyperframes` | Capturing a URL and turning it into a video — full website-to-video pipeline              |
| **gsap**                   | `/gsap`                   | GSAP animations for HyperFrames — tweens, timelines, easing, performance                 |

> **Skills not available?** Run `npx hyperframes skills` and restart your
> agent session, or install manually: `npx skills add heygen-com/hyperframes`.

## Commands

```bash
npx hyperframes preview          # preview in browser (studio editor)
npx hyperframes render           # render to MP4
npx hyperframes lint             # validate compositions (errors + warnings)
npx hyperframes lint --verbose   # include info-level findings
npx hyperframes lint --json      # machine-readable output for CI
npx hyperframes docs <topic>     # reference docs in terminal
```

## Documentation

**For quick reference**, use the local CLI docs command (no network required):

```bash
npx hyperframes docs <topic>
```

Topics: `data-attributes`, `gsap`, `compositions`, `rendering`, `examples`, `troubleshooting`

**For full documentation**, discover pages via the machine-readable index — do NOT guess URLs:

```
https://hyperframes.heygen.com/llms.txt
```

## Project Structure

- `index.html` — main composition (root timeline)
- `compositions/` — sub-compositions referenced via `data-composition-src`
- `assets/` — media files (images)
- `meta.json` — project metadata (id, name)

## Linting — ALWAYS RUN AFTER CHANGES

After creating or editing any `.html` composition, **always** run the linter
before considering the task complete:

```bash
npx hyperframes lint
```

Fix all errors before presenting the result. Warnings are informational and
usually safe to ignore.

## Key Rules

1. Every timed element needs `data-start`, `data-duration`, and `data-track-index`
2. Elements with timing **MUST** have `class="clip"` — the framework uses this for visibility control
3. Put `data-composition-id` on exactly one composition root. For static scenes,
   add `data-no-timeline` to that root. Do not use `requestAnimationFrame`.
4. Use a deterministic animation runtime that HyperFrames can pause and seek.
   GSAP timelines must be paused and registered on `window.__timelines`:
   ```js
   window.__timelines = window.__timelines || {};
   window.__timelines["composition-id"] = gsap.timeline({ paused: true });
   ```
   CSS Animations, WAAPI, Lottie, Anime.js, and Three.js must use the matching
   HyperFrames seek adapter; autonomous wall-clock animation is invalid.
5. Videos use `muted` with a separate `<audio>` element for the audio track
6. Sub-compositions use `data-composition-src="compositions/file.html"` to reference other HTML files
7. Only deterministic logic — no `Date.now()`, no `Math.random()`, no network fetches
"""

_AGENTS_MD = _CLAUDE_MD  # identical content, different filename convention

_HYPERFRAMES_JSON = {
    "$schema": "https://hyperframes.heygen.com/schema/hyperframes.json",
    "registry": "https://raw.githubusercontent.com/heygen-com/hyperframes/main/registry",
    "paths": {
        "blocks": "compositions",
        "components": "compositions/components",
        "assets": "assets",
    },
}

# Figure priority heuristics — prefer figures with these substrings in
# layer_id (architecture diagrams, benchmark charts, method diagrams).
# We pick at most MAX_FIGURES to copy; the rest are excluded to keep the
# video asset dir tidy.
_FIGURE_PRIORITY_KEYWORDS = [
    "architecture", "arch", "method", "pipeline", "overview",
    "benchmark", "result", "table", "comparison", "ablation",
    "hero", "fig_01", "fig_02", "fig_03", "fig_04", "fig_05",
]
MAX_FIGURES = 8
DEFAULT_VIDEO_DURATION_S = 360
DEFAULT_VIDEO_SCENES = 12
FRESH_OUTPUT_MTIME_TOLERANCE_NS = 1_000_000_000
MAX_CONSERVATIVE_TTS_SPEED = 1.25
MAX_DELIVERY_TTS_SPEED = 1.35
MAX_TTS_REFIT_ATTEMPTS = 36
SCENE_SPEECH_END_MARGIN_S = 0.25


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slugify(text: str, max_len: int = 48) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len] or "video"


def _is_sub_panel(layer_id: str) -> bool:
    """Return True if layer_id looks like an ingest sub-panel crop.

    Sub-panels are created when ingest splits a multi-panel figure into
    individual crops, e.g. ingest_fig_01_a, ingest_fig_01_b, ingest_fig_01_1.
    These are incomplete fragments — they lack axis labels, titles, and
    context.  The HyperFrames composer must never use them; only the parent
    figure (ingest_fig_01) should be copied to assets/figures/.

    Heuristic: the layer_id ends with _<single-letter> or _<single-digit>
    after stripping the numeric suffix, e.g. ingest_fig_01_a, ingest_fig_07_b,
    ingest_fig_45_1, ingest_fig_45_2.
    """
    import re
    return bool(re.search(r"_(?:[a-z]|\d)$", layer_id.lower()))


def _select_figures(rendered_layers: dict[str, Any]) -> list[tuple[str, Path]]:
    """Return at most MAX_FIGURES (layer_id, png_path) sorted by priority.

    Sub-panel crops (ingest_fig_NN_a, ingest_fig_NN_1, etc.) are excluded —
    they are incomplete fragments produced by ingest's panel-splitting logic
    and should never appear in the video.  Only the parent figures are kept.
    """
    scored: list[tuple[int, str, Path]] = []
    for lid, layer in rendered_layers.items():
        if layer.get("kind") not in ("image", "background"):
            continue
        src = layer.get("src_path") or layer.get("png_path") or ""
        if not src:
            continue
        p = Path(src)
        if not p.exists():
            continue
        # Skip sub-panel crops — they are partial fragments, not full figures.
        if _is_sub_panel(lid):
            continue
        # score: sum of matched priority keywords
        name_lower = lid.lower()
        score = sum(1 for kw in _FIGURE_PRIORITY_KEYWORDS if kw in name_lower)
        scored.append((score, lid, p))

    scored.sort(key=lambda t: (-t[0], t[1]))
    return [(lid, p) for _, lid, p in scored[:MAX_FIGURES]]


def _build_design_md(
    spec: Any,
    style_prompt: str | None,
    tone: str,
    duration_s: int,
    n_scenes: int,
) -> str:
    """Build DESIGN.md content from the design_spec + caller arguments."""
    ds = getattr(spec, "design_system", None)
    palette = getattr(spec, "palette", None) or []
    mood = getattr(spec, "mood", None) or []
    typography = getattr(spec, "typography", None)
    brief = getattr(spec, "brief", "") or ""

    # Color block
    color_lines = [f"- Palette entry {i+1}: `{c}`" for i, c in enumerate(palette[:8])]
    colors_block = "\n".join(color_lines) if color_lines else "- (inherit from design_spec)"

    # Typography block
    if typography:
        title_font = getattr(typography, "title_font", None) or "Inter"
        body_font = getattr(typography, "subtitle_font", None) or "Inter"
        typo_block = f"- Title / headlines: `{title_font}`\n- Body / labels: `{body_font}`"
    else:
        typo_block = "- Use `Inter` for headlines and labels"

    # Style prompt: explicit override > design_system style name > mood list
    if style_prompt:
        composed_style = style_prompt
    elif mood:
        composed_style = (
            f"Visual tone derived from design brief. "
            f"Mood keywords: {', '.join(mood)}. "
            f"Match the palette and typographic register of the source landing page. "
            f"The video should feel like an authored presentation, not a generic promo."
        )
    else:
        composed_style = (
            "Clean, editorial presentation. Match the source landing page's "
            "palette and typography. Restrained motion, deliberate pacing."
        )

    return f"""\
## Source Brief

{brief}

## Style Prompt

{composed_style}

## Video Parameters

- Duration: {duration_s} seconds total
- Scenes: {n_scenes} (required range: 10-14)
- Tone: {tone}
- Canvas: 1920 × 1080 (16:9)

## Colors

{colors_block}

## Typography

{typo_block}

## Motion Rules

- Entrances are staggered and directional (y-offset fade-in via GSAP power3.out).
- Ambient motion is slow and finite: figures get a gentle Ken Burns scale drift.
- Transitions are wipe-style: a single-color bar sweeps left-to-right then contracts,
  with a thin accent rule crossing the frame — use the first palette color as the bar
  and the second as the rule.
- Keep all animations deterministic — no Math.random(), no Date.now().

## What NOT to Do

- Do not use neon gradients or generic tech-hero stock imagery.
- Do not crowd scenes with paragraph walls — each scene needs one clear presenter point.
- Do not use fast jump cuts; transitions should feel like talk-slide changes (≥ 0.8 s).
- Do not invent data or claims not visible in the provided figures.
- Do not rasterize text — all titles/labels must be real HTML so they remain legible
  at 1920 × 1080.
"""


def _scene_manifest_from_html_artifact(spec: Any) -> list[dict[str, Any]]:
    artifact = getattr(spec, "html_artifact", None)
    if artifact is None:
        return []
    data = artifact.model_dump(mode="json") if hasattr(artifact, "model_dump") else artifact
    if not isinstance(data, dict):
        return []
    scenes: list[dict[str, Any]] = []
    timeline_cursor_s = 0.0
    for idx, frame in enumerate(data.get("frames") or []):
        if not isinstance(frame, dict) or str(frame.get("kind") or "") != "scene":
            continue
        blocks = frame.get("blocks") if isinstance(frame.get("blocks"), list) else []
        duration_s = float(frame.get("duration_s") or 0)
        narration_text = str(frame.get("speaker_notes") or "").strip()
        if not narration_text:
            narration_text = " ".join(
                str(block.get("text") or block.get("title") or "").strip()
                for block in blocks
                if isinstance(block, dict)
            ).strip()
        if not narration_text:
            narration_text = str(frame.get("title") or frame.get("role") or "").strip()
        scenes.append({
            "scene_id": str(frame.get("frame_id") or f"scene_{idx + 1:02d}"),
            "title": str(frame.get("title") or frame.get("role") or f"Scene {idx + 1}"),
            "start_s": timeline_cursor_s,
            "duration_s": duration_s,
            "narration_text": narration_text,
            "transition": str(frame.get("transition") or "cut"),
            "layout": str(frame.get("layout") or ""),
            "blocks": [
                {
                    "block_id": str(block.get("block_id") or block.get("layer_id") or ""),
                    "kind": str(block.get("kind") or ""),
                    "role": str(block.get("role") or ""),
                    "text": str(block.get("text") or block.get("title") or "")[:240],
                    "src_path": str(block.get("src_path") or ""),
                }
                for block in blocks if isinstance(block, dict)
            ],
        })
        timeline_cursor_s += duration_s
    return scenes


def _scene_manifest_markdown(scenes: list[dict[str, Any]]) -> str:
    if not scenes:
        return "(no explicit html_artifact scene frames; derive scenes from the source artifact)"
    lines: list[str] = []
    for scene in scenes:
        lines.append(
            f"- `{scene['scene_id']}` — {scene['title']} "
            f"({scene['start_s']:g}-{scene['start_s'] + scene['duration_s']:g}s, "
            f"{scene['transition']})\n"
            f"  - narration: {scene['narration_text']}"
        )
        for block in scene.get("blocks") or []:
            text = str(block.get("text") or block.get("src_path") or "").strip()
            if text:
                lines.append(f"  - {block.get('kind')}/{block.get('role')}: {text[:120]}")
    return "\n".join(lines)


def _subtitle_timestamp(seconds: float, *, decimal_mark: str) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return (
        f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}"
        f"{decimal_mark}{milliseconds:03d}"
    )


class _SubtitleReadabilityError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        diagnostics: list[dict[str, Any]],
    ) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


def _subtitle_cues(
    text: str,
    *,
    scene_id: str,
    start_s: float,
    end_s: float,
) -> tuple[list[tuple[float, float, str]], dict[str, Any]]:
    words = str(text or "").split()
    duration_s = max(0.001, end_s - start_s)
    if not words:
        return [], {
            "scene_id": scene_id,
            "cue_count": 0,
            "max_cps": 0.0,
            "target_cps": _SUBTITLE_MAX_READING_CHARS_PER_S,
            "hard_limit_cps": _SUBTITLE_HARD_MAX_READING_CHARS_PER_S,
            "soft_exceeded": False,
            "hard_exceeded": False,
            "violating_cues": [],
        }

    lines: list[str] = []
    current = ""
    for word in words:
        if len(word) > _SUBTITLE_MAX_LINE_CHARS:
            if current:
                lines.append(current)
                current = ""
            lines.extend(
                word[index:index + _SUBTITLE_MAX_LINE_CHARS]
                for index in range(0, len(word), _SUBTITLE_MAX_LINE_CHARS)
            )
            continue
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > _SUBTITLE_MAX_LINE_CHARS:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)

    minimum_cues = max(1, math.ceil(duration_s / _SUBTITLE_MAX_CUE_DURATION_S))
    lines_per_cue = 2
    if math.ceil(len(lines) / lines_per_cue) < minimum_cues:
        lines_per_cue = 1
    chunks = [
        "\n".join(lines[index:index + lines_per_cue])
        for index in range(0, len(lines), lines_per_cue)
    ]
    while len(chunks) < minimum_cues:
        longest_index = max(
            range(len(chunks)),
            key=lambda index: len(chunks[index].split()),
        )
        chunk_words = chunks[longest_index].replace("\n", " ").split()
        if len(chunk_words) < 2:
            break
        midpoint = math.ceil(len(chunk_words) / 2)
        chunks[longest_index:longest_index + 1] = [
            " ".join(chunk_words[:midpoint]),
            " ".join(chunk_words[midpoint:]),
        ]

    cue_duration_s = duration_s / len(chunks)
    cues: list[tuple[float, float, str]] = []
    violating_cues: list[dict[str, Any]] = []
    max_cps = 0.0
    for index, chunk in enumerate(chunks):
        cue_start = start_s + index * cue_duration_s
        cue_end = end_s if index + 1 == len(chunks) else start_s + (index + 1) * cue_duration_s
        flattened_length = len(chunk.replace("\n", " "))
        cps = flattened_length / max(0.001, cue_end - cue_start)
        max_cps = max(max_cps, cps)
        if cps > _SUBTITLE_MAX_READING_CHARS_PER_S:
            violating_cues.append({
                "cue_index": index + 1,
                "start_s": round(cue_start, 3),
                "end_s": round(cue_end, 3),
                "cps": round(cps, 2),
                "text": chunk,
            })
        cues.append((cue_start, cue_end, chunk))
    return cues, {
        "scene_id": scene_id,
        "cue_count": len(cues),
        "max_cps": round(max_cps, 2),
        "target_cps": _SUBTITLE_MAX_READING_CHARS_PER_S,
        "hard_limit_cps": _SUBTITLE_HARD_MAX_READING_CHARS_PER_S,
        "soft_exceeded": max_cps > _SUBTITLE_MAX_READING_CHARS_PER_S,
        "hard_exceeded": max_cps > _SUBTITLE_HARD_MAX_READING_CHARS_PER_S,
        "violating_cues": violating_cues,
    }


def _speech_delivery_metrics(
    scene_manifest: list[dict[str, Any]],
    speech_timing: list[dict[str, Any]],
    *,
    target_duration_s: float,
) -> tuple[dict[str, Any], str]:
    timing_by_scene: dict[str, float] = {}
    for item in speech_timing:
        scene_id = str(item.get("scene_id") or "").strip()
        if not scene_id or scene_id in timing_by_scene:
            return {}, "video delivery speech coverage timing has duplicate scene ids"
        try:
            speech_duration_s = float(item.get("speech_duration_s"))
        except (TypeError, ValueError):
            return {}, f"video delivery speech duration is invalid for {scene_id}"
        if speech_duration_s <= 0:
            return {}, f"video delivery speech duration must be positive for {scene_id}"
        timing_by_scene[scene_id] = speech_duration_s

    expected_scene_ids = {str(scene.get("scene_id") or "") for scene in scene_manifest}
    if set(timing_by_scene) != expected_scene_ids:
        return {}, "video delivery speech coverage requires measured timing for every scene"
    if target_duration_s <= 0:
        return {}, "video delivery speech coverage requires a positive target duration"
    speech_duration_s = sum(timing_by_scene.values())
    spoken_word_count = sum(
        len(_SPOKEN_WORD_RE.findall(str(scene.get("narration_text") or "")))
        for scene in scene_manifest
    )
    spoken_wpm = spoken_word_count / (target_duration_s / 60.0)
    speech_coverage_ratio = speech_duration_s / target_duration_s
    metrics = {
        "speech_duration_s": round(speech_duration_s, 3),
        "coverage_duration_s": target_duration_s,
        "speech_coverage_ratio": speech_coverage_ratio,
        "minimum_speech_coverage_ratio": _MINIMUM_SPEECH_COVERAGE_RATIO,
        "spoken_word_count": spoken_word_count,
        "spoken_wpm": spoken_wpm,
        "minimum_spoken_wpm": _MINIMUM_SPOKEN_WPM,
        "measured_speech_scene_count": len(timing_by_scene),
    }
    if spoken_wpm < _MINIMUM_SPOKEN_WPM:
        return metrics, (
            f"canonical narration rate {spoken_wpm:.1f} spoken WPM is below "
            f"the required {_MINIMUM_SPOKEN_WPM}"
        )
    if speech_coverage_ratio < _MINIMUM_SPEECH_COVERAGE_RATIO:
        return metrics, (
            f"actual TTS speech coverage {speech_coverage_ratio:.3f} is below "
            f"the required {_MINIMUM_SPEECH_COVERAGE_RATIO:.2f}"
        )
    return metrics, ""


def _write_narration_artifacts(
    proj_dir: Path,
    scene_manifest: list[dict[str, Any]],
    voice_preset: str,
    *,
    speech_timing: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write canonical transcript/subtitles and deterministic voice metadata."""
    narration_dir = proj_dir / "narration"
    narration_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = narration_dir / "transcript.en.txt"
    srt_path = narration_dir / "subtitles.en.srt"
    vtt_path = narration_dir / "subtitles.en.vtt"
    voice_path = narration_dir / "voice.json"
    timing_path = narration_dir / "timing.json"
    timing_by_scene = {
        str(item.get("scene_id") or ""): item
        for item in (speech_timing or [])
    }
    write_subtitles = speech_timing is not None

    transcript_lines: list[str] = []
    srt_blocks: list[str] = []
    vtt_blocks: list[str] = ["WEBVTT"]
    subtitle_diagnostics: list[dict[str, Any]] = []
    cue_index = 1
    for scene in scene_manifest:
        text = " ".join(str(scene["narration_text"]).split())
        start_s = float(scene["start_s"])
        transcript_lines.append(text)
        if not write_subtitles:
            continue
        measured = timing_by_scene.get(str(scene["scene_id"]))
        if measured is None:
            raise ValueError(
                f"missing measured narration timing for {scene['scene_id']}"
            )
        end_s = float(measured["end_s"])
        scene_id = str(scene["scene_id"])
        cues, scene_diagnostics = _subtitle_cues(
            text,
            scene_id=scene_id,
            start_s=start_s,
            end_s=end_s,
        )
        subtitle_diagnostics.append(scene_diagnostics)
        for cue_start, cue_end, cue_text in cues:
            srt_blocks.append(
                f"{cue_index}\n"
                f"{_subtitle_timestamp(cue_start, decimal_mark=',')} --> "
                f"{_subtitle_timestamp(cue_end, decimal_mark=',')}\n{cue_text}"
            )
            vtt_blocks.append(
                f"{_subtitle_timestamp(cue_start, decimal_mark='.')} --> "
                f"{_subtitle_timestamp(cue_end, decimal_mark='.')}\n{cue_text}"
            )
            cue_index += 1

    hard_failures = [
        diagnostic
        for diagnostic in subtitle_diagnostics
        if diagnostic["hard_exceeded"]
    ]
    if hard_failures:
        failure_summaries: list[str] = []
        for diagnostic in hard_failures:
            worst_cue = max(
                diagnostic["violating_cues"],
                key=lambda cue: float(cue["cps"]),
            )
            failure_summaries.append(
                f"{diagnostic['scene_id']} cue {worst_cue['cue_index']} "
                f"at {worst_cue['cps']:.2f} CPS"
            )
        raise _SubtitleReadabilityError(
            "English subtitle readability hard limit exceeded: "
            + "; ".join(failure_summaries)
            + (
                f" (hard limit "
                f"{_SUBTITLE_HARD_MAX_READING_CHARS_PER_S:.2f} CPS)"
            ),
            diagnostics=subtitle_diagnostics,
        )

    transcript_path.write_text("\n\n".join(transcript_lines) + "\n", encoding="utf-8")
    if write_subtitles:
        srt_path.write_text("\n\n".join(srt_blocks) + "\n", encoding="utf-8")
        vtt_path.write_text("\n\n".join(vtt_blocks) + "\n", encoding="utf-8")
    else:
        srt_path.unlink(missing_ok=True)
        vtt_path.unlink(missing_ok=True)
    voice_path.write_text(
        json.dumps(
            {
                "preset": voice_preset,
                "engine": "kokoro",
                "kokoro_voice_id": KOKORO_VOICE_BY_PRESET[voice_preset],
                "mapping_version": "kokoro-v1",
                "language": "en",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    timing_path.write_text(
        json.dumps(speech_timing or [], indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "transcript_path": str(transcript_path.relative_to(proj_dir)),
        "srt_path": str(srt_path.relative_to(proj_dir)),
        "vtt_path": str(vtt_path.relative_to(proj_dir)),
        "voice_metadata_path": str(voice_path.relative_to(proj_dir)),
        "narration_timing_path": str(timing_path.relative_to(proj_dir)),
        "subtitle_diagnostics": subtitle_diagnostics,
        "subtitle_soft_limit_exceeded": any(
            diagnostic["soft_exceeded"]
            for diagnostic in subtitle_diagnostics
        ),
    }


def _stable_pointer_cleanup_warnings(*warning_groups: object) -> list[str]:
    warnings: list[str] = []
    seen: set[str] = set()
    for group in warning_groups:
        if not isinstance(group, (list, tuple)):
            continue
        for warning in group:
            if type(warning) is str and warning not in seen:
                warnings.append(warning)
                seen.add(warning)
    return warnings


@contextmanager
def _pointer_cleanup_cancellation_scope(
    pointer_cleanup_warnings: object,
) -> Iterator[None]:
    try:
        yield
    except RunCancelled as exc:
        exc.pointer_cleanup_warnings = tuple(_stable_pointer_cleanup_warnings(
            pointer_cleanup_warnings,
            getattr(exc, "pointer_cleanup_warnings", ()),
        ))
        raise


def _clear_stale_video_delivery(proj_dir: Path, ctx: ToolContext) -> None:
    """Remove outputs that could make a failed current attempt look accepted."""
    ctx.raise_if_cancelled("video.export.before_delivery_invalidation")
    prior_delivery = ctx.state.get("video_delivery")
    pointer_update = update_video_delivery_pointer(
        ctx.run_dir,
        mode="invalidate_if_present",
        reason="new_video_export",
        prior_delivery=(
            prior_delivery if isinstance(prior_delivery, dict) else None
        ),
    )
    pointer_cleanup_warnings = _stable_pointer_cleanup_warnings(
        ctx.state.get("pointer_cleanup_warnings"),
        pointer_update.cleanup_warnings,
    )
    ctx.state["pointer_cleanup_warnings"] = pointer_cleanup_warnings
    log(
        "export_video.pointer_cleanup",
        pointer_cleanup_warnings=list(pointer_cleanup_warnings),
    )
    ctx.state.pop("video_delivery", None)
    ctx.state.pop("finalized", None)
    previous_payload = ctx.state.get("last_composite_payload")
    if isinstance(previous_payload, dict) and previous_payload.get("artifact_type") == "video":
        ctx.state.pop("last_composite_payload", None)
        ctx.state.pop("last_design_feedback", None)
    existing_composition = ctx.state.get("composition")
    manifest = getattr(existing_composition, "layer_manifest", None)
    if isinstance(manifest, list) and any(
        isinstance(item, dict) and item.get("kind") == "video" for item in manifest
    ):
        ctx.state.pop("composition", None)
    ctx.raise_if_cancelled("video.export.after_delivery_invalidation")
    for path in (
        proj_dir / "index.html",
        proj_dir / "media_probe.json",
        proj_dir / "delivery_manifest.json",
        proj_dir / "assets" / "narration.wav",
        proj_dir / "assets" / "source.html",
    ):
        path.unlink(missing_ok=True)
    for pattern in ("renders/*.mp4", "narration/scenes/*.wav"):
        for path in proj_dir.glob(pattern):
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

def export_video(args: dict[str, Any], *, ctx: ToolContext) -> ToolResultRecord:
    """Scaffold a HyperFrames project directory ready for index.html authoring.

    Args (from planner):
        video_id:      Short slug for the output directory name.
                       Defaults to a slug derived from the run_id.
        style_prompt:  Optional free-text override for DESIGN.md's style section.
                       If omitted, derived from design_spec.mood + palette.
        tone:          One of "academic" | "product" | "editorial" | "technical".
                       Guides DESIGN.md motion and copy register.
        duration_s:    Target video duration in seconds (default 360; 300-600).
        n_scenes:      Number of scenes (default 12; accepted 10-14).
        voice_preset:  Deterministic Kokoro voice preset: male or female.
        include_source_page: If true, copy a prior landing index.html as
                       assets/source.html. Disabled by default so the video
                       project remains a self-contained dependency closure.
    """
    spec = ctx.state.get("design_spec")
    if spec is None:
        return obs_error(
            "export_video requires a composited artifact — call composite first",
            category="validation",
        )

    # -- resolve args -------------------------------------------------------
    run_slug = ctx.run_id.replace("/", "-").replace("_", "-")[:32]
    raw_video_id = args.get("video_id") or f"{run_slug}-video"
    video_id = _slugify(raw_video_id)
    style_prompt: str | None = args.get("style_prompt") or None
    tone: str = args.get("tone", "academic")
    try:
        duration_s = int(args.get("duration_s", DEFAULT_VIDEO_DURATION_S))
        n_scenes = int(args.get("n_scenes", DEFAULT_VIDEO_SCENES))
    except (TypeError, ValueError) as exc:
        return obs_error(
            f"invalid numeric video argument: {exc}",
            category="validation",
        )
    voice_preset: str = str(args.get("voice_preset", "female"))
    include_source: bool = bool(args.get("include_source_page", False))
    scene_manifest = _scene_manifest_from_html_artifact(spec)
    if scene_manifest:
        n_scenes = len(scene_manifest)
        if "duration_s" not in args:
            duration_s = int(round(sum(float(s.get("duration_s") or 0) for s in scene_manifest))) or duration_s

    try:
        delivery_contract = VideoDeliveryContract(
            target_duration_s=duration_s,
            voice_preset=voice_preset,
            scenes=scene_manifest,
        )
    except Exception as exc:
        return obs_error(
            f"video delivery contract validation failed: {exc}",
            category="validation",
            payload={
                "duration_s": duration_s,
                "n_scenes": n_scenes,
                "voice_preset": voice_preset,
            },
        )

    # -- create project dir -------------------------------------------------
    proj_dir = ctx.run_dir / f"hyperframes-{video_id}"
    process_ledger = ProcessLedger(ctx.run_dir)
    cancellation_token = ctx.cancellation_token
    if proj_dir.exists():
        # idempotent — re-running overwrites meta/design files, re-copies figures
        log("export_video.overwrite", path=str(proj_dir))
    proj_dir.mkdir(parents=True, exist_ok=True)
    try:
        _clear_stale_video_delivery(proj_dir, ctx)
    except (OSError, RuntimeError, ValueError) as exc:
        return obs_error(
            f"Video delivery pointer invalidation failed: {exc}",
            category="validation",
            payload={"phase": "final_pointer"},
        )

    assets_dir = proj_dir / "assets"
    figures_dir = assets_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)
    renders_dir = proj_dir / "renders"
    renders_dir.mkdir(parents=True, exist_ok=True)

    # -- meta.json ----------------------------------------------------------
    meta = {
        "id": video_id,
        "name": video_id,
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    (proj_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # -- hyperframes.json ---------------------------------------------------
    (proj_dir / "hyperframes.json").write_text(
        json.dumps(_HYPERFRAMES_JSON, indent=2), encoding="utf-8"
    )

    # -- CLAUDE.md + AGENTS.md ---------------------------------------------
    (proj_dir / "CLAUDE.md").write_text(_CLAUDE_MD, encoding="utf-8")
    (proj_dir / "AGENTS.md").write_text(_AGENTS_MD, encoding="utf-8")

    # -- DESIGN.md ----------------------------------------------------------
    design_md = _build_design_md(spec, style_prompt, tone, duration_s, n_scenes)
    (proj_dir / "DESIGN.md").write_text(design_md, encoding="utf-8")
    if scene_manifest:
        (proj_dir / "scene_graph.json").write_text(
            json.dumps({"scenes": scene_manifest}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    delivery_contract_payload = delivery_contract.model_dump(mode="json")
    delivery_contract_payload["narration_contract"] = {
        "minimum_spoken_wpm": _MINIMUM_SPOKEN_WPM,
        "minimum_speech_coverage_ratio": _MINIMUM_SPEECH_COVERAGE_RATIO,
        "subtitle_max_line_chars": _SUBTITLE_MAX_LINE_CHARS,
        "subtitle_max_cue_duration_s": _SUBTITLE_MAX_CUE_DURATION_S,
        "subtitle_max_reading_chars_per_s": _SUBTITLE_MAX_READING_CHARS_PER_S,
        "subtitle_hard_max_reading_chars_per_s": (
            _SUBTITLE_HARD_MAX_READING_CHARS_PER_S
        ),
    }
    (proj_dir / "video_delivery_contract.json").write_text(
        json.dumps(delivery_contract_payload, indent=2) + "\n",
        encoding="utf-8",
    )
    narration_artifacts = _write_narration_artifacts(
        proj_dir, scene_manifest, voice_preset
    )

    # -- copy figures -------------------------------------------------------
    rendered = ctx.state.get("rendered_layers") or {}
    selected = _select_figures(rendered)
    figure_manifest: list[dict[str, str]] = []
    for layer_id, src_path in selected:
        layer = rendered[layer_id]
        dest_stem = layer_id.replace("ingest_", "").replace("img_", "")
        dest = figures_dir / f"{dest_stem}.png"
        shutil.copy2(src_path, dest)
        figure_manifest.append({
            "layer_id": layer_id,
            "filename": f"assets/figures/{dest_stem}.png",
            "caption": layer.get("caption") or "",
            "kind": layer.get("kind") or "image",
        })

    # -- source.html --------------------------------------------------------
    source_html_rel: str | None = None
    if include_source:
        # Look for the composited landing page HTML
        candidates = [
            ctx.run_dir / "final" / "index.html",
            ctx.run_dir / "composites" / "iter_01" / "index.html",
        ]
        for candidate in candidates:
            if candidate.exists():
                dest_src = assets_dir / "source.html"
                shutil.copy2(candidate, dest_src)
                source_html_rel = "assets/source.html"
                break

    # -- build the composer prompt context ----------------------------------
    # This is the "context block" the planner should pass (along with
    # prompts/hyperframes_composer.md) to the LLM that will write index.html.
    # Returned in payload so the planner can forward it without extra file reads.
    figures_summary = "\n".join(
        f"  - `{f['filename']}` — {f['caption'] or f['layer_id']}"
        for f in figure_manifest
    ) or "  (no figures available)"

    composer_context = (
        f"## Project directory\n\n"
        f"`{proj_dir.name}/` (relative to run dir)\n\n"
        f"## HTML artifact scenes\n\n"
        f"{_scene_manifest_markdown(scene_manifest)}\n\n"
        f"## Canonical narration\n\n"
        f"English transcript: `narration/transcript.en.txt`\n\n"
        f"Local Kokoro audio: `assets/narration.wav`\n\n"
        f"Subtitles: `narration/subtitles.en.srt` and "
        f"`narration/subtitles.en.vtt`\n\n"
        f"## Available figures\n\n"
        f"{figures_summary}\n\n"
        f"## Source landing page\n\n"
        + (f"`assets/source.html` — full landing page for closing-scene iframe\n"
           if source_html_rel else "(no source page available)\n")
        + f"\n## DESIGN.md (full text)\n\n{design_md}"
    )

    log(
        "export_video.scaffold.done",
        proj_dir=str(proj_dir),
        figures=len(figure_manifest),
        duration_s=duration_s,
        n_scenes=n_scenes,
    )

    # ------------------------------------------------------------------
    # Stage 2: HyperFrames Composer — write index.html automatically.
    # Skipped when `enable_video_composer=False` (SKIP_VIDEO_COMPOSER=1)
    # so power users can author index.html themselves.
    # ------------------------------------------------------------------
    index_html_path: Path | None = None
    mp4_path: Path | None = None
    composer_skipped = False
    composer_skip_reason = ""
    composer_model = ""
    composer_wall_s = 0.0
    composer_in_tok = 0
    composer_out_tok = 0
    lint_output = ""
    lint_ok: bool | None = None
    tts_output = ""
    tts_ok: bool | None = None
    narration_audio_path: Path | None = None
    render_output = ""
    render_ok: bool | None = None
    media_probe: VideoMediaProbe | None = None
    subtitle_track_output = ""
    subtitle_track_ok: bool | None = None
    render_started_at: str | None = None
    speech_timing: list[dict[str, Any]] = []
    speech_metrics: dict[str, Any] = {}
    speech_contract_error = ""
    speech_failure_kind = ""
    authoring_lint_output = ""
    authoring_lint_ok: bool | None = None

    if ctx.settings.enable_video_composer:
        system_prompt = load_composer_system_prompt(ctx.settings)
        if system_prompt is None:
            composer_skipped = True
            composer_skip_reason = "hyperframes_composer.md not found"
            log("export_video.composer.skip", reason=composer_skip_reason)
        else:
            composer = HyperFramesComposer(ctx.settings, system_prompt)
            result = composer.compose(
                composer_context,
                proj_dir,
                delivery_contract=delivery_contract,
            )
            composer_skipped = result.skipped
            composer_skip_reason = result.skip_reason
            composer_model = result.model
            composer_wall_s = result.wall_time_s
            composer_in_tok = result.input_tokens
            composer_out_tok = result.output_tokens
            if not result.skipped:
                index_html_path = proj_dir / "index.html"
                try:
                    authored_html = index_html_path.read_text(encoding="utf-8")
                except Exception as exc:
                    html_errors = [f"authored HTML could not be read: {exc}"]
                else:
                    html_errors = validate_authored_video_html(
                        authored_html,
                        delivery_contract,
                        project_dir=proj_dir,
                    )
                if html_errors:
                    composer_skipped = True
                    composer_skip_reason = (
                        "authored HTML does not match delivery contract: "
                        + "; ".join(html_errors)
                    )
                    index_html_path.unlink(missing_ok=True)
                    index_html_path = None
                else:
                    authoring_lint_output, authoring_lint_ok = (
                        _run_hyperframes_authoring_lint(
                            proj_dir,
                            process_ledger=process_ledger,
                            cancellation_token=cancellation_token,
                        )
                    )
                    if authoring_lint_ok:
                        (
                            tts_output,
                            tts_ok,
                            narration_audio_path,
                            speech_timing,
                        ) = _synthesize_timed_narration(
                            proj_dir,
                            scene_manifest=scene_manifest,
                            voice_id=delivery_contract.voice.kokoro_voice_id,
                            target_duration_s=duration_s,
                            process_ledger=process_ledger,
                            cancellation_token=cancellation_token,
                        )
                if tts_ok and narration_audio_path is not None:
                    speech_metrics, speech_contract_error = _speech_delivery_metrics(
                        scene_manifest,
                        speech_timing,
                        target_duration_s=duration_s,
                    )
                    if speech_contract_error:
                        speech_failure_kind = (
                            "spoken_wpm_below_minimum"
                            if speech_metrics.get("spoken_wpm", _MINIMUM_SPOKEN_WPM)
                            < _MINIMUM_SPOKEN_WPM
                            else "speech_coverage_below_minimum"
                        )
                        timing_path = proj_dir / narration_artifacts["narration_timing_path"]
                        timing_path.write_text(
                            json.dumps(speech_timing, indent=2) + "\n",
                            encoding="utf-8",
                        )
                    else:
                        try:
                            narration_artifacts = _write_narration_artifacts(
                                proj_dir,
                                scene_manifest,
                                voice_preset,
                                speech_timing=speech_timing,
                            )
                        except _SubtitleReadabilityError as exc:
                            speech_metrics["subtitle_diagnostics"] = exc.diagnostics
                            speech_metrics["subtitle_soft_limit_exceeded"] = True
                            speech_contract_error = (
                                f"{exc}. Shorten or rewrite the reported scene "
                                "narration before retrying."
                            )
                            speech_failure_kind = "subtitle_readability_failed"
                            timing_path = (
                                proj_dir
                                / narration_artifacts["narration_timing_path"]
                            )
                            timing_path.write_text(
                                json.dumps(speech_timing, indent=2) + "\n",
                                encoding="utf-8",
                            )
                        except ValueError as exc:
                            speech_contract_error = (
                                "English subtitle generation failed for the fitted "
                                f"speech duration: {exc}"
                            )
                            speech_failure_kind = "subtitle_generation_failed"
                            timing_path = (
                                proj_dir
                                / narration_artifacts["narration_timing_path"]
                            )
                            timing_path.write_text(
                                json.dumps(speech_timing, indent=2) + "\n",
                                encoding="utf-8",
                            )
                    voice_metadata_path = (
                        proj_dir / narration_artifacts["voice_metadata_path"]
                    )
                    voice_metadata = json.loads(
                        voice_metadata_path.read_text(encoding="utf-8")
                    )
                    voice_metadata["audio_path"] = str(
                        narration_audio_path.relative_to(proj_dir)
                    )
                    voice_metadata["timing_path"] = narration_artifacts[
                        "narration_timing_path"
                    ]
                    voice_metadata_path.write_text(
                        json.dumps(voice_metadata, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    if not speech_contract_error:
                        lint_output, lint_ok = _run_hyperframes_lint(
                            proj_dir,
                            process_ledger=process_ledger,
                            cancellation_token=cancellation_token,
                        )
                if tts_ok and lint_ok and not speech_contract_error:
                    # Record the render boundary so an MP4 left by an earlier
                    # attempt can never satisfy this delivery.
                    render_started_at = datetime.now(timezone.utc).isoformat()
                    render_started_ns = time.time_ns()
                    render_output, render_ok, mp4_path, media_probe = (
                        _run_hyperframes_render(
                            proj_dir,
                            video_id,
                            render_started_ns=render_started_ns,
                            process_ledger=process_ledger,
                            cancellation_token=cancellation_token,
                        )
                    )
                    if render_ok and mp4_path is not None and media_probe is not None:
                        (
                            subtitle_track_output,
                            subtitle_track_ok,
                            captioned_mp4_path,
                            captioned_media_probe,
                        ) = _prepare_captioned_delivery_mp4(
                            mp4_path,
                            proj_dir / narration_artifacts["srt_path"],
                            process_ledger=process_ledger,
                            cancellation_token=cancellation_token,
                        )
                        render_output = "\n".join(
                            part for part in (render_output, subtitle_track_output) if part
                        )
                        if (
                            subtitle_track_ok
                            and captioned_mp4_path is not None
                            and captioned_media_probe is not None
                        ):
                            mp4_path = captioned_mp4_path
                            media_probe = captioned_media_probe
                        else:
                            render_ok = False
                            mp4_path = None
                            media_probe = None
                    if render_ok and media_probe is not None:
                        authored_timeline_s = max(
                            float(scene["start_s"]) + float(scene["duration_s"])
                            for scene in scene_manifest
                        )
                        observed_duration_s = float(media_probe.duration_s)
                        duration_contract_error = _rendered_duration_contract_error(
                            observed_duration_s=observed_duration_s,
                            authored_timeline_s=authored_timeline_s,
                            selected_target_duration_s=float(
                                delivery_contract.target_duration_s
                            ),
                        )
                        if duration_contract_error:
                            speech_metrics.update({
                                "authored_timeline_duration_s": authored_timeline_s,
                                "coverage_duration_s": observed_duration_s,
                            })
                            speech_contract_error = duration_contract_error
                            speech_failure_kind = "render_duration_mismatch"
                        else:
                            speech_metrics, speech_contract_error = (
                                _speech_delivery_metrics(
                                    scene_manifest,
                                    speech_timing,
                                    target_duration_s=observed_duration_s,
                                )
                            )
                            speech_metrics["authored_timeline_duration_s"] = (
                                authored_timeline_s
                            )
                            if speech_contract_error:
                                speech_failure_kind = (
                                    "spoken_wpm_below_minimum"
                                    if speech_metrics.get(
                                        "spoken_wpm", _MINIMUM_SPOKEN_WPM
                                    ) < _MINIMUM_SPOKEN_WPM
                                    else "speech_coverage_below_minimum"
                                )
    else:
        composer_skipped = True
        composer_skip_reason = "disabled via SKIP_VIDEO_COMPOSER"
        log("export_video.composer.skip", reason=composer_skip_reason)

    log(
        "export_video.done",
        proj_dir=str(proj_dir),
        figures=len(figure_manifest),
        duration_s=duration_s,
        n_scenes=n_scenes,
        index_html=str(index_html_path) if index_html_path else None,
        mp4=str(mp4_path) if mp4_path else None,
        composer_skipped=composer_skipped,
        authoring_lint_ok=authoring_lint_ok,
        lint_ok=lint_ok,
        render_ok=render_ok,
    )

    payload: dict[str, Any] = {
        "video_id": video_id,
        "project_dir": proj_dir.name,  # relative — no absolute paths in payload
        "n_figures": len(figure_manifest),
        "figures": figure_manifest,
        "has_source_page": source_html_rel is not None,
        "duration_s": duration_s,
        "n_scenes": n_scenes,
        "html_artifact_scene_count": len(scene_manifest),
        "scene_manifest": scene_manifest,
        "tone": tone,
        "voice_preset": voice_preset,
        "voice": delivery_contract.voice.model_dump(mode="json"),
        "index_html_written": index_html_path is not None,
        "mp4_written": mp4_path is not None,
        "composer_model": composer_model,
        "composer_skipped": composer_skipped,
        "composer_skip_reason": composer_skip_reason,
        "pointer_cleanup_warnings": _stable_pointer_cleanup_warnings(
            ctx.state.get("pointer_cleanup_warnings")
        ),
        **speech_metrics,
        **narration_artifacts,
    }
    if authoring_lint_ok is not None:
        payload["authoring_lint_ok"] = authoring_lint_ok
        payload["authoring_lint_output"] = authoring_lint_output[:2000]
    if tts_ok is not None:
        payload["tts_ok"] = tts_ok
        payload["tts_output"] = tts_output[:2000]
    if narration_audio_path is not None:
        payload["narration_audio_path"] = str(
            narration_audio_path.relative_to(proj_dir)
        )
    if render_started_at is not None:
        payload["render_started_at"] = render_started_at

    if lint_ok is not None:
        payload["lint_ok"] = lint_ok
        payload["lint_output"] = lint_output[:2000]

    if render_ok is not None:
        payload["render_ok"] = render_ok
        payload["render_output"] = render_output[:2000]
    if subtitle_track_ok is not None:
        payload["subtitle_track_ok"] = subtitle_track_ok
        payload["subtitle_track_output"] = subtitle_track_output[:2000]

    if mp4_path is not None:
        # Relative path so the planner can surface it without absolute leak.
        payload["mp4_path"] = str(mp4_path.relative_to(ctx.run_dir))
    if media_probe is not None:
        probe_payload = media_probe.model_dump(mode="json")
        payload["media_probe"] = probe_payload
        (proj_dir / "media_probe.json").write_text(
            json.dumps(probe_payload, indent=2) + "\n",
            encoding="utf-8",
        )

    if composer_skipped:
        return obs_error(
            f"HyperFrames composition failed: {composer_skip_reason}",
            category="validation",
            payload=payload,
        )
    if authoring_lint_ok is not True:
        payload["lint_ok"] = False
        payload["lint_output"] = authoring_lint_output[:2000]
        return obs_error(
            "HyperFrames lint failed during authoring preflight: "
            f"{authoring_lint_output or 'lint did not complete'}",
            category="validation",
            payload=payload,
        )
    if tts_ok is not True or narration_audio_path is None:
        narration_failure = tts_output or "no fresh narration audio was produced"
        if narration_failure.startswith("narration_timing_unfit "):
            payload["delivery_failure_kind"] = "narration_timing_unfit"
            payload["delivery_repairable"] = True
        return obs_error(
            (
                narration_failure
                if narration_failure.startswith("narration_timing_unfit ")
                else f"Kokoro narration synthesis failed: {narration_failure}"
            ),
            category="validation",
            payload=payload,
        )
    if speech_contract_error:
        payload["delivery_repairable"] = True
        payload["delivery_failure_kind"] = speech_failure_kind
        payload.update(speech_metrics)
        failed_manifest = {
            "status": "failed",
            "failure_reason": speech_failure_kind,
            "design_spec_sha256": design_spec_sha256(spec),
            "design_spec_revision": int(ctx.state.get("spec_revision_count") or 0),
            "source_format": delivery_contract.source_format,
            "contract_path": "video_delivery_contract.json",
            **narration_artifacts,
            **speech_metrics,
        }
        if media_probe is not None:
            failed_manifest["media_probe_path"] = "media_probe.json"
            failed_manifest["media_probe"] = media_probe.model_dump(mode="json")
            failed_manifest["media_probe_sha256"] = sha256_file(
                proj_dir / "media_probe.json"
            )
        if mp4_path is not None:
            failed_manifest["mp4_path"] = str(mp4_path.relative_to(proj_dir))
            failed_manifest["mp4_sha256"] = sha256_file(mp4_path)
        failed_manifest["contract_sha256"] = sha256_file(
            proj_dir / "video_delivery_contract.json"
        )
        failed_manifest["narration_timing_sha256"] = sha256_file(
            proj_dir / narration_artifacts["narration_timing_path"]
        )
        failed_manifest_path = proj_dir / "delivery_manifest.json"
        failed_manifest_path.write_text(
            json.dumps(failed_manifest, indent=2) + "\n",
            encoding="utf-8",
        )
        payload["delivery_manifest_path"] = "delivery_manifest.json"
        return obs_error(
            speech_contract_error,
            category="validation",
            payload=payload,
        )
    if lint_ok is not True:
        return obs_error(
            f"HyperFrames lint failed: {lint_output or 'lint did not complete'}",
            category="validation",
            payload=payload,
        )
    if render_ok is not True or mp4_path is None or media_probe is None:
        return obs_error(
            f"HyperFrames render failed delivery validation: "
            f"{render_output or 'no fresh validated MP4 was produced'}",
            category="validation",
            payload=payload,
        )

    delivery_manifest = {
        "status": "passed",
        "design_spec_sha256": design_spec_sha256(spec),
        "design_spec_revision": int(ctx.state.get("spec_revision_count") or 0),
        "source_format": delivery_contract.source_format,
        "contract_path": "video_delivery_contract.json",
        "source_html_path": "index.html",
        "media_probe_path": "media_probe.json",
        "narration_audio_path": str(narration_audio_path.relative_to(proj_dir)),
        "mp4_path": str(mp4_path.relative_to(proj_dir)),
        "render_started_at": render_started_at,
        **speech_metrics,
        **narration_artifacts,
        "media_probe": media_probe.model_dump(mode="json"),
    }
    local_assets = authored_video_local_asset_paths(
        index_html_path.read_text(encoding="utf-8"),
        proj_dir,
    )
    delivery_manifest.update({
        "source_html_sha256": sha256_file(index_html_path),
        "contract_sha256": sha256_file(proj_dir / "video_delivery_contract.json"),
        "media_probe_sha256": sha256_file(proj_dir / "media_probe.json"),
        "mp4_sha256": sha256_file(mp4_path),
        "narration_audio_sha256": sha256_file(narration_audio_path),
        "transcript_sha256": sha256_file(proj_dir / narration_artifacts["transcript_path"]),
        "srt_sha256": sha256_file(proj_dir / narration_artifacts["srt_path"]),
        "vtt_sha256": sha256_file(proj_dir / narration_artifacts["vtt_path"]),
        "voice_metadata_sha256": sha256_file(
            proj_dir / narration_artifacts["voice_metadata_path"]
        ),
        "narration_timing_sha256": sha256_file(
            proj_dir / narration_artifacts["narration_timing_path"]
        ),
        "local_asset_sha256": {
            rel_path: sha256_file(path)
            for rel_path, path in sorted(local_assets.items())
        },
    })
    manifest_path = proj_dir / "delivery_manifest.json"
    manifest_path.write_text(
        json.dumps(delivery_manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    payload["delivery_manifest_path"] = "delivery_manifest.json"
    ctx.state["video_delivery"] = {
        "status": "passed",
        "project_dir": str(proj_dir),
        "manifest_path": str(manifest_path),
        "media_probe_path": str(proj_dir / "media_probe.json"),
        "mp4_path": str(mp4_path),
        "render_started_at": render_started_at,
        "design_spec_sha256": delivery_manifest["design_spec_sha256"],
        "design_spec_revision": delivery_manifest["design_spec_revision"],
        "delivery_manifest_sha256": sha256_file(manifest_path),
    }
    ctx.state["composition"] = CompositionArtifacts(
        html_path=str(index_html_path),
        preview_path=str(mp4_path),
        layer_manifest=[{
            "kind": "video",
            "path": str(mp4_path),
            "delivery_manifest_path": str(manifest_path),
        }],
    )
    ctx.state["last_composite_payload"] = {
        "artifact_type": "video",
        "render_mode": "html_first_video",
        "video_delivery_manifest_path": str(manifest_path),
        "mp4_path": str(mp4_path),
        "media_probe_path": str(proj_dir / "media_probe.json"),
    }
    return obs_ok(payload)


def _video_export_retry_phase_hook(
    phase: str,
    **_details: Any,
) -> None:
    """Deterministic test seam at retry delivery linearization boundaries."""


def _capture_retry_video_delivery_graph(
    run_dir: Path,
    project_dir: Path,
    expected_manifest: dict[str, Any],
    *,
    expected_spec_hash: str,
    expected_spec_revision: int,
) -> tuple[dict[str, object], tuple[SecureRunMemberSnapshot, ...]]:
    member_contract = (
        ("source_html_path", "source_html_sha256", False),
        ("contract_path", "contract_sha256", True),
        ("media_probe_path", "media_probe_sha256", True),
        ("mp4_path", "mp4_sha256", False),
        ("narration_audio_path", "narration_audio_sha256", False),
        ("transcript_path", "transcript_sha256", False),
        ("srt_path", "srt_sha256", False),
        ("vtt_path", "vtt_sha256", False),
        ("voice_metadata_path", "voice_metadata_sha256", False),
        ("narration_timing_path", "narration_timing_sha256", True),
    )

    with secure_run_member_access(run_dir) as accessor:
        snapshots: dict[Path, SecureRunMemberSnapshot] = {}

        def remember(snapshot: SecureRunMemberSnapshot) -> None:
            existing = snapshots.get(snapshot.relative_path)
            if existing is not None and existing != snapshot:
                raise ValueError(
                    f"{snapshot.relative_path.as_posix()} has inconsistent "
                    "validated snapshots"
                )
            snapshots.setdefault(snapshot.relative_path, snapshot)

        project_relative = accessor.validate_directory(
            project_dir,
            label="retry Video project directory",
        )
        manifest_snapshot = accessor.read_bytes(
            project_dir / "delivery_manifest.json",
            label="retry Video delivery manifest",
        )
        remember(manifest_snapshot)
        if manifest_snapshot.data is None:
            raise ValueError("delivery_manifest.json snapshot bytes were not retained")
        try:
            manifest = json.loads(manifest_snapshot.data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("delivery_manifest.json snapshot is not valid JSON") from exc
        if not isinstance(manifest, dict) or manifest != expected_manifest:
            raise ValueError(
                "delivery_manifest.json snapshot does not match the durable manifest"
            )

        spec_snapshot = accessor.read_bytes(
            Path("design_spec.json"),
            label="persisted DesignSpec",
        )
        remember(spec_snapshot)
        if spec_snapshot.data is None:
            raise ValueError("design_spec.json snapshot bytes were not retained")
        try:
            persisted_spec = json.loads(spec_snapshot.data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("design_spec.json snapshot is not valid JSON") from exc
        if not isinstance(persisted_spec, dict):
            raise ValueError("design_spec.json snapshot must contain an object")
        persisted_payload = persisted_spec.get("design_spec")
        persisted_hash = persisted_spec.get("design_spec_sha256")
        persisted_revision = persisted_spec.get("revision")
        if (
            not isinstance(persisted_payload, dict)
            or not isinstance(persisted_hash, str)
            or design_spec_sha256(persisted_payload) != persisted_hash
        ):
            raise ValueError("design_spec.json snapshot fingerprint is invalid")
        if (
            persisted_hash != expected_spec_hash
            or persisted_hash != manifest.get("design_spec_sha256")
        ):
            raise ValueError(
                "design_spec.json snapshot fingerprint does not match the manifest"
            )
        if (
            type(persisted_revision) is not int
            or persisted_revision != expected_spec_revision
            or persisted_revision != manifest.get("design_spec_revision")
        ):
            raise ValueError(
                "design_spec.json snapshot revision does not match the manifest"
            )

        for path_key, hash_key, capture in member_contract:
            member_value = manifest.get(path_key)
            if not isinstance(member_value, str) or not member_value:
                raise ValueError(f"delivery manifest is missing {path_key}")
            member_relative = accessor.member_relative_path(
                member_value,
                label=path_key,
                base=project_relative,
            )
            snapshot = (
                accessor.read_bytes(member_relative, label=path_key)
                if capture
                else accessor.digest(member_relative, label=path_key)
            )
            if snapshot.sha256 != manifest.get(hash_key):
                raise ValueError(
                    f"{snapshot.relative_path.as_posix()} snapshot hash mismatch "
                    f"against manifest-declared {hash_key}"
                )
            remember(snapshot)

        local_asset_hashes = manifest.get("local_asset_sha256")
        if not isinstance(local_asset_hashes, dict):
            raise ValueError("delivery manifest is missing local_asset_sha256")
        for relative_value, expected_hash in local_asset_hashes.items():
            if not isinstance(relative_value, str) or not relative_value:
                raise ValueError("delivery manifest local asset path is invalid")
            asset_relative = accessor.member_relative_path(
                relative_value,
                label=f"local asset {relative_value}",
                base=project_relative,
            )
            asset_snapshot = accessor.digest(
                asset_relative,
                label=f"local asset {relative_value}",
            )
            if asset_snapshot.sha256 != expected_hash:
                raise ValueError(
                    f"{asset_snapshot.relative_path.as_posix()} snapshot hash mismatch "
                    "against manifest-declared local_asset_sha256"
                )
            remember(asset_snapshot)

        pointer_payload: dict[str, object] = {
            "manifest_path": manifest_snapshot.relative_path.as_posix(),
            "manifest_sha256": manifest_snapshot.sha256,
            "design_spec_sha256": persisted_hash,
            "design_spec_revision": persisted_revision,
        }
        return pointer_payload, tuple(snapshots.values())


def retry_video_export_project(
    run_dir: Path,
    project_dir: Path,
    *,
    cancellation_token: CancellationToken | None = None,
    process_ledger: ProcessLedger | None = None,
) -> dict[str, Any]:
    """Retry only formal delivery for an already-authored HyperFrames project."""

    resolved_run_dir = run_dir.resolve()
    resolved_project_dir = project_dir.resolve()
    token = cancellation_token or CancellationToken.never(resolved_run_dir.name)
    ledger = process_ledger
    token.raise_if_cancelled("video.retry.start")
    if not resolved_project_dir.is_relative_to(resolved_run_dir):
        return {"ok": False, "phase": "validation", "error": "video project escapes run directory"}
    index_html_path = resolved_project_dir / "index.html"
    contract_path = resolved_project_dir / "video_delivery_contract.json"
    spec_path = resolved_run_dir / "design_spec.json"
    for required in (index_html_path, contract_path, spec_path):
        if not required.is_file():
            return {
                "ok": False,
                "phase": "validation",
                "error": f"required export retry input is missing: {required.name}",
            }

    try:
        contract = VideoDeliveryContract.model_validate(
            json.loads(contract_path.read_text(encoding="utf-8"))
        )
        spec_snapshot = json.loads(spec_path.read_text(encoding="utf-8"))
        spec = spec_snapshot["design_spec"]
        spec_hash = design_spec_sha256(spec)
        if spec_snapshot.get("design_spec_sha256") != spec_hash:
            raise ValueError("design_spec.json fingerprint is invalid")
        spec_revision = int(spec_snapshot.get("revision") or 0)
        authored_html = index_html_path.read_text(encoding="utf-8")
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        return {"ok": False, "phase": "validation", "error": str(exc)}

    scene_manifest = [
        scene.model_dump(mode="json")
        for scene in contract.scenes
    ]
    html_errors = validate_authored_video_html(
        authored_html,
        contract,
        project_dir=resolved_project_dir,
    )
    if html_errors:
        return {
            "ok": False,
            "phase": "validation",
            "error": "authored video project is invalid: " + "; ".join(html_errors),
        }

    try:
        pointer_update = update_video_delivery_pointer(
            resolved_run_dir,
            mode="invalidate_if_present",
            reason="video_export_retry",
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return {
            "ok": False,
            "phase": "final_pointer",
            "error": f"Video delivery pointer invalidation failed: {exc}",
        }
    pointer_cleanup_warnings = _stable_pointer_cleanup_warnings(
        pointer_update.cleanup_warnings
    )

    def retry_result(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            **payload,
            "pointer_cleanup_warnings": list(pointer_cleanup_warnings),
        }

    with _pointer_cleanup_cancellation_scope(pointer_cleanup_warnings):
        token.raise_if_cancelled("video.retry.after_delivery_invalidation")

    for stale in (
        resolved_project_dir / "delivery_manifest.json",
        resolved_project_dir / "media_probe.json",
        resolved_project_dir / "assets" / "narration.wav",
    ):
        stale.unlink(missing_ok=True)
    for stale_mp4 in (resolved_project_dir / "renders").glob("*.mp4"):
        stale_mp4.unlink(missing_ok=True)

    narration_artifacts = _write_narration_artifacts(
        resolved_project_dir,
        scene_manifest,
        contract.voice_preset,
    )
    with _pointer_cleanup_cancellation_scope(pointer_cleanup_warnings):
        authoring_lint_output, authoring_lint_ok = _run_hyperframes_authoring_lint(
            resolved_project_dir,
            process_ledger=ledger,
            cancellation_token=token,
        )
    if not authoring_lint_ok:
        return retry_result({
            "ok": False,
            "phase": "authoring_lint",
            "error": authoring_lint_output or "HyperFrames authoring lint failed",
        })

    with _pointer_cleanup_cancellation_scope(pointer_cleanup_warnings):
        tts_output, tts_ok, narration_audio_path, speech_timing = (
            _synthesize_timed_narration(
                resolved_project_dir,
                scene_manifest=scene_manifest,
                voice_id=contract.voice.kokoro_voice_id,
                target_duration_s=float(contract.target_duration_s),
                process_ledger=ledger,
                cancellation_token=token,
            )
        )
    if not tts_ok or narration_audio_path is None:
        return retry_result({
            "ok": False,
            "phase": "tts",
            "error": tts_output or "Kokoro narration synthesis failed",
        })
    speech_metrics, speech_error = _speech_delivery_metrics(
        scene_manifest,
        speech_timing,
        target_duration_s=float(contract.target_duration_s),
    )
    if speech_error:
        return retry_result({
            "ok": False,
            "phase": "tts",
            "error": speech_error,
            **speech_metrics,
        })
    try:
        narration_artifacts = _write_narration_artifacts(
            resolved_project_dir,
            scene_manifest,
            contract.voice_preset,
            speech_timing=speech_timing,
        )
    except _SubtitleReadabilityError as exc:
        return retry_result({
            "ok": False,
            "phase": "subtitles",
            "error": str(exc),
            "subtitle_diagnostics": exc.diagnostics,
        })
    except ValueError as exc:
        return retry_result(
            {"ok": False, "phase": "subtitles", "error": str(exc)}
        )

    voice_metadata_path = (
        resolved_project_dir / narration_artifacts["voice_metadata_path"]
    )
    voice_metadata = json.loads(voice_metadata_path.read_text(encoding="utf-8"))
    voice_metadata["audio_path"] = str(
        narration_audio_path.relative_to(resolved_project_dir)
    )
    voice_metadata["timing_path"] = narration_artifacts["narration_timing_path"]
    voice_metadata_path.write_text(
        json.dumps(voice_metadata, indent=2) + "\n",
        encoding="utf-8",
    )
    with _pointer_cleanup_cancellation_scope(pointer_cleanup_warnings):
        lint_output, lint_ok = _run_hyperframes_lint(
            resolved_project_dir,
            process_ledger=ledger,
            cancellation_token=token,
        )
    if not lint_ok:
        return retry_result({
            "ok": False,
            "phase": "lint",
            "error": lint_output or "HyperFrames lint failed",
        })

    video_id = str(
        json.loads((resolved_project_dir / "meta.json").read_text(encoding="utf-8")).get("id")
        if (resolved_project_dir / "meta.json").is_file()
        else resolved_project_dir.name.removeprefix("hyperframes-")
    )
    render_started_at = datetime.now(timezone.utc).isoformat()
    with _pointer_cleanup_cancellation_scope(pointer_cleanup_warnings):
        render_output, render_ok, mp4_path, media_probe = _run_hyperframes_render(
            resolved_project_dir,
            _slugify(video_id),
            render_started_ns=time.time_ns(),
            process_ledger=ledger,
            cancellation_token=token,
        )
    if not render_ok or mp4_path is None or media_probe is None:
        return retry_result({
            "ok": False,
            "phase": "render",
            "error": render_output or "HyperFrames render did not produce a validated MP4",
        })
    with _pointer_cleanup_cancellation_scope(pointer_cleanup_warnings):
        (
            subtitle_track_output,
            subtitle_track_ok,
            captioned_mp4_path,
            captioned_media_probe,
        ) = _prepare_captioned_delivery_mp4(
            mp4_path,
            resolved_project_dir / narration_artifacts["srt_path"],
            process_ledger=ledger,
            cancellation_token=token,
        )
    if (
        not subtitle_track_ok
        or captioned_mp4_path is None
        or captioned_media_probe is None
    ):
        return retry_result({
            "ok": False,
            "phase": "render",
            "error": subtitle_track_output
            or "MP4 subtitle-track packaging did not produce a validated delivery",
        })
    mp4_path = captioned_mp4_path
    media_probe = captioned_media_probe
    mp4_path = mp4_path.resolve()
    with _pointer_cleanup_cancellation_scope(pointer_cleanup_warnings):
        token.raise_if_cancelled("video.retry.before_delivery")

    authored_timeline_s = max(
        float(scene["start_s"]) + float(scene["duration_s"])
        for scene in scene_manifest
    )
    observed_duration_s = float(media_probe.duration_s)
    duration_contract_error = _rendered_duration_contract_error(
        observed_duration_s=observed_duration_s,
        authored_timeline_s=authored_timeline_s,
        selected_target_duration_s=float(contract.target_duration_s),
    )
    if duration_contract_error:
        return retry_result({
            "ok": False,
            "phase": "render",
            "error": duration_contract_error,
        })
    speech_metrics, speech_error = _speech_delivery_metrics(
        scene_manifest,
        speech_timing,
        target_duration_s=observed_duration_s,
    )
    if speech_error:
        return retry_result({
            "ok": False,
            "phase": "delivery",
            "error": speech_error,
            **speech_metrics,
        })
    speech_metrics["authored_timeline_duration_s"] = authored_timeline_s

    probe_payload = media_probe.model_dump(mode="json")
    media_probe_path = resolved_project_dir / "media_probe.json"
    media_probe_path.write_text(
        json.dumps(probe_payload, indent=2) + "\n",
        encoding="utf-8",
    )
    local_assets = authored_video_local_asset_paths(
        authored_html,
        resolved_project_dir,
    )
    delivery_manifest = {
        "status": "passed",
        "design_spec_sha256": spec_hash,
        "design_spec_revision": spec_revision,
        "source_format": contract.source_format,
        "contract_path": contract_path.name,
        "source_html_path": index_html_path.name,
        "media_probe_path": media_probe_path.name,
        "narration_audio_path": str(
            narration_audio_path.relative_to(resolved_project_dir)
        ),
        "mp4_path": str(mp4_path.relative_to(resolved_project_dir)),
        "render_started_at": render_started_at,
        **speech_metrics,
        **narration_artifacts,
        "media_probe": probe_payload,
        "source_html_sha256": sha256_file(index_html_path),
        "contract_sha256": sha256_file(contract_path),
        "media_probe_sha256": sha256_file(media_probe_path),
        "mp4_sha256": sha256_file(mp4_path),
        "narration_audio_sha256": sha256_file(narration_audio_path),
        "transcript_sha256": sha256_file(
            resolved_project_dir / narration_artifacts["transcript_path"]
        ),
        "srt_sha256": sha256_file(
            resolved_project_dir / narration_artifacts["srt_path"]
        ),
        "vtt_sha256": sha256_file(
            resolved_project_dir / narration_artifacts["vtt_path"]
        ),
        "voice_metadata_sha256": sha256_file(voice_metadata_path),
        "narration_timing_sha256": sha256_file(
            resolved_project_dir / narration_artifacts["narration_timing_path"]
        ),
        "local_asset_sha256": {
            rel_path: sha256_file(path)
            for rel_path, path in sorted(local_assets.items())
        },
    }
    manifest_path = resolved_project_dir / "delivery_manifest.json"
    atomic_write_json(manifest_path, delivery_manifest)
    try:
        pointer_payload, expected_snapshots = _capture_retry_video_delivery_graph(
            resolved_run_dir,
            resolved_project_dir,
            delivery_manifest,
            expected_spec_hash=spec_hash,
            expected_spec_revision=spec_revision,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return retry_result({
            "ok": False,
            "phase": "final_pointer",
            "error": f"Video delivery snapshot validation failed: {exc}",
        })
    manifest_sha256 = str(pointer_payload["manifest_sha256"])
    with _pointer_cleanup_cancellation_scope(pointer_cleanup_warnings):
        _video_export_retry_phase_hook(
            "manifest_durable_before_pointer",
            run_dir=resolved_run_dir,
            manifest_path=manifest_path,
            manifest_sha256=manifest_sha256,
        )
        token.raise_if_cancelled("video.retry.before_final_pointer")
    try:
        with _pointer_cleanup_cancellation_scope(pointer_cleanup_warnings):
            pointer_update = update_video_delivery_pointer(
                resolved_run_dir,
                mode="publish",
                payload=pointer_payload,
                expected_snapshots=expected_snapshots,
            )
    except (OSError, RuntimeError, ValueError) as exc:
        return retry_result({
            "ok": False,
            "phase": "final_pointer",
            "error": f"Video delivery pointer publication failed: {exc}",
        })
    pointer_cleanup_warnings = _stable_pointer_cleanup_warnings(
        pointer_cleanup_warnings,
        pointer_update.cleanup_warnings,
    )
    with _pointer_cleanup_cancellation_scope(pointer_cleanup_warnings):
        token.raise_if_cancelled("video.retry.after_final_pointer")
    log(
        "export_video.retry.done",
        project_dir=str(resolved_project_dir),
        mp4=str(mp4_path),
        pointer_cleanup_warnings=list(pointer_cleanup_warnings),
    )
    return retry_result({
        "ok": True,
        "phase": "done",
        "project_dir": str(resolved_project_dir),
        "manifest_path": str(manifest_path),
        "mp4_path": str(mp4_path),
        "media_probe_path": str(media_probe_path),
        "render_started_at": render_started_at,
    })


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_kokoro_tts(
    proj_dir: Path,
    *,
    transcript_path: Path,
    voice_id: str,
    output_path: Path | None = None,
    speed: float = 1.0,
    process_ledger: ProcessLedger | None = None,
    cancellation_token: CancellationToken | None = None,
) -> tuple[str, bool, Path | None]:
    """Synthesize one fresh local Kokoro WAV."""
    output_path = output_path or (proj_dir / "assets" / "narration.wav")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    started_ns = time.time_ns()
    attempt_output_path = output_path.with_name(
        f".{output_path.stem}-{started_ns}{output_path.suffix}"
    )
    try:
        proc = _run_video_process(
            _hyperframes_command(
                "tts",
                str(transcript_path.relative_to(proj_dir)),
                "--output", str(attempt_output_path.relative_to(proj_dir)),
                "--voice", voice_id,
                "--lang", "en-us",
                "--speed", f"{speed:.2f}",
                "--json",
            ),
            cwd=proj_dir,
            env=_hyperframes_subprocess_env(),
            timeout=900,
            role="video-tts",
            process_ledger=process_ledger,
            cancellation_token=cancellation_token,
        )
    except FileNotFoundError:
        return "pinned HyperFrames/Kokoro CLI is unavailable", False, None
    except subprocess.TimeoutExpired:
        return "Kokoro narration synthesis timed out after 900 s", False, None
    except Exception as exc:
        return f"Kokoro narration synthesis failed: {type(exc).__name__}: {exc}", False, None

    combined = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0 or not attempt_output_path.is_file():
        attempt_output_path.unlink(missing_ok=True)
        return combined or f"Kokoro TTS exited {proc.returncode}", False, None
    runtime_error = _verify_kokoro_runtime_assets()
    if runtime_error:
        attempt_output_path.unlink(missing_ok=True)
        return runtime_error, False, None
    current_state = (
        attempt_output_path.stat().st_mtime_ns,
        attempt_output_path.stat().st_size,
    )
    if (
        current_state[1] <= 0
        or current_state[0] + FRESH_OUTPUT_MTIME_TOLERANCE_NS < started_ns
    ):
        attempt_output_path.unlink(missing_ok=True)
        return "Kokoro TTS did not produce fresh non-empty narration audio", False, None
    attempt_output_path.replace(output_path)
    return combined, True, output_path


def _verify_kokoro_runtime_assets(cache_root: Path | None = None) -> str | None:
    """Bind local TTS to the model and voice blobs audited for this release."""
    root = cache_root or (Path.home() / ".cache" / "hyperframes" / "tts")
    for relative_path, expected_hash in _KOKORO_CACHE_ASSETS.items():
        path = root / relative_path
        if not path.is_file():
            return f"Kokoro runtime asset is missing after synthesis: {relative_path}"
        if sha256_file(path) != expected_hash:
            return f"Kokoro runtime asset checksum mismatch: {relative_path}"
    return None


def _probe_audio_duration(
    audio_path: Path,
    *,
    process_ledger: ProcessLedger | None = None,
    cancellation_token: CancellationToken | None = None,
) -> tuple[float | None, str | None]:
    try:
        proc = _run_video_process(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path),
            ],
            timeout=30,
            role="video-audio-probe",
            process_ledger=process_ledger,
            cancellation_token=cancellation_token,
        )
    except FileNotFoundError:
        return None, "ffprobe not found; narration duration cannot be measured"
    except subprocess.TimeoutExpired:
        return None, "ffprobe timed out while measuring narration"
    except Exception as exc:
        return None, f"ffprobe narration probe failed: {type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return None, f"ffprobe narration probe exited {proc.returncode}: {output}"
    try:
        duration_s = float((proc.stdout or "").strip())
    except ValueError:
        return None, f"ffprobe returned invalid narration duration: {proc.stdout!r}"
    if duration_s <= 0:
        return None, "narration duration must be positive"
    return duration_s, None


def _build_timed_narration_mix(
    proj_dir: Path,
    *,
    segments: list[dict[str, Any]],
    target_duration_s: float,
    output_path: Path,
    process_ledger: ProcessLedger | None = None,
    cancellation_token: CancellationToken | None = None,
) -> tuple[str, bool]:
    """Place each speech WAV at its scene start on a full-length silent bed."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    attempt_path = output_path.with_name(
        f".{output_path.stem}-mix-{time.time_ns()}{output_path.suffix}"
    )
    command = [
        "ffmpeg", "-v", "error", "-f", "lavfi", "-t", f"{target_duration_s:g}",
        "-i", "anullsrc=r=24000:cl=mono",
    ]
    filter_parts: list[str] = []
    mix_inputs = ["[0:a]"]
    for index, segment in enumerate(segments, start=1):
        path = Path(segment["path"])
        command.extend(["-i", str(path)])
        delay_ms = max(0, int(round(float(segment["start_s"]) * 1000)))
        label = f"speech{index}"
        filter_parts.append(f"[{index}:a]adelay={delay_ms}:all=1[{label}]")
        mix_inputs.append(f"[{label}]")
    filter_parts.append(
        "".join(mix_inputs)
        + f"amix=inputs={len(mix_inputs)}:duration=first:dropout_transition=0,"
        + f"atrim=duration={target_duration_s:g}[narration]"
    )
    command.extend([
        "-filter_complex", ";".join(filter_parts),
        "-map", "[narration]", "-ar", "24000", "-ac", "1",
        "-c:a", "pcm_s16le", "-y", str(attempt_path),
    ])
    try:
        proc = _run_video_process(
            command,
            cwd=proj_dir,
            env=_hyperframes_subprocess_env(),
            timeout=300,
            role="video-narration-mix",
            process_ledger=process_ledger,
            cancellation_token=cancellation_token,
        )
    except FileNotFoundError:
        return "ffmpeg not found; timed narration mix could not be built", False
    except subprocess.TimeoutExpired:
        return "ffmpeg timed narration mix timed out after 300 s", False
    except Exception as exc:
        return f"ffmpeg narration mix failed: {type(exc).__name__}: {exc}", False
    combined = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0 or not attempt_path.is_file() or attempt_path.stat().st_size <= 0:
        attempt_path.unlink(missing_ok=True)
        return combined or f"ffmpeg narration mix exited {proc.returncode}", False
    duration_s, probe_error = _probe_audio_duration(
        attempt_path,
        process_ledger=process_ledger,
        cancellation_token=cancellation_token,
    )
    if probe_error or duration_s is None or abs(duration_s - target_duration_s) > 0.05:
        attempt_path.unlink(missing_ok=True)
        return probe_error or (
            f"timed narration mix duration {duration_s} did not match {target_duration_s}"
        ), False
    attempt_path.replace(output_path)
    return combined, True


def _synthesize_timed_narration(
    proj_dir: Path,
    *,
    scene_manifest: list[dict[str, Any]],
    voice_id: str,
    target_duration_s: float,
    process_ledger: ProcessLedger | None = None,
    cancellation_token: CancellationToken | None = None,
) -> tuple[str, bool, Path | None, list[dict[str, Any]]]:
    """Synthesize, measure, conservatively fit, and place speech per scene."""
    scene_dir = proj_dir / "narration" / "scenes"
    scene_dir.mkdir(parents=True, exist_ok=True)
    output_lines: list[str] = []
    segments: list[dict[str, Any]] = []
    timings: list[dict[str, Any]] = []
    unfit_scenes: list[dict[str, float | str]] = []
    for index, scene in enumerate(scene_manifest, start=1):
        scene_id = str(scene["scene_id"])
        transcript_path = scene_dir / f"{index:02d}-{_slugify(scene_id)}.txt"
        audio_path = scene_dir / f"{index:02d}-{_slugify(scene_id)}.wav"
        transcript_path.write_text(
            " ".join(str(scene["narration_text"]).split()) + "\n",
            encoding="utf-8",
        )
        output, ok, produced = _run_kokoro_tts(
            proj_dir,
            transcript_path=transcript_path,
            voice_id=voice_id,
            output_path=audio_path,
            speed=1.0,
            process_ledger=process_ledger,
            cancellation_token=cancellation_token,
        )
        output_lines.append(f"{scene_id} speed=1.00: {output}")
        if not ok or produced is None:
            return "\n".join(output_lines), False, None, []
        measured_s, probe_error = _probe_audio_duration(
            produced,
            process_ledger=process_ledger,
            cancellation_token=cancellation_token,
        )
        if probe_error or measured_s is None:
            output_lines.append(f"{scene_id}: {probe_error}")
            return "\n".join(output_lines), False, None, []

        available_s = float(scene["duration_s"]) - SCENE_SPEECH_END_MARGIN_S
        speed = 1.0
        refit_attempts = 0
        scene_unfit = False
        while measured_s > available_s:
            if (
                speed >= MAX_DELIVERY_TTS_SPEED
                or refit_attempts >= MAX_TTS_REFIT_ATTEMPTS
            ):
                unfit_scenes.append({
                    "scene_id": scene_id,
                    "measured_s": measured_s,
                    "available_s": available_s,
                    "final_speed": speed,
                })
                scene_unfit = True
                break
            projected_speed = math.ceil(
                (speed * measured_s / available_s) * 1.02 * 100
            ) / 100
            projected_speed = max(projected_speed, speed + 0.01)
            speed_limit = (
                MAX_CONSERVATIVE_TTS_SPEED
                if speed < MAX_CONSERVATIVE_TTS_SPEED
                else MAX_DELIVERY_TTS_SPEED
            )
            speed = min(projected_speed, speed_limit)
            refit_attempts += 1
            output, ok, produced = _run_kokoro_tts(
                proj_dir,
                transcript_path=transcript_path,
                voice_id=voice_id,
                output_path=audio_path,
                speed=speed,
                process_ledger=process_ledger,
                cancellation_token=cancellation_token,
            )
            output_lines.append(f"{scene_id} speed={speed:.2f}: {output}")
            if not ok or produced is None:
                return "\n".join(output_lines), False, None, []
            measured_s, probe_error = _probe_audio_duration(
                produced,
                process_ledger=process_ledger,
                cancellation_token=cancellation_token,
            )
            if probe_error or measured_s is None:
                output_lines.append(f"{scene_id}: {probe_error}")
                return "\n".join(output_lines), False, None, []
            if measured_s > available_s + 0.05:
                continue
            break

        if scene_unfit:
            continue

        start_s = float(scene["start_s"])
        segments.append({"path": produced, "start_s": start_s})
        timings.append({
            "scene_id": scene_id,
            "start_s": start_s,
            "speech_duration_s": measured_s,
            "end_s": start_s + measured_s,
            "speed": speed,
        })

    if unfit_scenes:
        timing_summary = "; ".join(
            f"scene={scene['scene_id']} measured={scene['measured_s']:.3f}s "
            f"available={scene['available_s']:.3f}s "
            f"max_speed={MAX_DELIVERY_TTS_SPEED:.2f} "
            f"final_speed={scene['final_speed']:.2f}"
            for scene in unfit_scenes
        )
        return (
            "narration_timing_unfit " + timing_summary + "\n" + "\n".join(output_lines),
            False,
            None,
            [],
        )

    output_path = proj_dir / "assets" / "narration.wav"
    mix_output, mix_ok = _build_timed_narration_mix(
        proj_dir,
        segments=segments,
        target_duration_s=target_duration_s,
        output_path=output_path,
        process_ledger=process_ledger,
        cancellation_token=cancellation_token,
    )
    output_lines.append(mix_output)
    if not mix_ok:
        return "\n".join(output_lines), False, None, []
    return "\n".join(output_lines), True, output_path, timings


def _run_hyperframes_lint(
    proj_dir: Path,
    *,
    process_ledger: ProcessLedger | None = None,
    cancellation_token: CancellationToken | None = None,
) -> tuple[str, bool]:
    """Run the repository-pinned HyperFrames lint inside `proj_dir`.

    Returns ``(stdout_combined, ok)`` where ``ok`` is True when the exit
    code is 0 (no errors). Missing tooling and execution failures are hard
    failures because an unlinted composition is not deliverable.
    """
    try:
        proc = _run_video_process(
            _hyperframes_command("lint"),
            cwd=proj_dir,
            env=_hyperframes_subprocess_env(),
            timeout=60,
            role="video-lint",
            process_ledger=process_ledger,
            cancellation_token=cancellation_token,
        )
        combined = (proc.stdout or "") + (proc.stderr or "")
        ok = proc.returncode == 0
        log(
            "export_video.lint",
            returncode=proc.returncode,
            ok=ok,
            output_chars=len(combined),
        )
        return combined.strip(), ok
    except FileNotFoundError:
        log("export_video.lint.skip", reason="hyperframes_cli_missing")
        return "pinned HyperFrames CLI is missing; run `autodesign setup`", False
    except subprocess.TimeoutExpired:
        log("export_video.lint.timeout")
        return "lint timed out after 60 s", False
    except Exception as e:
        log("export_video.lint.error", error=f"{type(e).__name__}: {e}")
        return str(e), False


def _run_hyperframes_authoring_lint(
    proj_dir: Path,
    *,
    process_ledger: ProcessLedger | None = None,
    cancellation_token: CancellationToken | None = None,
) -> tuple[str, bool]:
    """Lint authored structure while isolating pipeline-generated narration.

    HyperFrames' full lint requires every referenced audio file to exist, while
    AutoDesign intentionally authors the narration reference before local TTS
    produces the file. A short valid WAV exists only for this preflight; the
    final lint still runs against the real synthesized narration before render.
    """
    narration_path = proj_dir / "assets" / "narration.wav"
    created_placeholder = not narration_path.is_file()
    try:
        if created_placeholder:
            narration_path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(narration_path), "wb") as placeholder:
                placeholder.setnchannels(1)
                placeholder.setsampwidth(2)
                placeholder.setframerate(16_000)
                placeholder.writeframes(b"\0\0" * 1_600)
        output, ok = _run_hyperframes_lint(
            proj_dir,
            process_ledger=process_ledger,
            cancellation_token=cancellation_token,
        )
        log(
            "export_video.authoring_lint",
            ok=ok,
            used_generated_audio_placeholder=created_placeholder,
        )
        return output, ok
    except OSError as exc:
        log(
            "export_video.authoring_lint.error",
            error=f"{type(exc).__name__}: {exc}",
        )
        return f"could not stage generated narration for authoring lint: {exc}", False
    finally:
        if created_placeholder:
            narration_path.unlink(missing_ok=True)


def _mux_optional_subtitle_track(
    mp4_path: Path,
    subtitle_path: Path,
    *,
    process_ledger: ProcessLedger | None = None,
    cancellation_token: CancellationToken | None = None,
) -> tuple[str, bool, Path | None]:
    """Attach an optional English subtitle track without changing pixels."""
    if not mp4_path.is_file():
        return f"rendered MP4 is missing: {mp4_path}", False, None
    if not subtitle_path.is_file():
        return f"subtitle file is missing: {subtitle_path}", False, None

    captioned_mp4 = mp4_path.with_name(f"{mp4_path.stem}-captions.mp4")
    captioned_mp4.unlink(missing_ok=True)
    try:
        proc = _run_video_process(
            [
                "ffmpeg",
                "-y",
                "-i", str(mp4_path),
                "-i", str(subtitle_path),
                "-map", "0:v:0",
                "-map", "0:a?",
                "-map", "1:0",
                "-c:v", "copy",
                "-c:a", "copy",
                "-c:s", "mov_text",
                "-metadata:s:s:0", "language=eng",
                "-metadata:s:s:0", "title=English",
                "-movflags", "+faststart",
                str(captioned_mp4),
            ],
            timeout=120,
            role="video-subtitle-mux",
            process_ledger=process_ledger,
            cancellation_token=cancellation_token,
        )
    except FileNotFoundError:
        return "ffmpeg not found; optional MP4 subtitles cannot be packaged", False, None
    except subprocess.TimeoutExpired:
        return "subtitle-track mux timed out after 120 s", False, None
    except Exception as exc:
        return f"subtitle-track mux failed: {type(exc).__name__}: {exc}", False, None

    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0 or not captioned_mp4.is_file() or captioned_mp4.stat().st_size <= 0:
        captioned_mp4.unlink(missing_ok=True)
        return output or "ffmpeg did not produce a captioned MP4", False, None
    log(
        "export_video.subtitle_track",
        path=str(captioned_mp4),
        subtitle_path=str(subtitle_path),
        size=captioned_mp4.stat().st_size,
    )
    return output, True, captioned_mp4


def _prepare_captioned_delivery_mp4(
    mp4_path: Path,
    subtitle_path: Path,
    *,
    process_ledger: ProcessLedger | None = None,
    cancellation_token: CancellationToken | None = None,
) -> tuple[str, bool, Path | None, VideoMediaProbe | None]:
    """Package and verify the optional subtitle track for a delivered MP4."""
    output, ok, captioned_mp4 = _mux_optional_subtitle_track(
        mp4_path,
        subtitle_path,
        process_ledger=process_ledger,
        cancellation_token=cancellation_token,
    )
    if not ok or captioned_mp4 is None:
        return output, False, None, None

    probe, probe_error = _probe_video(
        captioned_mp4,
        process_ledger=process_ledger,
        cancellation_token=cancellation_token,
    )
    if probe_error:
        return "\n".join(part for part in (output, probe_error) if part), False, None, None
    if probe is None or probe.subtitle_codec != "mov_text" or probe.subtitle_forced:
        return (
            "\n".join(
                part
                for part in (
                    output,
                    "captioned MP4 is missing a selectable non-forced mov_text subtitle track",
                )
                if part
            ),
            False,
            None,
            None,
        )
    return output, True, captioned_mp4, probe


def _run_hyperframes_render(
    proj_dir: Path,
    video_id: str,
    *,
    render_started_ns: int | None = None,
    process_ledger: ProcessLedger | None = None,
    cancellation_token: CancellationToken | None = None,
) -> tuple[str, bool, Path | None, VideoMediaProbe | None]:
    """Run the repository-pinned HyperFrames renderer inside `proj_dir`.

    Returns command output, acceptance status, fresh MP4 path, and normalized
    ffprobe evidence. A zero render exit code alone is never success.

    The render command writes the MP4 inside `renders/` by default.
    We accept only a non-empty MP4 whose modification time is at or after the
    tracked render start and whose probed media contract is exact.
    """
    started_ns = render_started_ns if render_started_ns is not None else time.time_ns()
    expected_mp4_path = proj_dir / "renders" / f"{video_id}-{started_ns}.mp4"
    expected_mp4_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = _run_video_process(
            _hyperframes_command(
                "render",
                "--fps", "30",
                "--resolution", "landscape",
                "--strict",
                "--no-best-effort",
                "--output", str(expected_mp4_path.relative_to(proj_dir)),
                ".",
            ),
            cwd=proj_dir,
            env=_hyperframes_subprocess_env(),
            timeout=1800,
            role="video-render",
            process_ledger=process_ledger,
            cancellation_token=cancellation_token,
        )
        combined = (proc.stdout or "") + (proc.stderr or "")
        ok = proc.returncode == 0
        log(
            "export_video.render",
            returncode=proc.returncode,
            ok=ok,
            output_chars=len(combined),
        )

        mp4_path: Path | None = None
        if ok and expected_mp4_path.is_file():
            current_state = (
                expected_mp4_path.stat().st_mtime_ns,
                expected_mp4_path.stat().st_size,
            )
            if (
                current_state[1] > 0
                and current_state[0] + FRESH_OUTPUT_MTIME_TOLERANCE_NS >= started_ns
            ):
                mp4_path = expected_mp4_path
                log(
                    "export_video.render.mp4",
                    path=str(mp4_path),
                    size=current_state[1],
                )
        if ok and mp4_path is None:
            log(
                "export_video.render.mp4_missing",
                reason="render succeeded but expected output was not fresh",
                expected_path=str(expected_mp4_path),
            )

        if not ok:
            return combined.strip(), False, None, None
        if mp4_path is None:
            message = "render exited zero but no fresh non-empty MP4 was produced"
            return "\n".join(part for part in (combined.strip(), message) if part), False, None, None

        probe, probe_error = _probe_video(
            mp4_path,
            process_ledger=process_ledger,
            cancellation_token=cancellation_token,
        )
        if probe_error:
            return (
                "\n".join(part for part in (combined.strip(), probe_error) if part),
                False,
                None,
                None,
            )
        return combined.strip(), True, mp4_path, probe

    except FileNotFoundError:
        log("export_video.render.skip", reason="hyperframes_cli_missing")
        return (
            "pinned HyperFrames CLI is missing; run `autodesign setup`",
            False,
            None,
            None,
        )
    except subprocess.TimeoutExpired:
        log("export_video.render.timeout")
        return "render timed out after 1800 s", False, None, None
    except Exception as e:
        log("export_video.render.error", error=f"{type(e).__name__}: {e}")
        return str(e), False, None, None


def _probe_video(
    mp4_path: Path,
    *,
    process_ledger: ProcessLedger | None = None,
    cancellation_token: CancellationToken | None = None,
) -> tuple[VideoMediaProbe | None, str | None]:
    """Validate the final MP4 with ffprobe and return normalized evidence."""
    try:
        proc = _run_video_process(
            [
                "ffprobe",
                "-v", "error",
                "-count_frames",
                "-show_streams",
                "-show_format",
                "-of", "json",
                str(mp4_path),
            ],
            timeout=30,
            role="video-media-probe",
            process_ledger=process_ledger,
            cancellation_token=cancellation_token,
        )
    except FileNotFoundError:
        return None, "ffprobe not found; MP4 delivery cannot be validated"
    except subprocess.TimeoutExpired:
        return None, "ffprobe timed out after 30 s"
    except Exception as exc:
        return None, f"ffprobe failed: {type(exc).__name__}: {exc}"

    if proc.returncode != 0:
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return None, f"ffprobe exited {proc.returncode}: {output}"
    try:
        raw = json.loads(proc.stdout or "{}")
        streams = raw.get("streams") if isinstance(raw, dict) else None
        streams = streams if isinstance(streams, list) else []
        video = next(
            stream for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == "video"
        )
        audio = next(
            stream for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == "audio"
        )
        subtitle = next(
            (
                stream for stream in streams
                if isinstance(stream, dict) and stream.get("codec_type") == "subtitle"
            ),
            None,
        )
        average_fps = Fraction(str(video.get("avg_frame_rate")))
        nominal_fps = Fraction(str(video.get("r_frame_rate")))
        if average_fps != 30 or nominal_fps != 30:
            raise ValueError(
                f"avg_frame_rate={average_fps}, r_frame_rate={nominal_fps}"
            )
        duration_s = float((raw.get("format") or {}).get("duration"))
        video_duration_s = float(video.get("duration"))
        audio_duration_s = float(audio.get("duration"))
        if video_duration_s < duration_s - 0.5:
            raise ValueError(
                f"video stream duration {video_duration_s:.3f}s is shorter than "
                f"container duration {duration_s:.3f}s"
            )
        if audio_duration_s < duration_s - 0.5:
            raise ValueError(
                f"audio stream duration {audio_duration_s:.3f}s is shorter than "
                f"container duration {duration_s:.3f}s"
            )
        frame_count = int(video.get("nb_read_frames") or video.get("nb_frames"))
        minimum_frames = math.floor(duration_s * 30) - 2
        if frame_count < minimum_frames:
            raise ValueError(
                f"video frame count {frame_count} is below expected {minimum_frames}"
            )
        observed = {
            "video_codec": video.get("codec_name"),
            "pixel_format": video.get("pix_fmt"),
            "audio_codec": audio.get("codec_name"),
            "width": video.get("width"),
            "height": video.get("height"),
            "fps": 30,
            "duration_s": duration_s,
            "video_stream_duration_s": video_duration_s,
            "audio_stream_duration_s": audio_duration_s,
            "video_frame_count": frame_count,
            "subtitle_codec": subtitle.get("codec_name") if subtitle else None,
            "subtitle_forced": (
                bool((subtitle.get("disposition") or {}).get("forced"))
                if subtitle
                else None
            ),
        }
        probe = VideoMediaProbe.model_validate(observed)
    except Exception as exc:
        return (
            None,
            "ffprobe media contract failed; expected "
            "H.264/yuv420p/AAC/1920x1080/30fps/300-600s: "
            f"{exc}",
        )
    return probe, None
