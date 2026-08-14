#!/usr/bin/env python3
"""Batch-generate paper project pages, then capture them against references."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


DEFAULT_REFERENCES = [
    "https://hongcanguo.github.io/Cola-DLM/",
    "https://cambrian-mllm.github.io/cambrian-p/",
    "https://cambrian-mllm.github.io/cambrian-s/",
    "https://vision-x-nyu.github.io/test-set-training/",
]

DEFAULT_PROMPT = (
    "请把这篇论文生成一个独立的 paper project page / paper-to-page 网页。"
    "目标是可传播、可浏览、可复现、入口齐全。按网页 viewport 逐页规划："
    "首屏放论文身份、作者、核心 thesis、横向资源按钮和主视觉；第二屏放 abstract "
    "和 framework/method 主图，abstract 要像 reference project page 一样是可读段落，"
    "不要拆成生硬的短文本块；后续放 key findings、demo/samples/results、benchmarks/"
    "ablations、citation/BibTeX。尽量使用更多论文原图；benchmark/ablation 用 native HTML "
    "table，不要只截图表格。整体美观服务于研究证据展示，不要做成营销页；"
    "先选择 page-level art direction 和 panel template：Hero、Resources、Abstract Narrative、"
    "Method/Framework、Result Dashboard、Demo Gallery、Benchmark Table、Analysis、Footer，"
    "每个 panel 都要像一个完整 viewport，而不是标题加段落的堆叠；"
    "samples/demo 必须做成真正 gallery 或 case-study strip，素材不足时合并到 findings；"
    "宽表不要撑破页面，超过 6 列就拆成 headline summary + full scroll table；"
    "最终文案不要出现 source-backed、ingested、fabricated、reconstructed 这类内部 harness 语言；"
    "先做 paper resource discovery，使用搜到的真实 arXiv/GitHub/Hugging Face/"
    "blog/twitter/demo/hardware-interface 链接作为带 icon 的横向 chips；"
    "不要编造链接，也不要把页面做成一堆 unavailable。"
)


def _slug(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", path.parent.name or path.stem).strip("-") or path.stem


def _discover_papers(root: Path) -> list[Path]:
    return sorted(root.glob("*/paper.pdf"))


def _parse_run_id(output: str) -> str | None:
    matches = re.findall(r'"run_id"\s*:\s*"([^"]+)"', output)
    if matches:
        return matches[-1]
    matches = re.findall(r"run_id[=:]\s*([0-9]{8}-[0-9]{6}-[A-Za-z0-9]+)", output)
    return matches[-1] if matches else None


def _run_one(pdf: Path, args: argparse.Namespace, batch_dir: Path) -> dict[str, Any]:
    name = _slug(pdf)
    work_dir = batch_dir / name
    work_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "autodesign.cli",
        "run",
        args.prompt,
        "--from-file",
        str(pdf),
    ]
    if args.skip_enhancer:
        cmd.append("--skip-enhancer")
    env = os.environ.copy()
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=Path.cwd(),
            env=env,
            timeout=args.timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", errors="replace")
        (work_dir / "stdout.log").write_text(stdout, encoding="utf-8")
        (work_dir / "stderr.log").write_text(stderr, encoding="utf-8")
        return {
            "paper": str(pdf),
            "name": name,
            "returncode": -1,
            "run_id": None,
            "html_path": None,
            "stdout_log": str(work_dir / "stdout.log"),
            "stderr_log": str(work_dir / "stderr.log"),
            "error": f"timeout after {args.timeout}s",
        }
    (work_dir / "stdout.log").write_text(proc.stdout, encoding="utf-8")
    (work_dir / "stderr.log").write_text(proc.stderr, encoding="utf-8")
    run_id = _parse_run_id(proc.stdout + "\n" + proc.stderr)
    html_path = None
    if run_id:
        candidate = Path("out/runs") / run_id / "final" / "index.html"
        if candidate.exists():
            html_path = candidate
    return {
        "paper": str(pdf),
        "name": name,
        "returncode": proc.returncode,
        "run_id": run_id,
        "html_path": str(html_path) if html_path else None,
        "stdout_log": str(work_dir / "stdout.log"),
        "stderr_log": str(work_dir / "stderr.log"),
    }


def _run_reference_harness(batch_dir: Path, references: list[str], pages: list[str], max_assets: int) -> dict[str, Any]:
    if not pages:
        return {"skipped": True, "reason": "no pages"}
    cmd = [
        sys.executable,
        "scripts/paper_page_reference_harness.py",
        "--out",
        str(batch_dir / "reference_harness"),
        "--max-assets",
        str(max_assets),
    ]
    for ref in references:
        cmd.extend(["--reference", ref])
    for page in pages:
        cmd.extend(["--page", page])
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=Path.cwd())
    (batch_dir / "reference_harness_stdout.log").write_text(proc.stdout, encoding="utf-8")
    (batch_dir / "reference_harness_stderr.log").write_text(proc.stderr, encoding="utf-8")
    return {
        "returncode": proc.returncode,
        "stdout_log": str(batch_dir / "reference_harness_stdout.log"),
        "stderr_log": str(batch_dir / "reference_harness_stderr.log"),
        "manifest": str(batch_dir / "reference_harness" / "manifest.json"),
    }


def _run_iteration_review(
    batch_dir: Path,
    references: list[str],
    pages: list[str],
    reference_manifest: str | None,
) -> dict[str, Any]:
    if not pages:
        return {"skipped": True, "reason": "no pages"}
    cmd = [
        sys.executable,
        "scripts/paper_page_iteration_review.py",
        "--out",
        str(batch_dir / "iteration_review"),
        "--label",
        batch_dir.name,
    ]
    for ref in references:
        cmd.extend(["--reference", ref])
    if reference_manifest and Path(reference_manifest).exists():
        cmd.extend(["--reference-manifest", reference_manifest])
    for page in pages:
        cmd.extend(["--page", page])
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=Path.cwd())
    (batch_dir / "iteration_review_stdout.log").write_text(proc.stdout, encoding="utf-8")
    (batch_dir / "iteration_review_stderr.log").write_text(proc.stderr, encoding="utf-8")
    return {
        "returncode": proc.returncode,
        "stdout_log": str(batch_dir / "iteration_review_stdout.log"),
        "stderr_log": str(batch_dir / "iteration_review_stderr.log"),
        "review_dir": str(batch_dir / "iteration_review"),
        "iteration_review": str(batch_dir / "iteration_review" / "iteration_review.json"),
        "system_patch_brief": str(batch_dir / "iteration_review" / "system_patch_brief.md"),
        "subagent_briefs": str(batch_dir / "iteration_review" / "subagent_briefs"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paper-root", default="out/four_paper_inputs_20260528")
    parser.add_argument("--paper", action="append", default=[])
    parser.add_argument("--out", default="out/diagnostics/paper_page_batch_loop")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--reference", action="append", default=[])
    parser.add_argument("--reference-max-assets", type=int, default=12)
    parser.add_argument("--skip-reference-harness", action="store_true")
    parser.add_argument("--skip-iteration-review", action="store_true")
    parser.add_argument("--skip-enhancer", action="store_true", default=True)
    parser.add_argument("--no-skip-enhancer", action="store_false", dest="skip_enhancer")
    args = parser.parse_args(argv)

    papers = [Path(p) for p in args.paper] if args.paper else _discover_papers(Path(args.paper_root))
    batch_dir = Path(args.out)
    batch_dir.mkdir(parents=True, exist_ok=True)
    references = args.reference or DEFAULT_REFERENCES

    results: list[dict[str, Any]] = []
    if not papers:
        print(f"no paper PDFs found under {args.paper_root}; pass --paper explicitly", file=sys.stderr)
        return 2

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(_run_one, pdf, args, batch_dir): pdf for pdf in papers}
        for future in concurrent.futures.as_completed(futures):
            pdf = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - batch diagnostics should keep going
                result = {
                    "paper": str(pdf),
                    "name": _slug(pdf),
                    "returncode": -1,
                    "run_id": None,
                    "html_path": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)

    pages = [r["html_path"] for r in results if r.get("html_path")]
    harness = (
        {"skipped": True, "reason": "--skip-reference-harness"}
        if args.skip_reference_harness
        else _run_reference_harness(batch_dir, references, pages, args.reference_max_assets)
    )
    iteration_review = (
        {"skipped": True, "reason": "--skip-iteration-review"}
        if args.skip_iteration_review
        else _run_iteration_review(
            batch_dir,
            references,
            pages,
            harness.get("manifest") if isinstance(harness, dict) else None,
        )
    )
    summary = {
        "papers": [str(p) for p in papers],
        "results": results,
        "reference_harness": harness,
        "iteration_review": iteration_review,
    }
    summary_path = batch_dir / "batch_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(summary_path)
    return 0 if (
        all(r["returncode"] == 0 for r in results)
        and harness.get("returncode", 0) == 0
        and iteration_review.get("returncode", 0) == 0
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
