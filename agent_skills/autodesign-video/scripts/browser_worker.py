#!/usr/bin/env python3
"""Network-denied Playwright worker for portable AutoDesign Agent Skills."""

from __future__ import annotations

import argparse
import collections
import hashlib
import importlib.metadata
import json
import math
import os
import re
import struct
import sys
import tempfile
import uuid
import zlib
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
_DIAGNOSTIC_URL_PATTERN = re.compile(
    r"(?i)(?:https?|wss?|file)://[^\s<>'\"()\[\]{}]+"
)
_VIEWPORT_PATTERN = re.compile(
    r"^(?P<label>[A-Za-z][A-Za-z0-9_-]{0,31}):(?P<width>[1-9][0-9]{1,4})x(?P<height>[1-9][0-9]{1,4})$"
)
_BROWSER_NETWORK_REDUCTION_ARGS = (
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-features=WebTransport",
    "--disable-quic",
    "--disable-sync",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--host-resolver-rules=MAP * ~NOTFOUND",
    "--metrics-recording-only",
    "--no-pings",
    "--no-proxy-server",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
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


def _sanitize_diagnostic_text(value: str, workspace_root: Path) -> str:
    text = _redact_text(value)

    def replace_url(match: re.Match[str]) -> str:
        return _sanitize_url(match.group(0), workspace_root)

    text = _DIAGNOSTIC_URL_PATTERN.sub(replace_url, text)
    workspace_input = workspace_root.expanduser().absolute()
    workspace = workspace_input.resolve(strict=True)
    roots: list[tuple[tuple[Path, ...], str]] = [
        ((workspace_input, workspace), "[workspace]")
    ]
    try:
        home_input = Path.home().expanduser().absolute()
        home = home_input.resolve()
    except (OSError, RuntimeError):
        pass
    else:
        roots.append(((home_input, home), "[home]"))
    for root_variants, replacement in roots:
        raw_variants: set[str] = set()
        for root in root_variants:
            raw_variants.update((str(root), root.as_posix()))
        for raw in sorted(raw_variants, key=len, reverse=True):
            if raw:
                text = text.replace(raw, replacement)
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


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
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


def _paeth(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    left_distance = abs(estimate - left)
    above_distance = abs(estimate - above)
    upper_left_distance = abs(estimate - upper_left)
    if left_distance <= above_distance and left_distance <= upper_left_distance:
        return left
    if above_distance <= upper_left_distance:
        return above
    return upper_left


def _decode_png_rows(payload: bytes) -> tuple[int, int, int, list[bytes]]:
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("screenshot is not a PNG")
    position = 8
    header: tuple[int, int, int, int] | None = None
    compressed = bytearray()
    while position + 12 <= len(payload):
        length = struct.unpack(">I", payload[position : position + 4])[0]
        kind = payload[position + 4 : position + 8]
        start = position + 8
        end = start + length
        if end + 4 > len(payload):
            raise ValueError("PNG chunk is truncated")
        chunk = payload[start:end]
        expected_crc = struct.unpack(">I", payload[end : end + 4])[0]
        if zlib.crc32(kind + chunk) & 0xFFFFFFFF != expected_crc:
            raise ValueError("PNG chunk checksum is invalid")
        if kind == b"IHDR":
            if len(chunk) != 13:
                raise ValueError("PNG header is invalid")
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", chunk
            )
            if (
                width <= 0
                or height <= 0
                or bit_depth != 8
                or color_type not in {0, 2, 4, 6}
                or compression != 0
                or filtering != 0
                or interlace != 0
            ):
                raise ValueError("PNG screenshot format is unsupported")
            channels = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
            header = (width, height, color_type, channels)
        elif kind == b"IDAT":
            compressed.extend(chunk)
        elif kind == b"IEND":
            break
        position = end + 4
    if header is None or not compressed:
        raise ValueError("PNG screenshot is incomplete")
    width, height, color_type, channels = header
    row_length = width * channels
    decoded = zlib.decompress(bytes(compressed))
    if len(decoded) != height * (row_length + 1):
        raise ValueError("PNG screenshot pixel payload has an unexpected size")
    rows: list[bytes] = []
    previous = bytearray(row_length)
    offset = 0
    for _ in range(height):
        filter_type = decoded[offset]
        source = decoded[offset + 1 : offset + 1 + row_length]
        offset += row_length + 1
        current = bytearray(row_length)
        for index, value in enumerate(source):
            left = current[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 0:
                reconstructed = value
            elif filter_type == 1:
                reconstructed = value + left
            elif filter_type == 2:
                reconstructed = value + above
            elif filter_type == 3:
                reconstructed = value + ((left + above) // 2)
            elif filter_type == 4:
                reconstructed = value + _paeth(left, above, upper_left)
            else:
                raise ValueError("PNG screenshot uses an unknown filter")
            current[index] = reconstructed & 0xFF
        rows.append(bytes(current))
        previous = current
    return width, height, color_type, rows


def _analyze_png_paint(payload: bytes) -> dict[str, object]:
    try:
        width, height, color_type, rows = _decode_png_rows(payload)
    except (OSError, ValueError, zlib.error) as error:
        return {
            "valid_png": False,
            "painted_content": False,
            "near_uniform": True,
            "error": _redact_text(str(error)),
        }

    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    pixel_count = width * height
    stride = max(1, math.ceil(math.sqrt(pixel_count / 200_000)))
    colors: collections.Counter[tuple[int, int, int]] = collections.Counter()
    channel_min = 255
    channel_max = 0
    luminance_sum = 0.0
    luminance_squared_sum = 0.0
    sampled = 0
    for row_index in range(0, height, stride):
        row = rows[row_index]
        for column in range(0, width, stride):
            offset = column * channels
            if color_type == 0:
                red = green = blue = row[offset]
                alpha = 255
            elif color_type == 2:
                red, green, blue = row[offset : offset + 3]
                alpha = 255
            elif color_type == 4:
                red = green = blue = row[offset]
                alpha = row[offset + 1]
            else:
                red, green, blue, alpha = row[offset : offset + 4]
            if alpha < 255:
                red = (red * alpha + 255 * (255 - alpha)) // 255
                green = (green * alpha + 255 * (255 - alpha)) // 255
                blue = (blue * alpha + 255 * (255 - alpha)) // 255
            colors[(red // 8, green // 8, blue // 8)] += 1
            channel_min = min(channel_min, red, green, blue)
            channel_max = max(channel_max, red, green, blue)
            luminance = (0.2126 * red) + (0.7152 * green) + (0.0722 * blue)
            luminance_sum += luminance
            luminance_squared_sum += luminance * luminance
            sampled += 1
    dominant_ratio = max(colors.values(), default=sampled) / max(1, sampled)
    mean = luminance_sum / max(1, sampled)
    variance = max(0.0, (luminance_squared_sum / max(1, sampled)) - (mean * mean))
    channel_span = channel_max - channel_min
    near_uniform = dominant_ratio >= 0.999 or channel_span < 8 or variance < 1.0
    return {
        "valid_png": True,
        "width": width,
        "height": height,
        "sampled_pixels": sampled,
        "dominant_color_ratio": round(dominant_ratio, 6),
        "channel_span": channel_span,
        "luminance_variance": round(variance, 4),
        "near_uniform": near_uniform,
        "painted_content": not near_uniform,
    }


def finalize_observation(observation: Mapping[str, object]) -> dict[str, object]:
    """Attach deterministic gates to one rendered viewport observation."""

    result = dict(observation)
    geometry = result.get("geometry") if isinstance(result.get("geometry"), dict) else {}
    checks = {
        "no_blocked_network": not bool(result.get("blocked_requests")),
        "no_direct_network_apis": not bool(result.get("direct_network_attempts")),
        "local_assets_complete": not bool(result.get("missing_local_assets")),
        "media_complete": not bool(result.get("media_errors")),
        "render_not_blank": result.get("blank_render") is False,
        "screenshot_has_painted_content": (
            isinstance(result.get("screenshot_analysis"), dict)
            and result["screenshot_analysis"].get("painted_content") is True
        ),
        "no_horizontal_overflow": int(geometry.get("horizontal_overflow", 0)) <= 0,
        "no_out_of_canvas": not bool(geometry.get("out_of_canvas")),
        "no_clipped_content": not bool(geometry.get("clipped_content")),
        "geometry_inspection_complete": geometry.get("inspection_truncated") is False,
        "no_console_errors": not bool(result.get("console_errors")),
        "no_page_errors": not bool(result.get("page_errors")),
        "no_request_errors": not bool(result.get("request_errors")),
        "dom_state_stable": result.get("dom_state_stable") is True,
    }
    result["checks"] = checks
    result["passed"] = all(checks.values())
    return result


_CONTEXT_INIT_SCRIPT = r"""
(() => {
  const global = globalThis;
  if (global.__autodesignAuditState) return;
  const original = {
    setTimeout: global.setTimeout.bind(global),
    clearTimeout: global.clearTimeout.bind(global),
    setInterval: global.setInterval.bind(global),
    clearInterval: global.clearInterval.bind(global),
    requestAnimationFrame: typeof global.requestAnimationFrame === 'function'
      ? global.requestAnimationFrame.bind(global) : null,
    cancelAnimationFrame: typeof global.cancelAnimationFrame === 'function'
      ? global.cancelAnimationFrame.bind(global) : null,
  };
  const state = {
    frozen: false,
    timeouts: new Set(),
    intervals: new Set(),
    animationFrames: new Set(),
  };
  Object.defineProperty(global, '__autodesignAuditState', {
    value: state, configurable: false, writable: false,
  });
  global.setTimeout = (handler, delay, ...args) => {
    let identifier;
    const wrapped = (...callbackArgs) => {
      state.timeouts.delete(identifier);
      if (!state.frozen && typeof handler === 'function') handler(...callbackArgs);
    };
    identifier = original.setTimeout(wrapped, delay, ...args);
    state.timeouts.add(identifier);
    return identifier;
  };
  global.clearTimeout = (identifier) => {
    state.timeouts.delete(identifier);
    return original.clearTimeout(identifier);
  };
  global.setInterval = (handler, delay, ...args) => {
    const wrapped = (...callbackArgs) => {
      if (!state.frozen && typeof handler === 'function') handler(...callbackArgs);
    };
    const identifier = original.setInterval(wrapped, delay, ...args);
    state.intervals.add(identifier);
    return identifier;
  };
  global.clearInterval = (identifier) => {
    state.intervals.delete(identifier);
    return original.clearInterval(identifier);
  };
  if (original.requestAnimationFrame && original.cancelAnimationFrame) {
    global.requestAnimationFrame = (callback) => {
      let identifier;
      identifier = original.requestAnimationFrame((timestamp) => {
        state.animationFrames.delete(identifier);
        if (!state.frozen) callback(timestamp);
      });
      state.animationFrames.add(identifier);
      return identifier;
    };
    global.cancelAnimationFrame = (identifier) => {
      state.animationFrames.delete(identifier);
      return original.cancelAnimationFrame(identifier);
    };
  }
  const notify = (api, targets) => {
    try {
      void global.__autodesignAuditBlockedNetwork({api, targets: targets.slice(0, 8)});
    } catch (_error) {}
  };
  const installBlockedConstructor = (api, targetExtractor) => {
    const nativeConstructor = global[api];
    if (typeof nativeConstructor !== 'function') return;
    const blocked = function(...args) {
      let targets = [];
      try { targets = targetExtractor(args); } catch (_error) {}
      notify(api, targets.map((target) => String(target)));
      throw new DOMException(`${api} is disabled during local artifact audit`, 'SecurityError');
    };
    try { Object.defineProperty(blocked, 'name', {value: api}); } catch (_error) {}
    blocked.prototype = nativeConstructor.prototype;
    try {
      Object.defineProperty(global, api, {
        value: blocked, configurable: false, writable: false,
      });
    } catch (_error) {
      try { global[api] = blocked; } catch (_ignored) {}
    }
  };
  const rtcTargets = (args) => {
    const config = args[0] || {};
    const targets = [];
    for (const server of (config.iceServers || [])) {
      const urls = server.urls || server.url || [];
      targets.push(...(Array.isArray(urls) ? urls : [urls]));
    }
    return targets;
  };
  installBlockedConstructor('RTCPeerConnection', rtcTargets);
  installBlockedConstructor('webkitRTCPeerConnection', rtcTargets);
  installBlockedConstructor('WebTransport', (args) => args.slice(0, 1));
  installBlockedConstructor('WebSocketStream', (args) => args.slice(0, 1));
  installBlockedConstructor('TCPSocket', (args) => args.slice(0, 1));
  installBlockedConstructor('UDPSocket', (args) => args.slice(0, 1));
  Object.defineProperty(global, '__autodesignFreezeAudit', {
    configurable: false,
    writable: false,
    value: () => {
      if (state.frozen) return;
      state.frozen = true;
      for (const identifier of state.timeouts) original.clearTimeout(identifier);
      for (const identifier of state.intervals) original.clearInterval(identifier);
      if (original.cancelAnimationFrame) {
        for (const identifier of state.animationFrames) original.cancelAnimationFrame(identifier);
      }
      state.timeouts.clear();
      state.intervals.clear();
      state.animationFrames.clear();
      for (const animation of (document.getAnimations ? document.getAnimations() : [])) {
        try { animation.pause(); } catch (_error) {}
      }
      for (const media of document.querySelectorAll('video,audio')) {
        try { media.pause(); } catch (_error) {}
      }
    },
  });
})();
"""


_FREEZE_SCRIPT = r"""
async () => {
  if (document.fonts && document.fonts.ready) {
    try { await document.fonts.ready; } catch (_error) {}
  }
  if (typeof globalThis.__autodesignFreezeAudit === 'function') {
    globalThis.__autodesignFreezeAudit();
  }
  return true;
}
"""


_PREPARE_MEDIA_SCRIPT = r"""
async () => {
  const timeout = (milliseconds) => new Promise((resolve) => {
    const identifier = setTimeout(() => resolve('timeout'), milliseconds);
    void identifier;
  });
  const prepareImage = async (image) => {
    if (image.complete && image.naturalWidth > 0 && image.naturalHeight > 0) return;
    try { image.loading = 'eager'; } catch (_error) {}
    if (typeof image.decode === 'function') {
      try { await Promise.race([image.decode(), timeout(5000)]); } catch (_error) {}
      return;
    }
    await Promise.race([
      new Promise((resolve) => {
        image.addEventListener('load', resolve, {once: true});
        image.addEventListener('error', resolve, {once: true});
      }),
      timeout(5000),
    ]);
  };
  const prepareMedia = async (media) => {
    const hasSource = Boolean(
      media.currentSrc || media.getAttribute('src') || media.querySelector('source[src]')
    );
    if (!hasSource || media.readyState !== HTMLMediaElement.HAVE_NOTHING) return;
    try { media.preload = 'metadata'; } catch (_error) {}
    const readiness = new Promise((resolve) => {
      media.addEventListener('loadedmetadata', resolve, {once: true});
      media.addEventListener('canplay', resolve, {once: true});
      media.addEventListener('error', resolve, {once: true});
    });
    try { media.load(); } catch (_error) {}
    await Promise.race([readiness, timeout(5000)]);
  };
  await Promise.all([
    ...Array.from(document.images || []).map(prepareImage),
    ...Array.from(document.querySelectorAll('video,audio')).map(prepareMedia),
  ]);
  return true;
}
"""


_GEOMETRY_SCRIPT = r"""
() => {
  const root = document.documentElement;
  const body = document.body;
  const viewportWidth = window.innerWidth;
  const viewportHeight = window.innerHeight;
  const scrollWidth = Math.max(root ? root.scrollWidth : 0, body ? body.scrollWidth : 0);
  const scrollHeight = Math.max(root ? root.scrollHeight : 0, body ? body.scrollHeight : 0);
  const round = (value) => Math.round(value * 100) / 100;
  const selectorFor = (element) => {
    if (!element || !element.tagName) return '[text]';
    const id = element.id ? `#${element.id}` : '';
    const classes = Array.from(element.classList || []).slice(0, 2)
      .map((name) => `.${name}`).join('');
    return `${element.tagName.toLowerCase()}${id}${classes}`.slice(0, 160);
  };
  const isVisible = (element, rect) => {
    const style = getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' &&
      style.visibility !== 'hidden' && Number.parseFloat(style.opacity || '1') > 0;
  };
  const maxContentBoxes = 10000;
  const maxInspectedNodes = 50000;
  const contentBoxes = [];
  let inspectedNodes = 0;
  let inspectionTruncated = false;
  if (body) {
    const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      inspectedNodes += 1;
      if (inspectedNodes > maxInspectedNodes || contentBoxes.length >= maxContentBoxes) {
        inspectionTruncated = true;
        break;
      }
      const node = walker.currentNode;
      if (!node.nodeValue || !node.nodeValue.trim() || !node.parentElement) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      for (const rect of range.getClientRects()) {
        if (contentBoxes.length >= maxContentBoxes) {
          inspectionTruncated = true;
          break;
        }
        if (isVisible(node.parentElement, rect)) {
          contentBoxes.push({element: node.parentElement, rect, kind: 'text'});
        }
      }
      if (inspectionTruncated) break;
    }
    if (!inspectionTruncated) {
      for (const element of body.querySelectorAll('img,svg,canvas,video')) {
        inspectedNodes += 1;
        if (inspectedNodes > maxInspectedNodes || contentBoxes.length >= maxContentBoxes) {
          inspectionTruncated = true;
          break;
        }
        const rect = element.getBoundingClientRect();
        if (isVisible(element, rect)) contentBoxes.push({element, rect, kind: 'media'});
      }
    }
  }
  const documentWidth = Math.max(viewportWidth, scrollWidth);
  const documentHeight = Math.max(viewportHeight, scrollHeight);
  const outOfCanvas = contentBoxes.map(({element, rect, kind}) => {
    const left = rect.left + window.scrollX;
    const right = rect.right + window.scrollX;
    const top = rect.top + window.scrollY;
    const bottom = rect.bottom + window.scrollY;
    if (left >= -1 && right <= documentWidth + 1 && top >= -1 && bottom <= documentHeight + 1) {
      return null;
    }
    return {
      selector: selectorFor(element), kind,
      left: round(left), right: round(right), top: round(top), bottom: round(bottom),
      width: round(rect.width), height: round(rect.height),
    };
  }).filter(Boolean).slice(0, 100);
  const clipped = [];
  const clippedSeen = new Set();
  for (const {element, rect, kind} of contentBoxes) {
    let ancestor = element;
    while (ancestor) {
      const style = getComputedStyle(ancestor);
      const clipsX = ['hidden', 'clip'].includes(style.overflowX);
      const clipsY = ['hidden', 'clip'].includes(style.overflowY);
      if (clipsX || clipsY) {
        const clipRect = ancestor.getBoundingClientRect();
        const axes = [];
        if (clipsX && (rect.left < clipRect.left - 1 || rect.right > clipRect.right + 1)) axes.push('x');
        if (clipsY && (rect.top < clipRect.top - 1 || rect.bottom > clipRect.bottom + 1)) axes.push('y');
        if (axes.length) {
          const key = `${selectorFor(element)}|${selectorFor(ancestor)}|${axes.join('')}`;
          if (!clippedSeen.has(key)) {
            clippedSeen.add(key);
            clipped.push({
              selector: selectorFor(element),
              clipped_by: selectorFor(ancestor),
              axes,
              kind,
            });
          }
        }
      }
      if (ancestor === root) break;
      ancestor = ancestor.parentElement;
    }
  }
  const mediaErrors = [];
  const mediaElements = body ? Array.from(body.querySelectorAll('img,video,audio')) : [];
  for (const element of mediaElements) {
    let reason = '';
    if (element.tagName === 'IMG' && (!element.complete || element.naturalWidth <= 0 || element.naturalHeight <= 0)) {
      reason = 'image_decode_failed';
    } else if (['VIDEO', 'AUDIO'].includes(element.tagName)) {
      const hasSource = Boolean(element.currentSrc || element.getAttribute('src') || element.querySelector('source[src]'));
      if (element.error || element.networkState === HTMLMediaElement.NETWORK_NO_SOURCE) {
        reason = 'media_decode_failed';
      } else if (hasSource && element.readyState === HTMLMediaElement.HAVE_NOTHING) {
        reason = 'media_not_ready';
      }
    }
    if (reason) mediaErrors.push({selector: selectorFor(element), reason});
  }
  const textVisible = Boolean(body && body.innerText && body.innerText.trim());
  const mediaVisible = contentBoxes.some(({element, kind}) => {
    if (kind !== 'media') return false;
    if (element.tagName === 'IMG') return element.complete && element.naturalWidth > 0;
    if (element.tagName === 'VIDEO') return element.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA;
    if (element.tagName === 'CANVAS') return element.width > 0 && element.height > 0;
    if (element.tagName === 'SVG') return Boolean(element.querySelector('*'));
    return false;
  });
  return {
    dom_blank: !(textVisible || mediaVisible),
    media_errors: mediaErrors.slice(0, 100),
    geometry: {
      viewport_width: viewportWidth,
      viewport_height: viewportHeight,
      scroll_width: scrollWidth,
      scroll_height: scrollHeight,
      horizontal_overflow: Math.max(0, scrollWidth - viewportWidth),
      vertical_overflow: Math.max(0, scrollHeight - viewportHeight),
      out_of_canvas: outOfCanvas,
      clipped_content: clipped.slice(0, 100),
      inspection_truncated: inspectionTruncated,
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
    blocked: list[dict[str, object]] = []
    missing: list[dict[str, object]] = []
    direct_network: list[dict[str, object]] = []
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
            console_errors.append(
                {
                    "type": "error",
                    "text": _sanitize_diagnostic_text(
                        str(message.text), paths.workspace_root
                    ),
                }
            )

    def page_error(web_error: object) -> None:
        error = getattr(web_error, "error", web_error)
        page_errors.append(
            {
                "message": _sanitize_diagnostic_text(
                    str(error), paths.workspace_root
                )
            }
        )

    def request_failed(request: object) -> None:
        failure = request.failure
        failure_text = failure if isinstance(failure, str) else str(failure or "request failed")
        request_errors.append(
            {
                "url": _sanitize_url(request.url, paths.workspace_root),
                "error": _sanitize_diagnostic_text(
                    failure_text, paths.workspace_root
                ),
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

    def block_direct_network(_source: object, payload: object) -> None:
        if not isinstance(payload, dict):
            payload = {}
        api = str(payload.get("api", "unknown"))
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", api) is None:
            api = "unknown"
        targets = payload.get("targets")
        if not isinstance(targets, list) or not targets:
            targets = [""]
        for target in targets[:8]:
            raw = str(target)
            record = {
                "url": _sanitize_url(raw, paths.workspace_root)
                if raw
                else "[not-provided]",
                "reason": "direct_network_api_blocked",
                "resource_type": api,
            }
            direct_network.append(record)
            blocked.append(record)

    context = browser.new_context(
        viewport={"width": width, "height": height},
        service_workers="block",
        reduced_motion="reduce",
        offline=True,
    )
    context.route("**/*", route_request)
    context.route_web_socket("**/*", block_websocket)
    context.expose_binding("__autodesignAuditBlockedNetwork", block_direct_network)
    context.add_init_script(_CONTEXT_INIT_SCRIPT)
    context.on("console", console_message)
    context.on("weberror", page_error)
    context.on("requestfailed", request_failed)
    page = context.new_page()
    try:
        try:
            page.goto(paths.html.as_uri(), wait_until="load", timeout=30000)
            page.wait_for_timeout(250)
        except Exception as error:  # Playwright error types live only in the pinned venv.
            page_errors.append(
                {
                    "message": _sanitize_diagnostic_text(
                        str(error), paths.workspace_root
                    )
                }
            )
        page.evaluate(_PREPARE_MEDIA_SCRIPT)
        page.evaluate(_FREEZE_SCRIPT)
        evaluated = page.evaluate(_GEOMETRY_SCRIPT)
        screenshot = _safe_output_file(paths.output_dir / f"{label}.png", paths.output_dir)
        screenshot_bytes = page.screenshot(full_page=True)
        screenshot_analysis = _analyze_png_paint(screenshot_bytes)
        _atomic_write_bytes(screenshot, screenshot_bytes)
        evaluated_after = page.evaluate(_GEOMETRY_SCRIPT)
        dom_state_stable = json.dumps(
            evaluated, sort_keys=True, separators=(",", ":")
        ) == json.dumps(evaluated_after, sort_keys=True, separators=(",", ":"))
        dom_blank = bool(evaluated.get("dom_blank", True))
        blank_render = dom_blank or screenshot_analysis.get("painted_content") is not True
        observation = {
            "label": label,
            "viewport": {"width": width, "height": height},
            "blocked_requests": _dedupe_records(blocked),
            "missing_local_assets": _dedupe_records(missing),
            "direct_network_attempts": _dedupe_records(direct_network),
            "console_errors": _dedupe_records(console_errors),
            "page_errors": _dedupe_records(page_errors),
            "request_errors": _dedupe_records(request_errors),
            "media_errors": evaluated.get("media_errors", []),
            "blank_render": blank_render,
            "dom_state_stable": dom_state_stable,
            "geometry": evaluated.get("geometry", {}),
            "screenshot_analysis": screenshot_analysis,
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
        browser = playwright.chromium.launch(
            headless=True, args=list(_BROWSER_NETWORK_REDUCTION_ARGS)
        )
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
    direct_network: list[dict[str, object]] = []
    for observation in observations.values():
        if isinstance(observation, dict):
            blocked.extend(observation.get("blocked_requests", []))
            missing.extend(observation.get("missing_local_assets", []))
            direct_network.extend(observation.get("direct_network_attempts", []))
    return {
        "format_version": REPORT_FORMAT_VERSION,
        "html": paths.html.relative_to(paths.workspace_root).as_posix(),
        "viewports": observations,
        "blocked_requests": _dedupe_records(blocked),
        "missing_local_assets": _dedupe_records(missing),
        "direct_network_attempts": _dedupe_records(direct_network),
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
    root = output.expanduser().resolve(strict=True)
    expanded = path.expanduser().absolute()
    try:
        parent = expanded.parent.resolve(strict=True)
    except OSError as error:
        raise BrowserAuditError("Audit output directory does not exist") from error
    if parent != root or expanded.name in {"", ".", ".."}:
        raise BrowserAuditError("Audit output file must be a direct child of the output directory")
    return root / expanded.name


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
                "direct_network_attempts": [],
                "passed": False,
                "runtime_error": _sanitize_diagnostic_text(
                    str(error), paths.workspace_root
                ),
            }
        _atomic_write_json(report, payload)
    except (BrowserAuditError, OSError, ValueError) as error:
        print(f"ERROR: {_redact_text(str(error))}", file=sys.stderr)
        return 1
    return 0 if payload.get("passed") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
