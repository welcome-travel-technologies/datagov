"""
On-demand graph traversal for the chatbot.

Answers "is dimension column D usable with measure M?" by finding a path of
TMDL relationship edges (PB_COLUMN -> PB_COLUMN) between M's home table and
D's parent table, scoped to a single dataset.

No closure table is materialised: each call runs a bounded BFS on the
dataset-scoped relationship subgraph. On Postgres the BFS is a recursive
CTE; on SQLite (used in tests) it falls back to an in-memory traversal.
"""

from dataclasses import dataclass, field
from typing import Optional

from django.db import connection

from ..models import Item, NetworkEdge


PB_TABLE = 'PB_TABLE'
PB_COLUMN = 'PB_COLUMN'
DEFAULT_MAX_DEPTH = 5


@dataclass
class PathHop:
    from_column: str
    to_column: str
    from_label: str
    to_label: str
    cardinality: Optional[str] = None
    other_cardinality: Optional[str] = None
    is_active: Optional[bool] = None
    cross_filter: Optional[str] = None


@dataclass
class PathResult:
    connected: bool
    distance: int = 0
    path_node_ids: list = field(default_factory=list)
    path_labels: list = field(default_factory=list)
    cardinality_chain: list = field(default_factory=list)
    inactive_hops: list = field(default_factory=list)
    reason: str = ''


def _hash_of(node_id: str) -> str:
    return node_id.split('::', 1)[1] if '::' in node_id else node_id


def _table_for_measure(measure_node_id: str) -> Optional[str]:
    edge = (
        NetworkEdge.objects
        .filter(target=measure_node_id, source__startswith=f'{PB_TABLE}::')
        .first()
    )
    return edge.source if edge else None


def _table_for_column(column_node_id: str) -> Optional[str]:
    edge = (
        NetworkEdge.objects
        .filter(target=column_node_id, source__startswith=f'{PB_TABLE}::')
        .first()
    )
    return edge.source if edge else None


def _columns_of_table(table_node_id: str, dataset_id: str) -> list:
    column_node_ids = list(
        NetworkEdge.objects
        .filter(source=table_node_id, target__startswith=f'{PB_COLUMN}::')
        .values_list('target', flat=True)
    )
    if not column_node_ids:
        return []
    item_ids = [_hash_of(c) for c in column_node_ids]
    valid = set(
        Item.objects
        .filter(item_id__in=item_ids, dataset_id=dataset_id)
        .values_list('item_id', flat=True)
    )
    return [c for c in column_node_ids if _hash_of(c) in valid]


def find_relationship_path(
    measure_node_id: str,
    dim_column_node_id: str,
    dataset_id: str,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> PathResult:
    """
    Find the shortest TMDL-relationship path connecting a measure's home table
    to a dimension column's parent table within `dataset_id`.

    Returns a PathResult. Power BI ignores inactive relationships unless DAX
    invokes USERELATIONSHIP, so callers should refuse live DAX (or warn) when
    `inactive_hops` is non-empty.
    """
    if not measure_node_id or not dim_column_node_id or not dataset_id:
        return PathResult(False, reason='Missing required identifiers.')

    home_table = _table_for_measure(measure_node_id)
    if not home_table:
        return PathResult(False, reason=f'Measure {measure_node_id} has no home-table edge.')

    dim_table = _table_for_column(dim_column_node_id)
    if not dim_table:
        return PathResult(False, reason=f'Dimension {dim_column_node_id} has no parent-table edge.')

    if home_table == dim_table:
        return PathResult(
            connected=True,
            distance=0,
            path_node_ids=[dim_column_node_id],
            path_labels=_label_columns([dim_column_node_id]),
            reason='Dimension column lives on the measure home table; no relationship hop needed.',
        )

    target_columns = set(_columns_of_table(dim_table, dataset_id))
    if not target_columns:
        return PathResult(False, reason='Dimension table has no columns scoped to this dataset.')
    start_columns = _columns_of_table(home_table, dataset_id)
    if not start_columns:
        return PathResult(False, reason='Measure home table has no columns scoped to this dataset.')

    path = _bfs(start_columns, target_columns, dataset_id, max_depth)
    if not path:
        return PathResult(
            False,
            reason=(
                f'No TMDL relationship path connects the measure home table to '
                f'the dimension table within {max_depth} hops.'
            ),
        )
    return _build_result(path)


def _bfs(start_columns, target_columns, dataset_id, max_depth):
    if connection.vendor == 'postgresql':
        return _bfs_postgres(start_columns, target_columns, dataset_id, max_depth)
    return _bfs_python(start_columns, target_columns, dataset_id, max_depth)


def _bfs_postgres(start_columns, target_columns, dataset_id, max_depth):
    """Recursive CTE: undirected walk on PB_COLUMN -> PB_COLUMN edges scoped
    to columns whose Item.dataset_id matches. Cycle-guarded via path array.
    Returns the list of node_ids of the shortest path, or [] if none."""
    sql = """
    WITH RECURSIVE
      ds_columns AS (
        SELECT 'PB_COLUMN::' || item_id AS node_id
        FROM   catalog_item
        WHERE  item_type = 'PB_COLUMN' AND dataset_id = %(dataset_id)s
      ),
      rel_edges AS (
        SELECT e.source, e.target
        FROM   catalog_networkedge e
        JOIN   ds_columns sc ON sc.node_id = e.source
        JOIN   ds_columns tc ON tc.node_id = e.target
        WHERE  e.source LIKE 'PB_COLUMN::%%'
          AND  e.target LIKE 'PB_COLUMN::%%'
      ),
      walk AS (
        SELECT  c::text AS node, 0 AS depth, ARRAY[c::text] AS path
        FROM    unnest(%(start_cols)s::text[]) AS c
        UNION ALL
        SELECT  CASE WHEN e.source = w.node THEN e.target ELSE e.source END,
                w.depth + 1,
                w.path || (CASE WHEN e.source = w.node THEN e.target ELSE e.source END)
        FROM    walk w
        JOIN    rel_edges e
                ON  (e.source = w.node OR e.target = w.node)
        WHERE   w.depth < %(max_depth)s
          AND   NOT (
                  CASE WHEN e.source = w.node THEN e.target ELSE e.source END
                  = ANY(w.path)
                )
      )
    SELECT path
    FROM   walk
    WHERE  node = ANY(%(target_cols)s::text[])
    ORDER  BY depth ASC
    LIMIT  1;
    """
    with connection.cursor() as cur:
        cur.execute(sql, {
            'dataset_id': dataset_id,
            'start_cols': list(start_columns),
            'target_cols': list(target_columns),
            'max_depth': max_depth,
        })
        row = cur.fetchone()
    return list(row[0]) if row else []


def _bfs_python(start_columns, target_columns, dataset_id, max_depth):
    dataset_column_ids = {
        f'PB_COLUMN::{x}' for x in Item.objects.filter(
            item_type='PB_COLUMN', dataset_id=dataset_id,
        ).values_list('item_id', flat=True)
    }
    if not dataset_column_ids:
        return []
    edges = NetworkEdge.objects.filter(
        source__in=dataset_column_ids, target__in=dataset_column_ids,
    ).values_list('source', 'target')
    adj: dict[str, set[str]] = {}
    for s, t in edges:
        adj.setdefault(s, set()).add(t)
        adj.setdefault(t, set()).add(s)

    target_set = set(target_columns)
    direct = [c for c in start_columns if c in target_set]
    if direct:
        return [direct[0]]

    parent: dict[str, Optional[str]] = {c: None for c in start_columns}
    frontier = list(start_columns)
    for _ in range(max_depth):
        next_frontier = []
        for node in frontier:
            for nb in adj.get(node, ()):
                if nb in parent:
                    continue
                parent[nb] = node
                if nb in target_set:
                    path = [nb]
                    while parent[path[-1]] is not None:
                        path.append(parent[path[-1]])
                    return list(reversed(path))
                next_frontier.append(nb)
        if not next_frontier:
            break
        frontier = next_frontier
    return []


def _label_columns(node_ids: list) -> list:
    item_ids = [_hash_of(n) for n in node_ids]
    by_id = {i.item_id: i for i in Item.objects.filter(item_id__in=item_ids)}
    out = []
    for n in node_ids:
        item = by_id.get(_hash_of(n))
        if item:
            out.append(f"'{item.table_name}'[{item.item_name}]")
        else:
            out.append(n)
    return out


def _build_result(path_node_ids: list) -> PathResult:
    item_ids = [_hash_of(n) for n in path_node_ids]
    by_id = {i.item_id: i for i in Item.objects.filter(item_id__in=item_ids)}
    labels = _label_columns(path_node_ids)

    hops: list[PathHop] = []
    inactive: list[PathHop] = []
    for i in range(len(path_node_ids) - 1):
        a_item = by_id.get(_hash_of(path_node_ids[i]))
        b_item = by_id.get(_hash_of(path_node_ids[i + 1]))
        rels = (a_item.relationships_json or []) if a_item else []
        match = None
        if a_item and b_item:
            for r in rels:
                if (r.get('other_table') == b_item.table_name
                        and r.get('other_column') == b_item.item_name):
                    match = r
                    break
        hop = PathHop(
            from_column=path_node_ids[i],
            to_column=path_node_ids[i + 1],
            from_label=labels[i],
            to_label=labels[i + 1],
            cardinality=(match or {}).get('cardinality'),
            other_cardinality=(match or {}).get('other_cardinality'),
            is_active=(match or {}).get('is_active'),
            cross_filter=(match or {}).get('cross_filter'),
        )
        hops.append(hop)
        if match and match.get('is_active') is False:
            inactive.append(hop)

    return PathResult(
        connected=True,
        distance=len(path_node_ids) - 1,
        path_node_ids=path_node_ids,
        path_labels=labels,
        cardinality_chain=hops,
        inactive_hops=inactive,
        reason='Path found.',
    )
