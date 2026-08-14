import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const vite = await createServer({
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true },
  appType: "custom",
});
const { PROVIDERS, VISION_MODELS } =
  await vite.ssrLoadModule("/src/lib/catalog.ts") as
    typeof import("../src/lib/catalog.ts");
await vite.close();

test("OpenAI-compatible model pickers expose the default nano model", () => {
  const direct = PROVIDERS.find((provider) => provider.id === "openai");
  const custom = PROVIDERS.find((provider) => provider.id === "custom_openai");
  const openrouter = PROVIDERS.find((provider) => provider.id === "openrouter");

  assert.ok(direct?.models.includes("gpt-5.4-nano"));
  assert.ok(custom?.models.includes("gpt-5.5"));
  assert.ok(custom?.models.includes("gpt-5.4-nano"));
  assert.ok(openrouter?.models.includes("openai/gpt-5.4-nano"));
  assert.ok(VISION_MODELS.includes("gpt-5.4-nano"));
  assert.ok(VISION_MODELS.includes("openai/gpt-5.4-nano"));
});
