#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def _write_forever(path: Path) -> int:
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        with path.open("a", encoding="utf-8") as handle:
            handle.write("tick\n")
            handle.flush()
            os.fsync(handle.fileno())
        time.sleep(0.02)


def _daemonize(pid_path: Path, heartbeat_path: Path) -> int:
    child_pid = os.fork()
    if child_pid:
        return 0
    os.setsid()
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    return _write_forever(heartbeat_path)


def main() -> int:
    if len(sys.argv) >= 4 and sys.argv[1] == "--daemonize":
        return _daemonize(Path(sys.argv[2]), Path(sys.argv[3]))
    if len(sys.argv) >= 2 and sys.argv[1] == "lint":
        project = Path.cwd()
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--daemonize",
                str(project / "detached-writer.pid"),
                str(project / "detached-writer-heartbeat.txt"),
            ],
            check=True,
        )
        while True:
            time.sleep(0.05)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
