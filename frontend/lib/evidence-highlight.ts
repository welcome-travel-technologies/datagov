/** Match a complete normalized passage and retain offsets into the original text.
 * No keyword/fuzzy match: a highlight must identify the cited passage itself.
 */
function normalized(text: string) {
  let value = "";
  const positions: number[] = [];
  let offset = 0;
  for (const character of text) {
    const part = character.normalize("NFKC").replace(/[\u00ad\u200b]/g, "").toLowerCase();
    for (const c of part) {
      const next = /\s/.test(c) ? " " : c;
      if (next === " " && value.endsWith(" ")) continue;
      value += next;
      positions.push(offset);
    }
    offset += character.length;
  }
  return { value, positions };
}

export function evidenceRange(text: string, passage?: string): [number, number] | null {
  if (!passage?.trim()) return null;
  const haystack = normalized(text), needle = normalized(passage).value.trim();
  if (!needle) return null;
  const start = haystack.value.indexOf(needle);
  // Repeated passages have no unique coordinates; show the excerpt fallback.
  if (start < 0 || haystack.value.indexOf(needle, start + 1) >= 0) return null;
  const last = haystack.positions[start + needle.length - 1];
  return [haystack.positions[start], last + (text.codePointAt(last)! > 0xffff ? 2 : 1)];
}

export function escapeHtml(text: string) {
  return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

export function highlightHtml(text: string, start: number, end: number) {
  return escapeHtml(text.slice(0, start)) + "<mark>" + escapeHtml(text.slice(start, end)) + "</mark>" + escapeHtml(text.slice(end));
}
