import assert from "node:assert/strict";
import test from "node:test";

import {
  attachmentsForReferencePosterSubmission,
  bindReferencePosterHandle,
  defaultPaperPosterBrief,
  inputForPaperPdfChoice,
  inputForReferencePosterChoice,
  isReferenceDraftConversation,
  isReferenceStyleControlEligible,
  partitionReferenceAttachments,
  referencePosterValidationMessage,
  replaceStyleReferenceAttachment,
  validateWebReferencePosterFile,
} from "../src/lib/reference_poster.ts";
import { DENSE_PAPER_POSTER_PROMPT } from "../src/lib/presets.ts";
import type { Attachment } from "../src/lib/types.ts";

for (const [name, type] of [
  ["reference.png", "image/png"],
  ["reference.jpg", "image/jpeg"],
  ["reference.JPEG", "image/jpeg"],
  ["reference.webp", "image/webp"],
  ["reference.webp", ""],
] as const) {
  test(`accepts Web reference image ${name}`, () => {
    assert.deepEqual(validateWebReferencePosterFile({ name, type }), { ok: true });
  });
}

for (const [name, type, code] of [
  ["reference.pdf", "application/pdf", "unsupported_reference_format"],
  ["reference.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation", "unsupported_reference_format"],
  ["reference.html", "text/html", "unsupported_reference_format"],
  ["reference.svg", "image/svg+xml", "unsupported_reference_format"],
  ["reference.gif", "image/gif", "unsupported_reference_format"],
  ["reference", "image/png", "unsupported_reference_format"],
  ["reference.png", "application/pdf", "reference_mime_mismatch"],
  ["reference.jpg", "image/png", "reference_mime_mismatch"],
] as const) {
  test(`rejects Web reference candidate ${name} as ${code}`, () => {
    assert.equal(validateWebReferencePosterFile({ name, type }).code, code);
  });
}

const paper: Attachment = {
  id: "paper",
  name: "paper.pdf",
  size: 100,
  kind: "pdf",
  role: "content",
};
const firstReference: Attachment = {
  id: "reference-a",
  name: "a.png",
  size: 200,
  kind: "image",
  role: "style_reference",
};
const secondReference: Attachment = {
  id: "reference-b",
  name: "b.webp",
  size: 300,
  kind: "image",
  role: "style_reference",
};

test("replaces one style reference without disturbing content", () => {
  assert.deepEqual(
    replaceStyleReferenceAttachment([paper, firstReference], secondReference),
    [paper, secondReference],
  );
});

test("partitions the style reference away from paper evidence", () => {
  assert.deepEqual(partitionReferenceAttachments([paper, secondReference]), {
    content: [paper],
    reference: secondReference,
  });
});

test("binds the backend handle to the exact reference task payload", () => {
  const bound = bindReferencePosterHandle(secondReference, " ref_exact ");

  assert.equal(bound?.reference_handle, "ref_exact");
  assert.equal(secondReference.reference_handle, undefined);
});

test("maps reference validation errors to actionable copy", () => {
  assert.equal(
    referencePosterValidationMessage("unsupported_reference_format"),
    "Choose a PNG, JPEG, or WebP poster image.",
  );
  assert.equal(
    referencePosterValidationMessage("reference_mime_mismatch"),
    "The file type does not match its image extension.",
  );
});

test("scopes a reference draft to its owning conversation", () => {
  assert.equal(isReferenceDraftConversation("conversation-a", "conversation-a"), true);
  assert.equal(isReferenceDraftConversation("conversation-a", "conversation-b"), false);
});

test("makes the reference control eligible only for known non-demo Poster state", () => {
  assert.equal(isReferenceStyleControlEligible(true, false), true);
  assert.equal(isReferenceStyleControlEligible(true, true), false);
  assert.equal(isReferenceStyleControlEligible(true, undefined), false);
  assert.equal(isReferenceStyleControlEligible(false, false), false);
});

test("strips style references from non-Poster submissions", () => {
  assert.deepEqual(
    attachmentsForReferencePosterSubmission([paper, firstReference], "landing"),
    [paper],
  );
  assert.deepEqual(
    attachmentsForReferencePosterSubmission([paper, firstReference], "poster"),
    [paper, firstReference],
  );
});

test("clears only the untouched dense default when choosing a reference", () => {
  assert.equal(
    inputForReferencePosterChoice(DENSE_PAPER_POSTER_PROMPT, DENSE_PAPER_POSTER_PROMPT),
    "",
  );
  assert.equal(
    inputForReferencePosterChoice("Use the paper's visual hierarchy.", DENSE_PAPER_POSTER_PROMPT),
    "Use the paper's visual hierarchy.",
  );
  assert.equal(
    inputForReferencePosterChoice(`${DENSE_PAPER_POSTER_PROMPT}\n`, DENSE_PAPER_POSTER_PROMPT),
    `${DENSE_PAPER_POSTER_PROMPT}\n`,
  );
});

test("chooses the paper PDF prompt without overriding an active reference", () => {
  assert.equal(inputForPaperPdfChoice("", true, DENSE_PAPER_POSTER_PROMPT), "");
  assert.equal(
    inputForPaperPdfChoice(
      "Use the paper's visual hierarchy.",
      true,
      DENSE_PAPER_POSTER_PROMPT,
    ),
    "Use the paper's visual hierarchy.",
  );
  assert.equal(
    inputForPaperPdfChoice("", false, DENSE_PAPER_POSTER_PROMPT),
    DENSE_PAPER_POSTER_PROMPT,
  );
});

test("does not restore the dense brief when submitting a paper with a reference", () => {
  assert.equal(
    defaultPaperPosterBrief(true, true, true, DENSE_PAPER_POSTER_PROMPT),
    "",
  );
  assert.equal(
    defaultPaperPosterBrief(true, true, false, DENSE_PAPER_POSTER_PROMPT),
    DENSE_PAPER_POSTER_PROMPT,
  );
  assert.equal(
    defaultPaperPosterBrief(true, false, false, DENSE_PAPER_POSTER_PROMPT),
    "",
  );
});
