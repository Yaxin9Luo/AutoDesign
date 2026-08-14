/** Validate the OpenAI-compatible endpoint before persisting it. */
export function customOpenAIBaseUrlError(
  value: string | undefined,
  customOpenAIKey?: string | undefined,
): "required" | "invalid" | null {
  const baseUrl = value?.trim();
  if (!baseUrl) return customOpenAIKey?.trim() ? "required" : null;
  if (!/^https?:\/\//i.test(baseUrl)) return "invalid";
  try {
    const parsed = new URL(baseUrl);
    if ((parsed.protocol !== "http:" && parsed.protocol !== "https:") || !parsed.hostname) {
      return "invalid";
    }
  } catch {
    return "invalid";
  }
  return null;
}
