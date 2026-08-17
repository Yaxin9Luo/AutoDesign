#!/usr/bin/env python3
"""Validate the standalone AutoDesign Agent Skill package boundary."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path


APPROVED_SKILLS = (
    "autodesign-poster",
    "autodesign-ppt",
    "autodesign-webpage",
    "autodesign-video",
)

_CACHE_OR_OUTPUT_PARTS = {
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "out",
    "output",
    "outputs",
    "runs",
    "sessions",
    "venv",
}
_CACHE_OR_OUTPUT_FILES = {".DS_Store"}
_SECRET_PATH_NAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    "auth.json",
    "credentials.json",
    "id_ed25519",
    "id_rsa",
}
_TEXT_SUFFIXES = {
    "",
    ".css",
    ".html",
    ".js",
    ".json",
    ".lock",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".ts",
    ".txt",
    ".yaml",
    ".yml",
}
_FORBIDDEN_IMPORT = re.compile(
    r"(?m)^\s*(?:from|import)\s+(?:autodesign|design_anything)(?:\.|\s|$)"
)
_FORBIDDEN_PRODUCT_REFERENCE = (
    "scripts.web_server",
    "python -m autodesign",
    "skills/runtime",
    "localhost:8000",
    "127.0.0.1:8000",
    "/api/health",
)
_SECRET_BYTES = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(
        rb"^\s*(?:OPENAI|ANTHROPIC|FRIDAY)_[A-Z0-9_]*(?:KEY|TOKEN)\s*=\s*\S+",
        flags=re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
)
_SECRET_SCAN_CHUNK_BYTES = 64 * 1024
_SECRET_SCAN_OVERLAP_BYTES = 4096


def _parse_frontmatter(path: Path) -> tuple[dict[str, str], list[str]]:
    errors: list[str] = []
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}, [f"{path}: missing YAML frontmatter"]
    marker = text.find("\n---\n", 4)
    if marker < 0:
        return {}, [f"{path}: unclosed YAML frontmatter"]

    values: dict[str, str] = {}
    for line_number, line in enumerate(text[4:marker].splitlines(), start=2):
        key, separator, value = line.partition(":")
        if not separator or not key.strip() or not value.strip():
            errors.append(f"{path}:{line_number}: invalid frontmatter field")
            continue
        normalized_key = key.strip()
        if normalized_key in values:
            errors.append(f"{path}:{line_number}: duplicate frontmatter field {normalized_key}")
            continue
        values[normalized_key] = value.strip().strip("\"'")
    return values, errors


def _validate_metadata(path: Path, skill_name: str) -> list[str]:
    if not path.is_file():
        return [f"{path}: missing agents/openai.yaml"]
    text = path.read_text(encoding="utf-8")
    errors: list[str] = []
    if not text.startswith("interface:\n"):
        errors.append(f"{path}: metadata must start with interface")

    fields: dict[str, str] = {}
    for key in ("display_name", "short_description", "default_prompt"):
        match = re.search(rf'(?m)^  {key}: "(?P<value>[^"\n]+)"$', text)
        if match is None:
            errors.append(f"{path}: missing quoted interface.{key}")
        else:
            fields[key] = match.group("value")
    short = fields.get("short_description", "")
    if short and not 25 <= len(short) <= 64:
        errors.append(f"{path}: short_description must contain 25-64 characters")
    prompt = fields.get("default_prompt", "")
    if prompt and f"${skill_name}" not in prompt:
        errors.append(f"{path}: default_prompt must mention ${skill_name}")

    for match in re.finditer(r'(?m)^\s+type:\s+"(?P<type>[^"\n]+)"$', text):
        if match.group("type") != "mcp":
            errors.append(f"{path}: only MCP tool dependencies are portable")
    return errors


def _iter_entries_without_following_symlinks(root: Path):
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in sorted(directories):
            yield current_path / name
        for name in sorted(files):
            yield current_path / name


def _contains_secret_bytes(path: Path) -> bool:
    overlap = b""
    with path.open("rb") as source:
        while chunk := source.read(_SECRET_SCAN_CHUNK_BYTES):
            window = overlap + chunk
            if any(pattern.search(window) for pattern in _SECRET_BYTES):
                return True
            overlap = window[-_SECRET_SCAN_OVERLAP_BYTES:]
    return False


def validate_skill_package(skill_root: Path, skill_name: str) -> list[str]:
    errors: list[str] = []
    if skill_name not in APPROVED_SKILLS:
        errors.append(f"{skill_root}: unapproved Skill name {skill_name}")
    if skill_root.is_symlink():
        return errors + [f"{skill_root}: Skill package root must not be a symlink"]
    if not skill_root.is_dir():
        return errors + [f"{skill_root}: missing Skill package"]

    skill_file = skill_root / "SKILL.md"
    if not skill_file.is_file():
        errors.append(f"{skill_file}: missing SKILL.md")
    else:
        frontmatter, frontmatter_errors = _parse_frontmatter(skill_file)
        errors.extend(frontmatter_errors)
        if set(frontmatter) != {"name", "description"}:
            errors.append(f"{skill_file}: frontmatter must contain only name and description")
        if frontmatter.get("name") != skill_name:
            errors.append(f"{skill_file}: frontmatter name must equal {skill_name}")
        if not frontmatter.get("description", "").strip():
            errors.append(f"{skill_file}: description must not be empty")
        text = skill_file.read_text(encoding="utf-8")
        if "user-selected output directory" not in text:
            errors.append(f"{skill_file}: missing external output-directory contract")
        if "Never write" not in text:
            errors.append(f"{skill_file}: missing read-only install contract")

    license_file = skill_root / "LICENSE"
    if not license_file.is_file() or not license_file.read_text(
        encoding="utf-8", errors="ignore"
    ).strip():
        errors.append(f"{license_file}: missing or empty legal file")
    errors.extend(_validate_metadata(skill_root / "agents" / "openai.yaml", skill_name))

    for entry in _iter_entries_without_following_symlinks(skill_root):
        relative = entry.relative_to(skill_root)
        if entry.is_symlink():
            errors.append(f"{relative}: symlink is not allowed")
            continue
        if entry.name == "skill.json":
            errors.append(f"{relative}: skill.json belongs to the product runtime, not Agent Skills")
        if entry.name in _SECRET_PATH_NAMES or entry.name.startswith(".env."):
            errors.append(f"{relative}: secret-like path is not allowed")
        if entry.name in _CACHE_OR_OUTPUT_FILES or any(
            part in _CACHE_OR_OUTPUT_PARTS for part in relative.parts
        ):
            errors.append(f"{relative}: cache or generated output is not allowed")
        if not entry.is_file():
            continue
        if _contains_secret_bytes(entry):
            errors.append(f"{relative}: secret-like content is not allowed")
        if entry.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        text = entry.read_text(encoding="utf-8", errors="ignore")
        if _FORBIDDEN_IMPORT.search(text) or any(
            marker in text.lower() for marker in _FORBIDDEN_PRODUCT_REFERENCE
        ):
            errors.append(f"{relative}: forbidden product dependency")
        if "../skills/" in text or "../autodesign/" in text or "../design_anything/" in text:
            errors.append(f"{relative}: forbidden package-external path")
    return errors


def validate_agent_skills(root: Path) -> list[str]:
    if not root.is_dir():
        return [f"{root}: Agent Skills root does not exist"]
    errors: list[str] = []
    package_names = sorted(
        entry.name
        for entry in root.iterdir()
        if entry.is_dir() and not entry.name.startswith("_")
    )
    if package_names != sorted(APPROVED_SKILLS):
        errors.append(
            f"{root}: expected exactly {', '.join(APPROVED_SKILLS)}; found {', '.join(package_names)}"
        )
    for skill_name in APPROVED_SKILLS:
        errors.extend(validate_skill_package(root / skill_name, skill_name))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("agent_skills"))
    args = parser.parse_args(argv)
    root = args.root.resolve()
    errors = validate_agent_skills(root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    for skill_name in APPROVED_SKILLS:
        print(f"OK: {skill_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
