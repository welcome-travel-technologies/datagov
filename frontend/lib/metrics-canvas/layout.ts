/**
 * Auto-layout ("Arrange") for the canvas — a Dagre layering, mirroring
 * `lib/lineage/layout.ts` `layoutAssetDagre`. Returns new top-left positions
 * keyed by node id; the editor writes them back and fits the view.
 *
 * Groups are laid out *nested* rather than as Dagre "compound" clusters: each
 * group is arranged on its own, then treated as a single sized super-node and
 * laid out alongside the ungrouped nodes; finally each group's internal layout
 * is dropped into its allotted box. Because every group occupies one Dagre slot
 * it gets Dagre's normal node separation, so the dashed group frames can never
 * overlap — Dagre's own compound mode lets cluster members interleave and the
 * frames collide. Annotation nodes (notes / text / sticky) and section frames
 * (containers) keep their hand-placed positions; only connected metric nodes are
 * arranged.
 */
import dagre from "dagre";
import type { RfEdge, RfNode } from "@/lib/metrics-canvas/serialize";
import type { CanvasGroup } from "@/lib/metrics-canvas/types";

export interface XY {
  x: number;
  y: number;
}

// Group frame geometry — mirrors GroupsOverlay so the dashed frames we space
// apart here are exactly the ones drawn on screen.
const FRAME_PAD = 16; // GroupsOverlay PAD
const FRAME_LABEL = 18; // GroupsOverlay LABEL_H
const GROUP_GAP = 24; // clear space kept between adjacent group frames

const NODE_SEP = 48;
const RANK_SEP = 96;
const MARGIN = 24;

/** Inset of group members from their super-node box. The frame (PAD beyond the
 *  members, plus the label band on top) then sits GROUP_GAP/2 inside the box, so
 *  adjacent boxes — separated by Dagre — leave the frames clearly apart. */
const INSET_X = FRAME_PAD + GROUP_GAP / 2;
const INSET_TOP = FRAME_PAD + FRAME_LABEL + GROUP_GAP / 2;
const INSET_BOTTOM = FRAME_PAD + GROUP_GAP / 2;

/** Notes and section frames are positioned by hand, not by the layout engine. */
function isLayoutNode(n: RfNode): boolean {
  return n.type !== "note" && n.type !== "container";
}

function sizeOf(n: RfNode): { width: number; height: number } {
  const w = (n.width as number | undefined) ?? (n.measured?.width as number | undefined) ?? 190;
  const h = (n.height as number | undefined) ?? (n.measured?.height as number | undefined) ?? 60;
  return { width: Math.max(40, w), height: Math.max(30, h) };
}

/**
 * Flat Dagre layering of a node set with the supplied edges. Returns top-left
 * positions normalized so the content's top-left sits at (0, 0), plus the
 * overall content size. Self-loops and edges that dangle outside the set are
 * ignored. Reused for the no-group path and for each group's internal layout.
 */
function flatLayout(
  nodes: RfNode[],
  edges: RfEdge[],
  rankdir: "LR" | "TB",
): { pos: Record<string, XY>; width: number; height: number } {
  const ids = new Set(nodes.map((n) => n.id));
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir, nodesep: NODE_SEP, ranksep: RANK_SEP, marginx: MARGIN, marginy: MARGIN });
  g.setDefaultEdgeLabel(() => ({}));

  for (const n of nodes) {
    const { width, height } = sizeOf(n);
    g.setNode(n.id, { width, height });
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

  const raw: Record<string, { x: number; y: number }> = {};
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const id of ids) {
    const dn = g.node(id);
    if (!dn || !Number.isFinite(dn.x) || !Number.isFinite(dn.y)) continue;
    const x = dn.x - dn.width / 2;
    const y = dn.y - dn.height / 2;
    raw[id] = { x, y };
    minX = Math.min(minX, x);
    minY = Math.min(minY, y);
    maxX = Math.max(maxX, x + dn.width);
    maxY = Math.max(maxY, y + dn.height);
  }
  if (!Number.isFinite(minX)) {
    minX = 0; minY = 0; maxX = 0; maxY = 0;
  }

  const pos: Record<string, XY> = {};
  for (const id of Object.keys(raw)) {
    pos[id] = { x: Math.round(raw[id].x - minX), y: Math.round(raw[id].y - minY) };
  }
  return { pos, width: maxX - minX, height: maxY - minY };
}

export function arrangeDagre(
  nodes: RfNode[],
  edges: RfEdge[],
  rankdir: "LR" | "TB" = "TB",
  groups: CanvasGroup[] = [],
): Record<string, XY> {
  const layoutNodes = nodes.filter(isLayoutNode);
  const ids = new Set(layoutNodes.map((n) => n.id));
  const byId = new Map(layoutNodes.map((n) => [n.id, n]));

  // Assign each arrangeable node to at most one group — the first that claims
  // it — so a node never lands in two boxes at once.
  const groupOf = new Map<string, string>();
  const clusterIds: string[] = [];
  const clusterMembers = new Map<string, string[]>();
  for (const grp of groups) {
    const members = grp.nodeIds.filter((id) => ids.has(id) && !groupOf.has(id));
    if (!members.length) continue;
    clusterIds.push(grp.id);
    clusterMembers.set(grp.id, members);
    for (const m of members) groupOf.set(m, grp.id);
  }

  // No groups → a single flat layering.
  if (!clusterIds.length) {
    return flatLayout(layoutNodes, edges, rankdir).pos;
  }

  // 1) Lay out each group internally (its members + the edges between them),
  //    and size its super-node to the resulting frame.
  const innerPos = new Map<string, Record<string, XY>>();
  const groupSize = new Map<string, { width: number; height: number }>();
  for (const gid of clusterIds) {
    const memberNodes = clusterMembers.get(gid)!.map((id) => byId.get(id)!).filter(Boolean);
    const memberIds = new Set(memberNodes.map((n) => n.id));
    const intra = edges.filter((e) => memberIds.has(e.source) && memberIds.has(e.target));
    const f = flatLayout(memberNodes, intra, rankdir);
    innerPos.set(gid, f.pos);
    groupSize.set(gid, {
      width: f.width + INSET_X * 2,
      height: f.height + INSET_TOP + INSET_BOTTOM,
    });
  }

  // 2) Super-graph: one node per group (sized to its frame) plus each ungrouped
  //    node. Edges are collapsed onto their endpoints' representatives (a group
  //    id for members, the node id otherwise); intra-group and self edges drop.
  const ungrouped = layoutNodes.filter((n) => !groupOf.has(n.id));
  const superG = new dagre.graphlib.Graph();
  superG.setGraph({ rankdir, nodesep: NODE_SEP, ranksep: RANK_SEP, marginx: MARGIN, marginy: MARGIN });
  superG.setDefaultEdgeLabel(() => ({}));

  for (const gid of clusterIds) {
    const s = groupSize.get(gid)!;
    superG.setNode(gid, { width: s.width, height: s.height });
  }
  for (const n of ungrouped) {
    const { width, height } = sizeOf(n);
    superG.setNode(n.id, { width, height });
  }

  const rep = (id: string) => groupOf.get(id) ?? id;
  const seen = new Set<string>();
  for (const e of edges) {
    if (!ids.has(e.source) || !ids.has(e.target)) continue;
    const a = rep(e.source);
    const b = rep(e.target);
    if (a === b) continue; // self-loop or wholly inside one group
    const key = `${a}->${b}`;
    if (seen.has(key)) continue;
    seen.add(key);
    superG.setEdge(a, b);
  }
  dagre.layout(superG);

  // 3) Compose: ungrouped nodes take their super-node slot; group members are
  //    offset by their group box's top-left plus the frame inset.
  const pos: Record<string, XY> = {};
  for (const n of ungrouped) {
    const dn = superG.node(n.id);
    if (dn && Number.isFinite(dn.x) && Number.isFinite(dn.y)) {
      pos[n.id] = { x: Math.round(dn.x - dn.width / 2), y: Math.round(dn.y - dn.height / 2) };
    }
  }
  for (const gid of clusterIds) {
    const dn = superG.node(gid);
    if (!dn || !Number.isFinite(dn.x) || !Number.isFinite(dn.y)) continue;
    const topLeftX = dn.x - dn.width / 2;
    const topLeftY = dn.y - dn.height / 2;
    const local = innerPos.get(gid)!;
    for (const [id, p] of Object.entries(local)) {
      pos[id] = {
        x: Math.round(topLeftX + INSET_X + p.x),
        y: Math.round(topLeftY + INSET_TOP + p.y),
      };
    }
  }
  return pos;
}
