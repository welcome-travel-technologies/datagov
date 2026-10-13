import { AlertCircle } from "lucide-react";
import { Button } from "@/components/ui/button";

export { StatusBadge, readableStatus } from "@/components/ui/run-status";

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
