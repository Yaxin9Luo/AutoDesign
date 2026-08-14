#!/usr/bin/env python3
"""Smoke test for the OpenResearch GUI submitter process contract."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from autodesign.agents.openresearch_gui_submitter import (
    AGENT_PROMPT_FILE,
    DONE_FILE,
    PROCESS_FILE,
    REQUEST_FILE,
    submit_openresearch_gui,
)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="autodesign-openresearch-gui-") as tmp:
        root = Path(tmp)
        fake = root / "fake_submitter.py"
        fake.write_text(
            "\n".join(
                [
                    "import json, pathlib, sys",
                    "cwd = pathlib.Path.cwd()",
                    "submitter_prompt = sys.stdin.read()",
                    "assert 'Do not reproduce the paper yourself' in submitter_prompt",
                    "request = json.loads((cwd / 'openresearch_gui_submit_request.json').read_text())",
                    "agent_prompt = (cwd / request['agent_prompt_file']).read_text()",
                    "assert 'Reproduce the code and experiments for this paper' in agent_prompt",
                    "assert request['submitter_contract']['create_project_if_needed'] is True",
                    "(cwd / 'prompt_seen.md').write_text(agent_prompt)",
                    "done = {",
                    "  'status': 'submitted',",
                    "  'project_url': 'https://openresearch.sh/orgs/org_fake/projects/proj_fake',",
                    "  'session_url': 'https://openresearch.sh/orgs/org_fake/projects/proj_fake?session=fake',",
                    "  'observed_text': 'message sent',",
                    "  'screenshot_path': None,",
                    "}",
                    "(cwd / request['done_file']).write_text(json.dumps(done))",
                ]
            ),
            encoding="utf-8",
        )

        job_dir = root / "job"
        settings = SimpleNamespace(
            openresearch_submitter_mode="custom",
            openresearch_submitter_cmd=f"{sys.executable} {fake}",
            openresearch_submitter_timeout_s=10,
        )
        result = submit_openresearch_gui(
            settings=settings,
            job_dir=job_dir,
            project_url="https://openresearch.sh/orgs/org_fake/projects",
            agent_prompt=(
                "Reproduce the code and experiments for this paper, then write a formal reproduction report.\n"
                "Paper: 1706.03762."
            ),
            project_id="proj_fake",
            org_id="org_fake",
            paper_id="1706.03762",
            repo_full_name="Yaxin9Luo/annotated-transformer",
            source_run_id="run_fake",
            artifact_id="art_run_fake",
        )
        assert result.status == "submitted", result.to_dict()
        assert result.project_url == "https://openresearch.sh/orgs/org_fake/projects/proj_fake"
        assert result.session_url and result.session_url.endswith("?session=fake")
        assert (job_dir / AGENT_PROMPT_FILE).exists()
        assert (job_dir / REQUEST_FILE).exists()
        assert (job_dir / DONE_FILE).exists()
        assert (job_dir / PROCESS_FILE).exists()
        request = json.loads((job_dir / REQUEST_FILE).read_text(encoding="utf-8"))
        assert request["submitter_contract"]["do_not_reproduce_locally"] is True
        assert request["paper_id"] == "1706.03762"
        assert "Paper: 1706.03762" in (job_dir / "prompt_seen.md").read_text(encoding="utf-8")

        disabled = submit_openresearch_gui(
            settings=SimpleNamespace(openresearch_submitter_mode="off", openresearch_submitter_cmd=""),
            job_dir=root / "disabled",
            project_url="https://openresearch.sh/orgs/org_fake/projects",
            agent_prompt="Reproduce the code and experiments for this paper, then write a formal reproduction report.",
            project_id="proj_fake",
            org_id="org_fake",
            paper_id="1706.03762",
            repo_full_name=None,
            source_run_id="run_fake",
            artifact_id="art_run_fake",
        )
        assert disabled.status == "disabled", disabled.to_dict()
        assert (root / "disabled" / AGENT_PROMPT_FILE).exists()

        missing = submit_openresearch_gui(
            settings=SimpleNamespace(openresearch_submitter_mode="custom", openresearch_submitter_cmd=""),
            job_dir=root / "missing",
            project_url="https://openresearch.sh/orgs/org_fake/projects",
            agent_prompt="Reproduce the code and experiments for this paper, then write a formal reproduction report.",
            project_id="proj_fake",
            org_id="org_fake",
            paper_id="1706.03762",
            repo_full_name=None,
            source_run_id="run_fake",
            artifact_id="art_run_fake",
        )
        assert missing.status == "not_configured", missing.to_dict()

    print("openresearch fake GUI submitter smoke passed")


if __name__ == "__main__":
    main()
