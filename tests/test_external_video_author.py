from __future__ import annotations

import importlib
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import shlex
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from autodesign.attempt_candidates import load_attempt_candidates
from autodesign.schema import ToolResultRecord
from autodesign.skills.registry import SkillRegistry
from autodesign.tools._contract import ToolContext


def _load_feature():
    try:
        author_module = importlib.import_module(
            "autodesign.agents.external_video_author"
        )
    except ModuleNotFoundError:
        author_module = None
    try:
        plan_module = importlib.import_module("autodesign.util.video_visual_plan")
    except ModuleNotFoundError:
        plan_module = None
    return author_module, plan_module


AUTHOR_MODULE, PLAN_MODULE = _load_feature()
REPO_ROOT = Path(__file__).resolve().parents[1]


def _asset(index: int, role: str) -> dict[str, object]:
    asset_id = f"ingest_fig_{index:02d}"
    return {
        "asset_id": asset_id,
        "kind": "image",
        "output_file": f"layers/img_{asset_id}.png",
        "output_width_px": 1200,
        "output_height_px": 700,
        "output_sha256": f"sha-{index:02d}",
        "source_page": index,
        "caption_short": f"{role.title()} evidence {index}",
        "caption_full": f"Source-backed {role} evidence for asset {index}.",
        "visual_role": role,
        "visual_score": 100 - index,
        "curation_flags": [],
        "extract_strategy": "captioned_group",
        "caption_confidence": 0.9,
        "caption_association_method": "captioned_group",
        "captioned_source_group": True,
    }


def _provenance() -> dict[str, object]:
    roles = [
        "method",
        "method",
        "method",
        "method",
        "evidence",
        "evidence",
        "evidence",
        "evidence",
        "evidence",
        "qualitative",
        "qualitative",
        "qualitative",
        "qualitative",
        "qualitative",
        "qualitative",
        "qualitative",
        "evidence",
        "method",
    ]
    return {
        "kind": "paper_visual_provenance",
        "version": 1,
        "assets": [_asset(index, role) for index, role in enumerate(roles, 1)],
        "generation_policy": {
            "used_ai_generated_imagery": False,
            "used_external_images": False,
            "all_raster_assets_derived_from_source_pdf": True,
        },
    }


def _spoken_transcript(index: int, *, word_count: int = 45) -> str:
    text = (
        f"Scene {index} explains how the paper frames its research question and "
        "connects the proposed method to source evidence. The narration guides "
        "conference viewers through assumptions, mechanisms, comparisons, measured "
        "outcomes, and limitations while keeping every claim grounded in original "
        "figures, documented experiments, and the authors careful interpretation for "
        "clear academic understanding."
    )
    words = text.split()
    while len(words) < word_count:
        words.append(f"detail-{index}-{len(words) + 1}")
    return " ".join(words[:word_count])


def _scene_manifest(*, word_count: int = 45) -> list[dict[str, object]]:
    scenes: list[dict[str, object]] = []
    for index in range(1, 13):
        scenes.append(
            {
                "scene_id": f"scene_{index:02d}",
                "title": f"Conference scene {index}",
                "duration_s": 30,
                "visual_ids": [f"ingest_fig_{index:02d}"],
                "narration_intent": _spoken_transcript(
                    index, word_count=word_count
                ),
                "subtitle_intent": "Match the English narration verbatim.",
            }
        )
    return scenes


def _write_validation_project(
    root: Path,
    scenes: list[dict[str, object]],
    *,
    element_kind: str = "img",
    wrong_paths: bool = False,
) -> tuple[Path, dict[str, Path]]:
    project = root / "project"
    assets = project / "assets" / "figures"
    sources = root / "catalog_sources"
    assets.mkdir(parents=True)
    sources.mkdir(parents=True)
    source_paths: dict[str, Path] = {}
    for scene in scenes:
        for asset_id in scene["visual_ids"]:
            source = sources / f"{asset_id}.png"
            source.write_bytes(f"source-payload:{asset_id}".encode("ascii"))
            source_paths[str(asset_id)] = source
            shutil.copy2(source, assets / f"{asset_id}.png")

    sections: list[str] = []
    ordered_ids = list(source_paths)
    start = 0.0
    for index, scene in enumerate(scenes, start=1):
        visual_html: list[str] = []
        for asset_id in scene["visual_ids"]:
            source_id = str(asset_id)
            path_id = source_id
            if wrong_paths:
                path_id = ordered_ids[(ordered_ids.index(source_id) + 1) % len(ordered_ids)]
            if element_kind == "hidden_div":
                visual_html.append(
                    f'<div data-source-id="{source_id}" style="display:none"></div>'
                )
            else:
                visual_html.append(
                    f'<img src="assets/figures/{path_id}.png" '
                    f'data-source-id="{source_id}">'
                )
        duration = float(scene["duration_s"])
        sections.append(
            f'<section id="{scene["scene_id"]}" class="clip" '
            f'data-start="{start:g}" data-duration="{duration:g}" data-track-index="{index}" '
            f'data-narration="{scene["narration_intent"]}">'
            + "".join(visual_html)
            + "</section>"
        )
        start += duration
    total_duration = sum(float(scene["duration_s"]) for scene in scenes)
    html = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Conference video</title></head><body>
<main data-composition-id="main" data-start="0" data-no-timeline data-duration="{duration:g}"
data-width="1920" data-height="1080">""".format(duration=total_duration) + "\n".join(sections) + """
<audio id="narration" class="clip" src="assets/narration.wav" data-start="0"
data-duration="{duration:g}" data-track-index="100" data-media-start="0"></audio>
</main></body></html>""".format(duration=total_duration)
    (project / "index.html").write_text(html, encoding="utf-8")
    return project, source_paths


def _fake_author_script(
    path: Path,
    *,
    repair_mode: bool = False,
    malformed_done: bool = False,
) -> None:
    scenes = _scene_manifest()
    source = f"""
from pathlib import Path
import json
import shutil

root = Path.cwd()
project = root / "project"
assets = project / "assets" / "figures"
assets.mkdir(parents=True, exist_ok=True)
scenes = {scenes!r}
repair_mode = {repair_mode!r}
malformed_done = {malformed_done!r}
if repair_mode and root.name == "attempt_02":
    assert (root / "repair_baseline" / "project" / "index.html").is_file()
    assert (root / "repair_baseline" / "video_author_manifest.json").is_file()
sections = []
for index, scene in enumerate(scenes, start=1):
    asset_id = scene["visual_ids"][0]
    source = root / "layers" / f"img_{{asset_id}}.png"
    target = assets / f"{{asset_id}}.png"
    shutil.copy2(source, target)
    start = (index - 1) * 30
    sections.append(
        f'<section id="{{scene["scene_id"]}}" class="clip" '
        f'data-start="{{start}}" data-duration="30" data-track-index="{{index}}" '
        f'data-narration="{{scene["narration_intent"]}}">'
        f'<img src="assets/figures/{{asset_id}}.png" data-source-id="{{asset_id}}">'
        '</section>'
    )
html = '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Conference video</title></head>
<body><main data-composition-id="main" data-start="0" data-no-timeline data-duration="360"
data-width="1920" data-height="1080">''' + "\\n".join(sections) + '''
<audio id="narration" class="clip" src="assets/narration.wav" data-start="0"
data-duration="360" data-track-index="100" data-media-start="0"></audio>
</main></body></html>'''
if repair_mode and root.name == "attempt_01":
    html = html.replace("assets/figures/ingest_fig_01.png", "https://cdn.example/bad.png")
(project / "index.html").write_text(html, encoding="utf-8")
(root / "video_author_manifest.json").write_text(json.dumps({{
    "version": 1,
    "language": "en",
    "target_duration_s": 360,
    "project_path": "project",
    "scenes": scenes,
}}, indent=2), encoding="utf-8")
done_payload = "[]" if malformed_done else json.dumps({{
    "status": "complete", "scene_count": len(scenes)
}})
(root / "designer_author_done.json").write_text(done_payload, encoding="utf-8")
"""
    path.write_text(textwrap.dedent(source), encoding="utf-8")


def _make_context(
    run_dir: Path,
    *,
    include_dossier: bool = True,
    repair_mode: bool = False,
    malformed_done: bool = False,
) -> tuple[ToolContext, SimpleNamespace, dict[str, object]]:
    layers_dir = run_dir / "layers"
    layers_dir.mkdir()
    provenance = _provenance()
    rendered_layers: dict[str, dict[str, object]] = {}
    for asset in provenance["assets"]:
        asset_path = run_dir / str(asset["output_file"])
        asset_path.write_bytes(
            f"source image:{asset['asset_id']}".encode("ascii")
        )
        rendered_layers[str(asset["asset_id"])] = {
            "kind": "image",
            "src_path": str(asset_path),
            "caption": asset["caption_full"],
            "designer_eligible": False,
            "visual_selection_tier": "rejected",
        }
    (run_dir / "paper_memory.json").write_text(
        json.dumps({"kind": "paper_memory", "chunks": []}),
        encoding="utf-8",
    )
    (run_dir / "paper_memory.md").write_text(
        "# Full paper memory\n", encoding="utf-8"
    )
    if include_dossier:
        (run_dir / "paper_memory_dossier.json").write_text(
            json.dumps({"kind": "paper_memory_dossier", "sections": []}),
            encoding="utf-8",
        )
        (run_dir / "paper_memory_dossier.md").write_text(
            "# Evidence dossier\n", encoding="utf-8"
        )
    (run_dir / "paper_visual_provenance.json").write_text(
        json.dumps(provenance), encoding="utf-8"
    )
    (run_dir / "paper_visual_storyboard.json").write_text(
        json.dumps({"selected_asset_ids": ["ingest_fig_01"]}),
        encoding="utf-8",
    )
    (run_dir / "narrative_context.md").write_text(
        "Method, results, and limitations narrative.\n", encoding="utf-8"
    )
    (run_dir / "transcript_context.txt").write_text(
        "Draft English conference transcript.\n", encoding="utf-8"
    )
    from autodesign.runner import _write_runtime_skill_snapshot

    registry = SkillRegistry.load(REPO_ROOT / "skills")
    bundle = registry.select(
        brief="Create an English conference paper video.",
        attachments=[],
        artifact_hint="video",
    )
    _write_runtime_skill_snapshot(
        run_dir,
        skill_bundle=bundle,
        skill_contexts=bundle.render_all(),
    )
    script = run_dir / "fake_video_author.py"
    _fake_author_script(
        script,
        repair_mode=repair_mode,
        malformed_done=malformed_done,
    )

    settings = SimpleNamespace(
        designer_author_cmd=shlex.join([sys.executable, str(script)]),
        designer_author_harness="custom",
        designer_author_timeout_s=20,
        designer_author_max_attempts=2 if repair_mode else 1,
        designer_author_model="fake-local-agent",
    )
    ctx = ToolContext(
        settings=settings,
        run_dir=run_dir,
        layers_dir=layers_dir,
        run_id="external-video-test",
    )
    state = {
        "paper_memory": {"kind": "paper_memory", "chunks": []},
        "paper_visual_provenance": provenance,
        "paper_visual_storyboard": {"selected_asset_ids": ["ingest_fig_01"]},
        "rendered_layers": rendered_layers,
        "narrative_context": {
            "arc": ["problem", "method", "results", "limitations"]
        },
        "transcript": "Draft state-backed English transcript.",
    }
    if include_dossier:
        state["paper_memory_dossier"] = {
            "kind": "paper_memory_dossier",
            "sections": [],
        }
    ctx.state.update(state)
    return ctx, settings, provenance


def _fake_finalize_tool(name: str, args: dict[str, object], ctx: ToolContext) -> ToolResultRecord:
    if name != "finalize":
        raise AssertionError(f"unexpected tool call: {name}")
    pointer = ctx.run_dir / "final" / "video_delivery.json"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text('{"manifest_path":"delivery_manifest.json"}\n', encoding="utf-8")
    ctx.state["finalized"] = True
    ctx.state["finalize_notes"] = str(args.get("notes") or "")
    return ToolResultRecord(status="ok", payload={})


@unittest.skipIf(PLAN_MODULE is None, "video author feature modules not implemented")
class VideoVisualPlanTest(unittest.TestCase):
    def test_plan_uses_full_provenance_with_coverage_and_no_repetition(self) -> None:
        catalog = PLAN_MODULE.build_video_visual_asset_catalog(_provenance())
        plan = PLAN_MODULE.build_video_visual_plan(
            _provenance(), scene_count=12, target_duration_s=360
        )
        adaptive_plan = PLAN_MODULE.build_video_visual_plan(_provenance())

        recommended = plan["recommended_assets"]
        recommended_ids = [item["asset_id"] for item in recommended]
        mapped_ids = [
            asset_id
            for scene in plan["scene_visual_map"]
            for asset_id in scene["visual_ids"]
        ]

        self.assertEqual(len(catalog["assets"]), 18)
        self.assertGreaterEqual(len(recommended), 8)
        self.assertLessEqual(len(recommended), 16)
        self.assertEqual(len(recommended_ids), len(set(recommended_ids)))
        self.assertEqual(sorted(mapped_ids), sorted(recommended_ids))
        self.assertEqual(len(mapped_ids), len(set(mapped_ids)))
        self.assertEqual(
            set(plan["coverage"]["roles_present"]),
            {"method", "results", "qualitative"},
        )
        self.assertEqual(plan["source_manifest"], "paper_visual_provenance.json")
        self.assertEqual(
            plan["narration_contract"]["intent_semantics"],
            "verbatim_spoken_transcript",
        )
        self.assertEqual(plan["narration_contract"]["minimum_spoken_wpm"], 90)
        self.assertEqual(
            plan["narration_contract"]["minimum_speech_coverage_ratio"], 0.72
        )
        self.assertEqual(plan["target_duration_s"], 360)
        self.assertNotIn("target_duration_s", adaptive_plan)
        self.assertEqual(
            adaptive_plan["target_duration_range_s"],
            {"minimum": 300, "maximum": 600},
        )
        self.assertIn("paper", adaptive_plan["duration_selection_policy"])

    def test_recomputes_stale_eligibility_and_preserves_hard_rejection(self) -> None:
        provenance = _provenance()
        clean = provenance["assets"][0]
        contaminated = provenance["assets"][1]
        clean["eligible"] = False
        clean["designer_eligible"] = False
        clean["visual_selection_tier"] = "rejected"
        contaminated["eligible"] = True
        contaminated["designer_eligible"] = True
        contaminated["visual_selection_tier"] = "eligible"
        rendered_layers = {
            str(clean["asset_id"]): {"designer_eligible": False},
            str(contaminated["asset_id"]): {
                "designer_eligible": True,
                "curation_flags": ["body_text_leak"],
            },
        }

        catalog = PLAN_MODULE.build_video_visual_asset_catalog(
            provenance,
            rendered_layers=rendered_layers,
        )
        plan = PLAN_MODULE.build_video_visual_plan(
            provenance,
            rendered_layers=rendered_layers,
        )
        records = {record["asset_id"]: record for record in catalog["assets"]}
        recommended_ids = {
            record["asset_id"] for record in plan["recommended_assets"]
        }

        self.assertTrue(records[str(clean["asset_id"])]["eligibility"]["eligible"])
        self.assertFalse(
            records[str(contaminated["asset_id"])]["eligibility"]["eligible"]
        )
        self.assertIn("body_text_leak", " ".join(
            records[str(contaminated["asset_id"])]["eligibility"]["reject_reasons"]
        ))
        self.assertIn(str(clean["asset_id"]), recommended_ids)
        self.assertNotIn(str(contaminated["asset_id"]), recommended_ids)

    def test_unmatched_reserve_is_not_required_visual_coverage(self) -> None:
        provenance = _provenance()
        for asset in provenance["assets"]:
            asset["caption_short"] = ""
            asset["caption_full"] = ""
            asset["caption_association_method"] = "unmatched"
            asset["captioned_source_group"] = False
            asset["extract_strategy"] = "raster"

        catalog = PLAN_MODULE.build_video_visual_asset_catalog(provenance)
        plan = PLAN_MODULE.build_video_visual_plan(provenance)

        self.assertEqual(catalog["eligible_asset_count"], 18)
        self.assertEqual(catalog["required_eligible_asset_count"], 0)
        self.assertEqual(plan["minimum_required_visual_count"], 0)
        self.assertEqual(plan["required_recommended_asset_count"], 0)
        self.assertLessEqual(plan["recommended_asset_count"], 2)
        self.assertFalse(any(plan["coverage"][role] for role in (
            "method", "results", "qualitative"
        )))

    def test_content_hash_dedupes_assets_and_keeps_most_reliable_candidate(self) -> None:
        provenance = _provenance()
        original = provenance["assets"][0]
        duplicate = dict(original)
        duplicate.update({
            "asset_id": "ingest_fig_99",
            "output_file": "layers/img_ingest_fig_99.png",
            "visual_score": 999,
            "caption_short": "",
            "caption_full": "",
            "caption_association_method": "unmatched",
            "captioned_source_group": False,
            "extract_strategy": "raster",
        })
        provenance["assets"].append(duplicate)

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            rendered_layers: dict[str, dict[str, object]] = {}
            for asset in provenance["assets"]:
                path = root / f"{asset['asset_id']}.png"
                payload = (
                    b"same-visual"
                    if asset["asset_id"] in {original["asset_id"], duplicate["asset_id"]}
                    else str(asset["asset_id"]).encode("ascii")
                )
                path.write_bytes(payload)
                rendered_layers[str(asset["asset_id"])] = {"src_path": str(path)}
            catalog = PLAN_MODULE.build_video_visual_asset_catalog(
                provenance, rendered_layers=rendered_layers
            )
            plan = PLAN_MODULE.build_video_visual_plan(
                provenance, rendered_layers=rendered_layers
            )
        records = {
            record["asset_id"]: record for record in catalog["assets"]
        }
        recommended = plan["recommended_assets"]
        mapped_fingerprints = [
            fingerprint
            for scene in plan["scene_visual_map"]
            for fingerprint in scene["visual_fingerprints"]
        ]

        self.assertEqual(catalog["source_asset_count"], 19)
        self.assertEqual(catalog["asset_count"], 18)
        self.assertEqual(catalog["unique_visual_count"], 18)
        self.assertIn(str(original["asset_id"]), records)
        self.assertNotIn(str(duplicate["asset_id"]), records)
        self.assertEqual(
            records[str(original["asset_id"])]["fingerprint"],
            "sha256:" + hashlib.sha256(b"same-visual").hexdigest(),
        )
        self.assertFalse(
            records[str(original["asset_id"])]["provenance_hash_verified"]
        )
        self.assertEqual(plan["eligible_asset_count"], 18)
        self.assertEqual(plan["unique_eligible_visual_count"], 18)
        self.assertEqual(
            len({item["fingerprint"] for item in recommended}),
            len(recommended),
        )
        self.assertEqual(
            sorted(mapped_fingerprints),
            sorted(item["fingerprint"] for item in recommended),
        )
        self.assertEqual(len(mapped_fingerprints), len(set(mapped_fingerprints)))
        self.assertEqual(
            plan["repetition_policy"]["placement_target_basis"],
            "content_fingerprint",
        )

    def test_content_hash_dedupe_uses_visual_score_after_reliability_tie(self) -> None:
        provenance = _provenance()
        original = provenance["assets"][0]
        duplicate = dict(original)
        duplicate.update({
            "asset_id": "ingest_fig_99",
            "output_file": "layers/img_ingest_fig_99.png",
            "visual_score": 999,
        })
        provenance["assets"].append(duplicate)

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            shared = root / "shared.png"
            shared.write_bytes(b"same-visual")
            rendered_layers = {
                str(original["asset_id"]): {"src_path": str(shared)},
                str(duplicate["asset_id"]): {"src_path": str(shared)},
            }
            catalog = PLAN_MODULE.build_video_visual_asset_catalog(
                provenance, rendered_layers=rendered_layers
            )
        records = {
            record["asset_id"]: record for record in catalog["assets"]
        }

        self.assertNotIn(str(original["asset_id"]), records)
        self.assertIn(str(duplicate["asset_id"]), records)

    def test_actual_bytes_override_incorrect_shared_provenance_hash(self) -> None:
        provenance = _provenance()
        first = provenance["assets"][0]
        second = provenance["assets"][1]
        second["output_sha256"] = first["output_sha256"]
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            first_path = root / "first.png"
            second_path = root / "second.png"
            first_path.write_bytes(b"first-visual")
            second_path.write_bytes(b"second-visual")
            catalog = PLAN_MODULE.build_video_visual_asset_catalog(
                provenance,
                rendered_layers={
                    str(first["asset_id"]): {"src_path": str(first_path)},
                    str(second["asset_id"]): {"src_path": str(second_path)},
                },
            )

        records = {record["asset_id"]: record for record in catalog["assets"]}
        self.assertIn(str(first["asset_id"]), records)
        self.assertIn(str(second["asset_id"]), records)
        self.assertNotEqual(
            records[str(first["asset_id"])]["fingerprint"],
            records[str(second["asset_id"])]["fingerprint"],
        )
        self.assertFalse(records[str(first["asset_id"])]["provenance_hash_verified"])
        self.assertFalse(records[str(second["asset_id"])]["provenance_hash_verified"])

    def test_actual_bytes_verify_matching_provenance_hash(self) -> None:
        provenance = _provenance()
        asset = provenance["assets"][0]
        payload = b"verified-source-visual"
        asset["output_sha256"] = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as raw_tmp:
            path = Path(raw_tmp) / "visual.png"
            path.write_bytes(payload)
            catalog = PLAN_MODULE.build_video_visual_asset_catalog(
                provenance,
                rendered_layers={
                    str(asset["asset_id"]): {"src_path": str(path)},
                },
            )

        record = next(
            item for item in catalog["assets"]
            if item["asset_id"] == asset["asset_id"]
        )
        self.assertTrue(record["provenance_hash_verified"])
        self.assertEqual(record["actual_sha256"], asset["output_sha256"])

    def test_relative_output_file_is_hashed_from_trusted_run_root(self) -> None:
        provenance = _provenance()
        asset = provenance["assets"][0]
        payload = b"trusted-relative-source"
        asset["output_sha256"] = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_root = Path(raw_tmp)
            source = run_root / str(asset["output_file"])
            source.parent.mkdir(parents=True)
            source.write_bytes(payload)
            catalog = PLAN_MODULE.build_video_visual_asset_catalog(
                provenance,
                trusted_run_root=run_root,
            )

        record = next(
            item for item in catalog["assets"]
            if item["asset_id"] == asset["asset_id"]
        )
        self.assertEqual(record["actual_sha256"], asset["output_sha256"])
        self.assertTrue(record["provenance_hash_verified"])

    def test_missing_payload_under_trusted_root_is_not_formally_eligible(self) -> None:
        provenance = _provenance()
        asset = provenance["assets"][0]
        with tempfile.TemporaryDirectory() as raw_tmp:
            catalog = PLAN_MODULE.build_video_visual_asset_catalog(
                provenance,
                trusted_run_root=Path(raw_tmp),
            )

        record = next(
            item for item in catalog["assets"]
            if item["asset_id"] == asset["asset_id"]
        )
        self.assertFalse(record["eligibility"]["eligible"])
        self.assertFalse(record["can_satisfy_required_coverage"])
        self.assertIn(
            "missing_trusted_source_payload",
            record["eligibility"]["reject_reasons"],
        )

    def test_unreadable_sources_do_not_dedupe_by_claimed_hash(self) -> None:
        provenance = _provenance()
        first = provenance["assets"][0]
        second = provenance["assets"][1]
        shared_claim = "a" * 64
        first["output_sha256"] = shared_claim
        second["output_sha256"] = shared_claim
        first["output_file"] = "layers/missing-first.png"
        second["output_file"] = "layers/missing-second.png"

        catalog = PLAN_MODULE.build_video_visual_asset_catalog(provenance)
        records = {record["asset_id"]: record for record in catalog["assets"]}

        self.assertIn(str(first["asset_id"]), records)
        self.assertIn(str(second["asset_id"]), records)
        self.assertNotEqual(
            records[str(first["asset_id"])]["fingerprint"],
            records[str(second["asset_id"])]["fingerprint"],
        )
        self.assertIsNone(records[str(first["asset_id"])]["actual_sha256"])
        self.assertIsNone(records[str(second["asset_id"])]["actual_sha256"])

    def test_fingerprint_falls_back_to_normalized_staged_path_then_asset_id(self) -> None:
        provenance = _provenance()
        first = provenance["assets"][0]
        second = provenance["assets"][1]
        third = provenance["assets"][2]
        fourth = provenance["assets"][3]
        for asset in (first, second, third, fourth):
            asset.pop("output_sha256", None)
        first["output_file"] = ".\\layers\\nested\\..\\shared.png"
        second["output_file"] = "layers/shared.png"
        third["output_file"] = ""
        fourth["output_file"] = ""

        catalog = PLAN_MODULE.build_video_visual_asset_catalog(provenance)
        records = {
            record["asset_id"]: record for record in catalog["assets"]
        }
        shared_records = [
            record for record in catalog["assets"]
            if record["fingerprint"] == "path:layers/shared.png"
        ]

        self.assertEqual(len(shared_records), 1)
        self.assertEqual(
            records[str(third["asset_id"])]["fingerprint"],
            f"asset:{third['asset_id']}",
        )
        self.assertEqual(
            records[str(fourth["asset_id"])]["fingerprint"],
            f"asset:{fourth['asset_id']}",
        )


@unittest.skipIf(AUTHOR_MODULE is None, "video author feature modules not implemented")
class ExternalVideoAuthorTest(unittest.TestCase):
    def test_pending_user_selection_preempts_automatic_video_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx, settings, _ = _make_context(run_dir)
            author = AUTHOR_MODULE.ExternalVideoAuthor(settings, "ignored")

            with patch.object(
                AUTHOR_MODULE,
                "promote_pending_selection",
                return_value="promoted",
            ), patch.object(
                AUTHOR_MODULE,
                "ranked_delivery_candidates",
            ) as ranked:
                promoted = author._try_deliver_best_available_candidate(
                    ctx,
                    eligible_asset_ids=set(),
                    eligible_asset_roles={},
                    eligible_asset_paths={},
                    eligible_asset_hashes={},
                    required_asset_ids=set(),
                    minimum_required_visual_count=0,
                    expected_target_duration_s=None,
                )

            self.assertTrue(promoted)
            ranked.assert_not_called()

    def test_exhaustion_does_not_deliver_video_with_a_hard_project_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx, settings, _ = _make_context(run_dir)
            author = AUTHOR_MODULE.ExternalVideoAuthor(settings, "ignored")

            with (
                patch.object(
                    AUTHOR_MODULE,
                    "validate_video_author_output",
                    return_value=["project/index.html is required"],
                ),
                patch.object(
                    AUTHOR_MODULE,
                    "deliver_authored_video_project",
                ) as delivery,
            ):
                author.run("Create an English conference video.", ctx)

            delivery.assert_not_called()
            self.assertEqual(
                ctx.state["designer_api_error"]["reason"],
                "video_author_attempts_exhausted",
            )
            candidate = load_attempt_candidates(run_dir)[0]
            self.assertEqual(candidate.safety_state, "blocked")

    def test_exhaustion_delivers_best_complete_video_with_quality_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx, settings, _ = _make_context(run_dir)
            author = AUTHOR_MODULE.ExternalVideoAuthor(settings, "ignored")
            quality_errors = [
                "video requires at least 10 unique formal eligible source visuals; found 8"
            ]

            with (
                patch.object(
                    AUTHOR_MODULE,
                    "validate_video_author_output",
                    return_value=quality_errors,
                ),
                patch.object(
                    AUTHOR_MODULE,
                    "deliver_authored_video_project",
                    return_value=ToolResultRecord(
                        status="ok", payload={"mp4_written": True}
                    ),
                ) as delivery,
                patch.object(
                    AUTHOR_MODULE,
                    "invoke_designer_tool",
                    side_effect=_fake_finalize_tool,
                ),
            ):
                author.run("Create an English conference video.", ctx)

            delivery.assert_called_once()
            self.assertNotIn("designer_api_error", ctx.state)
            self.assertTrue(ctx.state["finalized"])
            self.assertEqual(
                ctx.state["designer_author_direct_final"]["acceptance_path"],
                "best_available_artifact_fallback",
            )
            candidate = load_attempt_candidates(run_dir)[0]
            self.assertEqual(candidate.safety_state, "ready_with_warnings")
            self.assertEqual(
                [warning.message for warning in candidate.warnings],
                quality_errors,
            )

    def test_done_marker_must_be_a_json_object(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx, settings, _ = _make_context(run_dir, malformed_done=True)
            author = AUTHOR_MODULE.ExternalVideoAuthor(settings, "ignored")

            with patch.object(
                AUTHOR_MODULE,
                "deliver_authored_video_project",
            ) as delivery:
                author.run("Create an English conference video.", ctx)

            delivery.assert_not_called()
            errors = json.loads(
                (
                    run_dir
                    / "video_author"
                    / "attempt_01"
                    / "video_author_validation_errors.json"
                ).read_text(encoding="utf-8")
            )["errors"]
            self.assertIn(
                "designer_author_done.json must contain a JSON object",
                errors,
            )
            self.assertEqual(
                ctx.state["designer_api_error"]["reason"],
                "video_author_attempts_exhausted",
            )

    def test_delivery_installs_design_spec_through_the_persisting_tool(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx, _, _ = _make_context(run_dir)
            project_dir = run_dir / "authored-project"
            project_dir.mkdir()
            manifest = {
                "version": 1,
                "language": "en",
                "target_duration_s": 360,
                "project_path": "project",
                "scenes": _scene_manifest(),
            }
            expected = ToolResultRecord(status="ok", payload={"delivered": True})
            ctx.state["fake_export_result"] = expected

            def fake_export(args, *, ctx):
                return ctx.state["fake_export_result"]

            with (
                patch.object(
                    AUTHOR_MODULE,
                    "invoke_designer_tool",
                    return_value=ToolResultRecord(status="ok", payload={}),
                ) as tool,
                patch.object(AUTHOR_MODULE, "_export_video", fake_export),
            ):
                result = AUTHOR_MODULE.deliver_authored_video_project(
                    project_dir=project_dir,
                    manifest=manifest,
                    ctx=ctx,
                )

            self.assertIs(result, expected)
            tool.assert_called_once()
            tool_name, tool_args, tool_ctx = tool.call_args.args
            self.assertEqual(tool_name, "propose_design_spec")
            self.assertEqual(tool_args["artifact_type"], "video")
            self.assertIs(tool_ctx, ctx)

    def test_stages_full_ingest_and_hands_valid_project_to_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx, settings, provenance = _make_context(run_dir)
            ctx.state["artifact_type"] = "poster"
            ctx.state["poster_content_brief"] = {"kind": "paper_poster"}
            captured: dict[str, object] = {}

            def fake_delivery(*, project_dir, manifest, ctx):
                self.assertEqual(ctx.state["artifact_type"], "video")
                self.assertNotIn("designer_author_direct_final", ctx.state)
                # Lease-guarded paths must be consumed while delivery is active.
                captured["project_dir"] = Path(str(project_dir))
                captured["manifest"] = manifest
                captured["ctx"] = ctx
                return ToolResultRecord(
                    status="ok",
                    payload={"project_dir": str(project_dir), "mp4_written": True},
                )

            author = AUTHOR_MODULE.ExternalVideoAuthor(settings, "video system prompt")
            with (
                patch.object(
                    AUTHOR_MODULE,
                    "deliver_authored_video_project",
                    side_effect=fake_delivery,
                ) as delivery,
                patch.object(
                    AUTHOR_MODULE,
                    "invoke_designer_tool",
                    side_effect=_fake_finalize_tool,
                ) as finalize_tool,
                patch.object(AUTHOR_MODULE, "log") as log_event,
            ):
                result = author.run("Create an English conference video.", ctx)

            attempt_dir = run_dir / "video_author" / "attempt_01"
            catalog = json.loads(
                (attempt_dir / "video_visual_asset_catalog.json").read_text(
                    encoding="utf-8"
                )
            )
            plan = json.loads(
                (attempt_dir / "video_visual_plan.json").read_text(encoding="utf-8")
            )
            input_manifest = json.loads(
                (attempt_dir / "video_author_input_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            trusted_context = json.loads(
                (run_dir / "video_trusted_source_context.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertIsNone(result)
            delivery.assert_called_once()
            finalize_tool.assert_called_once()
            self.assertTrue((attempt_dir / "video_author_prompt.md").is_file())
            self.assertTrue((attempt_dir / "attempt_candidate.json").is_file())
            self.assertTrue(
                (attempt_dir / "candidate" / "project" / "index.html").is_file()
            )
            self.assertFalse((attempt_dir / "designer_author_prompt.md").exists())
            prompt = (attempt_dir / "video_author_prompt.md").read_text(encoding="utf-8")
            self.assertNotIn("video system prompt", prompt)
            self.assertEqual(
                captured["project_dir"].resolve(),
                (attempt_dir / "project").resolve(),
            )
            self.assertEqual(len(captured["manifest"]["scenes"]), 12)
            self.assertEqual(len(catalog["assets"]), 18)
            self.assertEqual(
                trusted_context["eligible_asset_ids"],
                sorted(asset["asset_id"] for asset in catalog["assets"]),
            )
            self.assertEqual(
                set(trusted_context["eligible_asset_hashes"]),
                set(trusted_context["eligible_asset_ids"]),
            )
            self.assertEqual(
                trusted_context["minimum_required_visual_count"],
                plan["minimum_required_visual_count"],
            )
            self.assertNotEqual(
                plan["recommended_assets"], [{"asset_id": "ingest_fig_01"}]
            )
            self.assertEqual(
                input_manifest["evidence_source"], "full_paper_ingest"
            )
            self.assertIn("paper_memory.json", input_manifest["staged_files"])
            self.assertIn(
                "paper_memory_dossier.json", input_manifest["staged_files"]
            )
            self.assertIn(
                "paper_visual_provenance.json", input_manifest["staged_files"]
            )
            self.assertIn("narrative_context.md", input_manifest["staged_files"])
            self.assertIn("transcript_context.txt", input_manifest["staged_files"])
            self.assertIn(
                "runtime_skills/index.md", input_manifest["staged_files"]
            )
            self.assertNotIn(
                "runtime_skills/snapshot.json", input_manifest["staged_files"]
            )
            self.assertIn("runtime_skills/index.md", input_manifest["staged_files"])
            self.assertTrue((attempt_dir / "layers" / "img_ingest_fig_18.png").is_file())
            self.assertTrue((attempt_dir / "narrative_context.json").is_file())
            self.assertTrue((attempt_dir / "transcript_context.json").is_file())
            self.assertEqual(
                (attempt_dir / "runtime_skills" / "index.md").stat().st_mode & 0o222,
                0,
            )
            self.assertIn("runtime_skills/index.md", prompt)
            self.assertIn("selected artifact skill", prompt)
            self.assertIn("verbatim spoken transcript", prompt)
            self.assertIn("45 spoken words", prompt)
            self.assertIn("canonical narration transcript", prompt)
            self.assertIn("1.25x", prompt)
            self.assertIn("0.72 measured", prompt)
            self.assertIn("300-600 seconds", prompt)
            self.assertIn("paper's complexity", prompt)
            self.assertNotIn("targeting 360 seconds", prompt)
            self.assertIn('project_path` MUST be exactly `"project"`', prompt)
            self.assertIn('class="clip"', prompt)
            self.assertIn("data-no-timeline", prompt)
            self.assertIn("requestAnimationFrame", prompt)
            self.assertEqual(
                input_manifest["output_contract"]["project_path"], "project"
            )
            self.assertEqual(
                input_manifest["output_contract"]["hyperframes_protocol"]
                ["scene"]["required_class"],
                "clip",
            )
            self.assertNotIn("SELECTED VIDEO SKILL CONTENT MUST NOT BE INLINED", prompt)
            self.assertEqual(author.token_totals, (0, 0))
            self.assertEqual(author.cache_totals, (0, 0))
            self.assertEqual(ctx.state["video_author"]["status"], "passed")
            self.assertEqual(ctx.state["artifact_type"], "video")
            self.assertTrue(ctx.state["finalized"])
            self.assertTrue((run_dir / "final" / "video_delivery.json").is_file())
            self.assertIn("formal delivery", ctx.state["finalize_notes"].lower())
            self.assertEqual(
                ctx.state["designer_author_direct_final"],
                {
                    "source": "external_video_author",
                    "artifact_type": "video",
                    "acceptance_path": "formal_video_delivery_pass",
                },
            )
            log_event.assert_any_call(
                "video_author.attempt_start",
                mode="external",
                attempt=1,
                max_attempts=settings.designer_author_max_attempts,
            )

    def test_paper_memory_dossier_is_optional(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx, settings, _ = _make_context(run_dir, include_dossier=False)
            author = AUTHOR_MODULE.ExternalVideoAuthor(settings, "must not be included")

            with (
                patch.object(
                    AUTHOR_MODULE,
                    "deliver_authored_video_project",
                    return_value=ToolResultRecord(
                        status="ok", payload={"mp4_written": True}
                    ),
                ) as delivery,
                patch.object(
                    AUTHOR_MODULE,
                    "invoke_designer_tool",
                    side_effect=_fake_finalize_tool,
                ),
            ):
                author.run("Create an English conference video.", ctx)

            attempt_dir = run_dir / "video_author" / "attempt_01"
            input_manifest = json.loads(
                (attempt_dir / "video_author_input_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            delivery.assert_called_once()
            self.assertNotIn("paper_memory_dossier.json", input_manifest["staged_files"])
            self.assertNotIn("designer_api_error", ctx.state)

    def test_degraded_dossier_resume_continues_with_attempt_3(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx, settings, _ = _make_context(run_dir, include_dossier=False)
            previous = run_dir / "video_author" / "attempt_02"
            previous_project = previous / "project"
            previous_project.mkdir(parents=True)
            (previous_project / "index.html").write_text(
                "<html></html>",
                encoding="utf-8",
            )
            (previous / "video_author_manifest.json").write_text(
                json.dumps({
                    "version": 1,
                    "language": "en",
                    "project_path": "project",
                    "target_duration_s": 360,
                    "scenes": _scene_manifest(),
                }),
                encoding="utf-8",
            )
            delivery_error = {
                "error_message": "subtitle delivery failed",
                "error_category": "validation",
                "payload": {
                    "delivery_failure_kind": "subtitle_readability_failed",
                },
            }
            (previous / "video_author_delivery_errors.json").write_text(
                json.dumps(delivery_error),
                encoding="utf-8",
            )
            ctx.state["video_author_attempts"] = 2
            ctx.state["external_author_resume"] = {
                "previous_attempt_dir": str(previous),
                "repair_feedback": delivery_error,
            }
            settings.designer_author_max_attempts = 1
            author = AUTHOR_MODULE.ExternalVideoAuthor(settings, "ignored")

            with (
                patch.object(
                    AUTHOR_MODULE,
                    "deliver_authored_video_project",
                    return_value=ToolResultRecord(
                        status="ok", payload={"mp4_written": True}
                    ),
                ),
                patch.object(
                    AUTHOR_MODULE,
                    "invoke_designer_tool",
                    side_effect=_fake_finalize_tool,
                ),
            ):
                author.run("Create an English conference video.", ctx)

            attempt_dir = run_dir / "video_author" / "attempt_03"
            input_manifest = json.loads(
                (attempt_dir / "video_author_input_manifest.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertTrue(attempt_dir.is_dir())
            self.assertEqual(ctx.state["video_author_attempts"], 3)
            self.assertNotIn(
                "paper_memory_dossier.json",
                input_manifest["staged_files"],
            )
            self.assertNotIn("designer_api_error", ctx.state)

    def test_input_manifest_reports_actual_eligible_asset_count(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx, settings, provenance = _make_context(run_dir)
            rejected = _asset(19, "evidence")
            rejected["curation_flags"] = ["body_text_leak"]
            provenance["assets"].append(rejected)
            rejected_path = run_dir / str(rejected["output_file"])
            rejected_path.write_bytes(b"rejected source image")
            ctx.state["rendered_layers"][str(rejected["asset_id"])] = {
                "src_path": str(rejected_path),
                "curation_flags": ["body_text_leak"],
            }
            (run_dir / "paper_visual_provenance.json").write_text(
                json.dumps(provenance), encoding="utf-8"
            )
            author = AUTHOR_MODULE.ExternalVideoAuthor(settings, "ignored")

            with (
                patch.object(
                    AUTHOR_MODULE,
                    "deliver_authored_video_project",
                    return_value=ToolResultRecord(
                        status="ok", payload={"mp4_written": True}
                    ),
                ),
                patch.object(
                    AUTHOR_MODULE,
                    "invoke_designer_tool",
                    side_effect=_fake_finalize_tool,
                ),
            ):
                author.run("Create an English conference video.", ctx)

            attempt_dir = run_dir / "video_author" / "attempt_01"
            catalog = json.loads(
                (attempt_dir / "video_visual_asset_catalog.json").read_text(
                    encoding="utf-8"
                )
            )
            input_manifest = json.loads(
                (attempt_dir / "video_author_input_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(catalog["asset_count"], 19)
            self.assertEqual(catalog["eligible_asset_count"], 18)
            self.assertEqual(input_manifest["eligible_asset_count"], 18)

    def test_repair_attempt_stages_previous_project_and_manifest_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx, settings, _ = _make_context(run_dir, repair_mode=True)
            author = AUTHOR_MODULE.ExternalVideoAuthor(settings, "ignored internal prompt")

            with (
                patch.object(
                    AUTHOR_MODULE,
                    "deliver_authored_video_project",
                    return_value=ToolResultRecord(
                        status="ok", payload={"mp4_written": True}
                    ),
                ) as delivery,
                patch.object(
                    AUTHOR_MODULE,
                    "invoke_designer_tool",
                    side_effect=_fake_finalize_tool,
                ),
            ):
                author.run("Create an English conference video.", ctx)

            repair_dir = run_dir / "video_author" / "attempt_02"
            repair_prompt = (repair_dir / "video_author_prompt.md").read_text(
                encoding="utf-8"
            )
            delivery.assert_called_once()
            self.assertTrue(
                (repair_dir / "repair_baseline" / "project" / "index.html").is_file()
            )
            self.assertTrue(
                (
                    repair_dir
                    / "repair_baseline"
                    / "video_author_manifest.json"
                ).is_file()
            )
            self.assertIn("patch the staged baseline", repair_prompt.lower())
            self.assertIn(
                "repair_baseline/video_author_delivery_errors.json",
                repair_prompt,
            )
            self.assertNotIn("ignored internal prompt", repair_prompt)

    def test_runtime_skill_staging_is_hash_verified_and_stage_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx, _, _ = _make_context(run_dir)
            plan_dir = run_dir / "plan-attempt"
            repair_dir = run_dir / "repair-attempt"
            plan_dir.mkdir()
            repair_dir.mkdir()
            AUTHOR_MODULE._stage_runtime_skills(ctx, plan_dir, stage="plan")
            AUTHOR_MODULE._stage_runtime_skills(ctx, repair_dir, stage="repair")

            plan_skill = next(
                (plan_dir / "runtime_skills" / "packs").glob("video.conference_video/SKILL.md")
            ).read_text(encoding="utf-8")
            repair_skill = next(
                (repair_dir / "runtime_skills" / "packs").glob("video.conference_video/SKILL.md")
            ).read_text(encoding="utf-8")
            self.assertIn("Create ordered", plan_skill)
            self.assertNotIn("Repair every blocking", plan_skill)
            self.assertIn("Repair every blocking", repair_skill)
            self.assertNotIn("Create ordered", repair_skill)
            self.assertFalse((plan_dir / "runtime_skills" / "snapshot.json").exists())

    def test_runtime_skill_snapshot_tampering_fails_closed_before_author_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx, settings, _ = _make_context(run_dir)
            snapshot_skill = next(
                (run_dir / "runtime_skills" / "packs").glob("*/SKILL.md")
            )
            snapshot_skill.write_text("tampered\n", encoding="utf-8")
            author = AUTHOR_MODULE.ExternalVideoAuthor(settings, "ignored")
            with patch.object(AUTHOR_MODULE, "_invoke_author_command") as invoke:
                author.run("Create an English conference video.", ctx)

            invoke.assert_not_called()
            self.assertEqual(
                ctx.state["designer_api_error"]["reason"],
                "video_author_staging_failed",
            )
            self.assertIn(
                "hash mismatch",
                ctx.state["designer_api_error"]["message"],
            )

    def test_runtime_skill_snapshot_is_required_for_v2_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=run_dir,
                layers_dir=run_dir / "layers",
                run_id="missing-video-skills",
            )
            attempt_dir = run_dir / "attempt"
            attempt_dir.mkdir()

            with self.assertRaisesRegex(
                ValueError, "runtime skill snapshot is required"
            ):
                AUTHOR_MODULE._stage_runtime_skills(ctx, attempt_dir, stage="plan")

    def test_runtime_skill_snapshot_missing_allows_explicit_legacy_compat(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=run_dir,
                layers_dir=run_dir / "layers",
                run_id="legacy-video-skills",
            )
            ctx.state["legacy_runtime_skills_compat"] = True
            attempt_dir = run_dir / "attempt"
            attempt_dir.mkdir()

            staged = AUTHOR_MODULE._stage_runtime_skills(
                ctx, attempt_dir, stage="plan"
            )

            self.assertFalse(staged["catalog"]["available"])
            self.assertTrue(staged["catalog"]["legacy_compat"])

    def test_runtime_skill_snapshot_requires_an_active_stage_pack(self) -> None:
        from autodesign.runner import _write_runtime_skill_snapshot

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            pack_root = root / "skills" / "video" / "repair_only"
            pack_root.mkdir(parents=True)
            (pack_root / "SKILL.md").write_text(
                "# Repair-only video skill\n\n## Stage: repair\nRepair guidance.\n",
                encoding="utf-8",
            )
            (pack_root / "skill.json").write_text(
                json.dumps({
                    "manifest_version": 2,
                    "id": "video.repair_only",
                    "version": "1.0.0",
                    "description": "Repair-only video fixture.",
                    "applies_to": ["video"],
                    "stages": ["repair"],
                    "triggers": [],
                    "priority": 100,
                    "enabled_by_default": True,
                    "source": {"kind": "test"},
                    "assets": [],
                    "outputs": [],
                    "resources": [],
                }),
                encoding="utf-8",
            )
            registry = SkillRegistry.load(root / "skills")
            bundle = registry.select(
                brief="Create a video.", attachments=[], artifact_hint="video"
            )
            run_dir = root / "run"
            _write_runtime_skill_snapshot(
                run_dir,
                skill_bundle=bundle,
                skill_contexts=bundle.render_all(),
            )
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=run_dir,
                layers_dir=run_dir / "layers",
                run_id="no-plan-video-skills",
            )
            attempt_dir = root / "attempt"
            attempt_dir.mkdir()

            with self.assertRaisesRegex(ValueError, "active-stage skill pack"):
                AUTHOR_MODULE._stage_runtime_skills(ctx, attempt_dir, stage="plan")

    def test_runtime_skill_resources_do_not_cross_plan_and_repair_stages(self) -> None:
        from autodesign.runner import _write_runtime_skill_snapshot

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            skills_root = root / "skills"
            pack_root = skills_root / "video" / "isolated"
            references = pack_root / "references"
            references.mkdir(parents=True)
            (pack_root / "SKILL.md").write_text(
                "# Isolated Video\n\n## Stage: plan\nPlan only.\n\n"
                "## Stage: repair\nRepair only.\n",
                encoding="utf-8",
            )
            (references / "plan.txt").write_text("plan resource", encoding="utf-8")
            (references / "repair.txt").write_text("repair resource", encoding="utf-8")
            (pack_root / "skill.json").write_text(
                json.dumps({
                    "manifest_version": 2,
                    "id": "video.isolated",
                    "version": "1.0.0",
                    "description": "Stage isolation fixture for video author tests.",
                    "applies_to": ["video"],
                    "stages": ["plan", "repair"],
                    "triggers": [],
                    "priority": 100,
                    "enabled_by_default": True,
                    "source": {"kind": "test"},
                    "assets": [],
                    "outputs": [],
                    "resources": [
                        {
                            "id": "plan",
                            "path": "references/plan.txt",
                            "description": "Plan-only guidance.",
                            "stages": ["plan"],
                            "when_to_read": "Read during planning.",
                            "media_type": "text/plain",
                        },
                        {
                            "id": "repair",
                            "path": "references/repair.txt",
                            "description": "Repair-only guidance.",
                            "stages": ["repair"],
                            "when_to_read": "Read during repair.",
                            "media_type": "text/plain",
                        },
                    ],
                }),
                encoding="utf-8",
            )
            registry = SkillRegistry.load(skills_root)
            bundle = registry.select(
                brief="Create a video.", attachments=[], artifact_hint="video"
            )
            run_dir = root / "run"
            _write_runtime_skill_snapshot(
                run_dir,
                skill_bundle=bundle,
                skill_contexts=bundle.render_all(),
            )
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=run_dir,
                layers_dir=run_dir / "layers",
                run_id="stage-isolation",
            )
            plan_dir = root / "plan"
            repair_dir = root / "repair"
            plan_dir.mkdir()
            repair_dir.mkdir()
            AUTHOR_MODULE._stage_runtime_skills(ctx, plan_dir, stage="plan")
            AUTHOR_MODULE._stage_runtime_skills(ctx, repair_dir, stage="repair")

            self.assertTrue(
                (plan_dir / "runtime_skills/packs/video.isolated/references/plan.txt").is_file()
            )
            self.assertFalse(
                (plan_dir / "runtime_skills/packs/video.isolated/references/repair.txt").exists()
            )
            self.assertTrue(
                (repair_dir / "runtime_skills/packs/video.isolated/references/repair.txt").is_file()
            )
            self.assertFalse(
                (repair_dir / "runtime_skills/packs/video.isolated/references/plan.txt").exists()
            )

    def test_fresh_attachment_run_invokes_ingest_before_external_authoring(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            layers_dir = run_dir / "layers"
            layers_dir.mkdir()
            paper = run_dir / "paper.pdf"
            paper.write_bytes(b"synthetic pdf")
            script = run_dir / "fake_video_author.py"
            _fake_author_script(script)
            settings = SimpleNamespace(
                designer_author_cmd=shlex.join([sys.executable, str(script)]),
                designer_author_harness="custom",
                designer_author_timeout_s=20,
                designer_author_max_attempts=1,
                designer_author_model="fake-local-agent",
            )
            ctx = ToolContext(
                settings=settings,
                run_dir=run_dir,
                layers_dir=layers_dir,
                run_id="fresh-external-video",
            )
            ctx.state["attachments"] = [str(paper)]
            from autodesign.runner import _write_runtime_skill_snapshot

            registry = SkillRegistry.load(REPO_ROOT / "skills")
            bundle = registry.select(
                brief="Create an English conference paper video.",
                attachments=[paper],
                artifact_hint="video",
            )
            _write_runtime_skill_snapshot(
                run_dir,
                skill_bundle=bundle,
                skill_contexts=bundle.render_all(),
            )
            tool_calls: list[tuple[str, dict[str, object]]] = []

            def fake_tool(name, args, tool_ctx):
                tool_calls.append((name, args))
                if name == "switch_artifact_type":
                    self.assertEqual(args, {"type": "video"})
                    tool_ctx.state["artifact_type"] = "video"
                    return ToolResultRecord(status="ok", payload={"type": "video"})
                if name == "finalize":
                    return _fake_finalize_tool(name, args, tool_ctx)
                self.assertEqual(name, "ingest_document")
                self.assertEqual(args, {"file_paths": [str(paper)]})
                provenance = _provenance()
                tool_ctx.state["paper_memory"] = {
                    "kind": "paper_memory",
                    "chunks": [],
                }
                tool_ctx.state["paper_visual_provenance"] = provenance
                tool_ctx.state["rendered_layers"] = {}
                for asset in provenance["assets"]:
                    asset_path = run_dir / str(asset["output_file"])
                    asset_path.write_bytes(
                        f"source image:{asset['asset_id']}".encode("ascii")
                    )
                    tool_ctx.state["rendered_layers"][str(asset["asset_id"])] = {
                        "src_path": str(asset_path),
                        "caption": asset["caption_full"],
                    }
                return ToolResultRecord(status="ok", payload={"ingested": True})

            author = AUTHOR_MODULE.ExternalVideoAuthor(settings, "ignored")
            delivery_artifact_types: list[str | None] = []

            def fake_delivery(**kwargs):
                delivery_artifact_types.append(
                    kwargs["ctx"].state.get("artifact_type")
                )
                return ToolResultRecord(
                    status="ok", payload={"mp4_written": True}
                )

            with (
                patch.object(
                    AUTHOR_MODULE,
                    "invoke_designer_tool",
                    side_effect=fake_tool,
                ),
                patch.object(
                    AUTHOR_MODULE,
                    "deliver_authored_video_project",
                    side_effect=fake_delivery,
                ) as delivery,
            ):
                author.run("Create a conference video from the PDF.", ctx)

            self.assertEqual(
                [name for name, _ in tool_calls],
                ["switch_artifact_type", "ingest_document", "finalize"],
            )
            delivery.assert_called_once()
            self.assertEqual(delivery_artifact_types, ["video"])
            self.assertEqual(ctx.state["artifact_type"], "video")
            self.assertNotIn("designer_api_error", ctx.state)

    def test_visible_source_media_must_match_catalog_payload(self) -> None:
        scenes = _scene_manifest()
        manifest = {
            "version": 1,
            "language": "en",
            "target_duration_s": 360,
            "project_path": "project",
            "scenes": scenes,
        }
        roles = {
            f"ingest_fig_{index:02d}": role
            for index, role in enumerate(
                ["method"] * 4 + ["results"] * 4 + ["qualitative"] * 4,
                start=1,
            )
        }
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            project, source_paths = _write_validation_project(
                root, scenes, element_kind="hidden_div"
            )
            errors = AUTHOR_MODULE.validate_video_author_output(
                project_dir=project,
                manifest=manifest,
                eligible_asset_ids=set(source_paths),
                eligible_asset_roles=roles,
                eligible_asset_paths=source_paths,
                minimum_required_visual_count=8,
            )
        self.assertTrue(any("visible local media" in error for error in errors))

    def test_scene_narration_rejects_static_transcript_below_spoken_word_floor(
        self,
    ) -> None:
        scenes = _scene_manifest()
        scenes[0]["narration_intent"] = "Short narration cannot cover this scene."
        manifest = {
            "version": 1,
            "language": "en",
            "target_duration_s": 360,
            "project_path": "project",
            "scenes": scenes,
        }
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            project, source_paths = _write_validation_project(root, scenes)
            errors = AUTHOR_MODULE.validate_video_author_output(
                project_dir=project,
                manifest=manifest,
                eligible_asset_ids=set(source_paths),
            )

        self.assertTrue(any(
            "scene 1 narration_intent" in error and "90 spoken WPM" in error
            for error in errors
        ))

    def test_total_narration_rejects_transcript_below_90_spoken_wpm(self) -> None:
        scenes = _scene_manifest(word_count=44)
        manifest = {
            "version": 1,
            "language": "en",
            "target_duration_s": 360,
            "project_path": "project",
            "scenes": scenes,
        }
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            project, source_paths = _write_validation_project(root, scenes)
            errors = AUTHOR_MODULE.validate_video_author_output(
                project_dir=project,
                manifest=manifest,
                eligible_asset_ids=set(source_paths),
            )

        self.assertFalse(any("scene 1 narration_intent" in error for error in errors))
        self.assertTrue(any(
            "total narration transcript" in error and "540" in error
            for error in errors
        ))

    def test_repeated_words_cannot_pad_narration_word_count(self) -> None:
        scenes = _scene_manifest()
        scenes[0]["narration_intent"] = "evidence " * 45
        manifest = {
            "version": 1,
            "language": "en",
            "target_duration_s": 360,
            "project_path": "project",
            "scenes": scenes,
        }
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            project, source_paths = _write_validation_project(root, scenes)
            errors = AUTHOR_MODULE.validate_video_author_output(
                project_dir=project,
                manifest=manifest,
                eligible_asset_ids=set(source_paths),
            )

        self.assertTrue(any("repeated filler" in error for error in errors))

    def test_complete_narration_transcript_passes_static_validation(self) -> None:
        scenes = _scene_manifest()
        manifest = {
            "version": 1,
            "language": "en",
            "target_duration_s": 360,
            "project_path": "project",
            "scenes": scenes,
        }
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            project, source_paths = _write_validation_project(root, scenes)
            errors = AUTHOR_MODULE.validate_video_author_output(
                project_dir=project,
                manifest=manifest,
                eligible_asset_ids=set(source_paths),
            )

        self.assertEqual(errors, [])

    def test_adaptive_duration_is_valid_but_repair_cannot_retime_selected_target(
        self,
    ) -> None:
        scenes = _scene_manifest(word_count=60)
        for scene in scenes:
            scene["duration_s"] = 40
        manifest = {
            "version": 1,
            "language": "en",
            "target_duration_s": 480,
            "project_path": "project",
            "scenes": scenes,
        }
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            project, source_paths = _write_validation_project(root, scenes)
            fresh_errors = AUTHOR_MODULE.validate_video_author_output(
                project_dir=project,
                manifest=manifest,
                eligible_asset_ids=set(source_paths),
            )
            repair_errors = AUTHOR_MODULE.validate_video_author_output(
                project_dir=project,
                manifest=manifest,
                eligible_asset_ids=set(source_paths),
                expected_target_duration_s=360,
            )

        self.assertEqual(fresh_errors, [])
        self.assertTrue(any(
            "must preserve selected target_duration_s 360" in error
            for error in repair_errors
        ))

    def test_repair_duration_authority_fails_closed_when_missing_or_corrupt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            (root / "project").mkdir()
            missing_target, missing_error = (
                AUTHOR_MODULE._authoritative_target_from_previous_attempt(root)
            )
            (root / "video_author_manifest.json").write_text(
                '{"target_duration_s":"480"}\n',
                encoding="utf-8",
            )
            corrupt_target, corrupt_error = (
                AUTHOR_MODULE._authoritative_target_from_previous_attempt(root)
            )
            (root / "video_author_manifest.json").write_text(
                '{"target_duration_s":480}\n',
                encoding="utf-8",
            )
            valid_target, valid_error = (
                AUTHOR_MODULE._authoritative_target_from_previous_attempt(root)
            )

        self.assertIsNone(missing_target)
        self.assertIn("refusing to guess", missing_error or "")
        self.assertIsNone(corrupt_target)
        self.assertIn("must be an integer", corrupt_error or "")
        self.assertEqual(valid_target, 480)
        self.assertIsNone(valid_error)

    def test_adaptive_target_flows_unchanged_into_delivery_export(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx, _, _ = _make_context(run_dir)
            project_dir = run_dir / "authored-project"
            project_dir.mkdir()
            scenes = _scene_manifest(word_count=60)
            for scene in scenes:
                scene["duration_s"] = 40
            manifest = {
                "version": 1,
                "language": "en",
                "target_duration_s": 480,
                "project_path": "project",
                "scenes": scenes,
            }
            captured: dict[str, object] = {}

            def fake_export(args, *, ctx):
                captured.update(args)
                return ToolResultRecord(status="ok", payload={"delivered": True})

            with (
                patch.object(
                    AUTHOR_MODULE,
                    "invoke_designer_tool",
                    return_value=ToolResultRecord(status="ok", payload={}),
                ),
                patch.object(AUTHOR_MODULE, "_export_video", fake_export),
            ):
                result = AUTHOR_MODULE.deliver_authored_video_project(
                    project_dir=project_dir,
                    manifest=manifest,
                    ctx=ctx,
                )

        self.assertEqual(result.status, "ok")
        self.assertEqual(captured["duration_s"], 480)
        self.assertEqual(captured["n_scenes"], 12)

    def test_source_media_with_wrong_catalog_payload_is_rejected(self) -> None:
        scenes = _scene_manifest()
        manifest = {
            "version": 1,
            "language": "en",
            "target_duration_s": 360,
            "project_path": "project",
            "scenes": scenes,
        }
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            project, source_paths = _write_validation_project(
                root, scenes, wrong_paths=True
            )
            errors = AUTHOR_MODULE.validate_video_author_output(
                project_dir=project,
                manifest=manifest,
                eligible_asset_ids=set(source_paths),
                eligible_asset_paths=source_paths,
                minimum_required_visual_count=8,
            )
        self.assertTrue(any("does not match catalog source" in error for error in errors))

    def test_tampering_staged_source_and_project_asset_cannot_bypass_hash(self) -> None:
        scenes = _scene_manifest()
        manifest = {
            "version": 1,
            "language": "en",
            "target_duration_s": 360,
            "project_path": "project",
            "scenes": scenes,
        }
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            project, source_paths = _write_validation_project(root, scenes)
            trusted_hashes = {
                asset_id: hashlib.sha256(path.read_bytes()).hexdigest()
                for asset_id, path in source_paths.items()
            }
            victim = "ingest_fig_01"
            tampered = b"agent-authored-replacement"
            source_paths[victim].write_bytes(tampered)
            (project / "assets" / "figures" / f"{victim}.png").write_bytes(tampered)
            errors = AUTHOR_MODULE.validate_video_author_output(
                project_dir=project,
                manifest=manifest,
                eligible_asset_ids=set(source_paths),
                eligible_asset_paths=source_paths,
                eligible_asset_hashes=trusted_hashes,
                minimum_required_visual_count=8,
            )

        self.assertTrue(any(
            victim in error and "trusted catalog hash" in error
            for error in errors
        ))

    def test_minimum_visual_count_counts_only_formal_eligible_assets(self) -> None:
        scenes = _scene_manifest()
        for scene in scenes[3:]:
            scene["visual_ids"] = []
        manifest = {
            "version": 1,
            "language": "en",
            "target_duration_s": 360,
            "project_path": "project",
            "scenes": scenes,
        }
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            project, source_paths = _write_validation_project(root, scenes)
            all_eligible = {f"ingest_fig_{index:02d}" for index in range(1, 13)}
            required_ids = {f"ingest_fig_{index:02d}" for index in range(1, 9)}
            errors = AUTHOR_MODULE.validate_video_author_output(
                project_dir=project,
                manifest=manifest,
                eligible_asset_ids=all_eligible,
                required_asset_ids=required_ids,
                eligible_asset_paths=source_paths,
                minimum_required_visual_count=8,
            )
        self.assertTrue(any("at least 8 unique formal" in error for error in errors))

    def test_repairable_delivery_lint_failure_uses_next_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx, settings, _ = _make_context(run_dir)
            settings.designer_author_max_attempts = 2
            author = AUTHOR_MODULE.ExternalVideoAuthor(settings, "ignored")
            delivery_error = ToolResultRecord(
                status="error",
                error_message="HyperFrames lint failed: invalid clip nesting",
                error_category="validation",
                payload={"tts_ok": True, "lint_ok": False, "lint_output": "invalid clip nesting"},
            )
            delivery_ok = ToolResultRecord(
                status="ok", payload={"mp4_written": True}
            )

            with (
                patch.object(
                    AUTHOR_MODULE,
                    "deliver_authored_video_project",
                    side_effect=[delivery_error, delivery_ok],
                ) as delivery,
                patch.object(
                    AUTHOR_MODULE,
                    "invoke_designer_tool",
                    side_effect=_fake_finalize_tool,
                ),
            ):
                author.run("Create an English conference video.", ctx)

            attempt_one = run_dir / "video_author" / "attempt_01"
            attempt_two = run_dir / "video_author" / "attempt_02"
            self.assertEqual(delivery.call_count, 2)
            self.assertTrue((attempt_one / "video_author_delivery_errors.json").is_file())
            self.assertTrue(
                (
                    attempt_two
                    / "repair_baseline"
                    / "video_author_delivery_errors.json"
                ).is_file()
            )
            repair_prompt = (attempt_two / "video_author_prompt.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("invalid clip nesting", repair_prompt)
            self.assertEqual(ctx.state["video_author"]["status"], "passed")

    def test_delivery_repair_ignores_advisory_split_warnings(self) -> None:
        lint_output = """◆  Linting project/index.html

  ⚠ composition_file_too_large: This HTML composition file has 322 lines.
    Fix: Split coherent scenes into compositions/.
  ⚠ timeline_track_too_dense: Track 0 has 12 timed elements.
    Fix: Move scene groups into sub-compositions.
  ✗ font_family_without_font_face: Font family used without @font-face declaration: source sans pro.
    Fix: Add a local @font-face declaration or use a renderer-supported system font.

◇  1 error(s), 2 warning(s)
"""
        result = ToolResultRecord(
            status="error",
            error_message="HyperFrames lint failed during authoring preflight",
            error_category="validation",
            payload={
                "lint_ok": False,
                "lint_output": lint_output,
            },
        )

        feedback = AUTHOR_MODULE._delivery_repair_feedback(result)

        self.assertEqual(len(feedback), 1)
        self.assertIn("font_family_without_font_face", feedback[0])
        self.assertIn("@font-face", feedback[0])
        self.assertNotIn("composition_file_too_large", feedback[0])
        self.assertNotIn("timeline_track_too_dense", feedback[0])
        self.assertNotIn("sub-compositions", feedback[0])

    def test_video_author_prompt_keeps_scenes_inline_and_fonts_local(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx, settings, _ = _make_context(run_dir)
            author = AUTHOR_MODULE.ExternalVideoAuthor(settings, "ignored")

            with (
                patch.object(
                    AUTHOR_MODULE,
                    "deliver_authored_video_project",
                    return_value=ToolResultRecord(
                        status="ok",
                        payload={"mp4_written": True},
                    ),
                ),
                patch.object(
                    AUTHOR_MODULE,
                    "invoke_designer_tool",
                    side_effect=_fake_finalize_tool,
                ),
            ):
                author.run("Create an English conference video.", ctx)

            prompt = (
                run_dir
                / "video_author"
                / "attempt_01"
                / "video_author_prompt.md"
            ).read_text(encoding="utf-8")

        self.assertIn("directly in\n  `project/index.html`", prompt)
        self.assertIn("Do not move scenes into `data-composition-src`", prompt)
        self.assertIn("warnings are advisory", prompt)
        self.assertIn("local `@font-face`", prompt)
        self.assertIn("another unstaged font", prompt)

    def test_infrastructure_delivery_failure_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx, settings, _ = _make_context(run_dir)
            settings.designer_author_max_attempts = 2
            author = AUTHOR_MODULE.ExternalVideoAuthor(settings, "ignored")
            with patch.object(
                AUTHOR_MODULE,
                "deliver_authored_video_project",
                return_value=ToolResultRecord(
                    status="error",
                    error_message="Kokoro narration synthesis failed: provider unavailable",
                    error_category="validation",
                    payload={"tts_ok": False, "tts_output": "provider unavailable"},
                ),
            ) as delivery:
                author.run("Create an English conference video.", ctx)

            self.assertEqual(delivery.call_count, 1)
            self.assertFalse((run_dir / "video_author" / "attempt_02").exists())
            self.assertEqual(
                ctx.state["designer_api_error"]["reason"],
                "video_author_delivery_failed",
            )

    def test_narration_duration_overflow_is_repairable(self) -> None:
        result = ToolResultRecord(
            status="error",
            error_message=(
                "Kokoro narration synthesis failed: scene_10: required speech "
                "speed 1.26 exceeds conservative limit 1.25"
            ),
            error_category="validation",
            payload={"tts_ok": False},
        )

        self.assertTrue(AUTHOR_MODULE._delivery_failure_is_repairable(result))

    def test_structured_narration_timing_failure_is_repairable(self) -> None:
        timing_error = (
            "narration_timing_unfit scene=scene_11 measured=30.677s "
            "available=29.750s max_speed=1.35 final_speed=1.35"
        )
        result = ToolResultRecord(
            status="error",
            error_message=timing_error,
            error_category="validation",
            payload={
                "tts_ok": False,
                "tts_output": timing_error,
                "delivery_failure_kind": "narration_timing_unfit",
                "delivery_repairable": True,
            },
        )

        self.assertTrue(AUTHOR_MODULE._delivery_failure_is_repairable(result))

    def test_narration_timing_failure_uses_remaining_author_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx, settings, _ = _make_context(run_dir)
            settings.designer_author_max_attempts = 2
            author = AUTHOR_MODULE.ExternalVideoAuthor(settings, "ignored")
            timing_error = (
                "narration_timing_unfit scene=scene_11 measured=30.677s "
                "available=29.750s max_speed=1.35 final_speed=1.35"
            )
            with (
                patch.object(
                    AUTHOR_MODULE,
                    "deliver_authored_video_project",
                    side_effect=[
                        ToolResultRecord(
                            status="error",
                            error_message=timing_error,
                            error_category="validation",
                            payload={
                                "tts_ok": False,
                                "tts_output": timing_error,
                                "delivery_failure_kind": "narration_timing_unfit",
                                "delivery_repairable": True,
                            },
                        ),
                        ToolResultRecord(status="ok", payload={"mp4_written": True}),
                    ],
                ) as delivery,
                patch.object(
                    AUTHOR_MODULE,
                    "invoke_designer_tool",
                    side_effect=_fake_finalize_tool,
                ),
            ):
                author.run("Create an English conference video.", ctx)

            self.assertEqual(delivery.call_count, 2)
            self.assertTrue((run_dir / "video_author" / "attempt_02").is_dir())
            self.assertEqual(ctx.state["video_author_attempts"], 2)
            self.assertEqual(ctx.state["video_author"]["status"], "passed")

    def test_authored_render_and_media_probe_failures_are_repairable(self) -> None:
        render_failure = ToolResultRecord(
            status="error",
            error_message="HyperFrames render failed delivery validation: invalid clip nesting",
            error_category="validation",
            payload={"tts_ok": True, "lint_ok": True, "render_ok": False},
        )
        probe_failure = ToolResultRecord(
            status="error",
            error_message="HyperFrames render failed delivery validation: media probe rejected output",
            error_category="validation",
            payload={
                "tts_ok": True,
                "lint_ok": True,
                "render_ok": False,
                "media_probe": None,
            },
        )

        self.assertTrue(AUTHOR_MODULE._delivery_failure_is_repairable(render_failure))
        self.assertTrue(AUTHOR_MODULE._delivery_failure_is_repairable(probe_failure))

    def test_authored_ffmpeg_render_failure_is_repairable_but_missing_binary_is_not(self) -> None:
        authored = ToolResultRecord(
            status="error",
            error_message="HyperFrames render failed delivery validation: ffmpeg rejected invalid clip timing",
            error_category="validation",
            payload={
                "tts_ok": True,
                "lint_ok": True,
                "render_ok": False,
                "render_output": "ffmpeg: invalid duration authored for scene_04",
            },
        )
        missing = ToolResultRecord(
            status="error",
            error_message="HyperFrames render failed: ffmpeg executable missing",
            error_category="validation",
            payload={
                "tts_ok": True,
                "lint_ok": True,
                "render_ok": False,
                "render_output": "failed to start ffmpeg: executable not found",
            },
        )

        self.assertTrue(AUTHOR_MODULE._delivery_failure_is_repairable(authored))
        self.assertFalse(AUTHOR_MODULE._delivery_failure_is_repairable(missing))

    def test_runtime_probe_and_permission_failures_do_not_reauthor(
        self,
    ) -> None:
        failures = [
            ToolResultRecord(
                status="error",
                error_message="HyperFrames render failed delivery validation",
                error_category="validation",
                payload={
                    "tts_ok": True,
                    "lint_ok": True,
                    "render_ok": False,
                    "render_output": (
                        "ffprobe not found; MP4 delivery cannot be validated"
                    ),
                },
            ),
            ToolResultRecord(
                status="error",
                error_message="HyperFrames lint failed",
                error_category="validation",
                payload={
                    "lint_ok": False,
                    "lint_output": "[Errno 13] Permission denied: 'hyperframes'",
                },
            ),
        ]

        self.assertEqual(
            [
                AUTHOR_MODULE._delivery_failure_is_repairable(result)
                for result in failures
            ],
            [False, False],
        )

    def test_render_timeout_remains_author_repairable(self) -> None:
        result = ToolResultRecord(
            status="error",
            error_message="HyperFrames render failed delivery validation",
            error_category="validation",
            payload={
                "tts_ok": True,
                "lint_ok": True,
                "render_ok": False,
                "render_output": "render timed out after 1800 s",
            },
        )

        self.assertTrue(AUTHOR_MODULE._delivery_failure_is_repairable(result))

    def test_resume_repair_stages_finalize_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx, settings, _ = _make_context(run_dir)
            previous = run_dir / "video_author" / "attempt_01"
            previous_project = previous / "project"
            previous_project.mkdir(parents=True)
            (previous_project / "index.html").write_text("<html></html>", encoding="utf-8")
            (previous / "video_author_manifest.json").write_text(
                json.dumps({
                    "version": 1,
                    "language": "en",
                    "project_path": "project",
                    "target_duration_s": 360,
                    "scenes": _scene_manifest(),
                }),
                encoding="utf-8",
            )
            finalize_error = {
                "error_message": "final media timing verification failed",
                "error_category": "validation",
                "payload": {"issue_id": "video_finalize_timing"},
            }
            (previous / "video_author_finalize_errors.json").write_text(
                json.dumps(finalize_error), encoding="utf-8"
            )
            ctx.state["video_author_attempts"] = 1
            ctx.state["external_author_resume"] = {
                "previous_attempt_dir": str(previous),
                "repair_feedback": finalize_error,
            }
            settings.designer_author_max_attempts = 1
            author = AUTHOR_MODULE.ExternalVideoAuthor(settings, "ignored")
            with (
                patch.object(
                    AUTHOR_MODULE,
                    "deliver_authored_video_project",
                    return_value=ToolResultRecord(status="ok", payload={"mp4_written": True}),
                ),
                patch.object(AUTHOR_MODULE, "invoke_designer_tool", side_effect=_fake_finalize_tool),
            ):
                author.run("Create an English conference video.", ctx)

            staged = (
                run_dir
                / "video_author"
                / "attempt_02"
                / "repair_baseline"
                / "video_author_finalize_errors.json"
            )
            self.assertTrue(staged.is_file())
            self.assertEqual(json.loads(staged.read_text(encoding="utf-8")), finalize_error)

    def test_process_log_omits_command_and_redacts_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            attempt_dir = Path(raw_tmp)
            harness_secret = "harness-secret-value"
            command_secret = "command-secret-value"
            command = shlex.join([
                sys.executable,
                "-c",
                "import os,sys; print(os.environ.get('ANTHROPIC_API_KEY','')); print(sys.argv[2], file=sys.stderr)",
                "--token",
                command_secret,
            ])
            settings = SimpleNamespace(
                designer_author_harness="claude",
                harness_api_key=harness_secret,
            )

            error = AUTHOR_MODULE._invoke_author_command(
                command,
                prompt="test",
                attempt_dir=attempt_dir,
                timeout_s=10,
                settings=settings,
            )
            log_payload = json.loads(
                (attempt_dir / "video_author_process_log.json").read_text(encoding="utf-8")
            )
            serialized = json.dumps(log_payload, sort_keys=True)

        self.assertEqual(error, "")
        self.assertNotIn("command", log_payload)
        self.assertRegex(str(log_payload["command_sha256"]), r"^[0-9a-f]{64}$")
        self.assertNotIn(harness_secret, serialized)
        self.assertNotIn(command_secret, serialized)
        self.assertIn("<redacted>", serialized)

    def test_authored_render_failure_uses_remaining_author_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx, settings, _ = _make_context(run_dir)
            settings.designer_author_max_attempts = 2
            author = AUTHOR_MODULE.ExternalVideoAuthor(settings, "ignored")
            with (
                patch.object(
                    AUTHOR_MODULE,
                    "deliver_authored_video_project",
                    side_effect=[
                        ToolResultRecord(
                            status="error",
                            error_message=(
                                "HyperFrames render failed delivery validation: "
                                "invalid clip nesting"
                            ),
                            error_category="validation",
                            payload={
                                "tts_ok": True,
                                "lint_ok": True,
                                "render_ok": False,
                            },
                        ),
                        ToolResultRecord(status="ok", payload={"mp4_written": True}),
                    ],
                ) as delivery,
                patch.object(
                    AUTHOR_MODULE,
                    "invoke_designer_tool",
                    side_effect=_fake_finalize_tool,
                ),
            ):
                author.run("Create an English conference video.", ctx)

            self.assertEqual(delivery.call_count, 2)
            self.assertEqual(ctx.state["video_author"]["status"], "passed")
            repair_error = json.loads(
                (
                    run_dir
                    / "video_author/attempt_02/repair_baseline/video_author_delivery_errors.json"
                ).read_text(encoding="utf-8")
            )
            self.assertIn("invalid clip nesting", repair_error["error_message"])

    def test_delivery_rejects_actual_speech_coverage_below_minimum(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx, _, _ = _make_context(run_dir)
            project_dir = run_dir / "authored-project"
            project_dir.mkdir()
            manifest = {
                "version": 1,
                "language": "en",
                "target_duration_s": 360,
                "project_path": "project",
                "scenes": _scene_manifest(),
            }

            def fake_export(args, *, ctx):
                delivery_project = ctx.run_dir / "hyperframes-test"
                narration_dir = delivery_project / "narration"
                narration_dir.mkdir(parents=True)
                speech_duration_s = 240.0
                coverage_duration_s = 360.0
                speech_coverage_ratio = speech_duration_s / coverage_duration_s
                timings = [
                    {
                        "scene_id": f"scene_{index:02d}",
                        "speech_duration_s": 20.0,
                    }
                    for index in range(1, 13)
                ]
                (narration_dir / "timing.json").write_text(
                    json.dumps(timings), encoding="utf-8"
                )
                (delivery_project / "delivery_manifest.json").write_text(
                    json.dumps({
                        "status": "passed",
                        "speech_duration_s": speech_duration_s,
                        "coverage_duration_s": coverage_duration_s,
                        "speech_coverage_ratio": speech_coverage_ratio,
                        "minimum_speech_coverage_ratio": 0.72,
                        "measured_speech_scene_count": 12,
                        "media_probe": {"duration_s": coverage_duration_s},
                    }),
                    encoding="utf-8",
                )
                return ToolResultRecord(
                    status="ok",
                    payload={
                        "project_dir": "hyperframes-test",
                        "narration_timing_path": "narration/timing.json",
                        "delivery_manifest_path": "delivery_manifest.json",
                        "tts_ok": True,
                        "speech_duration_s": speech_duration_s,
                        "coverage_duration_s": coverage_duration_s,
                        "speech_coverage_ratio": speech_coverage_ratio,
                        "minimum_speech_coverage_ratio": 0.72,
                        "measured_speech_scene_count": 12,
                        "media_probe": {"duration_s": coverage_duration_s},
                    },
                )

            with (
                patch.object(
                    AUTHOR_MODULE,
                    "invoke_designer_tool",
                    return_value=ToolResultRecord(status="ok", payload={}),
                ),
                patch.object(AUTHOR_MODULE, "_export_video", fake_export),
            ):
                result = AUTHOR_MODULE.deliver_authored_video_project(
                    project_dir=project_dir,
                    manifest=manifest,
                    ctx=ctx,
                )
            delivery_manifest = json.loads(
                (
                    run_dir
                    / "hyperframes-test"
                    / "delivery_manifest.json"
                ).read_text(encoding="utf-8")
            )

        self.assertEqual(result.status, "error")
        self.assertAlmostEqual(result.payload["speech_coverage_ratio"], 2 / 3)
        self.assertIn("speech coverage", str(result.error_message).lower())
        self.assertEqual(delivery_manifest["status"], "passed")
        self.assertAlmostEqual(delivery_manifest["speech_coverage_ratio"], 2 / 3)
        self.assertTrue(AUTHOR_MODULE._delivery_failure_is_repairable(result))

    def test_delivery_records_and_accepts_complete_actual_speech_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx, _, _ = _make_context(run_dir)
            project_dir = run_dir / "authored-project"
            project_dir.mkdir()
            manifest = {
                "version": 1,
                "language": "en",
                "target_duration_s": 360,
                "project_path": "project",
                "scenes": _scene_manifest(),
            }

            def fake_export(args, *, ctx):
                delivery_project = ctx.run_dir / "hyperframes-test"
                narration_dir = delivery_project / "narration"
                narration_dir.mkdir(parents=True)
                speech_duration_s = 264.0
                coverage_duration_s = 360.0
                speech_coverage_ratio = speech_duration_s / coverage_duration_s
                timings = [
                    {
                        "scene_id": f"scene_{index:02d}",
                        "speech_duration_s": 22.0,
                    }
                    for index in range(1, 13)
                ]
                (narration_dir / "timing.json").write_text(
                    json.dumps(timings), encoding="utf-8"
                )
                (delivery_project / "delivery_manifest.json").write_text(
                    json.dumps({
                        "status": "passed",
                        "speech_duration_s": speech_duration_s,
                        "coverage_duration_s": coverage_duration_s,
                        "speech_coverage_ratio": speech_coverage_ratio,
                        "minimum_speech_coverage_ratio": 0.72,
                        "measured_speech_scene_count": 12,
                        "media_probe": {"duration_s": coverage_duration_s},
                    }),
                    encoding="utf-8",
                )
                return ToolResultRecord(
                    status="ok",
                    payload={
                        "project_dir": "hyperframes-test",
                        "narration_timing_path": "narration/timing.json",
                        "delivery_manifest_path": "delivery_manifest.json",
                        "tts_ok": True,
                        "speech_duration_s": speech_duration_s,
                        "coverage_duration_s": coverage_duration_s,
                        "speech_coverage_ratio": speech_coverage_ratio,
                        "minimum_speech_coverage_ratio": 0.72,
                        "measured_speech_scene_count": 12,
                        "media_probe": {"duration_s": coverage_duration_s},
                    },
                )

            with (
                patch.object(
                    AUTHOR_MODULE,
                    "invoke_designer_tool",
                    return_value=ToolResultRecord(status="ok", payload={}),
                ),
                patch.object(AUTHOR_MODULE, "_export_video", fake_export),
            ):
                result = AUTHOR_MODULE.deliver_authored_video_project(
                    project_dir=project_dir,
                    manifest=manifest,
                    ctx=ctx,
                )
            delivery_manifest = json.loads(
                (
                    run_dir
                    / "hyperframes-test"
                    / "delivery_manifest.json"
                ).read_text(encoding="utf-8")
            )

        self.assertEqual(result.status, "ok")
        self.assertAlmostEqual(result.payload["speech_duration_s"], 264.0)
        self.assertAlmostEqual(result.payload["speech_coverage_ratio"], 11 / 15)
        self.assertEqual(result.payload["minimum_speech_coverage_ratio"], 0.72)
        self.assertEqual(delivery_manifest["status"], "passed")
        self.assertAlmostEqual(delivery_manifest["speech_coverage_ratio"], 11 / 15)

    def test_delivery_preserves_actual_mp4_duration_speech_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx, _, _ = _make_context(run_dir)
            project_dir = run_dir / "authored-project"
            project_dir.mkdir()
            manifest = {
                "version": 1,
                "language": "en",
                "target_duration_s": 360,
                "project_path": "project",
                "scenes": _scene_manifest(),
            }
            speech_duration_s = 260.0
            coverage_duration_s = 360.5
            speech_coverage_ratio = speech_duration_s / coverage_duration_s

            def fake_export(args, *, ctx):
                delivery_project = ctx.run_dir / "hyperframes-test"
                narration_dir = delivery_project / "narration"
                narration_dir.mkdir(parents=True)
                timings = [
                    {
                        "scene_id": f"scene_{index:02d}",
                        "speech_duration_s": speech_duration_s / 12,
                    }
                    for index in range(1, 13)
                ]
                (narration_dir / "timing.json").write_text(
                    json.dumps(timings), encoding="utf-8"
                )
                delivery_metrics = {
                    "status": "passed",
                    "speech_duration_s": speech_duration_s,
                    "coverage_duration_s": coverage_duration_s,
                    "speech_coverage_ratio": speech_coverage_ratio,
                    "minimum_speech_coverage_ratio": 0.72,
                    "measured_speech_scene_count": 12,
                    "media_probe": {"duration_s": coverage_duration_s},
                }
                (delivery_project / "delivery_manifest.json").write_text(
                    json.dumps(delivery_metrics), encoding="utf-8"
                )
                return ToolResultRecord(
                    status="ok",
                    payload={
                        "project_dir": "hyperframes-test",
                        "narration_timing_path": "narration/timing.json",
                        "delivery_manifest_path": "delivery_manifest.json",
                        "tts_ok": True,
                        **{
                            key: value
                            for key, value in delivery_metrics.items()
                            if key != "status"
                        },
                    },
                )

            with (
                patch.object(
                    AUTHOR_MODULE,
                    "invoke_designer_tool",
                    return_value=ToolResultRecord(status="ok", payload={}),
                ),
                patch.object(AUTHOR_MODULE, "_export_video", fake_export),
            ):
                result = AUTHOR_MODULE.deliver_authored_video_project(
                    project_dir=project_dir,
                    manifest=manifest,
                    ctx=ctx,
                )
            delivery_manifest = json.loads(
                (
                    run_dir
                    / "hyperframes-test"
                    / "delivery_manifest.json"
                ).read_text(encoding="utf-8")
            )

        self.assertEqual(result.status, "ok")
        self.assertAlmostEqual(result.payload["coverage_duration_s"], 360.5)
        self.assertAlmostEqual(
            result.payload["speech_coverage_ratio"], 260.0 / 360.5
        )
        self.assertAlmostEqual(delivery_manifest["coverage_duration_s"], 360.5)
        self.assertAlmostEqual(
            delivery_manifest["speech_coverage_ratio"], 260.0 / 360.5
        )

    def test_missing_lint_tooling_is_not_sent_to_designer_repair(self) -> None:
        result = ToolResultRecord(
            status="error",
            error_message="HyperFrames lint failed: tooling unavailable",
            error_category="validation",
            payload={
                "tts_ok": True,
                "lint_ok": False,
                "lint_output": "pinned HyperFrames CLI is missing; run npm install",
            },
        )
        self.assertFalse(AUTHOR_MODULE._delivery_failure_is_repairable(result))

    def test_generated_narration_missing_at_lint_is_not_sent_to_designer_repair(
        self,
    ) -> None:
        result = ToolResultRecord(
            status="error",
            error_message=(
                "HyperFrames lint failed: audio_src_not_found: "
                "assets/narration.wav does not exist"
            ),
            error_category="validation",
            payload={
                "tts_ok": None,
                "lint_ok": False,
                "lint_output": (
                    "audio_src_not_found: <audio> element references a file that "
                    "does not exist: assets/narration.wav"
                ),
            },
        )

        self.assertFalse(AUTHOR_MODULE._delivery_failure_is_repairable(result))

    def test_generated_narration_lint_failure_stops_author_attempt_loop(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx, settings, _ = _make_context(run_dir)
            settings.designer_author_max_attempts = 12
            author = AUTHOR_MODULE.ExternalVideoAuthor(settings, "ignored")
            with patch.object(
                AUTHOR_MODULE,
                "deliver_authored_video_project",
                return_value=ToolResultRecord(
                    status="error",
                    error_message=(
                        "HyperFrames lint failed: audio_src_not_found: "
                        "assets/narration.wav does not exist"
                    ),
                    error_category="validation",
                    payload={
                        "lint_ok": False,
                        "lint_output": (
                            "audio_src_not_found: assets/narration.wav does not exist"
                        ),
                    },
                ),
            ) as delivery:
                author.run("Create an English conference video.", ctx)

            self.assertEqual(delivery.call_count, 1)
            self.assertFalse((run_dir / "video_author" / "attempt_02").exists())
            self.assertEqual(
                ctx.state["designer_api_error"]["reason"],
                "video_author_delivery_failed",
            )

    def test_rejects_remote_dependency_before_delivery_handoff(self) -> None:
        manifest = {
            "version": 1,
            "language": "en",
            "target_duration_s": 360,
            "project_path": "project",
            "scenes": _scene_manifest(),
        }
        with tempfile.TemporaryDirectory() as raw_tmp:
            project = Path(raw_tmp) / "project"
            project.mkdir()
            (project / "index.html").write_text(
                '<!doctype html><script src="https://cdn.example/app.js"></script>',
                encoding="utf-8",
            )

            errors = AUTHOR_MODULE.validate_video_author_output(
                project_dir=project,
                manifest=manifest,
                eligible_asset_ids={f"ingest_fig_{index:02d}" for index in range(1, 19)},
            )

        self.assertTrue(any("external" in error or "network" in error for error in errors))


class FeatureModuleBootstrapTest(unittest.TestCase):
    def test_external_video_author_feature_modules_exist(self) -> None:
        self.assertIsNotNone(AUTHOR_MODULE)
        self.assertIsNotNone(PLAN_MODULE)


if __name__ == "__main__":
    unittest.main()
