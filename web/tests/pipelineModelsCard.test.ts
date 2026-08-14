import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { translate } from "../src/lib/i18n.ts";

const helperCopy = "Optional overrides for helper agents, never the coding agent. Leave text or vision blank to use the backend default: gpt-5.4-nano.";
const helperRoles = "enhancer · claim graph · outline · paper memory · composer";
const settingsSource = readFileSync(
  new URL("../src/components/SettingsDrawer.tsx", import.meta.url),
  "utf8",
);

test("Pipeline model controls keep Designer out of helper defaults", () => {
  assert.match(settingsSource, /placeholder="gpt-5\.4-nano"/);
  assert.ok(settingsSource.includes(helperCopy));
  assert.ok(settingsSource.includes(helperRoles));
  assert.ok(!settingsSource.includes("designer · enhancer · claim graph"));
});

test("Pipeline helper copy is localized for Chinese and Korean", () => {
  assert.notEqual(translate("zh", helperCopy), helperCopy);
  assert.notEqual(translate("ko", helperCopy), helperCopy);
  assert.notEqual(translate("zh", helperRoles), helperRoles);
  assert.notEqual(translate("ko", helperRoles), helperRoles);
});
