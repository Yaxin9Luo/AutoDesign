"""Runtime settings — env vars, model ids, paths, caps.

Multi-provider LLM backend (v2.1):
- Designer / Critic LLM access goes through `llm_backend.LLMBackend` so we
  can mix Anthropic Claude with OpenAI-compatible models (Moonshot Kimi,
  DeepSeek, Doubao, vLLM-served Qwen, etc.) without changing tool schemas
  or runtime contracts.
- Provider is auto-detected from model id prefix: `anthropic/...` and
  `claude-...` → Anthropic backend; everything else → OpenAI-compat
  backend (defaults to OpenRouter base_url).
- Override per-role: `DESIGNER_PROVIDER=anthropic|openai_compat|auto`
  (same for `CRITIC_PROVIDER`).
- Designer mirrors the coding-harness model when the Web UI can route it,
  with a `gpt-5.5` OpenAI-compatible fallback. Helper API roles default to
  `gpt-5.4-nano`. Every role remains swappable via env.

Credentials (any subset works depending on which providers you call):
- `OPENROUTER_API_KEY`: powers BOTH Anthropic-via-OpenRouter and the
  OpenAI-compat backend (single key, both endpoints).
- `ANTHROPIC_API_KEY`: stock Anthropic endpoint.
- `OPENAI_COMPAT_API_KEY` + `OPENAI_COMPAT_BASE_URL`: explicit override
  for self-hosted vLLM / native Moonshot / DeepSeek / Doubao endpoints.
- `GEMINI_API_KEY`: only required when `IMAGE_PROVIDER` resolves to
  native `gemini` (auto-routing on a model id starting with `gemini-` or
  `imagen-`).
"""

from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values


REPO_ROOT = Path(__file__).resolve().parent.parent

_CODEX_APP_BINARY_CANDIDATES: tuple[Path, ...] = (
    Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
    Path("/Applications/Codex.app/Contents/Resources/codex"),
)

_CODEX_CONFIGURED_BINARY_ENV_KEYS: tuple[str, ...] = (
    "AUTODESIGN_CODEX_BIN",
    "DESIGN_ANYTHING_CODEX_BIN",
    "AUTODESIGN_CODE_EDITOR_CODEX_BIN",
    "DESIGN_ANYTHING_CODE_EDITOR_CODEX_BIN",
    "AUTODESIGN_DESIGNER_AUTHOR_CODEX_BIN",
    "DESIGN_ANYTHING_DESIGNER_AUTHOR_CODEX_BIN",
    "DESIGN_ANYTHING_PLANNER_AUTHOR_CODEX_BIN",
)

_DEEPSEEK_CONFIGURED_BINARY_ENV_KEYS: tuple[str, ...] = (
    "AUTODESIGN_CODE_EDITOR_DEEPSEEK_BIN",
    "DESIGN_ANYTHING_CODE_EDITOR_DEEPSEEK_BIN",
    "AUTODESIGN_DESIGNER_AUTHOR_DEEPSEEK_BIN",
    "DESIGN_ANYTHING_DESIGNER_AUTHOR_DEEPSEEK_BIN",
    "DESIGN_ANYTHING_PLANNER_AUTHOR_DEEPSEEK_BIN",
)

_PROXY_ENV_NAMES: tuple[str, ...] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def _load_dotenv_defaults(path: Path) -> None:
    """Load .env values without clobbering explicit non-empty process env."""

    for key, value in dotenv_values(path).items():
        if value is None:
            continue
        if os.getenv(key, "").strip():
            continue
        os.environ[key] = value


_load_dotenv_defaults(REPO_ROOT / ".env")  # .env wins only over unset/empty env vars


# Anthropic SDK appends "/v1/messages" itself, so the base URL must NOT
# include the /v1 prefix — otherwise the request hits /api/v1/v1/messages → 404.
OPENROUTER_BASE_URL_ANTHROPIC = "https://openrouter.ai/api"
# OpenAI client DOES want the /v1 prefix (it appends /chat/completions itself).
OPENROUTER_BASE_URL_OPENAI = "https://openrouter.ai/api/v1"

# Multi-provider text-model defaults. The Web UI mirrors an explicit
# coding-harness model onto Designer when the configured API route can serve it.
# These values are only the provider-correct fallback when no model is mirrored.
DEFAULT_DESIGNER_MODEL = "gpt-5.5"
OPENROUTER_DEFAULT_DESIGNER_MODEL = "openai/gpt-5.5"
DEFAULT_CRITIC_MODEL = "gpt-5.4-nano"
ANTHROPIC_FALLBACK_DESIGNER = "claude-opus-4-7"            # if user only has ANTHROPIC_API_KEY
ANTHROPIC_FALLBACK_CRITIC = "claude-opus-4-7"

# v2.4 Prompt Enhancer — runs once before designer.start, converting a raw
# user brief into a structured multi-section enhanced brief.
DEFAULT_ENHANCER_MODEL = "gpt-5.4-nano"
ANTHROPIC_FALLBACK_ENHANCER = "claude-opus-4-7"

# v2.8.0 ClaimGraph extractor — runs between enhancer and planner when
# the input attaches a PDF.
DEFAULT_CLAIM_GRAPH_MODEL = "gpt-5.4-nano"
ANTHROPIC_FALLBACK_CLAIM_GRAPH = "claude-opus-4-7"

# Deck outline planner — runs after document ingest to choose source-aware
# slide count and outline before the main planner writes the DesignSpec.
DEFAULT_DECK_OUTLINE_MODEL = "gpt-5.4-nano"
ANTHROPIC_FALLBACK_DECK_OUTLINE = "claude-opus-4-7"

# Paper memory curator — runs after PDF ingest builds canonical paper_memory.
# Users can pin another model with PAPER_MEMORY_MODEL.
DEFAULT_PAPER_MEMORY_MODEL = "gpt-5.4-nano"
ANTHROPIC_FALLBACK_PAPER_MEMORY = "claude-opus-4-7"

# v2.8.1 HyperFrames Composer — single-turn LLM agent that writes index.html
# for a HyperFrames video project scaffolded by `export_video`.
# `SKIP_VIDEO_COMPOSER=1` disables the stage so power users can author
# index.html themselves (same escape-hatch as `SKIP_PROMPT_ENHANCER`).
DEFAULT_COMPOSER_MODEL = "gpt-5.4-nano"
ANTHROPIC_FALLBACK_COMPOSER = "claude-opus-4-7"

# Local coding-agent harness defaults. These are CLI model aliases, not
# AutoDesign planner model ids. ZCode has no stable top-level --model flag,
# so the wrapper selects its config under a lock and restores the prior model
# after the invocation.
DEFAULT_KIMI_CODE_HARNESS_MODEL = "kimi-k2.7-code-highspeed"
DEFAULT_ZCODE_HARNESS_MODEL = "glm-5.2"


_MODEL_ID_ALIASES: dict[str, str] = {
    # Gemini 3 Pro Preview was removed from OpenRouter routing; keep old saved
    # browser settings and older short slugs working by migrating to 3.1.
    "gemini-3-pro": "gemini-3.1-pro-preview",
    "gemini-3-pro-preview": "gemini-3.1-pro-preview",
    "gemini-3.1-pro": "gemini-3.1-pro-preview",
    "google/gemini-3-pro": "google/gemini-3.1-pro-preview",
    "google/gemini-3-pro-preview": "google/gemini-3.1-pro-preview",
    "google/gemini-3.1-pro": "google/gemini-3.1-pro-preview",
}


def normalize_model_id(model: str | None) -> str:
    """Return the provider-correct model id for known legacy aliases."""
    cleaned = (model or "").strip()
    return _MODEL_ID_ALIASES.get(cleaned, cleaned)


ProviderChoice = Literal["auto", "anthropic", "openai_compat"]
ImageProviderChoice = Literal["auto", "gemini", "openrouter", "openai_compat"]
SectionNumberPolicy = Literal["renumber", "strip", "preserve"]
DesignerAuthorMode = Literal["internal", "external"]
DesignerAuthorHarness = Literal["custom", "codex", "claude", "deepseek", "opencode", "kimi", "mimo", "pi", "zcode"]
CodeEditorHarness = Literal["custom", "codex", "claude", "deepseek", "opencode", "kimi", "mimo", "pi", "zcode"]
IdentityLogoAgentMode = Literal["auto", "off", "required"]
IdentityLogoAgentHarness = Literal["custom", "codex", "claude", "deepseek", "opencode", "kimi", "mimo", "pi", "zcode"]
OpenResearchSubmitterMode = Literal["off", "custom"]

POSTER_HARNESS_MODES = frozenset({"cheap", "standard", "quality", "dogfood"})
DEFAULT_USER_POSTER_HARNESS_MODE = "dogfood"
_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSEY_ENV_VALUES = frozenset({"0", "false", "no", "off"})
_USER_FACING_CREDENTIAL_PLACEHOLDERS = frozenset({
    "sk-or-v1-...",
    "sk-ant-...",
    "AIza...",
})


def _credential_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value in _USER_FACING_CREDENTIAL_PLACEHOLDERS:
        return ""
    return value


def _env_first(*keys: str, default: str = "") -> str:
    for key in keys:
        value = os.getenv(key, "").strip()
        if value:
            return value
    return default


def _project_env_keys(
    suffix: str,
    *,
    aliases: tuple[str, ...] = (),
    unprefixed: tuple[str, ...] = (),
) -> tuple[str, ...]:
    return (
        f"AUTODESIGN_{suffix}",
        f"DESIGN_ANYTHING_{suffix}",
        *aliases,
        *unprefixed,
    )


def _project_env_first(
    suffix: str,
    *,
    aliases: tuple[str, ...] = (),
    unprefixed: tuple[str, ...] = (),
    default: str = "",
) -> str:
    return _env_first(
        *_project_env_keys(suffix, aliases=aliases, unprefixed=unprefixed),
        default=default,
    )


def _prefixed_env_first(
    env_prefix: str | tuple[str, ...],
    suffix: str,
    *,
    default: str = "",
) -> str:
    prefixes = (env_prefix,) if isinstance(env_prefix, str) else env_prefix
    return _env_first(*(f"{prefix}_{suffix}" for prefix in prefixes), default=default)


def _parse_bool_value(raw: str | None, *, default: bool = False) -> bool:
    value = (raw or "").strip().lower()
    if value in _TRUTHY_ENV_VALUES:
        return True
    if value in _FALSEY_ENV_VALUES:
        return False
    return default


def _parse_custom_headers(raw: str | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    normalized = (raw or "").replace("\\n", "\n")
    for line in normalized.splitlines():
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        name = name.strip()
        value = value.strip()
        if name and value:
            headers[name] = value
    return headers


def _parse_poster_harness_mode(
    raw: str | None,
    *,
    default: str = DEFAULT_USER_POSTER_HARNESS_MODE,
) -> str:
    value = (raw or "").strip().lower()
    if value in POSTER_HARNESS_MODES:
        return value
    return default


def _claude_artifact_prompt_file_command(
    binary: str,
    model: str | None = None,
    *,
    prompt_file: str,
    target_files: list[str],
    done_file: str,
) -> str:
    required_outputs = [*target_files, done_file]
    output_list = ", ".join(required_outputs)
    system_guard = (
        "You are running as a non-interactive AutoDesign file author. "
        "A prose-only response is a failure. You must use filesystem tools to "
        f"create or update {output_list} in the "
        "current working directory before your final response. Before exiting, "
        "verify every required output exists; if any file is missing, keep working "
        "and write the missing file."
    )
    claude_cmd = [
        binary,
        "--print",
        "--permission-mode",
        "bypassPermissions",
        "--dangerously-skip-permissions",
        "--no-session-persistence",
        # Isolate the harness from the user's local Claude Code environment so
        # a fresh clone reproduces bit-for-bit: --bare skips hooks / LSP /
        # plugin sync / auto-memory / keychain / CLAUDE.md auto-discovery and
        # any user-level ~/.claude/settings.json; --disable-slash-commands
        # turns off every skill (built-in + user + plugin). Explicit config
        # still works via --mcp-config / --settings / --agents / --plugin-dir
        # if we ever want to opt in.
        "--bare",
        "--disable-slash-commands",
        "--effort",
        "high",
        "--append-system-prompt",
        system_guard,
    ]
    model = (model or "").strip()
    if model:
        claude_cmd.extend(["--model", model])
    instruction = (
        f"Read the full task prompt from {prompt_file} in the current directory, "
        "then execute it exactly. Do not summarize the prompt or announce a plan. "
        f"Create {output_list} before exiting."
    )
    script = f"cat > {shlex.quote(prompt_file)} && exec {shlex.join(claude_cmd + [instruction])}"
    return shlex.join(["/bin/sh", "-c", script])


def _claude_prompt_file_command(binary: str, model: str | None = None) -> str:
    return _claude_artifact_prompt_file_command(
        binary,
        model,
        prompt_file=".autodesign_claude_prompt.md",
        target_files=["poster.html"],
        done_file="designer_author_done.json",
    )


# Env vars that a corporate `~/.claude/settings.json` `env` block (or the
# user's shell) may set to point the claude CLI at a custom Anthropic-
# compatible gateway. When AutoDesign manages harness auth itself — an
# explicit `harness_api_key` OR an isolated login dir — these must be removed
# from the subprocess env so they cannot shadow our credential / OAuth login.
_CLAUDE_GATEWAY_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
)


def harness_auth_dir(harness: str) -> Path:
    """Per-harness isolated CLI config/credentials directory.

    Used as `CLAUDE_CONFIG_DIR` (claude) / `CODEX_HOME` (codex) so the Web-UI
    "Connect account" login lands in a directory separate from the user's
    daily `~/.claude` (which may be pinned to an internal gateway). Override
    the root with `AUTODESIGN_HARNESS_AUTH_DIR`.
    """

    root = _project_env_first("HARNESS_AUTH_DIR").strip()
    base = Path(root).expanduser() if root else (Path.home() / ".autodesign" / "harness-auth")
    return base / ((harness or "").strip().lower() or "claude")


_HARNESS_LOGIN_MARKER = ".autodesign-connected.json"
_LEGACY_HARNESS_LOGIN_MARKER = ".designanything-connected.json"


def _harness_auth_read_dirs(harness: str) -> tuple[Path, ...]:
    canonical = harness_auth_dir(harness)
    if _project_env_first("HARNESS_AUTH_DIR").strip():
        return (canonical,)
    legacy = (
        Path.home()
        / ".designanything"
        / "harness-auth"
        / ((harness or "").strip().lower() or "claude")
    )
    return (canonical, legacy)


def _existing_harness_login_dir(harness: str) -> Path | None:
    harness = (harness or "").strip().lower()
    for auth_dir in _harness_auth_read_dirs(harness):
        if any((auth_dir / marker).exists() for marker in (
            _HARNESS_LOGIN_MARKER,
            _LEGACY_HARNESS_LOGIN_MARKER,
        )):
            return auth_dir
        if harness == "claude" and (auth_dir / ".credentials.json").exists():
            return auth_dir
        if harness == "codex" and (auth_dir / "auth.json").exists():
            return auth_dir
    return None


def harness_auth_read_dir(harness: str) -> Path:
    """Return an existing canonical or legacy login dir, else the canonical dir."""

    return _existing_harness_login_dir(harness) or harness_auth_dir(harness)


def resolve_harness_binary(harness: str) -> str | None:
    """Resolve the CLI binary for a harness, falling back to known app paths.

    `codex` ships inside the ChatGPT or legacy Codex app bundle and is
    frequently not on PATH, so keep app discovery centralized here.
    """

    harness = (harness or "").strip().lower()
    if harness == "codex":
        candidates = codex_binary_candidates()
        return str(candidates[0]["binary"]) if candidates else None
    if harness == "deepseek":
        return shutil.which("dsh")
    return shutil.which(harness)


def codex_binary_candidates() -> list[dict[str, str]]:
    """Return executable Codex candidates in discovery order."""

    candidates: list[dict[str, str]] = []
    seen: set[str] = set()

    def append_candidate(binary: Path | str, source: str) -> None:
        path = Path(binary).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            return
        resolved = str(path.resolve())
        if resolved in seen:
            return
        seen.add(resolved)
        candidates.append({"binary": str(path), "source": source})

    for env_key in _CODEX_CONFIGURED_BINARY_ENV_KEYS:
        configured = os.getenv(env_key, "").strip()
        if configured:
            append_candidate(configured, "configured")

    path_binary = shutil.which("codex")
    if path_binary:
        append_candidate(path_binary, "path")
    for app_binary in _CODEX_APP_BINARY_CANDIDATES:
        append_candidate(app_binary, "app_bundle")
    return candidates


def inspect_codex_binary(binary: str) -> dict[str, Any]:
    """Probe one Codex executable without invoking a model request."""

    version = ""
    root_help = ""
    exec_help = ""
    errors: list[str] = []
    for command, output_key in (
        ([binary, "--version"], "version"),
        ([binary, "--help"], "root_help"),
        ([binary, "exec", "--help"], "exec_help"),
    ):
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{output_key}: {type(exc).__name__}: {exc}")
            continue
        output = ((completed.stdout or "") + (completed.stderr or "")).strip()
        if completed.returncode != 0:
            errors.append(f"{output_key}: exit {completed.returncode}")
            continue
        if output_key == "version":
            version = output.splitlines()[0] if output else ""
        elif output_key == "root_help":
            root_help = output
        else:
            exec_help = output
    return {
        "binary": binary,
        "version": version,
        "capabilities": {
            "search": "--search" in root_help,
            "exec_ephemeral": "--ephemeral" in exec_help,
            "exec_skip_git_repo_check": "--skip-git-repo-check" in exec_help,
            "exec_sandbox": "--sandbox" in exec_help,
            "exec_model": "--model" in exec_help or "-m," in exec_help,
            "exec_dangerously_bypass_approvals_and_sandbox": (
                "--dangerously-bypass-approvals-and-sandbox" in exec_help
            ),
        },
        "errors": errors,
    }


def resolve_codex_runtime(*, required: tuple[str, ...] = ()) -> dict[str, Any]:
    """Select the first Codex candidate that satisfies required capabilities."""

    rejected: list[dict[str, Any]] = []
    inspected: list[dict[str, Any]] = []
    capability_key_by_flag = {
        "--search": "search",
        "--ephemeral": "exec_ephemeral",
        "--skip-git-repo-check": "exec_skip_git_repo_check",
        "--sandbox": "exec_sandbox",
        "--model": "exec_model",
        "--dangerously-bypass-approvals-and-sandbox": (
            "exec_dangerously_bypass_approvals_and_sandbox"
        ),
    }
    for candidate in codex_binary_candidates():
        details = inspect_codex_binary(candidate["binary"])
        details["source"] = candidate["source"]
        inspected.append(details)
        missing = [
            flag
            for flag in required
            if not bool(details["capabilities"].get(capability_key_by_flag.get(flag, flag)))
        ]
        if not missing:
            return {
                **details,
                "available": True,
                "missing": [],
                "rejected_candidates": rejected,
            }
        rejected.append({
            "binary": details["binary"],
            "source": details["source"],
            "version": details["version"],
            "missing": missing,
        })
    fallback = inspected[0] if inspected else {
        "binary": "",
        "source": "missing",
        "version": "",
        "capabilities": {},
        "errors": [],
    }
    return {
        **fallback,
        "available": False,
        "missing": list(required),
        "rejected_candidates": rejected,
    }


@lru_cache(maxsize=8)
def inspect_deepseek_harness_binary(binary: str) -> dict[str, Any]:
    """Probe a DSH executable for the released non-interactive profile."""

    version = ""
    help_text = ""
    errors: list[str] = []
    for command, output_key in (
        ([binary, "--version"], "version"),
        ([binary, "--profile", "headless", "--help"], "headless_help"),
    ):
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{output_key}: {type(exc).__name__}: {exc}")
            continue
        output = ((completed.stdout or "") + (completed.stderr or "")).strip()
        if completed.returncode != 0:
            errors.append(f"{output_key}: exit {completed.returncode}")
            continue
        if output_key == "version":
            version = output.splitlines()[0] if output else ""
        else:
            help_text = output

    headless_profile = (
        "usage: dsh --profile headless" in help_text.lower()
        and "task" in help_text.lower()
    )
    missing = [] if headless_profile else ["--profile headless"]
    return {
        "binary": binary,
        "version": version,
        "available": bool(version and headless_profile and not errors),
        "capabilities": {"headless_profile": headless_profile},
        "missing": missing,
        "errors": errors,
    }


def resolve_deepseek_harness_runtime(
    *,
    configured_env_keys: tuple[str, ...] = _DEEPSEEK_CONFIGURED_BINARY_ENV_KEYS,
) -> dict[str, Any]:
    """Select the first configured/PATH DSH with the released headless profile."""

    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for env_key in configured_env_keys:
        binary = os.getenv(env_key, "").strip()
        if binary and binary not in seen:
            seen.add(binary)
            candidates.append((binary, "configured"))
    path_binary = resolve_harness_binary("deepseek")
    if path_binary and path_binary not in seen:
        candidates.append((path_binary, "path"))

    rejected: list[dict[str, Any]] = []
    for binary, source in candidates:
        details = {**inspect_deepseek_harness_binary(binary), "source": source}
        if details["available"]:
            return {**details, "rejected_candidates": rejected}
        rejected.append(details)

    if rejected:
        return {
            **rejected[0],
            "available": False,
            "source": "incompatible",
            "rejected_candidates": rejected,
        }
    return {
        "binary": "dsh",
        "version": "",
        "available": False,
        "source": "missing",
        "capabilities": {"headless_profile": False},
        "missing": ["dsh"],
        "errors": [],
        "rejected_candidates": [],
    }


def mark_harness_login(harness: str, *, config_dir: Path | None = None) -> None:
    """Record that `harness` has a connected account in its isolated dir.

    The CLIs store OAuth credentials in varied places (claude → `.claude.json`
    / keychain, codex → `auth.json`), so a file-presence check is unreliable.
    This explicit marker — kept in sync with the CLI's own `auth status` — is
    the authoritative signal that the author/smoke should use the isolated
    `CLAUDE_CONFIG_DIR` / `CODEX_HOME` instead of the ambient gateway env.
    """

    auth_dir = Path(config_dir) if config_dir is not None else harness_auth_dir(harness)
    try:
        auth_dir.mkdir(parents=True, exist_ok=True)
        (auth_dir / _HARNESS_LOGIN_MARKER).write_text("{}", encoding="utf-8")
    except OSError:
        pass


def clear_harness_login_marker(harness: str) -> None:
    for auth_dir in _harness_auth_read_dirs(harness):
        for marker in (_HARNESS_LOGIN_MARKER, _LEGACY_HARNESS_LOGIN_MARKER):
            try:
                (auth_dir / marker).unlink()
            except OSError:
                pass


def harness_login_present(harness: str) -> bool:
    """True when `harness_auth_dir(harness)` holds a connected account.

    Trusts the explicit login marker first (written on successful login and
    re-synced from `auth status`), then the CLI-native credential files.
    """

    return _existing_harness_login_dir(harness) is not None


def harness_subprocess_env(
    base_env: Any,
    *,
    harness: str,
    api_key: str | None = None,
    config_dir: str | None = None,
) -> dict[str, str]:
    """Build the env for a managed coding-harness subprocess.

    - When `config_dir` is given (e.g. the login endpoint), it is used
      verbatim. Otherwise an isolated `harness_auth_dir` is used iff we are
      managing auth — an explicit `api_key` or an existing login in that dir.
    - For claude, when managing auth, strip the gateway env vars (see
      `_CLAUDE_GATEWAY_ENV_VARS`) and inject `ANTHROPIC_API_KEY` when given.
    - For codex, inject `OPENAI_API_KEY` when given.
    - For DeepSeek Harness, inject `DEEPSEEK_API_KEY` and an isolated
      `DSH_HOME` when given, while preserving ambient DeepSeek setup otherwise.
    - When we are NOT managing auth (no key, no login, no explicit dir) the
      env is returned unchanged so existing setups keep working.
    """

    env = dict(base_env)
    legacy_prefix = "DESIGN_ANYTHING_"
    for name, value in tuple(env.items()):
        if not name.startswith(legacy_prefix):
            continue
        suffix = name[len(legacy_prefix):]
        if suffix.startswith(("IDENTITY_LOGO_AGENT", "PLANNER_AUTHOR")):
            continue
        env.setdefault(f"AUTODESIGN_{suffix}", value)
    planner_prefix = "DESIGN_ANYTHING_PLANNER_AUTHOR"
    for name, value in tuple(env.items()):
        if name == planner_prefix or name.startswith(f"{planner_prefix}_"):
            suffix = name[len(planner_prefix):]
            env.setdefault(f"AUTODESIGN_DESIGNER_AUTHOR{suffix}", value)
    openresearch_prefix = "OPEN_DESIGN_OPENRESEARCH"
    for name, value in tuple(env.items()):
        if name == openresearch_prefix or name.startswith(f"{openresearch_prefix}_"):
            suffix = name[len(openresearch_prefix):]
            env.setdefault(f"AUTODESIGN_OPENRESEARCH{suffix}", value)
    harness = (harness or "").strip().lower()
    api_key = (api_key or "").strip()
    explicit_dir = (config_dir or "").strip()
    if harness == "deepseek":
        env.setdefault("DSH_PERMISSION_MODE", "workspace-write")
        env.setdefault("DSH_TELEMETRY_DISABLED", "1")
        if api_key:
            env["DEEPSEEK_API_KEY"] = api_key
            env["DSH_HOME"] = explicit_dir or str(harness_auth_dir(harness))
        elif explicit_dir:
            env["DSH_HOME"] = explicit_dir
        return env
    if harness not in {"claude", "codex"}:
        return env

    login_dir = _existing_harness_login_dir(harness)
    resolved_dir = explicit_dir
    if not resolved_dir and api_key:
        resolved_dir = str(harness_auth_dir(harness))
    elif not resolved_dir and login_dir is not None:
        resolved_dir = str(login_dir)
    managing = bool(api_key or resolved_dir)
    if harness == "codex" and not api_key:
        _drop_unreachable_loopback_proxies(env)
    if not managing:
        return env

    if harness == "claude":
        for var in _CLAUDE_GATEWAY_ENV_VARS:
            env.pop(var, None)
        if resolved_dir:
            env["CLAUDE_CONFIG_DIR"] = resolved_dir
        if api_key:
            env["ANTHROPIC_API_KEY"] = api_key
    else:  # codex
        if resolved_dir:
            env["CODEX_HOME"] = resolved_dir
        if api_key:
            env["OPENAI_API_KEY"] = api_key
    return env


def _drop_unreachable_loopback_proxies(env: dict[str, str]) -> None:
    """Ignore stale local proxy variables for ChatGPT-authenticated Codex."""

    endpoints: dict[tuple[str, int], list[str]] = {}
    for name in _PROXY_ENV_NAMES:
        raw = str(env.get(name) or "").strip()
        if not raw:
            continue
        parsed = urlsplit(raw if "://" in raw else f"http://{raw}")
        host = (parsed.hostname or "").lower()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            continue
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            continue
        endpoints.setdefault((host, port), []).append(name)

    for endpoint, names in endpoints.items():
        try:
            connection = socket.create_connection(endpoint, timeout=0.1)
        except OSError:
            for name in names:
                env.pop(name, None)
        else:
            connection.close()


def _parse_designer_author_mode(raw: str | None, *, default: DesignerAuthorMode = "internal") -> DesignerAuthorMode:
    value = (raw or "").strip().lower()
    if value in {"internal", "external"}:
        return value  # type: ignore[return-value]
    return default


def _parse_designer_author_harness(
    raw: str | None,
    *,
    default: DesignerAuthorHarness = "custom",
) -> DesignerAuthorHarness:
    value = (raw or "").strip().lower().replace("_", "-")
    aliases = {
        "": default,
        "custom": "custom",
        "cmd": "custom",
        "command": "custom",
        "codex": "codex",
        "codex-cli": "codex",
        "claude": "claude",
        "claude-code": "claude",
        "cloud-code": "claude",
        "deepseek": "deepseek",
        "deepseek-harness": "deepseek",
        "dsh": "deepseek",
        "opencode": "opencode",
        "open-code": "opencode",
        "kimi": "kimi",
        "kimi-code": "kimi",
        "kimicode": "kimi",
        "moonshot": "kimi",
        "mimo": "mimo",
        "mimo-code": "mimo",
        "mimocode": "mimo",
        "xiaomi-mimo": "mimo",
        "xiaomimimo": "mimo",
        "pi": "pi",
        "pi-coding-agent": "pi",
        "zcode": "zcode",
        "z-code": "zcode",
        "zai-code": "zcode",
        "z-ai-code": "zcode",
    }
    return aliases.get(value, default)  # type: ignore[return-value]


def _parse_identity_logo_agent_mode(
    raw: str | None,
    *,
    default: IdentityLogoAgentMode = "off",
) -> IdentityLogoAgentMode:
    value = (raw or "").strip().lower()
    aliases = {
        "": default,
        "auto": "auto",
        "on": "auto",
        "enabled": "auto",
        "1": "auto",
        "true": "auto",
        "required": "required",
        "strict": "required",
        "off": "off",
        "disabled": "off",
        "0": "off",
        "false": "off",
        "no": "off",
    }
    return aliases.get(value, default)  # type: ignore[return-value]


def _parse_identity_logo_agent_harness(
    raw: str | None,
    *,
    default: IdentityLogoAgentHarness = "codex",
) -> IdentityLogoAgentHarness:
    return _parse_designer_author_harness(raw, default=default)  # type: ignore[return-value]


def _parse_openresearch_submitter_mode(
    raw: str | None,
    *,
    default: OpenResearchSubmitterMode = "off",
) -> OpenResearchSubmitterMode:
    value = (raw or "").strip().lower()
    aliases = {
        "": default,
        "off": "off",
        "disabled": "off",
        "0": "off",
        "false": "off",
        "no": "off",
        "custom": "custom",
        "cmd": "custom",
        "command": "custom",
        "on": "custom",
        "enabled": "custom",
        "1": "custom",
        "true": "custom",
        "yes": "custom",
    }
    return aliases.get(value, default)  # type: ignore[return-value]


def _resolve_cmd_binary(
    env_key: str | tuple[str, ...],
    default_name: str,
    fallback_path: Path | None = None,
) -> str:
    env_keys = (env_key,) if isinstance(env_key, str) else env_key
    explicit = _env_first(*env_keys)
    if explicit:
        if (
            default_name == "codex"
            and explicit in {str(path) for path in _CODEX_APP_BINARY_CANDIDATES}
            and not (Path(explicit).exists() and os.access(explicit, os.X_OK))
        ):
            return resolve_harness_binary("codex") or explicit
        return explicit
    if default_name == "codex":
        resolved = resolve_harness_binary("codex")
        if resolved:
            return resolved
    resolved = shutil.which(default_name)
    if resolved:
        return resolved
    if fallback_path is not None and fallback_path.exists():
        return str(fallback_path)
    return default_name


def _kimi_code_agent_command(
    *,
    env_prefix: str | tuple[str, ...],
    model: str,
    prompt_file: str,
    task: str,
    target_files: list[str],
    done_file: str = "",
) -> str:
    wrapper = REPO_ROOT / "autodesign" / "agents" / "kimi_code_agent.py"
    cmd = [
        sys.executable,
        str(wrapper),
        "--kimi-bin",
        _resolve_cmd_binary(tuple(f"{prefix}_KIMI_BIN" for prefix in ((env_prefix,) if isinstance(env_prefix, str) else env_prefix)), "kimi"),
        "--prompt-file",
        prompt_file,
        "--task",
        task,
    ]
    for target_file in target_files:
        cmd.extend(["--target-file", target_file])
    if done_file:
        cmd.extend(["--done-file", done_file])
    if model:
        cmd.extend(["--model", model])
    return shlex.join(cmd)


def _deepseek_harness_agent_command(
    *,
    env_prefix: str | tuple[str, ...],
    model: str,
    prompt_file: str,
    task: str,
    target_files: list[str],
    done_file: str = "",
) -> str:
    wrapper = REPO_ROOT / "autodesign" / "agents" / "deepseek_harness_agent.py"
    prefixes = (env_prefix,) if isinstance(env_prefix, str) else env_prefix
    cmd = [
        sys.executable,
        str(wrapper),
        "--dsh-bin",
        _resolve_cmd_binary(tuple(f"{prefix}_DEEPSEEK_BIN" for prefix in prefixes), "dsh"),
        "--prompt-file",
        prompt_file,
        "--task",
        task,
    ]
    for target_file in target_files:
        cmd.extend(["--target-file", target_file])
    if done_file:
        cmd.extend(["--done-file", done_file])
    if model:
        cmd.extend(["--model", model])
    return shlex.join(cmd)


def _zcode_code_agent_command(
    *,
    env_prefix: str | tuple[str, ...],
    model: str,
    prompt_file: str,
    task: str,
    target_files: list[str],
    done_file: str = "",
) -> str:
    explicit = _prefixed_env_first(env_prefix, "ZCODE_CMD")
    if explicit:
        return explicit
    wrapper = REPO_ROOT / "autodesign" / "agents" / "zcode_code_agent.py"
    cmd = [
        sys.executable,
        str(wrapper),
        "--zcode-bin",
        _resolve_cmd_binary(tuple(f"{prefix}_ZCODE_BIN" for prefix in ((env_prefix,) if isinstance(env_prefix, str) else env_prefix)), "zcode"),
        "--prompt-file",
        prompt_file,
        "--task",
        task,
    ]
    for target_file in target_files:
        cmd.extend(["--target-file", target_file])
    if done_file:
        cmd.extend(["--done-file", done_file])
    if model:
        cmd.extend(["--model", model])
    mode = _prefixed_env_first(env_prefix, "ZCODE_MODE")
    if mode:
        cmd.extend(["--mode", mode])
    return shlex.join(cmd)


def _mimo_code_agent_command(
    *,
    env_prefix: str | tuple[str, ...],
    model: str,
    prompt_file: str,
    task: str,
    target_files: list[str],
    done_file: str = "",
) -> str:
    wrapper = REPO_ROOT / "autodesign" / "agents" / "mimo_code_agent.py"
    cmd = [
        sys.executable,
        str(wrapper),
        "--mimo-bin",
        _resolve_cmd_binary(tuple(f"{prefix}_MIMO_BIN" for prefix in ((env_prefix,) if isinstance(env_prefix, str) else env_prefix)), "mimo"),
        "--prompt-file",
        prompt_file,
        "--task",
        task,
    ]
    for target_file in target_files:
        cmd.extend(["--target-file", target_file])
    if done_file:
        cmd.extend(["--done-file", done_file])
    if model:
        cmd.extend(["--model", model])
    if _parse_bool_value(
        _prefixed_env_first(env_prefix, "MIMO_SKIP_PERMISSIONS"),
        default=True,
    ):
        cmd.append("--dangerously-skip-permissions")
    return shlex.join(cmd)


def _pi_code_agent_command(
    *,
    env_prefix: str | tuple[str, ...],
    model: str,
    prompt_file: str,
    task: str,
    target_files: list[str],
    done_file: str = "",
) -> str:
    wrapper = REPO_ROOT / "autodesign" / "agents" / "pi_code_agent.py"
    prefixes = (env_prefix,) if isinstance(env_prefix, str) else env_prefix
    cmd = [
        sys.executable,
        str(wrapper),
        "--pi-bin",
        _resolve_cmd_binary(tuple(f"{prefix}_PI_BIN" for prefix in prefixes), "pi"),
        "--prompt-file",
        prompt_file,
        "--task",
        task,
    ]
    config_dir = _env_first(*(f"{prefix}_PI_CONFIG_DIR" for prefix in prefixes))
    if config_dir:
        cmd.extend(["--config-dir", config_dir])
    for target_file in target_files:
        cmd.extend(["--target-file", target_file])
    if done_file:
        cmd.extend(["--done-file", done_file])
    if model:
        cmd.extend(["--model", model])
    if _parse_bool_value(
        _env_first(*(f"{prefix}_PI_APPROVE" for prefix in prefixes)),
        default=True,
    ):
        cmd.append("--approve")
    return shlex.join(cmd)


def _default_model_for_code_harness(harness: str) -> str:
    if harness == "kimi":
        return DEFAULT_KIMI_CODE_HARNESS_MODEL
    if harness == "zcode":
        return DEFAULT_ZCODE_HARNESS_MODEL
    return ""


def coding_agent_smoke_command_for_harness(
    harness: str | None,
    model: str | None = None,
) -> str:
    """Return a staged-file command for harnesses needing a smoke adapter."""

    resolved_harness = _parse_designer_author_harness(harness)
    if resolved_harness != "deepseek":
        return ""
    return _deepseek_harness_agent_command(
        env_prefix=("AUTODESIGN_CODE_EDITOR", "DESIGN_ANYTHING_CODE_EDITOR"),
        model=(model or "").strip(),
        prompt_file="coding_agent_smoke_prompt.md",
        task="AutoDesign coding-agent smoke test",
        target_files=["coding_agent_smoke_output.json"],
    )


def designer_author_command_for_harness(
    harness: str | None,
    model: str | None = None,
    *,
    explicit_cmd: str | None = None,
) -> str:
    """Return the subprocess command for a known external author harness."""

    explicit = (explicit_cmd or "").strip()
    if explicit:
        return explicit
    resolved_harness = _parse_designer_author_harness(harness)
    model = (model or "").strip()
    if not model:
        model = _default_model_for_code_harness(resolved_harness)
    if resolved_harness == "custom":
        return ""
    if resolved_harness == "codex":
        cmd = [
            _resolve_cmd_binary(
                (
                    "AUTODESIGN_DESIGNER_AUTHOR_CODEX_BIN",
                    "DESIGN_ANYTHING_DESIGNER_AUTHOR_CODEX_BIN",
                    "DESIGN_ANYTHING_PLANNER_AUTHOR_CODEX_BIN",
                ),
                "codex",
            ),
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
            "-C",
            ".",
        ]
        if model:
            cmd.extend(["--model", model])
        return shlex.join(cmd)
    if resolved_harness == "claude":
        return _claude_prompt_file_command(
            _resolve_cmd_binary(
                (
                    "AUTODESIGN_DESIGNER_AUTHOR_CLAUDE_BIN",
                    "DESIGN_ANYTHING_DESIGNER_AUTHOR_CLAUDE_BIN",
                    "DESIGN_ANYTHING_PLANNER_AUTHOR_CLAUDE_BIN",
                ),
                "claude",
            ),
            model=model,
        )
    if resolved_harness == "deepseek":
        return _deepseek_harness_agent_command(
            env_prefix=(
                "AUTODESIGN_DESIGNER_AUTHOR",
                "DESIGN_ANYTHING_DESIGNER_AUTHOR",
                "DESIGN_ANYTHING_PLANNER_AUTHOR",
            ),
            model=model,
            prompt_file="designer_author_prompt.md",
            task="AutoDesign external designer-author poster generation",
            target_files=["poster.html"],
            done_file="designer_author_done.json",
        )
    if resolved_harness == "opencode":
        wrapper = REPO_ROOT / "autodesign" / "agents" / "opencode_designer_author.py"
        cmd = [
            sys.executable,
            str(wrapper),
            "--opencode-bin",
            _resolve_cmd_binary(
                (
                    "AUTODESIGN_DESIGNER_AUTHOR_OPENCODE_BIN",
                    "DESIGN_ANYTHING_DESIGNER_AUTHOR_OPENCODE_BIN",
                    "DESIGN_ANYTHING_PLANNER_AUTHOR_OPENCODE_BIN",
                ),
                "opencode",
            ),
        ]
        if model:
            cmd.extend(["--model", model])
        if _parse_bool_value(
            _env_first(
                "AUTODESIGN_DESIGNER_AUTHOR_OPENCODE_SKIP_PERMISSIONS",
                "DESIGN_ANYTHING_DESIGNER_AUTHOR_OPENCODE_SKIP_PERMISSIONS",
                "DESIGN_ANYTHING_PLANNER_AUTHOR_OPENCODE_SKIP_PERMISSIONS",
            ),
            default=True,
        ):
            cmd.append("--dangerously-skip-permissions")
        return shlex.join(cmd)
    designer_author_env_prefixes = (
        "AUTODESIGN_DESIGNER_AUTHOR",
        "DESIGN_ANYTHING_DESIGNER_AUTHOR",
        "DESIGN_ANYTHING_PLANNER_AUTHOR",
    )
    if resolved_harness == "kimi":
        return _kimi_code_agent_command(
            env_prefix=designer_author_env_prefixes,
            model=model,
            prompt_file="designer_author_prompt.md",
            task="AutoDesign external designer-author poster generation",
            target_files=["poster.html"],
            done_file="designer_author_done.json",
        )
    if resolved_harness == "mimo":
        return _mimo_code_agent_command(
            env_prefix=designer_author_env_prefixes,
            model=model,
            prompt_file="designer_author_prompt.md",
            task="AutoDesign external designer-author poster generation",
            target_files=["poster.html"],
            done_file="designer_author_done.json",
        )
    if resolved_harness == "pi":
        return _pi_code_agent_command(
            env_prefix=designer_author_env_prefixes,
            model=model,
            prompt_file="designer_author_prompt.md",
            task="AutoDesign external designer-author poster generation",
            target_files=["poster.html"],
            done_file="designer_author_done.json",
        )
    if resolved_harness == "zcode":
        return _zcode_code_agent_command(
            env_prefix=designer_author_env_prefixes,
            model=model,
            prompt_file="designer_author_prompt.md",
            task="AutoDesign external designer-author poster generation",
            target_files=["poster.html"],
            done_file="designer_author_done.json",
        )
    return ""


def artifact_author_command_for_harness(
    harness: str | None,
    *,
    artifact_type: str,
    model: str | None = None,
    explicit_cmd: str | None = None,
) -> str:
    """Build a coding-agent command for one artifact-specific author harness.

    Poster remains byte-for-byte on the established command path. The other
    artifact harnesses use their own prompt and output filenames while sharing
    only the local coding-agent transport.
    """

    explicit = (explicit_cmd or "").strip()
    if explicit:
        return explicit
    artifact = str(artifact_type or "").strip().lower()
    if artifact == "poster":
        return designer_author_command_for_harness(harness, model)
    contracts = {
        "landing": (
            "landing_author_prompt.md",
            ["index.html"],
            "AutoDesign external landing-page authoring",
        ),
        "deck": (
            "slides_author_prompt.md",
            ["slides.html"],
            "AutoDesign external HTML slides authoring",
        ),
        "video": (
            "video_author_prompt.md",
            ["project/index.html", "video_author_manifest.json"],
            "AutoDesign external conference-video project authoring",
        ),
    }
    if artifact not in contracts:
        return ""
    prompt_file, target_files, task = contracts[artifact]
    resolved_harness = _parse_designer_author_harness(harness)
    resolved_model = (model or "").strip() or _default_model_for_code_harness(resolved_harness)
    if resolved_harness == "custom":
        return ""
    if resolved_harness == "codex":
        return designer_author_command_for_harness("codex", resolved_model)
    if resolved_harness == "claude":
        return _claude_artifact_prompt_file_command(
            _resolve_cmd_binary(
                (
                    "AUTODESIGN_DESIGNER_AUTHOR_CLAUDE_BIN",
                    "DESIGN_ANYTHING_DESIGNER_AUTHOR_CLAUDE_BIN",
                    "DESIGN_ANYTHING_PLANNER_AUTHOR_CLAUDE_BIN",
                ),
                "claude",
            ),
            resolved_model,
            prompt_file=prompt_file,
            target_files=target_files,
            done_file="designer_author_done.json",
        )
    env_prefixes = (
        "AUTODESIGN_DESIGNER_AUTHOR",
        "DESIGN_ANYTHING_DESIGNER_AUTHOR",
        "DESIGN_ANYTHING_PLANNER_AUTHOR",
    )
    if resolved_harness == "deepseek":
        return _deepseek_harness_agent_command(
            env_prefix=env_prefixes,
            model=resolved_model,
            prompt_file=prompt_file,
            task=task,
            target_files=target_files,
            done_file="designer_author_done.json",
        )
    if resolved_harness == "kimi":
        return _kimi_code_agent_command(
            env_prefix=env_prefixes,
            model=resolved_model,
            prompt_file=prompt_file,
            task=task,
            target_files=target_files,
            done_file="designer_author_done.json",
        )
    if resolved_harness == "mimo":
        return _mimo_code_agent_command(
            env_prefix=env_prefixes,
            model=resolved_model,
            prompt_file=prompt_file,
            task=task,
            target_files=target_files,
            done_file="designer_author_done.json",
        )
    if resolved_harness == "pi":
        return _pi_code_agent_command(
            env_prefix=env_prefixes,
            model=resolved_model,
            prompt_file=prompt_file,
            task=task,
            target_files=target_files,
            done_file="designer_author_done.json",
        )
    if resolved_harness == "zcode":
        return _zcode_code_agent_command(
            env_prefix=env_prefixes,
            model=resolved_model,
            prompt_file=prompt_file,
            task=task,
            target_files=target_files,
            done_file="designer_author_done.json",
        )
    if resolved_harness == "opencode":
        wrapper = REPO_ROOT / "autodesign" / "agents" / "opencode_designer_author.py"
        command = [
            sys.executable,
            str(wrapper),
            "--opencode-bin",
            _resolve_cmd_binary(
                (
                    "AUTODESIGN_DESIGNER_AUTHOR_OPENCODE_BIN",
                    "DESIGN_ANYTHING_DESIGNER_AUTHOR_OPENCODE_BIN",
                    "DESIGN_ANYTHING_PLANNER_AUTHOR_OPENCODE_BIN",
                ),
                "opencode",
            ),
            "--prompt-file",
            prompt_file,
            "--done-file",
            "designer_author_done.json",
            "--task",
            task,
        ]
        for target_file in target_files:
            command.extend(["--target-file", target_file])
        if resolved_model:
            command.extend(["--model", resolved_model])
        command.append("--dangerously-skip-permissions")
        return shlex.join(command)
    return ""


def code_editor_command_for_harness(
    harness: str | None,
    model: str | None = None,
    *,
    explicit_cmd: str | None = None,
) -> str:
    """Return the subprocess command for multi-turn poster HTML revisions."""

    explicit = (explicit_cmd or "").strip()
    if explicit:
        return explicit
    resolved_harness = _parse_designer_author_harness(harness)
    model = (model or "").strip()
    if not model:
        model = _default_model_for_code_harness(resolved_harness)
    if resolved_harness == "custom":
        return ""
    if resolved_harness == "codex":
        cmd = [
            _resolve_cmd_binary(
                ("AUTODESIGN_CODE_EDITOR_CODEX_BIN", "DESIGN_ANYTHING_CODE_EDITOR_CODEX_BIN"),
                "codex",
            ),
            "--search",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "-",
        ]
        if model:
            cmd.extend(["--model", model])
        return shlex.join(cmd)
    if resolved_harness == "claude":
        return _claude_prompt_file_command(
            _resolve_cmd_binary(
                ("AUTODESIGN_CODE_EDITOR_CLAUDE_BIN", "DESIGN_ANYTHING_CODE_EDITOR_CLAUDE_BIN"),
                "claude",
            ),
            model=model,
        )
    if resolved_harness == "deepseek":
        return _deepseek_harness_agent_command(
            env_prefix=("AUTODESIGN_CODE_EDITOR", "DESIGN_ANYTHING_CODE_EDITOR"),
            model=model,
            prompt_file="edit_prompt.md",
            task="AutoDesign paper-poster code-editor revision",
            target_files=["poster.html"],
            done_file="code_editor_done.json",
        )
    if resolved_harness == "opencode":
        wrapper = REPO_ROOT / "autodesign" / "agents" / "opencode_code_editor.py"
        cmd = [
            sys.executable,
            str(wrapper),
            "--opencode-bin",
            _resolve_cmd_binary(
                ("AUTODESIGN_CODE_EDITOR_OPENCODE_BIN", "DESIGN_ANYTHING_CODE_EDITOR_OPENCODE_BIN"),
                "opencode",
            ),
        ]
        if model:
            cmd.extend(["--model", model])
        if _parse_bool_value(
            _project_env_first("CODE_EDITOR_OPENCODE_SKIP_PERMISSIONS"),
            default=True,
        ):
            cmd.append("--dangerously-skip-permissions")
        return shlex.join(cmd)
    if resolved_harness == "kimi":
        return _kimi_code_agent_command(
            env_prefix=("AUTODESIGN_CODE_EDITOR", "DESIGN_ANYTHING_CODE_EDITOR"),
            model=model,
            prompt_file="edit_prompt.md",
            task="AutoDesign paper-poster code-editor revision",
            target_files=["poster.html"],
            done_file="code_editor_done.json",
        )
    if resolved_harness == "mimo":
        return _mimo_code_agent_command(
            env_prefix=("AUTODESIGN_CODE_EDITOR", "DESIGN_ANYTHING_CODE_EDITOR"),
            model=model,
            prompt_file="edit_prompt.md",
            task="AutoDesign paper-poster code-editor revision",
            target_files=["poster.html"],
            done_file="code_editor_done.json",
        )
    if resolved_harness == "pi":
        return _pi_code_agent_command(
            env_prefix=("AUTODESIGN_CODE_EDITOR", "DESIGN_ANYTHING_CODE_EDITOR"),
            model=model,
            prompt_file="edit_prompt.md",
            task="AutoDesign paper-poster code-editor revision",
            target_files=["poster.html"],
            done_file="code_editor_done.json",
        )
    if resolved_harness == "zcode":
        return _zcode_code_agent_command(
            env_prefix=("AUTODESIGN_CODE_EDITOR", "DESIGN_ANYTHING_CODE_EDITOR"),
            model=model,
            prompt_file="edit_prompt.md",
            task="AutoDesign paper-poster code-editor revision",
            target_files=["poster.html"],
            done_file="code_editor_done.json",
        )
    return ""


def identity_logo_agent_command_for_harness(
    harness: str | None,
    model: str | None = None,
    *,
    explicit_cmd: str | None = None,
) -> str:
    """Return the subprocess command for the identity-logo discovery agent."""

    explicit = (explicit_cmd or "").strip()
    if explicit:
        return explicit
    resolved_harness = _parse_identity_logo_agent_harness(harness)
    model = (model or "").strip()
    if resolved_harness == "custom":
        return ""
    if resolved_harness == "codex":
        cmd = [
            _resolve_cmd_binary(
                "DESIGN_ANYTHING_IDENTITY_LOGO_AGENT_CODEX_BIN",
                "codex",
            ),
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
            "-C",
            ".",
        ]
        if model:
            cmd.extend(["--model", model])
        return shlex.join(cmd)
    if resolved_harness == "claude":
        return _claude_prompt_file_command(
            _resolve_cmd_binary("DESIGN_ANYTHING_IDENTITY_LOGO_AGENT_CLAUDE_BIN", "claude"),
            model=model,
        )
    if resolved_harness == "deepseek":
        return _deepseek_harness_agent_command(
            env_prefix="DESIGN_ANYTHING_IDENTITY_LOGO_AGENT",
            model=model,
            prompt_file="identity_logo_prompt.md",
            task="AutoDesign identity-logo candidate discovery",
            target_files=["identity_logo_candidates.json"],
        )
    if resolved_harness == "opencode":
        wrapper = REPO_ROOT / "autodesign" / "agents" / "opencode_identity_logo_agent.py"
        cmd = [
            sys.executable,
            str(wrapper),
            "--opencode-bin",
            _resolve_cmd_binary("DESIGN_ANYTHING_IDENTITY_LOGO_AGENT_OPENCODE_BIN", "opencode"),
        ]
        if model:
            cmd.extend(["--model", model])
        if _parse_bool_value(
            os.getenv("DESIGN_ANYTHING_IDENTITY_LOGO_AGENT_OPENCODE_SKIP_PERMISSIONS", ""),
            default=True,
        ):
            cmd.append("--dangerously-skip-permissions")
        return shlex.join(cmd)
    if resolved_harness == "kimi":
        return _kimi_code_agent_command(
            env_prefix="DESIGN_ANYTHING_IDENTITY_LOGO_AGENT",
            model=model,
            prompt_file="identity_logo_prompt.md",
            task="AutoDesign identity-logo candidate discovery",
            target_files=["identity_logo_candidates.json"],
        )
    if resolved_harness == "mimo":
        return _mimo_code_agent_command(
            env_prefix="DESIGN_ANYTHING_IDENTITY_LOGO_AGENT",
            model=model,
            prompt_file="identity_logo_prompt.md",
            task="AutoDesign identity-logo candidate discovery",
            target_files=["identity_logo_candidates.json"],
        )
    if resolved_harness == "pi":
        return _pi_code_agent_command(
            env_prefix="DESIGN_ANYTHING_IDENTITY_LOGO_AGENT",
            model=model,
            prompt_file="identity_logo_prompt.md",
            task="AutoDesign identity-logo candidate discovery",
            target_files=["identity_logo_candidates.json"],
        )
    if resolved_harness == "zcode":
        return _zcode_code_agent_command(
            env_prefix="DESIGN_ANYTHING_IDENTITY_LOGO_AGENT",
            model=model,
            prompt_file="identity_logo_prompt.md",
            task="AutoDesign identity-logo candidate discovery",
            target_files=["identity_logo_candidates.json"],
        )
    return ""


def effective_poster_harness_mode(settings: Any | None = None) -> str:
    """Return the current per-run poster harness mode.

    Explicit process env still wins for CLI/test overrides. Normal app/API
    calls should pass the immutable per-run Settings object so concurrent user
    runs do not depend on process-global env after settings resolution.
    """

    raw_env = _project_env_first(
        "POSTER_HARNESS_MODE",
        unprefixed=("POSTER_HARNESS_MODE",),
    )
    if raw_env:
        return _parse_poster_harness_mode(raw_env)
    if settings is not None:
        return _parse_poster_harness_mode(
            getattr(settings, "poster_harness_mode", None),
        )
    return DEFAULT_USER_POSTER_HARNESS_MODE


_AUTHORING_MAX_ATTEMPTS_DEFAULTS: dict[str, int] = {
    "poster": 12,
    "deck": 12,
    "slides": 12,
    "landing": 4,
    "video": 4,
}


def authoring_max_attempts_for(settings: Any, artifact_type: str) -> int:
    """Resolve the per-invocation external authoring budget."""

    explicit = getattr(settings, "authoring_max_attempts_override", None)
    if explicit is not None:
        return max(1, int(explicit))
    legacy = int(getattr(settings, "designer_author_max_attempts", 12) or 12)
    if legacy != 12:
        return max(1, legacy)
    return _AUTHORING_MAX_ATTEMPTS_DEFAULTS.get(str(artifact_type).lower(), 12)


@dataclass(frozen=True)
class Settings:
    # Anthropic credentials (also reused as OpenRouter creds when in OR mode)
    anthropic_api_key: str
    anthropic_base_url: str | None              # None → stock Anthropic endpoint

    # NBP (Gemini) credential — only required when image_provider resolves
    # to gemini. Empty string is fine when the user runs seedream / any
    # other OpenRouter image model. Validated lazily inside
    # `GeminiImageBackend.__init__` so an unset key on the seedream path
    # doesn't crash startup.
    gemini_api_key: str

    # Per-role model + provider selection
    designer_model: str
    critic_model: str
    anthropic_auth_token: str | None = None
    anthropic_custom_headers: dict[str, str] = field(default_factory=dict)
    designer_provider: ProviderChoice = "auto"
    critic_provider: ProviderChoice = "auto"

    # v2.4 Prompt Enhancer stage — runs before designer.start.
    # `enable_prompt_enhancer` gates the whole stage; the `--skip-enhancer`
    # CLI flag sets it to False per-run.
    enhancer_model: str = DEFAULT_ENHANCER_MODEL
    enhancer_provider: ProviderChoice = "auto"
    enhancer_thinking_budget: int = 10000
    enable_prompt_enhancer: bool = True

    # v2.8.0 ClaimGraph extractor stage — runs between the enhancer and
    # the planner whenever the brief attaches a PDF. `claim_graph_max_turns`
    # caps the sub-agent's loop in case the model never calls
    # `report_claim_graph`; on hit we synthesize a sentinel graph and the
    # runner drops it back to None so the planner degrades to v2.7.3
    # chapter-order behavior. `enable_claim_graph` gates the whole stage;
    # the `--no-claim-graph` CLI flag sets it to False per-run.
    claim_graph_model: str = DEFAULT_CLAIM_GRAPH_MODEL
    claim_graph_provider: ProviderChoice = "auto"
    claim_graph_max_turns: int = 15
    claim_graph_thinking_budget: int = 8000
    enable_claim_graph: bool = True

    # DeckOutlineAgent — runs inside ingest_document for deck requests with
    # attached source docs and no exact user slide-count lock.
    deck_outline_model: str = DEFAULT_DECK_OUTLINE_MODEL
    deck_outline_provider: ProviderChoice = "auto"
    deck_outline_max_turns: int = 8
    deck_outline_thinking_budget: int = 6000
    enable_deck_outline: bool = True

    # PaperMemoryAgent — curates a validated panel-ready evidence dossier over
    # canonical paper_memory chunks. Fails open to deterministic retrieval.
    paper_memory_model: str = DEFAULT_PAPER_MEMORY_MODEL
    paper_memory_provider: ProviderChoice = "auto"
    paper_memory_max_turns: int = 6
    paper_memory_thinking_budget: int = 4000
    enable_paper_memory_agent: bool = True

    # v2.8.1 HyperFrames Composer stage — single-turn LLM call that writes
    # `index.html` inside the HyperFrames project directory created by
    # `export_video`. Runs automatically after the scaffold step so the
    # video pipeline is fully end-to-end without a manual sub-call.
    # `enable_video_composer` gates the stage; `SKIP_VIDEO_COMPOSER=1`
    # disables it per-run when the user wants to author index.html manually.
    composer_model: str = DEFAULT_COMPOSER_MODEL
    composer_provider: ProviderChoice = "auto"
    enable_video_composer: bool = True

    # Artifact-specific designer author replacement. When set to "external",
    # the runner selects an independent Poster, Landing, Slides, or Video
    # coding-agent harness. Each harness owns its validation/repair contract;
    # only the local coding-agent transport configuration is shared.
    designer_author_mode: DesignerAuthorMode = "internal"
    designer_author_harness: DesignerAuthorHarness = "custom"
    designer_author_cmd: str = ""
    designer_author_model: str | None = None
    designer_author_timeout_s: int = 1800
    # Safety budget for the external author repair-until-pass loop. Validation
    # success is the normal stop condition; this only prevents unbounded loops.
    designer_author_max_attempts: int = 12
    # Explicit cross-artifact override. None keeps the quality-weighted
    # defaults (poster/deck 12, landing/video 4).
    authoring_max_attempts_override: int | None = None
    designer_author_poster_stable_s: float = 8.0

    # Optional explicit API key for the local coding-agent harness (claude →
    # ANTHROPIC_API_KEY, codex → OPENAI_API_KEY). Shared across coding-agent
    # harness surfaces because the Web UI binds them to one harness pick.
    # Distinct from the pipeline provider credentials: this only authenticates the CLI subprocess
    # and is injected via `harness_subprocess_env`. Empty → rely on the
    # harness CLI's own login (see `harness_auth_dir`).
    harness_api_key: str | None = None

    # Multi-turn paper-poster editor. This is intentionally separate from
    # the first-pass designer-author path: it edits an existing poster.html
    # while AutoDesign keeps staging, validation, preview, and promotion.
    code_editor_harness: CodeEditorHarness = "codex"
    code_editor_cmd: str = ""
    code_editor_model: str | None = None
    code_editor_timeout_s: int = 600
    code_editor_max_attempts: int = 2

    # Deprecated no-op compatibility for older identity-logo settings.
    # Paper-poster ingest no longer builds or stages academic identity assets.
    identity_logo_agent_mode: IdentityLogoAgentMode = "off"
    identity_logo_agent_harness: IdentityLogoAgentHarness = "codex"
    identity_logo_agent_cmd: str = ""
    identity_logo_agent_model: str | None = None
    identity_logo_agent_timeout_s: int = 240
    identity_logo_agent_max_entities: int = 6
    identity_logo_agent_max_candidates: int = 12

    # OpenResearch GUI submission. AutoDesign only stages paper context and
    # calls a configured GUI harness; OpenResearch owns reproduction work.
    openresearch_api_url: str = "https://api.openresearch.sh"
    openresearch_token: str = ""
    openresearch_timeout_s: int = 120
    openresearch_org_id: str = ""
    openresearch_default_repo_full_name: str = ""
    openresearch_submitter_mode: OpenResearchSubmitterMode = "off"
    openresearch_submitter_cmd: str = ""
    openresearch_submitter_timeout_s: int = 300

    # User-facing and direct Settings(...) generation use the same highest-quality
    # paper-poster route by default. Lower-cost modes are explicit eval/debug
    # overrides, not separate product pipelines.
    poster_harness_mode: str = DEFAULT_USER_POSTER_HARNESS_MODE

    # OpenAI-compat backend connection (used when provider resolves to openai_compat)
    openai_compat_api_key: str | None = None    # falls back to anthropic_api_key when OR
    openai_compat_base_url: str = OPENROUTER_BASE_URL_OPENAI
    llm_http_timeout: float = 180.0
    # Web transport sets these to False for hosted/public-user-isolated
    # requests. CLI and loopback installs keep local provider compatibility.
    allow_private_network: bool = True
    allow_remote_image_urls: bool = True

    # OpenRouter key kept separate for the v1.2 ingest VLM path (util/vlm.py)
    openrouter_api_key: str | None = None

    # v2.5 multi-provider image generation. `image_model` follows the same
    # auto-detect rule as planner/critic: `gemini-*` / `imagen-*` route to
    # the GeminiImageBackend; everything else routes to OpenRouter
    # (chat/completions with modalities=["image","text"]). Users override
    # with `IMAGE_MODEL=openai/gpt-5-image-mini` (etc) or pin a provider via
    # `IMAGE_PROVIDER=auto|gemini|openrouter|openai_compat`.
    #
    # v2.7.5 (2026-04-26) — default switched away from
    # `bytedance-seed/seedream-4.5` after a real dogfood run hit
    # `404 - No endpoints found that support the requested output
    # modalities: image, text` from OpenRouter (run
    # `20260426-142635-b46e3e51`, 4 consecutive failures → text-only deck
    # cascade regression). Seedream lost its image-modality endpoint on
    # OpenRouter; verified live against the v2.7.5 candidate list.
    # `google/gemini-2.5-flash-image` via OpenRouter is the new default —
    # confirmed 200 OK with a one-shot probe (8s, ~$0.0003/image) and
    # accessible through the same `OPENROUTER_API_KEY` plumbing, so no
    # new credential required.
    image_model: str = "google/gemini-2.5-flash-image"
    image_provider: ImageProviderChoice = "auto"

    # v2.7.5 hardcoded fallback model — used by `image_backend.generate(...)`
    # when the user's `image_model` returns 404 / no-endpoints-for-modality
    # / model-not-found. Different vendor than the default so a
    # Google-side outage doesn't take both down. Picked
    # `openai/gpt-5-image-mini` (different vendor, ~$0.000002/image,
    # ~38s cold latency — slow but reliable). User-overridable via env
    # `IMAGE_FALLBACK_MODEL=...`; set to empty string to disable the
    # fallback entirely (fail-loud on first 404 instead of attempting
    # the second model).
    image_fallback_model: str = "openai/gpt-5-image-mini"

    # v1.2 paper2any: VLM used by ingest_document
    ingest_model: str = "gpt-5.4-nano"
    ingest_http_timeout: float = 600.0

    repo_root: Path = REPO_ROOT
    fonts_dir: Path = REPO_ROOT / "assets" / "fonts"
    prompts_dir: Path = REPO_ROOT / "prompts"
    skills_dir: Path = REPO_ROOT / "skills"
    out_dir: Path = REPO_ROOT / "out"

    max_critique_iters: int = 11
    max_designer_turns: int = 30
    max_env_repair_attempts: int = 1
    enable_skills: bool = True
    critic_preview_max_edge: int = 1024
    poster_preview_max_edge: int = 2048

    # v2.7.2 deck section-number policy — applied inside `_composite_deck`
    # before deck composition, so renderer + apply-edits both see consistent
    # numbering. "renumber" (default) walks slides in order, assigns §1,
    # §1.1, §2, ... using a sub-rhythm heuristic; "strip" clears every
    # SlideNode.section_number; "preserve" passes the planner's values
    # through unchanged. Override per-run via `SECTION_NUMBER_POLICY=...`.
    section_number_policy: SectionNumberPolicy = "renumber"

    # v2.7.3 — Vision critic sub-agent (CriticAgent in
    # autodesign/agents/critic_agent.py). The critic now runs as an
    # independent loop with its own LLMBackend instance and turn budget.
    # `critic_max_turns` caps the
    # sub-agent's loop in case the model never calls `report_verdict`;
    # on hit we force-emit a fail verdict rather than recurse forever.
    # `max_critique_iters` above is the planner-side cap on how many times the
    # planner spawns the sub-agent per run (one CriticAgent invocation == one
    # review round). For external paper-poster authoring, the default is one less
    # than `designer_author_max_attempts`: critic feedback is useful between
    # attempts, but the final attempt should deliver the best available poster.
    critic_max_turns: int = 10

    # v2.7.3 hotfix (2026-04-26) — cap how many slide PNGs the critic
    # may pull into a single turn via `read_slide_render`. The 153K-token
    # context blow-up on longcat-next-2026.pdf came from a 13-slide deck
    # being fetched in parallel and the JSON-encoded base64 leaking into
    # subsequent turns as plain `tool` messages. We now deliver each
    # PNG as a real vision content block on a follow-up user message,
    # but the per-turn cap defends against the model still trying to
    # haul the whole deck in one go (Qwen-VL-Max bills ~1k image tokens
    # each; 4 per turn ≈ 4k image tokens + the small ack JSONs).
    # Surplus calls return a "deferred" ack and re-queue for the next
    # turn so the model learns to chunk its inspection.
    critic_max_images_per_turn: int = 4

    # Extended thinking — applies to BOTH backends (Anthropic uses thinking=
    # block; OpenAI-compat uses extra_body.reasoning.max_tokens for OpenRouter
    # unified format). budget=0 disables thinking entirely.
    designer_thinking_budget: int = 10000
    critic_thinking_budget: int = 10000
    # Anthropic-only: interleaved-thinking-2025-05-14 beta header. No-op for
    # OpenAI-compat backends (reasoning is naturally per-turn there).
    enable_interleaved_thinking: bool = True

    # v2.4.2 — optional local fonts. Flat `family → filename` for back-compat
    # with every downstream lookup (`settings.fonts.get(family)`). Families
    # with a single file ship their "-Variable.ttf" wght-axis master so
    # CSS `font-weight` picks the right cut; the legacy CJK `-Bold.otf`
    # entries are kept for the PSD/SVG/PNG path where PIL doesn't honour
    # the OpenType wght axis.
    #
    # When you add a family here, also extend the typography section of
    # `prompts/designer.md` so the planner knows it exists.
    fonts: dict[str, str] = field(default_factory=lambda: {
        # Legacy local CJK bold (used when installed for PIL rasterization).
        "NotoSansSC-Bold": "NotoSansSC-Bold.otf",
        "NotoSerifSC-Bold": "NotoSerifSC-Bold.otf",
        # Optional variable CJK — all weights in one file for HTML/SVG.
        "NotoSansSC": "NotoSansSC-Variable.ttf",
        "NotoSerifSC": "NotoSerifSC-Variable.ttf",
        # Optional local Latin masters; system-font fallbacks remain supported.
        "Inter": "Inter-Variable.ttf",
        "IBMPlexSans": "IBMPlexSans-Variable.ttf",
        "JetBrainsMono": "JetBrainsMono-Regular.ttf",  # variable wght axis
        "PlayfairDisplay": "PlayfairDisplay-Variable.ttf",
    })
    default_text_font: str = "NotoSansSC"
    default_title_font: str = "NotoSerifSC"

    @property
    def llm_backend(self) -> str:
        """Legacy convenience field — describes the underlying credential
        path, NOT the active provider per-role (use `designer_provider` /
        `critic_provider` for that)."""
        return "openrouter" if self.anthropic_base_url else "anthropic"

    @property
    def planner_model(self) -> str:
        return self.designer_model

    @property
    def planner_provider(self) -> ProviderChoice:
        return self.designer_provider

    @property
    def planner_author_mode(self) -> DesignerAuthorMode:
        return self.designer_author_mode

    @property
    def planner_author_harness(self) -> DesignerAuthorHarness:
        return self.designer_author_harness

    @property
    def planner_author_cmd(self) -> str:
        return self.designer_author_cmd

    @property
    def planner_author_model(self) -> str | None:
        return self.designer_author_model

    @property
    def planner_author_timeout_s(self) -> int:
        return self.designer_author_timeout_s

    @property
    def planner_author_max_attempts(self) -> int:
        return self.designer_author_max_attempts

    @property
    def planner_author_poster_stable_s(self) -> float:
        return self.designer_author_poster_stable_s

    @property
    def planner_thinking_budget(self) -> int:
        return self.designer_thinking_budget

    @property
    def max_planner_turns(self) -> int:
        return self.max_designer_turns


def load_settings() -> Settings:
    # Gemini key is now optional — only required when image_provider
    # resolves to `gemini`. We validate lazily inside the backend so a
    # seedream-only user doesn't need to set it. The startup check below
    # used to fail-loud here; that contract moved into
    # `GeminiImageBackend.__init__` (image_backend.py).
    gemini = _credential_env("GEMINI_API_KEY")

    ant_auth_token = _credential_env("ANTHROPIC_AUTH_TOKEN") or None
    or_key = _credential_env("OPENROUTER_API_KEY")
    ant_key = _credential_env("ANTHROPIC_API_KEY")
    explicit_oai_key = _credential_env("OPENAI_COMPAT_API_KEY")
    explicit_oai_base = os.getenv("OPENAI_COMPAT_BASE_URL", "").strip()
    base_url_override = os.getenv("ANTHROPIC_BASE_URL", "").strip() or None
    anthropic_custom_headers = _parse_custom_headers(
        os.getenv("ANTHROPIC_CUSTOM_HEADERS", ""),
    )

    # Anthropic SDK credential resolution — same as before. The OpenAI-compat
    # backend may use the same key (when in OR mode) or its own (next block).
    if or_key:
        api_key = or_key
        base_url = OPENROUTER_BASE_URL_ANTHROPIC
        anthropic_default_designer = OPENROUTER_DEFAULT_DESIGNER_MODEL
        anthropic_default_critic = DEFAULT_CRITIC_MODEL
    elif ant_key or ant_auth_token:
        api_key = ant_key
        base_url = base_url_override
        anthropic_default_designer = ANTHROPIC_FALLBACK_DESIGNER
        anthropic_default_critic = ANTHROPIC_FALLBACK_CRITIC
    elif explicit_oai_key:
        api_key = explicit_oai_key
        base_url = base_url_override
        anthropic_default_designer = DEFAULT_DESIGNER_MODEL
        anthropic_default_critic = DEFAULT_CRITIC_MODEL
    else:
        raise RuntimeError(
            "No LLM credential — set OPENROUTER_API_KEY (preferred, powers both "
            "providers), ANTHROPIC_API_KEY, or OPENAI_COMPAT_API_KEY in .env"
        )

    # OpenAI-compat backend: defaults to OpenRouter using the same key. User
    # can override to point at native Moonshot / DeepSeek / vLLM via env.
    oai_key = explicit_oai_key or (or_key or None)
    oai_base = explicit_oai_base or OPENROUTER_BASE_URL_OPENAI

    designer_model = normalize_model_id(
        _env_first("DESIGNER_MODEL", "PLANNER_MODEL", default=anthropic_default_designer)
    )
    critic_model = normalize_model_id(
        os.getenv("CRITIC_MODEL", "").strip() or anthropic_default_critic
    )
    designer_provider = _parse_provider(
        _env_first("DESIGNER_PROVIDER", "PLANNER_PROVIDER", default="auto")
    )
    critic_provider = _parse_provider(os.getenv("CRITIC_PROVIDER", "auto"))

    # v2.4 enhancer resolution — default is Opus 4.7, but if the user only
    # has ANTHROPIC_API_KEY (no OpenRouter), strip the `anthropic/` prefix
    # so the stock Anthropic endpoint accepts the model id.
    if or_key or explicit_oai_key:
        enhancer_default = DEFAULT_ENHANCER_MODEL
    else:
        enhancer_default = ANTHROPIC_FALLBACK_ENHANCER
    enhancer_model = normalize_model_id(
        os.getenv("ENHANCER_MODEL", "").strip() or enhancer_default
    )
    enhancer_provider = _parse_provider(os.getenv("ENHANCER_PROVIDER", "auto"))
    enhancer_budget = _parse_int_env("ENHANCER_THINKING_BUDGET", 10000)
    # SKIP_PROMPT_ENHANCER=1 disables the stage at settings-load time;
    # the `--skip-enhancer` CLI flag also toggles this per-run.
    skip_enhancer_env = os.getenv("SKIP_PROMPT_ENHANCER", "").strip() in (
        "1", "true", "True", "yes",
    )
    enable_prompt_enhancer = not skip_enhancer_env

    # v2.8.0 ClaimGraph extractor resolution — same fallback rule as the
    # enhancer (drop OpenRouter prefix when only ANTHROPIC_API_KEY is set).
    if or_key or explicit_oai_key:
        claim_graph_default = DEFAULT_CLAIM_GRAPH_MODEL
    else:
        claim_graph_default = ANTHROPIC_FALLBACK_CLAIM_GRAPH
    claim_graph_model = normalize_model_id(
        os.getenv("CLAIM_GRAPH_MODEL", "").strip() or claim_graph_default
    )
    claim_graph_provider = _parse_provider(
        os.getenv("CLAIM_GRAPH_PROVIDER", "auto"),
    )
    claim_graph_max_turns = _parse_int_env("CLAIM_GRAPH_MAX_TURNS", 15)
    claim_graph_budget = _parse_int_env("CLAIM_GRAPH_THINKING_BUDGET", 8000)
    no_claim_graph_env = os.getenv("NO_CLAIM_GRAPH", "").strip() in (
        "1", "true", "True", "yes",
    )
    enable_claim_graph = not no_claim_graph_env

    # DeckOutlineAgent resolution — same fallback rule as the other text
    # agents (drop OpenRouter prefix when only ANTHROPIC_API_KEY is set).
    if or_key or explicit_oai_key:
        deck_outline_default = DEFAULT_DECK_OUTLINE_MODEL
    else:
        deck_outline_default = ANTHROPIC_FALLBACK_DECK_OUTLINE
    deck_outline_model = normalize_model_id(
        os.getenv("DECK_OUTLINE_MODEL", "").strip() or deck_outline_default
    )
    deck_outline_provider = _parse_provider(
        os.getenv("DECK_OUTLINE_PROVIDER", "auto"),
    )
    deck_outline_max_turns = _parse_int_env("DECK_OUTLINE_MAX_TURNS", 8)
    deck_outline_budget = _parse_int_env("DECK_OUTLINE_THINKING_BUDGET", 6000)
    skip_deck_outline_env = os.getenv("SKIP_DECK_OUTLINE", "").strip() in (
        "1", "true", "True", "yes",
    )
    enable_deck_outline = not skip_deck_outline_env

    # PaperMemoryAgent resolution — defaults to the resolved claim-graph model
    # unless explicitly overridden.
    paper_memory_default = (
        DEFAULT_PAPER_MEMORY_MODEL
        if or_key or explicit_oai_key
        else ANTHROPIC_FALLBACK_PAPER_MEMORY
    )
    paper_memory_model = normalize_model_id(
        os.getenv("PAPER_MEMORY_MODEL", "").strip()
        or claim_graph_model
        or paper_memory_default
    )
    paper_memory_provider = _parse_provider(
        os.getenv("PAPER_MEMORY_PROVIDER", "auto"),
    )
    paper_memory_max_turns = _parse_int_env("PAPER_MEMORY_MAX_TURNS", 6)
    paper_memory_budget = _parse_int_env("PAPER_MEMORY_THINKING_BUDGET", 4000)
    no_paper_memory_agent_env = os.getenv("NO_PAPER_MEMORY_AGENT", "").strip() in (
        "1", "true", "True", "yes",
    )
    enable_paper_memory_agent = not no_paper_memory_agent_env
    # v2.8.1 HyperFrames Composer resolution — same fallback rule as above.
    if or_key or explicit_oai_key:
        composer_default = DEFAULT_COMPOSER_MODEL
    else:
        composer_default = ANTHROPIC_FALLBACK_COMPOSER
    composer_model = normalize_model_id(
        os.getenv("COMPOSER_MODEL", "").strip() or composer_default
    )
    composer_provider = _parse_provider(os.getenv("COMPOSER_PROVIDER", "auto"))
    skip_composer_env = os.getenv("SKIP_VIDEO_COMPOSER", "").strip() in (
        "1", "true", "True", "yes",
    )
    enable_video_composer = not skip_composer_env

    poster_harness_mode = _parse_poster_harness_mode(
        _project_env_first(
            "POSTER_HARNESS_MODE",
            unprefixed=("POSTER_HARNESS_MODE",),
        ),
        default=DEFAULT_USER_POSTER_HARNESS_MODE,
    )
    designer_author_mode = _parse_designer_author_mode(
        _project_env_first(
            "DESIGNER_AUTHOR",
            aliases=("DESIGN_ANYTHING_PLANNER_AUTHOR",),
        ),
    )
    designer_author_harness = _parse_designer_author_harness(
        _project_env_first(
            "DESIGNER_AUTHOR_HARNESS",
            aliases=("DESIGN_ANYTHING_PLANNER_AUTHOR_HARNESS",),
        ),
    )
    designer_author_model = _project_env_first(
        "DESIGNER_AUTHOR_MODEL",
        aliases=("DESIGN_ANYTHING_PLANNER_AUTHOR_MODEL",),
    ) or None
    designer_author_cmd = designer_author_command_for_harness(
        designer_author_harness,
        designer_author_model,
        explicit_cmd=_project_env_first(
            "DESIGNER_AUTHOR_CMD",
            aliases=("DESIGN_ANYTHING_PLANNER_AUTHOR_CMD",),
        ),
    )
    designer_author_timeout = _parse_int_env_any(
        _project_env_keys(
            "DESIGNER_AUTHOR_TIMEOUT_SECONDS",
            aliases=("DESIGN_ANYTHING_PLANNER_AUTHOR_TIMEOUT_SECONDS",),
        ),
        1800,
    )
    designer_author_max_attempt_keys = _project_env_keys(
        "DESIGNER_AUTHOR_MAX_ATTEMPTS",
        aliases=("DESIGN_ANYTHING_PLANNER_AUTHOR_MAX_ATTEMPTS",),
    )
    designer_author_max_attempts_raw = _project_env_first(
        "DESIGNER_AUTHOR_MAX_ATTEMPTS",
        aliases=("DESIGN_ANYTHING_PLANNER_AUTHOR_MAX_ATTEMPTS",),
    )
    designer_author_max_attempts = _parse_int_env_any(
        designer_author_max_attempt_keys,
        12,
    )
    authoring_max_attempts_override = (
        designer_author_max_attempts
        if designer_author_max_attempts_raw
        else None
    )
    designer_author_poster_stable_s = _parse_float_env_any(
        _project_env_keys(
            "DESIGNER_AUTHOR_POSTER_STABLE_SECONDS",
            aliases=("DESIGN_ANYTHING_PLANNER_AUTHOR_POSTER_STABLE_SECONDS",),
        ),
        8.0,
    )
    harness_api_key = _env_first(
        "AUTODESIGN_DESIGNER_AUTHOR_API_KEY",
        "AUTODESIGN_HARNESS_API_KEY",
        "AUTODESIGN_CODE_EDITOR_API_KEY",
        "DESIGN_ANYTHING_DESIGNER_AUTHOR_API_KEY",
        "DESIGN_ANYTHING_HARNESS_API_KEY",
        "DESIGN_ANYTHING_CODE_EDITOR_API_KEY",
    ) or None
    code_editor_harness = _parse_designer_author_harness(
        _project_env_first("CODE_EDITOR_HARNESS", default="codex"),
        default="codex",
    )
    code_editor_model = _project_env_first("CODE_EDITOR_MODEL") or None
    code_editor_cmd = code_editor_command_for_harness(
        code_editor_harness,
        code_editor_model,
        explicit_cmd=_project_env_first("CODE_EDITOR_CMD"),
    )
    code_editor_timeout = _parse_int_env_any(_project_env_keys("CODE_EDITOR_TIMEOUT_SECONDS"), 600)
    code_editor_max_attempts = _parse_int_env_any(_project_env_keys("CODE_EDITOR_MAX_ATTEMPTS"), 2)
    identity_logo_agent_mode: IdentityLogoAgentMode = "off"
    identity_logo_agent_harness: IdentityLogoAgentHarness = "codex"
    identity_logo_agent_model = None
    identity_logo_agent_cmd = ""
    identity_logo_agent_timeout = 240
    identity_logo_agent_max_entities = 6
    identity_logo_agent_max_candidates = 12
    openresearch_api_url = _project_env_first(
        "OPENRESEARCH_API_URL",
        aliases=("OPEN_DESIGN_OPENRESEARCH_API_URL",),
        unprefixed=("OPENRESEARCH_API_URL",),
        default="https://api.openresearch.sh",
    )
    openresearch_token = _project_env_first(
        "OPENRESEARCH_TOKEN",
        aliases=("OPEN_DESIGN_OPENRESEARCH_TOKEN",),
    )
    openresearch_timeout = _parse_int_env_any(
        _project_env_keys(
            "OPENRESEARCH_TIMEOUT_SECONDS",
            aliases=("OPEN_DESIGN_OPENRESEARCH_TIMEOUT_SECONDS",),
        ),
        120,
    )
    openresearch_org_id = _project_env_first(
        "OPENRESEARCH_ORG_ID",
        aliases=("OPEN_DESIGN_OPENRESEARCH_ORG_ID",),
    )
    openresearch_default_repo_full_name = _project_env_first(
        "OPENRESEARCH_REPO",
        aliases=("OPEN_DESIGN_OPENRESEARCH_REPO",),
    )
    openresearch_submitter_mode = _parse_openresearch_submitter_mode(
        _project_env_first(
            "OPENRESEARCH_SUBMITTER",
            aliases=("OPEN_DESIGN_OPENRESEARCH_SUBMITTER",),
            default="off",
        ),
    )
    openresearch_submitter_cmd = _project_env_first(
        "OPENRESEARCH_SUBMITTER_CMD",
        aliases=("OPEN_DESIGN_OPENRESEARCH_SUBMITTER_CMD",),
    )
    openresearch_submitter_timeout = _parse_int_env_any(
        _project_env_keys(
            "OPENRESEARCH_SUBMITTER_TIMEOUT_SECONDS",
            aliases=("OPEN_DESIGN_OPENRESEARCH_SUBMITTER_TIMEOUT_SECONDS",),
        ),
        300,
    )

    has_explicit_openai_compat_route = bool(explicit_oai_key or explicit_oai_base)
    if has_explicit_openai_compat_route:
        ingest_default = "gpt-5.4-nano"
    elif or_key:
        ingest_default = DEFAULT_CRITIC_MODEL
    else:
        ingest_default = "claude-sonnet-4-7"
    ingest_model = normalize_model_id(
        os.getenv("INGEST_MODEL", "").strip() or ingest_default
    )

    designer_budget = _parse_int_env_any(("DESIGNER_THINKING_BUDGET", "PLANNER_THINKING_BUDGET"), 10000)
    critic_budget = _parse_int_env("CRITIC_THINKING_BUDGET", 10000)
    critic_max_turns_env = _parse_int_env("CRITIC_MAX_TURNS", 10)
    critic_max_images_env = _parse_int_env("CRITIC_MAX_IMAGES_PER_TURN", 4)
    max_critique_iters = _parse_int_env("MAX_CRITIQUE_ITERS", max(0, designer_author_max_attempts - 1))
    max_env_repair_attempts = _parse_int_env("MAX_ENV_REPAIR_ATTEMPTS", 1)
    skip_skills_env = os.getenv("SKIP_SKILLS", "").strip() in (
        "1", "true", "True", "yes",
    )
    poster_preview_max_edge = _parse_int_env("POSTER_PREVIEW_MAX_EDGE", 2048)
    interleaved = os.getenv("ENABLE_INTERLEAVED_THINKING", "1").strip() not in (
        "0", "false", "False", "no", "",
    )
    ingest_timeout = float(_parse_int_env("INGEST_HTTP_TIMEOUT", 600))
    llm_http_timeout = _resolve_llm_http_timeout()

    # v2.5 image-backend resolution. Default model is the v2.7.5
    # `google/gemini-2.5-flash-image` via OpenRouter; `image_provider`
    # lets the user pin a backend even when auto-detection would pick
    # the other one (e.g. running an internal mirror that serves a
    # `gemini-*` slug from a non-Google endpoint).
    image_model_env = normalize_model_id(os.getenv("IMAGE_MODEL", "").strip())
    image_provider_env = _parse_image_provider(os.getenv("IMAGE_PROVIDER", "auto"))

    # v2.7.5 — hardcoded fallback model for image generation. Resolved
    # here so users can override via env without touching the dataclass
    # default. An explicitly empty string disables the fallback chain
    # (`IMAGE_FALLBACK_MODEL=` in .env), which is what dogfood SFT
    # capture wants when probing failure modes deterministically.
    image_fallback_env_raw = os.getenv("IMAGE_FALLBACK_MODEL")
    image_fallback_kwargs: dict[str, Any] = {}
    if image_fallback_env_raw is not None:
        image_fallback_kwargs["image_fallback_model"] = normalize_model_id(
            image_fallback_env_raw.strip()
        )
    section_policy = _parse_section_policy(os.getenv("SECTION_NUMBER_POLICY", "renumber"))

    return Settings(
        anthropic_api_key=api_key,
        anthropic_base_url=base_url,
        anthropic_auth_token=ant_auth_token,
        anthropic_custom_headers=anthropic_custom_headers,
        openrouter_api_key=or_key or None,
        openai_compat_api_key=oai_key,
        openai_compat_base_url=oai_base,
        llm_http_timeout=llm_http_timeout,
        gemini_api_key=gemini,
        designer_model=designer_model,
        critic_model=critic_model,
        designer_provider=designer_provider,
        critic_provider=critic_provider,
        enhancer_model=enhancer_model,
        enhancer_provider=enhancer_provider,
        enhancer_thinking_budget=enhancer_budget,
        enable_prompt_enhancer=enable_prompt_enhancer,
        claim_graph_model=claim_graph_model,
        claim_graph_provider=claim_graph_provider,
        claim_graph_max_turns=claim_graph_max_turns,
        claim_graph_thinking_budget=claim_graph_budget,
        enable_claim_graph=enable_claim_graph,
        deck_outline_model=deck_outline_model,
        deck_outline_provider=deck_outline_provider,
        deck_outline_max_turns=deck_outline_max_turns,
        deck_outline_thinking_budget=deck_outline_budget,
        enable_deck_outline=enable_deck_outline,
        paper_memory_model=paper_memory_model,
        paper_memory_provider=paper_memory_provider,
        paper_memory_max_turns=paper_memory_max_turns,
        paper_memory_thinking_budget=paper_memory_budget,
        enable_paper_memory_agent=enable_paper_memory_agent,
        composer_model=composer_model,
        composer_provider=composer_provider,
        enable_video_composer=enable_video_composer,
        designer_author_mode=designer_author_mode,
        designer_author_harness=designer_author_harness,
        designer_author_cmd=designer_author_cmd,
        designer_author_model=designer_author_model,
        designer_author_timeout_s=designer_author_timeout,
        designer_author_max_attempts=designer_author_max_attempts,
        authoring_max_attempts_override=authoring_max_attempts_override,
        designer_author_poster_stable_s=designer_author_poster_stable_s,
        harness_api_key=harness_api_key,
        code_editor_harness=code_editor_harness,
        code_editor_cmd=code_editor_cmd,
        code_editor_model=code_editor_model,
        code_editor_timeout_s=code_editor_timeout,
        code_editor_max_attempts=code_editor_max_attempts,
        identity_logo_agent_mode=identity_logo_agent_mode,
        identity_logo_agent_harness=identity_logo_agent_harness,
        identity_logo_agent_cmd=identity_logo_agent_cmd,
        identity_logo_agent_model=identity_logo_agent_model,
        identity_logo_agent_timeout_s=identity_logo_agent_timeout,
        identity_logo_agent_max_entities=identity_logo_agent_max_entities,
        identity_logo_agent_max_candidates=identity_logo_agent_max_candidates,
        openresearch_api_url=openresearch_api_url,
        openresearch_token=openresearch_token,
        openresearch_timeout_s=openresearch_timeout,
        openresearch_org_id=openresearch_org_id,
        openresearch_default_repo_full_name=openresearch_default_repo_full_name,
        openresearch_submitter_mode=openresearch_submitter_mode,
        openresearch_submitter_cmd=openresearch_submitter_cmd,
        openresearch_submitter_timeout_s=openresearch_submitter_timeout,
        poster_harness_mode=poster_harness_mode,
        ingest_model=ingest_model,
        ingest_http_timeout=ingest_timeout,
        designer_thinking_budget=designer_budget,
        critic_thinking_budget=critic_budget,
        max_critique_iters=max_critique_iters,
        critic_max_turns=critic_max_turns_env,
        critic_max_images_per_turn=critic_max_images_env,
        max_env_repair_attempts=max_env_repair_attempts,
        enable_skills=not skip_skills_env,
        poster_preview_max_edge=poster_preview_max_edge,
        enable_interleaved_thinking=interleaved,
        **({"image_model": image_model_env} if image_model_env else {}),
        **image_fallback_kwargs,
        image_provider=image_provider_env,
        section_number_policy=section_policy,
    )


def _parse_provider(raw: str) -> ProviderChoice:
    raw = (raw or "").strip().lower()
    if raw in ("auto", "anthropic", "openai_compat"):
        return raw  # type: ignore[return-value]
    if raw in ("openai", "openrouter", "moonshot", "deepseek", "kimi", "doubao"):
        return "openai_compat"
    if raw in ("claude",):
        return "anthropic"
    return "auto"


def _parse_section_policy(raw: str) -> SectionNumberPolicy:
    """Normalize SECTION_NUMBER_POLICY env. Falls back to "renumber" on
    anything unrecognised so a typo never stops a run."""
    raw = (raw or "").strip().lower()
    if raw in ("renumber", "strip", "preserve"):
        return raw  # type: ignore[return-value]
    if raw in ("auto", "default", ""):
        return "renumber"
    if raw in ("none", "off", "drop"):
        return "strip"
    if raw in ("keep", "noop", "as-is"):
        return "preserve"
    return "renumber"


def _parse_image_provider(raw: str) -> ImageProviderChoice:
    """Normalize IMAGE_PROVIDER env. Accepts a few friendly aliases
    (`google`, `nbp` → gemini; `or`, `seedream`, `bytedance` → openrouter)
    so users don't have to remember the canonical token."""
    raw = (raw or "").strip().lower()
    if raw in ("auto", "gemini", "openrouter", "openai_compat"):
        return raw  # type: ignore[return-value]
    if raw in ("google", "nbp", "imagen"):
        return "gemini"
    if raw in ("or", "seedream", "bytedance", "doubao"):
        return "openrouter"
    return "auto"


def resolve_font(family: str | None, weight: str = "regular",
                 settings: "Settings | None" = None) -> Path | None:
    """Resolve ``(family, weight)`` to an on-disk font path, or None.

    v2.4.2 forward-compat API. Most consumers still use the flat
    ``settings.fonts.get(family)`` lookup; this helper wraps it with two
    niceties:
    - Accepts legacy suffix-encoded names (``"NotoSansSC-Bold"`` →
      family=``NotoSansSC``, weight=``bold``). Downstream code can move
      to the ``(family, weight)`` pair incrementally.
    - Falls back to the plain-family key when no weight-specific file is
      registered (e.g. ``resolve_font("Inter", weight="bold")`` returns
      ``Inter-Variable.ttf`` because the variable TTF covers all cuts).
    - Returns ``None`` (not an exception) when nothing matches.
    """
    if not family:
        return None
    cfg = settings or load_settings()
    registry = cfg.fonts

    family_clean = family.strip()
    if not family_clean:
        return None

    # Legacy "Family-Weight" shortcut — if the exact key is registered,
    # prefer it for old artifact manifests.
    if family_clean in registry:
        return cfg.fonts_dir / registry[family_clean]

    # Split trailing -Bold / -Regular / -Medium etc. onto the weight axis.
    if "-" in family_clean:
        base, _, suffix = family_clean.rpartition("-")
        if suffix.lower() in {"regular", "bold", "medium", "light",
                              "thin", "black", "semibold", "extralight"}:
            weight = suffix.lower()
            family_clean = base

    weighted_key = f"{family_clean}-{weight.capitalize()}" if weight != "regular" else family_clean
    for candidate in (weighted_key, family_clean):
        path = registry.get(candidate)
        if path:
            return cfg.fonts_dir / path
    return None


def _parse_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_int_env_any(names: tuple[str, ...], default: int) -> int:
    raw = _env_first(*names)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _parse_float_env_any(names: tuple[str, ...], default: float) -> float:
    raw = _env_first(*names)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _resolve_llm_http_timeout() -> float:
    """Resolve the chat timeout, allowing an explicit environment override."""

    if os.getenv("LLM_HTTP_TIMEOUT", "").strip():
        return _parse_float_env("LLM_HTTP_TIMEOUT", 180.0)
    return 180.0


# ───────────────────────── Poster templates (v2.3) ─────────────────────
# Canonical canvas presets for academic poster venues. Users pass
# `--template <name>` at the CLI and the runner injects the resolved
# canvas into the brief prologue (like the `Attached files:` block for
# ingest). No DesignSpec schema change — `canvas` stays a dict; template
# is an input-side convenience, not an output-side concept.
#
# Dimensions are pragmatic working canvases for browser / PSD rendering.
# Some venue presets use print-ish DPI metadata, but the source of truth is
# the logical pixel size. Add new entries here as new venues surface in
# dogfood. Free to override any individual field (w_px / h_px / dpi /
# aspect_ratio) — the dict shape matches DesignSpec.canvas verbatim.
POSTER_TEMPLATES: dict[str, dict[str, object]] = {
    "academic-wide-2x1": {
        "w_px": 3072, "h_px": 1536, "dpi": 150,
        "aspect_ratio": "2:1", "color_mode": "RGB",
    },
    "academic-wide-3280x1860": {
        "w_px": 3072, "h_px": 1536, "dpi": 150,
        "aspect_ratio": "2:1", "color_mode": "RGB",
    },
    "academic-landscape-1.414": {
        "w_px": 3072, "h_px": 1536, "dpi": 150,
        "aspect_ratio": "2:1", "color_mode": "RGB",
    },
    "poster-classic-4x3": {
        "w_px": 2048, "h_px": 1536, "dpi": 300,
        "aspect_ratio": "4:3", "color_mode": "RGB",
    },
    "event-2x3": {
        "w_px": 1800, "h_px": 2700, "dpi": 300,
        "aspect_ratio": "2:3", "color_mode": "RGB",
    },
    "social-4x5": {
        "w_px": 2160, "h_px": 2700, "dpi": 300,
        "aspect_ratio": "4:5", "color_mode": "RGB",
    },
    "story-9x16": {
        "w_px": 1440, "h_px": 2560, "dpi": 300,
        "aspect_ratio": "9:16", "color_mode": "RGB",
    },
    "square-1x1": {
        "w_px": 2048, "h_px": 2048, "dpi": 300,
        "aspect_ratio": "1:1", "color_mode": "RGB",
    },
    "conference-poster-portrait": {
        "w_px": 2172, "h_px": 3072, "dpi": 150,
        "aspect_ratio": "1:1.414", "color_mode": "RGB",
    },
    "neurips-portrait": {
        "w_px": 1536, "h_px": 2048, "dpi": 300,
        "aspect_ratio": "3:4", "color_mode": "RGB",
    },
    "cvpr-landscape": {
        "w_px": 3072, "h_px": 1536, "dpi": 150,
        "aspect_ratio": "2:1", "color_mode": "RGB",
    },
    "icml-portrait": {
        "w_px": 1536, "h_px": 2048, "dpi": 300,
        "aspect_ratio": "3:4", "color_mode": "RGB",
    },
    # ISO A0 at 300 DPI: 841 mm × 1189 mm ≈ 9933 × 14043 px (too heavy
    # for most planners). We use a 1:√2 preset at 1/4 linear scale that
    # still prints crisply on a standard A0 plotter.
    "a0-portrait": {
        "w_px": 2378, "h_px": 3366, "dpi": 300,
        "aspect_ratio": "1:1.414", "color_mode": "RGB",
    },
    "a0-landscape": {
        "w_px": 3366, "h_px": 2378, "dpi": 300,
        "aspect_ratio": "1.414:1", "color_mode": "RGB",
    },
}


def resolve_template(name: str | None) -> dict[str, object] | None:
    """Return the canvas dict for a registered template name, or None
    if `name` is None / unknown. Case-insensitive + hyphen-or-underscore
    tolerant so `--template A0_Portrait` works."""
    if not name:
        return None
    key = name.strip().lower().replace("_", "-")
    return POSTER_TEMPLATES.get(key)


def available_templates() -> list[str]:
    """Sorted list of registered template names — used by CLI --help."""
    return sorted(POSTER_TEMPLATES.keys())
