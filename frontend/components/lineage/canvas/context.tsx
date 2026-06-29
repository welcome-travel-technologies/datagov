"use client";

import { createContext, useContext } from "react";
import type { Lens } from "@/lib/lineage/lens";

/** The active column-lineage trace (a clicked or hovered column's full path). */
export interface Highlight {
  active: boolean;
  cols: Set<string>;
  cards: Set<string>;
  edges: Set<string>;
  selectedCol: string | null;
}

export const EMPTY_HIGHLIGHT: Highlight = {
  active: false,
  cols: new Set(),
  cards: new Set(),
  edges: new Set(),
  selectedCol: null,
};

/** Everything a card node / column row needs from the canvas. Passed via context
 *  (not props) so memoized React-Flow nodes don't re-render on every state tick. */
export interface CanvasContext {
  highlight: Highlight;
  lens: Lens;
  onColumnClick: (colId: string) => void;
  onColumnHover: (colId: string | null) => void;
  onColumnContext: (colId: string, clientX: number, clientY: number) => void;
  onCardHeaderClick: (cardId: string) => void;
  onToggleCollapse: (cardId: string) => void;
  onExpandUpstream: (nodeId: string) => void;
  onExpandDownstream: (nodeId: string) => void;
}

const Ctx = createContext<CanvasContext | null>(null);

export const CanvasProvider = Ctx.Provider;

export function useCanvas(): CanvasContext {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useCanvas must be used inside a LineageCanvas provider");
  return ctx;
}
