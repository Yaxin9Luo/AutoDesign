import assert from "node:assert/strict";
import test from "node:test";

import { findInjectedStyle } from "../src/components/canvas/styleTagCompatibility.ts";


type StyleStub = { id: string };

function documentWith(...elements: StyleStub[]): Document {
  return {
    getElementById(id: string) {
      return elements.find((element) => element.id === id) ?? null;
    },
  } as unknown as Document;
}

test("renames and reuses a legacy injected style", () => {
  const legacy = { id: "designanything-web-selection-style" };
  const doc = documentWith(legacy);

  const found = findInjectedStyle(
    doc,
    "autodesign-web-selection-style",
    "designanything-web-selection-style",
  );

  assert.equal(found, legacy);
  assert.equal(legacy.id, "autodesign-web-selection-style");
  assert.equal(doc.getElementById("designanything-web-selection-style"), null);
  assert.equal(doc.getElementById("autodesign-web-selection-style"), legacy);
});

test("keeps an existing canonical injected style", () => {
  const canonical = { id: "autodesign-style-tweaks" };
  const doc = documentWith(canonical);

  const found = findInjectedStyle(
    doc,
    "autodesign-style-tweaks",
    "designanything-style-tweaks",
  );

  assert.equal(found, canonical);
  assert.equal(canonical.id, "autodesign-style-tweaks");
});

test("returns null when neither style ID exists", () => {
  assert.equal(
    findInjectedStyle(
      documentWith(),
      "autodesign-web-flow-layout-editor",
      "designanything-web-flow-layout-editor",
    ),
    null,
  );
});
