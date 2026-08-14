from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest

from PIL import Image

from autodesign.evaluator.batch_style_homogeneity import (
    BATCH_STYLE_FINGERPRINT,
    LEGACY_BATCH_STYLE_MODULE_VERSION,
    LEGACY_BATCH_STYLE_RUBRIC_VERSION,
    build_batch_style_cache_key,
    build_legacy_batch_style_cache_key,
    compute_layout_signature,
    evaluate_batch_style_homogeneity,
    map_style_score_to_adjustment,
    parse_batch_style_judge_response,
)


def _write_png(path: Path, color: tuple[int, int, int]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (320, 180), color).save(path)
    return path


def _write_batch(root: Path, *, n: int = 20, prefix: str = "poster") -> list[Path]:
    artifacts: list[Path] = []
    for i in range(n):
        artifacts.append(
            _write_png(
                root / f"{prefix}_{i:02d}.png",
                ((40 + i * 7) % 255, (90 + i * 11) % 255, (130 + i * 13) % 255),
            )
        )
    return artifacts


class CapturingJudgeBackend:
    def __init__(self, text: str) -> None:
        self.text = text
        self.vision_calls: list[dict[str, object]] = []
        self.turn_calls: list[dict[str, object]] = []

    def vision_user_message(self, **kwargs: object) -> dict[str, object]:
        self.vision_calls.append(kwargs)
        return {"role": "user", "content": "vision"}

    def create_turn(self, **kwargs: object) -> SimpleNamespace:
        self.turn_calls.append(kwargs)
        return SimpleNamespace(text=self.text)


class BatchStyleHomogeneityTest(unittest.TestCase):
    def test_skips_batches_below_minimum_without_adjustment_or_judge_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = _write_batch(root / "sensitive-system-secret-method", n=19)
            judge = CapturingJudgeBackend('{"style_adaptability_score_0_10": 2}')

            result = evaluate_batch_style_homogeneity(
                artifacts,
                out_dir=root / "out",
                judge_model="judge-v1",
                judge_backend=judge,
            )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["adjustment_points"], 0.0)
        self.assertIn("minimum", result["explanation"].lower())
        self.assertEqual(judge.vision_calls, [])
        self.assertEqual(judge.turn_calls, [])

    def test_anonymous_contact_sheet_and_prompt_do_not_expose_method_or_paths(self) -> None:
        response = {
            "style_adaptability_score_0_10": 4.8,
            "rationale": "The batch repeats one rigid skeleton.",
            "evidence": ["same header and column grid", "visual needs are not adapted"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sensitive_root = root / "Sensitive System secret method folder"
            artifacts = _write_batch(sensitive_root / "autodesign-system-run")
            judge = CapturingJudgeBackend(json.dumps(response))

            result = evaluate_batch_style_homogeneity(
                artifacts,
                out_dir=root / "style_review",
                judge_model="judge-v1",
                judge_backend=judge,
            )

            prompt = str(judge.vision_calls[0]["text"])
            result_blob = json.dumps(result, ensure_ascii=False)
            contact_sheet_exists = Path(result["contact_sheet"]["path"]).exists()
            hash_prefixes = [item[:12] for item in result["artifact_hashes"]]

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["style_adaptability_score_0_10"], 4.8)
        self.assertEqual(result["adjustment_points"], -1.0)
        self.assertEqual(result["eval_protocol"], "posterbench-final")
        self.assertEqual(result["batch_style_fingerprint"], BATCH_STYLE_FINGERPRINT)
        self.assertNotIn("module_version", result)
        self.assertNotIn("rubric_version", result)
        self.assertIn("P001", prompt)
        self.assertTrue(contact_sheet_exists)
        for forbidden in (
            "Sensitive System",
            "secret method",
            "autodesign-system-run",
            "poster_00.png",
            str(sensitive_root),
        ):
            self.assertNotIn(forbidden, prompt)
            self.assertNotIn(forbidden, result_blob)
        for hash_prefix in hash_prefixes:
            self.assertNotIn(hash_prefix, prompt)

    def test_corrupt_image_degrades_without_aborting_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = _write_batch(root / "batch")
            artifacts[0].write_bytes(b"not-a-png")
            judge = CapturingJudgeBackend('{"style_adaptability_score_0_10": 6}')

            result = evaluate_batch_style_homogeneity(
                artifacts,
                out_dir=root / "out",
                judge_model="judge-v1",
                judge_backend=judge,
            )

        self.assertIn(result["status"], {"skipped", "degraded"})
        self.assertEqual(result["adjustment_points"], 0.0)
        self.assertEqual(judge.vision_calls, [])

    def test_hundred_poster_contact_sheet_uses_balanced_grid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = _write_batch(root / "batch", n=100)
            judge = CapturingJudgeBackend(json.dumps({
                "style_adaptability_score_0_10": 7.0,
                "rationale": "Varied layouts.",
            }))

            result = evaluate_batch_style_homogeneity(
                artifacts,
                out_dir=root / "out",
                judge_model="judge-v1",
                judge_backend=judge,
            )

        self.assertEqual(result["contact_sheet"]["columns"], 10)
        self.assertEqual(result["contact_sheet"]["rows"], 10)

    def test_layout_signature_suppresses_text_and_source_image_details(self) -> None:
        html_template = """<!doctype html>
<html>
  <body>
    <main style="position:relative;width:3072px;height:1536px;background:#ffffff">
      <header style="position:absolute;left:0;top:0;width:3072px;height:180px;background:#123456">
        {title}
      </header>
      <section style="position:absolute;left:80px;top:220px;width:900px;height:1120px;background:#f6f8fa">
        <h2>{left_heading}</h2>
        <img src="{left_src}" style="position:absolute;left:24px;top:120px;width:420px;height:260px">
      </section>
      <section style="position:absolute;left:1086px;top:220px;width:900px;height:1120px;background:#edf2f7">
        <p>{middle_text}</p>
      </section>
      <section style="position:absolute;left:2092px;top:220px;width:900px;height:1120px;background:#f8fafc">
        <img src="{right_src}" style="position:absolute;left:60px;top:160px;width:520px;height:320px">
      </section>
    </main>
  </body>
</html>"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "sensitive-system" / "poster.html"
            second = root / "different-method" / "poster.html"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.write_text(
                html_template.format(
                    title="Sensitive System secret title",
                    left_heading="Private method result",
                    left_src="../source-images/confidential-a.png",
                    middle_text="This text should not enter the signature.",
                    right_src="../source-images/confidential-b.png",
                ),
                encoding="utf-8",
            )
            second.write_text(
                html_template.format(
                    title="Different system title",
                    left_heading="Completely different words",
                    left_src="../other/figure-one.png",
                    middle_text="Another paper's prose and claims.",
                    right_src="../other/figure-two.png",
                ),
                encoding="utf-8",
            )

            first_signature = compute_layout_signature(first)
            second_signature = compute_layout_signature(second)

        self.assertEqual(first_signature, second_signature)
        for field in (
            "header",
            "columns",
            "section_bands",
            "panel_geometry",
            "occupancy",
            "palette_structure",
        ):
            self.assertIn(field, first_signature)
        signature_blob = json.dumps(first_signature, ensure_ascii=False)
        for forbidden in (
            "Sensitive System",
            "secret title",
            "Private method",
            "confidential-a",
            "figure-one",
        ):
            self.assertNotIn(forbidden, signature_blob)

    def test_parse_and_adjustment_mapping_use_requested_thresholds(self) -> None:
        self.assertEqual(map_style_score_to_adjustment(7.0), 0.0)
        self.assertEqual(map_style_score_to_adjustment(10.0), 0.0)
        self.assertEqual(map_style_score_to_adjustment(6.999), -0.5)
        self.assertEqual(map_style_score_to_adjustment(5.0), -0.5)
        self.assertEqual(map_style_score_to_adjustment(4.999), -1.0)
        self.assertEqual(map_style_score_to_adjustment(3.0), -1.0)
        self.assertEqual(map_style_score_to_adjustment(2.999), -1.5)
        self.assertEqual(map_style_score_to_adjustment(0.0), -1.5)

        parsed = parse_batch_style_judge_response(
            """```json
{"style_adaptability_score_0_10": 4.25, "rationale": "Mostly one skeleton."}
```"""
        )

        self.assertEqual(parsed["status"], "ok")
        self.assertEqual(parsed["style_adaptability_score_0_10"], 4.25)
        self.assertEqual(parsed["adjustment_points"], -1.0)

        failed = parse_batch_style_judge_response("not json")
        self.assertEqual(failed["status"], "parse_error")
        self.assertEqual(failed["adjustment_points"], 0.0)

    def test_cache_key_is_order_independent_and_invalidates_hash_model_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = _write_batch(root / "batch")
            key = build_batch_style_cache_key(
                artifacts,
                judge_model="judge-v1",
            )
            reversed_key = build_batch_style_cache_key(
                list(reversed(artifacts)),
                judge_model="judge-v1",
            )
            other_model_key = build_batch_style_cache_key(
                artifacts,
                judge_model="judge-v2",
            )
            other_fingerprint_key = build_batch_style_cache_key(
                artifacts,
                judge_model="judge-v1",
                batch_style_fingerprint="sha256:" + "0" * 64,
            )

            artifacts[0].write_bytes(b"changed-poster-content")
            changed_hash_key = build_batch_style_cache_key(
                artifacts,
                judge_model="judge-v1",
            )

        self.assertEqual(key, reversed_key)
        self.assertNotEqual(key, other_model_key)
        self.assertNotEqual(key, other_fingerprint_key)
        self.assertNotEqual(key, changed_hash_key)

    def test_valid_cache_is_used_and_invalid_cache_skips_without_adjustment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = _write_batch(root / "batch")
            cache_path = root / "batch_style_cache.json"
            key = build_batch_style_cache_key(
                artifacts,
                judge_model="judge-v1",
            )
            cache_path.write_text(
                json.dumps(
                    {
                        "cache_key": key,
                        "eval_protocol": "posterbench-final",
                        "batch_style_fingerprint": BATCH_STYLE_FINGERPRINT,
                        "judge_model": "judge-v1",
                        "status": "ok",
                        "style_adaptability_score_0_10": 6.2,
                        "adjustment_points": -0.5,
                        "explanation": "cached",
                    }
                ),
                encoding="utf-8",
            )

            cached = evaluate_batch_style_homogeneity(
                artifacts,
                out_dir=root / "out",
                judge_model="judge-v1",
                cache_path=cache_path,
            )

            cache_path.write_text(
                json.dumps(
                    {
                        "cache_key": key,
                        "eval_protocol": "posterbench-final",
                        "batch_style_fingerprint": "sha256:" + "0" * 64,
                        "judge_model": "judge-v1",
                        "status": "ok",
                        "style_adaptability_score_0_10": 2.0,
                        "adjustment_points": -1.5,
                    }
                ),
                encoding="utf-8",
            )
            invalid = evaluate_batch_style_homogeneity(
                artifacts,
                out_dir=root / "out",
                judge_model="judge-v1",
                cache_path=cache_path,
            )

        self.assertEqual(cached["status"], "ok")
        self.assertEqual(cached["cache_status"], "hit")
        self.assertEqual(cached["adjustment_points"], -0.5)
        self.assertEqual(invalid["status"], "skipped")
        self.assertEqual(invalid["cache_status"], "invalid")
        self.assertEqual(invalid["adjustment_points"], 0.0)

    def test_legacy_manual_version_cache_is_read_only_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = _write_batch(root / "batch")
            cache_path = root / "batch_style_cache.json"
            legacy_key = build_legacy_batch_style_cache_key(
                artifacts,
                judge_model="judge-v1",
            )
            cache_path.write_text(json.dumps({
                "cache_key": legacy_key,
                "module_version": LEGACY_BATCH_STYLE_MODULE_VERSION,
                "rubric_version": LEGACY_BATCH_STYLE_RUBRIC_VERSION,
                "judge_model": "judge-v1",
                "status": "ok",
                "style_adaptability_score_0_10": 6.0,
                "adjustment_points": -0.5,
            }), encoding="utf-8")

            result = evaluate_batch_style_homogeneity(
                artifacts,
                out_dir=root / "out",
                judge_model="judge-v1",
                cache_path=cache_path,
            )

        self.assertEqual(result["cache_status"], "legacy_hit")
        self.assertEqual(result["adjustment_points"], -0.5)
        self.assertEqual(result["batch_style_fingerprint"], BATCH_STYLE_FINGERPRINT)
        self.assertEqual(result["eval_protocol"], "posterbench-final")
        self.assertEqual(
            result["legacy_batch_style_rubric_version"],
            LEGACY_BATCH_STYLE_RUBRIC_VERSION,
        )
        self.assertNotIn("rubric_version", result)
        self.assertNotIn("module_version", result)

    def test_non_numeric_cached_adjustment_is_invalid_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = _write_batch(root / "batch")
            cache_path = root / "batch_style_cache.json"
            key = build_batch_style_cache_key(
                artifacts,
                judge_model="judge-v1",
            )
            cache_path.write_text(json.dumps({
                "cache_key": key,
                "eval_protocol": "posterbench-final",
                "batch_style_fingerprint": BATCH_STYLE_FINGERPRINT,
                "judge_model": "judge-v1",
                "status": "ok",
                "style_adaptability_score_0_10": 6.0,
                "adjustment_points": "bad",
            }), encoding="utf-8")

            result = evaluate_batch_style_homogeneity(
                artifacts,
                out_dir=root / "out",
                judge_model="judge-v1",
                cache_path=cache_path,
            )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["cache_status"], "invalid")
        self.assertEqual(result["adjustment_points"], 0.0)

    def test_judge_parse_failure_is_degraded_and_applies_no_adjustment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = _write_batch(root / "batch")
            judge = CapturingJudgeBackend("not json")

            result = evaluate_batch_style_homogeneity(
                artifacts,
                out_dir=root / "out",
                judge_model="judge-v1",
                judge_backend=judge,
            )

        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["judge_status"], "parse_error")
        self.assertEqual(result["adjustment_points"], 0.0)


if __name__ == "__main__":
    unittest.main()
