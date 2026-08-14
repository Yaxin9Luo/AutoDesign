import { useEffect, useState } from "react";

export function formatElapsedSince(startMs: number, nowMs: number): string {
  const seconds = Math.max(0, Math.floor((nowMs - startMs) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  return `${minutes}m ${remainingSeconds.toString().padStart(2, "0")}s`;
}

export function useElapsed(startMs?: number, endMs?: number): string | null {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (typeof startMs !== "number" || typeof endMs === "number") return undefined;
    setNow(Date.now());
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [endMs, startMs]);

  return typeof startMs === "number"
    ? formatElapsedSince(startMs, typeof endMs === "number" ? endMs : now)
    : null;
}
