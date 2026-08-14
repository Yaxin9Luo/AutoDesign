/**
 * Compatibility shim. Real implementation moved to `api_settings.ts`
 * when v2 added per-agent model selection — this file is kept so old
 * imports (`@/lib/keys`) keep working without touching every call site.
 */
export { keyHeaders, maskKey, readKeys } from "./api_settings";
// Legacy types — `ApiKeyBag` no longer exists in the v2 schema, so map
// callers to a minimal shape that captures what they actually used.
export interface ApiKeyBag {
  openrouter?: string;
  gemini?: string;
}
import { clearConfig, readConfig, saveConfig } from "./api_settings";

/** Old API: save just the two legacy keys, leaving model picks alone. */
export function saveKeys(next: ApiKeyBag): void {
  const cfg = readConfig();
  cfg.keys.openrouter = next.openrouter?.trim() || undefined;
  cfg.keys.gemini = next.gemini?.trim() || undefined;
  saveConfig(cfg);
}

export function clearKeys(): void {
  clearConfig();
}
