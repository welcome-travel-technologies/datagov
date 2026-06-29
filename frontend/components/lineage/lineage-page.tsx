"use client";

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { ReactFlowProvider, type Node } from "@xyflow/react";
import { useSearchParams } from "next/navigation";
import { Network } from "lucide-react";
import { Spinner } from "@/components/ui/misc";
import { LineageCanvas } from "@/components/lineage/canvas/lineage-canvas";
import {
  ColumnContextMenu,
  type ColumnMenuAction,
  type ColumnMenuState,
} from "@/components/lineage/canvas/column-context-menu";
import { CanvasProvider, EMPTY_HIGHLIGHT, type Highlight } from "@/components/lineage/canvas/context";
import { LeftSidebar, type Grouping } from "@/components/lineage/panels/left-sidebar";
import { BottomToolbar } from "@/components/lineage/panels/bottom-toolbar";
import { LensLegend } from "@/components/lineage/panels/legend";
import { ModelDetailSidebar } from "@/components/lineage/model-detail-sidebar";
import {
  lineageReducer,
  initialLineageState,
  serializeState,
  deserializeState,
  type LineageState,
  type LineageAction,
  type SerializedLineageState,
} from "@/components/lineage/lineage-store";
import { buildColibriFlow, applyEdgeHighlight } from "@/lib/lineage/build-flow";
import { columnLineage, dbtBuildCommand } from "@/lib/lineage/column-model";
import { getLens, cardLayer, cardAccent, type LensId } from "@/lib/lineage/lens";
import type { ModelCard } from "@/lib/lineage/column-model";
import { useHistory } from "@/lib/lineage/history";
import {
  loadViews,
  saveView as persistSaveView,
  deleteView as persistDeleteView,
  type SavedView,
} from "@/lib/lineage/saved-views";
import { useResizableWidth } from "@/lib/lineage/use-resizable-width";
import { api, type NetworkNode, type Direction } from "@/lib/api";

/** Resizable-panel bounds (px). The chosen widths are persisted to localStorage. */
const SIDEBAR_MIN_W = 200;
const SIDEBAR_MAX_W = 520;
const SIDEBAR_DEFAULT_W = 256;
const SIDEBAR_WIDTH_KEY = "lineage:sidebarWidth";

const DETAIL_MIN_W = 280;
const DETAIL_MAX_W = 640;
const DETAIL_DEFAULT_W = 320;
const DETAIL_WIDTH_KEY = "lineage:detailWidth";

/** Actions that change the saveable view and should be undoable. */
const UNDOABLE: ReadonlySet<LineageAction["type"]> = new Set([
  "MERGE_GRAPH",
  "TOGGLE_COLLAPSE",
  "SET_COLLAPSED",
  "HIDE_NODES",
  "UNHIDE_ALL",
  "SET_POSITION",
  "CLEAR_POSITIONS",
  "FOCUS_COLUMN",
  "SET_LENS",
]);

function LineageExplorer({ onSelectModel }: { onSelectModel?: (id: string) => void }) {
  const search = useSearchParams();
  const [state, dispatch] = useReducer(lineageReducer, initialLineageState);
  const { rawNodes, rawEdges, centerId, direction, collapsed, hidden, positions } = state;

  // ---- undo / redo --------------------------------------------------------
  // useHistory returns a fresh object each render, but its methods are stable
  // (useCallback). Depend on the METHODS, never the object — otherwise callbacks
  // that capture `history` change every render and re-fire effects in a loop.
  const history = useHistory<SerializedLineageState>();
  const { push: pushHistory, undo: undoHistory, redo: redoHistory, reset: resetHistory } = history;
  const stateRef = useRef(state);
  stateRef.current = state;

  // Push a snapshot before any undoable mutation; raw dispatch otherwise.
  const commit = useCallback(
    (action: LineageAction) => {
      if (UNDOABLE.has(action.type)) pushHistory(serializeState(stateRef.current));
      dispatch(action);
    },
    [pushHistory],
  );

  // Skip the auto-load effect once when a RESTORE changes the center (undo /
  // redo / load-view already carry the graph, so we must not re-fetch it).
  const suppressLoadRef = useRef(false);
  // Holds a column id while a full-column-lineage fetch is in flight; once the
  // rebuilt model contains that column, an effect focuses + collapses around it.
  const pendingFocusRef = useRef<string | null>(null);
  const restore = useCallback((snap: SerializedLineageState) => {
    if (snap.centerId !== stateRef.current.centerId) suppressLoadRef.current = true;
    dispatch({ type: "RESTORE", state: deserializeState(snap) });
  }, []);

  const undo = useCallback(() => {
    const snap = undoHistory(serializeState(stateRef.current));
    if (snap) restore(snap);
  }, [undoHistory, restore]);
  const redo = useCallback(() => {
    const snap = redoHistory(serializeState(stateRef.current));
    if (snap) restore(snap);
  }, [redoHistory, restore]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const mod = e.metaKey || e.ctrlKey;
      if (!mod) return;
      if (e.key === "z" && !e.shiftKey) {
        e.preventDefault();
        undo();
      } else if ((e.key === "z" && e.shiftKey) || e.key === "y") {
        e.preventDefault();
        redo();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [undo, redo]);

  // ---- data loading -------------------------------------------------------
  const loadEgo = useCallback(
    async (id: string, d: number, dir: Direction, full = false) => {
      dispatch({ type: "LOAD_START", text: "Loading lineage…" });
      try {
        const data = await api.network.ego({ node_id: id, depth: d, direction: dir, mode: "unified", full });
        // "Show full lineage" (full=true) prunes each card to the columns on the
        // lineage so one huge card can't dominate the canvas; a plain focus does not.
        dispatch({ type: "LOAD_SUCCESS", nodes: data.nodes || [], links: data.links || [], centerId: id, linkedOnly: full });
        resetHistory(); // a fresh graph starts a new undo timeline
      } catch (e) {
        dispatch({ type: "LOAD_ERROR", error: e instanceof Error ? e.message : "Failed to load lineage." });
      }
    },
    [resetHistory],
  );

  useEffect(() => {
    if (!centerId) return;
    if (suppressLoadRef.current) {
      suppressLoadRef.current = false;
      return;
    }
    // Selecting an element loads ONLY that element (depth 0): its card with its
    // columns, no neighbours. The user then pulls in lineage with the +/- buttons
    // on a card or the "Show full lineage" action. depth/direction are inputs to
    // those follow-up actions, not the initial focus, so they don't retrigger here.
    loadEgo(centerId, 0, stateRef.current.direction);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [centerId, loadEgo]);

  // The "next action" after focusing one node: pull in its ENTIRE upstream +
  // downstream lineage (full=true → column edges followed to their transitive
  // ends, no depth cap), in the configured direction. "Show full lineage" means
  // full — there is no depth bound here.
  const showFullLineage = useCallback(() => {
    const s = stateRef.current;
    if (s.centerId) loadEgo(s.centerId, 1, s.direction, true);
  }, [loadEgo]);

  // Deep-link: /lineage?node_id=...
  const didInit = useRef(false);
  useEffect(() => {
    if (didInit.current) return;
    didInit.current = true;
    const nid = search.get("node_id");
    if (nid) dispatch({ type: "SET_CENTER", centerId: nid });
  }, [search]);

  // Latest built flow (for freezing positions before a lazy merge).
  const builtRef = useRef<ReturnType<typeof buildColibriFlow> | null>(null);

  const expand = useCallback(
    async (nodeId: string, dir: Direction) => {
      dispatch({ type: "LOAD_START", text: "Expanding…" });
      try {
        const data = await api.network.ego({ node_id: nodeId, depth: 1, direction: dir, mode: "unified" });
        const freeze = builtRef.current
          ? Object.fromEntries(builtRef.current.nodes.map((n) => [n.id, n.position]))
          : undefined;
        commit({ type: "MERGE_GRAPH", nodes: data.nodes || [], links: data.links || [], freeze });
      } catch {
        dispatch({ type: "LOAD_ERROR", error: "Failed to expand." });
      }
    },
    [commit],
  );

  // ---- derived: flow elements + model -------------------------------------
  const built = useMemo(
    () =>
      buildColibriFlow(rawNodes, rawEdges, centerId, {
        collapsed,
        hidden,
        positions,
        hiddenLayers: state.layersFilter,
        tagsFilter: state.tagsFilter,
        includeReportCards: state.showReports,
        // A pinned column drives "focused collapse": collapsed cards keep just
        // the connected columns visible. Hover traces only dim (cheaper, and no
        // card resizing as the pointer moves), so they don't feed the rebuild.
        pinnedCol: state.pinnedCol,
        // "Show full lineage": drop columns that aren't on the lineage.
        linkedColumnsOnly: state.linkedOnly,
      }),
    [
      rawNodes,
      rawEdges,
      centerId,
      collapsed,
      hidden,
      positions,
      state.layersFilter,
      state.tagsFilter,
      state.showReports,
      state.pinnedCol,
      state.linkedOnly,
    ],
  );
  builtRef.current = built;

  // Distinct layers / tags present in the loaded graph (for the toolbar filters).
  const presentLayers = useMemo(() => {
    const s = new Set<string>();
    for (const c of built.model.cards) s.add(cardLayer(c));
    return [...s] as ReturnType<typeof cardLayer>[];
  }, [built.model.cards]);
  const presentTags = useMemo(() => {
    const s = new Set<string>();
    for (const c of built.model.cards) for (const t of c.tags) s.add(t);
    return [...s].sort();
  }, [built.model.cards]);

  // ---- derived: column trace highlight ------------------------------------
  const traceCol = state.pinnedCol ?? state.hoverCol;
  const displayHighlight = useMemo<Highlight>(() => {
    if (!traceCol || !built.model) return EMPTY_HIGHLIGHT;
    const t = columnLineage(built.model, traceCol);
    return { active: true, cols: t.cols, cards: t.cards, edges: t.edges, selectedCol: traceCol };
  }, [traceCol, built.model]);

  const lens = useMemo(() => getLens(state.lens), [state.lens]);

  // ---- derived: selected-model detail panel -------------------------------
  // The card clicked via its header (SELECT_MODEL); drives the right detail
  // sidebar. Found in the current graph so its columns/type render instantly.
  const selectedCard = useMemo(
    () =>
      state.selectedModelId
        ? (built.model.cards.find((c) => c.id === state.selectedModelId) ?? null)
        : null,
    [state.selectedModelId, built.model.cards],
  );
  const onCloseDetail = useCallback(() => dispatch({ type: "SELECT_MODEL", id: null }), []);

  const rfEdges = useMemo(
    () => (displayHighlight.active ? applyEdgeHighlight(built.edges, displayHighlight) : built.edges),
    [built.edges, displayHighlight],
  );

  const fitKey = useMemo(
    () => `${centerId}|${built.nodes.length}|${[...collapsed].sort().join(",")}|${state.layoutMode}`,
    [centerId, built.nodes.length, collapsed, state.layoutMode],
  );

  // ---- interaction handlers (stable via refs to current model) ------------
  const modelRef = useRef(built.model);
  modelRef.current = built.model;

  // "Show full column lineage": re-fetch the column's entire upstream +
  // downstream chain (column edges followed to their transitive ends via
  // full=true, not the depth-bounded ego load), then — once the model rebuilds —
  // collapse the models and show only the lineage-related columns.
  const loadFullColumnLineage = useCallback(
    async (colId: string) => {
      const card = modelRef.current?.colToCard[colId] ?? null;
      dispatch({ type: "LOAD_START", text: "Loading full column lineage…" });
      try {
        const data = await api.network.ego({
          node_id: colId,
          direction: "both",
          mode: "unified",
          full: true,
        });
        // Defer focusing until the model is rebuilt from the new graph (effect below).
        pendingFocusRef.current = colId;
        const nextCenter = card ?? colId;
        // A center change would otherwise retrigger the depth-0 auto-load effect
        // and clobber the freshly fetched full graph — suppress that single fire.
        if (nextCenter !== stateRef.current.centerId) suppressLoadRef.current = true;
        dispatch({
          type: "LOAD_SUCCESS",
          nodes: data.nodes || [],
          links: data.links || [],
          centerId: nextCenter,
        });
        resetHistory();
      } catch (e) {
        pendingFocusRef.current = null;
        dispatch({
          type: "LOAD_ERROR",
          error: e instanceof Error ? e.message : "Failed to load full column lineage.",
        });
      }
    },
    [resetHistory],
  );

  // After a full-column-lineage fetch lands and the model rebuilds with the new
  // graph, focus that column: hide unrelated cards, pin the trace, and collapse
  // the related models so only the lineage columns remain visible.
  useEffect(() => {
    const colId = pendingFocusRef.current;
    if (!colId) return;
    const model = built.model;
    if (!model.colToCard[colId]) return; // model not yet rebuilt with this column
    pendingFocusRef.current = null;
    const trace = columnLineage(model, colId);
    commit({
      type: "FOCUS_COLUMN",
      colId,
      relatedCards: [...trace.cards],
      allCards: model.cards.map((c) => c.id),
      collapse: true,
    });
  }, [built, commit]);

  const [ctxMenu, setCtxMenu] = useState<ColumnMenuState | null>(null);

  const onColumnClick = useCallback(
    (colId: string) =>
      dispatch({ type: "SET_PINNED_COL", colId: state.pinnedCol === colId ? null : colId }),
    [state.pinnedCol],
  );
  const onColumnHover = useCallback((colId: string | null) => dispatch({ type: "SET_HOVER_COL", colId }), []);
  const onColumnContext = useCallback((colId: string, x: number, y: number) => {
    const model = modelRef.current;
    const card = model?.cards.find((c) => c.id === model.colToCard[colId]);
    setCtxMenu({ x, y, colId, canCopyDbt: !!(card && dbtBuildCommand(card)) });
  }, []);
  const onCardHeaderClick = useCallback(
    (cardId: string) => {
      dispatch({ type: "SELECT_MODEL", id: cardId });
      onSelectModel?.(cardId);
    },
    [onSelectModel],
  );
  const onToggleCollapse = useCallback((cardId: string) => commit({ type: "TOGGLE_COLLAPSE", cardId }), [commit]);
  const onExpandUpstream = useCallback((nodeId: string) => expand(nodeId, "upstream"), [expand]);
  const onExpandDownstream = useCallback((nodeId: string) => expand(nodeId, "downstream"), [expand]);

  // ---- context-menu actions ----------------------------------------------
  const onMenuAction = useCallback(
    (action: ColumnMenuAction, colId: string) => {
      const model = modelRef.current;
      if (!model) return;
      if (action === "show-lineage") {
        // Re-fetch the full upstream + downstream column chain, then focus it
        // (collapsed models, lineage columns only). See loadFullColumnLineage.
        void loadFullColumnLineage(colId);
        return;
      }
      const trace = columnLineage(model, colId);
      const allCards = model.cards.map((c) => c.id);
      if (action === "unfold") {
        const next = [...stateRef.current.collapsed].filter((id) => !trace.cards.has(id));
        commit({ type: "SET_COLLAPSED", ids: next });
      } else if (action === "hide-unrelated") {
        commit({ type: "HIDE_NODES", ids: allCards.filter((id) => !trace.cards.has(id)) });
      } else if (action === "copy-dbt") {
        const card = model.cards.find((c) => c.id === model.colToCard[colId]);
        const cmd = card && dbtBuildCommand(card);
        if (cmd) void navigator.clipboard?.writeText(cmd);
      }
    },
    [commit, loadFullColumnLineage],
  );

  // ---- P3 chrome: sidebar grouping, toolbar, saved views -----------------
  const [grouping, setGrouping] = useState<Grouping>("folder");
  const [savedViews, setSavedViews] = useState<SavedView[]>([]);
  useEffect(() => setSavedViews(loadViews()), []);

  // ---- resizable panels ---------------------------------------------------
  // The left directory sidebar (handle on its right edge) and the right detail
  // panel (handle on its left edge). Both persist their width to localStorage.
  const sidebar = useResizableWidth({
    defaultWidth: SIDEBAR_DEFAULT_W,
    min: SIDEBAR_MIN_W,
    max: SIDEBAR_MAX_W,
    storageKey: SIDEBAR_WIDTH_KEY,
    edge: "right",
  });
  const detail = useResizableWidth({
    defaultWidth: DETAIL_DEFAULT_W,
    min: DETAIL_MIN_W,
    max: DETAIL_MAX_W,
    storageKey: DETAIL_WIDTH_KEY,
    edge: "left",
  });

  // Full asset directory for the sidebar tree (everything we have), fetched once
  // and independent of whatever ego graph is currently loaded.
  const [catalogNodes, setCatalogNodes] = useState<NetworkNode[]>([]);
  useEffect(() => {
    let alive = true;
    api.network
      .assets()
      .then((d) => alive && setCatalogNodes(d.nodes || []))
      .catch(() => alive && setCatalogNodes([]));
    return () => {
      alive = false;
    };
  }, []);

  const onArrange = useCallback(() => commit({ type: "CLEAR_POSITIONS" }), [commit]);
  const onCollapseAll = useCallback(
    () => commit({ type: "SET_COLLAPSED", ids: builtRef.current?.model.cards.map((c) => c.id) ?? [] }),
    [commit],
  );
  const onExpandAll = useCallback(() => commit({ type: "SET_COLLAPSED", ids: [] }), [commit]);
  const onUnhideAll = useCallback(() => commit({ type: "UNHIDE_ALL" }), [commit]);
  const onLensChange = useCallback((l: LensId) => commit({ type: "SET_LENS", lens: l }), [commit]);
  const onToggleLayer = useCallback((l: string) => commit({ type: "TOGGLE_LAYER", group: l }), [commit]);
  const onToggleTag = useCallback(
    (t: string) => {
      const next = new Set(stateRef.current.tagsFilter);
      if (next.has(t)) next.delete(t);
      else next.add(t);
      commit({ type: "SET_TAGS_FILTER", tags: [...next] });
    },
    [commit],
  );

  const onSaveView = useCallback((name: string) => {
    const s = serializeState(stateRef.current);
    const view: SavedView = {
      id: globalThis.crypto?.randomUUID?.() ?? `v_${Date.now()}`,
      name,
      createdAt: Date.now(),
      centerId: s.centerId,
      collapsed: s.collapsed,
      hidden: s.hidden,
      positions: s.positions,
      layoutMode: s.layoutMode,
      pinnedCol: s.pinnedCol,
      lens: s.lens,
      layersFilter: s.layersFilter,
      tagsFilter: s.tagsFilter,
      linkedOnly: s.linkedOnly,
      rawNodes: s.rawNodes,
      rawEdges: s.rawEdges,
      viewport: null,
    };
    setSavedViews(persistSaveView(view));
  }, []);

  const onLoadView = useCallback(
    (view: SavedView) => {
      restore({
        rawNodes: view.rawNodes,
        rawEdges: view.rawEdges,
        centerId: view.centerId,
        depth: stateRef.current.depth,
        direction: stateRef.current.direction,
        collapsed: view.collapsed,
        hidden: view.hidden,
        positions: view.positions,
        layoutMode: view.layoutMode as LineageState["layoutMode"],
        pinnedCol: view.pinnedCol,
        lens: view.lens as LensId,
        layersFilter: view.layersFilter,
        tagsFilter: view.tagsFilter,
        linkedOnly: view.linkedOnly,
        selectedModelId: view.centerId,
      });
      resetHistory();
    },
    [restore, resetHistory],
  );

  const onDeleteView = useCallback((id: string) => setSavedViews(persistDeleteView(id)), []);

  const canvasCtx = useMemo(
    () => ({
      highlight: displayHighlight,
      lens,
      onColumnClick,
      onColumnHover,
      onColumnContext,
      onCardHeaderClick,
      onToggleCollapse,
      onExpandUpstream,
      onExpandDownstream,
    }),
    [
      displayHighlight,
      lens,
      onColumnClick,
      onColumnHover,
      onColumnContext,
      onCardHeaderClick,
      onToggleCollapse,
      onExpandUpstream,
      onExpandDownstream,
    ],
  );

  const miniMapColor = useCallback((n: Node) => {
    const card = (n.data as { card?: ModelCard })?.card;
    return card ? cardAccent(card) : "#94a3b8";
  }, []);

  const onPaneClick = useCallback(() => {
    dispatch({ type: "SET_PINNED_COL", colId: null });
    dispatch({ type: "SET_HOVER_COL", colId: null });
  }, []);

  const recenterFromTree = useCallback((id: string) => {
    dispatch({ type: "SET_CENTER", centerId: id });
  }, []);

  const hasGraph = built.nodes.length > 0;

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      {/* sidebar (search + directory) + canvas */}
      <div className="flex min-h-0 flex-1">
        <aside
          className="hidden min-h-0 shrink-0 rounded-lg border border-line bg-card lg:flex"
          style={{ width: sidebar.width }}
        >
          <LeftSidebar
            nodes={catalogNodes}
            activeId={centerId}
            onSelect={recenterFromTree}
            grouping={grouping}
            onGroupingChange={setGrouping}
            direction={direction}
            onDirectionChange={(dir) => dispatch({ type: "SET_DIRECTION", direction: dir })}
            showReports={state.showReports}
            onShowReportsChange={(v) => dispatch({ type: "SET_SHOW_REPORTS", show: v })}
            savedViews={savedViews}
            canSaveView={hasGraph}
            onSaveView={onSaveView}
            onLoadView={onLoadView}
            onDeleteView={onDeleteView}
          />
        </aside>

        {/* drag-to-resize handle (sits in the gutter between sidebar and canvas) */}
        <div
          onPointerDown={sidebar.onResizeStart}
          onDoubleClick={sidebar.reset}
          title="Drag to resize · double-click to reset"
          className="group hidden w-3 shrink-0 cursor-col-resize touch-none items-center justify-center self-stretch lg:flex"
        >
          <div className="h-10 w-1 rounded-full bg-line transition-colors group-hover:bg-brand/70" />
        </div>

        <ReactFlowProvider>
          <div className="relative h-full flex-1 overflow-hidden rounded-lg border border-line bg-card">
            <CanvasProvider value={canvasCtx}>
              <LineageCanvas
                nodes={built.nodes}
                edges={rfEdges}
                fitKey={fitKey}
                onNodeMove={(id, pos) => commit({ type: "SET_POSITION", id, pos })}
                onPaneClick={onPaneClick}
                miniMapColor={miniMapColor}
              />
            </CanvasProvider>

            {/* focus → expand: load the full lineage around the selected element */}
            {hasGraph && (
              <div className="absolute left-3 top-3 z-10">
                <button
                  type="button"
                  onClick={showFullLineage}
                  title="Load the full upstream + downstream lineage around this element"
                  className="flex items-center gap-1.5 rounded-md border border-line bg-panel/95 px-2.5 py-1.5 text-[12px] font-medium text-foreground/80 shadow-card backdrop-blur hover:border-brand hover:text-brand"
                >
                  <Network className="h-3.5 w-3.5" />
                  Show full lineage
                </button>
              </div>
            )}

            {/* bottom toolbar (inside provider so it can call fitView) */}
            {hasGraph && (
              <div className="absolute bottom-3 left-1/2 z-10 -translate-x-1/2">
                <BottomToolbar
                  canUndo={history.canUndo}
                  canRedo={history.canRedo}
                  onUndo={undo}
                  onRedo={redo}
                  onArrange={onArrange}
                  onCollapseAll={onCollapseAll}
                  onExpandAll={onExpandAll}
                  hasHidden={state.hidden.size > 0}
                  onUnhideAll={onUnhideAll}
                  lens={state.lens}
                  onLensChange={onLensChange}
                  layers={presentLayers}
                  hiddenLayers={state.layersFilter}
                  onToggleLayer={onToggleLayer}
                  tags={presentTags}
                  tagsFilter={state.tagsFilter}
                  onToggleTag={onToggleTag}
                />
              </div>
            )}

            {/* lens legend */}
            {hasGraph && (
              <div className="absolute bottom-3 right-3 z-10">
                <LensLegend lens={lens} />
              </div>
            )}

            <ColumnContextMenu menu={ctxMenu} onAction={onMenuAction} onClose={() => setCtxMenu(null)} />

            {!hasGraph && !state.loading && (
              <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-[14px] text-faint">
                {state.error ? (
                  <span className="text-err">{state.error}</span>
                ) : (
                  "Search or pick a model from the directory on the left to load its lineage."
                )}
              </div>
            )}
            {state.loading && (
              <div className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-3 bg-background/70">
                <Spinner className="h-6 w-6" />
                <span className="text-[13px] text-muted-foreground">{state.loadingText}</span>
              </div>
            )}
          </div>
        </ReactFlowProvider>

        {/* right detail panel — opens when a model card header is clicked */}
        {selectedCard && (
          <>
            {/* drag-to-resize handle (gutter between canvas and detail panel) */}
            <div
              onPointerDown={detail.onResizeStart}
              onDoubleClick={detail.reset}
              title="Drag to resize · double-click to reset"
              className="group flex w-3 shrink-0 cursor-col-resize touch-none items-center justify-center self-stretch"
            >
              <div className="h-10 w-1 rounded-full bg-line transition-colors group-hover:bg-brand/70" />
            </div>

            <ModelDetailSidebar
              card={selectedCard}
              width={detail.width}
              lens={lens}
              selectedColId={state.pinnedCol}
              onColumnClick={onColumnClick}
              onClose={onCloseDetail}
            />
          </>
        )}
      </div>
    </div>
  );
}

/** Public entry — the new colibri-style lineage explorer. */
export function LineagePage({ onSelectModel }: { onSelectModel?: (id: string) => void }) {
  return <LineageExplorer onSelectModel={onSelectModel} />;
}

// re-export for callers that want the state type
export type { LineageState };
