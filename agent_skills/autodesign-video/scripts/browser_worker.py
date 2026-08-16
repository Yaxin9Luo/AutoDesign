#!/usr/bin/env python3
"""Network-denied Playwright worker for portable AutoDesign Agent Skills."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname


REPORT_FORMAT_VERSION = 1
_EXPECTED_PYTHON_PACKAGES = (
    "greenlet",
    "playwright",
    "pyee",
    "typing-extensions",
)
_INLINE_SCHEMES = {"about", "blob", "data"}
_NETWORK_SCHEMES = {"http", "https", "ws", "wss"}
_VIEWPORT_PATTERN = re.compile(
    r"^(?P<label>[A-Za-z][A-Za-z0-9_-]{0,31}):(?P<width>[1-9][0-9]{1,4})x(?P<height>[1-9][0-9]{1,4})$"
)


class BrowserAuditError(RuntimeError):
    """The requested browser audit cannot be performed safely."""


@dataclass(frozen=True)
class RequestDecision:
    allowed: bool
    missing: bool
    reason: str
    sanitized_url: str


@dataclass(frozen=True)
class AuditPaths:
    html: Path
    workspace_root: Path
    output_dir: Path


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _installed_package_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _redact_text(value: str) -> str:
    text = value[:4000]
    text = re.sub(r"(?i)(authorization|cookie|set-cookie)\s*:\s*[^\r\n]+", r"\1: [REDACTED]", text)
    text = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~-]{8,}", "Bearer [REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", text)
    return text


def _sanitize_url(url: str, workspace_root: Path) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "[invalid-url]"
    scheme = parsed.scheme.lower()
    if scheme == "file":
        if parsed.netloc:
            return "file:///[non-local-host]"
        try:
            path = Path(url2pathname(unquote(parsed.path))).resolve(strict=False)
            workspace = workspace_root.resolve(strict=True)
        except (OSError, ValueError):
            return "file:///[invalid-path]"
        if _is_within(path, workspace):
            relative = path.relative_to(workspace).as_posix()
            return f"file:///[workspace]/{relative}"
        return "file:///[outside-workspace]"
    if scheme in _NETWORK_SCHEMES:
        try:
            host = parsed.hostname or "[unknown-host]"
            port = f":{parsed.port}" if parsed.port else ""
        except ValueError:
            return f"{scheme}://[invalid-host]"
        return f"{scheme}://{host}{port}"
    if scheme in _INLINE_SCHEMES:
        return f"{scheme}:[inline]"
    return f"{scheme or '[none]'}:[blocked]"


def classify_request(url: str, workspace_root: Path) -> RequestDecision:
    """Apply the browser's fail-closed local-resource policy."""

    workspace = workspace_root.resolve(strict=True)
    try:
        parsed = urlsplit(url)
    except ValueError:
        return RequestDecision(False, False, "invalid_url", "[invalid-url]")
    scheme = parsed.scheme.lower()
    sanitized = _sanitize_url(url, workspace)
    if scheme in _INLINE_SCHEMES:
        return RequestDecision(True, False, "safe_inline_scheme", sanitized)
    if scheme != "file":
        reason = "network_blocked" if scheme in _NETWORK_SCHEMES else "scheme_blocked"
        return RequestDecision(False, False, reason, sanitized)
    if parsed.netloc:
        return RequestDecision(False, False, "non_local_file_host", sanitized)
    try:
        candidate = Path(url2pathname(unquote(parsed.path))).resolve(strict=False)
    except (OSError, ValueError):
        return RequestDecision(False, False, "invalid_file_path", sanitized)
    if not _is_within(candidate, workspace):
        return RequestDecision(False, False, "file_outside_workspace", sanitized)
    if not candidate.is_file():
        return RequestDecision(False, True, "missing_local_asset", sanitized)
    return RequestDecision(True, False, "local_workspace_file", sanitized)


def _reject_symlink_components(path: Path, root: Path) -> None:
    relative = path.relative_to(root)
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise BrowserAuditError(f"Audit output path contains a symlink: {relative}")
        if not cursor.exists():
            break


def resolve_audit_paths(
    html_path: Path, workspace_root: Path, output_dir: Path
) -> AuditPaths:
    package_root = _installed_package_root()
    try:
        workspace = workspace_root.expanduser().resolve(strict=True)
    except OSError as error:
        raise BrowserAuditError(f"Workspace root does not exist: {workspace_root}") from error
    if not workspace.is_dir():
        raise BrowserAuditError("Workspace root must be a directory")
    if _is_within(workspace, package_root):
        raise BrowserAuditError("Audit workspace must be outside the installed Skill")
    try:
        html = html_path.expanduser().resolve(strict=True)
    except OSError as error:
        raise BrowserAuditError(f"Local HTML does not exist: {html_path}") from error
    if not html.is_file() or not _is_within(html, workspace):
        raise BrowserAuditError("Local HTML must be a file inside the workspace root")

    output = output_dir.expanduser().resolve(strict=False)
    if not _is_within(output, workspace):
        raise BrowserAuditError("Audit output directory must be inside the workspace root")
    if _is_within(output, package_root):
        raise BrowserAuditError("Audit output must be outside the installed Skill")
    _reject_symlink_components(output, workspace)
    if output.exists() and not output.is_dir():
        raise BrowserAuditError("Audit output path must be a directory")
    output.mkdir(parents=True, exist_ok=True)
    return AuditPaths(html, workspace, output)


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _parse_viewport(value: str) -> tuple[str, int, int]:
    match = _VIEWPORT_PATTERN.fullmatch(value)
    if match is None:
        raise BrowserAuditError(
            f"Invalid viewport {value!r}; expected label:WIDTHxHEIGHT"
        )
    width = int(match.group("width"))
    height = int(match.group("height"))
    if width > 10000 or height > 10000:
        raise BrowserAuditError("Viewport dimensions must not exceed 10000 pixels")
    return match.group("label"), width, height


def finalize_observation(observation: Mapping[str, object]) -> dict[str, object]:
    """Attach deterministic gates to one rendered viewport observation."""

    result = dict(observation)
    geometry = result.get("geometry") if isinstance(result.get("geometry"), dict) else {}
    checks = {
        "no_blocked_network": not bool(result.get("blocked_requests")),
        "local_assets_complete": not bool(result.get("missing_local_assets")),
        "render_not_blank": result.get("blank_render") is False,
        "no_horizontal_overflow": int(geometry.get("horizontal_overflow", 0)) <= 0,
        "no_page_errors": not bool(result.get("page_errors")),
    }
    result["checks"] = checks
    result["passed"] = all(checks.values())
    return result


_GEOMETRY_SCRIPT = r"""
() => {
  const root = document.documentElement;
  const body = document.body;
  const viewportWidth = window.innerWidth;
  const viewportHeight = window.innerHeight;
  const scrollWidth = Math.max(root ? root.scrollWidth : 0, body ? body.scrollWidth : 0);
  const scrollHeight = Math.max(root ? root.scrollHeight : 0, body ? body.scrollHeight : 0);
  const candidates = Array.from(document.querySelectorAll('body *'));
  const visible = candidates.filter((element) => {
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' &&
      style.visibility !== 'hidden' && Number.parseFloat(style.opacity || '1') > 0;
  });
  const outOfCanvas = visible.map((element) => {
    const rect = element.getBoundingClientRect();
    if (rect.left >= -1 && rect.right <= viewportWidth + 1 && rect.top >= -1) return null;
    const id = element.id ? `#${element.id}` : '';
    const classes = Array.from(element.classList || []).slice(0, 2).map((name) => `.${name}`).join('');
    return {
      selector: `${element.tagName.toLowerCase()}${id}${classes}`.slice(0, 160),
      left: Math.round(rect.left * 100) / 100,
      right: Math.round(rect.right * 100) / 100,
      top: Math.round(rect.top * 100) / 100,
      bottom: Math.round(rect.bottom * 100) / 100,
      width: Math.round(rect.width * 100) / 100,
      height: Math.round(rect.height * 100) / 100,
    };
  }).filter(Boolean).slice(0, 100);
  const textVisible = Boolean(body && body.innerText && body.innerText.trim());
  const mediaVisible = visible.some((element) => ['IMG', 'SVG', 'CANVAS', 'VIDEO'].includes(element.tagName));
  return {
    blank_render: !(textVisible || mediaVisible),
    geometry: {
      viewport_width: viewportWidth,
      viewport_height: viewportHeight,
      scroll_width: scrollWidth,
      scroll_height: scrollHeight,
      horizontal_overflow: Math.max(0, scrollWidth - viewportWidth),
      vertical_overflow: Math.max(0, scrollHeight - viewportHeight),
      out_of_canvas: outOfCanvas,
    },
  };
}
"""


def _dedupe_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
    seen: set[str] = set()
    result: list[dict[str, object]] = []
    for record in records:
        key = json.dumps(record, sort_keys=True, ensure_ascii=True)
        if key in seen:
            continue
        seen.add(key)
        result.append(record)
    return result


def _audit_viewport(
    browser: object,
    *,
    paths: AuditPaths,
    label: str,
    width: int,
    height: int,
) -> dict[str, object]:
    context = browser.new_context(
        viewport={"width": width, "height": height},
        service_workers="block",
    )
    page = context.new_page()
    blocked: list[dict[str, object]] = []
    missing: list[dict[str, object]] = []
    console_errors: list[dict[str, object]] = []
    page_errors: list[dict[str, object]] = []
    request_errors: list[dict[str, object]] = []

    def route_request(route: object) -> None:
        decision = classify_request(route.request.url, paths.workspace_root)
        if decision.allowed:
            route.continue_()
            return
        record = {
            "url": decision.sanitized_url,
            "reason": decision.reason,
            "resource_type": str(route.request.resource_type),
        }
        if decision.missing:
            missing.append(record)
        else:
            blocked.append(record)
        route.abort("blockedbyclient")

    def console_message(message: object) -> None:
        if str(message.type).lower() == "error":
            console_errors.append({"type": "error", "text": _redact_text(str(message.text))})

    def page_error(error: object) -> None:
        page_errors.append({"message": _redact_text(str(error))})

    def request_failed(request: object) -> None:
        failure = request.failure
        failure_text = failure if isinstance(failure, str) else str(failure or "request failed")
        request_errors.append(
            {
                "url": _sanitize_url(request.url, paths.workspace_root),
                "error": _redact_text(failure_text),
            }
        )

    def block_websocket(socket: object) -> None:
        blocked.append(
            {
                "url": _sanitize_url(socket.url, paths.workspace_root),
                "reason": "websocket_blocked",
                "resource_type": "websocket",
            }
        )
        # A routed socket does not connect unless connect_to_server() is called.
        # Leaving it as a local mock also avoids re-entrant close deadlocks.

    page.route("**/*", route_request)
    page.route_web_socket("**/*", block_websocket)
    page.on("console", console_message)
    page.on("pageerror", page_error)
    page.on("requestfailed", request_failed)
    try:
        try:
            page.goto(paths.html.as_uri(), wait_until="load", timeout=30000)
            page.wait_for_timeout(250)
        except Exception as error:  # Playwright error types live only in the pinned venv.
            page_errors.append({"message": _redact_text(str(error))})
        evaluated = page.evaluate(_GEOMETRY_SCRIPT)
        screenshot = _safe_output_file(paths.output_dir / f"{label}.png", paths.output_dir)
        page.screenshot(path=str(screenshot), full_page=True, animations="disabled")
        observation = {
            "label": label,
            "viewport": {"width": width, "height": height},
            "blocked_requests": _dedupe_records(blocked),
            "missing_local_assets": _dedupe_records(missing),
            "console_errors": _dedupe_records(console_errors),
            "page_errors": _dedupe_records(page_errors),
            "request_errors": _dedupe_records(request_errors),
            "blank_render": bool(evaluated.get("blank_render", True)),
            "geometry": evaluated.get("geometry", {}),
            "screenshot": screenshot.relative_to(paths.output_dir).as_posix(),
        }
        return finalize_observation(observation)
    finally:
        context.close()


def audit_html(
    paths: AuditPaths, viewports: Sequence[tuple[str, int, int]]
) -> dict[str, object]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise BrowserAuditError("Pinned Playwright runtime is not installed") from error

    observations: dict[str, object] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for label, width, height in viewports:
                observations[label] = _audit_viewport(
                    browser,
                    paths=paths,
                    label=label,
                    width=width,
                    height=height,
                )
        finally:
            browser.close()
    blocked: list[dict[str, object]] = []
    missing: list[dict[str, object]] = []
    for observation in observations.values():
        if isinstance(observation, dict):
            blocked.extend(observation.get("blocked_requests", []))
            missing.extend(observation.get("missing_local_assets", []))
    return {
        "format_version": REPORT_FORMAT_VERSION,
        "html": paths.html.relative_to(paths.workspace_root).as_posix(),
        "viewports": observations,
        "blocked_requests": _dedupe_records(blocked),
        "missing_local_assets": _dedupe_records(missing),
        "passed": bool(observations) and all(
            isinstance(item, dict) and item.get("passed") is True
            for item in observations.values()
        ),
    }


def probe_browser(report_path: Path) -> dict[str, object]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise BrowserAuditError("Pinned Playwright runtime is not installed") from error
    with sync_playwright() as playwright:
        executable = Path(playwright.chromium.executable_path).resolve(strict=True)
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 320, "height": 200})
            page.set_content("<!doctype html><title>probe</title><p>ready</p>")
            passed = page.locator("p").inner_text() == "ready"
            page.close()
        finally:
            browser.close()
    payload = {
        "format_version": REPORT_FORMAT_VERSION,
        "passed": passed,
        "browser_executable": str(executable),
        "browser_executable_sha256": _file_sha256(executable),
        "python_packages": {
            name: importlib.metadata.version(name) for name in _EXPECTED_PYTHON_PACKAGES
        },
    }
    _atomic_write_json(report_path, payload)
    return payload


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_report_path(report: Path, output: Path) -> Path:
    return _safe_output_file(report, output)


def _safe_output_file(path: Path, output: Path) -> Path:
    candidate = path.expanduser().resolve(strict=False)
    if not _is_within(candidate, output) or candidate.parent != output:
        raise BrowserAuditError("Audit output file must be a direct child of the output directory")
    if candidate.is_symlink():
        raise BrowserAuditError("Audit output file must not be a symlink")
    return candidate


def _safe_probe_report_path(report: Path) -> Path:
    candidate = report.expanduser().resolve(strict=False)
    if _is_within(candidate, _installed_package_root()) or candidate.is_symlink():
        raise BrowserAuditError("Browser probe report must be outside the installed Skill")
    if not candidate.parent.is_dir():
        raise BrowserAuditError("Browser probe report parent directory does not exist")
    return candidate


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    probe = subparsers.add_parser("probe")
    probe.add_argument("--report", type=Path, required=True)
    audit = subparsers.add_parser("audit")
    audit.add_argument("--workspace-root", type=Path, required=True)
    audit.add_argument("--html", type=Path, required=True)
    audit.add_argument("--output-dir", type=Path, required=True)
    audit.add_argument("--report", type=Path, required=True)
    audit.add_argument("--viewport", action="append", default=[])
    args = parser.parse_args(argv)

    if args.command == "probe":
        try:
            probe_browser(_safe_probe_report_path(args.report))
        except (BrowserAuditError, OSError, ValueError) as error:
            print(f"ERROR: {_redact_text(str(error))}", file=sys.stderr)
            return 1
        return 0

    try:
        paths = resolve_audit_paths(args.html, args.workspace_root, args.output_dir)
        report = _safe_report_path(args.report, paths.output_dir)
        requested = args.viewport or ["desktop:1440x900"]
        viewports = [_parse_viewport(value) for value in requested]
        labels = [viewport[0] for viewport in viewports]
        if len(set(labels)) != len(labels):
            raise BrowserAuditError("Viewport labels must be unique")
        try:
            payload = audit_html(paths, viewports)
        except Exception as error:  # Keep a machine-readable failure for runtime/browser errors.
            payload = {
                "format_version": REPORT_FORMAT_VERSION,
                "html": paths.html.relative_to(paths.workspace_root).as_posix(),
                "viewports": {},
                "blocked_requests": [],
                "missing_local_assets": [],
                "passed": False,
                "runtime_error": _redact_text(str(error)),
            }
        _atomic_write_json(report, payload)
    except (BrowserAuditError, OSError, ValueError) as error:
        print(f"ERROR: {_redact_text(str(error))}", file=sys.stderr)
        return 1
    return 0 if payload.get("passed") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
