import type { Artifact, Conversation, Message } from "./types";

export interface ArtifactValidationState {
  label: string;
  detail: string;
  tone: "ok" | "warning" | "running" | "neutral";
}

export interface ArtifactDownloadLink {
  label: string;
  href: string;
  format: string;
}

export function artifactMessage(
  conversation: Conversation | undefined,
  artifact_id: string | undefined,
): Message | undefined {
  if (!conversation || !artifact_id) return undefined;
  return conversation.messages.find((m) => m.artifact_id === artifact_id);
}

export function artifactValidationState(
  artifact: Artifact | undefined,
  message: Message | undefined,
): ArtifactValidationState {
  if (!artifact) {
    return { label: "No artifact", detail: "Waiting", tone: "neutral" };
  }
  if (message?.status === "streaming") {
    return { label: "Running", detail: "Generation in progress", tone: "running" };
  }
  if (!message) {
    return { label: "Imported", detail: "Review available", tone: "neutral" };
  }
  return { label: "Validated", detail: "Final artifact ready", tone: "ok" };
}

export function artifactDownloadLinks(artifact: Artifact): ArtifactDownloadLink[] {
  const links: ArtifactDownloadLink[] = [];
  if (artifact.pdf_url) {
    links.push({ label: "PDF", href: artifact.pdf_url, format: "pdf" });
  }
  const primaryFormat = artifact.native_format ?? artifact.view_format ?? artifact.artifact_type;
  if (artifact.download_url) {
    links.push({
      label: primaryFormat.toUpperCase(),
      href: artifact.download_url,
      format: primaryFormat,
    });
  }
  for (const [format, href] of Object.entries(artifact.downloads ?? {})) {
    if (!href || links.some((link) => link.href === href || link.format === format)) {
      continue;
    }
    links.push({ label: format.toUpperCase(), href, format });
  }
  return links;
}
