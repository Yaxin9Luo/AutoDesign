#!/usr/bin/env python3
"""Deterministically vendor the portable core and grounding contract."""

from __future__ import annotations

import argparse
import os
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
    shared = root / "_shared"
    sources = {
        Path("scripts/_portable.py"): shared / "portable_core.py",
        Path("references/source-grounding.md"): shared / "source-grounding.md",
    }
    drift: list[str] = []
    for skill_name in SKILL_NAMES:
        package = root / skill_name
        if not package.is_dir():
            raise FileNotFoundError(f"missing Skill package: {package}")
        for relative, source in sources.items():
            if not source.is_file():
                raise FileNotFoundError(f"missing canonical source: {source}")
            target = package / relative
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
    drift = sync(args.root.resolve(), check=args.check)
    if args.check and drift:
        for path in drift:
            print(f"DRIFT: {path}")
        return 1
    action = "checked" if args.check else "synced"
    print(f"{action} {len(SKILL_NAMES)} portable Skill packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
