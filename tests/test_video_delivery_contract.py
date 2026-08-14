from __future__ import annotations

import hashlib
import importlib
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from autodesign.agents import hyperframes_composer as composer_module
from autodesign.agents.hyperframes_composer import (
    authored_video_local_asset_paths,
    validate_authored_video_html,
)
from autodesign.agents.external_video_author import (
    _delivery_failure_is_repairable,
)
from autodesign.attempt_candidates import VideoDeliveryInvalidation
from autodesign.schema import (
    ArtifactType,
    KOKORO_VOICE_BY_PRESET,
    VideoDeliveryContract,
    VideoMediaProbe,
    VideoSceneContract,
)
from autodesign.skills.registry import SkillRegistry
from autodesign.tools._contract import ToolContext
from autodesign.tools import TOOL_SCHEMAS
from autodesign.tools.export_video import (
    _build_timed_narration_mix,
    _clear_stale_video_delivery,
    _mux_optional_subtitle_track,
    _probe_audio_duration,
    _run_hyperframes_authoring_lint,
    _run_kokoro_tts,
    _run_hyperframes_lint,
    _run_hyperframes_render,
    _hyperframes_command,
    _rendered_duration_contract_error,
    _synthesize_timed_narration,
    _write_narration_artifacts,
    export_video,
)
from autodesign.util.design_spec_fingerprint import design_spec_sha256
from autodesign.util.io import sha256_file
from autodesign.util.html_artifact import _audit_video_frames
from autodesign.video_delivery_validation import validate_current_video_delivery
from scripts.web_server import _build_video_artifact
from tests.test_video_web_delivery import _passed_delivery


REPO_ROOT = Path(__file__).resolve().parents[1]


def _scenes(count: int = 12, duration_s: float = 30.0) -> list[VideoSceneContract]:
    narration = (
        "This scene explains the paper question, method, evidence, measured results, "
        "and limitations for a conference audience. It connects the original figures "
        "to specific claims, compares the strongest baseline, and states what the "
        "authors actually conclude without adding unsupported interpretation or filler "
        "for clear technical communication."
    )
    return [
        VideoSceneContract(
            scene_id=f"scene_{index:02d}",
            title=f"Scene {index}",
            start_s=(index - 1) * duration_s,
            duration_s=duration_s,
            narration_text=f"Scene {index}. {narration}",
        )
        for index in range(1, count + 1)
    ]


def _authored_html(
    scenes: list[VideoSceneContract] | None = None,
    *,
    audio_tag: str | None = None,
) -> str:
    scenes = scenes or _scenes()
    scene_html = "\n".join(
        f'<section id="{scene.scene_id}" class="clip" '
        f'data-start="{scene.start_s:g}" data-duration="{scene.duration_s:g}" '
        f'data-track-index="{index}" '
        f'data-narration="{scene.narration_text}"></section>'
        for index, scene in enumerate(scenes, start=1)
    )
    audio = audio_tag or (
        '<audio id="narration-audio" class="clip" '
        'src="assets/narration.wav" data-start="0" data-duration="360" '
        'data-track-index="100" data-media-start="0"></audio>'
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Video</title></head><body>
<div id="root" data-composition-id="main" data-start="0" data-no-timeline
     data-duration="360"
     data-width="1920" data-height="1080">
  {scene_html}
  {audio}
</div></body></html>"""


class VideoSubtitleTrackDeliveryTest(unittest.TestCase):
    def test_multiplexes_an_optional_subtitle_track_into_the_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            source_mp4 = root / "source.mp4"
            subtitle_path = root / "subtitles.en.srt"
            subtitle_path.write_text(
                "1\n00:00:00,000 --> 00:00:00,800\nOptional English captions\n",
                encoding="utf-8",
            )
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-f", "lavfi", "-i", "color=c=black:s=320x180:r=30",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                    "-t", "1",
                    "-shortest",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac",
                    str(source_mp4),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            output, ok, captioned_mp4 = _mux_optional_subtitle_track(
                source_mp4,
                subtitle_path,
            )

            self.assertTrue(ok, output)
            self.assertIsNotNone(captioned_mp4)
            self.assertTrue(captioned_mp4.is_file())
            probe = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-show_streams", "-of", "json",
                    str(captioned_mp4),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            streams = json.loads(probe.stdout)["streams"]
            subtitle = next(stream for stream in streams if stream["codec_type"] == "subtitle")

        self.assertEqual(subtitle["codec_name"], "mov_text")
        self.assertEqual(subtitle["disposition"]["forced"], 0)
        self.assertEqual(subtitle["tags"]["language"], "eng")


class VideoSchemaContractTest(unittest.TestCase):
    def test_video_contract_defaults_are_conference_delivery_defaults(self) -> None:
        contract = VideoDeliveryContract(scenes=_scenes())

        self.assertEqual(ArtifactType.VIDEO.value, "video")
        self.assertEqual(contract.target_duration_s, 360)
        self.assertEqual((contract.width, contract.height, contract.fps), (1920, 1080, 30))
        self.assertEqual(contract.voice.kokoro_voice_id, KOKORO_VOICE_BY_PRESET["female"])
        self.assertEqual(contract.subtitle_formats, ["srt", "vtt"])
        self.assertEqual(contract.narration_language, "en")

    def test_video_contract_rejects_out_of_range_duration_or_scene_count(self) -> None:
        shortest = VideoDeliveryContract(
            target_duration_s=300,
            scenes=_scenes(count=10, duration_s=30.0),
        )
        longest = VideoDeliveryContract(
            target_duration_s=600,
            scenes=_scenes(count=12, duration_s=50.0),
        )

        self.assertEqual(shortest.target_duration_s, 300)
        self.assertEqual(longest.target_duration_s, 600)
        with self.assertRaises(ValidationError):
            VideoDeliveryContract(
                target_duration_s=299,
                scenes=_scenes(count=10, duration_s=30.0),
            )
        with self.assertRaises(ValidationError):
            VideoDeliveryContract(
                target_duration_s=601,
                scenes=_scenes(count=12, duration_s=50.0),
            )
        with self.assertRaises(ValidationError):
            VideoDeliveryContract(scenes=_scenes(count=9, duration_s=40.0))
        with self.assertRaises(ValidationError):
            VideoDeliveryContract(scenes=_scenes(count=15, duration_s=24.0))

    def test_media_probe_allows_only_boundary_jitter_around_selected_range(self) -> None:
        base = {
            "video_codec": "h264",
            "pixel_format": "yuv420p",
            "audio_codec": "aac",
            "width": 1920,
            "height": 1080,
            "fps": 30,
        }

        for duration_s in (299.5, 300.0, 600.0, 600.5):
            self.assertEqual(
                VideoMediaProbe(**base, duration_s=duration_s).duration_s,
                duration_s,
            )
        for duration_s in (299.49, 600.51):
            with self.assertRaises(ValidationError):
                VideoMediaProbe(**base, duration_s=duration_s)

    def test_rendered_duration_must_match_both_timeline_and_selected_target(self) -> None:
        self.assertIsNone(_rendered_duration_contract_error(
            observed_duration_s=480.5,
            authored_timeline_s=480.5,
            selected_target_duration_s=480.0,
        ))
        self.assertIn(
            "selected target",
            _rendered_duration_contract_error(
                observed_duration_s=481.0,
                authored_timeline_s=481.0,
                selected_target_duration_s=480.0,
            ) or "",
        )
        self.assertIn(
            "authored timeline",
            _rendered_duration_contract_error(
                observed_duration_s=480.0,
                authored_timeline_s=481.0,
                selected_target_duration_s=480.0,
            ) or "",
        )

    def test_voice_presets_have_deterministic_kokoro_mapping(self) -> None:
        male = VideoDeliveryContract(voice_preset="male", scenes=_scenes())
        female = VideoDeliveryContract(voice_preset="female", scenes=_scenes())

        self.assertEqual(male.voice.kokoro_voice_id, KOKORO_VOICE_BY_PRESET["male"])
        self.assertEqual(female.voice.kokoro_voice_id, KOKORO_VOICE_BY_PRESET["female"])
        self.assertEqual(male.voice.engine, "kokoro")
        self.assertEqual(female.voice.mapping_version, "kokoro-v1")

    def test_scene_narration_rejects_mostly_non_english_text(self) -> None:
        with self.assertRaises(ValidationError):
            VideoSceneContract(
                scene_id="scene_01",
                title="Overview",
                start_s=0,
                duration_s=30,
                narration_text="这是中文旁白，介绍 DDPM。",
            )

    def test_scene_duration_must_leave_room_for_narration_margin(self) -> None:
        with self.assertRaises(ValidationError):
            VideoSceneContract(
                scene_id="scene_01",
                title="Overview",
                start_s=0,
                duration_s=0.25,
                narration_text="This scene is too short for safe narration fitting.",
            )

    @patch("autodesign.skills.registry.log")
    def test_conference_video_runtime_skill_loads_without_errors(self, log_mock) -> None:
        registry = SkillRegistry.load(REPO_ROOT / "skills")

        pack = registry.get("video.conference_video")

        self.assertIsNotNone(pack)
        self.assertEqual(pack.manifest.stages, ["enhance", "plan", "critique", "repair"])
        self.assertEqual(pack.manifest.resources[0].stages, ["plan", "repair"])
        video_load_errors = [
            call
            for call in log_mock.call_args_list
            if call.args
            and call.args[0] == "skills.load_error"
            and "skills/video/conference_video" in str(call.kwargs.get("path", ""))
        ]
        self.assertEqual(video_load_errors, [])

    def test_video_artifact_hint_always_selects_conference_video_skill(self) -> None:
        registry = SkillRegistry.load(REPO_ROOT / "skills")

        bundle = registry.select(
            brief="Summarize the attached research paper.",
            attachments=[Path("paper.pdf")],
            artifact_hint="video",
        )

        self.assertIn("video.conference_video", bundle.ids)

    def test_runtime_contract_surfaces_adaptive_five_to_ten_minute_policy(self) -> None:
        export_schema = next(
            schema for schema in TOOL_SCHEMAS if schema["name"] == "export_video"
        )
        duration = export_schema["input_schema"]["properties"]["duration_s"]
        skill = (
            REPO_ROOT / "skills" / "video" / "conference_video" / "SKILL.md"
        ).read_text(encoding="utf-8")

        self.assertEqual(duration["minimum"], 300)
        self.assertEqual(duration["maximum"], 600)
        self.assertIn("paper complexity", duration["description"].lower())
        self.assertIn("300-600 seconds", skill)
        self.assertNotIn("330-390 seconds", skill)

    def test_longer_adaptive_scenes_are_not_rejected_by_legacy_thirty_second_gate(
        self,
    ) -> None:
        valid = _audit_video_frames({
            "frames": [{
                "frame_id": "scene_01",
                "kind": "scene",
                "duration_s": 50,
                "blocks": [],
            }]
        })
        invalid = _audit_video_frames({
            "frames": [{
                "frame_id": "scene_01",
                "kind": "scene",
                "duration_s": 601,
                "blocks": [],
            }]
        })

        self.assertFalse(any(
            finding["id"] == "video_invalid_scene_duration"
            for finding in valid
        ))
        self.assertTrue(any(
            finding["id"] == "video_invalid_scene_duration"
            for finding in invalid
        ))


class VideoAuthoredHtmlContractTest(unittest.TestCase):
    def test_authored_html_rejects_placeholder_and_external_assets(self) -> None:
        placeholder = "<!doctype html><html><body>index.html not yet generated</body></html>"
        networked = (
            '<!doctype html><html><head><script src="https://cdn.example/app.js"></script>'
            '</head><body><div id="root"></div></body></html>'
        )

        self.assertIn("placeholder", validate_authored_video_html(placeholder)[0])
        errors = validate_authored_video_html(networked)
        self.assertTrue(any("external" in error or "network" in error for error in errors))

    def test_authored_html_rejects_local_path_escape_or_missing_asset(self) -> None:
        contract = VideoDeliveryContract(scenes=_scenes())
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "assets").mkdir()
            (project / "assets" / "narration.wav").write_bytes(b"audio")
            escaped = _authored_html().replace(
                "</body>", '<img src="../../.env"><img src="/tmp/private.png"></body>'
            )

            errors = validate_authored_video_html(
                escaped,
                contract,
                project_dir=project,
            )

            self.assertTrue(any("../../.env" in error for error in errors))
            self.assertTrue(any("/tmp/private.png" in error for error in errors))

            missing = _authored_html().replace(
                "</body>", '<img src="assets/missing.png"></body>'
            )
            errors = validate_authored_video_html(
                missing,
                contract,
                project_dir=project,
            )
            self.assertTrue(any("assets/missing.png" in error for error in errors))

            escaped_composition = _authored_html().replace(
                "</body>",
                '<div data-composition-src="../../outside.html"></div></body>',
            )
            errors = validate_authored_video_html(
                escaped_composition,
                contract,
                project_dir=project,
            )
            self.assertTrue(any("../../outside.html" in error for error in errors))

    def test_authored_html_rejects_extended_and_dynamic_asset_escapes(self) -> None:
        contract = VideoDeliveryContract(scenes=_scenes())
        variants = [
            '<img srcset="assets/ok.png 1x, ../../outside.png 2x">',
            '<object data="../../outside.pdf"></object>',
            '<svg><use xlink:href="../../outside.svg#icon"></use></svg>',
            '<script>document.querySelector("img").src = "../../outside.png";</script>',
            '<script>document.querySelector("img").srcset = "../../outside.png";</script>',
            '<script>document.querySelector("img")["src"] = "../../outside.png";</script>',
            '<script>node.setAttributeNS(null, "href", "../../outside.svg")</script>',
            '<script>Object.assign(node, {src: "../../outside.png"})</script>',
        ]
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "assets").mkdir()
            (project / "assets" / "narration.wav").write_bytes(b"audio")
            (project / "assets" / "ok.png").write_bytes(b"image")
            for variant in variants:
                html = _authored_html().replace("</body>", f"{variant}</body>")
                errors = validate_authored_video_html(
                    html,
                    contract,
                    project_dir=project,
                )
                self.assertTrue(errors, variant)

    def test_dependency_closure_recurses_through_local_css(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "assets").mkdir()
            (project / "styles").mkdir()
            (project / "assets" / "narration.wav").write_bytes(b"audio")
            figure = project / "assets" / "figure.png"
            figure.write_bytes(b"figure")
            stylesheet = project / "styles" / "main.css"
            stylesheet.write_text(
                '.figure { background-image: url("../assets/figure.png"); }',
                encoding="utf-8",
            )
            html = _authored_html().replace(
                "</head>", '<link rel="stylesheet" href="styles/main.css"></head>'
            )

            assets = authored_video_local_asset_paths(html, project)

            self.assertEqual(
                set(assets),
                {"assets/narration.wav", "assets/figure.png", "styles/main.css"},
            )

            stylesheet.write_text(
                '.figure { background-image: url("../../outside.png"); }',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "outside|escapes"):
                authored_video_local_asset_paths(html, project)

    def test_dependency_closure_rejects_network_access_in_local_javascript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "assets").mkdir()
            (project / "assets" / "narration.wav").write_bytes(b"audio")
            script = project / "app.js"
            script.write_text(
                'fetch("https://example.com/figure.png").then(() => {});',
                encoding="utf-8",
            )
            html = _authored_html().replace(
                "</body>", '<script src="app.js"></script></body>'
            )

            with self.assertRaisesRegex(ValueError, "network|dynamic"):
                authored_video_local_asset_paths(html, project)

    def test_authored_html_accepts_local_html_first_composition(self) -> None:
        contract = VideoDeliveryContract(scenes=_scenes())
        html = _authored_html()

        self.assertEqual(validate_authored_video_html(html, contract), [])

    def test_authored_html_accepts_adaptive_five_to_ten_minute_timelines(self) -> None:
        for duration_s, scenes in (
            (300, _scenes(count=10, duration_s=30.0)),
            (480, _scenes(count=12, duration_s=40.0)),
            (600, _scenes(count=12, duration_s=50.0)),
        ):
            contract = VideoDeliveryContract(
                target_duration_s=duration_s,
                scenes=scenes,
            )
            html = _authored_html(scenes).replace(
                'data-duration="360"',
                f'data-duration="{duration_s}"',
            )

            self.assertEqual(validate_authored_video_html(html, contract), [])

    def test_authored_html_requires_hyperframes_audio_id_and_timing(self) -> None:
        contract = VideoDeliveryContract(scenes=_scenes())
        html = _authored_html(audio_tag='<audio src="assets/narration.wav"></audio>')

        errors = validate_authored_video_html(html, contract)

        self.assertTrue(any("audio id" in error for error in errors))
        self.assertTrue(any("audio data-start" in error for error in errors))
        self.assertTrue(any("audio data-duration" in error for error in errors))
        self.assertTrue(any("audio data-track-index" in error for error in errors))

    def test_authored_html_matches_authoritative_scene_contract_exactly(self) -> None:
        contract = VideoDeliveryContract(scenes=_scenes())
        changed = _scenes()
        changed[3] = changed[3].model_copy(
            update={
                "scene_id": "wrong_scene",
                "start_s": 88.0,
                "duration_s": 29.0,
                "narration_text": "Different English narration.",
            }
        )

        errors = validate_authored_video_html(_authored_html(changed), contract)

        self.assertTrue(any("scene ids/order" in error for error in errors))
        self.assertTrue(any("scene 4 data-start" in error for error in errors))
        self.assertTrue(any("scene 4 data-duration" in error for error in errors))
        self.assertTrue(any("scene 4 data-narration" in error for error in errors))

    def test_authored_html_explains_the_required_clip_class(self) -> None:
        contract = VideoDeliveryContract(scenes=_scenes())
        html = _authored_html().replace(
            'class="clip" data-start=',
            'class="hf-scene" data-hf-clip="scene" data-start=',
        )

        errors = validate_authored_video_html(html, contract)

        self.assertTrue(any(
            'class="clip"' in error and "data-hf-clip" in error
            for error in errors
        ))

    def test_authored_html_rejects_known_hyperframes_lint_blockers(self) -> None:
        contract = VideoDeliveryContract(scenes=_scenes())
        html = _authored_html()
        html = html.replace(" data-no-timeline", "")
        html = html.replace(
            "</div></body>",
            '<div data-composition-id="main"></div>'
            '<script>requestAnimationFrame(() => {});</script></div></body>',
        )

        errors = validate_authored_video_html(html, contract)

        self.assertTrue(any("data-no-timeline" in error for error in errors))
        self.assertTrue(any("data-composition-id" in error and "unique" in error for error in errors))
        self.assertTrue(any("requestAnimationFrame" in error for error in errors))

    def test_authored_html_allows_a_registered_timeline_for_its_root(self) -> None:
        contract = VideoDeliveryContract(scenes=_scenes())
        html = _authored_html().replace(" data-no-timeline", "")
        html = html.replace(
            "</div></body>",
            '<script>window.__timelines = {}; window.__timelines["main"] = timeline;</script>'
            "</div></body>",
        )

        errors = validate_authored_video_html(html, contract)

        self.assertEqual(errors, [])

    def test_authored_html_rejects_a_registry_without_a_root_entry(self) -> None:
        contract = VideoDeliveryContract(scenes=_scenes())
        html = _authored_html().replace(" data-no-timeline", "")
        html = html.replace(
            "</div></body>",
            "<script>window.__timelines = {};</script></div></body>",
        )

        errors = validate_authored_video_html(html, contract)

        self.assertTrue(any("data-no-timeline" in error for error in errors))

    def test_internal_composer_prompt_states_the_hyperframes_protocol(self) -> None:
        prompt = (REPO_ROOT / "prompts" / "hyperframes_composer.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("data-no-timeline", prompt)
        self.assertIn("exactly one root", prompt)
        self.assertIn("requestAnimationFrame", prompt)
        self.assertIn('class="clip"', prompt)

    def test_authored_html_rejects_request_animation_frame_in_local_javascript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "app.js").write_text(
                "requestAnimationFrame(() => {});", encoding="utf-8"
            )
            html = _authored_html().replace(
                "</body>", '<script src="app.js"></script></body>'
            )

            errors = validate_authored_video_html(html, project_dir=project)

        self.assertTrue(any("requestAnimationFrame" in error for error in errors))

    def test_hyperframes_0764_lints_real_audio_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "assets").mkdir()
            (project / "assets" / "narration.wav").write_bytes(b"fixture")
            (project / "index.html").write_text(_authored_html(), encoding="utf-8")
            try:
                proc = subprocess.run(
                    _hyperframes_command("lint", "--json", "."),
                    cwd=project,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except FileNotFoundError:
                self.skipTest("pinned HyperFrames CLI is unavailable")

        self.assertEqual(proc.returncode, 0, (proc.stdout or "") + (proc.stderr or ""))

    def test_hyperframes_0764_rejects_generated_audio_before_it_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "assets").mkdir()
            (project / "index.html").write_text(_authored_html(), encoding="utf-8")
            try:
                proc = subprocess.run(
                    _hyperframes_command("lint", "--json", "."),
                    cwd=project,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except FileNotFoundError:
                self.skipTest("pinned HyperFrames CLI is unavailable")

        output = (proc.stdout or "") + (proc.stderr or "")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("audio_src_not_found", output)
        self.assertIn("assets/narration.wav", output)

    def test_authoring_lint_stages_and_removes_generated_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            narration = project / "assets" / "narration.wav"
            narration.parent.mkdir()
            (project / "index.html").write_text(_authored_html(), encoding="utf-8")

            output, ok = _run_hyperframes_authoring_lint(project)

            self.assertTrue(ok, output)
            self.assertFalse(narration.exists())

    def test_authored_html_rejects_short_timeline_or_too_few_scenes(self) -> None:
        html = """<!doctype html><html><body>
<div data-composition-id="main" data-duration="60" data-width="1920" data-height="1080">
  <section class="clip" data-narration="English narration."></section>
</div></body></html>"""

        errors = validate_authored_video_html(html)

        self.assertTrue(any("300-600" in error for error in errors))
        self.assertTrue(any("10-14" in error for error in errors))

    def test_authored_html_requires_local_kokoro_narration_audio(self) -> None:
        scenes = "".join(
            f'<section class="clip" data-narration="English scene {index}."></section>'
            for index in range(10)
        )
        html = (
            '<!doctype html><html><body><div data-composition-id="main" '
            'data-duration="360" data-width="1920" data-height="1080">'
            f"{scenes}</div></body></html>"
        )

        errors = validate_authored_video_html(html)

        self.assertTrue(any("assets/narration.wav" in error for error in errors))

    def test_composer_failure_removes_stale_html_and_writes_no_placeholder(self) -> None:
        composer = composer_module.HyperFramesComposer.__new__(
            composer_module.HyperFramesComposer
        )
        composer.settings = SimpleNamespace()
        composer.system_prompt = "system"
        composer.backend = SimpleNamespace(
            model="test-model",
            name="mock",
            create_turn=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
        )
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            index_path = project / "index.html"
            index_path.write_text("stale", encoding="utf-8")

            result = composer.compose("context", project)

            self.assertFalse(index_path.exists())

        self.assertTrue(result.skipped)
        self.assertEqual(result.index_html, "")
        self.assertIn("api_error", result.skip_reason)


class VideoArtifactContractTest(unittest.TestCase):
    def test_export_only_retry_reuses_authored_project_and_writes_final_pointer(
        self,
    ) -> None:
        video_module = importlib.import_module("autodesign.tools.export_video")
        retry_export = getattr(video_module, "retry_video_export_project", None)
        self.assertTrue(
            callable(retry_export),
            "video delivery must expose an export-only retry boundary",
        )
        scenes = _scenes()
        scene_manifest = [scene.model_dump(mode="json") for scene in scenes]
        contract = VideoDeliveryContract(scenes=scenes)
        spec = {"artifact_type": "video", "name": "Paper video"}
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp) / "run"
            project = run_dir / "hyperframes-paper-video"
            (project / "assets").mkdir(parents=True)
            (project / "renders").mkdir()
            (project / "narration").mkdir()
            (project / "index.html").write_text(
                _authored_html(scenes),
                encoding="utf-8",
            )
            (project / "video_delivery_contract.json").write_text(
                json.dumps(contract.model_dump(mode="json"), indent=2) + "\n",
                encoding="utf-8",
            )
            (project / "scene_graph.json").write_text(
                json.dumps({"scenes": scene_manifest}, indent=2) + "\n",
                encoding="utf-8",
            )
            (run_dir / "design_spec.json").write_text(
                json.dumps(
                    {
                        "design_spec": spec,
                        "design_spec_sha256": design_spec_sha256(spec),
                        "revision": 3,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            _write_narration_artifacts(project, scene_manifest, "female")
            mp4_path = project / "renders" / "retried.mp4"
            captioned_mp4_path = project / "renders" / "retried-captions.mp4"
            probe = VideoMediaProbe(
                video_codec="h264",
                pixel_format="yuv420p",
                audio_codec="aac",
                width=1920,
                height=1080,
                fps=30,
                duration_s=360,
            )
            captioned_probe = probe.model_copy(
                update={"subtitle_codec": "mov_text", "subtitle_forced": False}
            )
            events: list[tuple[str, bytes | None]] = []

            def lint(proj_dir, **_kwargs):
                audio = proj_dir / "assets" / "narration.wav"
                events.append(("lint", audio.read_bytes() if audio.is_file() else None))
                return "lint ok", True

            def synthesize(proj_dir, *, scene_manifest, **_kwargs):
                audio = proj_dir / "assets" / "narration.wav"
                events.append(("tts", audio.read_bytes() if audio.is_file() else None))
                audio.write_bytes(b"mock wav")
                timing = [
                    {
                        "scene_id": scene["scene_id"],
                        "start_s": scene["start_s"],
                        "speech_duration_s": 25.0,
                        "end_s": scene["start_s"] + 25.0,
                        "speed": 1.0,
                    }
                    for scene in scene_manifest
                ]
                return "tts ok", True, audio, timing

            def render(proj_dir, *_args, **_kwargs):
                audio = proj_dir / "assets" / "narration.wav"
                events.append(("render", audio.read_bytes() if audio.is_file() else None))
                mp4_path.write_bytes(b"mock mp4")
                return "render ok", True, mp4_path, probe

            def mux(raw_mp4_path, subtitle_path, **_kwargs):
                self.assertEqual(raw_mp4_path, mp4_path)
                self.assertTrue(subtitle_path.is_file())
                captioned_mp4_path.write_bytes(b"mock captioned mp4")
                return "subtitle mux ok", True, captioned_mp4_path

            with (
                patch.object(
                    video_module,
                    "_run_hyperframes_lint",
                    side_effect=lint,
                ),
                patch.object(
                    video_module,
                    "_synthesize_timed_narration",
                    side_effect=synthesize,
                ),
                patch.object(
                    video_module,
                    "_run_hyperframes_render",
                    side_effect=render,
                ),
                patch.object(
                    video_module,
                    "_mux_optional_subtitle_track",
                    side_effect=mux,
                ) as mux_mock,
                patch.object(
                    video_module,
                    "_probe_video",
                    return_value=(captioned_probe, None),
                ) as probe_mock,
            ):
                result = retry_export(run_dir, project)

            self.assertTrue(result["ok"], result)
            pointer = json.loads(
                (run_dir / "final" / "video_delivery.json").read_text(
                    encoding="utf-8"
                )
            )
            manifest = json.loads(
                (project / "delivery_manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(manifest["status"], "passed")
        self.assertEqual(manifest["design_spec_revision"], 3)
        self.assertEqual(pointer["design_spec_sha256"], design_spec_sha256(spec))
        self.assertEqual(result["mp4_path"], str(captioned_mp4_path.resolve()))
        self.assertEqual(mux_mock.call_args.args, (
            mp4_path,
            (project / "narration" / "subtitles.en.srt").resolve(),
        ))
        self.assertEqual(probe_mock.call_args.args, (captioned_mp4_path,))
        self.assertEqual(
            [name for name, _payload in events],
            ["lint", "tts", "lint", "render"],
        )
        self.assertIsNotNone(events[0][1])
        self.assertEqual(events[1][1], None)
        self.assertEqual(events[2][1], b"mock wav")
        self.assertEqual(events[3][1], b"mock wav")

    def test_narration_artifacts_use_measured_speech_timing_for_srt_vtt(self) -> None:
        scene_manifest = [scene.model_dump(mode="json") for scene in _scenes()]
        for index, scene in enumerate(scene_manifest, start=1):
            scene["narration_text"] = f"Measured English narration for scene {index}."
        speech_timing = [
            {
                "scene_id": scene["scene_id"],
                "start_s": scene["start_s"],
                "speech_duration_s": 4.25,
                "end_s": scene["start_s"] + 4.25,
                "speed": 1.0,
            }
            for scene in scene_manifest
        ]
        with tempfile.TemporaryDirectory() as tmp:
            first = _write_narration_artifacts(
                Path(tmp), scene_manifest, "male", speech_timing=speech_timing
            )
            first_contents = {
                name: (Path(tmp) / rel_path).read_text(encoding="utf-8")
                for name, rel_path in first.items()
                if name in {"transcript_path", "srt_path", "vtt_path", "voice_metadata_path"}
            }
            second = _write_narration_artifacts(
                Path(tmp), scene_manifest, "male", speech_timing=speech_timing
            )
            second_contents = {
                name: (Path(tmp) / rel_path).read_text(encoding="utf-8")
                for name, rel_path in second.items()
                if name in first_contents
            }

            metadata = json.loads(first_contents["voice_metadata_path"])

        self.assertEqual(first, second)
        self.assertEqual(first_contents, second_contents)
        self.assertTrue(first_contents["vtt_path"].startswith("WEBVTT\n\n"))
        self.assertIn("00:00:00,000 --> 00:00:04,250", first_contents["srt_path"])
        self.assertNotIn("00:00:00,000 --> 00:00:30,000", first_contents["srt_path"])
        self.assertEqual(metadata["preset"], "male")
        self.assertEqual(metadata["kokoro_voice_id"], KOKORO_VOICE_BY_PRESET["male"])
        self.assertEqual(metadata["language"], "en")

    def test_subtitles_are_segmented_into_readable_timed_cues(self) -> None:
        scene = _scenes(count=1, duration_s=30.0)[0].model_dump(mode="json")
        scene["narration_text"] = (
            "This scene introduces the research question, explains the central method, "
            "connects the architecture to the paper evidence, compares the strongest "
            "results with prior work, and closes with limitations that matter for a "
            "careful conference audience."
        )
        speech_timing = [{
            "scene_id": scene["scene_id"],
            "start_s": 0.0,
            "speech_duration_s": 22.5,
            "end_s": 22.5,
            "speed": 1.0,
        }]
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = _write_narration_artifacts(
                Path(tmp), [scene], "female", speech_timing=speech_timing
            )
            srt = (Path(tmp) / artifacts["srt_path"]).read_text(encoding="utf-8")

        cue_blocks = [block for block in srt.strip().split("\n\n") if block.strip()]
        self.assertGreaterEqual(len(cue_blocks), 3)
        previous_end_ms = 0
        for block in cue_blocks:
            lines = block.splitlines()
            start_raw, end_raw = lines[1].split(" --> ")
            text = " ".join(lines[2:])

            def _milliseconds(raw: str) -> int:
                hours, minutes, seconds, milliseconds = raw.replace(",", ":").split(":")
                return (
                    int(hours) * 3_600_000
                    + int(minutes) * 60_000
                    + int(seconds) * 1000
                    + int(milliseconds)
                )

            start_ms = _milliseconds(start_raw)
            end_ms = _milliseconds(end_raw)
            duration_s = (end_ms - start_ms) / 1000
            self.assertGreaterEqual(start_ms, previous_end_ms)
            self.assertLessEqual(duration_s, 7.0)
            self.assertLessEqual(max(map(len, lines[2:])), 42)
            self.assertLessEqual(len(text) / duration_s, 20.0)
            previous_end_ms = end_ms

    def test_subtitle_soft_limit_warns_without_blocking_delivery_artifacts(self) -> None:
        scene = _scenes(count=1, duration_s=30.0)[0].model_dump(mode="json")
        scene["narration_text"] = " ".join(["abcdefghi"] * 8)
        speech_timing = [{
            "scene_id": scene["scene_id"],
            "start_s": 0.0,
            "speech_duration_s": 3.9,
            "end_s": 3.9,
            "speed": 1.0,
        }]

        with tempfile.TemporaryDirectory() as tmp:
            artifacts = _write_narration_artifacts(
                Path(tmp), [scene], "female", speech_timing=speech_timing
            )
            diagnostics = artifacts["subtitle_diagnostics"]
            srt_path = Path(tmp) / artifacts["srt_path"]

            self.assertTrue(srt_path.is_file())

        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["scene_id"], "scene_01")
        self.assertTrue(diagnostics[0]["soft_exceeded"])
        self.assertFalse(diagnostics[0]["hard_exceeded"])
        self.assertGreater(diagnostics[0]["max_cps"], 20.0)
        self.assertLess(diagnostics[0]["max_cps"], 24.0)

    @patch("autodesign.tools.export_video._build_timed_narration_mix")
    @patch("autodesign.tools.export_video._probe_audio_duration")
    @patch("autodesign.tools.export_video._run_kokoro_tts")
    def test_timed_narration_synthesizes_each_scene_and_refits_only_overflow(
        self, tts_mock, probe_mock, mix_mock
    ) -> None:
        scene_manifest = [scene.model_dump(mode="json") for scene in _scenes()[:2]]

        def _tts(
            project, *, transcript_path, voice_id, output_path=None, speed=1.0,
            **_kwargs,
        ):
            assert output_path is not None
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"wav")
            return f"speed={speed}", True, output_path

        tts_mock.side_effect = _tts
        probe_mock.side_effect = [(31.0, None), (27.0, None), (6.0, None)]

        def _mix(project, *, segments, target_duration_s, output_path, **_kwargs):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"mix")
            return "mixed", True

        mix_mock.side_effect = _mix
        with tempfile.TemporaryDirectory() as tmp:
            output, ok, audio_path, timings = _synthesize_timed_narration(
                Path(tmp),
                scene_manifest=scene_manifest,
                voice_id=KOKORO_VOICE_BY_PRESET["male"],
                target_duration_s=60,
            )

        self.assertTrue(ok, output)
        self.assertIsNotNone(audio_path)
        self.assertEqual(tts_mock.call_count, 3)
        self.assertGreater(tts_mock.call_args_list[1].kwargs["speed"], 1.0)
        self.assertEqual(tts_mock.call_args_list[2].kwargs["speed"], 1.0)
        self.assertEqual([timing["start_s"] for timing in timings], [0.0, 30.0])
        self.assertEqual([timing["speech_duration_s"] for timing in timings], [27.0, 6.0])
        mix_segments = mix_mock.call_args.kwargs["segments"]
        self.assertEqual([segment["start_s"] for segment in mix_segments], [0.0, 30.0])

    @patch("autodesign.tools.export_video._build_timed_narration_mix")
    @patch("autodesign.tools.export_video._probe_audio_duration")
    @patch("autodesign.tools.export_video._run_kokoro_tts")
    def test_timed_narration_refits_again_when_kokoro_speed_is_nonlinear(
        self, tts_mock, probe_mock, mix_mock
    ) -> None:
        scene_manifest = [scene.model_dump(mode="json") for scene in _scenes()[:1]]

        def _tts(
            project, *, transcript_path, voice_id, output_path=None, speed=1.0,
            **_kwargs,
        ):
            assert output_path is not None
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"wav")
            return f"speed={speed}", True, output_path

        tts_mock.side_effect = _tts
        probe_mock.side_effect = [(31.168, None), (30.059, None), (29.5, None)]

        def _mix(project, *, segments, target_duration_s, output_path, **_kwargs):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"mix")
            return "mixed", True

        mix_mock.side_effect = _mix
        with tempfile.TemporaryDirectory() as tmp:
            output, ok, audio_path, timings = _synthesize_timed_narration(
                Path(tmp),
                scene_manifest=scene_manifest,
                voice_id=KOKORO_VOICE_BY_PRESET["female"],
                target_duration_s=30,
            )

        self.assertTrue(ok, output)
        self.assertIsNotNone(audio_path)
        self.assertEqual(tts_mock.call_count, 3)
        speeds = [call.kwargs["speed"] for call in tts_mock.call_args_list]
        self.assertGreater(speeds[2], speeds[1])
        self.assertEqual(timings[0]["speech_duration_s"], 29.5)

    @patch("autodesign.tools.export_video._build_timed_narration_mix")
    @patch("autodesign.tools.export_video._probe_audio_duration")
    @patch("autodesign.tools.export_video._run_kokoro_tts")
    def test_timed_narration_tries_max_conservative_speed_before_failing(
        self, tts_mock, probe_mock, mix_mock
    ) -> None:
        scene_manifest = [scene.model_dump(mode="json") for scene in _scenes()[:1]]

        def _tts(
            project, *, transcript_path, voice_id, output_path=None, speed=1.0,
            **_kwargs,
        ):
            assert output_path is not None
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"wav")
            return f"speed={speed}", True, output_path

        tts_mock.side_effect = _tts
        probe_mock.side_effect = [(35.0, None), (33.0, None), (29.5, None)]

        def _mix(project, *, segments, target_duration_s, output_path, **_kwargs):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"mix")
            return "mixed", True

        mix_mock.side_effect = _mix
        with tempfile.TemporaryDirectory() as tmp:
            output, ok, audio_path, timings = _synthesize_timed_narration(
                Path(tmp),
                scene_manifest=scene_manifest,
                voice_id=KOKORO_VOICE_BY_PRESET["female"],
                target_duration_s=30,
            )

        self.assertTrue(ok, output)
        self.assertIsNotNone(audio_path)
        self.assertEqual(
            [call.kwargs["speed"] for call in tts_mock.call_args_list],
            [1.0, 1.2, 1.25],
        )
        self.assertEqual(timings[0]["speed"], 1.25)

    @patch("autodesign.tools.export_video._build_timed_narration_mix")
    @patch("autodesign.tools.export_video._probe_audio_duration")
    @patch("autodesign.tools.export_video._run_kokoro_tts")
    def test_timed_narration_uses_bounded_delivery_fallback_for_real_overflow(
        self, tts_mock, probe_mock, mix_mock
    ) -> None:
        scene = _scenes(count=1, duration_s=30.0)[0].model_dump(mode="json")
        original_scene = dict(scene)

        def _tts(
            project, *, transcript_path, voice_id, output_path=None, speed=1.0,
            **_kwargs,
        ):
            assert output_path is not None
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(f"wav at {speed:.2f}".encode())
            return f"speed={speed}", True, output_path

        tts_mock.side_effect = _tts
        # The real failure measured 30.677 s at the normal 1.25 cap. Kokoro's
        # response is nonlinear, so the first delivery probe can still miss.
        probe_mock.side_effect = [
            (38.0, None),
            (30.677, None),
            (30.2, None),
            (29.7, None),
        ]

        def _mix(project, *, segments, target_duration_s, output_path, **_kwargs):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"mix")
            return "mixed", True

        mix_mock.side_effect = _mix
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            output, ok, audio_path, timings = _synthesize_timed_narration(
                project,
                scene_manifest=[scene],
                voice_id=KOKORO_VOICE_BY_PRESET["female"],
                target_duration_s=30,
            )
            transcript = (
                project / "narration" / "scenes" / "01-scene-01.txt"
            ).read_text(encoding="utf-8")

        self.assertTrue(ok, output)
        self.assertIsNotNone(audio_path)
        self.assertEqual(scene, original_scene)
        self.assertEqual(
            transcript,
            " ".join(original_scene["narration_text"].split()) + "\n",
        )
        self.assertEqual(
            [call.kwargs["speed"] for call in tts_mock.call_args_list],
            [1.0, 1.25, 1.32, 1.35],
        )
        self.assertEqual(timings[0]["speed"], 1.35)
        self.assertEqual(timings[0]["speech_duration_s"], 29.7)
        self.assertLessEqual(timings[0]["end_s"], 30.0)
        self.assertEqual(mix_mock.call_args.kwargs["segments"][0]["start_s"], 0.0)
        self.assertEqual(mix_mock.call_args.kwargs["target_duration_s"], 30)

    @patch("autodesign.tools.export_video._build_timed_narration_mix")
    @patch("autodesign.tools.export_video._probe_audio_duration")
    @patch("autodesign.tools.export_video._run_kokoro_tts")
    def test_timed_narration_reports_all_scenes_that_do_not_fit(
        self, tts_mock, probe_mock, mix_mock
    ) -> None:
        scene_manifest = [scene.model_dump(mode="json") for scene in _scenes()[:2]]

        def _tts(
            project, *, transcript_path, voice_id, output_path=None, speed=1.0,
            **_kwargs,
        ):
            assert output_path is not None
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"wav")
            return f"speed={speed}", True, output_path

        tts_mock.side_effect = _tts
        probe_mock.side_effect = [
            (40.0, None),
            (35.0, None),
            (34.0, None),
            (45.0, None),
            (40.0, None),
            (39.0, None),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            output, ok, audio_path, timings = _synthesize_timed_narration(
                Path(tmp),
                scene_manifest=scene_manifest,
                voice_id=KOKORO_VOICE_BY_PRESET["female"],
                target_duration_s=60,
            )

        self.assertFalse(ok)
        self.assertIsNone(audio_path)
        self.assertEqual(timings, [])
        self.assertEqual(tts_mock.call_count, 6)
        self.assertTrue(output.startswith("narration_timing_unfit "), output)
        self.assertIn("scene=scene_01", output)
        self.assertIn("measured=34.000s", output)
        self.assertIn("available=29.750s", output)
        self.assertIn("max_speed=1.35", output)
        self.assertIn("final_speed=1.35", output)
        self.assertIn("scene_01", output)
        self.assertIn("scene_02", output)
        mix_mock.assert_not_called()


    def test_ffmpeg_mix_places_speech_at_scene_starts_and_has_full_duration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            segment_dir = project / "narration" / "scenes"
            segment_dir.mkdir(parents=True)
            first = segment_dir / "first.wav"
            second = segment_dir / "second.wav"
            for path, frequency in ((first, 440), (second, 880)):
                subprocess.run(
                    [
                        "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        f"sine=frequency={frequency}:duration=0.25", "-y", str(path),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            output_path = project / "assets" / "narration.wav"

            output, ok = _build_timed_narration_mix(
                project,
                segments=[
                    {"path": first, "start_s": 0.5},
                    {"path": second, "start_s": 1.5},
                ],
                target_duration_s=3.0,
                output_path=output_path,
            )
            duration, error = _probe_audio_duration(output_path)

            self.assertTrue(ok, output)
            self.assertIsNone(error)
            self.assertAlmostEqual(duration or 0, 3.0, places=2)

    @patch("autodesign.tools.export_video._run_video_process")
    def test_kokoro_tts_uses_resolved_voice_and_requires_fresh_audio(self, run_mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            transcript = project / "narration" / "transcript.en.txt"
            transcript.parent.mkdir()
            transcript.write_text("English narration.", encoding="utf-8")

            def _write_audio(command, **kwargs):
                output = project / command[command.index("--output") + 1]
                output.parent.mkdir(exist_ok=True)
                output.write_bytes(b"mock wav")
                return subprocess.CompletedProcess([], 0, '{"ok":true}', "")

            run_mock.side_effect = _write_audio

            output, ok, audio_path = _run_kokoro_tts(
                project,
                transcript_path=transcript,
                voice_id=KOKORO_VOICE_BY_PRESET["male"],
            )

        self.assertTrue(ok)
        self.assertIsNotNone(audio_path)
        self.assertIn("ok", output)
        command = run_mock.call_args.args[0]
        self.assertTrue(
            command[0].endswith("runtime/video/node_modules/.bin/hyperframes")
        )
        self.assertEqual(command[1], "tts")
        self.assertEqual(
            command[command.index("--voice") + 1],
            KOKORO_VOICE_BY_PRESET["male"],
        )
        self.assertEqual(command[command.index("--lang") + 1], "en-us")

    @patch("autodesign.tools.export_video._run_video_process")
    def test_lint_tooling_failure_is_not_reported_as_success(self, run_mock) -> None:
        run_mock.side_effect = FileNotFoundError("hyperframes")

        output, ok = _run_hyperframes_lint(Path("/tmp/project"))

        self.assertFalse(ok)
        self.assertIn("HyperFrames", output)

    @patch("autodesign.tools.export_video._probe_video")
    @patch("autodesign.tools.export_video._run_video_process")
    def test_render_rejects_stale_mp4_even_when_command_exits_zero(
        self, run_mock, probe_mock
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            stale = project / "renders" / "stale.mp4"
            stale.parent.mkdir()
            stale.write_bytes(b"old")
            stale.touch()
            start_ns = stale.stat().st_mtime_ns + 1
            run_mock.return_value = subprocess.CompletedProcess([], 0, "rendered", "")

            output, ok, mp4_path, probe = _run_hyperframes_render(
                project, "paper-video", render_started_ns=start_ns
            )

        self.assertFalse(ok)
        self.assertIsNone(mp4_path)
        self.assertIsNone(probe)
        self.assertIn("fresh", output)
        probe_mock.assert_not_called()
        command = run_mock.call_args.args[0]
        self.assertTrue(
            command[0].endswith("runtime/video/node_modules/.bin/hyperframes")
        )
        self.assertEqual(command[1], "render")
        self.assertIn("--strict", command)
        self.assertIn("--no-best-effort", command)
        self.assertEqual(command[command.index("--fps") + 1], "30")
        self.assertEqual(command[command.index("--resolution") + 1], "landscape")

    @patch("autodesign.tools.export_video._run_video_process")
    def test_ffprobe_contract_requires_h264_yuv420p_aac_1080p30_and_duration(
        self, run_mock
    ) -> None:
        from autodesign.tools.export_video import _probe_video

        valid_probe = {
            "streams": [
                {
                    "codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p",
                    "width": 1920, "height": 1080,
                    "avg_frame_rate": "30/1", "r_frame_rate": "30/1",
                    "duration": "360.0", "nb_read_frames": "10800",
                },
                {"codec_type": "audio", "codec_name": "aac", "duration": "360.0"},
            ],
            "format": {"duration": "360.0"},
        }
        run_mock.return_value = subprocess.CompletedProcess(
            [], 0, json.dumps(valid_probe), ""
        )

        probe, error = _probe_video(Path("video.mp4"))

        self.assertIsNone(error)
        self.assertEqual(probe.duration_s, 360.0)
        self.assertEqual(probe.video_codec, "h264")
        self.assertEqual(probe.audio_codec, "aac")
        self.assertEqual(probe.video_frame_count, 10800)

        invalid_probe = dict(valid_probe)
        invalid_probe["streams"] = [dict(valid_probe["streams"][0], pix_fmt="yuv444p")]
        run_mock.return_value = subprocess.CompletedProcess(
            [], 0, json.dumps(invalid_probe), ""
        )

        probe, error = _probe_video(Path("video.mp4"))

        self.assertIsNone(probe)
        self.assertIn("yuv420p", error or "")

        truncated_probe = json.loads(json.dumps(valid_probe))
        truncated_probe["streams"][0]["duration"] = "120.0"
        truncated_probe["streams"][0]["nb_read_frames"] = "3600"
        truncated_probe["streams"][1]["duration"] = "10.0"
        run_mock.return_value = subprocess.CompletedProcess(
            [], 0, json.dumps(truncated_probe), ""
        )

        probe, error = _probe_video(Path("video.mp4"))

        self.assertIsNone(probe)
        self.assertIn("shorter than", error or "")

    @patch("autodesign.tools.export_video._run_video_process")
    def test_ffprobe_records_an_optional_non_forced_subtitle_track(
        self, run_mock
    ) -> None:
        from autodesign.tools.export_video import _probe_video

        raw_probe = {
            "streams": [
                {
                    "codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p",
                    "width": 1920, "height": 1080,
                    "avg_frame_rate": "30/1", "r_frame_rate": "30/1",
                    "duration": "360.0", "nb_read_frames": "10800",
                },
                {"codec_type": "audio", "codec_name": "aac", "duration": "360.0"},
                {
                    "codec_type": "subtitle", "codec_name": "mov_text",
                    "disposition": {"forced": 0},
                },
            ],
            "format": {"duration": "360.0"},
        }
        run_mock.return_value = subprocess.CompletedProcess(
            [], 0, json.dumps(raw_probe), ""
        )

        probe, error = _probe_video(Path("captioned.mp4"))

        self.assertIsNone(error)
        self.assertEqual(probe.subtitle_codec, "mov_text")
        self.assertFalse(probe.subtitle_forced)

    def test_new_video_attempt_clears_old_final_pointer_across_video_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            final_pointer = run_dir / "final" / "video_delivery.json"
            manifest_path, mp4_path = _passed_delivery(run_dir)
            prior_pointer_bytes = final_pointer.read_bytes()
            prior_pointer = json.loads(prior_pointer_bytes)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            current = validate_current_video_delivery(run_dir)
            current_artifact = _build_video_artifact(
                run_dir,
                "video-retry",
                baseline_artifact_json=None,
            )
            self.assertTrue(current)
            self.assertEqual(current.reason_code, "passed")
            self.assertEqual(current[0], mp4_path.resolve())
            self.assertEqual(
                current.snapshots["mp4"].sha256,
                sha256_file(mp4_path),
            )
            self.assertIsNotNone(current_artifact)
            assert current_artifact is not None
            self.assertEqual(
                current_artifact.native_file_url,
                "/api/files/runs/video-retry/"
                "hyperframes-paper-video/renders/paper-video.mp4",
            )

            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=run_dir,
                layers_dir=run_dir / "layers",
                run_id="video-retry",
            )
            ctx.layers_dir.mkdir()
            ctx.state["video_delivery"] = {
                "status": "passed",
                "delivery_manifest_sha256": prior_pointer["manifest_sha256"],
                "design_spec_sha256": prior_pointer["design_spec_sha256"],
                "design_spec_revision": prior_pointer["design_spec_revision"],
                "render_started_at": manifest["render_started_at"],
            }
            new_project = run_dir / "hyperframes-new-id"
            new_project.mkdir()

            _clear_stale_video_delivery(new_project, ctx)

            tombstone_bytes = final_pointer.read_bytes()
            tombstone = VideoDeliveryInvalidation.from_payload(
                json.loads(tombstone_bytes)
            )
            invalidated_at = datetime.fromisoformat(tombstone.invalidated_at)
            self.assertRegex(tombstone.invalidation_id, r"^[0-9a-f]{32}$")
            self.assertEqual(tombstone.reason, "new_video_export")
            self.assertEqual(
                tombstone.prior_pointer_sha256,
                hashlib.sha256(prior_pointer_bytes).hexdigest(),
            )
            self.assertEqual(
                tombstone.prior_manifest_sha256,
                prior_pointer["manifest_sha256"],
            )
            self.assertEqual(
                tombstone.prior_design_spec_sha256,
                prior_pointer["design_spec_sha256"],
            )
            self.assertEqual(
                tombstone.prior_design_spec_revision,
                prior_pointer["design_spec_revision"],
            )
            self.assertEqual(
                tombstone.prior_render_started_at,
                manifest["render_started_at"],
            )
            self.assertIsNotNone(invalidated_at.tzinfo)
            self.assertNotEqual(tombstone_bytes, prior_pointer_bytes)
            self.assertNotIn("video_delivery", ctx.state)
            invalid = validate_current_video_delivery(run_dir)
            self.assertFalse(invalid)
            self.assertEqual(invalid.reason_code, "pointer_invalidated")
            self.assertEqual(invalid.public_paths, {})
            self.assertEqual(invalid.snapshots, {})
            self.assertIsNone(
                _build_video_artifact(
                    run_dir,
                    "video-retry",
                    baseline_artifact_json=None,
                )
            )


class ExportVideoFailureSemanticsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tts_patcher = patch(
            "autodesign.tools.export_video._synthesize_timed_narration"
        )
        self.tts_mock = self.tts_patcher.start()

        def _mock_tts(proj_dir, *, scene_manifest, **kwargs):
            audio_path = proj_dir / "assets" / "narration.wav"
            audio_path.parent.mkdir(parents=True, exist_ok=True)
            audio_path.write_bytes(b"mock wav")
            timings = [
                {
                    "scene_id": scene["scene_id"],
                    "start_s": scene["start_s"],
                    "speech_duration_s": 22.0,
                    "end_s": scene["start_s"] + 22.0,
                    "speed": 1.0,
                }
                for scene in scene_manifest
            ]
            return "", True, audio_path, timings

        self.tts_mock.side_effect = _mock_tts

    def tearDown(self) -> None:
        self.tts_patcher.stop()

    def _context(
        self,
        root: Path,
        *,
        scenes: list[VideoSceneContract] | None = None,
    ) -> ToolContext:
        scenes = scenes or _scenes()
        settings = SimpleNamespace(
            enable_video_composer=False,
            prompts_dir=root,
            composer_model="test-composer",
        )
        ctx = ToolContext(
            settings=settings,
            run_dir=root,
            layers_dir=root / "layers",
            run_id="video-contract-test",
        )
        ctx.state["design_spec"] = SimpleNamespace(
            artifact_type="video",
            brief="Paper title and conference summary",
            palette=["#111111", "#eeeeee"],
            mood=["academic"],
            typography={},
            design_system=None,
            model_dump=lambda mode=None: {
                "artifact_type": "video",
                "brief": "Paper title and conference summary",
                "scene_ids": [scene.scene_id for scene in scenes],
            },
            html_artifact=SimpleNamespace(
                model_dump=lambda mode: {
                    "frames": [
                        {
                            "frame_id": scene.scene_id,
                            "kind": "scene",
                            "title": scene.title,
                            "duration_s": scene.duration_s,
                            "speaker_notes": scene.narration_text,
                            "blocks": [],
                        }
                        for scene in scenes
                    ]
                }
            ),
        )
        return ctx

    def _successful_composer(self, *args, **kwargs):
        class _Composer:
            def compose(self, composer_context, proj_dir, delivery_contract=None):
                (proj_dir / "index.html").write_text(
                    _authored_html(delivery_contract.scenes),
                    encoding="utf-8",
                )
                return SimpleNamespace(
                    skipped=False,
                    skip_reason="",
                    model="mock-composer",
                    wall_time_s=0.01,
                    input_tokens=1,
                    output_tokens=1,
                )

        return _Composer()

    def test_disabled_or_failed_composer_returns_tool_error_not_placeholder_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "hyperframes-video-contract-test-video"
            project.mkdir()
            for name in ("index.html", "media_probe.json", "delivery_manifest.json"):
                (project / name).write_text("stale", encoding="utf-8")
            (project / "renders").mkdir()
            (project / "renders" / "old.mp4").write_bytes(b"old")
            (project / "assets").mkdir()
            (project / "assets" / "narration.wav").write_bytes(b"old")
            ctx = self._context(root)
            ctx.state["video_delivery"] = {"status": "passed"}
            ctx.state["composition"] = SimpleNamespace(
                layer_manifest=[{"kind": "video"}]
            )
            ctx.state["finalized"] = True

            result = export_video({}, ctx=ctx)

            self.assertFalse((project / "index.html").exists())
            self.assertFalse((project / "media_probe.json").exists())
            self.assertFalse((project / "delivery_manifest.json").exists())
            self.assertEqual(list((project / "renders").glob("*.mp4")), [])
            self.assertFalse((project / "assets" / "narration.wav").exists())
            self.assertNotIn("video_delivery", ctx.state)
            self.assertNotIn("composition", ctx.state)
            self.assertNotIn("finalized", ctx.state)

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_category, "validation")
        self.assertFalse(result.payload["index_html_written"])
        self.assertFalse(result.payload["mp4_written"])

    @patch("autodesign.tools.export_video._run_hyperframes_render")
    @patch("autodesign.tools.export_video._run_hyperframes_lint")
    @patch("autodesign.tools.export_video.HyperFramesComposer")
    @patch("autodesign.tools.export_video.load_composer_system_prompt")
    def test_lint_failure_returns_tool_error_and_does_not_render(
        self, prompt_mock, composer_mock, lint_mock, render_mock
    ) -> None:
        prompt_mock.return_value = "system"
        composer_mock.side_effect = self._successful_composer
        lint_mock.return_value = ("lint error", False)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._context(Path(tmp))
            ctx.settings.enable_video_composer = True

            result = export_video({}, ctx=ctx)

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_category, "validation")
        self.assertFalse(result.payload["mp4_written"])
        self.tts_mock.assert_not_called()
        render_mock.assert_not_called()

    @patch("autodesign.tools.export_video._run_hyperframes_render")
    @patch("autodesign.tools.export_video._run_hyperframes_lint")
    @patch("autodesign.tools.export_video.HyperFramesComposer")
    @patch("autodesign.tools.export_video.load_composer_system_prompt")
    def test_authoring_lint_uses_temporary_audio_then_full_lint_uses_tts_audio(
        self, prompt_mock, composer_mock, lint_mock, render_mock
    ) -> None:
        prompt_mock.return_value = "system"
        composer_mock.side_effect = self._successful_composer
        events: list[tuple[str, bytes | None]] = []

        def lint(proj_dir, **_kwargs):
            audio = proj_dir / "assets" / "narration.wav"
            events.append(("lint", audio.read_bytes() if audio.is_file() else None))
            return "", True

        def tts(proj_dir, *, scene_manifest, **_kwargs):
            audio = proj_dir / "assets" / "narration.wav"
            events.append(("tts", audio.read_bytes() if audio.is_file() else None))
            audio.write_bytes(b"real narration")
            timing = [
                {
                    "scene_id": scene["scene_id"],
                    "start_s": scene["start_s"],
                    "speech_duration_s": 22.0,
                    "end_s": scene["start_s"] + 22.0,
                    "speed": 1.0,
                }
                for scene in scene_manifest
            ]
            return "", True, audio, timing

        def render(proj_dir, *_args, **_kwargs):
            audio = proj_dir / "assets" / "narration.wav"
            events.append(("render", audio.read_bytes() if audio.is_file() else None))
            return "render error", False, None, None

        lint_mock.side_effect = lint
        self.tts_mock.side_effect = tts
        render_mock.side_effect = render
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._context(Path(tmp))
            ctx.settings.enable_video_composer = True

            result = export_video({}, ctx=ctx)

        self.assertEqual(result.status, "error")
        self.assertEqual(
            [name for name, _payload in events],
            ["lint", "tts", "lint", "render"],
        )
        self.assertIsNotNone(events[0][1])
        self.assertEqual(events[1][1], None)
        self.assertEqual(events[2][1], b"real narration")
        self.assertEqual(events[3][1], b"real narration")

    @patch("autodesign.tools.export_video._run_hyperframes_lint")
    @patch("autodesign.tools.export_video.HyperFramesComposer")
    @patch("autodesign.tools.export_video.load_composer_system_prompt")
    def test_tts_failure_returns_tool_error_after_lint_preflight(
        self, prompt_mock, composer_mock, lint_mock
    ) -> None:
        prompt_mock.return_value = "system"
        composer_mock.side_effect = self._successful_composer
        lint_mock.return_value = ("", True)
        self.tts_mock.side_effect = None
        self.tts_mock.return_value = ("tts error", False, None, [])
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._context(Path(tmp))
            ctx.settings.enable_video_composer = True

            result = export_video({}, ctx=ctx)
            project = Path(tmp) / "hyperframes-video-contract-test-video"
            self.assertFalse((project / "narration" / "subtitles.en.srt").exists())
            self.assertFalse((project / "narration" / "subtitles.en.vtt").exists())

        self.assertEqual(result.status, "error")
        self.assertIn("tts error", result.error_message or "")
        lint_mock.assert_called_once()

    @patch("autodesign.tools.export_video._run_hyperframes_lint")
    @patch("autodesign.tools.export_video.HyperFramesComposer")
    @patch("autodesign.tools.export_video.load_composer_system_prompt")
    def test_narration_timing_failure_is_not_mislabeled_as_synthesis_failure(
        self, prompt_mock, composer_mock, lint_mock
    ) -> None:
        prompt_mock.return_value = "system"
        composer_mock.side_effect = self._successful_composer
        lint_mock.return_value = ("", True)
        timing_error = (
            "narration_timing_unfit scene=scene_11 measured=30.677s "
            "available=29.750s max_speed=1.35 final_speed=1.35"
        )
        self.tts_mock.side_effect = None
        self.tts_mock.return_value = (timing_error, False, None, [])
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._context(Path(tmp))
            ctx.settings.enable_video_composer = True

            result = export_video({}, ctx=ctx)

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_message, timing_error)
        self.assertNotIn("Kokoro narration synthesis failed", result.error_message or "")
        self.assertEqual(
            result.payload.get("delivery_failure_kind"),
            "narration_timing_unfit",
        )
        self.assertIs(result.payload.get("delivery_repairable"), True)

    @patch("autodesign.tools.export_video._run_hyperframes_render")
    @patch("autodesign.tools.export_video._run_hyperframes_lint")
    @patch("autodesign.tools.export_video.HyperFramesComposer")
    @patch("autodesign.tools.export_video.load_composer_system_prompt")
    def test_render_failure_returns_tool_error(
        self, prompt_mock, composer_mock, lint_mock, render_mock
    ) -> None:
        prompt_mock.return_value = "system"
        composer_mock.side_effect = self._successful_composer
        lint_mock.return_value = ("", True)
        render_mock.return_value = ("render error", False, None, None)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._context(Path(tmp))
            ctx.settings.enable_video_composer = True

            result = export_video({}, ctx=ctx)

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_category, "validation")
        self.assertIn("render error", result.error_message or "")
        self.assertFalse(result.payload["mp4_written"])

    @patch("autodesign.tools.export_video._run_hyperframes_render")
    @patch("autodesign.tools.export_video._run_hyperframes_lint")
    @patch("autodesign.tools.export_video.HyperFramesComposer")
    @patch("autodesign.tools.export_video.load_composer_system_prompt")
    def test_low_measured_speech_coverage_is_formal_delivery_failure_before_render(
        self, prompt_mock, composer_mock, lint_mock, render_mock
    ) -> None:
        prompt_mock.return_value = "system"
        composer_mock.side_effect = self._successful_composer
        lint_mock.return_value = ("", True)
        self.tts_mock.side_effect = lambda proj_dir, *, scene_manifest, **kwargs: (
            "",
            True,
            (proj_dir / "assets" / "narration.wav"),
            [
                {
                    "scene_id": scene["scene_id"],
                    "start_s": scene["start_s"],
                    "speech_duration_s": 5.0,
                    "end_s": scene["start_s"] + 5.0,
                    "speed": 1.0,
                }
                for scene in scene_manifest
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "hyperframes-video-contract-test-video"

            def _tts_with_audio(proj_dir, *, scene_manifest, **kwargs):
                audio = proj_dir / "assets" / "narration.wav"
                audio.parent.mkdir(parents=True, exist_ok=True)
                audio.write_bytes(b"mock wav")
                return (
                    "",
                    True,
                    audio,
                    [
                        {
                            "scene_id": scene["scene_id"],
                            "start_s": scene["start_s"],
                            "speech_duration_s": 5.0,
                            "end_s": scene["start_s"] + 5.0,
                            "speed": 1.0,
                        }
                        for scene in scene_manifest
                    ],
                )

            self.tts_mock.side_effect = _tts_with_audio
            ctx = self._context(root)
            ctx.settings.enable_video_composer = True
            result = export_video({}, ctx=ctx)
            manifest = json.loads(
                (project / "delivery_manifest.json").read_text(encoding="utf-8")
            )
            contract = json.loads(
                (project / "video_delivery_contract.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result.status, "error")
        self.assertIn("speech coverage", (result.error_message or "").lower())
        self.assertAlmostEqual(result.payload["speech_coverage_ratio"], 1 / 6)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failure_reason"], "speech_coverage_below_minimum")
        self.assertEqual(contract["narration_contract"]["minimum_spoken_wpm"], 90)
        self.assertEqual(
            contract["narration_contract"]["minimum_speech_coverage_ratio"], 0.72
        )
        lint_mock.assert_called_once()
        render_mock.assert_not_called()

    @patch("autodesign.tools.export_video._mux_optional_subtitle_track")
    @patch("autodesign.tools.export_video._probe_video")
    @patch("autodesign.tools.export_video._run_hyperframes_render")
    @patch("autodesign.tools.export_video._run_hyperframes_lint")
    @patch("autodesign.tools.export_video.HyperFramesComposer")
    @patch("autodesign.tools.export_video.load_composer_system_prompt")
    def test_success_publishes_the_captioned_mp4(
        self,
        prompt_mock,
        composer_mock,
        lint_mock,
        render_mock,
        probe_mock,
        mux_mock,
    ) -> None:
        prompt_mock.return_value = "system"
        composer_mock.side_effect = self._successful_composer
        lint_mock.return_value = ("", True)
        probe = VideoMediaProbe(
            video_codec="h264",
            pixel_format="yuv420p",
            audio_codec="aac",
            width=1920,
            height=1080,
            fps=30,
            duration_s=360,
            subtitle_codec="mov_text",
            subtitle_forced=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "hyperframes-video-contract-test-video"
            source_mp4 = project / "renders" / "fresh.mp4"
            captioned_mp4 = project / "renders" / "fresh-captions.mp4"

            def _render(*_args, **_kwargs):
                source_mp4.parent.mkdir(parents=True, exist_ok=True)
                source_mp4.write_bytes(b"raw mp4")
                return "", True, source_mp4, probe

            def _mux(mp4_path, subtitle_path, **_kwargs):
                self.assertEqual(mp4_path, source_mp4)
                self.assertTrue(subtitle_path.is_file())
                captioned_mp4.write_bytes(b"captioned mp4")
                return "", True, captioned_mp4

            render_mock.side_effect = _render
            mux_mock.side_effect = _mux
            probe_mock.return_value = (probe, None)
            ctx = self._context(root)
            ctx.settings.enable_video_composer = True

            result = export_video({}, ctx=ctx)
            manifest = json.loads(
                (project / "delivery_manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result.status, "ok")
        self.assertEqual(
            mux_mock.call_args.args,
            (source_mp4, project / "narration" / "subtitles.en.srt"),
        )
        self.assertEqual(probe_mock.call_args.args, (captioned_mp4,))
        self.assertEqual(manifest["mp4_path"], "renders/fresh-captions.mp4")

    @patch("autodesign.tools.export_video._prepare_captioned_delivery_mp4")
    @patch("autodesign.tools.export_video._run_hyperframes_render")
    @patch("autodesign.tools.export_video._run_hyperframes_lint")
    @patch("autodesign.tools.export_video.HyperFramesComposer")
    @patch("autodesign.tools.export_video.load_composer_system_prompt")
    def test_success_requires_probe_and_writes_delivery_manifest(
        self, prompt_mock, composer_mock, lint_mock, render_mock, caption_mock
    ) -> None:
        prompt_mock.return_value = "system"
        composer_mock.side_effect = self._successful_composer
        lint_mock.return_value = ("", True)
        probe = VideoMediaProbe(
            video_codec="h264",
            pixel_format="yuv420p",
            audio_codec="aac",
            width=1920,
            height=1080,
            fps=30,
            duration_s=360,
            subtitle_codec="mov_text",
            subtitle_forced=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "hyperframes-video-contract-test-video"
            mp4_path = project / "renders" / "fresh.mp4"
            mp4_path.parent.mkdir(parents=True)
            mp4_path.write_bytes(b"mock mp4")
            def _render(*args, **kwargs):
                mp4_path.parent.mkdir(parents=True, exist_ok=True)
                mp4_path.write_bytes(b"mock mp4")
                return "", True, mp4_path, probe

            render_mock.side_effect = _render
            caption_mock.return_value = "", True, mp4_path, probe
            ctx = self._context(root)
            ctx.settings.enable_video_composer = True

            result = export_video({}, ctx=ctx)
            manifest = json.loads(
                (project / "delivery_manifest.json").read_text(encoding="utf-8")
            )
            video_delivery = dict(ctx.state["video_delivery"])
            composition = ctx.state["composition"]

        self.assertEqual(result.status, "ok")
        self.assertTrue(result.payload["mp4_written"])
        self.assertEqual(result.payload["media_probe"]["audio_codec"], "aac")
        self.assertIn("render_started_at", result.payload)
        self.assertEqual(manifest["status"], "passed")
        self.assertGreaterEqual(manifest["speech_coverage_ratio"], 0.72)
        self.assertGreaterEqual(manifest["spoken_wpm"], 90)
        self.assertEqual(manifest["minimum_speech_coverage_ratio"], 0.72)
        self.assertEqual(manifest["minimum_spoken_wpm"], 90)
        self.assertEqual(manifest["render_started_at"], result.payload["render_started_at"])
        self.assertEqual(manifest["vtt_path"], "narration/subtitles.en.vtt")
        self.assertEqual(video_delivery["status"], "passed")
        self.assertEqual(Path(composition.preview_path).name, "fresh.mp4")

    @patch("autodesign.tools.export_video._prepare_captioned_delivery_mp4")
    @patch("autodesign.tools.export_video._run_hyperframes_render")
    @patch("autodesign.tools.export_video._run_hyperframes_lint")
    @patch("autodesign.tools.export_video.HyperFramesComposer")
    @patch("autodesign.tools.export_video.load_composer_system_prompt")
    def test_post_render_rejects_probe_duration_outside_authored_timeline_tolerance(
        self, prompt_mock, composer_mock, lint_mock, render_mock, caption_mock
    ) -> None:
        prompt_mock.return_value = "system"
        composer_mock.side_effect = self._successful_composer
        lint_mock.return_value = ("", True)
        probe = VideoMediaProbe(
            video_codec="h264",
            pixel_format="yuv420p",
            audio_codec="aac",
            width=1920,
            height=1080,
            fps=30,
            duration_s=361.0,
            subtitle_codec="mov_text",
            subtitle_forced=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "hyperframes-video-contract-test-video"
            mp4_path = project / "renders" / "fresh.mp4"

            def _render(*args, **kwargs):
                mp4_path.parent.mkdir(parents=True, exist_ok=True)
                mp4_path.write_bytes(b"mock mp4")
                return "", True, mp4_path, probe

            render_mock.side_effect = _render
            caption_mock.return_value = "", True, mp4_path, probe
            ctx = self._context(root)
            ctx.settings.enable_video_composer = True

            result = export_video({}, ctx=ctx)
            manifest = json.loads(
                (project / "delivery_manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_category, "validation")
        self.assertTrue(result.payload["delivery_repairable"])
        self.assertEqual(manifest["failure_reason"], "render_duration_mismatch")
        self.assertNotIn("video_delivery", ctx.state)

    @patch("autodesign.tools.export_video._prepare_captioned_delivery_mp4")
    @patch("autodesign.tools.export_video._run_hyperframes_render")
    @patch("autodesign.tools.export_video._run_hyperframes_lint")
    @patch("autodesign.tools.export_video.HyperFramesComposer")
    @patch("autodesign.tools.export_video.load_composer_system_prompt")
    def test_post_render_coverage_uses_final_probe_duration(
        self, prompt_mock, composer_mock, lint_mock, render_mock, caption_mock
    ) -> None:
        prompt_mock.return_value = "system"
        composer_mock.side_effect = self._successful_composer
        lint_mock.return_value = ("", True)
        probe = VideoMediaProbe(
            video_codec="h264",
            pixel_format="yuv420p",
            audio_codec="aac",
            width=1920,
            height=1080,
            fps=30,
            duration_s=360.5,
            subtitle_codec="mov_text",
            subtitle_forced=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "hyperframes-video-contract-test-video"
            mp4_path = project / "renders" / "fresh.mp4"

            def _tts(proj_dir, *, scene_manifest, **kwargs):
                audio_path = proj_dir / "assets" / "narration.wav"
                audio_path.parent.mkdir(parents=True, exist_ok=True)
                audio_path.write_bytes(b"mock wav")
                timings = [
                    {
                        "scene_id": scene["scene_id"],
                        "start_s": scene["start_s"],
                        "speech_duration_s": 21.6,
                        "end_s": scene["start_s"] + 21.6,
                        "speed": 1.0,
                    }
                    for scene in scene_manifest
                ]
                return "", True, audio_path, timings

            def _render(*args, **kwargs):
                mp4_path.parent.mkdir(parents=True, exist_ok=True)
                mp4_path.write_bytes(b"mock mp4")
                return "", True, mp4_path, probe

            self.tts_mock.side_effect = _tts
            render_mock.side_effect = _render
            caption_mock.return_value = "", True, mp4_path, probe
            ctx = self._context(root)
            ctx.settings.enable_video_composer = True

            result = export_video({}, ctx=ctx)
            manifest = json.loads(
                (project / "delivery_manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result.status, "error")
        self.assertIn("speech coverage", (result.error_message or "").lower())
        self.assertAlmostEqual(result.payload["coverage_duration_s"], 360.5)
        self.assertAlmostEqual(result.payload["speech_coverage_ratio"], 259.2 / 360.5)
        self.assertEqual(manifest["failure_reason"], "speech_coverage_below_minimum")

    @patch("autodesign.tools.export_video._run_hyperframes_render")
    @patch("autodesign.tools.export_video._run_hyperframes_lint")
    @patch("autodesign.tools.export_video.HyperFramesComposer")
    @patch("autodesign.tools.export_video.load_composer_system_prompt")
    def test_dense_subtitles_return_structured_repairable_validation_error(
        self, prompt_mock, composer_mock, lint_mock, render_mock
    ) -> None:
        dense_text = " ".join(
            ["spectrotemporal"] * 500
        )
        scenes = [
            scene.model_copy(update={"narration_text": dense_text})
            for scene in _scenes()
        ]
        prompt_mock.return_value = "system"
        composer_mock.side_effect = self._successful_composer
        lint_mock.return_value = ("", True)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "hyperframes-video-contract-test-video"
            ctx = self._context(root, scenes=scenes)
            ctx.settings.enable_video_composer = True

            result = export_video({}, ctx=ctx)
            manifest = json.loads(
                (project / "delivery_manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_category, "validation")
        self.assertTrue(result.payload["delivery_repairable"])
        self.assertEqual(result.payload["delivery_failure_kind"], "subtitle_readability_failed")
        self.assertIn("scene_01", result.error_message or "")
        self.assertIn("cps", (result.error_message or "").lower())
        self.assertGreater(
            result.payload["subtitle_diagnostics"][0]["max_cps"],
            24.0,
        )
        self.assertTrue(
            result.payload["subtitle_diagnostics"][0]["hard_exceeded"]
        )
        self.assertTrue(_delivery_failure_is_repairable(result))
        self.assertEqual(manifest["failure_reason"], "subtitle_readability_failed")
        lint_mock.assert_called_once()
        render_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
