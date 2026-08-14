# Browser checks

Use repository browser helpers for unattended runs. Manual work may set `AUTODESIGN_REPO_ROOT` and `AUTODESIGN_REFINE_ATTEMPT_DIR`; store screenshots, traces, and QA output under the run directory, never a new top-level folder.

Use stable selectors and a fresh screenshot after navigation or DOM-changing actions. Inspect root dimensions, loaded images, overflow, out-of-bounds blocks, caption/figure and footer collisions, table behavior, clipping, and fallback export paths. Browser evidence outranks static parsing.

Repair missing roots, geometry, assets, and authored DOM/CSS directly. Prefer selector-based actions. Never rasterize a poster or slide to hide a browser-layout failure.
