from __future__ import annotations

import asyncio
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autodesign import config
from autodesign.harness_matrix import CODING_HARNESSES, build_coding_harness_capabilities
from scripts import web_server
from scripts.web_server import HarnessMatrixRequest


class WebHarnessMatrixContractTests(unittest.TestCase):
    def test_missing_codex_does_not_advertise_a_bare_command_template(self) -> None:
        with (
            patch.dict(os.environ, {"PATH": ""}, clear=True),
            patch.object(config.shutil, "which", return_value=None),
            patch.object(config, "_CODEX_APP_BINARY_CANDIDATES", ()),
        ):
            capability = build_coding_harness_capabilities()["codex"]

        self.assertFalse(capability["available"])
        self.assertEqual(capability["binary_source"], "missing")
        self.assertEqual(capability["surfaces"]["designer_author"]["cmd"], "")
        self.assertEqual(capability["surfaces"]["code_editor"]["cmd"], "")

    def test_configured_codex_binary_is_used_by_runtime_and_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            binary = Path(raw_tmp) / "custom" / "codex"
            binary.parent.mkdir(parents=True)
            binary.write_text(
                "#!/bin/sh\n"
                "if [ \"${1:-}\" = \"--version\" ]; then\n"
                "  echo 'codex-cli 0.146.0'\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"${1:-}\" = \"--help\" ]; then\n"
                "  echo '  --search'\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"${1:-}\" = \"exec\" ] && [ \"${2:-}\" = \"--help\" ]; then\n"
                "  echo '  --ephemeral --dangerously-bypass-approvals-and-sandbox --model'\n"
                "  exit 0\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            binary.chmod(0o755)

            with (
                patch.dict(
                    os.environ,
                    {
                        "PATH": "",
                        "AUTODESIGN_CODEX_BIN": str(binary),
                    },
                    clear=True,
                ),
                patch.object(config.shutil, "which", return_value=None),
                patch.object(config, "_CODEX_APP_BINARY_CANDIDATES", ()),
            ):
                capability = build_coding_harness_capabilities()["codex"]
                runtime = config.resolve_codex_runtime(required=("--ephemeral",))
                environment = web_server._environment_profile(path_env="")
                code_editor = web_server._code_editor_cmd_resolution(None)

        self.assertTrue(capability["available"])
        self.assertEqual(capability["binary"], str(binary))
        self.assertEqual(capability["binary_source"], "configured")
        self.assertEqual(
            shlex.split(capability["surfaces"]["code_editor"]["cmd"])[0],
            str(binary),
        )
        self.assertTrue(runtime["available"])
        self.assertEqual(runtime["source"], "configured")
        self.assertEqual(runtime["binary"], str(binary))
        self.assertTrue(environment["coding_agent"]["ready"])
        self.assertEqual(environment["coding_agent"]["binary"], str(binary))
        self.assertTrue(code_editor["available"])
        self.assertEqual(shlex.split(code_editor["cmd"])[0], str(binary))

    def test_configured_incompatible_codex_is_reported_as_incompatible(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            binary = Path(raw_tmp) / "custom" / "codex"
            binary.parent.mkdir(parents=True)
            binary.write_text(
                "#!/bin/sh\n"
                "if [ \"${1:-}\" = \"--version\" ]; then\n"
                "  echo 'codex-cli 0.27.0'\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"${1:-}\" = \"--help\" ]; then\n"
                "  echo '  --search'\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"${1:-}\" = \"exec\" ] && [ \"${2:-}\" = \"--help\" ]; then\n"
                "  echo '  --dangerously-bypass-approvals-and-sandbox'\n"
                "  exit 0\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            binary.chmod(0o755)

            with (
                patch.dict(
                    os.environ,
                    {
                        "PATH": "",
                        "AUTODESIGN_CODE_EDITOR_CODEX_BIN": str(binary),
                    },
                    clear=True,
                ),
                patch.object(config.shutil, "which", return_value=None),
                patch.object(config, "_CODEX_APP_BINARY_CANDIDATES", ()),
            ):
                runtime = config.resolve_codex_runtime(required=("--ephemeral",))

        self.assertFalse(runtime["available"])
        self.assertEqual(runtime["source"], "configured")
        self.assertEqual(runtime["binary"], str(binary))
        self.assertIn("--ephemeral", runtime["missing"])

    def test_codex_smoke_prefers_a_compatible_app_over_an_incompatible_path_binary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            old_binary = root / "path-bin" / "codex"
            app_binary = root / "ChatGPT.app" / "Contents" / "Resources" / "codex"
            for binary, version, supports_ephemeral in (
                (old_binary, "codex-cli 0.27.0", False),
                (app_binary, "codex-cli 0.146.0-alpha.3.1", True),
            ):
                binary.parent.mkdir(parents=True, exist_ok=True)
                help_line = (
                    "echo '  --ephemeral --dangerously-bypass-approvals-and-sandbox'"
                    if supports_ephemeral
                    else "echo '  --dangerously-bypass-approvals-and-sandbox'"
                )
                binary.write_text(
                    "#!/bin/sh\n"
                    "if [ \"${1:-}\" = \"--version\" ]; then\n"
                    f"  echo '{version}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [ \"${1:-}\" = \"--help\" ]; then\n"
                    "  echo '  --search'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [ \"${1:-}\" = \"exec\" ] && [ \"${2:-}\" = \"--help\" ]; then\n"
                    f"  {help_line}\n"
                    "  exit 0\n"
                    "fi\n"
                    "exit 0\n",
                    encoding="utf-8",
                )
                binary.chmod(0o755)

            settings = SimpleNamespace(
                designer_author_harness="codex",
                designer_author_model=None,
                designer_author_cmd="",
                code_editor_harness="codex",
                code_editor_model=None,
                code_editor_cmd="",
            )
            with (
                patch.object(config.shutil, "which", return_value=str(old_binary)),
                patch.object(config, "_CODEX_APP_BINARY_CANDIDATES", (app_binary,)),
            ):
                resolutions = {
                    "author": web_server._paper_poster_author_cmd_resolution(settings),
                    "editor": web_server._code_editor_cmd_resolution(settings),
                    "smoke": web_server._coding_agent_smoke_cmd_resolution(settings),
                }

        for surface, resolution in resolutions.items():
            with self.subTest(surface=surface):
                self.assertEqual(shlex.split(resolution["cmd"])[0], str(app_binary))
                self.assertEqual(
                    resolution["binary_version"],
                    "codex-cli 0.146.0-alpha.3.1",
                )
                self.assertEqual(
                    resolution["rejected_candidates"][0]["binary"],
                    str(old_binary),
                )
        self.assertTrue(resolutions["smoke"]["capabilities"]["exec_ephemeral"])
        self.assertIn(
            "--ephemeral",
            resolutions["smoke"]["rejected_candidates"][0]["missing"],
        )

    def test_codex_binary_resolution_supports_chatgpt_app_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            binary = Path(raw_tmp) / "ChatGPT.app" / "Contents" / "Resources" / "codex"
            legacy_binary = Path("/Applications/Codex.app/Contents/Resources/codex")
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)

            with (
                patch.object(config.shutil, "which", return_value=None),
                patch.object(
                    config,
                    "_CODEX_APP_BINARY_CANDIDATES",
                    (binary, legacy_binary),
                ),
                patch.dict(os.environ, {"PATH": ""}, clear=True),
            ):
                self.assertEqual(config.resolve_harness_binary("codex"), str(binary))
                self.assertEqual(
                    shlex.split(config.designer_author_command_for_harness("codex", None))[0],
                    str(binary),
                )
                self.assertEqual(
                    shlex.split(config.code_editor_command_for_harness("codex", None))[0],
                    str(binary),
                )
                with patch.dict(
                    os.environ,
                    {
                        "PATH": "",
                        "AUTODESIGN_CODE_EDITOR_CODEX_BIN": (
                            "/Applications/Codex.app/Contents/Resources/codex"
                        ),
                    },
                    clear=True,
                ):
                    self.assertEqual(
                        shlex.split(config.code_editor_command_for_harness("codex", None))[0],
                        str(binary),
                    )
                capability = build_coding_harness_capabilities()["codex"]
                self.assertTrue(capability["available"])
                self.assertEqual(capability["binary"], str(binary))
                self.assertEqual(capability["binary_source"], "app_bundle")

    def test_codex_web_resolvers_replace_unlaunchable_bare_command(self) -> None:
        binary = "/Applications/ChatGPT.app/Contents/Resources/codex"
        settings = SimpleNamespace(
            designer_author_harness="codex",
            designer_author_model=None,
            designer_author_cmd="codex --search exec --dangerously-bypass-approvals-and-sandbox -",
            code_editor_harness="codex",
            code_editor_model=None,
            code_editor_cmd="codex --search exec --dangerously-bypass-approvals-and-sandbox -",
        )

        with patch.object(web_server, "resolve_harness_binary", return_value=binary):
            author = web_server._paper_poster_author_cmd_resolution(settings)
            editor = web_server._code_editor_cmd_resolution(settings)
            smoke = web_server._coding_agent_smoke_cmd_resolution(settings)
            submitter = web_server._openresearch_submitter_cmd_resolution(
                SimpleNamespace(
                    openresearch_submitter_cmd=(
                        "/Applications/Codex.app/Contents/Resources/codex exec -"
                    )
                )
            )

        for surface, result in (
            ("author", author),
            ("editor", editor),
            ("smoke", smoke),
            ("submitter", submitter),
        ):
            with self.subTest(surface=surface):
                self.assertTrue(result["available"])
                self.assertEqual(shlex.split(result["cmd"])[0], binary)

        smoke_parts = shlex.split(smoke["cmd"])
        self.assertIn("--ephemeral", smoke_parts)
        self.assertNotIn("--search", smoke_parts)
        self.assertEqual(web_server.CodingAgentSmokeRequest(timeout_s=60).timeout_s, 60)

    def test_codex_auth_status_uses_ambient_login_without_managed_auth(self) -> None:
        binary = "/Applications/ChatGPT.app/Contents/Resources/codex"
        auth_dir = Path("/tmp/autodesign-empty-codex-home")
        captured: dict[str, object] = {}

        def build_env(base_env, *, harness, api_key=None, config_dir=None):
            captured["config_dir"] = config_dir
            return dict(base_env)

        with (
            patch.object(web_server, "resolve_harness_binary", return_value=binary),
            patch.object(web_server, "harness_auth_read_dir", return_value=auth_dir),
            patch.object(web_server, "harness_login_present", return_value=False),
            patch.object(web_server, "harness_subprocess_env", side_effect=build_env),
            patch.object(
                web_server,
                "_run_auth_status_blocking",
                return_value={"logged_in": True, "account": None, "returncode": 0},
            ),
            patch.object(web_server, "mark_harness_login") as mark_login,
        ):
            result = asyncio.run(web_server.harness_auth_status("codex"))

        self.assertIsNone(captured["config_dir"])
        self.assertTrue(result["logged_in"])
        mark_login.assert_not_called()

    def test_codex_subprocess_drops_only_unreachable_loopback_proxy(self) -> None:
        base_env = {
            "HTTP_PROXY": "http://127.0.0.1:7897",
            "HTTPS_PROXY": "http://proxy.example.test:8443",
        }
        with (
            patch.object(config, "_existing_harness_login_dir", return_value=None),
            patch.object(config.socket, "create_connection", side_effect=OSError("closed")),
        ):
            child_env = config.harness_subprocess_env(base_env, harness="codex")

        self.assertNotIn("HTTP_PROXY", child_env)
        self.assertEqual(child_env["HTTPS_PROXY"], "http://proxy.example.test:8443")

        with (
            patch.object(config, "_existing_harness_login_dir", return_value=None),
            patch.object(config.socket, "create_connection") as connect,
        ):
            reachable_env = config.harness_subprocess_env(
                {"HTTPS_PROXY": "http://localhost:7897"},
                harness="codex",
            )

        self.assertEqual(reachable_env["HTTPS_PROXY"], "http://localhost:7897")
        connect.return_value.close.assert_called_once()

    def test_default_coding_harnesses_are_used_by_initial_snapshot(self) -> None:
        request = HarnessMatrixRequest(paper_path="paper.pdf", prompt="Create a poster.")

        snapshot = web_server._initial_harness_matrix_snapshot(
            "matrix-test",
            request,
            matrix_dir=Path("matrix-test"),
        )

        self.assertEqual(
            tuple(row["harness"] for row in snapshot["rows"]),
            CODING_HARNESSES,
        )

    def test_designer_author_pi_binary_status_honors_configured_paths(self) -> None:
        env_keys = (
            "AUTODESIGN_DESIGNER_AUTHOR_PI_BIN",
            "DESIGN_ANYTHING_DESIGNER_AUTHOR_PI_BIN",
            "DESIGN_ANYTHING_PLANNER_AUTHOR_PI_BIN",
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            binary = Path(raw_tmp) / "configured-pi"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)

            for env_key in env_keys:
                with self.subTest(env_key=env_key), patch.dict(
                    os.environ,
                    {"PATH": "", env_key: str(binary)},
                    clear=True,
                ):
                    self.assertEqual(
                        web_server._designer_author_binary_status("pi"),
                        {
                            "available": True,
                            "source": "configured",
                            "binary": str(binary),
                        },
                    )

    def test_code_editor_pi_binary_status_honors_configured_paths(self) -> None:
        env_keys = (
            "AUTODESIGN_CODE_EDITOR_PI_BIN",
            "DESIGN_ANYTHING_CODE_EDITOR_PI_BIN",
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            binary = Path(raw_tmp) / "configured-pi"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)

            for env_key in env_keys:
                with self.subTest(env_key=env_key), patch.dict(
                    os.environ,
                    {"PATH": "", env_key: str(binary)},
                    clear=True,
                ):
                    self.assertEqual(
                        web_server._code_editor_binary_status("pi"),
                        {
                            "available": True,
                            "source": "configured",
                            "binary": str(binary),
                        },
                    )

    def test_pi_capability_honors_all_configured_binary_aliases(self) -> None:
        surface_env_keys = {
            "designer_author": (
                "AUTODESIGN_DESIGNER_AUTHOR_PI_BIN",
                "DESIGN_ANYTHING_DESIGNER_AUTHOR_PI_BIN",
                "DESIGN_ANYTHING_PLANNER_AUTHOR_PI_BIN",
            ),
            "code_editor": (
                "AUTODESIGN_CODE_EDITOR_PI_BIN",
                "DESIGN_ANYTHING_CODE_EDITOR_PI_BIN",
            ),
        }
        with tempfile.TemporaryDirectory() as raw_tmp:
            binary = Path(raw_tmp) / "configured-pi"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)

            for surface, env_keys in surface_env_keys.items():
                for env_key in env_keys:
                    with self.subTest(surface=surface, env_key=env_key), patch.dict(
                        os.environ,
                        {"PATH": "", env_key: str(binary)},
                        clear=True,
                    ):
                        capability = build_coding_harness_capabilities(None)["pi"]

                        self.assertTrue(capability["available"])
                        self.assertEqual(capability["binary_source"], "configured")
                        self.assertEqual(capability["binary"], str(binary))
                        self.assertIn(str(binary), capability["surfaces"][surface]["cmd"])


class WebHarnessMatrixBackgroundContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_request_is_converted_to_runner_cell_specs(self) -> None:
        observed: dict[str, object] = {}

        async def fake_to_thread(func: object, *args: object, **kwargs: object) -> None:
            observed["func"] = func
            observed.update(kwargs)

        request = HarnessMatrixRequest(paper_path="paper.pdf", prompt="Create a poster.")
        with tempfile.TemporaryDirectory() as raw_tmp, patch.object(
            web_server.asyncio,
            "to_thread",
            side_effect=fake_to_thread,
        ):
            matrix_dir = Path(raw_tmp) / "matrix-test"
            state = web_server._HarnessMatrixJobState(
                matrix_id="matrix-test",
                matrix_dir=matrix_dir,
            )

            await web_server._run_harness_matrix_in_background(
                state=state,
                req=request,
                env_overrides={},
                out_dir=Path(raw_tmp),
            )

        self.assertIs(observed["func"], web_server.run_harness_matrix)
        self.assertEqual(
            tuple(spec.harness for spec in observed["harnesses"]),
            CODING_HARNESSES,
        )


if __name__ == "__main__":
    unittest.main()
