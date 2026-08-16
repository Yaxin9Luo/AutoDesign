from __future__ import annotations

import hashlib
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / "agent_skills"
PACKAGER = REPO_ROOT / "scripts" / "package_agent_skills.py"
VERSION = "0.1.0"
APPROVED_SKILLS = (
    "autodesign-poster",
    "autodesign-ppt",
    "autodesign-webpage",
    "autodesign-video",
)


def _build(output_dir: Path, source_root: Path = SKILLS_ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(PACKAGER),
            "build",
            "--source-root",
            str(source_root),
            "--output-dir",
            str(output_dir),
            "--version",
            VERSION,
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


class PortableAgentSkillPackagingTests(unittest.TestCase):
    def test_build_is_deterministic_and_emits_safe_versioned_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            first = temp / "first"
            second = temp / "second"
            first_result = _build(first)
            second_result = _build(second)
            self.assertEqual(first_result.returncode, 0, first_result.stdout + first_result.stderr)
            self.assertEqual(second_result.returncode, 0, second_result.stdout + second_result.stderr)

            expected_files = {"manifest.json"}
            for name in APPROVED_SKILLS:
                expected_files.add(f"{name}-{VERSION}.zip")
                expected_files.add(f"{name}-{VERSION}.zip.sha256")
            self.assertEqual({path.name for path in first.iterdir()}, expected_files)
            self.assertEqual({path.name for path in second.iterdir()}, expected_files)

            self.assertEqual(
                (first / "manifest.json").read_bytes(),
                (second / "manifest.json").read_bytes(),
            )
            manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["version"], VERSION)
            self.assertEqual(sorted(manifest["skills"]), sorted(APPROVED_SKILLS))

            for name in APPROVED_SKILLS:
                archive_name = f"{name}-{VERSION}.zip"
                first_archive = first / archive_name
                second_archive = second / archive_name
                self.assertEqual(first_archive.read_bytes(), second_archive.read_bytes())
                digest = hashlib.sha256(first_archive.read_bytes()).hexdigest()
                self.assertEqual(manifest["skills"][name]["sha256"], digest)
                self.assertEqual(
                    (first / f"{archive_name}.sha256").read_text(encoding="utf-8"),
                    f"{digest}  {archive_name}\n",
                )

                with zipfile.ZipFile(first_archive) as archive:
                    infos = archive.infolist()
                    self.assertTrue(infos)
                    roots = {info.filename.split("/", 1)[0] for info in infos}
                    self.assertEqual(roots, {name})
                    self.assertIn(f"{name}/SKILL.md", archive.namelist())
                    self.assertIn(f"{name}/LICENSE", archive.namelist())
                    self.assertIn(f"{name}/agents/openai.yaml", archive.namelist())
                    for info in infos:
                        self.assertFalse(info.is_dir())
                        self.assertNotIn("..", Path(info.filename).parts)
                        self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
                        self.assertEqual((info.external_attr >> 16) & 0o777, 0o644)

    def test_build_refuses_to_overwrite_an_existing_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "release"
            first = _build(output)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            before = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in output.iterdir()
            }
            second = _build(output)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("refusing to overwrite", second.stdout + second.stderr)
            after = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in output.iterdir()
            }
            self.assertEqual(before, after)

    def test_install_is_atomic_and_refuses_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            release = temp / "release"
            result = _build(release)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            archive = release / f"{APPROVED_SKILLS[0]}-{VERSION}.zip"
            checksum = release / f"{archive.name}.sha256"
            destination = temp / "skills"

            first = subprocess.run(
                [
                    sys.executable,
                    str(PACKAGER),
                    "install",
                    "--archive",
                    str(archive),
                    "--checksum",
                    str(checksum),
                    "--destination",
                    str(destination),
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            installed = destination / APPROVED_SKILLS[0]
            self.assertTrue((installed / "SKILL.md").is_file())

            before = hashlib.sha256((installed / "SKILL.md").read_bytes()).hexdigest()
            second = subprocess.run(
                [
                    sys.executable,
                    str(PACKAGER),
                    "install",
                    "--archive",
                    str(archive),
                    "--checksum",
                    str(checksum),
                    "--destination",
                    str(destination),
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("already exists", second.stdout + second.stderr)
            self.assertEqual(
                hashlib.sha256((installed / "SKILL.md").read_bytes()).hexdigest(), before
            )
            self.assertFalse(any(destination.glob(".*.installing-*")))

    def test_install_rejects_traversal_and_symlink_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            destination = temp / "skills"

            traversal = temp / "traversal.zip"
            with zipfile.ZipFile(traversal, "w") as archive:
                archive.writestr(f"{APPROVED_SKILLS[0]}/SKILL.md", "safe")
                archive.writestr(f"{APPROVED_SKILLS[0]}/../../escape", "unsafe")
            traversal_result = subprocess.run(
                [
                    sys.executable,
                    str(PACKAGER),
                    "install",
                    "--archive",
                    str(traversal),
                    "--allow-unverified",
                    "--destination",
                    str(destination),
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(traversal_result.returncode, 0)
            self.assertIn("unsafe archive path", traversal_result.stdout + traversal_result.stderr)
            self.assertFalse((temp / "escape").exists())

            linked = temp / "linked.zip"
            with zipfile.ZipFile(linked, "w") as archive:
                archive.writestr(f"{APPROVED_SKILLS[0]}/SKILL.md", "safe")
                info = zipfile.ZipInfo(f"{APPROVED_SKILLS[0]}/linked")
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, "SKILL.md")
            linked_result = subprocess.run(
                [
                    sys.executable,
                    str(PACKAGER),
                    "install",
                    "--archive",
                    str(linked),
                    "--allow-unverified",
                    "--destination",
                    str(destination),
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(linked_result.returncode, 0)
            self.assertIn("symlink", linked_result.stdout + linked_result.stderr)

    def test_install_revalidates_extracted_package_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            release = temp / "release"
            result = _build(release)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            archive = release / f"{APPROVED_SKILLS[0]}-{VERSION}.zip"
            with zipfile.ZipFile(archive, "a") as bundle:
                bundle.writestr(f"{APPROVED_SKILLS[0]}/.env", "TOKEN=not-a-real-secret\n")

            install = subprocess.run(
                [
                    sys.executable,
                    str(PACKAGER),
                    "install",
                    "--archive",
                    str(archive),
                    "--allow-unverified",
                    "--destination",
                    str(temp / "skills"),
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(install.returncode, 0)
            self.assertIn("secret-like path", install.stdout + install.stderr)
            self.assertFalse((temp / "skills" / APPROVED_SKILLS[0]).exists())

    def test_build_rejects_symlinks_in_source_packages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            source = temp / "agent_skills"
            shutil.copytree(SKILLS_ROOT, source)
            (source / APPROVED_SKILLS[0] / "linked-license").symlink_to(
                source / APPROVED_SKILLS[0] / "LICENSE"
            )
            result = _build(temp / "release", source)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("symlink", result.stdout + result.stderr)

    def test_install_fails_closed_without_release_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            release = temp / "release"
            result = _build(release)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            archive = release / f"{APPROVED_SKILLS[0]}-{VERSION}.zip"

            install = subprocess.run(
                [
                    sys.executable,
                    str(PACKAGER),
                    "install",
                    "--archive",
                    str(archive),
                    "--destination",
                    str(temp / "skills"),
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(install.returncode, 0)
            self.assertIn("checksum is required", install.stdout + install.stderr)
            self.assertFalse((temp / "skills" / APPROVED_SKILLS[0]).exists())

    def test_install_rejects_tampered_archive_against_stale_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            release = temp / "release"
            result = _build(release)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            archive = release / f"{APPROVED_SKILLS[0]}-{VERSION}.zip"
            checksum = release / f"{archive.name}.sha256"

            with zipfile.ZipFile(archive) as source:
                entries = [(info, source.read(info)) for info in source.infolist()]
            tampered = False
            with zipfile.ZipFile(archive, "w") as target:
                for info, content in entries:
                    if info.filename.endswith("/SKILL.md"):
                        updated = content.replace(
                            b"Create a conference poster",
                            b"Create a polished conference poster",
                            1,
                        )
                        tampered = tampered or updated != content
                        content = updated
                    target.writestr(info, content)
            self.assertTrue(tampered)

            install = subprocess.run(
                [
                    sys.executable,
                    str(PACKAGER),
                    "install",
                    "--archive",
                    str(archive),
                    "--checksum",
                    str(checksum),
                    "--destination",
                    str(temp / "skills"),
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(install.returncode, 0)
            self.assertIn("checksum mismatch", install.stdout + install.stderr)
            self.assertFalse((temp / "skills" / APPROVED_SKILLS[0]).exists())


if __name__ == "__main__":
    unittest.main()
