from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PackageCompatibilityTest(unittest.TestCase):
    def run_python(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, *args],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    def assert_python_ok(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = self.run_python(*args)
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        return result

    def test_legacy_and_canonical_modules_share_identity_in_both_orders(self) -> None:
        pairs = [
            ("schema", "RunResult"),
            ("config", "Settings"),
            ("runner", "PipelineRunner"),
        ]
        for first_package, second_package in [
            ("autodesign", "design_anything"),
            ("design_anything", "autodesign"),
        ]:
            with self.subTest(first=first_package):
                script = (
                    "import importlib, json\n"
                    f"pairs = {pairs!r}\n"
                    f"first_package = {first_package!r}\n"
                    f"second_package = {second_package!r}\n"
                    "for suffix, symbol in pairs:\n"
                    "    first = importlib.import_module(f'{first_package}.{suffix}')\n"
                    "    second = importlib.import_module(f'{second_package}.{suffix}')\n"
                    "    assert first is second, (first, second)\n"
                    "    assert getattr(first, symbol) is getattr(second, symbol)\n"
                    "    canonical_name = f'autodesign.{suffix}'\n"
                    "    assert first.__name__ == canonical_name\n"
                    "    assert first.__spec__.name == canonical_name\n"
                    "print(json.dumps({'status': 'ok'}))\n"
                )
                result = self.assert_python_ok("-c", script)
                self.assertEqual(json.loads(result.stdout), {"status": "ok"})

    def test_legacy_executable_modules_are_canonical_on_ordinary_import(self) -> None:
        script = """
import importlib

for suffix in ("cli", "smoke", "evaluator.tools"):
    canonical = importlib.import_module(f"autodesign.{suffix}")
    legacy = importlib.import_module(f"design_anything.{suffix}")
    assert canonical is legacy, (suffix, canonical, legacy)
"""
        self.assert_python_ok("-c", script)

    def test_legacy_wrappers_are_runpy_compatible(self) -> None:
        for module, canonical_module in (
            ("design_anything.cli", "autodesign.cli"),
            ("design_anything.smoke", "autodesign.smoke"),
            ("design_anything.evaluator.tools", "autodesign.evaluator.tools"),
        ):
            with self.subTest(module=module):
                script = (
                    "import runpy\n"
                    f"namespace = runpy.run_module({module!r}, run_name='compat_probe')\n"
                    "assert callable(namespace['main'])\n"
                    f"assert namespace['main'].__module__ == {canonical_module!r}\n"
                )
                self.assert_python_ok("-c", script)

    def test_canonical_module_help_entrypoints(self) -> None:
        for module in ("autodesign", "autodesign.cli"):
            with self.subTest(module=module):
                result = self.assert_python_ok("-m", module, "--help")
                self.assertIn("usage: autodesign", result.stdout)

    def test_distribution_and_console_script_metadata_are_canonical(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        self.assertEqual(project["name"], "autodesign")
        self.assertEqual(project["scripts"]["autodesign"], "autodesign.cli:main")
        self.assertEqual(project["scripts"]["design-anything"], "autodesign.cli:main")

    def test_evaluator_asset_is_available_from_canonical_package(self) -> None:
        script = """
from importlib.resources import files
import json

asset = files("autodesign.evaluator").joinpath("assets/poster_gold_reference_specs.json")
assert asset.is_file(), asset
payload = json.loads(asset.read_text(encoding="utf-8"))
assert payload["version"]
"""
        self.assert_python_ok("-c", script)


if __name__ == "__main__":
    unittest.main()
