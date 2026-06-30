/**
 * User-tunable options for the Metrics Map auto-arrange, remembered across
 * sessions (localStorage). Shared by the toolbar control and the canvas, and
 * mapped to the concrete ELK / Dagre inputs here so both stay in sync.
 *
 * The spacing distances are first-class variables (`nodeSep` / `rankSep`) the
 * user adjusts directly, rather than fixed presets.
 */
export type ArrangeDir = "vertical" | "horizontal";

export interface ArrangeSettings {
  direction: ArrangeDir;
  /** Gap between side-by-side nodes (px). */
  nodeSep: number;
  /** Gap between successive rows / layers along the flow (px). */
  rankSep: number;
  /** Offset stacked nodes left/right (zig-zag) instead of a straight stack. */
  stagger: boolean;
}

export const ARRANGE_KEY = "metrics-map:arrange";
export const DEFAULT_ARRANGE: ArrangeSettings = {
  direction: "vertical",
  nodeSep: 100,
  rankSep: 190,
  stagger: false,
};

/** Slider bounds for the two spacing variables. */
export const NODE_SEP_RANGE = { min: 40, max: 220, step: 4 };
export const RANK_SEP_RANGE = { min: 80, max: 360, step: 5 };

export interface ElkArrangeOpts {
  direction: "DOWN" | "RIGHT";
  nodeSep: number;
  rankSep: number;
  stagger: boolean;
}

export function toElkOpts(s: ArrangeSettings): ElkArrangeOpts {
  return {
    direction: s.direction === "horizontal" ? "RIGHT" : "DOWN",
    nodeSep: s.nodeSep,
    rankSep: s.rankSep,
    stagger: s.stagger,
  };
}

/** Dagre fallback rank direction. */
export function toDagreDir(s: ArrangeSettings): "TB" | "LR" {
  return s.direction === "horizontal" ? "LR" : "TB";
}

function clamp(n: unknown, lo: number, hi: number, fallback: number): number {
  return typeof n === "number" && Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : fallback;
}

/** Coerce an unknown (parsed localStorage) value into a valid settings object. */
export function sanitizeArrange(raw: unknown): ArrangeSettings {
  if (!raw || typeof raw !== "object") return DEFAULT_ARRANGE;
  const o = raw as Partial<ArrangeSettings>;
  return {
    direction: o.direction === "horizontal" ? "horizontal" : "vertical",
    nodeSep: clamp(o.nodeSep, NODE_SEP_RANGE.min, NODE_SEP_RANGE.max, DEFAULT_ARRANGE.nodeSep),
    rankSep: clamp(o.rankSep, RANK_SEP_RANGE.min, RANK_SEP_RANGE.max, DEFAULT_ARRANGE.rankSep),
    stagger: !!o.stagger,
  };
}
