# AutoDesign Agent Skills

This directory contains four independently installable Agent Skills:

- `autodesign-poster`
- `autodesign-ppt`
- `autodesign-webpage`
- `autodesign-video`

Each package is self-contained at installation time. It does not require the
AutoDesign application or a running server, and it stores mutable run data only
in a user-selected output directory.

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

The release contains one versioned ZIP and SHA-256 sidecar per Skill plus a
deterministic `manifest.json`.

## Install one Skill

```bash
python3 scripts/package_agent_skills.py install \
  --archive dist/agent-skills-v0.1.0/autodesign-poster-0.1.0.zip \
  --checksum dist/agent-skills-v0.1.0/autodesign-poster-0.1.0.zip.sha256 \
  --destination "${CODEX_HOME:-$HOME/.codex}/skills"
```

Installation verifies the release checksum, is atomic, and refuses to replace
an existing Skill directory. Installing a raw development archive requires the
explicit `--allow-unverified` opt-in.
