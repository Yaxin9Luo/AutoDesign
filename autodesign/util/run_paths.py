"""Run directory path helpers shared between CLI, runner, and ingest tool.

`resolve_run_dir` collapses the three-way logic that was duplicated across
`cli.py`, `runner.py`, and `tools/ingest_document.py`: accept either a raw
run_id (looked up under `settings.out_dir/runs/`) or a filesystem path
(absolute or relative), and return the resolved absolute path.
"""

from __future__ import annotations

from pathlib import Path


def resolve_run_dir(out_dir: Path | str, value: str) -> Path:
    """Resolve a run reference (run_id or filesystem path) to an absolute path.

    Semantics (kept identical to the pre-existing duplicated logic):
      1. Expand ``~``.
      2. If the value is already absolute, return it resolved.
      3. If it's relative and the path exists, return it resolved.
      4. Otherwise fall back to ``<out_dir>/runs/<value>``.

    The caller is responsible for asserting existence; this helper is a
    pure path resolver so it can be used both by callers that will error
    on missing directories (CLI) and by callers that expect the target to
    already be there (runner).
    """
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    if path.exists():
        return path.resolve()
    return (Path(out_dir) / "runs" / value).resolve()
