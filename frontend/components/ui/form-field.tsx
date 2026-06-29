import type { ReactNode } from "react";
import { AlertCircle } from "lucide-react";

/** Labeled form field: an uppercase caption above its input control. */
export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block space-y-1.5">
      <span className="text-[11px] font-semibold uppercase tracking-[0.06em] text-faint">{label}</span>
      {children}
    </label>
  );
}

/** Inline error alert for dialog/form footers. */
export function FormError({ children }: { children: ReactNode }) {
  return (
    <div className="flex items-center gap-2 rounded-md bg-err/10 px-3 py-2 text-[12.5px] text-err">
      <AlertCircle className="h-3.5 w-3.5 shrink-0" />
      {children}
    </div>
  );
}
