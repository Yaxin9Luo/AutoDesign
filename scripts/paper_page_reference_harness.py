#!/usr/bin/env python3
"""Capture paper-project-page references and generated pages for visual review.

This is intentionally lightweight: it mirrors enough static assets for common
GitHub Pages paper sites, strips scripts that can hang headless capture, writes
first/long screenshots, and emits a JSON manifest with simple structure counts.
The actual quality judgment is still made visually by the agent/human against
the screenshots.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def _slug(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    base = parsed.netloc + parsed.path if parsed.scheme else value
    base = base.strip("/").replace("/", "-")
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-")
    if not base:
        base = hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
    return base[:80]


def _read_url(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "AutoDesign-page-harness/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _strip_scripts(html: str) -> str:
    html = re.sub(r"(?is)<script\b[^>]*>.*?</script>", "", html)
    html = re.sub(r"(?is)<script\b[^>]*>", "", html)
    html = re.sub(r"(?is)<iframe\b[^>]*>.*?</iframe>", "", html)
    html = re.sub(r"(?is)<iframe\b[^>]*>", "", html)
    visible_css = (
        "<style id=\"autodesign-reference-visible\">"
        ".reveal,[data-reveal],.fade-in,.animate,.animated{"
        "opacity:1!important;transform:none!important;transition:none!important}"
        "</style>"
    )
    if "</head>" in html:
        return html.replace("</head>", visible_css + "</head>", 1)
    return visible_css + html


def _relative_assets(html: str) -> list[str]:
    assets: list[str] = []
    for match in re.finditer(r"""(?:src|href)=["']([^"']+)["']""", html):
        ref = match.group(1).strip()
        if not ref or ref.startswith(("#", "mailto:", "javascript:", "data:")):
            continue
        if ref.startswith(("http://", "https://", "//")):
            continue
        if not _should_mirror_asset(ref):
            continue
        if ref not in assets:
            assets.append(ref)
    return assets


def _should_mirror_asset(ref: str) -> bool:
    path = urllib.parse.urlparse(ref).path.lower()
    suffix = Path(path).suffix
    return suffix in {
        ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
        ".woff", ".woff2", ".ttf", ".otf",
    }


def _mirror_reference(url: str, out_dir: Path, *, max_assets: int) -> dict[str, Any]:
    print(f"[reference] fetch {url}", flush=True)
    raw = _read_url(url)
    html = raw.decode("utf-8", errors="replace")
    clean = _strip_scripts(html)
    index_path = out_dir / "index.html"
    visible_path = out_dir / "index_visible.html"
    out_dir.mkdir(parents=True, exist_ok=True)
    index_path.write_text(html, encoding="utf-8")
    visible_path.write_text(clean, encoding="utf-8")

    asset_results: list[dict[str, Any]] = []
    refs = _relative_assets(html)[:max_assets]
    print(f"[reference] mirror {len(refs)} asset(s) for {_slug(url)}", flush=True)
    for idx, ref in enumerate(refs, 1):
        dest = out_dir / urllib.parse.unquote(ref)
        asset_url = urllib.parse.urljoin(url, ref)
        try:
            data = _read_url(asset_url, timeout=10)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            asset_results.append({"path": ref, "bytes": len(data), "ok": True})
            print(f"  [{idx}/{len(refs)}] ok {ref}", flush=True)
        except Exception as exc:  # noqa: BLE001 - diagnostic script
            asset_results.append({"path": ref, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
            print(f"  [{idx}/{len(refs)}] fail {ref}: {type(exc).__name__}", flush=True)

    return {
        "url": url,
        "html": str(index_path),
        "visible_html": str(visible_path),
        "asset_results": asset_results,
        "structure": _structure_counts(html),
    }


def _structure_counts(html: str) -> dict[str, Any]:
    title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    words = re.findall(r"\b[\w.-]+\b", text)
    return {
        "title": re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else "",
        "sections": len(re.findall(r"(?is)<section\b", html)),
        "links": len(re.findall(r"""(?is)<a\b[^>]*href=["']""", html)),
        "images": len(re.findall(r"(?is)<img\b", html)),
        "buttons": len(re.findall(r"(?is)(class=[\"'][^\"']*(?:btn|button)[^\"']*[\"']|<button\b)", html)),
        "tables": len(re.findall(r"(?is)<table\b", html)),
        "words": len(words),
    }


def _chrome_path(explicit: str | None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("CHROME", "").strip()
    if env:
        return env
    found = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if found:
        return found
    return DEFAULT_CHROME


def _screenshot(
    html_path: Path,
    out_path: Path,
    *,
    chrome: str,
    width: int,
    height: int,
    timeout: int,
) -> dict[str, Any]:
    print(f"[screenshot] {html_path} -> {out_path.name} ({width}x{height})", flush=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    url = html_path.resolve().as_uri()
    cmd = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-sync",
        "--no-first-run",
        "--hide-scrollbars",
        f"--window-size={width},{height}",
        f"--screenshot={out_path}",
        url,
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "error": f"timeout after {timeout}s", "stdout": exc.stdout, "stderr": exc.stderr}
    return {
        "ok": proc.returncode == 0 and out_path.exists(),
        "returncode": proc.returncode,
        "path": str(out_path),
        "bytes": out_path.stat().st_size if out_path.exists() else 0,
        "tail": (proc.stdout + proc.stderr)[-1200:],
    }


def _capture_local_page(page: str, out_dir: Path, *, chrome: str, width: int, short_h: int, long_h: int, timeout: int) -> dict[str, Any]:
    path = Path(page)
    html = path.read_text(encoding="utf-8", errors="replace")
    entry = {
        "path": str(path),
        "structure": _structure_counts(html),
        "short_screenshot": _screenshot(path, out_dir / "top.png", chrome=chrome, width=width, height=short_h, timeout=timeout),
        "long_screenshot": _screenshot(path, out_dir / "long.png", chrome=chrome, width=width, height=long_h, timeout=timeout),
    }
    return entry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="out/diagnostics/paper_page_reference_harness")
    parser.add_argument("--reference", action="append", default=[])
    parser.add_argument("--page", action="append", default=[])
    parser.add_argument("--chrome", default=None)
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--short-height", type=int, default=1200)
    parser.add_argument("--long-height", type=int, default=3000)
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--max-assets", type=int, default=12)
    args = parser.parse_args(argv)

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    chrome = _chrome_path(args.chrome)
    manifest: dict[str, Any] = {"chrome": chrome, "references": [], "pages": []}

    for ref in args.reference:
        ref_dir = out_root / "references" / _slug(ref)
        entry = _mirror_reference(ref, ref_dir, max_assets=args.max_assets)
        visible = Path(entry["visible_html"])
        entry["short_screenshot"] = _screenshot(
            visible,
            ref_dir / "top.png",
            chrome=chrome,
            width=args.width,
            height=args.short_height,
            timeout=args.timeout,
        )
        entry["long_screenshot"] = _screenshot(
            visible,
            ref_dir / "long.png",
            chrome=chrome,
            width=args.width,
            height=args.long_height,
            timeout=args.timeout,
        )
        manifest["references"].append(entry)

    for page in args.page:
        page_dir = out_root / "pages" / _slug(page)
        manifest["pages"].append(
            _capture_local_page(
                page,
                page_dir,
                chrome=chrome,
                width=args.width,
                short_h=args.short_height,
                long_h=args.long_height,
                timeout=args.timeout,
            )
        )

    manifest_path = out_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
