"use client";

import { createContext, useContext } from "react";

/** Callbacks passed from the editor host down into memoized node components
 *  (mirrors the lineage `interaction.tsx` pattern — avoids prop-drilling). */
export interface CanvasInteraction {
  /** Commit an inline-edited label (text / sticky nodes). */
  onLabelCommit: (nodeId: string, label: string) => void;
  /** Map-wide text-size multiplier (1 = default) applied across all nodes. */
  fontScale: number;
  /** Read-only host (the public share viewer): disables inline label editing. */
  readOnly?: boolean;
}

const CanvasInteractionContext = createContext<CanvasInteraction | null>(null);

export const CanvasInteractionProvider = CanvasInteractionContext.Provider;

export function useCanvasInteraction(): CanvasInteraction {
  const ctx = useContext(CanvasInteractionContext);
  if (!ctx) throw new Error("useCanvasInteraction must be used inside a MetricsCanvas");
  return ctx;
}
