# AutoDesign Agent Skills

This directory contains four independently installable Agent Skills:

- `autodesign-poster`
- `autodesign-ppt`
- `autodesign-webpage`
- `autodesign-video`

Each package is self-contained at installation time. It does not require the
AutoDesign application or a running server, and it stores mutable run data only
in a user-selected output directory.

## Choose an installation root

Install each complete Skill folder directly below one host discovery root:

| Host | User discovery roots |
|---|---|
| Codex | `~/.agents/skills` or `~/.codex/skills` |
| DeepSeek Harness | `~/.agents/skills` or `~/.dsh/skills` |
| Claude Code | `~/.claude/skills` |

`~/.agents/skills` can be shared by Codex and DeepSeek Harness. Restart the
host after installation so it discovers the new Skill folders.

## Python and platform prerequisites

The harness scripts require Python 3.10+. On macOS and Linux, use `python3`
when available and fall back to `python`. On Windows, prefer `py -3` and fall
back to `python`. Use the selected command consistently in place of `PYTHON`
below.

| Skill | Additional local prerequisites |
|---|---|
| Poster | Poppler (`pdftotext`, `pdfinfo`, `pdftoppm`, `pdfimages`); its setup installs a pinned browser in the user cache |
| PPT | Poppler and LibreOffice; its setup installs pinned browser and PPT runtimes in the user cache |
| Webpage | Poppler; its setup installs a pinned browser in the user cache |
| Video | Node.js 22+, npm, `ffmpeg`, `ffprobe`, and Python 3.10–3.12 for Kokoro/HyperFrames runtime setup |

The setup tools select macOS, Linux, or Windows runtime packages and fail
closed on unsupported architectures. Run each Skill's `doctor` before its first
job. Initial browser, PPT, and Video setup requires network access; generated
artifacts and caches remain outside the installed Skill.

## Validate

```bash
python3 scripts/validate_agent_skills.py --root agent_skills
```

## Build deterministic release archives

Choose a new, unused output directory. Release builds deliberately refuse to
overwrite an existing directory.

```bash
python3 scripts/package_agent_skills.py build \
  --source-root agent_skills \
  --output-dir dist/agent-skills-v0.1.0 \
  --version 0.1.0
```

The release contains one versioned ZIP and SHA-256 sidecar per Skill, a
deterministic `manifest.json`, and the release-local `package_agent_skills.py`
installer with its `validate_agent_skills.py` validation helper.

## Install one Skill

```bash
# macOS / Linux
if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
else
  echo "Python 3.10+ is required" >&2
  exit 1
fi
"$PYTHON" ./package_agent_skills.py install \
  --archive ./autodesign-poster-0.1.0.zip \
  --checksum ./autodesign-poster-0.1.0.zip.sha256 \
  --destination "$HOME/.agents/skills"
```

On Windows, run the same release-local installer with `py -3` (or `python`)
and choose the corresponding user discovery root, for example:

```powershell
py -3 .\package_agent_skills.py install `
  --archive .\autodesign-poster-0.1.0.zip `
  --checksum .\autodesign-poster-0.1.0.zip.sha256 `
  --destination "$HOME\.claude\skills"
```

Installation verifies the release checksum, is atomic, and refuses to replace
an existing Skill directory. Installing a raw development archive requires the
explicit `--allow-unverified` opt-in.
