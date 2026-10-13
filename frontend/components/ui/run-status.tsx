import { Badge, type BadgeProps } from "@/components/ui/badge";

const labels: Record<string, string> = {
  ready: "Ready", completed: "Completed", succeeded: "Completed", complete: "Completed", published: "Published",
  failed: "Failed", error: "Error", running: "Running", processing: "Processing", indexing: "Indexing", claimed: "Processing",
  queued: "Queued", pending: "Pending", cancel_requested: "Cancelling", cancelled: "Cancelled", needs_ocr: "Needs OCR", partial: "Partially complete",
};

export function readableStatus(status: string) {
  return labels[status.toLowerCase()] || status.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}

/** Shared by integration workflows and document processing. */
export function statusVariant(status?: string | null): NonNullable<BadgeProps["variant"]> {
  const value = (status || "").toLowerCase();
  if (["success", "completed", "ok", "ready", "succeeded", "complete", "published"].includes(value)) return "success";
  if (["failed", "error"].includes(value)) return "danger";
  if (["cancel_requested", "needs_ocr", "partial"].includes(value)) return "warning";
  return isActiveStatus(value) ? "info" : "outline";
}

export function StatusBadge({ status }: { status?: string | null }) {
  return <Badge variant={statusVariant(status)} dot>{status ? readableStatus(status) : "Never run"}</Badge>;
}

/** Cancellation is still active until the worker acknowledges it. */
export function isActiveStatus(status?: string | null): boolean {
  return ["running", "pending", "queued", "processing", "indexing", "claimed", "cancel_requested"].includes((status || "").toLowerCase());
}
