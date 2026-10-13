import { AlertCircle } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

export function readableStatus(status: string) {
  const labels: Record<string, string> = {
    ready: "Ready", completed: "Completed", succeeded: "Completed", complete: "Completed", published: "Published",
    failed: "Failed", error: "Error", running: "Running", processing: "Processing", indexing: "Indexing", claimed: "Processing",
    queued: "Queued", pending: "Pending", cancel_requested: "Cancelling", cancelled: "Cancelled", needs_ocr: "Needs OCR", partial: "Partially complete",
  };
  return labels[status] || status.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}

export function StatusBadge({ status }: { status: string }) {
  const variant = ["ready", "completed", "succeeded", "published"].includes(status) ? "success"
    : ["failed", "error"].includes(status) ? "danger"
      : ["running", "processing", "indexing", "claimed"].includes(status) ? "info"
        : ["queued", "pending", "cancel_requested", "needs_ocr", "partial"].includes(status) ? "warning" : "default";
  return <Badge variant={variant} dot>{readableStatus(status)}</Badge>;
}

export function KnowledgeError({ message, retry }: { message: string; retry?: () => void }) {
  return (
    <div role="alert" className="flex flex-wrap items-center gap-3 rounded-xl border border-err/25 bg-err/5 p-4 text-[13px]">
      <AlertCircle className="h-4 w-4 shrink-0 text-err" />
      <p className="min-w-0 flex-1">{message}</p>
      {retry && <Button variant="outline" size="sm" onClick={retry}>Try again</Button>}
    </div>
  );
}

export function displayDate(value: string | null | undefined) {
  if (!value) return "Not yet";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Unknown" : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
