<p align="center">
  <a href="./README.md">English</a> ·
  <a href="./README.zh-CN.md">简体中文</a> ·
  <strong>한국어</strong>
</p>

# PosterBench

PosterBench는 학술 포스터 생성을 위한 이미지 네이티브 벤치마크입니다. 최종적으로 보이는 포스터를 원문 논문과 대조해 평가하므로 PNG, JPEG, PDF 또는 HTML 결과물을 생성하는 시스템을 동일한 프로토콜로 비교할 수 있습니다.

이 벤치마크는 결정론적 OCR/CV 검사, 차원별 시각 평가, 고정된 점수 집계를 결합합니다. 실행 진입점은 [`scripts/run_poster_benchmark_main_table.py`](../scripts/run_poster_benchmark_main_table.py)이며, 평가기 코드는 [`autodesign/evaluator/`](../autodesign/evaluator/)에 있습니다.

## 평가 항목

| 평가 차원 | 가중치 |
|---|---:|
| Faithfulness(충실도) | 10 |
| Coverage(포괄성) | 10 |
| Density(밀도) | 15 |
| Visual Evidence(시각적 근거) | 10 |
| Layout(레이아웃) | 20 |
| Readability(가독성) | 25 |
| Aesthetics(미학) | 10 |

가중 차원 점수를 계산한 뒤 각 레코드에 가장 엄격한 활성 점수 상한을 적용합니다. 네 가지 상한은 심각한 레이아웃 손상, 부족한 presentation viability, 확인된 가시적 실패, 보호된 render integrity를 다룹니다. Render integrity는 여덟 번째 보상 차원이 아니라 gate입니다.

## 공정한 평가

> [!IMPORTANT]
> 공정하고 비교 가능한 결과를 얻으려면 Judge 모델로 **`gemini-3.5-flash`**를 사용하세요. 벤치마크의 기본값이지만, 공개하는 실행에서는 `--model gemini-3.5-flash`를 명시적으로 전달해야 합니다.

공정한 비교를 위해 동일한 논문 코퍼스, 후보 매핑, 평가기 커밋, 실행 설정을 사용해야 합니다. 공식 결과에는 `--allow-degraded-detectors`를 사용하지 마세요.

## 환경 설정

벤치마크 OCR 의존성과 함께 AutoDesign을 설치합니다.

```bash
uv sync --extra ocr
```

Judge 경로에 필요한 제공자 자격 증명을 설정하세요. 지원되는 환경 변수는 [`.env.example`](../.env.example)을 참고하세요.

## 데이터 준비

저장소는 100편 벤치마크와 고정된 10편 개발 하위 집합의 메타데이터를 공개하지만, 논문 PDF를 **재배포하지 않습니다**.

- [`benchmark_manifest.jsonl`](benchmark_manifest.jsonl) — 제목, 저자, 식별자, 공식 랜딩 페이지, 접근 정책, 예상 SHA-256
- [`small_subset_ids.json`](small_subset_ids.json) — 분야별 두 편으로 고정된 10편 하위 집합

동일한 메타데이터 전용 release를 Hugging Face에서도 직접 불러올 수 있습니다.

```python
from datasets import load_dataset

posterbench = load_dataset("YaxinLuo/PosterBench", split="test")
posterbench_mini = load_dataset("YaxinLuo/PosterBench-mini", split="test")
```

dataset repository의 라이선스는 benchmark 메타데이터, benchmark 전용 주석,
문서에만 적용됩니다. 원문 논문에 대한 권리를 부여하거나 논문 콘텐츠를
재배포하지 않습니다.

네트워크 요청 없이 다운로드 도구로 Manifest를 검증하고 로컬 코퍼스를 확인할 수 있습니다.

```bash
uv --cache-dir .uv-cache run python scripts/download_benchmark_papers.py \
  --split small \
  --verify-only
```

10편 개발 하위 집합 또는 전체 100편 코퍼스를 준비합니다.

```bash
# Fixed 10-paper development subset
uv --cache-dir .uv-cache run python scripts/download_benchmark_papers.py \
  --split small

# Full 100-paper benchmark
uv --cache-dir .uv-cache run python scripts/download_benchmark_papers.py \
  --split full
```

다운로드 도구는 의도적으로 엄격하게 동작합니다. Manifest에 HTTPS PDF URL과 승인된 Creative Commons 또는 Public Domain 라이선스가 있고, 다운로드한 파일이 벤치마크의 예상 SHA-256과 정확히 일치할 때만 논문을 자동으로 내려받습니다. 미러를 검색하거나, Cookie를 사용하거나, 접근 제한을 우회하거나, 다른 논문 버전을 허용하지 않습니다.

접근 메타데이터는 최선의 노력으로 관리되며 법률 자문이 아닙니다. 접근과 사용이 합법적인지 확인할 책임은 사용자에게 있습니다.

재배포 권리가 충분히 명확하지 않은 레코드는 `manual_access_required`로 보고됩니다. 레코드의 `landing_url`을 방문하고, 본인이 합법적으로 이용할 수 있는 출처에서 논문을 구한 뒤, 다운로드 보고서에 표시된 경로에 벤치마크와 정확히 같은 버전을 저장하세요. `--verify-only`를 다시 실행하면 파일 해시를 확인할 수 있습니다. 로컬 논문은 Git에서 제외된 `eval/EvaData/` 디렉터리에 저장됩니다.

```text
eval/EvaData/
  <discipline>/<case>/paper.pdf

/absolute/path/to/candidates/
  <discipline>/<case>/poster.png
```

벤치마크는 다음 분야 디렉터리를 사용합니다.

- `ai_ml_existing_20`
- `biomed_health`
- `climate_earth_environment`
- `economics_policy`
- `physics_astronomy`

사용자 정의 시스템은 `codex_native` 같은 직접 디렉터리 입력 슬롯을 재사용하고, `--system-label`로 표시 이름을 설정하는 방식이 가장 간단합니다.

## 벤치마크 실행

### 1. 후보 매핑 확인

모델을 호출하기 전에 매핑 사전 검사를 실행합니다.

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

채점하기 전에 `case_mapping.csv`를 검토하고 누락, 중복 또는 잘못된 매핑을 모두 해결하세요.

### 2. Smoke 평가 실행

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

### 3. 전체 평가 실행

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

한 번의 실행에서 여러 시스템을 비교하려면 `--systems`에 쉼표로 구분한 목록을 전달하고, 각 시스템에 해당하는 `--*-root` 인자를 제공하세요. 지원되는 모든 입력 슬롯은 `--help`에서 확인할 수 있습니다.

## 결과

출력 디렉터리에는 다음 파일이 포함됩니다.

- `benchmark_main_table_zh.html` — 시각적 리더보드와 case별 결과
- `benchmark_summary.json` — 집계된 시스템 점수와 실행 메타데이터
- `scores.csv`와 `scores.jsonl` — 기계가 읽을 수 있는 포스터별 점수
- `case_mapping.csv` — 후보와 논문의 매핑 감사 자료
- `detector_preflight.json` — OCR/CV 의존성 상태
- `candidates/` — 포스터별 결정론적 검사, Judge 보고서, 최종 보고서

실행 결과는 캐시되며 같은 명령으로 이어서 실행할 수 있습니다. `--force-vlm`을 사용하면 Judge 호출을 다시 실행하고, `--reaggregate-only`를 사용하면 Judge를 다시 호출하지 않고도 기존의 호환 가능한 보고서에서 집계 점수를 다시 만들 수 있습니다.
