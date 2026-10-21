/**
 * Client for the Document Analyzer (luccy behind `/api/document-analyzer/`): the
 * page's status, and how the web chat shows a verbatim analyzer answer. The chat
 * page itself is luccy's own, served through DataGov's sign-in gate.
 */
import { request } from "@/lib/api";

export interface AnalyzerStatus {
  enabled: boolean;
  configured: boolean;
  /** This user may use the page. */
  available: boolean;
  /** A quick health probe of the engine; null when it is not configured. */
  healthy: boolean | null;
  /** Plain-words explanation when not available ("" otherwise). */
  reason: string;
  sources: string[];
  tool_available: boolean;
}

/** Answer markdown with images switched off, as luccy renders it (render.py
 *  disables them): a cited page could steer the model into an image whose URL
 *  carries the question, and the browser would fetch it unasked. Every `!`
 *  opening an image gets a backslash, unless an odd run of backslashes already
 *  escapes it, so `![alt](url)` shows as "!" and a link. Inside code the added
 *  backslash shows; images never do. */
export function withoutImages(markdown: string): string {
  return markdown.replace(/(\\*)!\[/g, (match, slashes: string) => (slashes.length % 2 ? match : `${slashes}\\![`));
}

// ---- web chat answers ---------------------------------------------------------

/** What DataGov's chat API says about a stored message: `verbatim` is true when
 *  the message is the Document Analyzer's own answer, stored as luccy wrote it
 *  and closed by the line "Answered by the Document Analyzer · ref <log>" (or
 *  "· ref -"). null or absent for every other message. */
export interface ChatAnalyzerMeta {
  verbatim?: boolean | null;
  ref?: string | null;
}

/** Whether a stored chat message is a verbatim Document Analyzer answer. Only
 *  the API's flag decides; what the text says never does. */
export function isVerbatimAnalyzerAnswer(meta: ChatAnalyzerMeta | null | undefined): boolean {
  return meta?.verbatim === true;
}

/** Splits a verbatim analyzer answer into its markdown and its closing line,
 *  which the backend always writes last. Call it only for verbatim messages. */
export function splitClosingLine(content: string): { body: string; footnote: string } {
  const text = String(content ?? "").replace(/\s+$/, "");
  const cut = Math.max(text.lastIndexOf("\n"), text.lastIndexOf("\r"));
  return { body: text.slice(0, cut + 1).replace(/\s+$/, ""), footnote: text.slice(cut + 1).trim() };
}

export const analyzerApi = {
  status: () => request<AnalyzerStatus>("/document-analyzer/status/"),
};
