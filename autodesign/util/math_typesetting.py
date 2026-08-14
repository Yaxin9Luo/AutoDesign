"""Shared TeX math rendering helpers for self-contained HTML artifacts."""

from __future__ import annotations

import base64
from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Any

from .logging import log


_STRIP_CONTEXT_RE = re.compile(
    r"(?is)<(script|style|template|pre|code|kbd|samp)\b[^>]*>.*?</\1>"
)
_INLINE_DOLLAR_RE = re.compile(
    r"(?<!\\)(?<![\w])\$(?![\s$])(.{1,240}?)(?<!\\)\$(?![\w])",
    re.S,
)
_MATH_SPAN_RE = re.compile(
    r"\$\$(?P<display_dollar>.*?)\$\$"
    r"|\\\[(?P<display_bracket>.*?)\\\]"
    r"|\\\((?P<inline_paren>.*?)\\\)"
    r"|(?<!\\)(?<![\w])\$(?P<inline_dollar>(?![\s$]).{1,1000}?)(?<!\\)\$(?![\w])",
    re.S,
)
_LATEX_RESIDUE_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "paper_poster_html_latex_reference_residue",
        re.compile(r"\\(?:ref|eqref|cite|label)\s*\{", re.I),
        "Remove paper-drafting references such as \\ref, \\cite, or \\label from visible poster copy.",
    ),
    (
        "paper_poster_html_latex_section_residue",
        re.compile(r"\\(?:section|subsection|paragraph|caption)\s*\{", re.I),
        "Convert LaTeX sectioning/caption commands into normal HTML headings or captions.",
    ),
    (
        "paper_poster_html_latex_text_style_residue",
        re.compile(r"\\(?:textbf|emph|textit)\s*\{", re.I),
        "Use HTML <strong> or <em> outside math instead of LaTeX text-style commands.",
    ),
    (
        "paper_poster_html_latex_environment_residue",
        re.compile(r"\\(?:begin|end)\s*\{(?:equation|figure|table|align)\*?\}", re.I),
        "Wrap equations in \\[...\\] and express figures/tables as normal HTML, not LaTeX environments.",
    ),
)


def has_tex_math(value: Any) -> bool:
    source = _math_scan_source(value)
    if "\\(" in source or "\\[" in source or "$$" in source:
        return True
    return bool(_INLINE_DOLLAR_RE.search(source))


def lint_tex_math_source(value: Any) -> list[dict[str, Any]]:
    """Return repair-oriented issues for TeX that will not render cleanly in HTML."""

    source = _math_scan_source(value)
    issues: list[dict[str, Any]] = []
    for idx, match in enumerate(_MATH_SPAN_RE.finditer(source), start=1):
        math_src = next(group for group in match.groups() if group is not None)
        if "<" in math_src:
            issues.append({
                "id": "paper_poster_html_math_raw_angle",
                "severity": "P0",
                "message": "Math source contains a raw '<' character inside TeX delimiters.",
                "fix": "Use TeX operators such as \\lt, \\le, or \\langle instead of raw '<' inside math.",
                "evidence": {"math_index": idx, "sample": math_src[:160]},
            })
    outside_math = _MATH_SPAN_RE.sub(" ", source)
    for issue_id, pattern, fix in _LATEX_RESIDUE_PATTERNS:
        match = pattern.search(outside_math)
        if match:
            issues.append({
                "id": issue_id,
                "severity": "P0",
                "message": f"Visible poster copy contains LaTeX residue: {match.group(0)}",
                "fix": fix,
                "evidence": {"sample": _window(outside_math, match.start(), match.end())},
            })
    return issues


def inline_katex_bundle(
    repo_root: Path,
    *,
    root_selector: str = ".paper-poster",
    style_id: str = "autodesign-katex-css",
    log_prefix: str = "html.katex",
) -> str:
    assets = _load_katex_assets(str(Path(repo_root)))
    if assets is None:
        log(f"{log_prefix}.vendor_missing", path=str(Path(repo_root) / "assets" / "vendor" / "katex"))
        return ""
    css, core_js, auto_js = assets
    selector_json = json.dumps(root_selector)
    init_js = (
        "(function(){\n"
        "  window.__autoDesignMathPending = true;\n"
        "  window.__autoDesignMathReady = false;\n"
        "  function countRaw(text) {\n"
        "    text = text || '';\n"
        "    return {\n"
        "      backslash: (text.match(/\\\\\\(|\\\\\\[|\\\\\\)|\\\\\\]/g) || []).length,\n"
        "      displayDollar: (text.match(/\\$\\$/g) || []).length,\n"
        "      inlineDollar: (text.match(/(^|[^\\\\])\\$(?!\\s|\\$)/g) || []).length\n"
        "    };\n"
        "  }\n"
        "  function run() {\n"
        "    var root = document.querySelector(" + selector_json + ") || document.body;\n"
        "    var beforeText = root ? (root.textContent || '') : '';\n"
        "    var beforeRaw = countRaw(beforeText);\n"
        "    var hasSource = /\\\\\\(|\\\\\\[|\\$\\$|(^|[^\\\\])\\$(?!\\s|\\$)/.test(beforeText);\n"
        "    var errorMessage = '';\n"
        "    try {\n"
        "      if (root && window.renderMathInElement) {\n"
        "        renderMathInElement(root, {\n"
        "          delimiters: [\n"
        "            {left: '$$', right: '$$', display: true},\n"
        "            {left: '\\\\[', right: '\\\\]', display: true},\n"
        "            {left: '\\\\(', right: '\\\\)', display: false},\n"
        "            {left: '$', right: '$', display: false}\n"
        "          ],\n"
        "          ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code'],\n"
        "          throwOnError: false,\n"
        "          strict: 'ignore'\n"
        "        });\n"
        "      }\n"
        "    } catch (e) {\n"
        "      errorMessage = String(e && e.message ? e.message : e);\n"
        "      if (console && console.warn) console.warn('KaTeX render failed:', e);\n"
        "    }\n"
        "    var afterText = root ? (root.textContent || '') : '';\n"
        "    var afterRaw = countRaw(afterText);\n"
        "    window.__autoDesignMathStats = {\n"
        "      bundlePresent: true,\n"
        "      hasSource: hasSource,\n"
        "      beforeRawDelimiterCount: beforeRaw.backslash + beforeRaw.displayDollar + beforeRaw.inlineDollar,\n"
        "      rawBackslashDelimiterCount: afterRaw.backslash,\n"
        "      rawDollarDelimiterCount: afterRaw.displayDollar + afterRaw.inlineDollar,\n"
        "      renderedCount: root ? root.querySelectorAll('.katex').length : 0,\n"
        "      katexErrorCount: root ? root.querySelectorAll('.katex-error').length : 0,\n"
        "      errorMessage: errorMessage\n"
        "    };\n"
        "    window.__autoDesignMathPending = false;\n"
        "    window.__autoDesignMathReady = true;\n"
        "  }\n"
        "  if (document.readyState === 'loading') {\n"
        "    document.addEventListener('DOMContentLoaded', run, {once: true});\n"
        "  } else {\n"
        "    run();\n"
        "  }\n"
        "})();"
    )
    poster_css = (
        f"<style id=\"{style_id}\" data-autodesign-katex=\"1\">{css}\n"
        f"{root_selector} :where(.formula,.math-block,.equation,[data-block-kind=\"formula\"],[data-role~=\"formula\"]) {{\n"
        "  line-height: 1.12 !important;\n"
        "  overflow-wrap: normal;\n"
        "}\n"
        f"{root_selector} :where(.katex) {{ font-size: 1em; line-height: 1.08 !important; }}\n"
        f"{root_selector} :where(.katex-display) {{ margin: .18em 0 .22em; max-width: 100%; overflow-x: visible; overflow-y: visible; }}\n"
        f"{root_selector} :where(.formula,.math-block,.equation,[data-block-kind=\"formula\"],[data-role~=\"formula\"]) .katex-display {{ margin: .08em 0; }}\n"
        "</style>\n"
    )
    return (
        poster_css
        + f"<script data-autodesign-katex=\"1\">{core_js}</script>\n"
        + f"<script data-autodesign-katex=\"1\">{auto_js}</script>\n"
        + f"<script data-autodesign-katex=\"1\">{init_js}</script>\n"
    )


def _has_katex_bundle_marker(html_text: str) -> bool:
    return any(marker in html_text for marker in (
        "data-autodesign-katex",
        'id="autodesign-katex-css"',
        "data-designanything-katex",
        'id="od-katex-css"',
    ))


def inject_katex_into_html(
    html_text: str,
    repo_root: Path,
    *,
    root_selector: str = ".paper-poster",
) -> tuple[str, bool]:
    if not has_tex_math(html_text):
        return html_text, False
    if _has_katex_bundle_marker(html_text):
        return html_text, False
    block = inline_katex_bundle(repo_root, root_selector=root_selector)
    if not block:
        return html_text, False
    if re.search(r"(?i)</head\s*>", html_text):
        return re.sub(r"(?i)</head\s*>", lambda _m: block + "</head>", html_text, count=1), True
    if re.search(r"(?i)</body\s*>", html_text):
        return re.sub(r"(?i)</body\s*>", lambda _m: block + "</body>", html_text, count=1), True
    return html_text + "\n" + block, True


def ensure_poster_katex_document(
    html_path: Path,
    repo_root: Path,
    *,
    root_selector: str = ".paper-poster",
) -> dict[str, Any]:
    html_path = Path(html_path)
    html_text = html_path.read_text(encoding="utf-8", errors="replace")
    detected = has_tex_math(html_text)
    patched, applied = inject_katex_into_html(
        html_text,
        repo_root,
        root_selector=root_selector,
    )
    if applied:
        html_path.write_text(patched, encoding="utf-8")
    return {
        "detected": detected,
        "applied": applied,
        "already_present": detected and not applied and _has_katex_bundle_marker(html_text),
    }


def wait_for_autodesign_math(page: Any, *, timeout_ms: int = 2500) -> None:
    try:
        present = bool(page.evaluate(
            """() => Boolean(
              window.__autoDesignMathPending ||
              window.__autoDesignMathReady ||
              window.__designAnythingMathPending ||
              window.__designAnythingMathReady ||
              document.querySelector('[data-autodesign-katex="1"]') ||
              document.querySelector('[data-designanything-katex="1"]')
            )"""
        ))
    except Exception:
        return
    if not present:
        return
    try:
        page.wait_for_function(
            "() => window.__autoDesignMathReady === true || window.__designAnythingMathReady === true",
            timeout=max(250, min(4000, int(timeout_ms))),
        )
    except Exception:
        pass


def collect_autodesign_math_status(page: Any) -> dict[str, Any]:
    try:
        status = page.evaluate(
            """() => {
              const root = document.querySelector('.paper-poster') || document.body;
              const stats = window.__autoDesignMathStats || window.__designAnythingMathStats || {};
              const renderedCount = root ? root.querySelectorAll('.katex').length : 0;
              const katexErrorCount = root ? root.querySelectorAll('.katex-error').length : 0;
              const text = root ? (root.textContent || '') : '';
              const rawBackslash = (text.match(/\\\\\\(|\\\\\\[|\\\\\\)|\\\\\\]/g) || []).length;
              const rawDollar = (text.match(/\\$\\$/g) || []).length + (text.match(/(^|[^\\\\])\\$(?!\\s|\\$)/g) || []).length;
              return {
                bundlePresent: Boolean(
                  stats.bundlePresent ||
                  document.querySelector('[data-autodesign-katex="1"]') ||
                  document.querySelector('[data-designanything-katex="1"]')
                ),
                hasSource: Boolean(stats.hasSource),
                renderedCount: Math.max(Number(stats.renderedCount || 0), renderedCount),
                katexErrorCount: Math.max(Number(stats.katexErrorCount || 0), katexErrorCount),
                rawBackslashDelimiterCount: Math.max(Number(stats.rawBackslashDelimiterCount || 0), rawBackslash),
                rawDollarDelimiterCount: Math.max(Number(stats.rawDollarDelimiterCount || 0), rawDollar),
                beforeRawDelimiterCount: Number(stats.beforeRawDelimiterCount || 0),
                errorMessage: String(stats.errorMessage || '')
              };
            }"""
        )
    except Exception as exc:
        return {"bundlePresent": False, "error": f"{type(exc).__name__}: {exc}"}
    return status if isinstance(status, dict) else {}


# Compatibility aliases for callers that imported the pre-rename helper names.
wait_for_designanything_math = wait_for_autodesign_math
collect_designanything_math_status = collect_autodesign_math_status


def _math_scan_source(value: Any) -> str:
    source = str(value or "")
    return _STRIP_CONTEXT_RE.sub(" ", source)


def _window(text: str, start: int, end: int, radius: int = 80) -> str:
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    return re.sub(r"\s+", " ", text[lo:hi]).strip()


@lru_cache(maxsize=4)
def _load_katex_assets(repo_root: str) -> tuple[str, str, str] | None:
    vendor = Path(repo_root) / "assets" / "vendor" / "katex"
    css_path = vendor / "katex.min.css"
    core_js_path = vendor / "katex.min.js"
    auto_js_path = vendor / "auto-render.min.js"
    fonts_dir = vendor / "fonts"
    if not (css_path.exists() and core_js_path.exists() and auto_js_path.exists()):
        return None

    css = css_path.read_text(encoding="utf-8")
    css = re.sub(r',\s*url\(fonts/[^)]+\.woff\)\s*format\("woff"\)', "", css)
    css = re.sub(r',\s*url\(fonts/[^)]+\.ttf\)\s*format\("truetype"\)', "", css)

    def replace_font(match: re.Match[str]) -> str:
        fname = match.group(1)
        fp = fonts_dir / fname
        if not fp.exists():
            log("html.katex.font_missing", file=fname)
            return match.group(0)
        b64 = base64.b64encode(fp.read_bytes()).decode("ascii")
        return f"url(data:font/woff2;base64,{b64})"

    css = re.sub(r"url\(fonts/([^)]+\.woff2)\)", replace_font, css)
    return (
        css,
        core_js_path.read_text(encoding="utf-8"),
        auto_js_path.read_text(encoding="utf-8"),
    )
