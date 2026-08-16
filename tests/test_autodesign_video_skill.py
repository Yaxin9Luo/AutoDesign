from __future__ import annotations

import contextlib
import importlib.util
import hashlib
import html
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "agent_skills" / "autodesign-video"
HARNESS_PATH = SKILL_ROOT / "scripts" / "video_harness.py"
SETUP_PATH = SKILL_ROOT / "scripts" / "setup_video.py"


def _load_script(name: str, path: Path):
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _plan(*, scene_count: int = 12, duration_s: int = 360) -> dict[str, object]:
    base_duration = duration_s // scene_count
    remainder = duration_s - (base_duration * scene_count)
    cursor = 0
    scenes: list[dict[str, object]] = []
    roles = (
        "opening", "problem", "context", "method", "method", "method",
        "results", "results", "analysis", "limitations", "implications", "closing",
    )
    for index in range(scene_count):
        scene_duration = base_duration + (1 if index < remainder else 0)
        scene_id = f"scene_{index + 1:02d}"
        narration = (
            f"Scene {index + 1} explains one source-grounded part of the paper. "
            "The narration names the research question, method, evidence, and boundary "
            "in complete English sentences so the conference audience can follow the "
            "argument without reading dense text from the screen. "
        )
        scenes.append(
            {
                "scene_id": scene_id,
                "title": f"Evidence-led scene {index + 1}",
                "role": roles[min(index, len(roles) - 1)],
                "start_s": cursor,
                "duration_s": scene_duration,
                "narration": narration,
                "source_ids": ["ev-001"],
                "visual_ids": ["vis-001"] if index == 3 else [],
                "title_claim_id": f"claim-{scene_id}-title",
                "narration_claim_id": f"claim-{scene_id}-narration",
                "visible_claim_ids": [f"claim-{scene_id}-title"],
            }
        )
        cursor += scene_duration
    return {
        "format_version": 1,
        "artifact_type": "video",
        "width": 1920,
        "height": 1080,
        "fps": 30,
        "scene_count": scene_count,
        "duration_s": duration_s,
        "voice_id": "af_heart",
        "language": "en",
        "scenes": scenes,
        "max_attempts": 4,
    }


def _claims(plan: dict[str, object]) -> list[dict[str, object]]:
    scenes = plan["scenes"]
    assert isinstance(scenes, list)
    claims: list[dict[str, object]] = []
    for scene in scenes:
        assert isinstance(scene, dict)
        claims.extend(
            [
                {
                    "id": scene["title_claim_id"],
                    "text": scene["title"],
                    "source_ids": list(scene["source_ids"]),
                },
                {
                    "id": scene["narration_claim_id"],
                    "text": scene["narration"],
                    "source_ids": list(scene["source_ids"]),
                },
            ]
        )
    return claims


def _project_html(plan: dict[str, object], *, bad: str = "") -> str:
    scenes = plan["scenes"]
    assert isinstance(scenes, list)
    scene_html: list[str] = []
    for scene_index, scene in enumerate(scenes):
        assert isinstance(scene, dict)
        source_media = ""
        visual_ids = scene["visual_ids"]
        assert isinstance(visual_ids, list)
        if visual_ids:
            source_media = (
                '<img src="assets/figure.png" data-source-id="vis-001" '
                'alt="Source method figure">'
            )
        clip_class = "" if bad == "data-hf-only" and scene["scene_id"] == "scene_01" else ' class="clip"'
        scene_start = "nan" if bad == "scene-malformed-timing" and scene_index == 0 else scene["start_s"]
        extra_number = "<p>Accuracy 99.9%</p>" if bad == "unbound-number" and scene_index == 0 else ""
        title_markup = html.escape(str(scene["title"]))
        if bad == "nested-title" and scene_index == 0:
            first, rest = title_markup.split(" ", 1)
            title_markup = f"<span>{first}</span> {rest}"
        scene_html.append(
            f'<section id="{scene["scene_id"]}"{clip_class} data-hf-clip="true" '
            f'data-start="{scene_start}" data-duration="{scene["duration_s"]}" '
            f'data-track-index="1" data-narration="{html.escape(str(scene["narration"]), quote=True)}" '
            f'data-title-claim-id="{scene["title_claim_id"]}" '
            f'data-narration-claim-id="{scene["narration_claim_id"]}" '
            f'data-claim-ids="{" ".join(scene["visible_claim_ids"])}" '
            f'data-source-ids="ev-001"><h2 data-claim-id="{scene["title_claim_id"]}">'
            f'{title_markup}</h2>{extra_number}{source_media}</section>'
        )
    extra_root = (
        '<div data-composition-id="duplicate" data-start="0" data-duration="360" '
        'data-width="1920" data-height="1080" data-no-timeline></div>'
        if bad == "two-roots"
        else ""
    )
    remote = '<img src="https://example.com/remote.png">' if bad == "remote" else ""
    data_url = '<img src="data:image/png;base64,AA==">' if bad == "data-url" else ""
    remote_css = '<div style="background:url(https://example.com/remote.png)"></div>' if bad == "remote-css" else ""
    iframe = '<iframe src="assets/figure.png"></iframe>' if bad == "iframe" else ""
    unsafe_script = (
        "requestAnimationFrame(()=>fetch('https://example.com'))"
        if bad == "network-script"
        else ""
    )
    if bad == "new-image":
        unsafe_script = "const i = new Image(); i.src = 'https://example.com/tracker.png'"
    meta_refresh = '<meta http-equiv="refresh" content="0;url=https://example.com">' if bad == "meta-refresh" else ""
    duplicate_attr = ' src="https://example.com/second.png"' if bad == "duplicate-attr" else ""
    css_escape = "<div style=\"background:url('../escape.png')\"></div>" if bad == "css-escape" else ""
    css_absolute = "<div style=\"background:url('/etc/passwd')\"></div>" if bad == "css-absolute" else ""
    inline_handler = (
        '<button type="button" onclick="fetch(\'https://example.com/track\')">Track</button>'
        if bad == "inline-handler"
        else ""
    )
    secondary_control = (
        '<button type="button" id="secondary-control">Details</button>'
        if bad == "secondary-control"
        else ""
    )
    subtitle_override = (
        ".subtitle-overlay[hidden]{display:block !important;visibility:visible !important}"
        if bad == "subtitle-css-override"
        else ""
    )
    subtitle_button = "" if bad == "no-subtitle-toggle" else (
        f'<button type="button" data-subtitle-toggle aria-pressed="{str(bad == "subtitles-default-on").lower()}" '
        'aria-controls="subtitles">CC</button>'
    )
    subtitle_hidden = "" if bad == "subtitles-default-on" else " hidden"
    root_start = "not-a-number" if bad == "malformed-timing" else "0"
    audio = "" if bad == "no-audio" else (
        f'<audio id="narration" class="clip" src="assets/narration.wav" '
        f'data-start="0" data-duration="{plan["duration_s"]}" '
        'data-track-index="2" data-media-start="0"></audio>'
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">{meta_refresh}<style>
html,body{{margin:0;width:1920px;height:1080px;overflow:hidden}}
[data-composition-id]{{position:relative;width:1920px;height:1080px;background:#101820;color:white}}
.clip{{position:absolute;inset:0}} .subtitle-overlay[hidden]{{display:none}}
{subtitle_override}
</style></head><body>
<main data-composition-id="conference-video" data-start="{root_start}"
      data-duration="{plan['duration_s']}" data-width="1920" data-height="1080"
      data-no-timeline>{''.join(scene_html)}{audio}
  {subtitle_button}<div id="subtitles" class="subtitle-overlay" aria-live="polite"
    data-subtitle-source="narration/subtitles.en.vtt"{subtitle_hidden}>
    {''.join(f'<span>{html.escape(str(scene["narration"]))}</span>' for scene in scenes)}
  </div>
  {remote}{data_url}{remote_css}{iframe}{css_escape}{css_absolute}{inline_handler}{secondary_control}
  {f'<img src="assets/figure.png"{duplicate_attr} data-source-id="vis-001" alt="duplicate">' if duplicate_attr else ''}
</main>{extra_root}
<script>{unsafe_script}
document.querySelector('[data-subtitle-toggle]')?.addEventListener('click',event=>{{
 const button=event.currentTarget; const target=document.getElementById('subtitles');
 const shown=button.getAttribute('aria-pressed')==='true';
 button.setAttribute('aria-pressed',String(!shown)); target.hidden=shown;
}});</script></body></html>"""


def _write_project(root: Path, plan: dict[str, object], *, bad: str = "") -> Path:
    project = root / "project"
    (project / "assets").mkdir(parents=True)
    (project / "assets" / "figure.png").write_bytes(b"source-figure")
    (project / "index.html").write_text(_project_html(plan, bad=bad), encoding="utf-8")
    (project / "hyperframes.json").write_text(
        json.dumps({"version": 1, "entry": "index.html"}) + "\n", encoding="utf-8"
    )
    return project


def _make_executable(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _fake_runtime(
    root: Path,
    *,
    fail_stage: str = "",
    stale_render: bool = False,
    subtitle_css_override: bool = False,
    subtitle_effective_opacity: float = 1.0,
    subtitle_intersection_width: int = 640,
    subtitle_intersection_height: int = 80,
    control_count: int = 1,
    controls_exercised: int = 1,
    control_results: list[dict[str, object]] | None = None,
    quiescence_ms: int = 500,
    late_activity: list[str] | None = None,
    pending_timers: int = 0,
) -> dict[str, str]:
    bin_dir = root / "fake-bin"
    bin_dir.mkdir(parents=True)
    log = root / "commands.jsonl"
    hyperframes = _make_executable(
        bin_dir / "hyperframes",
        f"""
        import json, os, pathlib, sys, wave
        log = pathlib.Path(os.environ['AUTODESIGN_VIDEO_TEST_LOG'])
        with log.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({{'tool':'hyperframes','args':sys.argv[1:]}})+'\\n')
        args=sys.argv[1:]
        if args == ['--version']:
            print('0.7.86'); raise SystemExit(0)
        stage=args[0] if args else ''
        if stage == 'tts':
            if {fail_stage!r} == 'tts': raise SystemExit(7)
            output=pathlib.Path(args[args.index('--output')+1])
            output.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(output), 'wb') as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000)
                wav.writeframes(b'\\0\\0' * 2400)
        elif stage == 'lint':
            if not pathlib.Path('assets/narration.wav').is_file():
                print('audio_src_not_found assets/narration.wav', file=sys.stderr); raise SystemExit(9)
            if {fail_stage!r} == 'lint': print('invalid clip nesting', file=sys.stderr); raise SystemExit(8)
        elif stage == 'render':
            if {fail_stage!r} == 'render': raise SystemExit(6)
            output=pathlib.Path(args[args.index('--output')+1])
            output.parent.mkdir(parents=True, exist_ok=True); output.write_bytes(b'HYPERFRAMES-MP4')
            if {stale_render!r}: os.utime(output, (1, 1))
        elif stage == 'browser' and args[1:] == ['ensure']:
            print('browser ready')
        """,
    )
    ffmpeg = _make_executable(
        bin_dir / "ffmpeg",
        f"""
        import json, os, pathlib, sys, wave
        log=pathlib.Path(os.environ['AUTODESIGN_VIDEO_TEST_LOG'])
        with log.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({{'tool':'ffmpeg','args':sys.argv[1:]}})+'\\n')
        args=sys.argv[1:]; output=pathlib.Path(args[-1]); output.parent.mkdir(parents=True, exist_ok=True)
        if output.suffix == '.wav':
            with wave.open(str(output), 'wb') as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000)
                wav.writeframes(b'\\0\\0' * 2400)
        elif output.suffix == '.png':
            output.write_bytes(bytes.fromhex('89504e470d0a1a0a') + b'frame')
        else:
            source=pathlib.Path(args[args.index('-i')+1]); output.write_bytes(source.read_bytes()+b'-SUBTITLES')
        """,
    )
    ffprobe = _make_executable(
        bin_dir / "ffprobe",
        f"""
        import json, os, pathlib, sys
        log=pathlib.Path(os.environ['AUTODESIGN_VIDEO_TEST_LOG'])
        with log.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({{'tool':'ffprobe','args':sys.argv[1:]}})+'\\n')
        if {fail_stage!r} == 'probe': print('probe failed', file=sys.stderr); raise SystemExit(5)
        path=pathlib.Path(sys.argv[-1])
        if path.suffix == '.wav':
            print('360.0' if 'narration.wav' in path.name else '0.1'); raise SystemExit(0)
        print(json.dumps({{
          'streams':[
            {{'codec_type':'video','codec_name':'h264','pix_fmt':'yuv420p','width':1920,'height':1080,
              'avg_frame_rate':'30/1','r_frame_rate':'30/1','duration':'360.0','nb_read_frames':'10800'}},
            {{'codec_type':'audio','codec_name':'aac','duration':'360.0'}},
            {{'codec_type':'subtitle','codec_name':'mov_text','tags':{{'language':'eng'}},'disposition':{{'forced':0}}}}
          ], 'format':{{'duration':'360.0'}}
        }}))
        """,
    )
    browser = _make_executable(bin_dir / "chrome-headless-shell", "raise SystemExit(0)")
    hidden_display = "block" if subtitle_css_override else "none"
    hidden_extent = 640 if subtitle_css_override else 0
    fake_control_results = control_results
    if fake_control_results is None:
        fake_control_results = [
            {
                "identity": {
                    "token": f"control-{index + 1}",
                    "tag": "button",
                    "id": "",
                    "type": "button",
                    "role": "",
                    "name": "",
                    "aria_label": "",
                    "text": "Control",
                },
                "kind": "button",
                "operation": "click",
                "result": "ok",
            }
            for index in range(control_count)
        ]
    fake_checkpoints = [
        {
            "label": f"checkpoint-{index + 1}",
            "waited_ms": quiescence_ms,
            "pending_timers": pending_timers,
            "late_activity": list(late_activity or []),
        }
        for index in range(control_count + 3)
    ]
    node = _make_executable(
        bin_dir / "node",
        """
        import hashlib, json, pathlib, sys
        vtt = next((pathlib.Path(value) for value in sys.argv if value.endswith('.vtt')), None)
        blocks = vtt.read_text(encoding='utf-8').strip().split('\\n\\n')[1:] if vtt else []
        cues = [' '.join(block.splitlines()[2:]).strip() for block in blocks]
        print(json.dumps({
          'passed': True,
          'initial': {'aria_pressed': 'false', 'overlay_hidden': True},
          'after_first_click': {'aria_pressed': 'true', 'overlay_hidden': False},
          'after_second_click': {'aria_pressed': 'false', 'overlay_hidden': True},
          'computed_states': {
            'initial': {'display': %r, 'visibility': 'visible',
                        'width': %d, 'height': %d, 'visible': %r,
                        'effective_opacity': %r,
                        'intersection_width': %d, 'intersection_height': %d},
            'after_first_click': {'display': 'block', 'visibility': 'visible',
                                  'width': 640, 'height': 80, 'visible': True,
                                  'effective_opacity': %r,
                                  'intersection_width': %d, 'intersection_height': %d},
            'after_second_click': {'display': %r, 'visibility': 'visible',
                                   'width': %d, 'height': %d, 'visible': %r,
                                   'effective_opacity': %r,
                                   'intersection_width': %d, 'intersection_height': %d},
          },
          'control_count': %d,
          'controls_exercised': %d,
          'control_results': %r,
          'quiescence': {
            'minimum_wait_ms': %d,
            'checkpoints': %r,
            'late_activity': %r,
            'pending_timers': %d,
          },
          'blocked_requests': [],
          'page_errors': [],
          'cue_count': len(cues),
          'overlay_matches_all_cues': True,
          'subtitle_source': 'narration/subtitles.en.vtt',
          'overlay_texts': cues,
          'subtitle_source_sha256': hashlib.sha256(vtt.read_bytes()).hexdigest() if vtt else '',
        }))
        """ % (
            hidden_display,
            hidden_extent,
            80 if subtitle_css_override else 0,
            subtitle_css_override,
            subtitle_effective_opacity,
            hidden_extent,
            80 if subtitle_css_override else 0,
            subtitle_effective_opacity,
            subtitle_intersection_width,
            subtitle_intersection_height,
            hidden_display,
            hidden_extent,
            80 if subtitle_css_override else 0,
            subtitle_css_override,
            subtitle_effective_opacity,
            hidden_extent,
            80 if subtitle_css_override else 0,
            control_count,
            controls_exercised,
            fake_control_results,
            quiescence_ms,
            fake_checkpoints,
            late_activity or [],
            pending_timers,
        ),
    )
    return {
        "status": "ready",
        "cache_dir": str(root / "runtime-cache"),
        "home_dir": str(root / "runtime-home"),
        "hyperframes": str(hyperframes),
        "python": sys.executable,
        "node": str(node),
        "browser": str(browser),
        "node_root": str(bin_dir),
        "ffmpeg": str(ffmpeg),
        "ffprobe": str(ffprobe),
        "command_log": str(log),
        "hyperframes_version": "0.7.86",
    }


def _write_runtime_fixture(setup: object, spec: object) -> tuple[str, str]:
    cache = spec.cache_dir
    home = cache / "home"
    node_root = cache / "node"
    hyperframes = node_root / "node_modules" / ".bin" / "hyperframes"
    python = cache / "tts-venv" / "bin" / "python"
    model = home / ".cache" / "hyperframes" / "tts" / "models" / "kokoro-v1.0.onnx"
    voices = home / ".cache" / "hyperframes" / "tts" / "voices" / "voices-v1.0.bin"
    smoke = cache / "smoke" / "tts.wav"
    browser = home / ".cache" / "hyperframes" / "chrome" / "chrome-headless-shell"
    package_file = cache / "tts-venv" / "lib" / f"python{spec.python_major_minor}" / "site-packages" / "fixture" / "module.py"
    for path in (hyperframes, python, model, voices, smoke, browser, package_file):
        path.parent.mkdir(parents=True, exist_ok=True)
    _make_executable(hyperframes, "print('0.7.86')")
    _make_executable(
        python,
        """
        import sys
        source = sys.argv[-1] if sys.argv else ''
        print('0.5.0' if 'kokoro-onnx' in source else '0.14.0')
        """,
    )
    model.write_bytes(b"fixture-model")
    voices.write_bytes(b"fixture-voices")
    smoke.write_bytes(b"fixture-wav")
    _make_executable(browser, "raise SystemExit(0)")
    package_file.write_text("VALUE = 1\n", encoding="utf-8")
    model_hash = setup._sha256(model)
    voices_hash = setup._sha256(voices)
    state = {
        "format_version": setup.RUNTIME_FORMAT_VERSION,
        "cache_key": spec.cache_key,
        "system": spec.system,
        "machine": spec.machine,
        "python_major_minor": spec.python_major_minor,
        "node_major": spec.node_major,
        "node_binary": str(spec.node_binary),
        "ffmpeg_binary": str(spec.ffmpeg_binary),
        "ffprobe_binary": str(spec.ffprobe_binary),
        "hyperframes_version": setup.HYPERFRAMES_VERSION,
        "hyperframes_relative": hyperframes.relative_to(cache).as_posix(),
        "python_relative": python.relative_to(cache).as_posix(),
        "home_relative": home.relative_to(cache).as_posix(),
        "package_sha256": spec.package_sha256,
        "package_lock_sha256": spec.package_lock_sha256,
        "python_lock_sha256": spec.python_lock_sha256,
        "kokoro_onnx_version": setup.KOKORO_ONNX_VERSION,
        "soundfile_version": setup.SOUNDFILE_VERSION,
        "kokoro_model_relative": model.relative_to(cache).as_posix(),
        "kokoro_model_sha256": model_hash,
        "kokoro_voices_relative": voices.relative_to(cache).as_posix(),
        "kokoro_voices_sha256": voices_hash,
        "tts_smoke_relative": smoke.relative_to(cache).as_posix(),
        "tts_smoke_sha256": setup._sha256(smoke),
        "browser_relative": browser.relative_to(cache).as_posix(),
        "browser_sha256": setup._sha256(browser),
        "browser_ensured": True,
    }
    setup._atomic_write_json(cache / "runtime-state.json", state)
    setup._make_python_packages_read_only(cache / "tts-venv")
    return model_hash, voices_hash


class AutoDesignVideoSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.harness = _load_script("portable_video_harness", HARNESS_PATH)
        self.setup = _load_script("portable_video_setup", SETUP_PATH)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _require(self, module: object | None, path: Path) -> object:
        self.assertIsNotNone(module, f"missing standalone video implementation: {path}")
        return module

    def _review_ready_run(
        self,
        harness: object,
        name: str,
    ) -> tuple[Path, str, dict[str, object]]:
        source = self.root / f"{name}-source.md"
        plan = _plan()
        source.write_text(
            "# Grounded video\n\n"
            + "\n\n".join(str(claim["text"]) for claim in _claims(plan))
            + "\n",
            encoding="utf-8",
        )
        run = self.root / name
        harness.core.initialize_run(run, SKILL_ROOT, release_version="0.1.0")
        harness.core.prepare_source(run, source)
        harness.core.save_plan(run, plan)
        attempt_id = harness.begin_video_attempt(run)
        project = _write_project(self.root / f"{name}-project", plan)
        report = harness.deliver_project(
            project,
            plan,
            _fake_runtime(self.root / f"{name}-runtime"),
            claims=_claims(plan),
            canonical_plan_sha256=harness.sha256_file(run / "plan.json"),
            smoke=True,
        )
        self.assertTrue(report["passed"], report)
        harness.record_attempt_delivery(
            run, attempt_id, project, report, claims=_claims(plan)
        )
        context = harness.create_video_review_context(run, attempt_id)
        review: dict[str, object] = {
            "format_version": 1,
            "attempt_id": attempt_id,
            "review_context_sha256": context["context_sha256"],
            "artifact_hashes": context["artifact_hashes"],
            "preview_hashes": context["preview_hashes"],
            "reviewed_frame_ids": sorted(context["preview_hashes"]),
            "source_manifest_sha256": context["source_manifest_sha256"],
            "source_map_sha256": context["source_map_sha256"],
            "rubric_sha256": context["rubric_sha256"],
            "reviewer_mode": "fresh_host_vlm",
            "dimension_scores": {
                name: 5 for name in harness.REVIEW_RUBRIC["dimensions"]
            },
            "blockers": [],
            "localized_repairs": [],
            "verdict": "pass",
            "complete": True,
        }
        return run, attempt_id, review

    def test_cli_help_exposes_complete_video_lifecycle(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(HARNESS_PATH), "--help"],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        for command in (
            "doctor", "setup", "init", "evidence", "bind-visuals", "plan",
            "begin-attempt", "validate", "deliver", "review-context",
            "record-review", "finalize", "resume",
        ):
            self.assertIn(command, completed.stdout)
        setup_help = subprocess.run(
            [sys.executable, str(SETUP_PATH), "--help"],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(setup_help.returncode, 0, setup_help.stdout + setup_help.stderr)
        for command in ("doctor", "setup", "remove", "smoke"):
            self.assertIn(command, setup_help.stdout)
        unbound_delivery = subprocess.run(
            [sys.executable, str(HARNESS_PATH), "deliver", "project", "plan.json"],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(unbound_delivery.returncode, 0)
        for required in ("--run", "--attempt", "--claims"):
            self.assertIn(required, unbound_delivery.stderr)

    def test_help_keeps_fresh_writable_skill_tree_byte_identical(self) -> None:
        def fingerprint(root: Path) -> dict[str, str]:
            return {
                path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(root.rglob("*"))
                if path.is_file()
            }

        for entrypoint in ("video_harness.py", "setup_video.py"):
            with self.subTest(entrypoint=entrypoint):
                installed = self.root / f"installed-{entrypoint}"
                shutil.copytree(SKILL_ROOT, installed)
                before = fingerprint(installed)
                child_env = dict(os.environ)
                child_env.pop("PYTHONDONTWRITEBYTECODE", None)
                child_env.pop("PYTHONPYCACHEPREFIX", None)
                completed = subprocess.run(
                    [sys.executable, str(installed / "scripts" / entrypoint), "--help"],
                    cwd=self.root,
                    text=True,
                    capture_output=True,
                    check=False,
                    env=child_env,
                )
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                self.assertEqual(fingerprint(installed), before)
                self.assertFalse(any(installed.rglob("__pycache__")))

    def test_video_review_pass_requires_every_dimension_at_least_four(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        run, attempt_id, review = self._review_ready_run(harness, "review-threshold")
        context = json.loads(
            (run / "attempts" / attempt_id / "qa" / "review-context.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(context["rubric"]["minimum_passing_score_per_dimension"], 4)
        scores = review["dimension_scores"]
        self.assertIsInstance(scores, dict)

        for label, mutate in (
            ("below", lambda value: value.update({name: 1 for name in value})),
            ("missing", lambda value: value.pop(next(iter(value)))),
            ("nonfinite", lambda value: value.update({next(iter(value)): float("nan")})),
        ):
            with self.subTest(label=label):
                candidate = json.loads(json.dumps(review))
                candidate_scores = candidate["dimension_scores"]
                self.assertIsInstance(candidate_scores, dict)
                mutate(candidate_scores)
                with self.assertRaisesRegex(
                    harness.VideoContractError,
                    "dimension|score|4",
                ):
                    harness.record_video_semantic_review(run, attempt_id, candidate)
                self.assertFalse(
                    (run / "attempts" / attempt_id / "qa" / "semantic-review.json").exists()
                )

        review["dimension_scores"] = {
            name: 4 for name in harness.REVIEW_RUBRIC["dimensions"]
        }
        recorded = harness.record_video_semantic_review(run, attempt_id, review)
        self.assertEqual(min(recorded["dimension_scores"].values()), 4)
        self.assertEqual(json.loads((run / "run.json").read_text())["state"], "semantic_passed")

    def test_resume_and_finalize_revalidate_persisted_video_review(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)

        low_run, low_attempt, low_review = self._review_ready_run(
            harness, "persisted-low-review"
        )
        low_review["dimension_scores"] = {
            name: 1 for name in harness.REVIEW_RUBRIC["dimensions"]
        }
        harness.core.record_semantic_review(low_run, low_attempt, low_review)
        for operation in (
            lambda: harness.resume_video_run(low_run),
            lambda: harness.finalize_video_attempt(low_run, low_attempt),
        ):
            with self.assertRaisesRegex(harness.VideoContractError, "4"):
                operation()
        self.assertFalse((low_run / "final").exists())

        stale_run, stale_attempt, stale_review = self._review_ready_run(
            harness, "persisted-stale-review"
        )
        harness.core.record_semantic_review(stale_run, stale_attempt, stale_review)
        review_path = stale_run / "attempts" / stale_attempt / "qa" / "semantic-review.json"
        persisted = json.loads(review_path.read_text(encoding="utf-8"))
        persisted["review_context_sha256"] = "0" * 64
        review_path.write_text(json.dumps(persisted) + "\n", encoding="utf-8")
        for operation in (
            lambda: harness.resume_video_run(stale_run),
            lambda: harness.finalize_video_attempt(stale_run, stale_attempt),
        ):
            with self.assertRaises(harness.core.ContractError):
                operation()
        self.assertFalse((stale_run / "final").exists())

    def test_passing_delivery_report_uses_only_portable_project_relative_paths(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        plan = _plan()
        project = _write_project(self.root / "portable-report", plan)
        navigation = (project / "index.html").as_uri() + "#scene_01"
        report = harness.deliver_project(
            project,
            plan,
            _fake_runtime(
                self.root / "portable-report-runtime",
                control_results=[
                    {
                        "identity": {
                            "token": "control-1",
                            "tag": "a",
                            "id": "",
                            "type": "",
                            "role": "",
                            "name": "",
                            "aria_label": "Start",
                            "href": "#scene_01",
                            "text": "Start",
                        },
                        "kind": "anchor",
                        "operation": "click",
                        "result": "ok",
                        "navigation": navigation,
                    }
                ],
            ),
            claims=_claims(plan),
            smoke=True,
        )
        self.assertTrue(report["passed"], report)
        persisted = json.loads((project / "delivery-report.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["mp4_path"], "conference-video.mp4")
        self.assertEqual(persisted["contact_sheet"], "contact-sheet.png")
        self.assertEqual(
            persisted["stages"][3]["control_results"][0]["navigation"],
            "index.html#scene_01",
        )
        serialized = json.dumps(persisted, sort_keys=True)
        self.assertNotIn(str(project), serialized)
        self.assertNotIn(str(self.root), serialized)
        report_strings: list[str] = []
        pending: list[object] = [persisted]
        while pending:
            value = pending.pop()
            if isinstance(value, str):
                report_strings.append(value)
            elif isinstance(value, dict):
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
        self.assertEqual(
            [
                value
                for value in report_strings
                if value.startswith("file://")
                or Path(value).is_absolute()
                or re.match(r"^[A-Za-z]:[\\/]", value)
            ],
            [],
        )

    def test_pipeline_owned_directory_symlinks_fail_before_any_generated_write(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        plan = _plan()
        for owned_name in ("narration", "assets", "renders", "frames"):
            with self.subTest(owned_name=owned_name):
                case = self.root / f"unsafe-{owned_name}"
                project = _write_project(case, plan)
                outside = self.root / f"outside-{owned_name}"
                outside.mkdir()
                sentinel = outside / "sentinel.txt"
                sentinel.write_text("unchanged", encoding="utf-8")
                owned = project / owned_name
                if owned_name == "assets":
                    figure = (owned / "figure.png").read_bytes()
                    shutil.rmtree(owned)
                    (outside / "figure.png").write_bytes(figure)
                owned.symlink_to(outside, target_is_directory=True)
                before = {
                    path.relative_to(outside).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in outside.rglob("*")
                    if path.is_file()
                }

                report = harness.deliver_project(
                    project,
                    plan,
                    _fake_runtime(self.root / f"unsafe-{owned_name}-runtime"),
                    claims=_claims(plan),
                    smoke=True,
                )

                self.assertFalse(report["passed"], report)
                self.assertEqual(report["failed_stage"], "structural")
                self.assertFalse((project / "delivery-report.json").exists())
                after = {
                    path.relative_to(outside).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in outside.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(after, before)
                if owned_name != "narration":
                    self.assertFalse((project / "narration").exists())

    def test_plan_defaults_to_12_scenes_and_360_seconds(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        plan = harness.normalize_plan(
            {"format_version": 1, "artifact_type": "video", "scenes": _plan()["scenes"]}
        )
        self.assertEqual(plan["scene_count"], 12)
        self.assertEqual(plan["duration_s"], 360)
        self.assertEqual(plan["width"], 1920)
        self.assertEqual(plan["height"], 1080)
        self.assertEqual(plan["fps"], 30)

    def test_plan_honors_valid_user_scene_and_duration_overrides(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        for scene_count, duration_s in ((10, 300), (11, 427), (14, 600)):
            with self.subTest(scene_count=scene_count, duration_s=duration_s):
                normalized = harness.normalize_plan(_plan(scene_count=scene_count, duration_s=duration_s))
                self.assertEqual(normalized["scene_count"], scene_count)
                self.assertEqual(normalized["duration_s"], duration_s)
                self.assertEqual(
                    sum(scene["duration_s"] for scene in normalized["scenes"]), duration_s
                )

    def test_plan_rejects_out_of_range_scene_duration_and_noncontiguous_timing(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        for value in (
            _plan(scene_count=9, duration_s=360),
            _plan(scene_count=15, duration_s=360),
            _plan(scene_count=12, duration_s=299),
            _plan(scene_count=12, duration_s=601),
        ):
            with self.subTest(scene_count=value["scene_count"], duration_s=value["duration_s"]):
                with self.assertRaises(harness.VideoContractError):
                    harness.normalize_plan(value)
        broken = _plan()
        broken["scenes"][3]["start_s"] += 1
        with self.assertRaisesRegex(harness.VideoContractError, "contiguous"):
            harness.normalize_plan(broken)

    def test_structural_validation_accepts_local_editable_hyperframes_project(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        plan = _plan()
        project = _write_project(self.root, plan)
        report = harness.validate_project(
            project,
            plan,
            evidence_ids={"ev-001"},
            claims=_claims(plan),
            visual_catalog={
                "vis-001": {
                    "path": str(project / "assets" / "figure.png"),
                    "sha256": harness.sha256_file(project / "assets" / "figure.png"),
                }
            },
        )
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["scene_count"], 12)
        self.assertEqual(report["timeline_duration_s"], 360)
        self.assertTrue(report["subtitle_toggle"])

        nested_title_project = _write_project(self.root / "nested-title", plan, bad="nested-title")
        nested_title = harness.validate_project(
            nested_title_project,
            plan,
            evidence_ids={"ev-001"},
            claims=_claims(plan),
        )
        self.assertTrue(nested_title["passed"], nested_title)

    def test_scene_titles_narration_and_visible_numbers_require_exact_nonempty_claims(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        plan = _plan()
        claims = _claims(plan)
        project = _write_project(self.root / "valid-claims", plan)
        valid = harness.validate_project(
            project,
            plan,
            evidence_ids={"ev-001"},
            claims=claims,
        )
        self.assertTrue(valid["passed"], valid)

        empty = harness.validate_project(
            project,
            plan,
            evidence_ids={"ev-001"},
            claims=[],
        )
        self.assertFalse(empty["passed"])
        self.assertIn("claims_empty", {item["code"] for item in empty["issues"]})

        for field, replacement, code in (
            ("title", "A paraphrased title", "title_claim_mismatch"),
            ("narration", "A paraphrased narration.", "narration_claim_mismatch"),
        ):
            with self.subTest(field=field):
                changed = json.loads(json.dumps(claims))
                target_id = plan["scenes"][0][f"{field}_claim_id"]
                next(item for item in changed if item["id"] == target_id)["text"] = replacement
                report = harness.validate_project(
                    project,
                    plan,
                    evidence_ids={"ev-001"},
                    claims=changed,
                )
                self.assertFalse(report["passed"])
                self.assertIn(code, {item["code"] for item in report["issues"]})

        numbered = _write_project(self.root / "unbound-number", plan, bad="unbound-number")
        report = harness.validate_project(
            numbered,
            plan,
            evidence_ids={"ev-001"},
            claims=claims,
        )
        self.assertFalse(report["passed"])
        self.assertIn("unbound_visible_number", {item["code"] for item in report["issues"]})

    def test_source_catalog_preserves_visual_policy_and_shared_plan_validator_enforces_reuse(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        core = harness.core
        source = self.root / "source.md"
        source.write_text("# Method\nA grounded method and result.\n", encoding="utf-8")
        visual = self.root / "method.png"
        visual.write_bytes(b"source-figure")
        run = self.root / "visual-run"
        core.initialize_run(run, SKILL_ROOT, release_version="0.1.0")
        core.prepare_source(run, source, extra_assets=[visual])
        evidence_ids, catalog = harness._source_contract(run)
        self.assertIn("ev-001", evidence_ids)
        self.assertEqual(catalog["vis-001"]["eligibility"], "eligible")
        self.assertIn("method", catalog["vis-001"]["allowed_content_roles"])
        self.assertEqual(catalog["vis-001"]["max_reuse"], 1)

        plan = _plan()
        plan["scenes"][3]["visual_ids"] = ["vis-001"]
        plan["scenes"][4]["visual_ids"] = ["vis-001"]
        project = _write_project(self.root / "visual-project", plan)
        staged = project / "assets" / "figure.png"
        source_visual = Path(catalog["vis-001"]["path"])
        staged.write_bytes(source_visual.read_bytes())
        with mock.patch.object(
            core,
            "validate_visual_plan",
            wraps=core.validate_visual_plan,
        ) as shared_validator:
            report = harness.validate_project(
                project,
                plan,
                run_dir=run,
                evidence_ids=evidence_ids,
                visual_catalog=catalog,
                claims=_claims(plan),
            )
        shared_validator.assert_called_once()
        self.assertFalse(report["passed"])
        self.assertIn("visual_reuse_limit", {item["code"] for item in report["issues"]})

    def test_each_scene_image_set_must_equal_its_canonical_visual_ids(self) -> None:
        """Catches omitted, moved, or extra source visuals that still hash correctly."""
        harness = self._require(self.harness, HARNESS_PATH)
        plan = _plan()
        image = (
            '<img src="assets/figure.png" data-source-id="vis-001" '
            'alt="Source method figure">'
        )
        catalog = {
            "vis-001": {
                "path": "",
                "sha256": "",
            }
        }
        for case in ("missing", "wrong-scene", "unplanned"):
            with self.subTest(case=case):
                project = _write_project(self.root / case, plan)
                index = project / "index.html"
                text = index.read_text(encoding="utf-8")
                if case == "missing":
                    text = text.replace(image, "", 1)
                elif case == "wrong-scene":
                    text = text.replace(image, "", 1)
                    text = re.sub(
                        r'(<section id="scene_05".*?<h2[^>]*>.*?</h2>)',
                        rf"\1{image}",
                        text,
                        count=1,
                        flags=re.DOTALL,
                    )
                else:
                    text = re.sub(
                        r'(<section id="scene_01".*?<h2[^>]*>.*?</h2>)',
                        rf"\1{image}",
                        text,
                        count=1,
                        flags=re.DOTALL,
                    )
                index.write_text(text, encoding="utf-8")
                catalog["vis-001"]["path"] = str(project / "assets" / "figure.png")
                catalog["vis-001"]["sha256"] = harness.sha256_file(
                    project / "assets" / "figure.png"
                )
                report = harness.validate_project(
                    project,
                    plan,
                    evidence_ids={"ev-001"},
                    visual_catalog=catalog,
                    claims=_claims(plan),
                )
                self.assertFalse(report["passed"], case)
                self.assertIn(
                    "scene_visual_binding",
                    {item["code"] for item in report["issues"]},
                )

    def test_structural_validation_rejects_unsafe_or_noncanonical_projects(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        for bad, code in (
            ("remote", "remote_asset"),
            ("remote-css", "remote_asset"),
            ("data-url", "data_url"),
            ("iframe", "unsafe_embedded_content"),
            ("network-script", "non_seekable_or_network_script"),
            ("two-roots", "composition_root_count"),
            ("data-hf-only", "literal_clip_required"),
            ("no-audio", "narration_audio_missing"),
            ("no-subtitle-toggle", "subtitle_toggle_missing"),
            ("subtitles-default-on", "subtitle_default_state"),
            ("malformed-timing", "composition_contract"),
            ("scene-malformed-timing", "scene_timing"),
            ("duplicate-attr", "duplicate_attribute"),
            ("meta-refresh", "meta_refresh"),
            ("new-image", "dynamic_image"),
            ("css-escape", "unsafe_css_asset"),
            ("css-absolute", "unsafe_css_asset"),
        ):
            with self.subTest(bad=bad):
                project = _write_project(self.root / bad, _plan(), bad=bad)
                report = harness.validate_project(project, _plan(), evidence_ids={"ev-001"})
                self.assertFalse(report["passed"])
                self.assertIn(code, {item["code"] for item in report["issues"]})

    def test_inline_event_handlers_are_rejected_and_all_controls_must_be_exercised(self) -> None:
        """Catches hidden onclick network behavior and untested secondary controls."""
        harness = self._require(self.harness, HARNESS_PATH)
        plan = _plan()
        inline = _write_project(self.root / "inline", plan, bad="inline-handler")
        structural = harness.validate_project(
            inline,
            plan,
            evidence_ids={"ev-001"},
            claims=_claims(plan),
        )
        self.assertFalse(structural["passed"])
        self.assertIn("inline_event_handler", {item["code"] for item in structural["issues"]})

        secondary = _write_project(self.root / "secondary", plan, bad="secondary-control")
        report = harness.deliver_project(
            secondary,
            plan,
            _fake_runtime(
                self.root / "secondary-runtime",
                control_count=2,
                controls_exercised=1,
            ),
            claims=_claims(plan),
            smoke=True,
        )
        self.assertFalse(report["passed"])
        self.assertEqual(report["failed_stage"], "browser_preflight")
        self.assertIn("control", report["error"].lower())

    def test_delivery_runs_strict_offline_browser_toggle_and_binds_generated_vtt(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        plan = _plan()
        project = _write_project(self.root, plan)
        report = harness.deliver_project(
            project,
            plan,
            _fake_runtime(self.root),
            claims=_claims(plan),
            smoke=True,
        )
        self.assertTrue(report["passed"], report)
        browser = next(stage for stage in report["stages"] if stage["id"] == "browser_preflight")
        self.assertTrue(browser["passed"], browser)
        self.assertEqual(browser["initial"], {"aria_pressed": "false", "overlay_hidden": True})
        self.assertEqual(browser["after_first_click"], {"aria_pressed": "true", "overlay_hidden": False})
        self.assertEqual(browser["after_second_click"], {"aria_pressed": "false", "overlay_hidden": True})
        self.assertEqual(browser["blocked_requests"], [])
        self.assertTrue(browser["overlay_matches_all_cues"])
        self.assertEqual(
            browser["subtitle_source_sha256"],
            harness.sha256_file(project / "narration" / "subtitles.en.vtt"),
        )

    def test_subtitle_toggle_uses_computed_visibility_not_only_hidden_attribute(self) -> None:
        """Catches CSS !important rules that visually override the hidden attribute."""
        harness = self._require(self.harness, HARNESS_PATH)
        plan = _plan()
        project = _write_project(self.root, plan, bad="subtitle-css-override")
        report = harness.deliver_project(
            project,
            plan,
            _fake_runtime(self.root, subtitle_css_override=True),
            claims=_claims(plan),
            smoke=True,
        )
        self.assertFalse(report["passed"])
        self.assertEqual(report["failed_stage"], "browser_preflight")
        self.assertIn("computed", report["error"].lower())

    def test_subtitle_toggle_requires_paint_visible_opacity_and_viewport_intersection(self) -> None:
        """Catches overlays with valid bounds that are transparent or fully clipped."""
        harness = self._require(self.harness, HARNESS_PATH)
        plan = _plan()
        for label, runtime_options in (
            ("transparent", {"subtitle_effective_opacity": 0.0}),
            ("clipped", {"subtitle_intersection_width": 0}),
        ):
            with self.subTest(label=label):
                project = _write_project(self.root / label, plan)
                runtime = _fake_runtime(
                    self.root / f"{label}-runtime",
                    **runtime_options,
                )
                report = harness.deliver_project(
                    project,
                    plan,
                    runtime,
                    claims=_claims(plan),
                    smoke=True,
                )
                self.assertFalse(report["passed"], label)
                self.assertEqual(report["failed_stage"], "browser_preflight")
                self.assertIn("paint", report["error"].lower())

    def test_browser_preflight_requires_identity_results_for_every_control(self) -> None:
        """Catches count-only preflights that never prove which controls were operated."""
        harness = self._require(self.harness, HARNESS_PATH)
        plan = _plan()
        project = _write_project(self.root, plan, bad="secondary-control")
        report = harness.deliver_project(
            project,
            plan,
            _fake_runtime(
                self.root,
                control_count=2,
                controls_exercised=2,
                control_results=[
                    {
                        "identity": "subtitle-toggle",
                        "kind": "button",
                        "operation": "toggle-twice",
                        "result": "ok",
                    }
                ],
            ),
            claims=_claims(plan),
            smoke=True,
        )
        self.assertFalse(report["passed"])
        self.assertEqual(report["failed_stage"], "browser_preflight")
        self.assertIn("identity", report["error"].lower())

    def test_browser_preflight_fails_closed_on_late_activity_or_short_quiescence(self) -> None:
        """Catches delayed requests, timers, and checks shorter than the 500 ms bound."""
        harness = self._require(self.harness, HARNESS_PATH)
        plan = _plan()
        for label, runtime_options in (
            ("late-request", {"late_activity": ["request:https://example.com/late"]}),
            ("pending-timer", {"pending_timers": 1}),
            ("short-wait", {"quiescence_ms": 499}),
        ):
            with self.subTest(label=label):
                project = _write_project(self.root / label, plan)
                report = harness.deliver_project(
                    project,
                    plan,
                    _fake_runtime(self.root / f"{label}-runtime", **runtime_options),
                    claims=_claims(plan),
                    smoke=True,
                )
                self.assertFalse(report["passed"], label)
                self.assertEqual(report["failed_stage"], "browser_preflight")
                self.assertIn("quiescence", report["error"].lower())

    def test_source_visual_binding_rejects_missing_hash_symlink_and_hardlink(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        plan = _plan()
        for mode, expected in (("hash", "source_visual_hash_mismatch"), ("symlink", "unsafe_local_asset"), ("hardlink", "unsafe_local_asset")):
            with self.subTest(mode=mode):
                root = self.root / mode
                project = _write_project(root, plan)
                source = root / "trusted.png"
                source.write_bytes(b"source-figure")
                asset = project / "assets" / "figure.png"
                if mode == "hash":
                    expected_hash = "0" * 64
                elif mode == "symlink":
                    asset.unlink(); asset.symlink_to(source)
                    expected_hash = harness.sha256_file(source)
                else:
                    asset.unlink(); os.link(source, asset)
                    expected_hash = harness.sha256_file(source)
                report = harness.validate_project(
                    project,
                    plan,
                    evidence_ids={"ev-001"},
                    visual_catalog={"vis-001": {"path": str(source), "sha256": expected_hash}},
                )
                self.assertFalse(report["passed"])
                self.assertIn(expected, {item["code"] for item in report["issues"]})

    def test_delivery_uses_structural_tts_subtitles_full_lint_render_probe_order(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        plan = _plan()
        project = _write_project(self.root, plan)
        runtime = _fake_runtime(self.root)
        report = harness.deliver_project(project, plan, runtime, claims=_claims(plan), smoke=True)
        self.assertTrue(report["passed"], report)
        events = [json.loads(line) for line in Path(runtime["command_log"]).read_text().splitlines()]
        tools_and_stages = [
            (item["tool"], item["args"][0] if item["tool"] == "hyperframes" else "")
            for item in events
        ]
        tts_positions = [index for index, value in enumerate(tools_and_stages) if value == ("hyperframes", "tts")]
        lint_position = tools_and_stages.index(("hyperframes", "lint"))
        render_position = tools_and_stages.index(("hyperframes", "render"))
        probe_position = next(index for index, item in enumerate(events) if item["tool"] == "ffprobe" and item["args"][-1].endswith(".mp4"))
        self.assertEqual(len(tts_positions), 12)
        self.assertLess(max(tts_positions), lint_position)
        self.assertLess(lint_position, render_position)
        self.assertLess(render_position, probe_position)
        self.assertTrue((project / "assets" / "narration.wav").is_file())
        self.assertTrue((project / "narration" / "transcript.en.txt").is_file())
        self.assertTrue((project / "narration" / "subtitles.en.srt").is_file())
        self.assertTrue((project / "narration" / "subtitles.en.vtt").is_file())
        first_cue = (project / "narration" / "subtitles.en.srt").read_text(
            encoding="utf-8"
        ).splitlines()[1]
        self.assertEqual(first_cue, "00:00:00,000 --> 00:00:00,100")
        subtitle_metadata = json.loads(
            (project / "narration" / "voice-and-subtitles.json").read_text(encoding="utf-8")
        )
        self.assertFalse(subtitle_metadata["html_subtitles_default_on"])
        self.assertEqual(report["media_probe"]["video_codec"], "h264")
        self.assertEqual(report["media_probe"]["subtitle_language"], "eng")
        self.assertFalse(report["media_probe"]["subtitle_forced"])

    def test_full_lint_runs_only_after_real_narration_reference_exists(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        plan = _plan()
        project = _write_project(self.root, plan)
        runtime = _fake_runtime(self.root)
        report = harness.deliver_project(project, plan, runtime, claims=_claims(plan), smoke=True)
        self.assertTrue(report["passed"], report)
        lint = next(stage for stage in report["stages"] if stage["id"] == "full_lint")
        self.assertEqual(lint["narration_sha256"], harness.sha256_file(project / "assets" / "narration.wav"))
        self.assertNotIn("placeholder", json.dumps(report).lower())

    def test_deterministic_runtime_failures_do_not_route_back_to_authoring(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        for stage in ("tts", "probe"):
            with self.subTest(stage=stage):
                root = self.root / stage
                plan = _plan()
                project = _write_project(root, plan)
                report = harness.deliver_project(
                    project, plan, _fake_runtime(root, fail_stage=stage), claims=_claims(plan), smoke=True
                )
                self.assertFalse(report["passed"])
                self.assertEqual(report["failure_class"], "runtime")
                self.assertFalse(report["authoring_retryable"])

    def test_delivery_failure_persists_runtime_resume_and_routes_authoring_next(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        core = harness.core
        source = self.root / "source.md"
        source.write_text("# Video source\nA grounded contribution and result.\n", encoding="utf-8")
        run = self.root / "run"
        core.initialize_run(run, SKILL_ROOT, release_version="0.1.0")
        core.prepare_source(run, source)
        plan = _plan()
        core.save_plan(run, plan)
        attempt_id = harness.begin_video_attempt(run)
        project = _write_project(self.root / "failure", plan)
        report = harness.deliver_project(
            project,
            plan,
            _fake_runtime(self.root / "runtime-failure", fail_stage="tts"),
            claims=_claims(plan),
            smoke=True,
        )
        report["runtime_diagnostics"] = {
            "ready": False,
            "status": "corrupt",
            "issues": ["browser launch probe failed"],
        }
        routed = harness.record_delivery_failure(run, attempt_id, report)
        self.assertEqual(routed["next_action"], "repair_runtime_and_resume_same_attempt")
        resumed = harness.resume_video_run(run)
        self.assertEqual(resumed["active_attempt"], attempt_id)
        self.assertEqual(resumed["next_action"], "repair_runtime_and_resume_same_attempt")
        self.assertIn("narration", resumed["runtime_failure"]["failed_stage"])
        self.assertEqual(
            resumed["runtime_failure"]["runtime_diagnostics"]["issues"],
            ["browser launch probe failed"],
        )
        self.assertEqual(json.loads((run / "run.json").read_text())["state"], "authoring")
        authoring = {
            "passed": False,
            "failure_class": "authoring",
            "failed_stage": "full_lint",
            "error": "authored clip contract failed",
        }
        routed = harness.record_delivery_failure(run, attempt_id, authoring)
        self.assertEqual(routed["next_action"], "repair_authoring_in_next_attempt")
        self.assertFalse((run / "attempts" / attempt_id / "qa" / "runtime-failure.json").exists())
        self.assertEqual(harness.begin_video_attempt(run), "02")

    def test_lint_and_render_failures_are_truthful_and_never_use_ffmpeg_as_final_renderer(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        for stage in ("lint", "render"):
            with self.subTest(stage=stage):
                root = self.root / stage
                plan = _plan()
                project = _write_project(root, plan)
                runtime = _fake_runtime(root, fail_stage=stage)
                with mock.patch.object(
                    harness.setup_video,
                    "doctor_video_runtime",
                    return_value={"ready": True, "status": "ready", "issues": []},
                ):
                    report = harness.deliver_project(
                        project, plan, runtime, claims=_claims(plan), smoke=True
                    )
                self.assertFalse(report["passed"])
                self.assertEqual(report["failed_stage"], "full_lint" if stage == "lint" else "render")
                self.assertEqual(report["failure_class"], "authoring")
                commands = Path(runtime["command_log"]).read_text(encoding="utf-8")
                if stage == "render":
                    self.assertNotIn("-c:v", commands)
                self.assertFalse(any(project.glob("renders/delivery*.mp4")))

    def test_lint_or_render_rechecks_browser_doctor_and_routes_infrastructure_same_attempt(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        for failed_stage in ("lint", "render"):
            with self.subTest(failed_stage=failed_stage):
                root = self.root / f"infra-{failed_stage}"
                plan = _plan()
                project = _write_project(root, plan)
                with mock.patch.object(
                    harness.setup_video,
                    "doctor_video_runtime",
                    return_value={
                        "ready": False,
                        "status": "corrupt",
                        "issues": ["fresh Chrome launch probe failed"],
                    },
                ) as doctor:
                    report = harness.deliver_project(
                        project,
                        plan,
                        _fake_runtime(root, fail_stage=failed_stage),
                        claims=_claims(plan),
                        smoke=True,
                    )
                doctor.assert_called()
                self.assertFalse(report["passed"])
                self.assertEqual(report["failure_class"], "runtime")
                self.assertFalse(report["authoring_retryable"])
                self.assertIn("Chrome", report["runtime_diagnostics"]["issues"][0])

    def test_delivery_requires_byte_identical_canonical_run_plan(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        core = harness.core
        source = self.root / "source.md"
        source.write_text("# Video source\nA grounded contribution and result.\n", encoding="utf-8")
        run = self.root / "bound-plan-run"
        core.initialize_run(run, SKILL_ROOT, release_version="0.1.0")
        core.prepare_source(run, source)
        plan = _plan()
        core.save_plan(run, harness.normalize_plan(plan))
        supplied = self.root / "supplied-plan.json"
        supplied.write_text((run / "plan.json").read_text(encoding="utf-8"), encoding="utf-8")
        bound, digest = harness.load_canonical_delivery_plan(run, supplied)
        self.assertEqual(bound, harness.normalize_plan(plan))
        self.assertEqual(digest, harness.sha256_file(run / "plan.json"))

        supplied.write_text(json.dumps(bound), encoding="utf-8")
        with self.assertRaisesRegex(harness.VideoContractError, "canonical run plan"):
            harness.load_canonical_delivery_plan(run, supplied)

    def test_validate_cli_rejects_a_reserialized_or_drifted_noncanonical_plan(self) -> None:
        """Catches validate accepting plan bytes that deliver must later reject."""
        harness = self._require(self.harness, HARNESS_PATH)
        core = harness.core
        plan = _plan()
        for scene in plan["scenes"]:
            scene["visual_ids"] = []
        source = self.root / "source.md"
        source.write_text(
            "# Grounded video\n\n"
            + "\n\n".join(str(claim["text"]) for claim in _claims(plan))
            + "\n",
            encoding="utf-8",
        )
        run = self.root / "validate-run"
        core.initialize_run(run, SKILL_ROOT, release_version="0.1.0")
        core.prepare_source(run, source)
        core.save_plan(run, harness.normalize_plan(plan))
        project = _write_project(self.root / "validate-project", plan)
        claims_path = self.root / "claims.json"
        claims_path.write_text(json.dumps(_claims(plan)), encoding="utf-8")
        canonical_plan = harness.normalize_plan(plan)
        drifted_plan = json.loads(json.dumps(canonical_plan))
        drifted_plan["max_attempts"] = 5
        for label, payload in (
            ("reserialized", canonical_plan),
            ("drifted", drifted_plan),
        ):
            with self.subTest(label=label):
                supplied = self.root / f"{label}-plan.json"
                supplied.write_text(json.dumps(payload), encoding="utf-8")
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    result = harness.main(
                        [
                            "validate",
                            str(project),
                            str(supplied),
                            "--run",
                            str(run),
                            "--claims",
                            str(claims_path),
                        ]
                    )
                self.assertEqual(result, 2)
                self.assertIn("canonical run plan", stderr.getvalue())

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = harness.main(
                [
                    "validate",
                    str(project),
                    str(run / "plan.json"),
                    "--run",
                    str(run),
                    "--claims",
                    str(claims_path),
                ]
            )
        self.assertEqual(result, 0, stderr.getvalue())
        self.assertTrue(json.loads(stdout.getvalue())["passed"])

    def test_publish_allowlist_rejects_hidden_unknown_and_unreferenced_files(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        core = harness.core
        source = self.root / "source.md"
        source.write_text("# Grounded video\nThe paper reports a grounded method and evidence.\n", encoding="utf-8")
        run = self.root / "publish-run"
        core.initialize_run(run, SKILL_ROOT, release_version="0.1.0")
        core.prepare_source(run, source)
        plan = _plan()
        core.save_plan(run, harness.normalize_plan(plan))
        attempt_id = core.begin_attempt(run)
        project = _write_project(self.root / "publish-project", plan)
        report = harness.deliver_project(
            project,
            plan,
            _fake_runtime(self.root / "publish-runtime"),
            claims=_claims(plan),
            canonical_plan_sha256=harness.sha256_file(run / "plan.json"),
            smoke=True,
        )
        self.assertTrue(report["passed"], report)
        for name in (".env", "debug.log", "assets/unreferenced.png"):
            with self.subTest(name=name):
                path = project / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("must not publish\n", encoding="utf-8")
                with self.assertRaisesRegex(harness.VideoContractError, "allowlist"):
                    harness.record_attempt_delivery(
                        run,
                        attempt_id,
                        project,
                        report,
                        claims=_claims(plan),
                    )
                path.unlink()
        self.assertEqual(list((run / "attempts" / attempt_id / "artifact").iterdir()), [])

    def test_publish_copy_failure_is_crash_atomic_and_retryable(self) -> None:
        """Catches partial live artifacts that make an identical retry impossible."""
        harness = self._require(self.harness, HARNESS_PATH)
        core = harness.core
        plan = _plan()
        source = self.root / "source.md"
        source.write_text(
            "# Grounded video\n\n"
            + "\n\n".join(str(claim["text"]) for claim in _claims(plan))
            + "\n",
            encoding="utf-8",
        )
        run = self.root / "atomic-run"
        core.initialize_run(run, SKILL_ROOT, release_version="0.1.0")
        core.prepare_source(run, source)
        core.save_plan(run, harness.normalize_plan(plan))
        attempt_id = core.begin_attempt(run)
        project = _write_project(self.root / "atomic-project", plan)
        report = harness.deliver_project(
            project,
            plan,
            _fake_runtime(self.root / "atomic-runtime"),
            claims=_claims(plan),
            canonical_plan_sha256=harness.sha256_file(run / "plan.json"),
            smoke=True,
        )
        self.assertTrue(report["passed"], report)
        original_write = core.atomic_write_bytes
        failed = False

        def fail_mid_publish(path: Path | str, data: bytes) -> None:
            nonlocal failed
            destination = Path(path)
            if destination.name == "contact-sheet.png" and "qa" not in destination.parts and not failed:
                failed = True
                raise OSError("simulated publish crash")
            original_write(path, data)

        with mock.patch.object(core, "atomic_write_bytes", side_effect=fail_mid_publish):
            with self.assertRaisesRegex(OSError, "simulated publish crash"):
                harness.record_attempt_delivery(
                    run,
                    attempt_id,
                    project,
                    report,
                    claims=_claims(plan),
                )
        artifact = run / "attempts" / attempt_id / "artifact"
        self.assertEqual(list(artifact.iterdir()), [])
        self.assertEqual(
            list((run / "attempts" / attempt_id).glob(".artifact.stage-*")),
            [],
        )
        crashed_stage = run / "attempts" / attempt_id / ".artifact.stage-recovery"
        shutil.copytree(project, crashed_stage)
        artifact.rmdir()
        (run / "attempts" / attempt_id / ".artifact.empty-recovery").mkdir()
        result = harness.record_attempt_delivery(
            run,
            attempt_id,
            project,
            report,
            claims=_claims(plan),
        )
        self.assertTrue(result["passed"], result)
        idempotent_paths = harness._copy_tree_allowlist(
            project,
            artifact,
            report["publish_allowlist"],
        )
        self.assertEqual(
            idempotent_paths,
            [f"artifact/{path}" for path in report["publish_allowlist"]],
        )

    def test_publish_promotion_is_windows_safe_and_recovers_interrupted_transaction(self) -> None:
        """Catches replacing a pre-created directory, which fails on Windows."""
        harness = self._require(self.harness, HARNESS_PATH)
        source = self.root / "publish-source"
        (source / "nested").mkdir(parents=True)
        (source / "index.html").write_text("ready\n", encoding="utf-8")
        (source / "nested" / "asset.bin").write_bytes(b"grounded")
        expected = ["index.html", "nested/asset.bin"]
        destination = self.root / "attempt" / "artifact"
        destination.mkdir(parents=True)
        original_replace = os.replace

        def windows_replace(source_path: Path | str, destination_path: Path | str) -> None:
            if Path(destination_path).exists():
                raise PermissionError("Windows refuses to replace an existing directory")
            original_replace(source_path, destination_path)

        with mock.patch.object(harness.os, "replace", side_effect=windows_replace):
            paths = harness._copy_tree_allowlist(source, destination, expected)
        self.assertEqual(paths, ["artifact/index.html", "artifact/nested/asset.bin"])
        self.assertEqual(harness._actual_project_files(destination), expected)

        interrupted = self.root / "interrupted" / "artifact"
        interrupted.parent.mkdir(parents=True)
        crashed_stage = interrupted.parent / ".artifact.stage-crashed"
        shutil.copytree(source, crashed_stage)
        crashed_backup = interrupted.parent / ".artifact.empty-crashed"
        crashed_backup.mkdir()
        with mock.patch.object(harness.os, "replace", side_effect=windows_replace):
            paths = harness._copy_tree_allowlist(source, interrupted, expected)
        self.assertEqual(paths, ["artifact/index.html", "artifact/nested/asset.bin"])
        self.assertEqual(harness._actual_project_files(interrupted), expected)
        self.assertFalse(crashed_stage.exists())
        self.assertFalse(crashed_backup.exists())

    def test_stale_render_output_cannot_satisfy_delivery(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        plan = _plan()
        project = _write_project(self.root, plan)
        with mock.patch.object(
            harness.setup_video,
            "doctor_video_runtime",
            return_value={"ready": True, "status": "ready", "issues": []},
        ):
            report = harness.deliver_project(
                project,
                plan,
                _fake_runtime(self.root, stale_render=True),
                claims=_claims(plan),
                smoke=True,
            )
        self.assertFalse(report["passed"])
        self.assertEqual(report["failed_stage"], "render")
        self.assertIn("fresh", report["error"].lower())

    def test_media_probe_requires_exact_h264_aac_1080p_30fps_english_optional_subtitles(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        valid = {
            "streams": [
                {"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p", "width": 1920, "height": 1080, "avg_frame_rate": "30/1", "r_frame_rate": "30/1", "duration": "360", "nb_read_frames": "10800"},
                {"codec_type": "audio", "codec_name": "aac", "duration": "360"},
                {"codec_type": "subtitle", "codec_name": "mov_text", "tags": {"language": "eng"}, "disposition": {"forced": 0}},
            ],
            "format": {"duration": "360"},
        }
        report = harness.validate_media_probe(valid, expected_duration_s=360)
        self.assertTrue(report["passed"], report)
        for mutation, code in (
            (("streams", 0, "width", 1280), "video_dimensions"),
            (("streams", 0, "codec_name", "hevc"), "video_codec"),
            (("streams", 0, "avg_frame_rate", "unknown"), "video_frame_rate"),
            (("streams", 1, "codec_name", "opus"), "audio_codec"),
            (("streams", 2, "tags", {"language": "fra"}), "subtitle_language"),
            (("streams", 2, "disposition", {"forced": 1}), "subtitle_forced"),
        ):
            payload = json.loads(json.dumps(valid))
            _, index, key, value = mutation
            payload["streams"][index][key] = value
            broken = harness.validate_media_probe(payload, expected_duration_s=360)
            self.assertFalse(broken["passed"])
            self.assertIn(code, {item["code"] for item in broken["issues"]})

        malformed = json.loads(json.dumps(valid))
        malformed["streams"][2]["disposition"]["forced"] = "unknown"
        report = harness.validate_media_probe(malformed, expected_duration_s=360)
        self.assertFalse(report["passed"])
        self.assertIn("subtitle_forced", {item["code"] for item in report["issues"]})

    def test_setup_contract_pins_exact_runtime_and_kokoro_hashes(self) -> None:
        setup = self._require(self.setup, SETUP_PATH)
        self.assertEqual(setup.HYPERFRAMES_VERSION, "0.7.86")
        self.assertEqual(
            setup.KOKORO_MODEL_SHA256,
            "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
        )
        self.assertEqual(
            setup.KOKORO_VOICES_SHA256,
            "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
        )
        package = json.loads((SKILL_ROOT / "assets" / "video-runtime" / "package.json").read_text())
        lock = json.loads((SKILL_ROOT / "assets" / "video-runtime" / "package-lock.json").read_text())
        self.assertEqual(package["dependencies"], {"hyperframes": "0.7.86"})
        self.assertEqual(lock["packages"]["node_modules/hyperframes"]["version"], "0.7.86")
        self.assertEqual(
            lock["packages"]["node_modules/hyperframes"]["integrity"],
            "sha512-R8Vds5hY9XULMsCGUa+qynC7F0tL7KZyDaL6cgQ4xyJAATC9fOIPgRMOBkOHYd9JOntRqbR9bFSsfK7mYJjaow==",
        )
        spec = setup.runtime_spec(cache_root=self.root / "cache")
        self.assertLessEqual(len(spec.cache_key), 40)
        self.assertEqual(setup._venv_python_relative().parts[0], "p")

    def test_python_runtime_lock_is_generated_hash_complete_platform_bound_and_tamper_evident(self) -> None:
        setup = self._require(self.setup, SETUP_PATH)
        requirements_input = SKILL_ROOT / "assets" / "video-runtime" / "requirements-kokoro.in"
        lock = SKILL_ROOT / "assets" / "video-runtime" / "requirements-kokoro.lock"
        self.assertEqual(
            requirements_input.read_text(encoding="utf-8"),
            "kokoro-onnx==0.5.0\nsoundfile==0.14.0\n",
        )
        text = lock.read_text(encoding="utf-8")
        self.assertIn("autogenerated by uv", text)
        self.assertIn("--universal", text.splitlines()[1])
        self.assertIn("uv pip compile requirements-kokoro.in", text.splitlines()[1])
        self.assertIn("kokoro-onnx==0.5.0", text)
        self.assertIn("soundfile==0.14.0", text)
        self.assertNotIn("--index-url", text)
        self.assertEqual(hashlib.sha256(lock.read_bytes()).hexdigest(), setup.PYTHON_LOCK_SHA256)
        self.assertRegex(text, r"(?m)^onnxruntime==[^\n]+")
        requirement_blocks: list[str] = []
        current: list[str] = []
        for line in text.splitlines():
            if line and not line.startswith((" ", "#")):
                if current:
                    requirement_blocks.append("\n".join(current))
                current = [line]
            elif current:
                current.append(line)
        if current:
            requirement_blocks.append("\n".join(current))
        self.assertTrue(requirement_blocks)
        self.assertTrue(all("--hash=sha256:" in block for block in requirement_blocks))

        original = setup.runtime_spec(cache_root=self.root / "cache")
        self.assertEqual(original.python_lock_sha256, setup.PYTHON_LOCK_SHA256)
        self.assertIn(setup.PYTHON_LOCK_SHA256[:12], original.cache_key)
        tampered = self.root / "requirements-kokoro.lock"
        tampered.write_bytes(lock.read_bytes() + b"# tampered\n")
        with mock.patch.object(setup, "PYTHON_LOCK_PATH", tampered):
            with self.assertRaisesRegex(setup.VideoRuntimeError, "Python lock checksum"):
                setup.runtime_spec(cache_root=self.root / "tampered-cache")

        with (
            mock.patch.object(setup.platform, "system", return_value="FreeBSD"),
            mock.patch.object(setup.platform, "machine", return_value="sparc64"),
        ):
            with self.assertRaisesRegex(setup.VideoRuntimeError, "unsupported video runtime platform"):
                setup.runtime_spec(cache_root=self.root / "unsupported")

    def test_doctor_launches_exact_browser_and_rejects_writable_python_packages(self) -> None:
        setup = self._require(self.setup, SETUP_PATH)
        spec = setup.runtime_spec(cache_root=self.root / "cache")
        model_hash, voices_hash = _write_runtime_fixture(setup, spec)
        setup._make_python_packages_read_only(spec.cache_dir / "tts-venv")
        with (
            mock.patch.object(setup, "KOKORO_MODEL_SHA256", model_hash),
            mock.patch.object(setup, "KOKORO_VOICES_SHA256", voices_hash),
            mock.patch.object(setup, "_probe_hyperframes_browser", return_value={"passed": True}) as probe,
        ):
            ready = setup.doctor_video_runtime(cache_root=spec.cache_root)
        self.assertTrue(ready["ready"], ready)
        probe.assert_called_once()

        package_file = next((spec.cache_dir / "tts-venv").rglob("*.py"))
        package_file.chmod(package_file.stat().st_mode | stat.S_IWUSR)
        with (
            mock.patch.object(setup, "KOKORO_MODEL_SHA256", model_hash),
            mock.patch.object(setup, "KOKORO_VOICES_SHA256", voices_hash),
            mock.patch.object(setup, "_probe_hyperframes_browser", return_value={"passed": True}),
        ):
            corrupt = setup.doctor_video_runtime(cache_root=spec.cache_root)
        self.assertFalse(corrupt["ready"])
        self.assertIn("writable", json.dumps(corrupt).lower())

    def test_doctor_distinguishes_missing_partial_corrupt_and_ready_cache(self) -> None:
        setup = self._require(self.setup, SETUP_PATH)
        cache_root = self.root / "cache"
        missing = setup.doctor_video_runtime(cache_root=cache_root)
        self.assertEqual(missing["status"], "missing")
        spec = setup.runtime_spec(cache_root=cache_root)
        spec.cache_dir.mkdir(parents=True)
        partial = setup.doctor_video_runtime(cache_root=cache_root)
        self.assertEqual(partial["status"], "partial")
        (spec.cache_dir / "runtime-state.json").write_text("{}\n", encoding="utf-8")
        corrupt = setup.doctor_video_runtime(cache_root=cache_root)
        self.assertEqual(corrupt["status"], "corrupt")
        shutil.rmtree(spec.cache_dir)
        model_hash, voices_hash = _write_runtime_fixture(setup, spec)
        with (
            mock.patch.object(setup, "KOKORO_MODEL_SHA256", model_hash),
            mock.patch.object(setup, "KOKORO_VOICES_SHA256", voices_hash),
            mock.patch.object(setup, "_probe_hyperframes_browser", return_value={"passed": True}),
        ):
            ready = setup.doctor_video_runtime(cache_root=cache_root)
        self.assertEqual(ready["status"], "ready")
        self.assertTrue(ready["ready"])
        state_path = spec.cache_dir / "runtime-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["ffmpeg_binary"] = str(Path(shutil.which("echo") or "/bin/echo").resolve())
        setup._atomic_write_json(state_path, state)
        with (
            mock.patch.object(setup, "KOKORO_MODEL_SHA256", model_hash),
            mock.patch.object(setup, "KOKORO_VOICES_SHA256", voices_hash),
        ):
            changed_tool = setup.doctor_video_runtime(cache_root=cache_root)
        self.assertEqual(changed_tool["status"], "corrupt")
        self.assertIn("ffmpeg_binary", json.dumps(changed_tool))
        state["ffmpeg_binary"] = str(spec.ffmpeg_binary)
        external_state = self.root / "external-runtime-state.json"
        setup._atomic_write_json(external_state, state)
        state_path.unlink()
        state_path.symlink_to(external_state)
        with (
            mock.patch.object(setup, "KOKORO_MODEL_SHA256", model_hash),
            mock.patch.object(setup, "KOKORO_VOICES_SHA256", voices_hash),
        ):
            linked_state = setup.doctor_video_runtime(cache_root=cache_root)
        self.assertEqual(linked_state["status"], "corrupt")
        self.assertIn("runtime-state.json", json.dumps(linked_state))

    def test_runtime_cache_rejects_skill_internal_symlink_and_hardlinked_assets(self) -> None:
        setup = self._require(self.setup, SETUP_PATH)
        with self.assertRaises(setup.VideoRuntimeError):
            setup.runtime_spec(cache_root=SKILL_ROOT / "cache")
        linked_root = self.root / "linked"
        linked_root.mkdir()
        real_root = self.root / "real"
        real_root.mkdir()
        (linked_root / "cache").symlink_to(real_root, target_is_directory=True)
        with self.assertRaises(setup.VideoRuntimeError):
            setup.runtime_spec(cache_root=linked_root / "cache")
        if setup.platform.system() == "Darwin":
            with self.assertRaisesRegex(setup.VideoRuntimeError, "too long"):
                setup.runtime_spec(cache_root=self.root / ("deep-cache-" * 20))

        short_root = Path(tempfile.mkdtemp(prefix="adv-v-broken-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, short_root, True)
        broken_spec = setup.runtime_spec(cache_root=short_root)
        broken_spec.cache_root.mkdir(parents=True, exist_ok=True)
        broken_spec.cache_dir.symlink_to(self.root / "missing-runtime", target_is_directory=True)
        broken = setup.doctor_video_runtime(cache_root=broken_spec.cache_root)
        self.assertEqual(broken["status"], "corrupt")
        self.assertIn("symlink", json.dumps(broken).lower())

        spec = setup.runtime_spec(cache_root=self.root / "cache")
        model_hash, voices_hash = _write_runtime_fixture(setup, spec)
        model = spec.cache_dir / "home" / ".cache" / "hyperframes" / "tts" / "models" / "kokoro-v1.0.onnx"
        twin = self.root / "model-copy"
        os.link(model, twin)
        with (
            mock.patch.object(setup, "KOKORO_MODEL_SHA256", model_hash),
            mock.patch.object(setup, "KOKORO_VOICES_SHA256", voices_hash),
        ):
            doctor = setup.doctor_video_runtime(cache_root=spec.cache_root)
        self.assertEqual(doctor["status"], "corrupt")
        self.assertIn("hard link", json.dumps(doctor).lower())

    def test_runtime_environment_isolated_to_versioned_home_and_contains_no_secrets(self) -> None:
        setup = self._require(self.setup, SETUP_PATH)
        spec = setup.runtime_spec(cache_root=self.root / "cache")
        model_hash, voices_hash = _write_runtime_fixture(setup, spec)
        with (
            mock.patch.object(setup, "KOKORO_MODEL_SHA256", model_hash),
            mock.patch.object(setup, "KOKORO_VOICES_SHA256", voices_hash),
            mock.patch.object(setup, "_probe_hyperframes_browser", return_value={"passed": True}),
        ):
            runtime = setup.require_video_runtime(cache_root=spec.cache_root)
        env = setup.runtime_environment(
            runtime,
            base={"PATH": os.environ.get("PATH", ""), "OPENAI_API_KEY": "secret", "HOME": "/tmp/elsewhere"},
        )
        self.assertEqual(Path(env["HOME"]), runtime.home_dir)
        self.assertEqual(Path(env["HYPERFRAMES_PYTHON"]), runtime.python_executable)
        self.assertEqual(env["HYPERFRAMES_NO_TELEMETRY"], "1")
        self.assertNotIn("OPENAI_API_KEY", env)

    def test_runtime_remove_is_explicit_idempotent_and_confined_to_exact_cache(self) -> None:
        setup = self._require(self.setup, SETUP_PATH)
        cache_root = self.root / "cache"
        spec = setup.runtime_spec(cache_root=cache_root)
        spec.cache_dir.mkdir(parents=True)
        (spec.cache_dir / "owned.txt").write_text("runtime\n", encoding="utf-8")
        sibling = cache_root / "keep-me"
        sibling.mkdir()
        (sibling / "user.txt").write_text("keep\n", encoding="utf-8")

        removed = setup.remove_video_runtime(cache_root=cache_root)
        self.assertEqual(removed["status"], "removed")
        self.assertFalse(spec.cache_dir.exists())
        self.assertEqual((sibling / "user.txt").read_text(encoding="utf-8"), "keep\n")

        missing = setup.remove_video_runtime(cache_root=cache_root)
        self.assertEqual(missing["status"], "missing")

        spec.cache_dir.symlink_to(sibling, target_is_directory=True)
        with self.assertRaisesRegex(setup.VideoRuntimeError, "symlink"):
            setup.remove_video_runtime(cache_root=cache_root)
        self.assertTrue((sibling / "user.txt").is_file())

    def test_attempt_delivery_is_hash_bound_and_resume_rejects_tampered_mp4(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        core = harness.core
        source = self.root / "source.md"
        plan = _plan()
        source.write_text(
            "# Grounded video\n\n" + "\n\n".join(str(claim["text"]) for claim in _claims(plan)) + "\n",
            encoding="utf-8",
        )
        run = self.root / "run"
        core.initialize_run(run, SKILL_ROOT, release_version="0.1.0")
        core.prepare_source(run, source)
        core.save_plan(run, plan)
        attempt_id = core.begin_attempt(run)
        attempt = run / "attempts" / attempt_id
        project = _write_project(attempt / "artifact-root", plan)
        report = harness.deliver_project(
            project,
            plan,
            _fake_runtime(self.root / "runtime"),
            claims=_claims(plan),
            canonical_plan_sha256=harness.sha256_file(run / "plan.json"),
            smoke=True,
        )
        self.assertTrue(report["passed"], report)
        claims = _claims(plan)
        runtime_marker = attempt / "qa" / "runtime-failure.json"
        core.atomic_write_json(
            runtime_marker,
            {
                "format_version": 1,
                "attempt_id": attempt_id,
                "failure_class": "runtime",
                "failed_stage": "narration",
                "error": "temporary runtime failure",
            },
        )
        with self.assertRaisesRegex(harness.VideoContractError, "persisted delivery report"):
            harness.record_attempt_delivery(
                run,
                attempt_id,
                project,
                {**report, "publish_allowlist": []},
                claims=claims,
            )
        self.assertTrue(runtime_marker.exists())
        harness.record_attempt_delivery(run, attempt_id, project, report, claims=claims)
        self.assertFalse(runtime_marker.exists())
        context = harness.create_video_review_context(run, attempt_id)
        materials = context["review_materials"]
        self.assertEqual(
            materials["evidence_jsonl"]["sha256"],
            harness.sha256_file(Path(materials["evidence_jsonl"]["path"])),
        )
        self.assertEqual(
            materials["source_text"]["sha256"],
            harness.sha256_file(Path(materials["source_text"]["path"])),
        )
        self.assertEqual(
            materials["source_map"]["sha256"],
            context["source_map_sha256"],
        )
        review = {
            "format_version": 1,
            "attempt_id": attempt_id,
            "review_context_sha256": context["context_sha256"],
            "artifact_hashes": context["artifact_hashes"],
            "preview_hashes": context["preview_hashes"],
            "reviewed_frame_ids": sorted(context["preview_hashes"]),
            "source_manifest_sha256": context["source_manifest_sha256"],
            "source_map_sha256": context["source_map_sha256"],
            "rubric_sha256": context["rubric_sha256"],
            "reviewer_mode": "fresh_host_vlm",
            "dimension_scores": {name: 5 for name in harness.REVIEW_RUBRIC["dimensions"]},
            "blockers": [],
            "localized_repairs": [],
            "verdict": "pass",
            "complete": True,
        }
        core.record_semantic_review(run, attempt_id, review)
        copied_mp4 = attempt / "artifact" / "conference-video.mp4"
        copied_mp4.write_bytes(copied_mp4.read_bytes() + b"tampered")
        with self.assertRaises(core.IntegrityError):
            core.finalize_attempt(run, attempt_id)
        with self.assertRaises(core.IntegrityError):
            core.resume_run(run, skill_root=SKILL_ROOT)

    def test_video_attempt_budget_is_enforced_without_fallback(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        core = harness.core
        source = self.root / "source.md"
        source.write_text("# Grounded video\nA grounded method and evidence.\n", encoding="utf-8")
        run = self.root / "bounded-run"
        core.initialize_run(run, SKILL_ROOT, release_version="0.1.0")
        core.prepare_source(run, source)
        plan = _plan()
        plan["max_attempts"] = 1
        core.save_plan(run, harness.normalize_plan(plan))
        attempt_id = harness.begin_video_attempt(run)
        self.assertEqual(attempt_id, "01")
        core.mark_side_state(run, "failed", reason="authored timeline failed")
        with self.assertRaisesRegex(harness.VideoContractError, "budget exhausted"):
            harness.begin_video_attempt(run)
        self.assertFalse((run / "final").exists())

    @unittest.skipUnless(
        os.environ.get("AUTODESIGN_VIDEO_REAL_SMOKE") == "1",
        "set AUTODESIGN_VIDEO_REAL_SMOKE=1 for the real HyperFrames/Kokoro smoke",
    )
    def test_real_hyperframes_0786_tts_render_probe_and_selectable_subtitles(self) -> None:
        setup = self._require(self.setup, SETUP_PATH)
        harness = self._require(self.harness, HARNESS_PATH)
        with tempfile.TemporaryDirectory(prefix="adv-video-", dir="/tmp") as short_cache:
            runtime = setup.ensure_video_runtime(cache_root=Path(short_cache))
            evidence = setup.run_real_smoke(runtime, output_dir=self.root / "real-smoke")
        self.assertTrue(evidence["passed"], evidence)
        self.assertEqual(evidence["hyperframes_version"], "0.7.86")
        probe = harness.validate_media_probe(
            json.loads(Path(evidence["ffprobe_json"]).read_text()),
            expected_duration_s=evidence["duration_s"],
            smoke=True,
        )
        self.assertTrue(probe["passed"], probe)
        self.assertEqual(probe["subtitle_language"], "eng")
        self.assertFalse(probe["subtitle_forced"])
        self.assertTrue(Path(evidence["contact_sheet"]).is_file())
        browser = next(
            stage for stage in evidence["report"]["stages"]
            if stage["id"] == "browser_preflight"
        )
        self.assertEqual(browser["control_count"], 10)
        self.assertEqual(browser["controls_exercised"], 10)
        self.assertTrue(
            {
                "button",
                "input:checkbox",
                "input:radio",
                "input:range",
                "select",
                "textarea",
                "summary",
                "anchor",
                "role:button",
            }.issubset({result["kind"] for result in browser["control_results"]})
        )
        self.assertTrue(all(result["result"] == "ok" for result in browser["control_results"]))
        quiescence = browser["quiescence"]
        self.assertGreaterEqual(len(quiescence["checkpoints"]), 13)
        self.assertTrue(all(item["waited_ms"] >= 500 for item in quiescence["checkpoints"]))
        self.assertEqual(quiescence["late_activity"], [])
        self.assertEqual(quiescence["pending_timers"], 0)
        self.assertFalse(browser["computed_states"]["initial"]["visible"])
        self.assertTrue(browser["computed_states"]["after_first_click"]["visible"])
        self.assertGreater(
            browser["computed_states"]["after_first_click"]["effective_opacity"],
            0.001,
        )
        self.assertGreater(
            browser["computed_states"]["after_first_click"]["intersection_width"],
            0,
        )
        self.assertFalse(browser["computed_states"]["after_second_click"]["visible"])


if __name__ == "__main__":
    unittest.main()
