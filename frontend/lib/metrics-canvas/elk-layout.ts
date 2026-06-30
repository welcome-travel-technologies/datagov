/**
 * ELK-powered auto-layout for the canvas — the "powerful" arrange.
 *
 * Unlike the Dagre path (which only positions nodes and lets the floating edges
 * draw straight lines), ELK runs a layered layout with an *orthogonal edge
 * router*: it returns both node positions and clean right-angle edge routes that
 * weave **around** the boxes. Groups are modelled as ELK compound (container)
 * nodes, so members stay clustered and group frames never overlap — no nested
 * workaround needed.
 *
 * Coordinate handling (verified against elkjs output):
 *  - node x/y are relative to the parent → we accumulate offsets to absolutes.
 *  - edge section points are relative to the endpoints' lowest common ancestor;
 *    with our single level of groups that's either the shared group (add its
 *    absolute offset) or the root (add nothing).
 *
 * `arrangeElk` is async (ELK runs off a lazily-imported bundle). The caller
 * falls back to {@link arrangeDagre} if it throws.
 */
import type { RfEdge, RfNode } from "@/lib/metrics-canvas/serialize";
import type { CanvasGroup } from "@/lib/metrics-canvas/types";
import type { XY } from "@/lib/metrics-canvas/layout";
import type { ElkArrangeOpts } from "@/lib/metrics-canvas/arrange-settings";

// Group container padding — mirrors GroupsOverlay (PAD 16 + label 18) plus slack
// so the drawn frame sits comfortably inside ELK's container box.
const GROUP_PAD_X = 28;
const GROUP_PAD_TOP = 44;
const GROUP_PAD_BOTTOM = 28;

export interface ElkResult {
  /** Absolute top-left position per arranged node id. */
  positions: Record<string, XY>;
  /** Absolute orthogonal route (≥2 points) per edge id. */
  routes: Record<string, XY[]>;
}

interface ElkNode {
  id: string;
  x?: number;
  y?: number;
  width?: number;
  height?: number;
  layoutOptions?: Record<string, string>;
  children?: ElkNode[];
  edges?: ElkEdge[];
}
interface ElkEdge {
  id: string;
  sources: string[];
  targets: string[];
  sections?: { startPoint: XY; endPoint: XY; bendPoints?: XY[] }[];
}

function isLayoutNode(n: RfNode): boolean {
  return n.type !== "note" && n.type !== "container";
}

function sizeOf(n: RfNode): { width: number; height: number } {
  const w = (n.width as number | undefined) ?? (n.measured?.width as number | undefined) ?? 190;
  const h = (n.height as number | undefined) ?? (n.measured?.height as number | undefined) ?? 60;
  return { width: Math.max(40, w), height: Math.max(30, h) };
}

export async function arrangeElk(
  nodes: RfNode[],
  edges: RfEdge[],
  groups: CanvasGroup[] = [],
  opts: ElkArrangeOpts = {
    direction: "DOWN",
    nodeSep: 100,
    rankSep: 190,
    groupSep: 160,
    stagger: false,
    staggerStep: 80,
  },
): Promise<ElkResult> {
  const layoutNodes = nodes.filter(isLayoutNode);
  const ids = new Set(layoutNodes.map((n) => n.id));
  const byId = new Map(layoutNodes.map((n) => [n.id, n]));

  // Each node joins at most one group (the first that claims it).
  const groupOf = new Map<string, string>();
  const clusterIds: string[] = [];
  const clusterMembers = new Map<string, string[]>();
  for (const g of groups) {
    const members = g.nodeIds.filter((id) => ids.has(id) && !groupOf.has(id));
    if (!members.length) continue;
    clusterIds.push(g.id);
    clusterMembers.set(g.id, members);
    for (const m of members) groupOf.set(m, g.id);
  }

  const leaf = (n: RfNode): ElkNode => {
    const { width, height } = sizeOf(n);
    return { id: n.id, width, height };
  };

  const children: ElkNode[] = [];
  for (const n of layoutNodes) if (!groupOf.has(n.id)) children.push(leaf(n));
  for (const gid of clusterIds) {
    children.push({
      id: gid,
      layoutOptions: {
        "elk.padding": `[top=${GROUP_PAD_TOP},left=${GROUP_PAD_X},bottom=${GROUP_PAD_BOTTOM},right=${GROUP_PAD_X}]`,
        // Spacing options are read per-container, NOT inherited from root even
        // with INCLUDE_CHILDREN — so without these the nodes *inside* a group
        // would always sit at ELK's defaults while only the group-to-group gap
        // tracked the sliders. Mirror the root spacing onto every container.
        "elk.spacing.nodeNode": String(opts.nodeSep),
        "elk.layered.spacing.nodeNodeBetweenLayers": String(opts.rankSep),
      },
      children: clusterMembers.get(gid)!.map((id) => leaf(byId.get(id)!)),
    });
  }

  const seen = new Set<string>();
  const elkEdges: ElkEdge[] = [];
  for (const e of edges) {
    if (e.source === e.target) continue;
    if (!ids.has(e.source) || !ids.has(e.target)) continue;
    const key = `${e.source}->${e.target}`;
    if (seen.has(key)) continue;
    seen.add(key);
    elkEdges.push({ id: e.id, sources: [e.source], targets: [e.target] });
  }

  // Root-level spacing governs the gaps between top-level items. When there are
  // groups those items ARE the group frames, so the dedicated `groupSep` drives
  // them; on a flat map (no groups) the root holds the leaf nodes, so the
  // node/row sliders apply there instead.
  const hasGroups = clusterIds.length > 0;
  const rootNodeSep = hasGroups ? opts.groupSep : opts.nodeSep;
  const rootRankSep = hasGroups ? opts.groupSep : opts.rankSep;

  const graph: ElkNode = {
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": opts.direction,
      "elk.edgeRouting": "ORTHOGONAL",
      "elk.hierarchyHandling": "INCLUDE_CHILDREN",
      "elk.layered.spacing.nodeNodeBetweenLayers": String(rootRankSep),
      "elk.spacing.nodeNode": String(rootNodeSep),
      "elk.spacing.edgeNode": "28",
      "elk.spacing.edgeEdge": "18",
      "elk.layered.spacing.edgeNodeBetweenLayers": "28",
      "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
      "elk.padding": "[top=24,left=24,bottom=24,right=24]",
    },
    children,
    edges: elkEdges,
  };

  const mod = (await import("elkjs/lib/elk.bundled.js")) as unknown as {
    default: new () => { layout: (g: ElkNode) => Promise<ElkNode> };
  };
  const elk = new mod.default();
  const res = await elk.layout(graph);

  // Absolute leaf positions + absolute offset of each group container.
  const positions: Record<string, XY> = {};
  const groupAbs: Record<string, XY> = {};
  const walk = (n: ElkNode, ox: number, oy: number) => {
    const ax = ox + (n.x ?? 0);
    const ay = oy + (n.y ?? 0);
    if (n.children && n.children.length) {
      if (n.id !== "root") groupAbs[n.id] = { x: ax, y: ay };
      for (const c of n.children) walk(c, ax, ay);
    } else if (ids.has(n.id)) {
      positions[n.id] = { x: Math.round(ax), y: Math.round(ay) };
    }
  };
  walk(res, 0, 0);

  // Stagger: nudge alternate layers sideways so stacked nodes zig-zag instead of
  // sitting in a straight column. Done per container (ungrouped set + each group)
  // so members shift relative to their own chain. Baked routes are skipped — the
  // edges fall back to live floating, which angles cleanly between the offsets.
  if (opts.stagger) {
    const step = opts.staggerStep;
    staggerLayers(positions, layoutNodes.filter((n) => !groupOf.has(n.id)).map((n) => n.id), opts.direction, step);
    for (const gid of clusterIds) staggerLayers(positions, clusterMembers.get(gid)!, opts.direction, step);
    return { positions, routes: {} };
  }

  // Edge routes → absolute. Offset = shared group's absolute pos (intra-group
  // edge) else origin (cross-group / ungrouped). Wherever ELK lists the edge,
  // its points are LCA-relative, and the LCA is exactly that shared group.
  const routes: Record<string, XY[]> = {};
  const collect = (n: ElkNode) => {
    for (const e of n.edges ?? []) {
      const sec = e.sections?.[0];
      if (!sec) continue;
      const sg = groupOf.get(e.sources[0]);
      const tg = groupOf.get(e.targets[0]);
      const off = sg && sg === tg ? (groupAbs[sg] ?? { x: 0, y: 0 }) : { x: 0, y: 0 };
      routes[e.id] = [sec.startPoint, ...(sec.bendPoints ?? []), sec.endPoint].map((p) => ({
        x: Math.round(p.x + off.x),
        y: Math.round(p.y + off.y),
      }));
    }
    for (const c of n.children ?? []) collect(c);
  };
  collect(res);

  return { positions, routes };
}

/**
 * Shift every other layer of a node set sideways (cross-axis), so a vertical
 * chain reads as a left/right zig-zag. Nodes are bucketed into layers by their
 * position along the flow axis (y for DOWN, x for RIGHT); odd layers move.
 */
function staggerLayers(
  positions: Record<string, XY>,
  memberIds: string[],
  direction: "DOWN" | "RIGHT",
): void {
  const primary: keyof XY = direction === "DOWN" ? "y" : "x";
  const cross: keyof XY = direction === "DOWN" ? "x" : "y";
  const present = memberIds.filter((id) => positions[id]);
  if (present.length < 2) return;

  const sorted = [...present].sort((a, b) => positions[a][primary] - positions[b][primary]);
  const TOL = 24; // same layer if the flow-axis gap is within this
  const layerOf: Record<string, number> = {};
  let layer = 0;
  let prev = positions[sorted[0]][primary];
  layerOf[sorted[0]] = 0;
  for (let i = 1; i < sorted.length; i++) {
    const p = positions[sorted[i]][primary];
    if (p - prev > TOL) layer++;
    layerOf[sorted[i]] = layer;
    prev = p;
  }
  for (const id of present) {
    if (layerOf[id] % 2 === 1) positions[id] = { ...positions[id], [cross]: positions[id][cross] + STAGGER_STEP };
  }
}
