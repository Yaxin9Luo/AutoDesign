<p align="center">
  <a href="./README.md">English</a> ·
  <strong>简体中文</strong>
</p>

# AutoDesign Agent Skills

直接在 Coding Agent 中，将一篇研究论文转换为可编辑的海报、幻灯片、项目网页
或带旁白的会议视频。这里提供四个可独立安装、彼此自包含的 Agent Skills：

| Skill | 生成内容 | 主要交付物 |
|---|---|---|
| [`autodesign-poster`](./autodesign-poster/) | 有来源依据的学术会议海报 | 可编辑 HTML、单页 PDF、预览图、来源记录 |
| [`autodesign-ppt`](./autodesign-ppt/) | 学术报告与幻灯片 | 可编辑 HTML、原生可编辑 PPTX、PDF、演讲者备注 |
| [`autodesign-webpage`](./autodesign-webpage/) | 响应式研究项目网页 | 可编辑本地 HTML、资源文件、桌面端与移动端 QA |
| [`autodesign-video`](./autodesign-video/) | 带旁白与字幕的会议视频 | 可编辑 HyperFrames 项目、1080p MP4、音频、SRT/VTT |

每个 Skill 都包含一个用于证据整理、多轮尝试、确定性检查、渲染与交付的轻量级
本地 harness，不需要运行 AutoDesign 应用服务器。运行状态与生成成果会保存在用户
指定的输出目录中；托管运行时则保存在带版本的用户缓存中。整个工作流不会把生成
内容写回已安装的 Skill。

> [!IMPORTANT]
> Skill 版是在现有 Coding Agent 中使用部分 AutoDesign 能力的便携方式，重点是安装
> 和使用方便。它不能替代完整的 AutoDesign Harness。评估生成质量前，请先阅读
> [Skills 与完整 AutoDesign Harness 的区别](#skills-vs-full-autodesign-harness)。

<a id="poster-agent-first-v2"></a>

## Poster Agent-first v2 · 2026-08-18

Poster 是第一个接入 Agent-first 来源工作流的便携 Skill。论文 PDF 始终是事实来源：
宿主 Coding Agent 负责判断什么内容重要，本地工具只提供确定性的检查、裁图、来源
记录、验证与恢复能力。Skill 中没有写入任何特定论文的图片清单或验收标签。

本次主要提升：

- **直接整理 PDF 素材。** Agent 可以检查不可变的页面栅格图并请求精确像素裁切，
  不再被一次自动提取出的视觉目录限制。被 PDF 拆碎的复杂系统图也可以重新摄取为
  一张干净、完整的来源视觉。
- **经过评审的来源目录。** 每张获准使用的图片都会通过规范化的选择与评审记录，
  绑定原始页码、裁切范围、文件哈希、证据角色和目录修订号，之后才能进入创作尝试。
- **与修订绑定的计划和尝试。** 每次尝试都会冻结来源、目录、计划与授权素材快照。
  评审失败后会回到正确层级：重试布局、创建新的内容计划，或回到 PDF 获取替代裁图；
  早期尝试保持不可变且可检查。
- **只读的屏幕与打印 QA。** 同一套浏览器探针会分别检查 screen 和 print，并覆盖
  13 类稳定失败，包括裁切、溢出、字号过小、表格越界、来源流结构断裂和画布留白
  过多。审计只报告问题，不会改写 Agent 创作的 HTML、CSS、文字或布局。
- **更强的便携运行记录。** 原子恢复、只追加的修订记录、归档与 Release receipt、
  只读安装检查和零 bytecode 运行，让不同宿主上的恢复与审计更可靠。

这些改动针对首版 Skill 最明显的质量瓶颈：重要图片选择偏弱、PDF 图片被拆碎、修复
回到了错误阶段，以及布局检查无法区分屏幕与打印结果。合并代码已通过 481 项集成
测试、全部 53 项产品 smoke、四个独立 Skill 校验器，以及真实 Chromium 的 Poster
屏幕与打印回归测试。

这是一次 **Poster-first** 更新。PPT、Webpage 与 Video 仍可正常安装，并继续使用各自
现有的生成工作流。达到完整 Harness 约 70–80% 效果仍是路线图目标，不是当前已测得
的结论。当前可下载的 `agent-skills-v0.1.0` 仍是首发包；Poster Agent-first v2 已进入
`main`，并将在下一版打包的 Skills Release 中提供。

## 快速安装

使用 GitHub CLI 下载带校验文件的发布包：

```bash
mkdir -p autodesign-skills-v0.1.0
gh release download agent-skills-v0.1.0 \
  --repo Yaxin9Luo/AutoDesign \
  --dir autodesign-skills-v0.1.0
cd autodesign-skills-v0.1.0
```

然后将四个 Skills 安装到 Codex 与 DeepSeek Harness 共用的用户目录：

```bash
DESTINATION="${DESTINATION:-$HOME/.agents/skills}"

for skill in autodesign-poster autodesign-ppt autodesign-webpage autodesign-video; do
  python3 -I ./package_agent_skills.py install \
    --archive "./${skill}-0.1.0.zip" \
    --checksum "./${skill}-0.1.0.zip.sha256" \
    --destination "$DESTINATION"
done
```

如果只需要某一种成果，单独执行对应 Skill 的安装命令即可。安装器会校验 SHA-256，
验证解压后的包，以原子方式完成安装，并拒绝覆盖已有安装。

## 按 Coding Agent 安装

Agent Skill 本质上是一个包含必需 `SKILL.md`，以及可选脚本、参考资料和资源文件的
目录。AutoDesign 遵循开放的
[Agent Skills 规范](https://agentskills.io/specification)。

### Codex

Codex 会从 `~/.agents/skills` 发现用户 Skills。较早的本地配置也可能使用
`~/.codex/skills`。

```bash
DESTINATION="$HOME/.agents/skills"
```

在执行[快速安装](#快速安装)的 shell 中将 `DESTINATION` 设为该值。如果安装后没有
立即显示这些 Skills，请新建一个 Codex 任务。详见官方
[Codex Skills 文档](https://developers.openai.com/codex/skills)。

### Claude Code

Claude Code 会从 `~/.claude/skills` 发现个人 Skills：

```bash
DESTINATION="$HOME/.claude/skills"
```

在执行[快速安装](#快速安装)循环前，在同一个 shell 中设置该值；安装循环会保留用户
已经选择的目标目录。Claude Code 通常能自动发现已有 Skills 目录中的变化。如果这是
本机首次创建 Skills 目录，请重启 Claude Code。详见官方
[Claude Code Skills 文档](https://code.claude.com/docs/en/skills)。

### DeepSeek Harness

DeepSeek Harness 支持项目级 `.dsh/skills` 与 `.agents/skills`，以及用户级
`~/.dsh/skills` 与 `~/.agents/skills`。如果只为 DSH 用户安装，请使用：

```bash
DESTINATION="$HOME/.dsh/skills"
```

在执行[快速安装](#快速安装)循环前，在同一个 shell 中设置该值；安装循环会保留用户
已经选择的目标目录。如果希望 Codex 与 DeepSeek Harness 共用一份安装，请改用
`DESTINATION="$HOME/.agents/skills"`。详见官方
[DeepSeek Harness Skills 子系统](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/skills.md)。

### 安装器说明与 Windows

[`agent-skills-v0.1.0` Release](https://github.com/Yaxin9Luo/AutoDesign/releases/tag/agent-skills-v0.1.0)
包含四个 ZIP、四个 SHA-256 校验文件、一个 manifest 和一个独立安装器。使用
GitHub CLI 下载：

```bash
mkdir -p autodesign-skills-v0.1.0
gh release download agent-skills-v0.1.0 \
  --repo Yaxin9Luo/AutoDesign \
  --dir autodesign-skills-v0.1.0
cd autodesign-skills-v0.1.0
```

选择 Python 3 启动命令：大多数 macOS/Linux 系统使用 `python3`，必要时使用
`python`，Windows 则使用 `py -3`。随后安装一个 Skill ZIP 及其对应校验文件：

```bash
python3 -I ./package_agent_skills.py install \
  --archive ./autodesign-poster-0.1.0.zip \
  --checksum ./autodesign-poster-0.1.0.zip.sha256 \
  --destination "$HOME/.agents/skills"
```

将 `autodesign-poster` 替换为其他 Skill 名称，并选择 Coding Agent 对应的安装目录。
安装器会校验 SHA-256、验证解压内容、以原子方式完成安装，并拒绝覆盖已有 Skill。

在 Windows 上安装全部四个 Skills 的示例（请根据 Codex、Claude Code 或 DeepSeek
Harness 修改 `$Destination`）：

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

## 首次使用

把论文 PDF 交给 Coding Agent，指定要使用的 Skill，并说明目标受众与输出目录。
例如：

| 宿主 | 示例 |
|---|---|
| Codex | `$autodesign-poster 将 /path/paper.pdf 转换为可编辑的 CVPR 横版海报，并把完整运行保存在 /path/output。` |
| Claude Code | `/autodesign-ppt 将 /path/paper.pdf 转换为面向技术受众的 18 页会议报告。` |
| DeepSeek Harness | `使用 autodesign-webpage 将 /path/paper.pdf 转换为响应式研究项目网页，并保存到 /path/output。` |
| 任意支持的宿主 | `使用 autodesign-video 基于 /path/paper.pdf 创建一段六分钟的会议视频，包含旁白与可选英文字幕。` |

Coding Agent 会读取 Skill 工作流、运行本地 `doctor`、准备来源证据、生成受约束的多轮
尝试、执行确定性 QA，并在最终交付前请求一次新的视觉评审。首次运行浏览器、PPT 或
Video Skill 时，可能会向用户缓存下载锁定版本的运行时依赖。

生成的交付物位于用户指定的 run 目录中。工作流不会把运行状态、缓存或成果写入
已安装的 Skill。

## 环境要求

四个 harness 都要求 Python 3.10 或更高版本。Video 当前因锁定的 Kokoro 与
HyperFrames 运行时要求 Python 3.10–3.12。

| Skill | 额外本地依赖 |
|---|---|
| Poster | Poppler：`pdftotext`、`pdfinfo`、`pdftoppm`、`pdfimages`；首次设置会在用户缓存中安装锁定版本的浏览器 |
| PPT | Poppler、LibreOffice；首次设置会在用户缓存中安装锁定版本的浏览器与原生 PPT 运行时 |
| Webpage | Poppler；首次设置会在用户缓存中安装锁定版本的浏览器 |
| Video | Poppler、Node.js 22+、npm、`ffmpeg`、`ffprobe`、Python 3.10–3.12；设置过程会安装精确版本 `hyperframes@0.7.86` 与锁定的 Kokoro 资源 |

设置工具会为受支持的 macOS、Linux 或 Windows 平台选择对应软件包；遇到不支持的
平台或架构时会直接阻止继续。首次设置运行时需要网络访问。请通过 Coding Agent
运行 Skill，让 Agent 根据 `doctor` 诊断解决依赖问题，而不是跳过必要检查。

<a id="skills-vs-full-autodesign-harness"></a>

## Skills 与完整 AutoDesign Harness

这四个 Skills 保留了 AutoDesign 的许多成果契约与本地 QA 检查，但它们运行在宿主
Coding Agent 内部，而不是完整的 AutoDesign 系统中。

| 方面 | 独立 Skills | 完整 AutoDesign Harness |
|---|---|---|
| 入口 | 现有 Coding Agent 对话 | AutoDesign Workbench 与完整流水线 |
| 编排 | 宿主 Agent 遵循一个便携工作流 | 协同的成果流水线、共享上下文、路由与生命周期管理 |
| 来源理解 | 宿主 Agent 加本地证据与来源记录工具 | 集成式论文记忆、planner、成果专用 harness 与共享来源状态 |
| 评审与迭代 | 确定性检查，加宿主 Agent 或子 Agent 的独立新评审 | 集成式 critic、重试路由、跨阶段诊断与 Workbench 反馈 |
| 编辑体验 | 文件与宿主 Coding Agent | Workbench、预览、多轮历史、Canvas 编辑与成果导出 |
| 输出一致性 | 更依赖具体 Agent、模型与本地环境 | 由完整优化流水线提供更多控制 |

独立 Skill 更容易安装，但不能替代 AutoDesign 的完整效果，也不保证相同的成果质量。
尤其是 PDF 理解、重要图片选择、视觉层级与多轮修复，目前仍更依赖宿主 Coding Agent。

我们的长期目标，是让这些便携 Skills 在受支持的成果工作流中达到完整 Harness 体验的
约 **70–80%**。这是未来路线图目标，不代表当前已经测得或保证的效果。

如需完整系统，请使用 [AutoDesign Workbench](../README.zh-CN.md#quickstart)与完整的
DesignHarness 流水线。

## 路线图与贡献

这些 Skills 正在持续更新。我们欢迎以下方向的贡献：

- PDF 理解、图片重要性排序、裁切选择与来源绑定；
- 成果规划、上下文管理与修复路由；
- Poster、PPT、Webpage 与 Video 的布局和可编辑导出；
- 确定性评估器、视觉评审标准与回归案例；
- 能暴露 Skills 与完整 Harness 差距的真实论文案例。

欢迎提交带有可复现论文/run 案例的
[Issue](https://github.com/Yaxin9Luo/AutoDesign/issues)，或提交包含针对性测试的
Pull Request。参与改进这些便携 Skills，也是在不削弱来源绑定与交付契约的前提下，
帮助它们逐步接近未来 70–80% 目标的直接方式。

## 维护者验证

验证全部四个包：

```bash
python3 -B scripts/validate_agent_skills.py --root agent_skills
```

构建确定性、不可覆盖的 Release 压缩包：

```bash
python3 -B scripts/package_agent_skills.py build \
  --source-root agent_skills \
  --output-dir dist/agent-skills-v0.1.0 \
  --version 0.1.0
```

输出目录会包含每个 Skill 对应的版本化 ZIP 与 SHA-256 校验文件，以及
`manifest.json`、Release 内可直接使用的 `package_agent_skills.py` 和
`validate_agent_skills.py`。发布前请构建两次，并逐字节比较全部文件。
