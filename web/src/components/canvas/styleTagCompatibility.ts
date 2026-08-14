export function findInjectedStyle(
  doc: Document,
  canonicalId: string,
  legacyId: string,
): HTMLStyleElement | null {
  const canonical = doc.getElementById(canonicalId) as HTMLStyleElement | null;
  if (canonical) return canonical;

  const legacy = doc.getElementById(legacyId) as HTMLStyleElement | null;
  if (!legacy) return null;
  legacy.id = canonicalId;
  return legacy;
}
