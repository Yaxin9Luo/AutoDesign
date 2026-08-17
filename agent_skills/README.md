<p align="center">
  <strong>English</strong> ·
  <a href="./README.zh-CN.md">简体中文</a>
</p>

# AutoDesign Agent Skills

Turn one research paper into an editable poster, slide deck, project webpage,
or narrated conference video directly inside your Coding Agent. These are four
independently installable, self-contained Agent Skills:

| Skill | Creates | Primary delivery |
|---|---|---|
| [`autodesign-poster`](./autodesign-poster/) | Source-grounded academic conference poster | Editable HTML, one-page PDF, preview, provenance |
| [`autodesign-ppt`](./autodesign-ppt/) | Research talk and slide deck | Editable HTML, native editable PPTX, PDF, notes |
| [`autodesign-webpage`](./autodesign-webpage/) | Responsive research project webpage | Editable local HTML, assets, desktop/mobile QA |
| [`autodesign-video`](./autodesign-video/) | Narrated and subtitled conference video | Editable HyperFrames project, 1080p MP4, audio, SRT/VTT |

Each Skill carries a lightweight local harness for evidence, attempts,
deterministic checks, rendering, and delivery. No AutoDesign application server
is required. Run state and generated artifacts stay in an output directory
selected by the user. Managed runtimes stay in a versioned user cache; the
workflow never writes generated output into the installed Skill.

> [!IMPORTANT]
> The Skill edition is the portable, convenient way to use part of AutoDesign
> from an existing Coding Agent. It does not replace the full AutoDesign
> Harness. Read [Skills vs. the full AutoDesign Harness](#skills-vs-the-full-autodesign-harness)
> before evaluating output quality.

## Quick install

Download the checksum-verified release bundle with GitHub CLI:

```bash
mkdir -p autodesign-skills-v0.1.0
gh release download agent-skills-v0.1.0 \
  --repo Yaxin9Luo/AutoDesign \
  --dir autodesign-skills-v0.1.0
cd autodesign-skills-v0.1.0
```

Then install all four Skills into the shared Codex and DeepSeek Harness user
directory:

```bash
DESTINATION="${DESTINATION:-$HOME/.agents/skills}"

for skill in autodesign-poster autodesign-ppt autodesign-webpage autodesign-video; do
  python3 -I ./package_agent_skills.py install \
    --archive "./${skill}-0.1.0.zip" \
    --checksum "./${skill}-0.1.0.zip.sha256" \
    --destination "$DESTINATION"
done
```

Install only the artifact types you need by running the command for one Skill.
The installer checks SHA-256, validates the extracted package, promotes it
atomically, and refuses to overwrite an existing installation.

## Install by Coding Agent

Agent Skills are directories containing a required `SKILL.md` plus optional
scripts, references, and assets. AutoDesign follows the open
[Agent Skills specification](https://agentskills.io/specification).

### Codex

Codex discovers user Skills under `~/.agents/skills`. Older local setups may
also use `~/.codex/skills`.

```bash
DESTINATION="$HOME/.agents/skills"
```

Use this value for `DESTINATION` in the [Quick install](#quick-install) shell.
After installation, start a new Codex task if the Skills do not appear
immediately. See the official [Codex Skills documentation](https://developers.openai.com/codex/skills).

### Claude Code

Claude Code discovers personal Skills under `~/.claude/skills`:

```bash
DESTINATION="$HOME/.claude/skills"
```

Set this in the same shell before running the [Quick install](#quick-install)
loop; the loop preserves a destination you already selected. Claude Code
normally detects changes in an existing Skills directory. Restart it if this is
the first Skill directory on the machine. See the official [Claude Code Skills
documentation](https://code.claude.com/docs/en/skills).

### DeepSeek Harness

DeepSeek Harness supports project roots `.dsh/skills` and `.agents/skills`, plus
the user roots `~/.dsh/skills` and `~/.agents/skills`. For a DSH-only user
installation, use:

```bash
DESTINATION="$HOME/.dsh/skills"
```

Set this in the same shell before running the [Quick install](#quick-install)
loop; the loop preserves a destination you already selected. Use
`DESTINATION="$HOME/.agents/skills"` instead when you want one shared installation
for Codex and DeepSeek Harness. See the official
[DeepSeek Harness Skills subsystem](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/skills.md).

### Installer details and Windows

The [`agent-skills-v0.1.0` release](https://github.com/Yaxin9Luo/AutoDesign/releases/tag/agent-skills-v0.1.0)
contains four ZIPs, four SHA-256 sidecars, a manifest, and a standalone
installer. Download it with GitHub CLI:

```bash
mkdir -p autodesign-skills-v0.1.0
gh release download agent-skills-v0.1.0 \
  --repo Yaxin9Luo/AutoDesign \
  --dir autodesign-skills-v0.1.0
cd autodesign-skills-v0.1.0
```

Choose a Python 3 launcher: `python3` on most macOS/Linux systems, `python` as
the fallback, or `py -3` on Windows. Then install one archive and its matching
checksum:

```bash
python3 -I ./package_agent_skills.py install \
  --archive ./autodesign-poster-0.1.0.zip \
  --checksum ./autodesign-poster-0.1.0.zip.sha256 \
  --destination "$HOME/.agents/skills"
```

Replace `autodesign-poster` with another Skill name and choose the destination
for your Coding Agent. The installer verifies SHA-256, validates extracted
contents, promotes atomically, and refuses to overwrite an existing Skill.

Windows example for all four Skills (change `$Destination` for Codex or
DeepSeek Harness):

```powershell
New-Item -ItemType Directory -Force autodesign-skills-v0.1.0 | Out-Null
gh release download agent-skills-v0.1.0 `
  --repo Yaxin9Luo/AutoDesign `
  --dir autodesign-skills-v0.1.0
Set-Location autodesign-skills-v0.1.0

$Destination = "$HOME\.claude\skills"
foreach ($Skill in @("autodesign-poster", "autodesign-ppt", "autodesign-webpage", "autodesign-video")) {
  py -3 -I .\package_agent_skills.py install `
    --archive ".\$Skill-0.1.0.zip" `
    --checksum ".\$Skill-0.1.0.zip.sha256" `
    --destination $Destination
}
```

## First run

Give the Coding Agent the paper PDF, name the Skill, and state the intended
audience and output directory. For example:

| Host | Example |
|---|---|
| Codex | `$autodesign-poster Turn /path/paper.pdf into an editable CVPR landscape poster. Save the complete run under /path/output.` |
| Claude Code | `/autodesign-ppt Turn /path/paper.pdf into an 18-slide conference talk for a technical audience.` |
| DeepSeek Harness | `Use autodesign-webpage to turn /path/paper.pdf into a responsive research project page under /path/output.` |
| Any supported host | `Use autodesign-video to create a six-minute narrated conference video from /path/paper.pdf with optional English subtitles.` |

The Coding Agent reads the Skill workflow, runs its local `doctor`, prepares
the source evidence, authors bounded attempts, executes deterministic QA, and
requests a fresh visual review before final delivery. The first browser, PPT,
or Video run may download pinned runtime dependencies into a user cache.

Generated deliverables live under the run directory you requested. The workflow
does not write run state, caches, or artifacts into the installed Skill.

## Requirements

All four harnesses require Python 3.10 or newer. Video currently requires
Python 3.10–3.12 for its locked Kokoro and HyperFrames runtime.

| Skill | Additional local requirements |
|---|---|
| Poster | Poppler: `pdftotext`, `pdfinfo`, `pdftoppm`, `pdfimages`; first setup installs a pinned browser in the user cache |
| PPT | Poppler, LibreOffice; first setup installs pinned browser and native-PPT runtimes in the user cache |
| Webpage | Poppler; first setup installs a pinned browser in the user cache |
| Video | Poppler, Node.js 22+, npm, `ffmpeg`, `ffprobe`, Python 3.10–3.12; setup installs exact `hyperframes@0.7.86` and locked Kokoro assets |

The setup tools select supported macOS, Linux, or Windows packages and fail
closed on unsupported platforms or architectures. Initial runtime setup
requires network access. Run the Skill through your Coding Agent so it can act
on `doctor` diagnostics rather than skipping a required gate.

## Skills vs. the full AutoDesign Harness

The four Skills preserve many of AutoDesign's artifact contracts and local QA
checks, but they run inside a host Coding Agent rather than the complete
AutoDesign system.

| Area | Standalone Skills | Full AutoDesign Harness |
|---|---|---|
| Entry point | Existing Coding Agent conversation | AutoDesign Workbench and pipeline |
| Orchestration | Host agent follows one portable workflow | Coordinated artifact pipeline, shared context, routing, and lifecycle management |
| Source understanding | Host agent plus local evidence and provenance tools | Integrated paper memory, planners, artifact-specific harnesses, and shared source state |
| Review and iteration | Deterministic gates plus fresh host-agent or subagent review | Integrated critics, retry routing, cross-stage diagnostics, and workbench feedback |
| Editing experience | Files and the host Coding Agent | Workbench, previews, attempt history, Canvas editing, and artifact export |
| Output consistency | More dependent on the selected agent, model, and local environment | More controlled by the full optimized pipeline |

The standalone Skill path is intentionally easier to install, but it does not
replace AutoDesign's full effect or guarantee the same artifact quality. In
particular, PDF interpretation, important-figure selection, visual hierarchy,
and multi-round repair still depend more heavily on the host Coding Agent.

Our future target is to bring these portable Skills to roughly **70–80%** of
the full Harness experience across supported artifact workflows. That is a
roadmap target, not a claim about current measured performance.

For the complete system, use the [AutoDesign Workbench](../README.md#quickstart)
and the full DesignHarness pipeline.

## Roadmap and contributing

The Skills are under active development. Contributions are welcome in:

- PDF understanding, figure ranking, crop selection, and source grounding;
- artifact planning, context management, and repair routing;
- layouts and editable export for Poster, PPT, Webpage, and Video;
- deterministic evaluators, visual review rubrics, and regression cases;
- real-paper examples that expose gaps between the Skills and full Harness.

Open an [issue](https://github.com/Yaxin9Luo/AutoDesign/issues) with a reproducible
paper/run case, or submit a pull request with focused tests. **Contributing** to
the portable Skills is also a direct way to help move them toward the 70–80%
future target without weakening their source-grounding and delivery contracts.

## Maintainer verification

Validate all four packages:

```bash
python3 -B scripts/validate_agent_skills.py --root agent_skills
```

Build deterministic, non-overwriting release archives:

```bash
python3 -B scripts/package_agent_skills.py build \
  --source-root agent_skills \
  --output-dir dist/agent-skills-v0.1.0 \
  --version 0.1.0
```

The output directory contains one versioned ZIP and SHA-256 sidecar per Skill,
`manifest.json`, and the release-local `package_agent_skills.py` and
`validate_agent_skills.py` tools. Build twice and compare every byte before
publishing a release.
