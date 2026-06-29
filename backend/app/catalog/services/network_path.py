"""
Shortest-path search across the lineage graph (NetworkNode / NetworkEdge).

Used by:
  • catalog/views.py — `/api/network/path/` REST endpoint for the lineage UI

Keep this module thin: BFS over NetworkEdge with a per-step direction filter.
The TMDL-specific path search lives in `graph_paths.py` and is unrelated.

Path-search semantics (current behaviour):
  ``find_shortest_path`` returns *all* shortest paths of equal length — when
  two distinct routes exist at the same hop count (e.g. one via tables and one
  via columns), both are returned and the consumer renders the union as a DAG.

Possible extensions (not implemented):
  • Top-K shortest paths (Yen's algorithm) — useful when the user wants
    near-shortest alternatives at greater hop counts (e.g. 1 path of length 4
    plus 3 paths of length 5). Heavier and needs a list-style picker in the UI.
  • All simple paths up to ``max_depth`` — exhaustive enumeration. Risk of
    combinatorial blow-up on dense subgraphs (a measure with 50 columns can
    yield millions of paths) so it would need an aggressive output cap.
"""
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ..models import Item, NetworkEdge, NetworkNode


def _pb_nodes_outside_workspace(workspace_id: str) -> set:
    """Return the set of node_ids that are PowerBI items whose workspace_id is
    different from ``workspace_id``. Used to block path traversal across two
    workspaces of the same PowerBI source while still allowing paths to cross
    INTO non-PowerBI services (dbt) that share lineage edges via the bridge.

    PowerBI items with NULL/blank workspace_id are NOT blocked — they can act
    as cross-workspace bridges (e.g. shared datasets) and excluding them would
    sever legitimate paths.
    """
    if not workspace_id:
        return set()
    rows = (
        Item.objects
        .filter(item_type__startswith='PB_')
        .exclude(workspace_id__isnull=True)
        .exclude(workspace_id='')
        .exclude(workspace_id=workspace_id)
        .values_list('item_type', 'item_id')
    )
    return {f'{t}::{h}' for t, h in rows}


@dataclass
class PathNode:
    id: str
    label: str
    group: str


@dataclass
class NetworkPathResult:
    found: bool
    distance: int = 0
    # Union of all nodes across every shortest path (for vis.js DAG rendering).
    nodes: List[PathNode] = field(default_factory=list)
    # Union of all edges across every shortest path. Each edge keeps its DB
    # direction (source, target) so the UI can draw arrows correctly.
    edges: List[Tuple[str, str]] = field(default_factory=list)
    # Each individual shortest path as an ordered list of node_ids
    # (source → ... → target). When multiple paths share the same minimum hop
    # count, they all appear here.
    paths: List[List[str]] = field(default_factory=list)
    message: str = ""


_VALID_DIRECTIONS = ('both', 'upstream', 'downstream')

# Hard cap on number of distinct shortest paths returned to prevent runaway
# enumeration on highly-connected subgraphs (e.g. a hub node sitting between
# the source and target with 100 sibling routes).
_MAX_PATHS = 50

# Supported algorithms (BFS structure is the same; the difference is only how
# many reconstructed paths we return).
#   'shortest'      — return one shortest path (whichever the DFS finds first).
#   'all_shortest'  — return every distinct path of the minimum hop count.
_VALID_ALGORITHMS = ('shortest', 'all_shortest')


def _serialize_ids(ids: List[str]) -> List[PathNode]:
    """Hydrate a list of node_ids into PathNode dataclasses (preserving order),
    synthesizing placeholders for any ids not in NetworkNode (data drift)."""
    out: List[PathNode] = []
    seen = set()
    chunk_size = 900
    # Build a lookup so we can preserve the input order
    rows = {}
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i:i + chunk_size]
        for n in NetworkNode.objects.filter(node_id__in=chunk):
            rows[n.node_id] = n
    for nid in ids:
        if nid in seen:
            continue
        seen.add(nid)
        n = rows.get(nid)
        if n is not None:
            out.append(PathNode(id=n.node_id, label=n.name or n.node_id, group=n.group or "UNKNOWN"))
        else:
            group = nid.split("::", 1)[0] if "::" in nid else "UNKNOWN"
            out.append(PathNode(id=nid, label=nid, group=group))
    return out


def find_shortest_path(
    source_id: str,
    target_id: str,
    max_depth: int = 6,
    direction: str = 'both',
    algorithm: str = 'all_shortest',
    workspace_id: str = '',
) -> NetworkPathResult:
    """
    BFS the lineage graph between two nodes.

    Args:
        source_id: composite node_id (e.g. ``PB_MEASURE::<hash>``) of the start node.
        target_id: composite node_id of the end node.
        max_depth: max number of hops to search (clamped to 1..30).
        direction: 'both' (default), 'downstream' (only follow source→target),
                   or 'upstream' (only follow target→source).
        algorithm:
          ``'shortest'``     — return one shortest path (cheaper to reconstruct;
                               picks whichever route the DFS finds first when
                               several share the same minimum length).
          ``'all_shortest'`` — return every distinct path of the minimum hop
                               count (default). Capped at ``_MAX_PATHS``.

    Returns:
        NetworkPathResult — ``found=False`` with a message if no path exists
        within ``max_depth``. Otherwise the ``nodes`` / ``edges`` fields hold
        the union of all returned paths (a DAG ready to render) and ``paths``
        lists each individual path as an ordered list of node_ids.
    """
    if not source_id or not target_id:
        return NetworkPathResult(found=False, message="Both source and target node ids are required.")

    if direction not in _VALID_DIRECTIONS:
        direction = 'both'

    if algorithm not in _VALID_ALGORITHMS:
        algorithm = 'all_shortest'

    max_depth = max(1, min(int(max_depth or 6), 30))

    if source_id == target_id:
        return NetworkPathResult(
            found=True,
            distance=0,
            nodes=_serialize_ids([source_id]),
            edges=[],
            paths=[[source_id]],
            message="Source and target are the same node.",
        )

    # Workspace constraint: block PowerBI nodes from a different workspace so
    # paths can never cross between two workspaces of the same source. The
    # source/target are always exempt — the user explicitly chose them.
    blocked_pb_nodes = _pb_nodes_outside_workspace(workspace_id)
    blocked_pb_nodes.discard(source_id)
    blocked_pb_nodes.discard(target_id)

    # Layered BFS that records every shortest-distance predecessor.
    #   dist[node]  = depth at which the node was first reached (its shortest distance).
    #   preds[node] = list of (predecessor_id, db_source, db_target) — every
    #                 edge that reaches `node` at exactly `dist[node]`.
    # We process the graph level-by-level; for each edge we only consider the
    # endpoint whose `cur` side is at the current depth. A neighbour at the
    # next depth gets *appended* to its preds list (never deduped: distinct
    # predecessors mean distinct shortest paths).
    dist = {source_id: 0}
    preds: dict = {}
    frontier = [source_id]
    target_dist = None

    for d in range(max_depth):
        if not frontier:
            break

        next_frontier_set = set()
        next_frontier = []

        chunk_size = 900
        for i in range(0, len(frontier), chunk_size):
            chunk = frontier[i:i + chunk_size]

            edges_iter = []
            if direction in ('both', 'downstream'):
                edges_iter.extend(
                    ('fwd', e.source, e.target)
                    for e in NetworkEdge.objects.filter(source__in=chunk)
                )
            if direction in ('both', 'upstream'):
                edges_iter.extend(
                    ('rev', e.source, e.target)
                    for e in NetworkEdge.objects.filter(target__in=chunk)
                )

            for kind, e_src, e_tgt in edges_iter:
                if e_src == e_tgt:
                    continue
                if kind == 'fwd':
                    cur, nxt = e_src, e_tgt
                else:
                    cur, nxt = e_tgt, e_src
                # Only treat `cur` as part of *this* layer (queries on chunks
                # can yield edges whose `cur` side lives elsewhere).
                if dist.get(cur) != d:
                    continue
                # Skip neighbours that violate the workspace constraint.
                if nxt in blocked_pb_nodes:
                    continue
                new_d = d + 1
                existing = dist.get(nxt)
                if existing is None:
                    dist[nxt] = new_d
                    preds[nxt] = [(cur, e_src, e_tgt)]
                    if nxt == target_id:
                        target_dist = new_d
                    if nxt not in next_frontier_set:
                        next_frontier_set.add(nxt)
                        next_frontier.append(nxt)
                elif existing == new_d:
                    # Another edge reaching nxt at the same shortest distance —
                    # record this alternative predecessor.
                    preds[nxt].append((cur, e_src, e_tgt))

        # If the target was reached at the layer we just finished processing,
        # we have collected every shortest predecessor for it; stop expanding.
        if target_dist is not None:
            break
        frontier = next_frontier

    if target_id not in preds:
        return NetworkPathResult(
            found=False,
            message=(
                f"No path found between the two nodes within {max_depth} hops "
                f"(direction={direction})."
            ),
        )

    # DFS-reconstruct paths from preds. Because preds only lists
    # shortest-distance predecessors, every walk produced is a shortest path.
    # The cap depends on the algorithm: 1 for 'shortest', _MAX_PATHS otherwise.
    path_cap = 1 if algorithm == 'shortest' else _MAX_PATHS
    all_paths: List[List[str]] = []
    nodes_union: set = set()
    edges_union: set = set()
    capped = False

    def _walk(node, trace_nodes, trace_edges):
        nonlocal capped
        if capped:
            return
        if node == source_id:
            seq = [source_id] + list(reversed(trace_nodes))
            edges_seq = list(reversed(trace_edges))
            all_paths.append(seq)
            for n in seq:
                nodes_union.add(n)
            for e in edges_seq:
                edges_union.add(e)
            if len(all_paths) >= path_cap:
                capped = True
            return
        for pred, e_src, e_tgt in preds.get(node, []):
            if capped:
                return
            _walk(pred, trace_nodes + [node], trace_edges + [(e_src, e_tgt)])

    _walk(target_id, [], [])

    distance = target_dist or 0
    if algorithm == 'shortest':
        msg = f"Found shortest path of {distance} hop(s)."
    else:
        msg = (
            f"Found {len(all_paths)} shortest path(s) of {distance} hop(s)."
            + (f" Truncated to first {_MAX_PATHS}." if capped else "")
        )

    # Preserve the union order so source/target stay first/last for layouts
    # that respect insertion order.
    union_ids = [source_id] + [n for n in nodes_union if n not in (source_id, target_id)] + [target_id]

    return NetworkPathResult(
        found=True,
        distance=distance,
        nodes=_serialize_ids(union_ids),
        edges=list(edges_union),
        paths=all_paths,
        message=msg,
    )


@dataclass
class ReachableNode:
    id: str
    label: str
    group: str
    distance: int


@dataclass
class ReachableResult:
    nodes: List[ReachableNode] = field(default_factory=list)
    truncated: bool = False
    message: str = ""


# Caps for the reachable-nodes search. Keeps dropdown population fast even when
# the start node is a hub with thousands of upstream contributors.
_REACHABLE_MAX_DEPTH = 15
_REACHABLE_MAX_NODES = 500


def find_reachable_nodes(
    start_id: str,
    direction: str = 'upstream',
    workspace_id: str = '',
) -> ReachableResult:
    """
    BFS from ``start_id`` and return every node reachable in the given direction.

    Used to populate the "Start" dropdown of the Path tab once the user has
    picked an "End" — we only offer real candidate starting points so the user
    can never pick a Start that has no path to End.

    Args:
        start_id: composite node_id of the End node (the BFS origin).
        direction: 'upstream' (default — follow target→source edges, i.e. what
                   feeds the End node) or 'downstream' (follow source→target).
        workspace_id: optional PowerBI workspace constraint; nodes outside this
                      workspace are excluded (same rule as ``find_shortest_path``).

    Results are sorted by distance descending (farthest first), so the deepest
    upstream sources surface at the top of the dropdown — matching the natural
    "track back as far as we can" reading.
    """
    if not start_id:
        return ReachableResult(message="A start node id is required.")

    if direction not in ('upstream', 'downstream'):
        direction = 'upstream'

    blocked_pb_nodes = _pb_nodes_outside_workspace(workspace_id)
    blocked_pb_nodes.discard(start_id)

    dist = {start_id: 0}
    frontier = [start_id]
    truncated = False

    for d in range(_REACHABLE_MAX_DEPTH):
        if not frontier or len(dist) >= _REACHABLE_MAX_NODES:
            break

        next_frontier_set = set()
        next_frontier = []

        chunk_size = 900
        for i in range(0, len(frontier), chunk_size):
            chunk = frontier[i:i + chunk_size]

            if direction == 'upstream':
                # Follow target→source: nodes that feed the current frontier.
                edges = NetworkEdge.objects.filter(target__in=chunk).values_list('source', 'target')
                neighbours = ((tgt, src) for src, tgt in edges)  # (cur, nxt)
            else:
                edges = NetworkEdge.objects.filter(source__in=chunk).values_list('source', 'target')
                neighbours = ((src, tgt) for src, tgt in edges)

            for cur, nxt in neighbours:
                if cur == nxt:
                    continue
                if dist.get(cur) != d:
                    continue
                if nxt in dist:
                    continue
                if nxt in blocked_pb_nodes:
                    continue
                dist[nxt] = d + 1
                if nxt not in next_frontier_set:
                    next_frontier_set.add(nxt)
                    next_frontier.append(nxt)
                if len(dist) >= _REACHABLE_MAX_NODES:
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            break
        frontier = next_frontier

    # Drop the start node from the result — it can't be its own start point.
    reachable_ids = [nid for nid in dist if nid != start_id]
    if not reachable_ids:
        return ReachableResult(message="No reachable nodes from this entity.")

    # Hydrate labels/groups in chunks.
    rows = {}
    chunk_size = 900
    for i in range(0, len(reachable_ids), chunk_size):
        chunk = reachable_ids[i:i + chunk_size]
        for n in NetworkNode.objects.filter(node_id__in=chunk):
            rows[n.node_id] = n

    out: List[ReachableNode] = []
    for nid in reachable_ids:
        n = rows.get(nid)
        if n is not None:
            label = n.name or n.node_id
            group = n.group or "UNKNOWN"
        else:
            label = nid
            group = nid.split("::", 1)[0] if "::" in nid else "UNKNOWN"
        out.append(ReachableNode(id=nid, label=label, group=group, distance=dist[nid]))

    # Farthest first — matches "trace back as far as possible".
    out.sort(key=lambda r: (-r.distance, r.group, r.label.lower()))

    msg = f"Found {len(out)} reachable node(s)."
    if truncated:
        msg += f" Truncated at {_REACHABLE_MAX_NODES} — refine your selection or filter by type."

    return ReachableResult(nodes=out, truncated=truncated, message=msg)


def resolve_node_id_by_name(name: str, group: Optional[str] = None) -> List[NetworkNode]:
    """
    Resolve a free-form name to one or more NetworkNode rows.

    Useful for chatbot tools where the user supplies a human name like
    "Driver Availability" rather than a composite ``PB_MEASURE::<hash>`` id.
    Returns at most 25 candidates so the caller can disambiguate.
    """
    from django.db.models import Q
    if not name:
        return []
    # Exact composite-id match first (cheapest, unambiguous)
    exact = list(NetworkNode.objects.filter(node_id=name))
    if exact:
        return exact
    qs = NetworkNode.objects.filter(Q(name__iexact=name) | Q(name__icontains=name))
    if group:
        qs = qs.filter(group=group)
    return list(qs[:25])
