import assert from "node:assert/strict";
import test from "node:test";

class MemoryStorage implements Storage {
  private readonly values = new Map<string, string>();

  get length() { return this.values.size; }
  clear() { this.values.clear(); }
  getItem(key: string) { return this.values.get(key) ?? null; }
  key(index: number) { return [...this.values.keys()][index] ?? null; }
  removeItem(key: string) { this.values.delete(key); }
  setItem(key: string, value: string) { this.values.set(key, value); }
}

const localStorage = new MemoryStorage();
Object.assign(globalThis, { window: globalThis, localStorage });

const { configHeaders, readConfig, saveConfig } = await import("../src/lib/api_settings.ts");

test("Pi settings preserve the harness and optional provider/model selection", () => {
  localStorage.clear();
  saveConfig({
    keys: {},
    bases: {},
    models: {},
    pipeline_models: {},
    harnesses: {
      designer_author: "pi",
      designer_author_model: "   ",
      code_editor: "pi",
      code_editor_model: "   ",
    },
    openresearch: {},
  });

  const restored = readConfig();
  assert.deepEqual(restored.harnesses, {
    designer_author: "pi",
    code_editor: "pi",
  });
  assert.deepEqual(configHeaders(restored), {
    "X-Designer-Author-Harness": "pi",
    "X-Code-Editor-Harness": "pi",
    "X-Model-Image": "openai/gpt-5-image-mini",
  });

  localStorage.clear();
  saveConfig({
    keys: {},
    bases: {},
    models: {},
    pipeline_models: {},
    harnesses: {
      designer_author: "pi",
      designer_author_model: " openai/gpt-5.5 ",
      code_editor: "pi",
      code_editor_model: " openai/gpt-5.5 ",
    },
    openresearch: {},
  });

  const restoredWithModel = readConfig();
  assert.equal(restoredWithModel.harnesses.designer_author_model, "openai/gpt-5.5");
  assert.equal(restoredWithModel.harnesses.code_editor_model, "openai/gpt-5.5");
});

test("Coding-harness models stay isolated from pipeline helper models", () => {
  const configuredHarness = configHeaders({
    keys: { openai: "test-openai-key" },
    bases: {},
    models: {},
    pipeline_models: {},
    harnesses: {
      designer_author: "codex",
      designer_author_model: "gpt-5.5",
    },
    openresearch: {},
  });
  assert.equal(configuredHarness["X-Designer-Author-Model"], "gpt-5.5");
  assert.equal(configuredHarness["X-Model-Designer"], undefined);
  assert.equal(configuredHarness["X-Model-Enhancer"], undefined);
  assert.equal(configuredHarness["X-Model-Ingest"], undefined);

  const blankPipeline = configHeaders({
    keys: { openai: "test-openai-key" },
    bases: {},
    models: {},
    pipeline_models: { text: "   ", vision: "" },
    harnesses: {
      designer_author: "codex",
      designer_author_model: "gpt-5.5",
    },
    openresearch: {},
  });
  assert.equal(blankPipeline["X-Model-Designer"], undefined);
  assert.equal(blankPipeline["X-Model-Enhancer"], undefined);
  assert.equal(blankPipeline["X-Model-Ingest"], undefined);

  const harnessDefault = configHeaders({
    keys: { openai: "test-openai-key" },
    bases: {},
    models: {},
    pipeline_models: {},
    harnesses: { designer_author: "codex" },
    openresearch: {},
  });
  assert.equal(harnessDefault["X-Model-Designer"], undefined);
});

test("Explicit pipeline models configure helper roles without overriding Designer", () => {
  const headers = configHeaders({
    keys: { custom_openai: "test-company-key" },
    bases: { custom_openai: "https://company.example/v1" },
    models: {},
    pipeline_models: {
      text: "helper-text-model",
      vision: "helper-vision-model",
    },
    harnesses: {
      designer_author: "codex",
      designer_author_model: "gpt-5.5",
    },
    openresearch: {},
  });

  assert.equal(headers["X-Designer-Author-Model"], "gpt-5.5");
  assert.equal(headers["X-Model-Designer"], undefined);
  for (const name of [
    "X-Model-Enhancer",
    "X-Model-Claim-Graph",
    "X-Model-Deck-Outline",
    "X-Model-Paper-Memory",
    "X-Model-Composer",
  ]) {
    assert.equal(headers[name], "helper-text-model", name);
  }
  for (const name of ["X-Model-Ingest", "X-Model-Critic"]) {
    assert.equal(headers[name], "helper-vision-model", name);
  }
});
