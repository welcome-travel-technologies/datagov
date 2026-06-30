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
  /** Gap between side-by-side nodes, inside a group (px). */
  nodeSep: number;
  /** Gap between successive rows / layers along the flow, inside a group (px). */
  rankSep: number;
  /** Gap between whole groups (and any ungrouped nodes) at the top level (px). */
  groupSep: number;
  /** Offset stacked nodes left/right (zig-zag) instead of a straight stack. */
  stagger: boolean;
  /** How far alternate layers shift sideways when stagger is on (px). */
  staggerStep: number;
}

export const ARRANGE_KEY = "metrics-map:arrange";
export const DEFAULT_ARRANGE: ArrangeSettings = {
  direction: "vertical",
  nodeSep: 100,
  rankSep: 190,
  groupSep: 140,
  stagger: false,
  staggerStep: 80,
};

/** Slider bounds for the spacing variables. */
export const NODE_SEP_RANGE = { min: 40, max: 220, step: 4 };
export const RANK_SEP_RANGE = { min: 80, max: 360, step: 5 };
export const GROUP_SEP_RANGE = { min: 60, max: 400, step: 5 };
export const STAGGER_STEP_RANGE = { min: 24, max: 200, step: 4 };

export interface ElkArrangeOpts {
  direction: "DOWN" | "RIGHT";
  nodeSep: number;
  rankSep: number;
  groupSep: number;
  stagger: boolean;
  staggerStep: number;
}

export function toElkOpts(s: ArrangeSettings): ElkArrangeOpts {
  return {
    direction: s.direction === "horizontal" ? "RIGHT" : "DOWN",
    nodeSep: s.nodeSep,
    rankSep: s.rankSep,
    groupSep: s.groupSep,
    stagger: s.stagger,
    staggerStep: s.staggerStep,
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
    groupSep: clamp(o.groupSep, GROUP_SEP_RANGE.min, GROUP_SEP_RANGE.max, DEFAULT_ARRANGE.groupSep),
    stagger: !!o.stagger,
    staggerStep: clamp(o.staggerStep, STAGGER_STEP_RANGE.min, STAGGER_STEP_RANGE.max, DEFAULT_ARRANGE.staggerStep),
  };
}
