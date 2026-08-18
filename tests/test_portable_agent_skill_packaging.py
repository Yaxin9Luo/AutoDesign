from __future__ import annotations

import hashlib
import json
import os
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
VERSION = "0.2.0-rc1"
RELEASE_TOOLS = (
    "package_agent_skills.py",
    "validate_agent_skills.py",
)
APPROVED_SKILLS = (
    "autodesign-poster",
    "autodesign-ppt",
    "autodesign-webpage",
    "autodesign-video",
)
INSTALL_RECEIPT_RELATIVE = Path("scripts/install-receipt.json")
INSTALL_RECEIPT_SCHEMA = "autodesign-agent-skill-install-receipt-v1"


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


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def _make_tree_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def _make_tree_removable(root: Path) -> None:
    root.chmod(0o755)
    for path in root.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)


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

            expected_files = {"manifest.json", *RELEASE_TOOLS}
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
            self.assertEqual(sorted(manifest["release_tools"]), sorted(RELEASE_TOOLS))

            for name in RELEASE_TOOLS:
                first_tool = first / name
                second_tool = second / name
                self.assertEqual(first_tool.read_bytes(), second_tool.read_bytes())
                self.assertEqual(
                    manifest["release_tools"][name],
                    hashlib.sha256(first_tool.read_bytes()).hexdigest(),
                )

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

    def test_release_local_installer_runs_outside_repo_without_mutating_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            release = temp / "downloaded-release"
            result = _build(release)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            archive = release / f"{APPROVED_SKILLS[0]}-{VERSION}.zip"
            checksum = release / f"{archive.name}.sha256"
            installer = release / "package_agent_skills.py"
            self.assertTrue(installer.is_file(), "release must bundle its installer")
            destination = temp / "host-home" / "skills"
            outside = temp / "unrelated-working-directory"
            outside.mkdir()
            before = {
                path.relative_to(release).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in release.rglob("*")
                if path.is_file()
            }
            environment = os.environ.copy()
            environment.pop("PYTHONHOME", None)
            environment.pop("PYTHONPATH", None)

            install = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(installer),
                    "install",
                    "--archive",
                    str(archive),
                    "--checksum",
                    str(checksum),
                    "--destination",
                    str(destination),
                ],
                cwd=outside,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
            installed = destination / APPROVED_SKILLS[0]
            self.assertTrue((installed / "SKILL.md").is_file())
            archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.assertTrue((installed / INSTALL_RECEIPT_RELATIVE).is_file())
            self.assertEqual(
                json.loads((installed / INSTALL_RECEIPT_RELATIVE).read_text(encoding="utf-8")),
                {
                    "archive_sha256": archive_digest,
                    "release_version": VERSION,
                    "schema": INSTALL_RECEIPT_SCHEMA,
                    "skill_name": APPROVED_SKILLS[0],
                    "verification_status": "sha256_verified",
                },
            )
            after = {
                path.relative_to(release).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in release.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)

    def test_read_only_installed_poster_runs_agent_first_lifecycle_outside_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            release_a = temp / "release-a"
            release_b = temp / "release-b"
            first = _build(release_a)
            second = _build(release_b)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual(_tree_hashes(release_a), _tree_hashes(release_b))

            archive = release_a / f"autodesign-poster-{VERSION}.zip"
            checksum = release_a / f"{archive.name}.sha256"
            archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            with zipfile.ZipFile(archive) as bundle:
                self.assertNotIn(
                    f"autodesign-poster/{INSTALL_RECEIPT_RELATIVE.as_posix()}",
                    bundle.namelist(),
                )
            destination = temp / "host-root" / "skills"
            outside = temp / "unrelated-working-directory"
            mutable = temp / "mutable"
            outside.mkdir()
            mutable.mkdir()
            install = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(release_a / "package_agent_skills.py"),
                    "install",
                    "--archive",
                    str(archive),
                    "--checksum",
                    str(checksum),
                    "--destination",
                    str(destination),
                ],
                cwd=outside,
                env={
                    key: value
                    for key, value in os.environ.items()
                    if key not in {"PYTHONHOME", "PYTHONPATH"}
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
            installed = destination / "autodesign-poster"
            harness = installed / "scripts" / "poster_harness.py"
            before = _tree_hashes(installed)
            _make_tree_read_only(installed)
            environment = {
                key: value
                for key, value in os.environ.items()
                if key not in {"PYTHONHOME", "PYTHONPATH"}
            }

            def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [sys.executable, "-I", "-B", str(harness), *arguments],
                    cwd=outside,
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                )

            try:
                help_result = run_cli("--help")
                self.assertEqual(help_result.returncode, 0, help_result.stdout + help_result.stderr)
                self.assertIn("inspect-source", help_result.stdout)
                init_help = run_cli("init", "--help")
                self.assertEqual(init_help.returncode, 0, init_help.stdout + init_help.stderr)
                self.assertNotIn("--asset", init_help.stdout)

                doctor = run_cli(
                    "doctor",
                    "--cache-root",
                    str(mutable / "browser-cache"),
                )
                self.assertEqual(doctor.returncode, 2, doctor.stdout + doctor.stderr)
                self.assertFalse(json.loads(doctor.stdout)["ready"])
                self.assertFalse((mutable / "browser-cache").exists())

                source = mutable / "paper.md"
                source.write_text(
                    "# Grounded paper\n\nCentral method and primary result.\n",
                    encoding="utf-8",
                )
                run = mutable / "poster-run"
                initialized = run_cli(
                    "init",
                    "--run-dir",
                    str(run),
                    "--source",
                    str(source),
                )
                self.assertEqual(
                    initialized.returncode,
                    0,
                    initialized.stdout + initialized.stderr,
                )
                self.assertEqual(
                    json.loads(initialized.stdout)["resume"]["next_action"],
                    "inspect_source",
                )
                snapshot = json.loads(
                    (run / "skill_snapshot" / "manifest.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(snapshot["release_version"], VERSION)
                self.assertEqual(snapshot["archive_sha256"], archive_digest)
                snapshot_files = {entry["path"]: entry for entry in snapshot["files"]}
                self.assertIn(INSTALL_RECEIPT_RELATIVE.as_posix(), snapshot_files)
                self.assertEqual(
                    snapshot_files[INSTALL_RECEIPT_RELATIVE.as_posix()]["sha256"],
                    hashlib.sha256(
                        (installed / INSTALL_RECEIPT_RELATIVE).read_bytes()
                    ).hexdigest(),
                )

                inspected = run_cli("inspect-source", "--run-dir", str(run))
                self.assertEqual(inspected.returncode, 0, inspected.stdout + inspected.stderr)
                inspection = json.loads(inspected.stdout)
                self.assertEqual(inspection["run_format_version"], 2)
                self.assertEqual(inspection["source"]["source_type"], "markdown")
                self.assertEqual(inspection["pages"], [])

                resumed = run_cli("resume", "--run-dir", str(run))
                self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
                self.assertEqual(json.loads(resumed.stdout)["next_action"], "inspect_source")

                legacy = mutable / "legacy-run"
                legacy.mkdir()
                (legacy / "run.json").write_text(
                    json.dumps(
                        {
                            "active_attempt": None,
                            "attempt_count": 0,
                            "format_version": 1,
                            "state": "initialized",
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n",
                    encoding="utf-8",
                )
                diagnosed = run_cli("diagnose-v1", "--run-dir", str(legacy))
                self.assertEqual(diagnosed.returncode, 0, diagnosed.stdout + diagnosed.stderr)
                diagnosis = json.loads(diagnosed.stdout)
                self.assertEqual(diagnosis["mode"], "read_only")
                self.assertEqual(diagnosis["run_format_version"], 1)

                self.assertEqual(_tree_hashes(installed), before)
                self.assertFalse(any(installed.rglob("*.pyc")))
                self.assertFalse(any(path.name == "__pycache__" for path in installed.rglob("*")))
                for mutable_path in (run, mutable / "browser-cache", legacy):
                    self.assertFalse(
                        mutable_path == installed or installed in mutable_path.parents
                    )
            finally:
                _make_tree_removable(installed)

    def test_verified_install_rejects_an_unversioned_archive_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            release = temp / "release"
            result = _build(release)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            source = release / f"autodesign-poster-{VERSION}.zip"
            archive = temp / "autodesign-poster-latest.zip"
            shutil.copyfile(source, archive)
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            checksum = temp / f"{archive.name}.sha256"
            checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")

            install = subprocess.run(
                [
                    sys.executable,
                    str(release / "package_agent_skills.py"),
                    "install",
                    "--archive",
                    str(archive),
                    "--checksum",
                    str(checksum),
                    "--destination",
                    str(temp / "skills"),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(install.returncode, 0)
            self.assertIn("versioned release archive name", install.stdout + install.stderr)
            self.assertFalse((temp / "skills" / "autodesign-poster").exists())

    def test_unverified_manual_install_keeps_existing_init_provenance_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            release = temp / "release"
            result = _build(release)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            archive = release / f"autodesign-poster-{VERSION}.zip"
            destination = temp / "skills"
            install = subprocess.run(
                [
                    sys.executable,
                    str(release / "package_agent_skills.py"),
                    "install",
                    "--archive",
                    str(archive),
                    "--allow-unverified",
                    "--destination",
                    str(destination),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
            installed = destination / "autodesign-poster"
            self.assertFalse((installed / INSTALL_RECEIPT_RELATIVE).exists())
            harness = installed / "scripts" / "poster_harness.py"
            source = temp / "paper.md"
            source.write_text("# Manual source\n", encoding="utf-8")

            default_run = temp / "default-run"
            default_init = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(harness),
                    "init",
                    "--run-dir",
                    str(default_run),
                    "--source",
                    str(source),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(
                default_init.returncode, 0, default_init.stdout + default_init.stderr
            )
            default_snapshot = json.loads(
                (default_run / "skill_snapshot" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(default_snapshot["release_version"], "0.1.0")
            self.assertIsNone(default_snapshot["archive_sha256"])

            explicit_run = temp / "explicit-run"
            explicit_digest = "a" * 64
            explicit_init = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(harness),
                    "init",
                    "--run-dir",
                    str(explicit_run),
                    "--source",
                    str(source),
                    "--release-version",
                    "manual-build",
                    "--archive-sha256",
                    explicit_digest,
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(
                explicit_init.returncode, 0, explicit_init.stdout + explicit_init.stderr
            )
            explicit_snapshot = json.loads(
                (explicit_run / "skill_snapshot" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(explicit_snapshot["release_version"], "manual-build")
            self.assertEqual(explicit_snapshot["archive_sha256"], explicit_digest)

    def test_installed_poster_rejects_tampered_receipt_and_explicit_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            release = temp / "release"
            result = _build(release)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            archive = release / f"autodesign-poster-{VERSION}.zip"
            checksum = release / f"{archive.name}.sha256"
            destination = temp / "skills"
            install = subprocess.run(
                [
                    sys.executable,
                    str(release / "package_agent_skills.py"),
                    "install",
                    "--archive",
                    str(archive),
                    "--checksum",
                    str(checksum),
                    "--destination",
                    str(destination),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
            installed = destination / "autodesign-poster"
            source = temp / "paper.md"
            source.write_text("# Grounded source\n", encoding="utf-8")

            for label, extra in (
                ("release-version", ["--release-version", "9.9.9"]),
                ("archive-digest", ["--archive-sha256", "b" * 64]),
            ):
                with self.subTest(mismatch=label):
                    run = temp / f"mismatch-{label}"
                    completed = subprocess.run(
                        [
                            sys.executable,
                            "-B",
                            str(installed / "scripts" / "poster_harness.py"),
                            "init",
                            "--run-dir",
                            str(run),
                            "--source",
                            str(source),
                            *extra,
                        ],
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(completed.returncode, 1)
                    self.assertEqual(json.loads(completed.stdout)["status"], "error")
                    self.assertFalse(run.exists())

            receipt_path = installed / INSTALL_RECEIPT_RELATIVE
            self.assertTrue(receipt_path.is_file())
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["verification_status"] = "unknown"
            receipt_path.write_text(
                json.dumps(receipt, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            tampered_run = temp / "tampered-run"
            tampered = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(installed / "scripts" / "poster_harness.py"),
                    "init",
                    "--run-dir",
                    str(tampered_run),
                    "--source",
                    str(source),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(tampered.returncode, 1)
            self.assertEqual(json.loads(tampered.stdout)["status"], "error")
            self.assertFalse(tampered_run.exists())

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
                    if not tampered and info.filename.endswith("/SKILL.md"):
                        content += b"\n# tampered archive\n"
                        tampered = True
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
