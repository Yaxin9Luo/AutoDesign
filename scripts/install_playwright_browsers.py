"""Install the Playwright browser binary required by AutoDesign render QA."""

from __future__ import annotations

import subprocess
import sys


def _chromium_launches() -> tuple[bool, str | None]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return False, f"playwright import failed: {type(e).__name__}: {e}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            browser.close()
        return True, None
    except Exception as e:
        return False, f"chromium launch failed: {type(e).__name__}: {e}"


def main() -> int:
    ok, warning = _chromium_launches()
    if ok:
        print("Playwright Chromium is already installed.")
        return 0
    if warning:
        print(warning)
    cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
    print("$ " + " ".join(cmd))
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        return result.returncode
    ok, warning = _chromium_launches()
    if not ok:
        print(warning or "chromium launch failed after install", file=sys.stderr)
        return 1
    print("Playwright Chromium installed and launch verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
