from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from autodesign import config


ROOT = Path(__file__).resolve().parents[1]


_SETTINGS_PROBE = r"""
import json
import os

from autodesign.config import harness_auth_dir, harness_subprocess_env, load_settings

payload = json.loads(os.environ.pop("AUTODESIGN_ENV_TEST_PAYLOAD"))
for name in list(os.environ):
    if name.startswith(("AUTODESIGN_", "DESIGN_ANYTHING_", "OPEN_DESIGN_")):
        os.environ.pop(name, None)
for name in ("HARNESS_API_KEY", "OPENRESEARCH_API_URL", "POSTER_HARNESS_MODE"):
    os.environ.pop(name, None)
os.environ.update(payload)

settings = load_settings()
child_env = harness_subprocess_env(os.environ, harness="custom")
canonical_logo_prefix = "AUTODESIGN_" + "IDENTITY_LOGO_AGENT"
print(json.dumps({
    "child_designer_author": child_env.get("AUTODESIGN_DESIGNER_AUTHOR"),
    "child_has_canonical_logo_agent": any(
        name.startswith(f"{canonical_logo_prefix}_")
        or name == canonical_logo_prefix
        for name in child_env
    ),
    "code_editor_harness": settings.code_editor_harness,
    "designer_author_harness": settings.designer_author_harness,
    "designer_author_max_attempts": settings.designer_author_max_attempts,
    "designer_author_mode": settings.designer_author_mode,
    "designer_author_timeout_s": settings.designer_author_timeout_s,
    "harness_auth_dir": str(harness_auth_dir("codex").parent),
    "identity_logo_agent_mode": settings.identity_logo_agent_mode,
    "openresearch_api_url": settings.openresearch_api_url,
    "openresearch_default_repo_full_name": settings.openresearch_default_repo_full_name,
    "openresearch_org_id": settings.openresearch_org_id,
    "openresearch_submitter_cmd": settings.openresearch_submitter_cmd,
    "openresearch_submitter_mode": settings.openresearch_submitter_mode,
    "openresearch_submitter_timeout_s": settings.openresearch_submitter_timeout_s,
    "openresearch_timeout_s": settings.openresearch_timeout_s,
    "poster_harness_mode": settings.poster_harness_mode,
}, sort_keys=True))
"""


class EnvironmentNamespaceCompatibilityTest(unittest.TestCase):
    def run_probe(self, payload: dict[str, str]) -> dict[str, object]:
        env = os.environ.copy()
        env["OPENROUTER_API_KEY"] = "env-namespace-test-key"
        env["AUTODESIGN_ENV_TEST_PAYLOAD"] = json.dumps(payload)
        completed = subprocess.run(
            [sys.executable, "-c", _SETTINGS_PROBE],
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        return json.loads(completed.stdout)

    def test_autodesign_namespace_wins_over_all_legacy_names(self) -> None:
        result = self.run_probe({
            "AUTODESIGN_CODE_EDITOR_HARNESS": "zcode",
            "AUTODESIGN_DESIGNER_AUTHOR": "external",
            "AUTODESIGN_DESIGNER_AUTHOR_HARNESS": "claude",
            "AUTODESIGN_DESIGNER_AUTHOR_MAX_ATTEMPTS": "4",
            "AUTODESIGN_DESIGNER_AUTHOR_TIMEOUT_SECONDS": "41",
            "AUTODESIGN_HARNESS_AUTH_DIR": "/tmp/autodesign-auth",
            "AUTODESIGN_OPENRESEARCH_API_URL": "https://canonical.example/api",
            "AUTODESIGN_OPENRESEARCH_ORG_ID": "canonical-org",
            "AUTODESIGN_POSTER_HARNESS_MODE": "quality",
            "DESIGN_ANYTHING_CODE_EDITOR_HARNESS": "codex",
            "DESIGN_ANYTHING_DESIGNER_AUTHOR": "internal",
            "DESIGN_ANYTHING_DESIGNER_AUTHOR_HARNESS": "codex",
            "DESIGN_ANYTHING_DESIGNER_AUTHOR_MAX_ATTEMPTS": "5",
            "DESIGN_ANYTHING_DESIGNER_AUTHOR_TIMEOUT_SECONDS": "42",
            "DESIGN_ANYTHING_HARNESS_AUTH_DIR": "/tmp/design-anything-auth",
            "DESIGN_ANYTHING_OPENRESEARCH_API_URL": "https://design-anything.example/api",
            "DESIGN_ANYTHING_OPENRESEARCH_ORG_ID": "design-anything-org",
            "DESIGN_ANYTHING_POSTER_HARNESS_MODE": "standard",
            "DESIGN_ANYTHING_PLANNER_AUTHOR_HARNESS": "opencode",
            "OPEN_DESIGN_OPENRESEARCH_API_URL": "https://open-design.example/api",
            "OPEN_DESIGN_OPENRESEARCH_ORG_ID": "open-design-org",
            "OPENRESEARCH_API_URL": "https://unprefixed.example/api",
            "POSTER_HARNESS_MODE": "cheap",
        })

        self.assertEqual(result["designer_author_mode"], "external")
        self.assertEqual(result["child_designer_author"], "external")
        self.assertFalse(result["child_has_canonical_logo_agent"])
        self.assertEqual(result["designer_author_harness"], "claude")
        self.assertEqual(result["designer_author_timeout_s"], 41)
        self.assertEqual(result["designer_author_max_attempts"], 4)
        self.assertEqual(result["code_editor_harness"], "zcode")
        self.assertEqual(result["poster_harness_mode"], "quality")
        self.assertEqual(result["openresearch_api_url"], "https://canonical.example/api")
        self.assertEqual(result["openresearch_org_id"], "canonical-org")
        self.assertEqual(result["harness_auth_dir"], "/tmp/autodesign-auth")

    def test_design_anything_local_env_shape_keeps_baseline_settings(self) -> None:
        result = self.run_probe({
            "DESIGN_ANYTHING_DESIGNER_AUTHOR": "external",
            "DESIGN_ANYTHING_DESIGNER_AUTHOR_HARNESS": "codex",
            "DESIGN_ANYTHING_DESIGNER_AUTHOR_MAX_ATTEMPTS": "12",
            "DESIGN_ANYTHING_DESIGNER_AUTHOR_TIMEOUT_SECONDS": "3600",
            "DESIGN_ANYTHING_IDENTITY_LOGO_AGENT": "required",
        })

        self.assertEqual(result["designer_author_mode"], "external")
        self.assertEqual(result["child_designer_author"], "external")
        self.assertFalse(result["child_has_canonical_logo_agent"])
        self.assertEqual(result["designer_author_harness"], "codex")
        self.assertEqual(result["designer_author_timeout_s"], 3600)
        self.assertEqual(result["designer_author_max_attempts"], 12)
        self.assertEqual(result["code_editor_harness"], "codex")
        self.assertEqual(result["poster_harness_mode"], "dogfood")
        self.assertEqual(result["identity_logo_agent_mode"], "off")

    def test_planner_author_and_open_design_aliases_remain_fallbacks(self) -> None:
        result = self.run_probe({
            "DESIGN_ANYTHING_PLANNER_AUTHOR": "external",
            "DESIGN_ANYTHING_PLANNER_AUTHOR_HARNESS": "claude",
            "DESIGN_ANYTHING_PLANNER_AUTHOR_MAX_ATTEMPTS": "3",
            "DESIGN_ANYTHING_PLANNER_AUTHOR_TIMEOUT_SECONDS": "77",
            "OPEN_DESIGN_OPENRESEARCH_API_URL": "https://legacy-open-design.example/api",
            "OPEN_DESIGN_OPENRESEARCH_ORG_ID": "legacy-org",
            "OPEN_DESIGN_OPENRESEARCH_REPO": "legacy/repo",
            "OPEN_DESIGN_OPENRESEARCH_SUBMITTER": "custom",
            "OPEN_DESIGN_OPENRESEARCH_SUBMITTER_CMD": "legacy-submitter",
            "OPEN_DESIGN_OPENRESEARCH_SUBMITTER_TIMEOUT_SECONDS": "88",
            "OPEN_DESIGN_OPENRESEARCH_TIMEOUT_SECONDS": "99",
            "POSTER_HARNESS_MODE": "cheap",
        })

        self.assertEqual(result["designer_author_mode"], "external")
        self.assertEqual(result["child_designer_author"], "external")
        self.assertEqual(result["designer_author_harness"], "claude")
        self.assertEqual(result["designer_author_timeout_s"], 77)
        self.assertEqual(result["designer_author_max_attempts"], 3)
        self.assertEqual(result["poster_harness_mode"], "cheap")
        self.assertEqual(result["openresearch_api_url"], "https://legacy-open-design.example/api")
        self.assertEqual(result["openresearch_org_id"], "legacy-org")
        self.assertEqual(result["openresearch_default_repo_full_name"], "legacy/repo")
        self.assertEqual(result["openresearch_submitter_mode"], "custom")
        self.assertEqual(result["openresearch_submitter_cmd"], "legacy-submitter")
        self.assertEqual(result["openresearch_submitter_timeout_s"], 88)
        self.assertEqual(result["openresearch_timeout_s"], 99)

    def test_brand_neutral_web_headers_map_to_canonical_env_names(self) -> None:
        script = r"""
import json

from starlette.requests import Request
from scripts import web_server

request = Request({
    "type": "http",
    "headers": [
        (b"x-designer-author-model", b"author-model"),
        (b"x-code-editor-harness", b"claude"),
        (b"x-openresearch-org-id", b"org-from-header"),
    ],
})
overrides, has_key = web_server._request_env_overrides(request)
print(json.dumps({"overrides": overrides, "has_key": has_key}, sort_keys=True))
"""
        env = os.environ.copy()
        env["OPENROUTER_API_KEY"] = "env-namespace-test-key"
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        self.assertEqual(json.loads(completed.stdout), {
            "has_key": False,
            "overrides": {
                "AUTODESIGN_CODE_EDITOR_HARNESS": "claude",
                "AUTODESIGN_DESIGNER_AUTHOR_MODEL": "author-model",
                "AUTODESIGN_OPENRESEARCH_ORG_ID": "org-from-header",
            },
        })

    def test_web_demo_flags_use_canonical_false_and_numeric_values(self) -> None:
        script = r"""
import json
from scripts import web_server

print(json.dumps({
    "daily_limit": web_server._DEMO_DAILY_LIMIT,
    "demo_mode": web_server._DEMO_MODE,
    "public_user_isolation": web_server._PUBLIC_USER_ISOLATION,
}, sort_keys=True))
"""
        env = os.environ.copy()
        env.update({
            "AUTODESIGN_DEMO_DAILY_LIMIT": "7",
            "AUTODESIGN_DEMO_MODE": "0",
            "AUTODESIGN_PUBLIC_USER_ISOLATION": "0",
            "DESIGN_ANYTHING_DEMO_DAILY_LIMIT": "9",
            "DESIGN_ANYTHING_DEMO_MODE": "1",
            "DESIGN_ANYTHING_PUBLIC_USER_ISOLATION": "1",
            "DEMO_MODE": "1",
            "PUBLIC_USER_ISOLATION": "1",
        })
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        self.assertEqual(json.loads(completed.stdout), {
            "daily_limit": 7,
            "demo_mode": False,
            "public_user_isolation": False,
        })

    def test_openresearch_subprocess_env_contains_canonical_and_legacy_names(self) -> None:
        script = r"""
import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autodesign.agents.openresearch_gui_submitter import submit_openresearch_gui

captured = {}
def fake_run(*args, **kwargs):
    captured.update(kwargs["env"])
    return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

with tempfile.TemporaryDirectory() as tmp, patch("autodesign.agents.openresearch_gui_submitter.subprocess.run", side_effect=fake_run):
    submit_openresearch_gui(
        settings=SimpleNamespace(
            openresearch_submitter_mode="custom",
            openresearch_submitter_cmd="fake-submitter",
            openresearch_submitter_timeout_s=10,
        ),
        job_dir=Path(tmp),
        project_url="https://openresearch.sh/orgs/org-test/projects",
        agent_prompt="test prompt",
        project_id=None,
        org_id="org-test",
        paper_id="paper-test",
        repo_full_name="owner/repo",
        source_run_id="run-test",
        artifact_id="artifact-test",
    )

keys = [
    "AUTODESIGN_OPENRESEARCH_URL",
    "AUTODESIGN_OPENRESEARCH_PROJECT_URL",
    "AUTODESIGN_OPENRESEARCH_AGENT_PROMPT_FILE",
    "AUTODESIGN_OPENRESEARCH_SUBMIT_REQUEST",
    "AUTODESIGN_OPENRESEARCH_DONE_FILE",
    "DESIGN_ANYTHING_OPENRESEARCH_URL",
    "DESIGN_ANYTHING_OPENRESEARCH_PROJECT_URL",
    "DESIGN_ANYTHING_OPENRESEARCH_AGENT_PROMPT_FILE",
    "DESIGN_ANYTHING_OPENRESEARCH_SUBMIT_REQUEST",
    "DESIGN_ANYTHING_OPENRESEARCH_DONE_FILE",
]
print(json.dumps({key: bool(captured.get(key)) for key in keys}, sort_keys=True))
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=os.environ.copy(),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        self.assertTrue(all(json.loads(completed.stdout).values()))

    def test_deepseek_harness_key_uses_isolated_home_and_preserves_base_url(self) -> None:
        auth_dir = Path("/tmp/autodesign-deepseek-auth")
        with patch.object(config, "harness_auth_dir", return_value=auth_dir):
            child_env = config.harness_subprocess_env(
                {
                    "DEEPSEEK_BASE_URL": "https://api.deepseek.example/v1",
                    "DEEPSEEK_API_KEY": "ambient-key",
                },
                harness="deepseek",
                api_key="explicit-key",
            )

        self.assertEqual(child_env["DEEPSEEK_API_KEY"], "explicit-key")
        self.assertEqual(child_env["DEEPSEEK_BASE_URL"], "https://api.deepseek.example/v1")
        self.assertEqual(child_env["DSH_HOME"], str(auth_dir))
        self.assertEqual(child_env["DSH_PERMISSION_MODE"], "workspace-write")
        self.assertEqual(child_env["DSH_TELEMETRY_DISABLED"], "1")

    def test_deepseek_harness_without_explicit_key_preserves_ambient_setup(self) -> None:
        child_env = config.harness_subprocess_env(
            {
                "DEEPSEEK_API_KEY": "ambient-key",
                "DEEPSEEK_BASE_URL": "https://api.deepseek.example/v1",
                "DSH_HOME": "/tmp/existing-dsh-home",
                "DSH_PERMISSION_MODE": "danger-full-access",
                "DSH_TELEMETRY_DISABLED": "custom",
            },
            harness="deepseek",
        )

        self.assertEqual(child_env["DEEPSEEK_API_KEY"], "ambient-key")
        self.assertEqual(child_env["DSH_HOME"], "/tmp/existing-dsh-home")
        self.assertEqual(child_env["DSH_PERMISSION_MODE"], "danger-full-access")
        self.assertEqual(child_env["DSH_TELEMETRY_DISABLED"], "custom")

    def test_utility_env_reads_use_canonical_precedence(self) -> None:
        script = r"""
import json
import os
from types import SimpleNamespace

for name in list(os.environ):
    if name.startswith(("AUTODESIGN_", "DESIGN_ANYTHING_", "OPEN_DESIGN_")):
        os.environ.pop(name, None)
os.environ.update({
    "AUTODESIGN_ACADEMIC_IDENTITY_SEARCH": "0",
    "AUTODESIGN_ALLOW_CHROME_CHANNEL_FALLBACK": "1",
    "AUTODESIGN_INGEST_PDF_CACHE": "0",
    "AUTODESIGN_MODEL_PRICES_JSON": '{"canonical/model":{"input":2}}',
    "AUTODESIGN_OPENRESEARCH_API_URL": "https://canonical-client.example/api",
    "AUTODESIGN_OPENRESEARCH_TOKEN": "canonical-token",
    "AUTODESIGN_DETERMINISTIC_LAYOUT_REPAIR": "0",
    "AUTODESIGN_POSTER_CANVAS_AUTO_EXPAND": "0",
    "AUTODESIGN_POSTER_REFERENCE_PROFILE": "editorial-flow",
    "AUTODESIGN_POSTER_TEMPLATE": "portrait",
    "DESIGN_ANYTHING_ACADEMIC_IDENTITY_SEARCH": "1",
    "DESIGN_ANYTHING_ALLOW_CHROME_CHANNEL_FALLBACK": "0",
    "DESIGN_ANYTHING_INGEST_PDF_CACHE": "1",
    "DESIGN_ANYTHING_MODEL_PRICES_JSON": '{"legacy/model":{"input":3}}',
    "DESIGN_ANYTHING_OPENRESEARCH_API_URL": "https://legacy-client.example/api",
    "DESIGN_ANYTHING_OPENRESEARCH_TOKEN": "legacy-token",
    "DESIGN_ANYTHING_DETERMINISTIC_LAYOUT_REPAIR": "1",
    "DESIGN_ANYTHING_POSTER_CANVAS_AUTO_EXPAND": "1",
    "DESIGN_ANYTHING_POSTER_REFERENCE_PROFILE": "research-synthesis",
    "DESIGN_ANYTHING_POSTER_TEMPLATE": "landscape",
    "POSTER_CANVAS_AUTO_EXPAND": "1",
    "POSTER_CANVAS_TEMPLATE": "landscape",
    "POSTER_DETERMINISTIC_LAYOUT_REPAIR": "1",
    "POSTER_REFERENCE_PROFILE": "research-synthesis",
})

from autodesign.util import browser_render, layout_grounder, run_telemetry
from autodesign.util.academic_identity_search import academic_identity_search_enabled
from autodesign.util.openresearch_api import OpenResearchApiClient
from autodesign.util.pipeline_cache import pipeline_cache_enabled
from autodesign.tools.ingest_document import (
    _reference_profile_env,
    _reference_template_requests_landscape,
)
from autodesign.tools.propose_paper_poster_html import (
    _canvas_auto_expand_enabled,
    _deterministic_layout_repair_enabled,
)

class FakeChromium:
    def launch(self, *args, **kwargs):
        if kwargs.get("channel") == "chrome":
            return "chrome-fallback"
        raise RuntimeError("primary unavailable")

p = SimpleNamespace(chromium=FakeChromium())
client = OpenResearchApiClient(credentials_path=__import__("pathlib").Path("/missing"))
print(json.dumps({
    "browser_render": browser_render._launch_chromium(p),
    "cache_enabled": pipeline_cache_enabled("ingest_pdf"),
    "canvas_auto_expand": _canvas_auto_expand_enabled(SimpleNamespace(state={
        "canvas_plan": {"preset_id": "cvpr-landscape"},
    })),
    "deterministic_layout_repair": _deterministic_layout_repair_enabled(),
    "identity_search": academic_identity_search_enabled(),
    "layout_grounder": layout_grounder._launch_chromium(p),
    "openresearch_api_url": client.api_url,
    "openresearch_token": client.token,
    "prices": run_telemetry._load_prices(),
    "reference_profile": _reference_profile_env(),
    "reference_template_landscape": _reference_template_requests_landscape(),
}, sort_keys=True))
"""
        env = os.environ.copy()
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        self.assertEqual(json.loads(completed.stdout), {
            "browser_render": "chrome-fallback",
            "cache_enabled": False,
            "canvas_auto_expand": False,
            "deterministic_layout_repair": False,
            "identity_search": False,
            "layout_grounder": "chrome-fallback",
            "openresearch_api_url": "https://canonical-client.example/api",
            "openresearch_token": "canonical-token",
            "prices": {"canonical/model": {"input": 2}},
            "reference_profile": "editorial-flow",
            "reference_template_landscape": False,
        })


if __name__ == "__main__":
    unittest.main()
