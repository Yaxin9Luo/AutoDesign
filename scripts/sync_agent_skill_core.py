#!/usr/bin/env python3
"""Deterministically vendor the portable core and grounding contract."""

from __future__ import annotations

import argparse
import os
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
    drift: list[str] = []
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
        for relative, source in sources.items():
            if source.is_symlink():
                raise ValueError(f"canonical source must not be a symlink: {source}")
            if not source.is_file():
                raise FileNotFoundError(f"missing canonical source: {source}")
            target = package / relative
            if target.is_symlink():
                raise ValueError(f"vendored target must not be a symlink: {target}")
            if not target.is_file() or target.read_bytes() != source.read_bytes():
                drift.append(target.relative_to(root).as_posix())
                if not check:
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
