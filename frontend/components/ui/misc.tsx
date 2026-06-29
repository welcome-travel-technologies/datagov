import * as React from "react";
import { Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={cn("h-4 w-4 animate-spin text-brand", className)} />;
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("animate-pulse rounded-md bg-foreground/[0.06]", className)} />;
}

/** Full-area centered loader with a label. */
export function LoadingState({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex min-h-[200px] flex-col items-center justify-center gap-3 text-[13px] text-muted-foreground">
      <Spinner className="h-6 w-6" />
      <span>{label}</span>
    </div>
  );
}

/** Empty / error placeholder. */
export function EmptyState({
  title,
  hint,
  icon,
}: {
  title: string;
  hint?: string;
  icon?: React.ReactNode;
}) {
  return (
    <div className="flex min-h-[180px] flex-col items-center justify-center gap-2 text-center">
      {icon && <div className="text-faint">{icon}</div>}
      <div className="text-[14px] font-semibold">{title}</div>
      {hint && <div className="max-w-md text-[13px] text-muted-foreground">{hint}</div>}
    </div>
  );
}

/** KPI stat card used on dashboard / catalog summaries. */
export function Stat({
  label,
  value,
  hint,
  accent,
}: {
  label: string;
  value: React.ReactNode;
  hint?: string;
  accent?: boolean;
}) {
  return (
    <div className="rounded-lg border border-line bg-card p-4 shadow-card">
      <div className="text-[11px] font-semibold uppercase tracking-[0.06em] text-faint">{label}</div>
      <div className={cn("mt-1.5 text-2xl font-semibold -tracking-[0.02em]", accent && "text-brand")}>
        {value}
      </div>
      {hint && <div className="mt-1 text-[12px] text-muted-foreground">{hint}</div>}
    </div>
  );
}
