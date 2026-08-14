import type { ArtifactAsset } from "./types";

export const PAPER_ASSET_DRAG_MIME = "application/x-autodesign-paper-asset";

type DragAssetPayload = Pick<
  ArtifactAsset,
  "asset_id" | "name" | "kind" | "url" | "filename" | "run_id" | "source" | "size"
>;

export function encodePaperAssetDrag(asset: ArtifactAsset): string {
  const payload: DragAssetPayload = {
    asset_id: asset.asset_id,
    name: asset.name,
    kind: asset.kind,
    url: asset.url,
    filename: asset.filename,
    run_id: asset.run_id,
    source: asset.source,
    size: asset.size,
  };
  return JSON.stringify(payload);
}

export function hasPaperAssetDrag(dataTransfer: DataTransfer | null): boolean {
  if (!dataTransfer) return false;
  return Array.from(dataTransfer.types).includes(PAPER_ASSET_DRAG_MIME);
}

export function readPaperAssetDrag(dataTransfer: DataTransfer | null): ArtifactAsset | null {
  if (!dataTransfer) return null;
  const raw = dataTransfer.getData(PAPER_ASSET_DRAG_MIME);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<ArtifactAsset>;
    if (
      typeof parsed.asset_id !== "string" ||
      typeof parsed.name !== "string" ||
      (parsed.kind !== "figure" && parsed.kind !== "table" && parsed.kind !== "image") ||
      typeof parsed.url !== "string" ||
      typeof parsed.filename !== "string" ||
      typeof parsed.run_id !== "string" ||
      typeof parsed.source !== "string" ||
      typeof parsed.size !== "number"
    ) {
      return null;
    }
    return parsed as ArtifactAsset;
  } catch {
    return null;
  }
}
