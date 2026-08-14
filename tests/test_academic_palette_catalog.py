from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autodesign.util.academic_palette import (
    AcademicPaletteCatalogError,
    academic_palette_catalog_payload,
    load_academic_palette_library,
    require_academic_color_system,
)


class AcademicPaletteCatalogTest(unittest.TestCase):
    def test_catalog_payload_uses_canonical_ids_and_roles(self) -> None:
        payload = academic_palette_catalog_payload()
        self.assertEqual(payload["kind"], "academic_poster_color_palettes")
        self.assertGreaterEqual(len(payload["palettes"]), 13)
        first = payload["palettes"][0]
        self.assertEqual(set(first), {"id", "name", "roles"})
        self.assertEqual(
            set(first["roles"]),
            {"background", "text", "primary", "secondary", "accent", "header_text", "bar"},
        )

    def test_strict_loader_rejects_invalid_json_but_legacy_loader_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            path = Path(raw_tmp) / "palettes.json"
            path.write_text("not-json", encoding="utf-8")
            with self.assertRaises(AcademicPaletteCatalogError):
                load_academic_palette_library(path, strict=True)
            self.assertTrue(load_academic_palette_library(path)["palettes"])

    def test_strict_loader_rejects_invalid_palette_and_duplicate_id(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            path = Path(raw_tmp) / "palettes.json"
            palette = {
                "id": "test",
                "name": "Test",
                "roles": {
                    "background": "#FFFFFF",
                    "text": "#111111",
                    "primary": "#123456",
                    "secondary": "#DDEEFF",
                    "accent": "#123456",
                    "header_text": "#FFFFFF",
                    "bar": "#123456",
                },
            }
            path.write_text(json.dumps({
                "version": 1,
                "default_palette_id": "test",
                "palettes": [palette, palette],
            }), encoding="utf-8")
            with self.assertRaises(AcademicPaletteCatalogError):
                load_academic_palette_library(path, strict=True)

    def test_strict_loader_rejects_missing_or_extra_palette_roles(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            path = Path(raw_tmp) / "palettes.json"
            roles = {
                "background": "#FFFFFF",
                "text": "#111111",
                "primary": "#123456",
                "secondary": "#DDEEFF",
                "accent": "#123456",
                "header_text": "#FFFFFF",
                "bar": "#123456",
            }
            missing_role = dict(roles)
            missing_role.pop("background")
            for invalid_roles in (missing_role, {**roles, "unexpected": "#000000"}):
                path.write_text(json.dumps({
                    "version": 1,
                    "default_palette_id": "test",
                    "palettes": [{"id": "test", "name": "Test", "roles": invalid_roles}],
                }), encoding="utf-8")
                with self.assertRaises(AcademicPaletteCatalogError):
                    load_academic_palette_library(path, strict=True)

    def test_required_palette_rejects_unknown_id(self) -> None:
        with self.assertRaises(ValueError):
            require_academic_color_system("not-a-real-palette")


if __name__ == "__main__":
    unittest.main()
