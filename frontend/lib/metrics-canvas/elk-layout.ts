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

const NODE_SEP = 100; // gap between side-by-side nodes (within a layer)
const RANK_SEP = 190; // gap between layers (row to row)
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
  direction: "DOWN" | "RIGHT" = "DOWN",
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

  const graph: ElkNode = {
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": direction,
      "elk.edgeRouting": "ORTHOGONAL",
      "elk.hierarchyHandling": "INCLUDE_CHILDREN",
      "elk.layered.spacing.nodeNodeBetweenLayers": String(RANK_SEP),
      "elk.spacing.nodeNode": String(NODE_SEP),
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
