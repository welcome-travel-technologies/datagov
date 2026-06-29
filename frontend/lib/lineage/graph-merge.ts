/**
 * Merge an incoming ego-graph payload into the graph already loaded on the
 * canvas, deduping nodes by id and edges by (kind, source, target). Pure (no
 * React) so the lazy upstream/downstream "+" expansion is unit-testable.
 *
 * `newNodeIds` reports which node ids did NOT exist before the merge — the store
 * uses it to auto-position ONLY brand-new cards and preserve the user's existing
 * arrangement.
 */
import type { NetworkNode, NetworkLink } from "@/lib/api";

export interface MergeResult {
  nodes: NetworkNode[];
  links: NetworkLink[];
  /** Ids present in `incNodes` that were absent from `prevNodes`. */
  newNodeIds: Set<string>;
}

/** Stable key for an edge — kind matters so a `contains` and a `column` edge
 *  between the same two nodes are not collapsed into one. */
export function edgeKey(e: Pick<NetworkLink, "source" | "target" | "kind">): string {
  return `${e.kind ?? ""}|${e.source}->${e.target}`;
}

export function mergeGraph(
  prevNodes: NetworkNode[],
  prevLinks: NetworkLink[],
  incNodes: NetworkNode[],
  incLinks: NetworkLink[],
): MergeResult {
  const nodeById = new Map<string, NetworkNode>();
  for (const n of prevNodes) nodeById.set(n.id, n);

  const newNodeIds = new Set<string>();
  for (const n of incNodes) {
    if (!nodeById.has(n.id)) {
      newNodeIds.add(n.id);
      nodeById.set(n.id, n);
    } else {
      // Enrich an existing node with any fields the new payload filled in
      // (e.g. a column first seen as an edge endpoint, now with metadata).
      nodeById.set(n.id, { ...nodeById.get(n.id)!, ...n });
    }
  }

  const linkByKey = new Map<string, NetworkLink>();
  for (const e of prevLinks) linkByKey.set(edgeKey(e), e);
  for (const e of incLinks) {
    const k = edgeKey(e);
    if (!linkByKey.has(k)) linkByKey.set(k, e);
  }

  return {
    nodes: Array.from(nodeById.values()),
    links: Array.from(linkByKey.values()),
    newNodeIds,
  };
}
