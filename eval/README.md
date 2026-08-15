<p align="center">
  <strong>English</strong> ·
  <a href="./README.zh-CN.md">简体中文</a> ·
  <a href="./README.ko.md">한국어</a>
</p>

# PosterBench

PosterBench is an image-native benchmark for academic poster generation.
It evaluates the final visible poster against its source paper, so systems that
produce PNG, JPEG, PDF, or HTML artifacts can be compared under the same
protocol.

The benchmark combines deterministic OCR/CV checks, dimension-specific visual
judging, and fixed score aggregation. The runnable entrypoint is
[`scripts/run_poster_benchmark_main_table.py`](../scripts/run_poster_benchmark_main_table.py),
with evaluator code in [`autodesign/evaluator/`](../autodesign/evaluator/).

## What is measured

| Dimension | Weight |
|---|---:|
| Faithfulness | 10 |
| Coverage | 10 |
| Density | 15 |
| Visual Evidence | 10 |
| Layout | 20 |
| Readability | 25 |
| Aesthetics | 10 |

The weighted rubric is followed by the strictest active record-level ceiling.
The four ceiling families capture severe layout damage, insufficient
presentation viability, confirmed visible failures, and protected render
integrity. Render integrity is a gate, not an eighth reward dimension.

## Fair evaluation

> [!IMPORTANT]
> For fair and comparable results, use **`gemini-3.5-flash`** as the judge
> model. This is the benchmark default, but published runs should pass it
> explicitly with `--model gemini-3.5-flash`.

Fair comparisons should also use the same paper corpus, candidate mapping,
evaluator commit, and runner settings. Do not use
`--allow-degraded-detectors` for official results.

## Setup

Install AutoDesign with the benchmark OCR dependencies:

```bash
uv sync --extra ocr
```

Configure the provider credentials required by the judge route. See
[`.env.example`](../.env.example) for the supported environment variables.

## Prepare the data

The repository publishes metadata for the 100-paper benchmark and the fixed
10-paper development subset, but it does **not** redistribute paper PDFs:

- [`benchmark_manifest.jsonl`](benchmark_manifest.jsonl) — title, authors,
  identifiers, official landing page, access policy, and expected SHA-256
- [`small_subset_ids.json`](small_subset_ids.json) — the fixed two-per-discipline
  10-paper subset

The same metadata-only releases are available from Hugging Face and can be
loaded directly:

```python
from datasets import load_dataset

posterbench = load_dataset("YaxinLuo/PosterBench", split="test")
posterbench_mini = load_dataset("YaxinLuo/PosterBench-mini", split="test")
```

The dataset repository license covers only the benchmark metadata,
benchmark-specific annotations, and documentation. It does not grant rights in
or redistribute the underlying papers.

Use the downloader to validate the manifests and inspect your local corpus
without making network requests:

```bash
uv --cache-dir .uv-cache run python scripts/download_benchmark_papers.py \
  --split small \
  --verify-only
```

To prepare the 10-paper development subset or the full 100-paper corpus:

```bash
# Fixed 10-paper development subset
uv --cache-dir .uv-cache run python scripts/download_benchmark_papers.py \
  --split small

# Full 100-paper benchmark
uv --cache-dir .uv-cache run python scripts/download_benchmark_papers.py \
  --split full
```

The downloader is intentionally strict. It automatically downloads a paper
only when the manifest has an HTTPS PDF URL, an approved Creative Commons or
public-domain license, and the downloaded file exactly matches the benchmark's
expected SHA-256. It does not search for mirrors, use cookies, bypass access
controls, or accept a different paper version.

Access metadata is maintained on a best-effort basis and is not legal advice;
users remain responsible for confirming that their access and use are lawful.

Records without sufficiently clear redistribution rights are reported as
`manual_access_required`. Follow the record's `landing_url`, obtain the paper
through a lawful source available to you, and save the exact benchmark version
at the path shown in the download report. Re-run with `--verify-only` to check
the file hash. Local papers are written under the ignored `eval/EvaData/`
directory:

```text
eval/EvaData/
  <discipline>/<case>/paper.pdf

/absolute/path/to/candidates/
  <discipline>/<case>/poster.png
```

The benchmark uses these discipline directories:

- `ai_ml_existing_20`
- `biomed_health`
- `climate_earth_environment`
- `economics_policy`
- `physics_astronomy`

For a custom system, the simplest path is to reuse a direct-directory input
slot such as `codex_native` and set its display name with `--system-label`.

## Run the benchmark

### 1. Check candidate mapping

Run the mapping preflight before making any model calls:

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

Review `case_mapping.csv` and resolve every missing, duplicate, or incorrect
mapping before scoring.

### 2. Run a smoke evaluation

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

### 3. Run the full evaluation

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

Use a comma-separated `--systems` list and the corresponding `--*-root`
arguments to compare multiple systems in one run. Run `--help` to see every
supported input slot.

## Results

The output directory contains:

- `benchmark_main_table_zh.html` — visual leaderboard and per-case results
- `benchmark_summary.json` — aggregate system scores and run metadata
- `scores.csv` and `scores.jsonl` — machine-readable per-poster scores
- `case_mapping.csv` — candidate-to-paper mapping audit
- `detector_preflight.json` — OCR/CV dependency status
- `candidates/` — per-poster deterministic, judge, and final reports

Runs are cached and can be resumed with the same command. Use `--force-vlm` to
rerun judge calls, or `--reaggregate-only` to rebuild aggregate scores from
existing compatible reports without calling the judge again.
