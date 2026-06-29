import { CheckCircle2, XCircle } from "lucide-react";
import { Spinner } from "@/components/ui/misc";
import { cn } from "@/lib/utils";
import type { TestResult } from "@/lib/api";

/** Inline connectivity-test output (status + line-by-line log) for a source or
 * destination card. Renders nothing until a test has been triggered. */
export function TestResultPanel({
  pending,
  result,
  error,
}: {
  pending: boolean;
  result?: TestResult;
  error?: string | null;
}) {
  if (pending) {
    return (
      <div className="mt-2 flex items-center gap-2 text-[12.5px] text-muted-foreground">
        <Spinner /> Testing connection…
      </div>
    );
  }
  if (error) {
    return (
      <div className="mt-2 flex items-center gap-2 text-[12.5px] text-err">
        <XCircle className="h-3.5 w-3.5" /> {error}
      </div>
    );
  }
  if (!result) return null;

  const ok = (result.status || "").toLowerCase() === "ok";
  return (
    <div
      className={cn(
        "mt-2 rounded-lg border p-2.5 text-[12px]",
        ok ? "border-ok/30 bg-ok/5" : "border-err/30 bg-err/5",
      )}
    >
      <div className={cn("flex items-center gap-1.5 font-medium", ok ? "text-ok" : "text-err")}>
        {ok ? <CheckCircle2 className="h-3.5 w-3.5" /> : <XCircle className="h-3.5 w-3.5" />}
        {ok ? "Connection OK" : "Connection failed"}
      </div>
      {Array.isArray(result.lines) && result.lines.length > 0 && (
        <pre className="mt-1.5 max-h-40 overflow-auto whitespace-pre-wrap text-faint">
          {result.lines.join("\n")}
        </pre>
      )}
    </div>
  );
}
