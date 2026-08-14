import type { Message } from "./types";

export const restoredPosterPaletteId = (
  messages: Message[],
  stored: unknown,
): string | null => {
  // An explicit null is a catalog invalidation, not a legacy missing value.
  if (stored === null) return null;
  if (typeof stored === "string" && stored.trim()) return stored.trim();
  for (let idx = messages.length - 1; idx >= 0; idx -= 1) {
    const payload = messages[idx]?.task_payload;
    if (payload?.artifact_type === "poster" && payload.palette_id) {
      return payload.palette_id;
    }
  }
  return null;
};
