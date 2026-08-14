# Browser QA

Use for browser-rendered poster, deck, and landing artifacts. Screenshots and DOM inspection outrank static HTML parsing for layout truth.

## Stage: plan

Use stable selectors, block ids, local assets, and one clear root per frame. Read `browser_checks` before manual browser work.

## Stage: critique

Check root size, image loads, overflow, bounds, figure/caption collisions, and clipping from a current screenshot and DOM measurements; static HTML alone is not proof.

## Stage: repair

Fix missing roots, geometry, unloaded assets, and authored HTML/CSS directly; do not hide DOM faults with raster output. Read `browser_checks`.
