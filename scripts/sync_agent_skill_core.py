#!/usr/bin/env python3
"""Deterministically vendor the portable core and runtime contracts."""

from __future__ import annotations

import argparse
import os
import stat
import sys
import tempfile
from pathlib import Path


SKILL_NAMES = (
    "autodesign-poster",
    "autodesign-ppt",
    "autodesign-webpage",
    "autodesign-video",
)


def _atomic_copy(source: Path, target: Path) -> None:
    data = source.read_bytes()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.tmp-", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _canonical_source_bytes(source: Path) -> bytes:
    try:
        details = source.lstat()
    except FileNotFoundError as error:
        raise FileNotFoundError(f"missing canonical source: {source}") from error
    if stat.S_ISLNK(details.st_mode):
        raise ValueError(f"canonical source must not be a symlink: {source}")
    if not stat.S_ISREG(details.st_mode):
        raise ValueError(f"canonical source must be a regular file: {source}")
    if details.st_nlink != 1:
        raise ValueError(f"canonical source must not be a hardlink: {source}")
    return source.read_bytes()


def _target_bytes(target: Path) -> bytes | None:
    try:
        details = target.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(details.st_mode):
        raise ValueError(f"vendored target must not be a symlink: {target}")
    if not stat.S_ISREG(details.st_mode):
        raise ValueError(f"vendored target must be a regular file: {target}")
    if details.st_nlink != 1:
        raise ValueError(f"vendored target must not be a hardlink: {target}")
    return target.read_bytes()


def sync(root: Path, *, check: bool = False) -> list[str]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Agent Skills root must be a regular directory, not a symlink: {root}")
    shared = root / "_shared"
    if shared.is_symlink() or not shared.is_dir():
        raise ValueError(f"shared source directory must not be a symlink: {shared}")
    sources = {
        Path("scripts/_portable.py"): shared / "portable_core.py",
        Path("references/source-grounding.md"): shared / "source-grounding.md",
    }
    skill_specific_sources = {
        "autodesign-poster": {
            Path("scripts/portable_png.py"): shared / "portable_png.py",
        },
    }
    browser_sources = {
        Path("scripts/browser_worker.py"): shared / "browser_worker.py",
        Path("scripts/setup_browser.py"): shared / "setup_browser.py",
        Path("scripts/requirements-browser.lock"): shared / "requirements-browser.lock",
    }
    sources.update(browser_sources)
    source_bytes = {
        source: _canonical_source_bytes(source)
        for source in (*sources.values(), *(source for entries in skill_specific_sources.values() for source in entries.values()))
    }
    manifest: list[tuple[Path, Path, bytes | None]] = []
    for skill_name in SKILL_NAMES:
        package = root / skill_name
        if package.is_symlink():
            raise ValueError(f"Skill package must not be a symlink: {package}")
        if not package.is_dir():
            raise FileNotFoundError(f"missing Skill package: {package}")
        for directory_name in ("scripts", "references"):
            directory = package / directory_name
            if directory.is_symlink():
                raise ValueError(f"Skill runtime directory must not be a symlink: {directory}")
            if not directory.is_dir():
                raise FileNotFoundError(f"missing Skill runtime directory: {directory}")
        package_sources = dict(sources)
        package_sources.update(skill_specific_sources.get(skill_name, {}))
        for relative, source in package_sources.items():
            target = package / relative
            manifest.append((target, source, _target_bytes(target)))
    drift = [
        target.relative_to(root).as_posix()
        for target, source, target_bytes in manifest
        if target_bytes != source_bytes[source]
    ]
    if not check:
        for target, source, target_bytes in manifest:
            if target_bytes != source_bytes[source]:
                _atomic_copy(source, target)
    return drift


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "agent_skills",
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        drift = sync(args.root.absolute(), check=args.check)
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    if args.check and drift:
        for path in drift:
            print(f"DRIFT: {path}")
        return 1
    action = "checked" if args.check else "synced"
    print(f"{action} {len(SKILL_NAMES)} portable Skill packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
