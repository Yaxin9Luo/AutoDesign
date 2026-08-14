from __future__ import annotations

import json
import inspect
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
from starlette.requests import Request

from autodesign import config
from autodesign.agents import external_designer_author
from autodesign.tools import html_renderer
from autodesign.util import html_artifact, logging, math_typesetting
from autodesign.util.layout_export import (
    LAYOUT_SCHEMA,
    layout_state_to_json,
)


def _request(*, cookie: str = "", query: str = "", path: str = "/") -> Request:
    headers = [(b"cookie", cookie.encode("latin-1"))] if cookie else []
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query.encode("ascii"),
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "root_path": "",
    })


class PersistedIdentifierCompatibilityTest(unittest.TestCase):
    def test_katex_bundle_writes_only_canonical_markers(self) -> None:
        with patch.object(
            math_typesetting,
            "_load_katex_assets",
            return_value=("KATEX_CSS", "KATEX_CORE", "KATEX_AUTO"),
        ):
            bundle = math_typesetting.inline_katex_bundle(Path("/repo"))

        self.assertIn('id="autodesign-katex-css"', bundle)
        self.assertIn('data-autodesign-katex="1"', bundle)
        self.assertIn("window.__autoDesignMathReady", bundle)
        self.assertNotIn("data-designanything-katex", bundle)
        self.assertNotIn("window.__designAnythingMathReady", bundle)

    def test_katex_injection_recognizes_legacy_marker(self) -> None:
        legacy = (
            '<html><head><style id="od-katex-css" data-designanything-katex="1"></style>'
            "</head><body><main class='paper-poster'>$x$</main></body></html>"
        )
        patched, applied = math_typesetting.inject_katex_into_html(
            legacy,
            Path("/missing-repo"),
        )

        self.assertFalse(applied)
        self.assertEqual(patched, legacy)

    def test_math_helpers_keep_legacy_runtime_globals_readable(self) -> None:
        class FakePage:
            def __init__(self) -> None:
                self.evaluate_source = ""
                self.wait_source = ""

            def evaluate(self, source: str) -> bool:
                self.evaluate_source = source
                return True

            def wait_for_function(self, source: str, **_kwargs: object) -> None:
                self.wait_source = source

        page = FakePage()
        wait_for_math = getattr(math_typesetting, "wait_for_autodesign_math", None)
        self.assertIsNotNone(wait_for_math)
        wait_for_math(page)

        self.assertIn("__autoDesignMathReady", page.evaluate_source)
        self.assertIn("__designAnythingMathReady", page.evaluate_source)
        self.assertIn("data-autodesign-katex", page.evaluate_source)
        self.assertIn("data-designanything-katex", page.evaluate_source)
        self.assertIn("__autoDesignMathReady", page.wait_source)
        self.assertIn("__designAnythingMathReady", page.wait_source)
        self.assertIs(
            math_typesetting.wait_for_designanything_math,
            math_typesetting.wait_for_autodesign_math,
        )

    def test_external_author_accepts_both_katex_markers(self) -> None:
        canonical = BeautifulSoup(
            '<script data-autodesign-katex="1">core</script>',
            "html.parser",
        ).script
        legacy = BeautifulSoup(
            '<script data-designanything-katex="1">core</script>',
            "html.parser",
        ).script

        self.assertTrue(external_designer_author._is_system_katex_tag(canonical))
        self.assertTrue(external_designer_author._is_system_katex_tag(legacy))

    def test_external_author_accepts_legacy_katex_init_script_hash(self) -> None:
        canonical_script = "window.__autoDesignMathReady = true;"
        legacy_script = "window.__designAnythingMathReady = true;"
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            external_designer_author,
            "inline_katex_bundle",
            return_value=(
                f'<script data-autodesign-katex="1">{canonical_script}</script>'
            ),
        ):
            hashes = external_designer_author._system_katex_script_hashes(Path(tmp))

        legacy_tag = BeautifulSoup(
            f'<script data-designanything-katex="1">{legacy_script}</script>',
            "html.parser",
        ).script
        self.assertTrue(
            external_designer_author._is_allowed_system_katex_script(
                legacy_tag,
                hashes,
            )
        )

    def test_header_guard_writes_canonical_marker_and_reads_legacy_marker(self) -> None:
        self.assertIn(
            'data-autodesign-header-collapse-guard="1"',
            external_designer_author._HEADER_COLLAPSE_GUARD_CSS,
        )
        self.assertNotIn(
            "data-designanything-header-collapse-guard",
            external_designer_author._HEADER_COLLAPSE_GUARD_CSS,
        )
        with tempfile.TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "poster.html"
            html_path.write_text(
                '<style data-designanything-header-collapse-guard="1"></style>',
                encoding="utf-8",
            )
            with patch(
                "playwright.sync_api.sync_playwright",
            ) as sync_playwright:
                result = external_designer_author._maybe_repair_collapsed_poster_header(
                    html_path,
                    {"w_px": 1200, "h_px": 800},
                )

        self.assertIsNone(result)
        sync_playwright.assert_not_called()

    def test_layout_export_and_browser_script_write_canonical_identifiers(self) -> None:
        payload = json.loads(layout_state_to_json(
            {"hero": {"tx": 1, "ty": 2, "w": 300, "h": 200}},
            run_id="run-1",
        ))
        script = html_renderer._edit_script([])

        self.assertEqual(LAYOUT_SCHEMA, "autodesign.layout.v1")
        self.assertEqual(payload["schema"], "autodesign.layout.v1")
        self.assertIn("'autodesign.layout.' + runId", script)
        self.assertIn("schema: 'autodesign.layout.v1'", script)
        self.assertIn("localStorage.setItem(LS_KEY", script)
        self.assertNotIn("localStorage.setItem(LEGACY_LS_KEY", script)

    def test_browser_layout_restore_reads_legacy_storage_key(self) -> None:
        script = html_renderer._edit_script([])

        self.assertIn("'designanything.layout.' + runId", script)
        self.assertIn("localStorage.getItem(LEGACY_LS_KEY)", script)

    def test_legacy_source_marker_writes_canonical_and_reads_both_names(self) -> None:
        artifact = html_artifact.deck_html_to_html_artifact(
            {"title": "Deck", "theme": {}, "slides": []},
        )

        self.assertEqual(
            artifact.theme.get("_autodesign_legacy_source"),
            "deck_html",
        )
        self.assertNotIn("_designanything_legacy_source", artifact.theme)
        self.assertTrue(html_artifact.has_legacy_source_marker({
            "_autodesign_legacy_source": "layer_graph",
        }))
        self.assertTrue(html_artifact.has_legacy_source_marker({
            "_designanything_legacy_source": "layer_graph",
        }))

    def test_legacy_source_layout_audit_still_skips_old_artifacts(self) -> None:
        blocks = [
            {"block_id": f"text_{idx}", "kind": "text", "text": "body"}
            for idx in range(6)
        ]
        artifact = {
            "target": "poster",
            "theme": {"_designanything_legacy_source": "layer_graph"},
            "frames": [{
                "frame_id": "poster_canvas",
                "kind": "canvas",
                "blocks": blocks,
            }],
        }

        self.assertEqual(html_artifact.audit_frame_layout_plan(artifact), [])

    def test_contextvars_use_canonical_debug_labels(self) -> None:
        self.assertIn("autodesign_run_id", repr(logging._run_id_var))
        self.assertIn("autodesign_run_dir", repr(logging._run_dir_var))

    def test_claude_prompt_command_uses_canonical_filename(self) -> None:
        command = config._claude_prompt_file_command("claude", model="test-model")

        self.assertIn(".autodesign_claude_prompt.md", command)
        self.assertNotIn(".designanything_claude_prompt.md", command)

    def test_harness_auth_defaults_to_canonical_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            config,
            "_project_env_first",
            return_value="",
        ), patch.object(config.Path, "home", return_value=Path(tmp)):
            auth_dir = config.harness_auth_dir("claude")

        self.assertEqual(
            auth_dir,
            Path(tmp) / ".autodesign" / "harness-auth" / "claude",
        )

    def test_harness_auth_uses_existing_legacy_login_for_subprocesses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            config,
            "_project_env_first",
            return_value="",
        ), patch.object(config.Path, "home", return_value=Path(tmp)):
            legacy_dir = Path(tmp) / ".designanything" / "harness-auth" / "claude"
            legacy_dir.mkdir(parents=True)
            (legacy_dir / ".designanything-connected.json").write_text(
                "{}",
                encoding="utf-8",
            )

            self.assertTrue(config.harness_login_present("claude"))
            env = config.harness_subprocess_env(
                {"ANTHROPIC_BASE_URL": "https://legacy-gateway.example"},
                harness="claude",
            )

        self.assertEqual(env.get("CLAUDE_CONFIG_DIR"), str(legacy_dir))
        self.assertNotIn("ANTHROPIC_BASE_URL", env)

    def test_harness_login_marker_writes_canonical_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            config,
            "_project_env_first",
            return_value=str(Path(tmp) / "auth"),
        ):
            config.mark_harness_login("codex")
            auth_dir = config.harness_auth_dir("codex")

            self.assertTrue((auth_dir / ".autodesign-connected.json").is_file())
            self.assertFalse((auth_dir / ".designanything-connected.json").exists())

    def test_harness_login_marker_can_follow_selected_legacy_auth_dir(self) -> None:
        self.assertIn("config_dir", inspect.signature(config.mark_harness_login).parameters)
        with tempfile.TemporaryDirectory() as tmp:
            legacy_dir = Path(tmp) / ".designanything" / "harness-auth" / "claude"
            config.mark_harness_login("claude", config_dir=legacy_dir)

            self.assertTrue((legacy_dir / ".autodesign-connected.json").is_file())
            self.assertFalse((legacy_dir / ".designanything-connected.json").exists())

    def test_web_demo_user_prefers_canonical_cookie_and_reads_legacy_cookie(self) -> None:
        from scripts import web_server

        canonical = _request(
            cookie="autodesign_demo_user=new-user; designanything_demo_user=old-user",
        )
        legacy = _request(cookie="designanything_demo_user=old-user")

        self.assertEqual(web_server._demo_user_id(canonical), "user:new-user")
        self.assertEqual(web_server._demo_user_id(legacy), "user:old-user")

    def test_web_run_token_cookie_writes_canonical_and_reads_legacy_name(self) -> None:
        from scripts import web_server

        self.assertEqual(
            web_server._demo_run_file_token_cookie_name("run-1"),
            "autodesign_run_token_run-1",
        )
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            run_dir = runs_dir / "run-1"
            run_dir.mkdir()
            (run_dir / "poster.html").write_text("poster", encoding="utf-8")
            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "_RUN_ACCESS_CONTROL", True),
                patch.object(
                    web_server,
                    "_demo_run_access",
                    return_value={"token": "secret"},
                ),
            ):
                legacy_response = web_server._demo_run_file_response(
                    "run-1/poster.html",
                    _request(
                        cookie="designanything_run_token_run-1=secret",
                        path="/api/files/runs/run-1/poster.html",
                    ),
                )
                canonical_response = web_server._demo_run_file_response(
                    "run-1/poster.html",
                    _request(
                        query="token=secret",
                        path="/api/files/runs/run-1/poster.html",
                    ),
                )

        self.assertEqual(legacy_response.status_code, 200)
        self.assertIn(
            "autodesign_run_token_run-1=secret",
            canonical_response.headers.get("set-cookie", ""),
        )

    def test_web_style_patch_migrates_legacy_id_to_canonical_id(self) -> None:
        from scripts import web_server

        doc = BeautifulSoup(
            "<html><head><style id='designanything-style-tweaks'>old</style></head>"
            "<body><main class='paper-poster'></main></body></html>",
            "html.parser",
        )

        web_server._apply_global_poster_style_patch(doc, {"accent": "#112233"})

        self.assertIsNotNone(doc.find(id="autodesign-style-tweaks"))
        self.assertIsNone(doc.find(id="designanything-style-tweaks"))
        self.assertEqual(len(doc.find_all("style", id="autodesign-style-tweaks")), 1)


if __name__ == "__main__":
    unittest.main()
