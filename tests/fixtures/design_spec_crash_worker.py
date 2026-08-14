from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from autodesign.design_spec_persistence import commit_design_spec_revision


def main() -> int:
    root = Path(sys.argv[1])
    phase = sys.argv[2]
    request = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))

    def hard_exit_hook(observed_phase: str) -> None:
        if observed_phase == phase:
            os._exit(91)

    commit_design_spec_revision(
        canonical_path=root / "design_spec.json",
        artifact_type=str(request["artifact_type"]),
        design_spec=dict(request["design_spec"]),
        is_revision=True,
        expected_base_revision=int(request["expected_base_revision"]),
        expected_base_sha256=str(request["expected_base_sha256"]),
        phase_hook=hard_exit_hook,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
