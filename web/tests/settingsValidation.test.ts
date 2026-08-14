import assert from "node:assert/strict";
import test from "node:test";

import { customOpenAIBaseUrlError } from "../src/lib/settings_validation.ts";

test("Custom OpenAI base URL accepts public, localhost, and private HTTP(S) hosts", () => {
  for (const value of [
    "https://api.example.com/v1",
    "http://localhost:11434/v1",
    "http://127.0.0.1:8000/v1",
    "http://192.168.1.10:8080/v1",
    "https://10.0.0.8/v1",
    "http://[::1]:1234/v1",
  ]) {
    assert.equal(customOpenAIBaseUrlError(value), null, value);
  }
});

test("Custom OpenAI base URL rejects non-HTTP(S), hostless, and malformed values", () => {
  for (const value of [
    "api.example.com/v1",
    "ftp://api.example.com/v1",
    "https://",
    "https://?version=v1",
    "http:/api.example.com/v1",
  ]) {
    assert.equal(customOpenAIBaseUrlError(value), "invalid", value);
  }
});

test("Custom OpenAI API keys require a nonempty base URL", () => {
  assert.equal(customOpenAIBaseUrlError("", "sk-custom"), "required");
  assert.equal(customOpenAIBaseUrlError("   ", "sk-custom"), "required");
  assert.equal(customOpenAIBaseUrlError("api.example.com/v1", "sk-custom"), "invalid");
  assert.equal(customOpenAIBaseUrlError("https://api.example.com/v1", "sk-custom"), null);
});

test("Custom OpenAI base URL permits an empty value only without an API key", () => {
  assert.equal(customOpenAIBaseUrlError("   "), null);
  assert.equal(customOpenAIBaseUrlError("", "   "), null);
});
