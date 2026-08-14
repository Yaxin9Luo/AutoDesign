from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "eval" / "benchmark_manifest.jsonl"
DEFAULT_SMALL_SUBSET = REPO_ROOT / "eval" / "small_subset_ids.json"
DEFAULT_OUTPUT = REPO_ROOT / "eval" / "EvaData"

DISCIPLINES = {
    "ai_ml_existing_20",
    "biomed_health",
    "climate_earth_environment",
    "economics_policy",
    "physics_astronomy",
}
APPROVED_LICENSE_PREFIXES = (
    "https://creativecommons.org/licenses/",
    "https://creativecommons.org/publicdomain/",
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FORBIDDEN_METADATA_KEYS = {
    "abstract",
    "body_text",
    "content",
    "first_page_text_sample",
    "paper_pdf",
    "source_local_path",
    "summary",
    "text",
}


class ManifestError(ValueError):
    pass


def load_manifest(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ManifestError(f"invalid JSON on {path}:{line_number}: {exc}") from exc
        if not isinstance(record, dict):
            raise ManifestError(f"record on {path}:{line_number} must be an object")
        records.append(record)
    return records


def load_subset_ids(path: Path) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read small-subset manifest {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ManifestError("small-subset manifest must use schema_version 1")
    ids = payload.get("ids")
    if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
        raise ManifestError("small-subset manifest ids must be a list of strings")
    return ids


def _require_string(record: dict[str, Any], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{record.get('id', '<unknown>')}: {key} must be a non-empty string")
    return value


def validate_record(record: dict[str, Any]) -> None:
    record_id = _require_string(record, "id")
    discipline = _require_string(record, "discipline")
    case_id = _require_string(record, "case_id")
    if record.get("schema_version") != 1:
        raise ManifestError(f"{record_id}: schema_version must be 1")
    if discipline not in DISCIPLINES:
        raise ManifestError(f"{record_id}: unknown discipline {discipline}")
    if record_id != f"{discipline}/{case_id}":
        raise ManifestError(f"{record_id}: id must equal discipline/case_id")

    _require_string(record, "title")
    if not isinstance(record.get("authors"), list) or not all(
        isinstance(author, str) for author in record["authors"]
    ):
        raise ManifestError(f"{record_id}: authors must be a list of strings")
    if not isinstance(record.get("year"), int):
        raise ManifestError(f"{record_id}: year must be an integer")
    if not isinstance(record.get("identifiers"), dict):
        raise ManifestError(f"{record_id}: identifiers must be an object")
    if not SHA256_RE.fullmatch(str(record.get("expected_sha256", ""))):
        raise ManifestError(f"{record_id}: expected_sha256 must be 64 lowercase hex characters")
    if FORBIDDEN_METADATA_KEYS.intersection(record):
        raise ManifestError(f"{record_id}: manifest contains non-metadata content")
    if "/Users/" in json.dumps(record):
        raise ManifestError(f"{record_id}: manifest contains a local filesystem path")

    landing_url = record.get("landing_url")
    if not isinstance(landing_url, str) or not landing_url.startswith("https://"):
        raise ManifestError(f"{record_id}: landing_url must be HTTPS")

    policy = record.get("download_policy")
    pdf_url = record.get("pdf_url")
    license_url = record.get("license_url")
    if policy == "open_access":
        if not isinstance(pdf_url, str) or not pdf_url.startswith("https://"):
            raise ManifestError(f"{record_id}: open_access pdf_url must be HTTPS")
        if not isinstance(license_url, str) or not license_url.startswith(
            APPROVED_LICENSE_PREFIXES
        ):
            raise ManifestError(
                f"{record_id}: license_url is not an approved Creative Commons "
                "or public-domain URL"
            )
    elif policy == "manual_access_required":
        if pdf_url is not None or license_url is not None:
            raise ManifestError(
                f"{record_id}: manual records cannot contain pdf_url or license_url"
            )
    else:
        raise ManifestError(f"{record_id}: unsupported download_policy {policy!r}")


def validate_manifest(records: list[dict[str, Any]], subset_ids: list[str]) -> None:
    for record in records:
        validate_record(record)
    ids = [record["id"] for record in records]
    if len(records) != 100 or len(set(ids)) != 100:
        raise ManifestError("full manifest must contain exactly 100 unique records")
    expected_counts = {discipline: 20 for discipline in DISCIPLINES}
    if Counter(record["discipline"] for record in records) != expected_counts:
        raise ManifestError("full manifest must contain exactly 20 records per discipline")
    if len(subset_ids) != 10 or len(set(subset_ids)) != 10:
        raise ManifestError("small subset must contain exactly 10 unique ids")
    unknown = set(subset_ids).difference(ids)
    if unknown:
        raise ManifestError(f"small subset contains unknown ids: {sorted(unknown)}")
    if Counter(item.split("/", 1)[0] for item in subset_ids) != {
        discipline: 2 for discipline in DISCIPLINES
    }:
        raise ManifestError("small subset must contain exactly two records per discipline")


def select_records(
    records: list[dict[str, Any]], split: str, subset_ids: list[str]
) -> list[dict[str, Any]]:
    if split == "full":
        return records
    if split != "small":
        raise ValueError(f"unsupported split: {split}")
    by_id = {record["id"]: record for record in records}
    return [by_id[record_id] for record_id in subset_ids]


def is_auto_downloadable(record: dict[str, Any]) -> bool:
    return (
        record.get("download_policy") == "open_access"
        and isinstance(record.get("pdf_url"), str)
        and record["pdf_url"].startswith("https://")
        and isinstance(record.get("license_url"), str)
        and record["license_url"].startswith(APPROVED_LICENSE_PREFIXES)
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_record(
    record: dict[str, Any],
    output_root: Path,
    *,
    overwrite: bool,
    timeout: float,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    record_id = record["id"]
    destination = output_root / record["discipline"] / record["case_id"] / "paper.pdf"
    result = {"id": record_id, "path": str(destination)}

    if not is_auto_downloadable(record):
        return {
            **result,
            "status": "skipped",
            "reason": "manual_access_required",
            "landing_url": record["landing_url"],
        }

    if destination.exists():
        actual_hash = _sha256(destination)
        if actual_hash == record["expected_sha256"]:
            return {**result, "status": "reused", "sha256": actual_hash}
        if not overwrite:
            return {
                **result,
                "status": "failed",
                "reason": (
                    "existing paper.pdf has a different sha256; "
                    "use --overwrite to replace it"
                ),
                "sha256": actual_hash,
            }

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".pdf.part")
    request = urllib.request.Request(
        record["pdf_url"],
        headers={"User-Agent": "AutoDesign-Benchmark-Downloader/1.0"},
    )
    try:
        digest = hashlib.sha256()
        first_chunk = True
        with opener(request, timeout=timeout) as response, temporary.open("wb") as target:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                if first_chunk and not chunk.startswith(b"%PDF-"):
                    raise ValueError("downloaded content is not a PDF")
                first_chunk = False
                digest.update(chunk)
                target.write(chunk)
        actual_hash = digest.hexdigest()
        if first_chunk:
            raise ValueError("downloaded content is empty")
        if actual_hash != record["expected_sha256"]:
            raise ValueError(
                f"downloaded sha256 {actual_hash} does not match expected sha256 "
                f"{record['expected_sha256']}"
            )
        temporary.replace(destination)
        return {**result, "status": "downloaded", "sha256": actual_hash}
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        return {**result, "status": "failed", "reason": str(exc)}


def _verify_local(records: Iterable[dict[str, Any]], output_root: Path) -> Counter[str]:
    counts: Counter[str] = Counter()
    for record in records:
        destination = output_root / record["discipline"] / record["case_id"] / "paper.pdf"
        if not destination.exists():
            counts["missing"] += 1
        elif _sha256(destination) == record["expected_sha256"]:
            counts["verified"] += 1
        else:
            counts["mismatched"] += 1
    return counts


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Download only explicitly licensed AutoPosterBench PDFs whose "
            "hashes match the benchmark corpus."
        )
    )
    parser.add_argument("--split", choices=("small", "full"), default="small")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--small-subset", type=Path, default=DEFAULT_SMALL_SUBSET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--delay", type=float, default=0.5, help="seconds between network attempts")
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="per-request timeout in seconds"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        records = load_manifest(args.manifest)
        subset_ids = load_subset_ids(args.small_subset)
        validate_manifest(records, subset_ids)
    except ManifestError as exc:
        print(json.dumps({"status": "invalid_manifest", "error": str(exc)}, ensure_ascii=False))
        return 2

    selected = select_records(records, args.split, subset_ids)
    if args.verify_only:
        local_counts = _verify_local(selected, args.output)
        report = {
            "status": "verified_manifest",
            "split": args.split,
            "selected": len(selected),
            "auto_downloadable": sum(is_auto_downloadable(record) for record in selected),
            "manual_access_required": sum(not is_auto_downloadable(record) for record in selected),
            "local": dict(sorted(local_counts.items())),
        }
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0

    results: list[dict[str, Any]] = []
    attempted_download = False
    for record in selected:
        if attempted_download and is_auto_downloadable(record) and args.delay > 0:
            time.sleep(args.delay)
        result = download_record(
            record,
            args.output,
            overwrite=args.overwrite,
            timeout=args.timeout,
        )
        results.append(result)
        if is_auto_downloadable(record) and result["status"] not in {"reused"}:
            attempted_download = True

    counts = Counter(result["status"] for result in results)
    report = {
        "status": "completed" if not counts["failed"] else "completed_with_failures",
        "split": args.split,
        "selected": len(selected),
        "counts": dict(sorted(counts.items())),
        "results": results,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    report_path = args.output / "benchmark_download_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {**report, "report_path": str(report_path)},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
