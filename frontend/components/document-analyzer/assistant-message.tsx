"use client";

import { Markdown } from "@/components/markdown";
import { isVerbatimAnalyzerAnswer, splitClosingLine, withoutImages, type ChatAnalyzerMeta } from "@/lib/document-analyzer-api";

/** An assistant message in the web chat. When the API marks it as a verbatim
 *  Document Analyzer answer (`analyzer.verbatim`), its last line ("Answered by
 *  the Document Analyzer · ref …") shows as a small plain-text footnote and the
 *  answer's images stay off, as on luccy's own page. The text itself never
 *  decides: every other message renders exactly as before, whatever it says. */
export function AssistantMessage({ content, analyzer }: { content: string; analyzer?: ChatAnalyzerMeta | null }) {
  if (!isVerbatimAnalyzerAnswer(analyzer)) return <Markdown>{content}</Markdown>;
  const { body, footnote } = splitClosingLine(content);
  return (
    <>
      {body && <Markdown>{withoutImages(body)}</Markdown>}
      {footnote && <p className="mt-2 text-[11px] leading-snug text-faint">{footnote}</p>}
    </>
  );
}
