"use client";
import { useEffect, useRef } from "react";
import { evidenceRange } from "@/lib/evidence-highlight";

export function HighlightedText({ text, passage }: { text: string; passage?: string }) {
  const mark = useRef<HTMLElement>(null);
  const range = evidenceRange(text, passage);
  useEffect(() => { mark.current?.scrollIntoView?.({ block: "nearest" }); }, [text, passage]);
  return <>
    {passage && !range && <p role="status" className="mb-4 text-xs text-muted-foreground">An exact, unique match could not be located on this page. Use the cited passage above.</p>}
    <p className="mx-auto max-w-3xl whitespace-pre-wrap break-words text-[14px] leading-7">
      {range ? <>{text.slice(0, range[0])}<mark ref={mark} className="rounded bg-amber-200/70 text-inherit">{text.slice(...range)}</mark>{text.slice(range[1])}</> : text}
    </p>
  </>;
}
