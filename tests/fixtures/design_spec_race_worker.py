from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

from autodesign.design_spec_persistence import (
    DesignSpecPersistenceError,
    commit_design_spec_revision,
)


def main() -> int:
    root = Path(sys.argv[1])
    request = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    start_path = Path(sys.argv[3])
    result_path = Path(sys.argv[4])

    deadline = time.monotonic() + 10
    while not start_path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("race start signal was not published")
        time.sleep(0.005)

    try:
        result = commit_design_spec_revision(
            canonical_path=root / "design_spec.json",
            artifact_type=str(request["artifact_type"]),
            design_spec=dict(request["design_spec"]),
            is_revision=True,
            expected_base_revision=int(request["expected_base_revision"]),
            expected_base_sha256=str(request["expected_base_sha256"]),
        )
        payload = {
            "status": "ok",
            "revision": result.revision,
            "design_spec_sha256": result.design_spec_sha256,
        }
    except DesignSpecPersistenceError as exc:
        payload = {"status": "error", "phase": exc.phase, "message": str(exc)}
    result_path.write_text(json.dumps(payload), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
