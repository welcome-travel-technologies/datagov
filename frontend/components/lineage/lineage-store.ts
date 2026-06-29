/**
 * Central reducer for the colibri-style lineage page. One serializable state
 * blob so undo/redo and saved-views can snapshot it wholesale. The heavy derived
 * artifacts (ColumnModel, React-Flow nodes/edges, the highlight trace) are
 * computed in `useMemo` selectors by the orchestrator — NOT stored here — so
 * they can never drift from the raw graph.
 *
 * The reducer is pure (no async / no layout): data loads happen in the
 * orchestrator, which dispatches LOAD_SUCCESS / MERGE_GRAPH with fetched rows.
 */
import type { NetworkNode, NetworkLink, Direction } from "@/lib/api";
import type { XY } from "@/lib/lineage/layout";
import type { LensId } from "@/lib/lineage/lens";
import { mergeGraph } from "@/lib/lineage/graph-merge";

export type LayoutMode = "auto" | "manual" | "focused";

export interface LineageState {
  rawNodes: NetworkNode[];
  rawEdges: NetworkLink[];
  centerId: string | null;
  depth: number;
  direction: Direction;

  collapsed: Set<string>;
  hidden: Set<string>;
  positions: Record<string, XY>; // manual drag overrides (card id -> top-left)
  layoutMode: LayoutMode;

  pinnedCol: string | null; // click-to-pin a column's lineage trace
  hoverCol: string | null; // transient hover trace (not undoable / not saved)

  lens: LensId;
  layersFilter: Set<string>; // hidden group keys (legend toggles)
  tagsFilter: Set<string>;
  showReports: boolean; // include PowerBI report → page → visual consumer cards
  linkedOnly: boolean; // "Show full lineage": keep only columns on the lineage

  selectedModelId: string | null; // drives the Model Info tab

  loading: boolean;
  loadingText: string;
  error: string | null;
}

export const initialLineageState: LineageState = {
  rawNodes: [],
  rawEdges: [],
  centerId: null,
  depth: 3,
  direction: "both",
  collapsed: new Set(),
  hidden: new Set(),
  positions: {},
  layoutMode: "auto",
  pinnedCol: null,
  hoverCol: null,
  lens: "lineage-type",
  layersFilter: new Set(),
  tagsFilter: new Set(),
  showReports: false,
  linkedOnly: false,
  selectedModelId: null,
  loading: false,
  loadingText: "",
  error: null,
};

export type LineageAction =
  | { type: "LOAD_START"; text?: string }
  | { type: "LOAD_SUCCESS"; nodes: NetworkNode[]; links: NetworkLink[]; centerId: string; linkedOnly?: boolean }
  | { type: "LOAD_ERROR"; error: string }
  | { type: "MERGE_GRAPH"; nodes: NetworkNode[]; links: NetworkLink[]; freeze?: Record<string, XY> }
  | { type: "SET_CENTER"; centerId: string | null }
  | { type: "SET_DIRECTION"; direction: Direction }
  | { type: "TOGGLE_COLLAPSE"; cardId: string }
  | { type: "SET_COLLAPSED"; ids: string[] }
  | { type: "HIDE_NODES"; ids: string[] }
  | { type: "UNHIDE_ALL" }
  | { type: "SET_POSITION"; id: string; pos: XY }
  | { type: "CLEAR_POSITIONS" }
  | { type: "SET_PINNED_COL"; colId: string | null }
  | { type: "SET_HOVER_COL"; colId: string | null }
  | { type: "FOCUS_COLUMN"; colId: string; relatedCards: string[]; allCards: string[]; collapse?: boolean }
  | { type: "SET_LAYOUT_MODE"; mode: LayoutMode }
  | { type: "SET_LENS"; lens: LensId }
  | { type: "TOGGLE_LAYER"; group: string }
  | { type: "SET_TAGS_FILTER"; tags: string[] }
  | { type: "SET_SHOW_REPORTS"; show: boolean }
  | { type: "SELECT_MODEL"; id: string | null }
  | { type: "RESTORE"; state: LineageState };

function toggle(set: Set<string>, key: string): Set<string> {
  const next = new Set(set);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  return next;
}

export function lineageReducer(state: LineageState, action: LineageAction): LineageState {
  switch (action.type) {
    case "LOAD_START":
      return { ...state, loading: true, error: null, loadingText: action.text ?? "Loading…" };

    case "LOAD_SUCCESS":
      return {
        ...state,
        rawNodes: action.nodes,
        rawEdges: action.links,
        centerId: action.centerId,
        // a fresh load resets view-local state
        collapsed: new Set(),
        hidden: new Set(),
        positions: {},
        layoutMode: "auto",
        pinnedCol: null,
        hoverCol: null,
        // Set by "Show full lineage" (full ego load); a plain focus load clears it.
        linkedOnly: action.linkedOnly ?? false,
        selectedModelId: action.centerId,
        loading: false,
        loadingText: "",
        error: null,
      };

    case "LOAD_ERROR":
      return { ...state, loading: false, loadingText: "", error: action.error };

    case "MERGE_GRAPH": {
      const merged = mergeGraph(state.rawNodes, state.rawEdges, action.nodes, action.links);
      return {
        ...state,
        rawNodes: merged.nodes,
        rawEdges: merged.links,
        // Freeze existing cards' positions so only brand-new cards auto-layout.
        positions: { ...state.positions, ...(action.freeze ?? {}) },
        loading: false,
        loadingText: "",
      };
    }

    case "SET_CENTER":
      return { ...state, centerId: action.centerId };

    case "SET_DIRECTION":
      return { ...state, direction: action.direction };

    case "TOGGLE_COLLAPSE":
      return { ...state, collapsed: toggle(state.collapsed, action.cardId) };

    case "SET_COLLAPSED":
      return { ...state, collapsed: new Set(action.ids) };

    case "HIDE_NODES":
      return { ...state, hidden: new Set([...state.hidden, ...action.ids]) };

    case "UNHIDE_ALL":
      return { ...state, hidden: new Set() };

    case "SET_POSITION":
      return {
        ...state,
        layoutMode: "manual",
        positions: { ...state.positions, [action.id]: action.pos },
      };

    case "CLEAR_POSITIONS":
      return { ...state, positions: {}, layoutMode: "auto" };

    case "SET_PINNED_COL":
      return { ...state, pinnedCol: action.colId };

    case "SET_HOVER_COL":
      return { ...state, hoverCol: action.colId };

    case "FOCUS_COLUMN": {
      const related = new Set(action.relatedCards);
      return {
        ...state,
        pinnedCol: action.colId,
        hidden: new Set(action.allCards.filter((id) => !related.has(id))),
        // `collapse` (used after a full-lineage fetch) collapses every related
        // card so models render compact and — combined with the pinned column —
        // expose only their lineage columns (focused collapse). Without it the
        // existing in-graph focus leaves card expansion untouched.
        collapsed: action.collapse ? new Set(action.relatedCards) : state.collapsed,
        positions: {}, // re-layout the focused set L→R from scratch
        layoutMode: "focused",
        hoverCol: null,
      };
    }

    case "SET_LAYOUT_MODE":
      return { ...state, layoutMode: action.mode };

    case "SET_LENS":
      return { ...state, lens: action.lens };

    case "TOGGLE_LAYER":
      return { ...state, layersFilter: toggle(state.layersFilter, action.group) };

    case "SET_TAGS_FILTER":
      return { ...state, tagsFilter: new Set(action.tags) };

    case "SET_SHOW_REPORTS":
      return { ...state, showReports: action.show };

    case "SELECT_MODEL":
      return { ...state, selectedModelId: action.id };

    case "RESTORE":
      return action.state;

    default:
      return state;
  }
}

// ── serialization (undo/redo snapshots + saved views) ───────────────────────
export interface SerializedLineageState {
  rawNodes: NetworkNode[];
  rawEdges: NetworkLink[];
  centerId: string | null;
  depth: number;
  direction: Direction;
  collapsed: string[];
  hidden: string[];
  positions: Record<string, XY>;
  layoutMode: LayoutMode;
  pinnedCol: string | null;
  lens: LensId;
  layersFilter: string[];
  tagsFilter: string[];
  showReports?: boolean;
  linkedOnly?: boolean;
  selectedModelId: string | null;
}

/** Snapshot the undoable / saveable slice (Sets → arrays; transient hover and
 *  loading flags excluded). */
export function serializeState(s: LineageState): SerializedLineageState {
  return {
    rawNodes: s.rawNodes,
    rawEdges: s.rawEdges,
    centerId: s.centerId,
    depth: s.depth,
    direction: s.direction,
    collapsed: [...s.collapsed],
    hidden: [...s.hidden],
    positions: s.positions,
    layoutMode: s.layoutMode,
    pinnedCol: s.pinnedCol,
    lens: s.lens,
    layersFilter: [...s.layersFilter],
    tagsFilter: [...s.tagsFilter],
    showReports: s.showReports,
    linkedOnly: s.linkedOnly,
    selectedModelId: s.selectedModelId,
  };
}

export function deserializeState(s: SerializedLineageState): LineageState {
  return {
    ...initialLineageState,
    ...s,
    collapsed: new Set(s.collapsed),
    hidden: new Set(s.hidden),
    layersFilter: new Set(s.layersFilter),
    tagsFilter: new Set(s.tagsFilter),
    linkedOnly: s.linkedOnly ?? false,
    hoverCol: null,
    loading: false,
    loadingText: "",
    error: null,
  };
}
