from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path

from scripts import download_benchmark_papers as downloader


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "eval" / "benchmark_manifest.jsonl"
SMALL_SUBSET_PATH = REPO_ROOT / "eval" / "small_subset_ids.json"


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._consumed = False

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        if self._consumed:
            return b""
        self._consumed = True
        return self._payload


class DownloadBenchmarkPapersTests(unittest.TestCase):
    def test_public_manifests_define_exact_full_and_small_splits(self) -> None:
        records = downloader.load_manifest(MANIFEST_PATH)
        subset_ids = downloader.load_subset_ids(SMALL_SUBSET_PATH)
        downloader.validate_manifest(records, subset_ids)

        self.assertEqual(len(records), 100)
        self.assertEqual(len({record["id"] for record in records}), 100)
        self.assertEqual(Counter(record["discipline"] for record in records), {
            "ai_ml_existing_20": 20,
            "biomed_health": 20,
            "climate_earth_environment": 20,
            "economics_policy": 20,
            "physics_astronomy": 20,
        })
        self.assertEqual(len(subset_ids), 10)
        selected = downloader.select_records(records, "small", subset_ids)
        self.assertEqual(Counter(record["discipline"] for record in selected), {
            "ai_ml_existing_20": 2,
            "biomed_health": 2,
            "climate_earth_environment": 2,
            "economics_policy": 2,
            "physics_astronomy": 2,
        })

    def test_manifest_contains_metadata_only_and_denies_uncertain_downloads(self) -> None:
        records = downloader.load_manifest(MANIFEST_PATH)
        forbidden_keys = {
            "abstract",
            "content",
            "first_page_text_sample",
            "paper_pdf",
            "source_local_path",
            "summary",
            "text",
        }
        for record in records:
            self.assertFalse(forbidden_keys.intersection(record))
            self.assertNotIn("/Users/", json.dumps(record))
            self.assertRegex(record["expected_sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(record["title"].strip())
            self.assertIsInstance(record["authors"], list)
            if record["download_policy"] == "open_access":
                self.assertTrue(downloader.is_auto_downloadable(record))
            else:
                self.assertEqual(record["download_policy"], "manual_access_required")
                self.assertFalse(record.get("pdf_url"))
                self.assertFalse(downloader.is_auto_downloadable(record))

    def test_validation_rejects_open_access_without_approved_license(self) -> None:
        record = self._record(
            download_policy="open_access",
            pdf_url="https://example.org/paper.pdf",
            license_url="https://example.org/custom-license",
        )
        with self.assertRaisesRegex(downloader.ManifestError, "license_url"):
            downloader.validate_record(record)

    def test_full_and_small_selection_preserve_manifest_order(self) -> None:
        records = downloader.load_manifest(MANIFEST_PATH)
        subset_ids = downloader.load_subset_ids(SMALL_SUBSET_PATH)
        self.assertEqual(downloader.select_records(records, "full", subset_ids), records)
        selected = downloader.select_records(records, "small", subset_ids)
        self.assertEqual([record["id"] for record in selected], subset_ids)

    def test_download_record_writes_verified_pdf_atomically(self) -> None:
        payload = b"%PDF-1.7\nbenchmark fixture\n%%EOF\n"
        calls: list[object] = []

        def opener(request: object, *, timeout: float) -> _FakeResponse:
            calls.append((request, timeout))
            return _FakeResponse(payload)

        record = self._record(
            download_policy="open_access",
            pdf_url="https://example.org/paper.pdf",
            license_url="https://creativecommons.org/licenses/by/4.0/",
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = downloader.download_record(
                record,
                Path(tmp),
                overwrite=False,
                timeout=7.0,
                opener=opener,
            )
            output = Path(tmp) / "ai_ml_existing_20" / "paper-one" / "paper.pdf"
            self.assertEqual(result["status"], "downloaded")
            self.assertEqual(output.read_bytes(), payload)
            self.assertFalse(output.with_suffix(".pdf.part").exists())
            self.assertEqual(len(calls), 1)

    def test_download_record_reuses_matching_file_without_network(self) -> None:
        payload = b"%PDF-1.7\nexisting\n%%EOF\n"
        record = self._record(expected_sha256=hashlib.sha256(payload).hexdigest())

        def fail_opener(*_args: object, **_kwargs: object) -> _FakeResponse:
            raise AssertionError("network must not be used for a matching file")

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "ai_ml_existing_20" / "paper-one" / "paper.pdf"
            output.parent.mkdir(parents=True)
            output.write_bytes(payload)
            result = downloader.download_record(
                record,
                Path(tmp),
                overwrite=False,
                timeout=7.0,
                opener=fail_opener,
            )
            self.assertEqual(result["status"], "reused")

    def test_download_record_rejects_hash_mismatch_without_promoting_file(self) -> None:
        payload = b"%PDF-1.7\nwrong version\n%%EOF\n"
        record = self._record(expected_sha256="0" * 64)

        def opener(_request: object, *, timeout: float) -> _FakeResponse:
            return _FakeResponse(payload)

        with tempfile.TemporaryDirectory() as tmp:
            result = downloader.download_record(
                record,
                Path(tmp),
                overwrite=False,
                timeout=7.0,
                opener=opener,
            )
            output = Path(tmp) / "ai_ml_existing_20" / "paper-one" / "paper.pdf"
            self.assertEqual(result["status"], "failed")
            self.assertIn("sha256", result["reason"])
            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix(".pdf.part").exists())

    def test_verify_only_reports_without_network_or_output_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "papers"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = downloader.main([
                    "--manifest",
                    str(MANIFEST_PATH),
                    "--small-subset",
                    str(SMALL_SUBSET_PATH),
                    "--split",
                    "small",
                    "--output",
                    str(output),
                    "--verify-only",
                ])
            report = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(report["selected"], 10)
            self.assertFalse(output.exists())

    @staticmethod
    def _record(**overrides: object) -> dict[str, object]:
        record: dict[str, object] = {
            "schema_version": 1,
            "id": "ai_ml_existing_20/paper-one",
            "discipline": "ai_ml_existing_20",
            "case_id": "paper-one",
            "title": "Paper One",
            "authors": ["A. Author"],
            "year": 2024,
            "identifiers": {"doi": "10.0000/example", "arxiv": None, "openalex": None},
            "landing_url": "https://doi.org/10.0000/example",
            "pdf_url": "https://example.org/paper.pdf",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "download_policy": "open_access",
            "expected_sha256": hashlib.sha256(b"%PDF-1.7\nfixture\n").hexdigest(),
        }
        record.update(overrides)
        return record


if __name__ == "__main__":
    unittest.main()
