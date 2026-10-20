/** A cited passage is marked only when its complete normalized text has one
 * location in the document. Missing or ambiguous text keeps its excerpt as
 * the fallback, rather than highlighting a similar but unrelated sentence. */
export interface AnalyzerPassage { heading: string; text: string }

export interface HighlightedAnalyzerDocument {
  html: string;
  matchedPassages: number[];
  activeMatched: boolean;
}

export const ACTIVE_EVIDENCE_ID = "active-evidence";

interface TextSpan { node: Text; start: number; end: number }
interface PassageRange { start: number; end: number; index: number }

const BLOCKS = new Set([
  "ADDRESS", "ARTICLE", "ASIDE", "BLOCKQUOTE", "DD", "DIV", "DL", "DT",
  "FIGCAPTION", "FIGURE", "FOOTER", "H1", "H2", "H3", "H4", "H5", "H6",
  "HEADER", "HR", "LI", "MAIN", "NAV", "OL", "P", "PRE", "SECTION",
  "TABLE", "TBODY", "TD", "TFOOT", "TH", "THEAD", "TR", "UL",
]);

/** Normalize whole graphemes so composed/decomposed accents, ligatures and
 * supplementary characters retain correct offsets into the original text. */
function normalized(text: string) {
  let value = "";
  const starts: number[] = [], ends: number[] = [];
  const segments = typeof Intl.Segmenter === "function"
    ? Array.from(new Intl.Segmenter(undefined, { granularity: "grapheme" }).segment(text), (part) => ({ text: part.segment, start: part.index }))
    : Array.from(text.matchAll(/\P{M}\p{M}*|\p{M}+/gu), (part) => ({ text: part[0], start: part.index! }));
  for (const segment of segments) {
    const part = segment.text.normalize("NFKC").replace(/[\u00ad\u200b]/g, "").toLowerCase();
    for (const character of part) {
      const next = /\s/.test(character) ? " " : character;
      if (next === " " && value.endsWith(" ")) continue;
      value += next;
      for (let i = 0; i < next.length; i++) {
        starts.push(segment.start);
        ends.push(segment.start + segment.text.length);
      }
    }
  }
  return { value, starts, ends };
}

/** Additional defense for the source viewer. Its sandbox and no-network CSP
 * remain required; this is not a general-purpose HTML sanitizer. */
function removeActiveContent(fragment: DocumentFragment) {
  fragment.querySelectorAll("script, style, iframe, frame, frameset, object, embed, link, meta, base, template, noscript, form, input, button, textarea, select").forEach((element) => element.remove());
  for (const element of fragment.querySelectorAll("*")) {
    for (const attribute of Array.from(element.attributes)) {
      const name = attribute.name.toLowerCase();
      const value = Array.from(attribute.value).filter((character) => {
        const code = character.codePointAt(0)!;
        return code > 0x20 && (code < 0x7f || code > 0x9f);
      }).join("");
      if (name.startsWith("on") || name === "srcdoc" || name === "autofocus" ||
        (["href", "src", "xlink:href", "action", "formaction"].includes(name) && /^(?:javascript|vbscript):/i.test(value)) ||
        (["id", "name"].includes(name) && attribute.value === ACTIVE_EVIDENCE_ID)) element.removeAttribute(attribute.name);
    }
  }
}

function documentText(fragment: DocumentFragment) {
  let text = "";
  const spans: TextSpan[] = [];
  const visit = (node: Node) => {
    if (node.nodeType === 3) {
      const value = node.textContent || "";
      if (value) spans.push({ node: node as Text, start: text.length, end: text.length + value.length });
      text += value;
      return;
    }
    const element = node.nodeType === 1 ? node as Element : null;
    if (element?.hasAttribute("hidden") || element?.getAttribute("aria-hidden") === "true") return;
    const boundary = !!element && (BLOCKS.has(element.tagName) || element.tagName === "BR");
    if (boundary) text += "\n";
    node.childNodes.forEach(visit);
    if (boundary) text += "\n";
  };
  visit(fragment);
  return { text, spans };
}

/** The engine stores passages as Markdown but renders the document as HTML.
 * Strip recognized formatting only; the entire resulting passage must still
 * match uniquely. Code is protected so literal identifiers and asterisks do
 * not change meaning. Unsupported syntax simply keeps the excerpt fallback. */
function renderedPassageText(text: string, doc: Document) {
  const code: string[] = [];
  let marker = "\ue000";
  while (text.includes(marker)) marker += "\ue000";
  const keep = (value: string) => `${marker}${code.push(value) - 1}\ue001`;
  let value = text
    .replace(/^\s{0,3}(`{3,}|~{3,})[^\n]*\n([\s\S]*?)^\s{0,3}\1\s*$/gm, (_match, _fence: string, content: string) => keep(content))
    .replace(/(`+)([^`]+)\1/g, (_match, _fence: string, content: string) => keep(content.replace(/\n/g, " ")))
    .replace(/\[([^\]\n]+)\]\((?:\\.|[^()\n])*\)/g, "$1")
    .replace(/<((?:https?|mailto):[^>\s]+)>/g, "$1")
    .replace(/^\s{0,3}#{1,6}\s+(.+?)(?:\s+#+\s*)?$/gm, "$1")
    .replace(/^\s*>\s?/gm, "")
    .replace(/^\s*[-*+]\s+/gm, "")
    .replace(/^\s*\d+[.)]\s+/gm, "")
    .replace(/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/gm, "");
  // A delimiter row identifies an actual Markdown table; pipes in ordinary
  // prose or code remain literal characters.
  const lines = value.split("\n");
  for (let i = 1; i < lines.length; i++) {
    if (!/^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[i])) continue;
    lines[i - 1] = lines[i - 1].replace(/(?<!\\)\|/g, " ");
    lines[i] = "";
    for (let row = i + 1; row < lines.length && lines[row].includes("|"); row++) lines[row] = lines[row].replace(/(?<!\\)\|/g, " ");
  }
  value = lines.join("\n");
  for (let pass = 0; pass < 3; pass++) {
    value = value.replace(/(^|[\s([{])([*_]{1,3})(?=\S)([\s\S]*?\S)\2(?=$|[\s.,!?;:)\]}])/g, "$1$3");
  }
  value = value.replace(/\\([\\`*_{}[\]()#+\-.!|>~])/g, "$1");
  const entities = doc.createElement("textarea");
  entities.innerHTML = value;
  value = entities.value;
  code.forEach((content, index) => { value = value.replace(`${marker}${index}\ue001`, content); });
  return value;
}

/** Browser-only document markup for the sandboxed source frame. All source
 * content is parsed inside an inert template, so resource elements cannot
 * fetch while it is inspected. Marks preserve existing inline formatting.
 * Active marks use `.analyzer-evidence-active`; the first has the stable
 * `#active-evidence` fragment for automatic navigation without frame scripts.
 * The server returns an empty body; render this after client mount. */
export function highlightAnalyzerDocument(
  bodyHtml: string,
  passages: readonly AnalyzerPassage[],
  activeIndex: number,
): HighlightedAnalyzerDocument {
  if (typeof DOMParser === "undefined") return { html: "", matchedPassages: [], activeMatched: false };
  const doc = new DOMParser().parseFromString("<!doctype html><html><body></body></html>", "text/html");
  const template = doc.createElement("template");
  template.innerHTML = bodyHtml;
  const fragment = template.content;
  removeActiveContent(fragment);
  const { text, spans } = documentText(fragment);
  const haystack = normalized(text);
  const ranges: PassageRange[] = [];
  passages.forEach((passage, index) => {
    const fullText = passage.text?.trim();
    // A heading alone is useful only if it is an actual, unique heading.
    if (!fullText) {
      const heading = normalized(passage.heading || "").value.trim();
      if (!heading) return;
      const headings = Array.from(fragment.querySelectorAll("h1, h2, h3, h4, h5, h6"))
        .filter((element) => normalized(element.textContent || "").value.trim() === heading);
      if (headings.length !== 1) return;
      const matching = spans.filter((span) => headings[0].contains(span.node));
      if (matching.length) ranges.push({ start: matching[0].start, end: matching[matching.length - 1].end, index });
      return;
    }
    const candidates = [fullText, renderedPassageText(fullText, doc)];
    for (const candidate of candidates) {
      const needle = normalized(candidate).value.trim();
      if (!needle) continue;
      const start = haystack.value.indexOf(needle);
      if (start < 0 || haystack.value.indexOf(needle, start + 1) >= 0) continue;
      ranges.push({ start: haystack.starts[start], end: haystack.ends[start + needle.length - 1], index });
      break;
    }
  });

  let anchored = false;
  for (const span of spans) {
    const applicable = ranges.filter((range) => range.start < span.end && range.end > span.start);
    if (!applicable.length) continue;
    const cuts = new Set([span.start, span.end]);
    applicable.forEach((range) => { cuts.add(Math.max(span.start, range.start)); cuts.add(Math.min(span.end, range.end)); });
    const boundaries = Array.from(cuts).sort((a, b) => a - b);
    const replacement = doc.createDocumentFragment();
    for (let i = 0; i < boundaries.length - 1; i++) {
      const start = boundaries[i], end = boundaries[i + 1];
      const content = span.node.data.slice(start - span.start, end - span.start);
      const covering = applicable.filter((range) => range.start <= start && range.end >= end);
      if (!covering.length) { replacement.append(doc.createTextNode(content)); continue; }
      const active = covering.some((range) => range.index === activeIndex);
      const mark = doc.createElement("mark");
      mark.className = active ? "analyzer-evidence analyzer-evidence-active" : "analyzer-evidence";
      mark.dataset.passageIndex = String(active ? activeIndex : covering[0].index);
      mark.textContent = content;
      if (active && !anchored) { mark.id = ACTIVE_EVIDENCE_ID; anchored = true; }
      replacement.append(mark);
    }
    span.node.replaceWith(replacement);
  }
  return { html: template.innerHTML, matchedPassages: ranges.map((range) => range.index), activeMatched: anchored };
}
