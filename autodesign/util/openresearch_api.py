"""Small HTTP adapter for the OpenResearch project API."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .remote_url_policy import validate_remote_http_url


DEFAULT_OPENRESEARCH_API_URL = "https://api.openresearch.sh"
DEFAULT_CREDENTIALS_PATH = Path.home() / ".config" / "openresearch" / "credentials.json"


def _env_first(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


@dataclass
class OpenResearchApiResult:
    method: str
    path: str
    status_code: int | None
    elapsed_s: float
    request_body: dict[str, Any] | None = None
    response_body: Any = None
    error: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None and self.status_code is not None and 200 <= self.status_code < 300

    @property
    def data(self) -> Any:
        return self.response_body

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "path": self.path,
            "status_code": self.status_code,
            "elapsed_s": self.elapsed_s,
            "request_body": self.request_body,
            "response_body": self.response_body,
            "error": self.error,
            "ok": self.ok,
        }


class OpenResearchApiClient:
    def __init__(
        self,
        *,
        api_url: str | None = None,
        token: str | None = None,
        timeout_s: int = 120,
        request_log_path: Path | None = None,
        credentials_path: Path | None = None,
        allow_private_network: bool = True,
    ) -> None:
        credentials = _read_credentials(credentials_path or DEFAULT_CREDENTIALS_PATH)
        configured_api_url = (
            (api_url or "").strip()
            or _env_first(
                "AUTODESIGN_OPENRESEARCH_API_URL",
                "DESIGN_ANYTHING_OPENRESEARCH_API_URL",
                "OPEN_DESIGN_OPENRESEARCH_API_URL",
                "OPENRESEARCH_API_URL",
            )
            or str(credentials.get("apiUrl") or "").strip()
            or DEFAULT_OPENRESEARCH_API_URL
        ).rstrip("/")
        self.api_url = validate_remote_http_url(
            configured_api_url,
            allow_private_network=allow_private_network,
            require_https=not allow_private_network,
        )
        self.allow_private_network = allow_private_network
        self.token = (
            token
            or _env_first(
                "AUTODESIGN_OPENRESEARCH_TOKEN",
                "DESIGN_ANYTHING_OPENRESEARCH_TOKEN",
                "OPEN_DESIGN_OPENRESEARCH_TOKEN",
            )
            or str(credentials.get("token") or "").strip()
        )
        self.timeout_s = max(1, int(timeout_s or 120))
        self.request_log_path = request_log_path

    def get_project(self, project_id: str) -> OpenResearchApiResult:
        return self.request_json("GET", f"/projects/{project_id}")

    def list_reports(self, project_id: str) -> OpenResearchApiResult:
        return self.request_json("GET", f"/projects/{project_id}/reports")

    def get_report(self, project_id: str, report_id: str) -> OpenResearchApiResult:
        return self.request_json("GET", f"/projects/{project_id}/reports/{report_id}")

    def request_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> OpenResearchApiResult:
        method = method.upper()
        started = time.monotonic()
        if not self.token:
            result = OpenResearchApiResult(
                method=method,
                path=path,
                status_code=None,
                elapsed_s=0.0,
                request_body=body,
                error=(
                    "missing OpenResearch token: run `orx login` or set "
                    "AUTODESIGN_OPENRESEARCH_TOKEN"
                ),
            )
            self._append_log(result)
            return result

        url = urllib.parse.urljoin(self.api_url + "/", path.lstrip("/"))
        data = None
        headers = {
            "Accept": "application/json, text/markdown, text/plain",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "AutoDesign-OpenResearch-Adapter/0.1",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            opener = urllib.request.build_opener(
                _RejectRedirects() if not self.allow_private_network else urllib.request.HTTPRedirectHandler()
            )
            with opener.open(request, timeout=self.timeout_s) as response:
                raw = response.read()
                parsed = _parse_response_body(raw, response.headers.get("content-type", ""))
                result = OpenResearchApiResult(
                    method=method,
                    path=path,
                    status_code=response.status,
                    elapsed_s=time.monotonic() - started,
                    request_body=body,
                    response_body=parsed,
                    headers=dict(response.headers.items()),
                )
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            parsed = _parse_response_body(raw, exc.headers.get("content-type", ""))
            result = OpenResearchApiResult(
                method=method,
                path=path,
                status_code=exc.code,
                elapsed_s=time.monotonic() - started,
                request_body=body,
                response_body=parsed,
                error=_error_message(parsed) or str(exc),
                headers=dict(exc.headers.items()),
            )
        except (OSError, TimeoutError) as exc:
            result = OpenResearchApiResult(
                method=method,
                path=path,
                status_code=None,
                elapsed_s=time.monotonic() - started,
                request_body=body,
                error=f"{type(exc).__name__}: {exc}",
            )
        self._append_log(result)
        return result

    def _append_log(self, result: OpenResearchApiResult) -> None:
        if self.request_log_path is None:
            return
        self.request_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.request_log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        del req, fp, code, msg, headers, newurl
        return None


def _read_credentials(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_response_body(raw: bytes, content_type: str) -> Any:
    text = raw.decode("utf-8", errors="replace")
    if "json" in content_type.lower():
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _error_message(parsed: Any) -> str | None:
    if isinstance(parsed, dict):
        detail = parsed.get("detail") or parsed.get("message") or parsed.get("error")
        if isinstance(detail, str):
            return detail
        if detail is not None:
            return json.dumps(detail, ensure_ascii=False)
    if isinstance(parsed, str) and parsed.strip():
        return parsed.strip()[:1000]
    return None
