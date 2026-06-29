"use client";

import { useState } from "react";
import { Check, Copy, FunctionSquare } from "lucide-react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

/** DAX expression viewer with copy-to-clipboard (ported from `daxModal`). */
export function DaxModal({
  name,
  expr,
  onClose,
}: {
  name: string | null;
  expr: string | null;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(expr ?? "");
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard blocked — no-op */
    }
  }

  return (
    <Dialog open={!!name} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 pr-8">
            <FunctionSquare className="h-4 w-4 text-brand" />
            DAX Expression
            {name && (
              <span className="rounded border border-line bg-panel px-2 py-0.5 font-mono text-[12px] text-welcome-blue">
                {name}
              </span>
            )}
          </DialogTitle>
        </DialogHeader>
        <pre className="max-h-[60vh] overflow-auto rounded-lg border border-line bg-panel2 p-4 font-mono text-[12px] leading-relaxed whitespace-pre-wrap">
          <code>{expr || "—"}</code>
        </pre>
        <div className="flex justify-end">
          <Button variant={copied ? "brand" : "outline"} onClick={copy}>
            {copied ? <Check /> : <Copy />}
            {copied ? "Copied!" : "Copy to clipboard"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
