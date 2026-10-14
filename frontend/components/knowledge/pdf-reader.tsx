"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import type { TextContent } from "pdfjs-dist/types/src/display/api";
import { Button } from "@/components/ui/button";
import { escapeHtml, evidenceRange, highlightHtml } from "@/lib/evidence-highlight";
import "react-pdf/dist/Page/TextLayer.css";

// Bundle the matching worker locally. Private documents never go to a CDN.
pdfjs.GlobalWorkerOptions.workerSrc = new URL("pdfjs-dist/build/pdf.worker.min.mjs", import.meta.url).toString();
const options = { isEvalSupported: false };

export default function PdfReader({ url, page, passage, onPages }: {
  url: string; page: number; passage?: string; onPages: (count: number) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(600);
  const [zoom, setZoom] = useState(1);
  const [ranges, setRanges] = useState<Map<number, [number, number]>>(new Map());
  const [matched, setMatched] = useState<boolean | null>(null);
  useEffect(() => {
    if (!box.current) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(200, entry.contentRect.width - 24)));
    observer.observe(box.current);
    return () => observer.disconnect();
  }, []);
  const textLoaded = useCallback((content: TextContent) => {
    let text = "";
    const offsets = content.items.map((item) => {
      const start = text.length;
      const value = "str" in item ? item.str : "";
      text += value + " ";
      return { start, length: value.length };
    });
    const range = evidenceRange(text, passage);
    const next = new Map<number, [number, number]>();
    if (range) offsets.forEach(({ start, length }, index) => {
      const left = Math.max(0, range[0] - start), right = Math.min(length, range[1] - start);
      if (left < right) next.set(index, [left, right]);
    });
    setRanges(next);
    setMatched(!!range);
  }, [passage]);
  const renderText = useCallback(({ str, itemIndex }: { str: string; itemIndex: number }) => {
    const range = ranges.get(itemIndex);
    return range ? highlightHtml(str, ...range) : escapeHtml(str);
  }, [ranges]);
  return <div ref={box} className="min-w-0">
    <div className="flex items-center gap-2 border-b border-line px-3 py-2 text-xs">
      <Button size="sm" variant="ghost" aria-label="Zoom out" disabled={zoom <= 0.5} onClick={() => setZoom((v) => Math.max(0.5, v - 0.25))}>−</Button>
      <span aria-live="polite">{Math.round(zoom * 100)}%</span>
      <Button size="sm" variant="ghost" aria-label="Zoom in" disabled={zoom >= 2} onClick={() => setZoom((v) => Math.min(2, v + 0.25))}>+</Button>
      <Button size="sm" variant="ghost" onClick={() => setZoom(1)}>Fit width</Button>
    </div>
    {passage && matched === false && <p role="status" className="p-3 text-xs text-muted-foreground">The passage could not be uniquely matched in the PDF text layer. Try Extracted text or read the cited passage above.</p>}
    <div className="max-h-[72vh] overflow-auto bg-panel2 p-3 [&_mark]:rounded-sm [&_mark]:bg-amber-300/60 [&_mark]:text-transparent">
      <Document file={url} options={options} onLoadSuccess={({ numPages }) => onPages(numPages)}
        loading={<p className="p-5 text-sm">Loading PDF…</p>}
        error={<p role="alert" className="p-5 text-sm">This PDF could not be displayed. Use Open PDF or Extracted text.</p>}>
        <Page key={`${url}:${page}:${passage}`} pageNumber={page} width={width * zoom} renderAnnotationLayer={false}
          onGetTextSuccess={textLoaded} customTextRenderer={renderText}
          onRenderTextLayerSuccess={() => box.current?.querySelector("mark")?.scrollIntoView({ block: "nearest", inline: "nearest" })}
          error={<p className="p-4 text-sm">This page is unavailable. Choose a page within the document.</p>} />
      </Document>
    </div>
  </div>;
}
