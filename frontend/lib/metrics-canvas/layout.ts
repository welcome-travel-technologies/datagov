/**
 * Auto-layout ("Arrange") for the canvas — a Dagre layering, mirroring
 * `lib/lineage/layout.ts` `layoutAssetDagre`. Returns new top-left positions
 * keyed by node id; the editor writes them back and fits the view.
 *
 * When `groups` are supplied it runs Dagre as a *compound* graph so the members
 * of each group stay clustered and the group frames never overlap each other —
 * a plain layering interleaves grouped nodes and the dashed frames collide.
 * Annotation nodes (notes / text / sticky) and section frames (containers) keep
 * their hand-placed positions; only the connected metric nodes are arranged.
 */
import dagre from "dagre";
import type { RfEdge, RfNode } from "@/lib/metrics-canvas/serialize";
import type { CanvasGroup } from "@/lib/metrics-canvas/types";

export interface XY {
  x: number;
  y: number;
}

/** Notes and section frames are positioned by hand, not by the layout engine. */
function isLayoutNode(n: RfNode): boolean {
  return n.type !== "note" && n.type !== "container";
}

function sizeOf(n: RfNode): { width: number; height: number } {
  const w = (n.width as number | undefined) ?? (n.measured?.width as number | undefined) ?? 190;
  const h = (n.height as number | undefined) ?? (n.measured?.height as number | undefined) ?? 60;
  return { width: Math.max(40, w), height: Math.max(30, h) };
}

export function arrangeDagre(
  nodes: RfNode[],
  edges: RfEdge[],
  rankdir: "LR" | "TB" = "TB",
  groups: CanvasGroup[] = [],
): Record<string, XY> {
  const layoutNodes = nodes.filter(isLayoutNode);
  const ids = new Set(layoutNodes.map((n) => n.id));

  // Only groups with at least one arrangeable member become Dagre clusters.
  const clusters = groups
    .map((g) => ({ id: g.id, nodeIds: g.nodeIds.filter((id) => ids.has(id)) }))
    .filter((g) => g.nodeIds.length > 0);
  const compound = clusters.length > 0;

  const g = new dagre.graphlib.Graph({ compound });
  g.setGraph({ rankdir, nodesep: 48, ranksep: 96, marginx: 24, marginy: 24 });
  g.setDefaultEdgeLabel(() => ({}));

  for (const n of layoutNodes) {
    const { width, height } = sizeOf(n);
    g.setNode(n.id, { width, height });
  }

  if (compound) {
    const assigned = new Set<string>();
    for (const c of clusters) {
      g.setNode(c.id, {}); // cluster parent
      for (const childId of c.nodeIds) {
        if (assigned.has(childId)) continue; // a node belongs to one cluster only
        g.setParent(childId, c.id);
        assigned.add(childId);
      }
    }
  }

  const seen = new Set<string>();
  for (const e of edges) {
    if (e.source === e.target) continue;
    if (!ids.has(e.source) || !ids.has(e.target)) continue;
    const key = `${e.source}->${e.target}`;
    if (seen.has(key)) continue;
    seen.add(key);
    g.setEdge(e.source, e.target);
  }
  dagre.layout(g);

  const pos: Record<string, XY> = {};
  for (const id of ids) {
    const dn = g.node(id);
    if (dn && Number.isFinite(dn.x) && Number.isFinite(dn.y)) {
      pos[id] = { x: Math.round(dn.x - dn.width / 2), y: Math.round(dn.y - dn.height / 2) };
    }
  }
  return pos;
}
