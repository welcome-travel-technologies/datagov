"use client";

import type { Lens } from "@/lib/lineage/lens";

/** Bottom-right legend that reflects the active lens (colibri's Transformations
 *  key). */
export function LensLegend({ lens }: { lens: Lens }) {
  return (
    <div className="rounded-lg border border-line bg-panel/95 p-2.5 text-[11px] shadow-card backdrop-blur">
      <div className="mb-1 font-semibold text-foreground/80">{lens.label}</div>
      <div className="flex flex-col gap-1">
        {lens.legend.map((item) => (
          <div key={item.key} className="flex items-center gap-1.5">
            <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: item.color }} />
            <span className="text-faint">{item.label}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
