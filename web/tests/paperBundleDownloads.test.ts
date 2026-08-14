import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const vite = await createServer({
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true },
  appType: "custom",
});
const downloadMenu = await vite.ssrLoadModule("/src/components/ArtifactDownloadMenu.tsx");
const paperBundle = await vite.ssrLoadModule("/src/lib/paper_bundle.ts");
await vite.close();

test("running paper bundles disable only the agent-backed PPTX export", () => {
  const isDisabled = downloadMenu.isArtifactExportDisabled as
    | ((format: string, unavailable: boolean, busy: boolean, pptxDisabled: boolean) => boolean)
    | undefined;

  assert.equal(typeof isDisabled, "function");
  if (!isDisabled) return;
  assert.equal(isDisabled("pptx", false, false, true), true);
  assert.equal(isDisabled("pdf", false, false, true), false);
  assert.equal(isDisabled("original_html", false, false, true), false);
  assert.equal(isDisabled("standalone_html", false, false, true), false);
});

test("cancelling paper bundles keep the agent-backed PPTX export disabled", () => {
  const blocksPptx = paperBundle.paperBundleBlocksPptxExport as
    | ((bundle: ReturnType<typeof paperBundle.createPaperBundleParentState>) => boolean)
    | undefined;
  assert.equal(typeof blocksPptx, "function");
  if (!blocksPptx) return;
  const bundle = paperBundle.createPaperBundleParentState("parent", "paper.pdf");
  bundle.tasks.poster.status = "complete";
  bundle.tasks.deck.status = "cancelling";
  bundle.tasks.landing.status = "cancelled";
  bundle.tasks.video.status = "failed";

  assert.equal(blocksPptx(bundle), true);
  bundle.tasks.deck.status = "cancelled";
  assert.equal(blocksPptx(bundle), false);
  bundle.backend_state = "cancelling";
  assert.equal(blocksPptx(bundle), true);
  bundle.backend_state = "completed";
  assert.equal(blocksPptx(bundle), false);
});

test("ordinary chat and canvas download menus receive the bundle PPTX guard", async () => {
  const [chatSource, canvasSource] = await Promise.all([
    readFile(fileURLToPath(new URL("../src/components/Chat.tsx", import.meta.url)), "utf8"),
    readFile(fileURLToPath(new URL("../src/components/Canvas.tsx", import.meta.url)), "utf8"),
  ]);

  assert.match(
    chatSource,
    /<ArtifactDownloadMenu[\s\S]*?pptxExportDisabled=\{paperBundlePptxExportDisabled\}[\s\S]*?\/>/,
  );
  assert.equal(
    canvasSource.match(/pptxExportDisabled=\{pptxExportDisabled\}/g)?.length,
    2,
  );
  assert.match(chatSource, /paperBundleBlocksPptxExport\(parent\.paper_bundle\)/);
  assert.match(canvasSource, /paperBundleBlocksPptxExport\(parent\.paper_bundle\)/);
});
