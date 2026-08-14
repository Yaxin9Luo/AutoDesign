export interface ArtifactViewportSize {
  w: number;
  h: number;
}

function completeSize(value: ArtifactViewportSize): ArtifactViewportSize | null {
  return Number.isFinite(value.w) && value.w > 0
    && Number.isFinite(value.h) && value.h > 0
    ? { w: Math.ceil(value.w), h: Math.ceil(value.h) }
    : null;
}

export function resolveDeckViewportSize(
  artifactCanvas: ArtifactViewportSize,
  deckContract: ArtifactViewportSize,
  renderedSlide: ArtifactViewportSize,
): ArtifactViewportSize {
  return completeSize(deckContract)
    ?? completeSize(renderedSlide)
    ?? completeSize(artifactCanvas)
    ?? { w: 1920, h: 1080 };
}
