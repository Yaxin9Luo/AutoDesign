import type { ArtifactType, Attachment } from "./types";

const MIME_BY_SUFFIX: Record<string, string> = {
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
};

export const WEB_REFERENCE_POSTER_ACCEPT =
  ".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp";

export type ReferencePosterValidation =
  | { ok: true }
  | { ok: false; code: "unsupported_reference_format" | "reference_mime_mismatch" };

export function referencePosterValidationMessage(
  code: Extract<ReferencePosterValidation, { ok: false }>["code"],
): string {
  return code === "unsupported_reference_format"
    ? "Choose a PNG, JPEG, or WebP poster image."
    : "The file type does not match its image extension.";
}

export function validateWebReferencePosterFile(
  file: Pick<File, "name" | "type">,
): ReferencePosterValidation {
  const match = /\.[^.]+$/.exec(file.name.trim().toLowerCase());
  const suffix = match?.[0] ?? "";
  const expectedMime = MIME_BY_SUFFIX[suffix];
  if (!expectedMime) return { ok: false, code: "unsupported_reference_format" };
  if (file.type && file.type.toLowerCase() !== expectedMime) {
    return { ok: false, code: "reference_mime_mismatch" };
  }
  return { ok: true };
}

export function replaceStyleReferenceAttachment(
  attachments: Attachment[],
  reference: Attachment,
): Attachment[] {
  return [
    ...attachments.filter((item) => item.role !== "style_reference"),
    reference,
  ];
}

export function partitionReferenceAttachments(
  attachments: Attachment[],
): { content: Attachment[]; reference?: Attachment } {
  return {
    content: attachments.filter((item) => item.role !== "style_reference"),
    reference: attachments.find((item) => item.role === "style_reference"),
  };
}

export function bindReferencePosterHandle<T extends { reference_handle?: string }>(
  reference: T | undefined,
  handle: string | null | undefined,
): T | undefined {
  const normalized = handle?.trim();
  return reference && normalized
    ? { ...reference, reference_handle: normalized }
    : reference;
}

export function isReferenceDraftConversation(
  ownerConversationId: string | null,
  currentConversationId: string | null,
): boolean {
  return ownerConversationId === currentConversationId;
}

export function isReferenceStyleControlEligible(
  posterContext: boolean,
  demoMode: boolean | undefined,
): boolean {
  return posterContext && demoMode === false;
}

export function attachmentsForReferencePosterSubmission(
  attachments: Attachment[],
  artifactType: ArtifactType,
): Attachment[] {
  return artifactType === "poster"
    ? attachments
    : partitionReferenceAttachments(attachments).content;
}

export function inputForReferencePosterChoice(
  input: string,
  autoInsertedPrompt: string,
): string {
  return input === autoInsertedPrompt ? "" : input;
}

export function inputForPaperPdfChoice(
  input: string,
  hasReference: boolean,
  autoInsertedPrompt: string,
): string {
  if (input.trim() || hasReference) return input;
  return autoInsertedPrompt;
}

export function defaultPaperPosterBrief(
  hasPaperPdf: boolean,
  posterIntent: boolean,
  hasReference: boolean,
  defaultBrief: string,
): string {
  return hasPaperPdf && posterIntent && !hasReference ? defaultBrief : "";
}
