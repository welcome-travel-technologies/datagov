"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Check, AlertTriangle } from "lucide-react";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  ConnectionMode,
  addEdge,
  useNodesState,
  useEdgesState,
  useReactFlow,
  getNodesBounds,
  getViewportForBounds,
  type Connection,
  type OnSelectionChangeParams,
} from "@xyflow/react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type MetricsMap } from "@/lib/api";
import { cn } from "@/lib/utils";
import { typeMeta } from "@/lib/metrics-canvas/catalog";
import { uid } from "@/lib/metrics-canvas/ids";
import { makeCatalogNode, makeTypeNode } from "@/lib/metrics-canvas/catalog-tiles";
import { readPayload } from "@/lib/metrics-canvas/dnd";
import { arrangeDagre } from "@/lib/metrics-canvas/layout";
import { useHistory, type HistorySnapshot } from "@/lib/metrics-canvas/history";
import {
  fromDoc,
  rfToStoredEdge,
  rfToStoredNode,
  storedToRfEdge,
  storedToRfNode,
  toDoc,
  exportJson,
  exportPng,
  importJson,
  decorateEdgeLabel,
  type RfEdge,
  type RfNode,
} from "@/lib/metrics-canvas/serialize";
import { emptyDoc, type CanvasGroup, type CanvasMeta, type CanvasNodeData } from "@/lib/metrics-canvas/types";
import { nodeTypes } from "@/components/metrics-canvas/nodes";
import { edgeTypes } from "@/components/metrics-canvas/edges";
import { CanvasInteractionProvider } from "@/components/metrics-canvas/interaction";
import { Palette } from "@/components/metrics-canvas/palette";
import { PropertiesPanel } from "@/components/metrics-canvas/properties-panel";
import { Toolbar } from "@/components/metrics-canvas/toolbar";
import { SavedMapsPanel, CANVAS_MAPS_KEY } from "@/components/metrics-canvas/maps-sidebar";
import { GroupsOverlay } from "@/components/metrics-canvas/groups-overlay";
import { ShareDialog } from "@/components/metrics-canvas/share-dialog";

const GROUP_COLORS = ["#0ea5e9", "#a855f7", "#f59e0b", "#10b981", "#ef4444", "#6366f1"];

// Autosave is opt-in (off by default — manual saving is the norm) and the choice
// is remembered across sessions.
const AUTOSAVE_KEY = "metrics-map:autosave";

function newMeta(): CanvasMeta {
  return { ...emptyDoc().meta };
}

export function MetricsCanvas() {
  const qc = useQueryClient();
  const rf = useReactFlow();
  const { screenToFlowPosition, fitView, getViewport, setViewport } = rf;

  const [nodes, setNodes, onNodesChange] = useNodesState<RfNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<RfEdge>([]);
  const [groups, setGroups] = useState<CanvasGroup[]>([]);
  const [meta, setMeta] = useState<CanvasMeta>(newMeta);
  const drawingsRef = useRef<unknown[]>([]);

  const [draftId, setDraftId] = useState<number | null>(null);
  const [dirty, setDirty] = useState(false);
  const [rev, setRev] = useState(0);

  // Autosave (opt-in, persisted) and a transient save/import notification.
  const [autosave, setAutosave] = useState(false);
  const [toast, setToast] = useState<{ msg: string; kind: "ok" | "err" } | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const [selNodeId, setSelNodeId] = useState<string | null>(null);
  const [selEdgeId, setSelEdgeId] = useState<string | null>(null);
  const [selCount, setSelCount] = useState(0);
  // Group selection lives outside React Flow (groups are an overlay, not nodes):
  // clicking a group's label selects it and shows the Group panel.
  const [selGroupId, setSelGroupId] = useState<string | null>(null);

  // Public-sharing state for the active map (mirrors the saved row).
  const [shareOpen, setShareOpen] = useState(false);
  const [publicToken, setPublicToken] = useState<string | null>(null);
  const [publicCanDrag, setPublicCanDrag] = useState(true);

  const history = useHistory<HistorySnapshot>();
  const wrapperRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  // Always points at the latest `save` so the keydown handler (registered before
  // `save` is declared) can call it without a stale closure or TDZ.
  const saveRef = useRef<() => void>(() => {});

  // Keep refs to the latest nodes/edges for snapshotting without stale closures.
  const nodesRef = useRef(nodes);
  const edgesRef = useRef(edges);
  const groupsRef = useRef(groups);
  const metaRef = useRef(meta);
  nodesRef.current = nodes;
  edgesRef.current = edges;
  groupsRef.current = groups;
  metaRef.current = meta;

  const snapshot = useCallback(
    (): HistorySnapshot => ({
      nodes: nodesRef.current.map(rfToStoredNode),
      edges: edgesRef.current.map(rfToStoredEdge),
      groups: groupsRef.current,
      meta: metaRef.current,
    }),
    [],
  );

  const touch = useCallback(() => {
    setDirty(true);
    setRev((r) => r + 1);
  }, []);

  // Pop a small notification that auto-dismisses after a couple of seconds.
  const showToast = useCallback((msg: string, kind: "ok" | "err" = "ok") => {
    setToast({ msg, kind });
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 2200);
  }, []);

  // Restore the saved autosave preference once on mount (client only).
  useEffect(() => {
    try {
      setAutosave(window.localStorage.getItem(AUTOSAVE_KEY) === "1");
    } catch {
      /* localStorage unavailable — leave autosave off */
    }
    return () => {
      if (toastTimer.current) clearTimeout(toastTimer.current);
    };
  }, []);

  const toggleAutosave = useCallback(() => {
    setAutosave((on) => {
      const next = !on;
      try {
        window.localStorage.setItem(AUTOSAVE_KEY, next ? "1" : "0");
      } catch {
        /* ignore persistence failure */
      }
      showToast(next ? "Autosave on" : "Autosave off");
      return next;
    });
  }, [showToast]);

  const pushHistory = useCallback(() => history.push(snapshot()), [history, snapshot]);

  const restore = useCallback(
    (snap: HistorySnapshot) => {
      setNodes(snap.nodes.map(storedToRfNode));
      setEdges(snap.edges.map(storedToRfEdge));
      setGroups(snap.groups);
      setMeta(snap.meta);
      setSelNodeId(null);
      setSelEdgeId(null);
      setSelCount(0);
      setSelGroupId(null);
      touch();
    },
    [setNodes, setEdges, touch],
  );

  // ---- mutations ----------------------------------------------------------

  const onConnect = useCallback(
    (conn: Connection) => {
      pushHistory();
      const color = "#64748b";
      const edge: RfEdge = {
        ...conn,
        id: uid("e"),
        type: "smoothstep",
        data: { color, arrowEnd: true },
        markerEnd: { type: "arrowclosed" as never, color, width: 16, height: 16 },
        style: { stroke: color, strokeWidth: 2 },
      };
      setEdges((eds) => addEdge(edge, eds));
      touch();
    },
    [pushHistory, setEdges, touch],
  );

  const onDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  }, []);

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      const payload = readPayload(e.dataTransfer);
      if (!payload) return;
      const pos = screenToFlowPosition({ x: e.clientX, y: e.clientY });
      const stored =
        payload.kind === "type" ? makeTypeNode(payload.elementType, pos) : makeCatalogNode(payload.tile, pos);
      pushHistory();
      const node = storedToRfNode(stored);
      setNodes((ns) => ns.concat({ ...node, selected: true }).map((n) => (n.id === node.id ? n : { ...n, selected: false })));
      setSelNodeId(node.id);
      setSelEdgeId(null);
      setSelCount(1);
      touch();
    },
    [screenToFlowPosition, pushHistory, setNodes, touch],
  );

  const patchNodeData = useCallback(
    (id: string, patch: Partial<CanvasNodeData>) => {
      pushHistory();
      setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data: { ...n.data, ...patch } } : n)));
      touch();
    },
    [pushHistory, setNodes, touch],
  );

  const patchEdge = useCallback(
    (id: string, patch: Partial<RfEdge>) => {
      pushHistory();
      setEdges((es) => es.map((e) => (e.id === id ? { ...e, ...patch } : e)));
      touch();
    },
    [pushHistory, setEdges, touch],
  );

  const patchMeta = useCallback(
    (patch: Partial<CanvasMeta>) => {
      setMeta((m) => ({ ...m, ...patch }));
      touch();
    },
    [touch],
  );

  const onLabelCommit = useCallback(
    (id: string, label: string) => patchNodeData(id, { label }),
    [patchNodeData],
  );

  const deleteSelection = useCallback(() => {
    const delNodeIds = new Set(nodesRef.current.filter((n) => n.selected).map((n) => n.id));
    const delEdgeIds = new Set(edgesRef.current.filter((e) => e.selected).map((e) => e.id));
    if (!delNodeIds.size && !delEdgeIds.size) return;
    pushHistory();
    setNodes((ns) => ns.filter((n) => !delNodeIds.has(n.id)));
    setEdges((es) =>
      es.filter((e) => !delEdgeIds.has(e.id) && !delNodeIds.has(e.source) && !delNodeIds.has(e.target)),
    );
    if (delNodeIds.size) {
      setGroups((gs) =>
        gs
          .map((g) => ({ ...g, nodeIds: g.nodeIds.filter((id) => !delNodeIds.has(id)) }))
          .filter((g) => g.nodeIds.length > 0),
      );
    }
    setSelNodeId(null);
    setSelEdgeId(null);
    setSelCount(0);
    setSelGroupId(null);
    touch();
  }, [pushHistory, setNodes, setEdges, touch]);

  // Click a group's label → select it (and clear any node/edge selection so the
  // Group panel is shown). Deselecting nodes fires onSelectionChange with empty
  // arrays, which is guarded there so it doesn't immediately clear selGroupId.
  const selectGroup = useCallback(
    (groupId: string) => {
      setNodes((ns) => (ns.some((n) => n.selected) ? ns.map((n) => ({ ...n, selected: false })) : ns));
      setEdges((es) => (es.some((e) => e.selected) ? es.map((e) => ({ ...e, selected: false })) : es));
      setSelNodeId(null);
      setSelEdgeId(null);
      setSelCount(0);
      setSelGroupId(groupId);
    },
    [setNodes, setEdges],
  );

  const groupSelection = useCallback(() => {
    const ids = nodesRef.current.filter((n) => n.selected).map((n) => n.id);
    if (ids.length < 2) return;
    pushHistory();
    setGroups((gs) => [
      ...gs,
      {
        id: uid("g"),
        name: `Group ${gs.length + 1}`,
        color: GROUP_COLORS[gs.length % GROUP_COLORS.length],
        nodeIds: ids,
      },
    ]);
    touch();
  }, [pushHistory, touch]);

  const ungroup = useCallback(
    (groupId: string) => {
      pushHistory();
      setGroups((gs) => gs.filter((g) => g.id !== groupId));
      setSelGroupId((cur) => (cur === groupId ? null : cur));
      touch();
    },
    [pushHistory, touch],
  );

  const renameGroup = useCallback(
    (groupId: string, name: string) => {
      setGroups((gs) => gs.map((g) => (g.id === groupId ? { ...g, name } : g)));
      touch();
    },
    [touch],
  );

  const setGroupColor = useCallback(
    (groupId: string, color: string) => {
      setGroups((gs) => gs.map((g) => (g.id === groupId ? { ...g, color } : g)));
      touch();
    },
    [touch],
  );

  // Drag a group by its label: shift every member node by the same flow-space
  // delta. History is pushed once at drag start; `touch` (autosave) fires at stop.
  const moveGroupBy = useCallback(
    (groupId: string, dx: number, dy: number) => {
      const g = groupsRef.current.find((gr) => gr.id === groupId);
      if (!g) return;
      const ids = new Set(g.nodeIds);
      setNodes((ns) =>
        ns.map((n) =>
          ids.has(n.id) ? { ...n, position: { x: n.position.x + dx, y: n.position.y + dy } } : n,
        ),
      );
    },
    [setNodes],
  );

  const arrange = useCallback(() => {
    if (!nodesRef.current.length) return;
    pushHistory();

    // Drop redundant connections first. As floating edges, two edges with the
    // same source→target draw the same line, so a second one is just a hidden
    // duplicate (the "doubled line"). Collapse them — keeping a labelled edge
    // over a blank one — but leave genuinely different labelled parallels alone.
    const kept: RfEdge[] = [];
    const slotOf = new Map<string, number>();
    let removed = 0;
    for (const e of edgesRef.current) {
      const key = `${e.source}->${e.target}`;
      const label = e.label ? String(e.label).trim() : "";
      const idx = slotOf.get(key);
      if (idx == null) {
        slotOf.set(key, kept.length);
        kept.push(e);
        continue;
      }
      const existingLabel = kept[idx].label ? String(kept[idx].label).trim() : "";
      if (existingLabel && label && existingLabel !== label) {
        kept.push(e); // distinct labelled parallels — keep both
        continue;
      }
      removed++;
      if (label && !existingLabel) kept[idx] = e; // upgrade to the labelled one
    }
    const deduped = removed ? kept : edgesRef.current;
    if (removed) setEdges(deduped);

    // Edges re-route themselves: they're floating, so they always attach to the
    // facing side of each re-positioned box — no handle bookkeeping needed here.
    const pos = arrangeDagre(nodesRef.current, deduped, "TB", groupsRef.current);
    setNodes((ns) => ns.map((n) => (pos[n.id] ? { ...n, position: pos[n.id] } : n)));
    touch();
    if (removed) showToast(`Merged ${removed} duplicate ${removed === 1 ? "connection" : "connections"}`);
    setTimeout(() => fitView({ padding: 0.2, duration: 300 }), 60);
  }, [pushHistory, setNodes, setEdges, touch, showToast, fitView]);

  const undo = useCallback(() => {
    const snap = history.undo(snapshot());
    if (snap) restore(snap);
  }, [history, snapshot, restore]);

  const redo = useCallback(() => {
    const snap = history.redo(snapshot());
    if (snap) restore(snap);
  }, [history, snapshot, restore]);

  // ---- keyboard: delete + undo/redo (guarded against form fields) ---------
  useEffect(() => {
    function isEditable(t: EventTarget | null): boolean {
      const el = t as HTMLElement | null;
      if (!el) return false;
      const tag = el.tagName;
      return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
    }
    function onKey(e: KeyboardEvent) {
      const mod = e.ctrlKey || e.metaKey;
      // Ctrl/Cmd+S saves — handled even from form fields so it always wins over
      // the browser's own save dialog. Called via ref to dodge declaration order.
      if (mod && e.key.toLowerCase() === "s") {
        e.preventDefault();
        saveRef.current();
        return;
      }
      if (isEditable(e.target)) return;
      if (mod && e.key.toLowerCase() === "z" && !e.shiftKey) {
        e.preventDefault();
        undo();
      } else if (mod && (e.key.toLowerCase() === "y" || (e.key.toLowerCase() === "z" && e.shiftKey))) {
        e.preventDefault();
        redo();
      } else if (e.key === "Delete" || e.key === "Backspace") {
        e.preventDefault();
        deleteSelection();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [undo, redo, deleteSelection]);

  // ---- selection tracking -------------------------------------------------
  const onSelectionChange = useCallback((p: OnSelectionChangeParams) => {
    setSelCount(p.nodes.length);
    setSelNodeId(p.nodes[0]?.id ?? null);
    setSelEdgeId(p.nodes.length === 0 && p.edges.length ? (p.edges[0]?.id ?? null) : null);
    // Selecting a node/edge supersedes a selected group. Only clear on a real
    // selection (not the empty event fired while programmatically deselecting in
    // selectGroup), so group-select doesn't race with this handler.
    if (p.nodes.length || p.edges.length) setSelGroupId(null);
  }, []);

  // Clicking empty canvas clears the group selection (React Flow handles node/edge).
  const onPaneClick = useCallback(() => setSelGroupId(null), []);

  const selectedNode = useMemo(() => nodes.find((n) => n.id === selNodeId) ?? null, [nodes, selNodeId]);
  const selectedEdge = useMemo(() => edges.find((e) => e.id === selEdgeId) ?? null, [edges, selEdgeId]);
  const selectedGroup = useMemo(() => groups.find((g) => g.id === selGroupId) ?? null, [groups, selGroupId]);

  const fontScale = meta.fontScale ?? 1;

  // Concrete inline fills for edge-label pills (see `decorateEdgeLabel`); keeps
  // operator badges legible in the editor and the PNG export. Derived only — the
  // undecorated `edges` state is what gets serialized/saved.
  // Every edge renders as a "floating" edge (attaches to the facing side of each
  // box) regardless of its stored type — so existing maps and skill imports get
  // clean routing without a data migration.
  const rfEdges = useMemo(
    () => edges.map((e) => ({ ...decorateEdgeLabel(e, fontScale), type: "floating" })),
    [edges, fontScale],
  );

  // ---- load / new / save --------------------------------------------------

  const loadState = useCallback(
    (doc = emptyDoc(), id: number | null = null) => {
      const s = fromDoc(doc);
      setNodes(s.nodes);
      setEdges(s.edges);
      setGroups(s.groups);
      setMeta(s.meta);
      drawingsRef.current = s.drawings;
      setDraftId(id);
      setDirty(false);
      setSelNodeId(null);
      setSelEdgeId(null);
      setSelCount(0);
      setSelGroupId(null);
      // Sharing is per-saved-map; reset to "not shared" until the caller (e.g.
      // onSelectMap) hydrates it from the fetched row.
      setPublicToken(null);
      setPublicCanDrag(true);
      history.reset();
      requestAnimationFrame(() => {
        if (s.nodes.length) setViewport(s.viewport);
        else fitView();
      });
    },
    [setNodes, setEdges, history, setViewport, fitView],
  );

  const onNew = useCallback(() => loadState(emptyDoc(), null), [loadState]);

  const onSelectMap = useCallback(
    async (m: MetricsMap) => {
      try {
        const full = await api.metricsMaps.get(m.id);
        loadState(full.graph ?? emptyDoc(full.name), full.id);
        setMeta((prev) => ({ ...prev, name: full.name, description: full.description ?? "" }));
        setPublicToken(full.public_token ?? null);
        setPublicCanDrag(full.public_can_drag ?? true);
      } catch {
        /* ignore */
      }
    },
    [loadState],
  );

  const saveMut = useMutation({
    mutationFn: () => {
      const doc = toDoc({
        meta: metaRef.current,
        viewport: getViewport(),
        nodes: nodesRef.current,
        edges: edgesRef.current,
        groups: groupsRef.current,
        drawings: drawingsRef.current,
      });
      const body = {
        name: metaRef.current.name.trim() || "Untitled Map",
        description: metaRef.current.description ?? "",
        kind: "canvas" as const,
        graph: doc,
      };
      return draftId ? api.metricsMaps.update(draftId, body) : api.metricsMaps.create(body);
    },
    onSuccess: (saved) => {
      setDraftId(saved.id);
      setDirty(false);
      setPublicToken(saved.public_token ?? null);
      setPublicCanDrag(saved.public_can_drag ?? true);
      qc.invalidateQueries({ queryKey: CANVAS_MAPS_KEY });
      showToast("Map saved");
    },
    onError: () => showToast("Save failed — try again", "err"),
  });

  // Manual save (Save button / Ctrl+S): updates the active map or creates one if
  // this is a fresh canvas. Independent of the autosave toggle.
  const save = useCallback(() => {
    if (saveMut.isPending || !nodesRef.current.length) return;
    saveMut.mutate();
  }, [saveMut]);
  saveRef.current = save;

  // "Save current map" from the bottom panel: always creates a NEW named map
  // (mirrors the lineage saved-views "+"), then adopts it as the active draft.
  const saveNamedMut = useMutation({
    mutationFn: (name: string) => {
      const trimmed = name.trim() || "Untitled Map";
      const doc = toDoc({
        meta: { ...metaRef.current, name: trimmed },
        viewport: getViewport(),
        nodes: nodesRef.current,
        edges: edgesRef.current,
        groups: groupsRef.current,
        drawings: drawingsRef.current,
      });
      return api.metricsMaps.create({
        name: trimmed,
        description: metaRef.current.description ?? "",
        kind: "canvas" as const,
        graph: doc,
      });
    },
    onSuccess: (saved) => {
      setDraftId(saved.id);
      setMeta((m) => ({ ...m, name: saved.name }));
      setDirty(false);
      setPublicToken(saved.public_token ?? null);
      setPublicCanDrag(saved.public_can_drag ?? true);
      qc.invalidateQueries({ queryKey: CANVAS_MAPS_KEY });
    },
  });
  const onSaveNamedMap = useCallback((name: string) => saveNamedMut.mutate(name), [saveNamedMut]);

  // If the map currently loaded gets deleted from the panel, drop back to a blank
  // canvas so autosave doesn't keep PATCHing a now-missing record.
  const onMapDeleted = useCallback(
    (id: number) => {
      if (id === draftId) onNew();
    },
    [draftId, onNew],
  );

  // Autosave: opt-in only. When enabled, save 1.5s after the last change (and
  // only once the map has been saved at least once, so we have a record to PATCH).
  useEffect(() => {
    if (!autosave || !draftId || !dirty) return;
    const t = setTimeout(() => saveMut.mutate(), 1500);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rev, draftId, dirty, autosave]);

  // Warn before leaving with unsaved changes (autosave off means nothing else
  // will persist them). Only guards full-page unload/refresh.
  useEffect(() => {
    if (!dirty) return;
    function warn(e: BeforeUnloadEvent) {
      e.preventDefault();
      e.returnValue = "";
    }
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  // ---- import / export ----------------------------------------------------

  const onImportClick = useCallback(() => fileRef.current?.click(), []);
  const onImportFile = useCallback(
    async (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      e.target.value = "";
      if (!file) return;
      try {
        const doc = importJson(await file.text());
        // Always land an import as a fresh, unnamed map (no draftId) so it can
        // never overwrite the map that was open — saving it creates a new record.
        loadState({ ...doc, meta: { ...doc.meta, name: "Untitled Map" } }, null);
        showToast("Imported as a new map");
      } catch {
        showToast("Couldn't import that file", "err");
      }
    },
    [loadState],
  );

  const onExportJson = useCallback(() => {
    exportJson(
      toDoc({
        meta: metaRef.current,
        viewport: getViewport(),
        nodes: nodesRef.current,
        edges: edgesRef.current,
        groups: groupsRef.current,
        drawings: drawingsRef.current,
      }),
    );
  }, [getViewport]);

  const onExportPng = useCallback(async () => {
    const el = wrapperRef.current?.querySelector(".react-flow__viewport") as HTMLElement | null;
    if (!el || !nodesRef.current.length) return;
    const bounds = getNodesBounds(nodesRef.current);
    const width = Math.min(2400, Math.max(480, Math.round(bounds.width + 160)));
    const height = Math.min(2000, Math.max(320, Math.round(bounds.height + 160)));
    const vp = getViewportForBounds(bounds, width, height, 0.2, 2, 0.1);
    await exportPng(el, metaRef.current.name, {
      width,
      height,
      transform: `translate(${vp.x}px, ${vp.y}px) scale(${vp.zoom})`,
    });
  }, []);

  const interaction = useMemo(() => ({ onLabelCommit, fontScale }), [onLabelCommit, fontScale]);

  return (
    <CanvasInteractionProvider value={interaction}>
      <div className="relative flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-line bg-card">
        <Toolbar
          canUndo={history.canUndo}
          canRedo={history.canRedo}
          dirty={dirty}
          saving={saveMut.isPending}
          canSave={nodes.length > 0}
          autosave={autosave}
          onNew={onNew}
          onSave={save}
          onToggleAutosave={toggleAutosave}
          onUndo={undo}
          onRedo={redo}
          onArrange={arrange}
          onFit={() => fitView({ padding: 0.2, duration: 300 })}
          onImport={onImportClick}
          onExportJson={onExportJson}
          onExportPng={onExportPng}
          onShare={() => setShareOpen(true)}
        />

        <div className="grid min-h-0 flex-1 grid-cols-[228px_1fr_264px]">
          {/* left: palette + saved maps pinned at the bottom (matches lineage) */}
          <div className="flex min-h-0 flex-col border-r border-line">
            <div className="min-h-0 flex-1">
              <Palette />
            </div>
            <SavedMapsPanel
              activeId={draftId}
              canSave={nodes.length > 0}
              onSave={onSaveNamedMap}
              onSelect={onSelectMap}
              onDeleted={onMapDeleted}
            />
          </div>

          {/* center: canvas */}
          <div ref={wrapperRef} className="relative min-w-0" onDrop={onDrop} onDragOver={onDragOver}>
            <ReactFlow
              nodes={nodes}
              edges={rfEdges}
              nodeTypes={nodeTypes}
              edgeTypes={edgeTypes}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onConnect={onConnect}
              onSelectionChange={onSelectionChange}
              onPaneClick={onPaneClick}
              onNodeDragStart={pushHistory}
              onNodeDragStop={touch}
              connectionMode={ConnectionMode.Loose}
              deleteKeyCode={null}
              minZoom={0.1}
              maxZoom={2.5}
              proOptions={{ hideAttribution: true }}
              defaultEdgeOptions={{ type: "smoothstep" }}
              style={{ width: "100%", height: "100%" }}
              fitView
            >
              <Background gap={20} color="oklch(0.92 0.004 90)" />
              <Controls showInteractive={false} />
              <MiniMap
                pannable
                zoomable
                nodeColor={(n) => typeMeta((n.data as CanvasNodeData)?.elementType).color}
                nodeStrokeWidth={2}
                maskColor="oklch(0.97 0.003 95 / 0.6)"
              />
              <GroupsOverlay
                groups={groups}
                nodes={nodes}
                fontScale={fontScale}
                selectedId={selGroupId}
                onSelect={selectGroup}
                onDragStart={pushHistory}
                onDrag={moveGroupBy}
                onDragStop={touch}
              />
            </ReactFlow>

            {nodes.length === 0 && (
              <div className="pointer-events-none absolute inset-0 flex items-center justify-center px-6 text-center text-[13px] text-faint">
                Drag elements or catalog items from the left onto the canvas, then connect them with arrows.
              </div>
            )}
          </div>

          {/* right: properties */}
          <div className="min-h-0 overflow-y-auto border-l border-line">
            <PropertiesPanel
              node={selectedNode}
              edge={selectedEdge}
              selectedGroup={selectedGroup}
              meta={meta}
              groups={groups}
              selectionCount={selCount}
              onPatchNodeData={patchNodeData}
              onPatchEdge={patchEdge}
              onPatchMeta={patchMeta}
              onDeleteSelection={deleteSelection}
              onGroupSelection={groupSelection}
              onUngroup={ungroup}
              onRenameGroup={renameGroup}
              onSetGroupColor={setGroupColor}
            />
          </div>
        </div>

        <input ref={fileRef} type="file" accept="application/json,.json" className="hidden" onChange={onImportFile} />

        {toast && (
          <div
            role="status"
            aria-live="polite"
            className={cn(
              "pointer-events-none absolute bottom-4 right-4 z-50 flex items-center gap-2 rounded-md border bg-panel px-3 py-2 text-[12.5px] font-medium shadow-lg",
              toast.kind === "ok" ? "border-ok/30 text-ok" : "border-err/30 text-err",
            )}
          >
            {toast.kind === "ok" ? <Check className="h-4 w-4" /> : <AlertTriangle className="h-4 w-4" />}
            {toast.msg}
          </div>
        )}
      </div>

      <ShareDialog
        open={shareOpen}
        onOpenChange={setShareOpen}
        mapId={draftId}
        publicToken={publicToken}
        canDrag={publicCanDrag}
        onSaveFirst={() => saveMut.mutate()}
        onChanged={(patch) => {
          if (patch.public_token !== undefined) setPublicToken(patch.public_token);
          if (patch.public_can_drag !== undefined) setPublicCanDrag(patch.public_can_drag);
        }}
      />
    </CanvasInteractionProvider>
  );
}
