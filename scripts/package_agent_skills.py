#!/usr/bin/env python3
"""Build and safely install deterministic AutoDesign Agent Skill archives."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from validate_agent_skills import (
    APPROVED_SKILLS,
    validate_agent_skills,
    validate_skill_package,
)


_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_FILE_MODE = 0o644
_MAX_ARCHIVE_ENTRIES = 10_000
_MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024


class PackageError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _package_files(package_root: Path) -> list[Path]:
    files: list[Path] = []
    for current, directories, names in os.walk(package_root, followlinks=False):
        current_path = Path(current)
        for directory in directories:
            path = current_path / directory
            if path.is_symlink():
                raise PackageError(f"symlink is not allowed: {path}")
        for name in names:
            path = current_path / name
            if path.is_symlink():
                raise PackageError(f"symlink is not allowed: {path}")
            if not path.is_file():
                raise PackageError(f"unsupported package entry: {path}")
            resolved = path.resolve()
            try:
                resolved.relative_to(package_root.resolve())
            except ValueError as error:
                raise PackageError(f"package entry escapes its root: {path}") from error
            files.append(path)
    return sorted(files, key=lambda path: path.relative_to(package_root).as_posix())


def _write_archive(package_root: Path, skill_name: str, destination: Path) -> list[str]:
    archive_names: list[str] = []
    with zipfile.ZipFile(
        destination, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for source in _package_files(package_root):
            relative = source.relative_to(package_root).as_posix()
            archive_name = f"{skill_name}/{relative}"
            info = zipfile.ZipInfo(archive_name, date_time=_ZIP_TIMESTAMP)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | _FILE_MODE) << 16
            archive.writestr(info, source.read_bytes(), compresslevel=9)
            archive_names.append(archive_name)
    return archive_names


def build_release(source_root: Path, output_dir: Path, version: str) -> Path:
    if not _VERSION.fullmatch(version):
        raise PackageError(f"invalid release version: {version}")
    source_root = source_root.resolve()
    errors = validate_agent_skills(source_root)
    if errors:
        raise PackageError("Skill validation failed:\n" + "\n".join(errors))

    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise PackageError(f"refusing to overwrite existing release directory: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)

    stage = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.building-", dir=output_dir.parent)
    )
    try:
        manifest_skills: dict[str, dict[str, object]] = {}
        for skill_name in APPROVED_SKILLS:
            archive_name = f"{skill_name}-{version}.zip"
            archive_path = stage / archive_name
            archived_files = _write_archive(
                source_root / skill_name, skill_name, archive_path
            )
            digest = _sha256(archive_path)
            (stage / f"{archive_name}.sha256").write_text(
                f"{digest}  {archive_name}\n", encoding="utf-8"
            )
            manifest_skills[skill_name] = {
                "archive": archive_name,
                "files": archived_files,
                "sha256": digest,
            }

        manifest = {
            "format_version": 1,
            "skills": manifest_skills,
            "version": version,
        }
        (stage / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        if output_dir.exists():
            raise PackageError(
                f"refusing to overwrite existing release directory: {output_dir}"
            )
        stage.rename(output_dir)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return output_dir


def _validate_archive(archive: zipfile.ZipFile) -> tuple[str, list[zipfile.ZipInfo]]:
    infos = archive.infolist()
    if not infos:
        raise PackageError("archive is empty")
    if len(infos) > _MAX_ARCHIVE_ENTRIES:
        raise PackageError("archive contains too many entries")
    if sum(info.file_size for info in infos) > _MAX_UNCOMPRESSED_BYTES:
        raise PackageError("archive expands beyond the installation size limit")
    seen: set[str] = set()
    roots: set[str] = set()
    for info in infos:
        name = info.filename
        if name in seen:
            raise PackageError(f"duplicate archive entry: {name}")
        seen.add(name)
        if "\\" in name:
            raise PackageError(f"unsafe archive path: {name}")
        path = PurePosixPath(name)
        if path.is_absolute() or not path.parts or any(
            part in {"", ".", ".."} for part in path.parts
        ):
            raise PackageError(f"unsafe archive path: {name}")
        roots.add(path.parts[0])
        mode = (info.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode):
            raise PackageError(f"archive symlink is not allowed: {name}")
        if info.flag_bits & 0x1:
            raise PackageError(f"encrypted archive entry is not supported: {name}")
        if info.is_dir():
            continue
        if stat.S_IFMT(mode) not in {0, stat.S_IFREG}:
            raise PackageError(f"unsupported archive entry: {name}")

    if len(roots) != 1:
        raise PackageError("archive must contain exactly one top-level Skill folder")
    skill_name = next(iter(roots))
    if skill_name not in APPROVED_SKILLS:
        raise PackageError(f"archive contains unapproved Skill: {skill_name}")
    required = {
        f"{skill_name}/SKILL.md",
        f"{skill_name}/LICENSE",
        f"{skill_name}/agents/openai.yaml",
    }
    missing = sorted(required - seen)
    if missing:
        raise PackageError(f"archive is missing required files: {', '.join(missing)}")
    return skill_name, infos


def install_archive(archive_path: Path, destination: Path) -> Path:
    archive_path = archive_path.resolve()
    if not archive_path.is_file():
        raise PackageError(f"archive does not exist: {archive_path}")
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(archive_path) as archive:
        skill_name, infos = _validate_archive(archive)
        installed = destination / skill_name
        if installed.exists() or installed.is_symlink():
            raise PackageError(f"installation destination already exists: {installed}")

        temporary = Path(
            tempfile.mkdtemp(prefix=f".{skill_name}.installing-", dir=destination)
        )
        try:
            for info in infos:
                relative = PurePosixPath(info.filename)
                target = temporary.joinpath(*relative.parts)
                try:
                    target.resolve().relative_to(temporary.resolve())
                except ValueError as error:
                    raise PackageError(f"unsafe archive path: {info.filename}") from error
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(_FILE_MODE)

            extracted = temporary / skill_name
            validation_errors = validate_skill_package(extracted, skill_name)
            if validation_errors:
                raise PackageError(
                    "extracted Skill validation failed:\n" + "\n".join(validation_errors)
                )
            if installed.exists() or installed.is_symlink():
                raise PackageError(f"installation destination already exists: {installed}")
            extracted.rename(installed)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
    return installed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    build = subcommands.add_parser("build", help="build deterministic release archives")
    build.add_argument("--source-root", type=Path, default=Path("agent_skills"))
    build.add_argument("--output-dir", type=Path, required=True)
    build.add_argument("--version", required=True)

    install = subcommands.add_parser("install", help="safely install one Skill archive")
    install.add_argument("--archive", type=Path, required=True)
    install.add_argument("--destination", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            output = build_release(args.source_root, args.output_dir, args.version)
            print(f"Built Agent Skill release: {output}")
        else:
            output = install_archive(args.archive, args.destination)
            print(f"Installed Agent Skill: {output}")
    except (OSError, PackageError, zipfile.BadZipFile) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
