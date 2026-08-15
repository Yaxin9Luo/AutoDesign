<p align="center">
  <a href="./README.md">English</a> ·
  <strong>简体中文</strong> ·
  <a href="./README.ko.md">한국어</a>
</p>

# PosterBench

PosterBench 是一个面向学术海报生成的图像原生 Benchmark。它根据原始论文评估最终可见的海报，因此生成 PNG、JPEG、PDF 或 HTML 成果的系统都可以在同一套协议下进行比较。

Benchmark 结合确定性的 OCR/CV 检查、按维度划分的视觉评审和固定的分数聚合。可执行入口为 [`scripts/run_poster_benchmark_main_table.py`](../scripts/run_poster_benchmark_main_table.py)，评估器代码位于 [`autodesign/evaluator/`](../autodesign/evaluator/)。

## 评测内容

| 维度 | 权重 |
|---|---:|
| Faithfulness（忠实度） | 10 |
| Coverage（覆盖度） | 10 |
| Density（密度） | 15 |
| Visual Evidence（视觉证据） | 10 |
| Layout（布局） | 20 |
| Readability（可读性） | 25 |
| Aesthetics（美感） | 10 |

加权维度分之后，还会对每条记录应用最严格的有效分数上限。四类上限分别对应严重布局损坏、展示可用性不足、已确认的可见失败，以及受保护的渲染完整性。渲染完整性是 gate，不是第八个奖励维度。

## 公平评测

> [!IMPORTANT]
> 为获得公平且可比较的结果，请使用 **`gemini-3.5-flash`** 作为 Judge 模型。它是 Benchmark 的默认设置，但公开结果仍应显式传入 `--model gemini-3.5-flash`。

公平比较还应使用相同的论文语料、候选映射、评估器 Commit 和 Runner 设置。正式结果不要使用 `--allow-degraded-detectors`。

## 配置环境

安装 AutoDesign 以及 Benchmark 所需的 OCR 依赖：

```bash
uv sync --extra ocr
```

配置 Judge 路由需要的 Provider 凭据。支持的环境变量请参阅 [`.env.example`](../.env.example)。

## 准备数据

仓库会公开 100 篇论文 Benchmark 和固定 10 篇开发子集的元数据，但**不会重新分发论文 PDF**：

- [`benchmark_manifest.jsonl`](benchmark_manifest.jsonl) — 标题、作者、标识符、官方落地页、访问策略和预期 SHA-256
- [`small_subset_ids.json`](small_subset_ids.json) — 每个学科固定两篇、共 10 篇的子集

同一份仅含元数据的 release 也已发布到 Hugging Face，可直接加载：

```python
from datasets import load_dataset

posterbench = load_dataset("YaxinLuo/PosterBench", split="test")
posterbench_mini = load_dataset("YaxinLuo/PosterBench-mini", split="test")
```

dataset repository 的许可证只覆盖 benchmark 元数据、benchmark 专用标注和文档，
不授予底层论文的任何权利，也不会重新分发论文内容。

无需访问网络，即可用下载器验证 Manifest 并检查本地语料：

```bash
uv --cache-dir .uv-cache run python scripts/download_benchmark_papers.py \
  --split small \
  --verify-only
```

准备 10 篇开发子集或完整的 100 篇语料：

```bash
# Fixed 10-paper development subset
uv --cache-dir .uv-cache run python scripts/download_benchmark_papers.py \
  --split small

# Full 100-paper benchmark
uv --cache-dir .uv-cache run python scripts/download_benchmark_papers.py \
  --split full
```

下载器采用严格策略。只有当 Manifest 同时包含 HTTPS PDF 地址、认可的 Creative Commons 或 Public Domain 许可证，并且下载文件与 Benchmark 预期 SHA-256 完全一致时，才会自动下载论文。它不会搜索镜像、使用 Cookie、绕过访问控制，也不会接受不同版本的论文。

访问元数据仅按尽力原则维护，不构成法律建议；用户有责任确认自己的访问与使用行为合法。

对于重新分发权利不够明确的记录，下载器会报告 `manual_access_required`。请访问记录中的 `landing_url`，通过你有权使用的合法来源获取论文，并将 Benchmark 对应的准确版本保存到下载报告给出的路径。再次运行 `--verify-only` 即可检查文件哈希。本地论文会写入已忽略的 `eval/EvaData/` 目录：

```text
eval/EvaData/
  <discipline>/<case>/paper.pdf

/absolute/path/to/candidates/
  <discipline>/<case>/poster.png
```

Benchmark 使用以下学科目录：

- `ai_ml_existing_20`
- `biomed_health`
- `climate_earth_environment`
- `economics_policy`
- `physics_astronomy`

对于自定义系统，最简单的方式是复用 `codex_native` 等直接目录输入槽位，并通过 `--system-label` 设置展示名称。

## 运行 Benchmark

### 1. 检查候选映射

在发起任何模型调用之前，先运行映射预检：

```bash
uv --cache-dir .uv-cache run --extra ocr \
  python scripts/run_poster_benchmark_main_table.py \
  --paper-root eval/EvaData \
  --systems codex_native \
  --codex-native-root /absolute/path/to/candidates \
  --system-label "codex_native=Your System" \
  --dry-map \
  --out-dir out/eval/your-system
```

评分前请检查 `case_mapping.csv`，解决所有缺失、重复或错误的映射。

### 2. 运行 Smoke 评测

```bash
uv --cache-dir .uv-cache run --extra ocr \
  python scripts/run_poster_benchmark_main_table.py \
  --paper-root eval/EvaData \
  --systems codex_native \
  --codex-native-root /absolute/path/to/candidates \
  --system-label "codex_native=Your System" \
  --model gemini-3.5-flash \
  --limit 2 \
  --workers 1 \
  --out-dir out/eval/your-system-smoke
```

### 3. 运行完整评测

```bash
uv --cache-dir .uv-cache run --extra ocr \
  python scripts/run_poster_benchmark_main_table.py \
  --paper-root eval/EvaData \
  --systems codex_native \
  --codex-native-root /absolute/path/to/candidates \
  --system-label "codex_native=Your System" \
  --model gemini-3.5-flash \
  --workers 4 \
  --out-dir out/eval/your-system
```

要在一次运行中比较多个系统，请向 `--systems` 传入逗号分隔的列表，并提供对应的 `--*-root` 参数。运行 `--help` 可以查看所有支持的输入槽位。

## 评测结果

输出目录包含：

- `benchmark_main_table_zh.html` — 可视化排行榜和逐 Case 结果
- `benchmark_summary.json` — 聚合后的系统分数和运行元数据
- `scores.csv` 和 `scores.jsonl` — 机器可读的逐海报分数
- `case_mapping.csv` — 候选与论文映射审计
- `detector_preflight.json` — OCR/CV 依赖状态
- `candidates/` — 每张海报的确定性检查、Judge 报告和最终报告

运行结果会被缓存，同一命令可以继续执行。使用 `--force-vlm` 重新运行 Judge 调用；使用 `--reaggregate-only` 可以基于已有且兼容的报告重建聚合分数，而无需再次调用 Judge。
