from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

import autodesign.run_file_access as run_file_access
from autodesign.run_file_access import RunFileAccessError, canonical_run_file_parts


class RunFileAccessTests(unittest.TestCase):
    def test_canonical_policy_rejects_cross_platform_alias_spellings(self) -> None:
        denied = (
            "source/RUN_CONTROL.JSON",
            "source/DERIVED_JOB.JSON",
            "source/final/trailing.png.",
            "source/final/trailing.png ",
            "source/final/secret:stream",
            "source/final/CON",
            "source/final/aux.txt",
            "source/final/CON .txt",
            "source/final/RUN_CO~1.JSO",
            "source%2F..%2Fvictim/final/secret.png",
            "source/%52UN_CONTROL.JSON",
            "source%252F..%252Fvictim/final/secret.png",
        )

        for relative in denied:
            with self.subTest(relative=relative), self.assertRaises(RunFileAccessError):
                canonical_run_file_parts(relative, expected_run_id="source")

        self.assertEqual(
            canonical_run_file_parts(
                "source/final/figure.v1.png",
                expected_run_id="source",
            ),
            ("source", "final", "figure.v1.png"),
        )

    def test_portable_opener_rejects_link_and_hardlink_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "runs"
            source = root / "source" / "final"
            victim = root / "victim" / "secret.png"
            source.mkdir(parents=True)
            victim.parent.mkdir(parents=True)
            victim.write_bytes(b"private")
            (source / "symlink.png").symlink_to(victim)
            (source / "hardlink.png").hardlink_to(victim)

            for name in ("symlink.png", "hardlink.png"):
                with self.subTest(name=name), self.assertRaises(RunFileAccessError):
                    run_file_access._open_portable(
                        root,
                        ("source", "final", name),
                    )

    def test_portable_opener_rejects_intermediate_junction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "runs"
            junction = root / "source" / "final"
            public = junction / "figure.png"
            public.parent.mkdir(parents=True)
            public.write_bytes(b"public")
            checked_paths: list[Path] = []

            def reports_junction(path: Path) -> bool:
                checked_paths.append(path)
                return path == junction

            with (
                mock.patch.object(
                    Path,
                    "is_junction",
                    new=reports_junction,
                    create=True,
                ),
                self.assertRaises(RunFileAccessError),
            ):
                run_file_access._open_portable(
                    root,
                    ("source", "final", "figure.png"),
                )

            self.assertIn(junction, checked_paths)

    def test_portable_opener_rejects_component_swap_during_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "runs"
            public = root / "source" / "final" / "figure.png"
            victim = root / "victim" / "secret.png"
            public.parent.mkdir(parents=True)
            victim.parent.mkdir(parents=True)
            public.write_bytes(b"public")
            victim.write_bytes(b"private")
            original_stat = run_file_access._portable_component_stat

            def stat_then_swap(path: Path):
                metadata = original_stat(path)
                if path == public and public.is_file() and not public.is_symlink():
                    public.unlink()
                    public.symlink_to(victim)
                return metadata

            with (
                mock.patch.object(
                    run_file_access,
                    "_portable_component_stat",
                    side_effect=stat_then_swap,
                ),
                self.assertRaises(RunFileAccessError),
            ):
                run_file_access._open_portable(
                    root,
                    ("source", "final", "figure.png"),
                )


if __name__ == "__main__":
    unittest.main()
