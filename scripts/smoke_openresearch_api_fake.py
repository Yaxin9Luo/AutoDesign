#!/usr/bin/env python3
"""Smoke test for read-only OpenResearch HTTP API adapter calls."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from autodesign.util.openresearch_api import OpenResearchApiClient


ORG_ID = "org_fake"
PROJECT_ID = "proj_fake"
REPORT_ID = "report_fake"
PAPER_ID = "1706.03762"


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="autodesign-openresearch-api-") as tmp:
        root = Path(tmp)
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenResearchHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            api_url = f"http://127.0.0.1:{server.server_port}"
            os.environ["AUTODESIGN_OPENRESEARCH_TOKEN"] = "fake-token"
            client = OpenResearchApiClient(
                api_url=api_url,
                timeout_s=5,
                request_log_path=root / "openresearch_api.jsonl",
                credentials_path=root / "missing-credentials.json",
            )

            project = client.get_project(PROJECT_ID)
            assert project.ok, project.to_dict()
            assert project.data["repoFullName"] == "Yaxin9Luo/annotated-transformer"
            assert project.data["paperId"] == PAPER_ID

            reports = client.list_reports(PROJECT_ID)
            assert reports.ok, reports.to_dict()
            assert reports.data[0]["id"] == REPORT_ID

            report = client.get_report(PROJECT_ID, REPORT_ID)
            assert report.ok, report.to_dict()
            assert "Formal Reproduction Report" in report.data["markdown"]

            log_text = (root / "openresearch_api.jsonl").read_text(encoding="utf-8")
            assert "fake-token" not in log_text
            records = [json.loads(line) for line in log_text.strip().splitlines()]
            assert records[0]["path"] == f"/projects/{PROJECT_ID}"
            assert records[1]["path"] == f"/projects/{PROJECT_ID}/reports"

            os.environ.pop("AUTODESIGN_OPENRESEARCH_TOKEN", None)
            missing = OpenResearchApiClient(
                api_url=api_url,
                timeout_s=5,
                request_log_path=root / "missing_token.jsonl",
                credentials_path=root / "missing-credentials.json",
            ).get_project(PROJECT_ID)
            assert not missing.ok
            assert "missing OpenResearch token" in (missing.error or "")
        finally:
            server.shutdown()
            server.server_close()

    print("openresearch fake HTTP API smoke passed")


class _FakeOpenResearchHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    project: dict[str, Any] = {
        "id": PROJECT_ID,
        "name": "Fake OpenResearch project",
        "repoFullName": "Yaxin9Luo/annotated-transformer",
        "paperId": PAPER_ID,
        "isPublic": False,
    }

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        if self.path == f"/projects/{PROJECT_ID}":
            self._json(type(self).project)
            return
        if self.path == f"/projects/{PROJECT_ID}/reports":
            self._json([{"id": REPORT_ID, "title": "Formal Reproduction Report", "createdAt": "2026-06-22"}])
            return
        if self.path == f"/projects/{PROJECT_ID}/reports/{REPORT_ID}":
            self._json({"id": REPORT_ID, "markdown": "# Formal Reproduction Report\n\npass: true\n"})
            return
        self._json({"detail": f"not found: {self.path}"}, status=404)

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") != "Bearer fake-token":
            self._json({"detail": "unauthorized"}, status=401)
            return False
        return True

    def _json(self, payload: Any, *, status: int = 200) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


if __name__ == "__main__":
    main()
