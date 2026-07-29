"""
Build cross-tool bridge edges (dbt to PowerBI) at table and column level.

This module owns the SQL I/O. Matching decisions stay in ``bridge_matching``
so they remain pure Python and unit-testable.
"""
from typing import Callable, Optional

from django.core.management.base import CommandError

from .bridge_matching import (
    DbtModelKey,
    PbiTableKey,
    iter_column_pairs,
    iter_table_pairs,
)
from .network_classify import classify_edge
from .load_scope import assert_incoming_identities_available


_TABLE_BRIDGE_KIND, _TABLE_BRIDGE_LEVEL = classify_edge(
    'DBT_MODEL', 'PB_TABLE',
)
_COLUMN_BRIDGE_KIND, _COLUMN_BRIDGE_LEVEL = classify_edge(
    'DBT_COLUMN', 'PB_COLUMN',
)


def _noop(_msg: str) -> None:  # pragma: no cover
    pass


class BridgeTenantCollision(RuntimeError):
    """A globally unique graph key is already owned by another organization."""


def _validated_organization_id(cursor, organization_id) -> int:
    if isinstance(organization_id, bool):
        raise ValueError('organization_id must be a positive integer.')
    try:
        organization_id = int(organization_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            'organization_id must be a positive integer.'
        ) from exc
    if organization_id <= 0:
        raise ValueError('organization_id must be a positive integer.')

    cursor.execute(
        'SELECT 1 FROM catalog_organization WHERE id = %s;',
        [organization_id],
    )
    if cursor.fetchone() is None:
        raise ValueError(
            f'Organization {organization_id} does not exist.'
        )
    return organization_id


def _delete_existing_bridges(cursor, organization_id: int) -> None:
    """Delete only bridge rows owned by the requested organization."""
    cursor.execute(r"""
        DELETE FROM catalog_networkedge
        WHERE organization_id = %s
          AND (
                (source LIKE 'DBT\_%%' AND target NOT LIKE 'DBT\_%%')
             OR (target LIKE 'DBT\_%%' AND source NOT LIKE 'DBT\_%%')
          );
    """, [organization_id])
    # Historical column bridges were not consistently tagged. Retain the
    # endpoint-based cleanup, with exact-tenant predicates on both the edge and
    # the Item subqueries.
    cursor.execute("""
        DELETE FROM catalog_networkedge
        WHERE organization_id = %s
          AND source IN (
              SELECT 'DBT_COLUMN::' || item_id FROM catalog_item
              WHERE organization_id = %s
                AND item_type = 'DBT_COLUMN'
                AND deleted = FALSE
          )
          AND target IN (
              SELECT 'PB_COLUMN::' || item_id FROM catalog_item
              WHERE organization_id = %s
                AND item_type = 'PB_COLUMN'
                AND deleted = FALSE
          );
    """, [organization_id, organization_id, organization_id])


def _preflight_graph_keys(
    cursor,
    organization_id: int,
    planned_nodes: dict,
    planned_edges: dict,
) -> None:
    """Reject foreign owners of legacy global graph keys before cleanup."""
    try:
        assert_incoming_identities_available(
            organization_id=organization_id,
            source_id=None,
            node_ids=planned_nodes,
            edges=planned_edges,
        )
    except CommandError as exc:
        raise BridgeTenantCollision(
            f'Cannot rebuild cross-tool bridges: {exc}'
        ) from exc


def _ensure_node(
    cursor,
    organization_id: int,
    node_id: str,
    name: str,
    group: str,
) -> None:
    # The ownership read after INSERT closes the concurrent-insert race left
    # between preflight and write.
    cursor.execute("""
        INSERT INTO catalog_networknode (
            node_id, name, "group", organization_id
        )
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (node_id) DO NOTHING;
    """, [node_id, name, group, organization_id])
    cursor.execute("""
        SELECT organization_id
        FROM catalog_networknode
        WHERE node_id = %s;
    """, [node_id])
    row = cursor.fetchone()
    if row is None or row[0] != organization_id:
        owner_id = row[0] if row else None
        raise BridgeTenantCollision(
            f'Graph node {node_id!r} was claimed by organization '
            f'{owner_id!r} during bridge rebuild.'
        )


def _write_edge(
    cursor,
    organization_id: int,
    source: str,
    target: str,
    reason: str,
    kind: str,
    level: str,
    lineage_type,
) -> None:
    # Never let a global-key conflict update another tenant's row. Insert first,
    # then update only the exact tenant. A zero-row update means a concurrent
    # foreign claim and raises inside the caller's atomic transaction.
    cursor.execute("""
        INSERT INTO catalog_networkedge (
            source, target, organization_id, bridge_reason,
            kind, level, lineage_type
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (source, target) DO NOTHING;
    """, [
        source, target, organization_id, reason, kind, level, lineage_type,
    ])
    cursor.execute("""
        UPDATE catalog_networkedge
        SET bridge_reason = %s,
            kind = %s,
            level = %s,
            lineage_type = %s
        WHERE source = %s
          AND target = %s
          AND organization_id = %s;
    """, [
        reason, kind, level, lineage_type,
        source, target, organization_id,
    ])
    if cursor.rowcount != 1:
        cursor.execute("""
            SELECT organization_id
            FROM catalog_networkedge
            WHERE source = %s AND target = %s;
        """, [source, target])
        row = cursor.fetchone()
        owner_id = row[0] if row else None
        raise BridgeTenantCollision(
            f'Graph edge ({source!r}, {target!r}) was claimed by '
            f'organization {owner_id!r} during bridge rebuild.'
        )


def build_cross_tool_bridges(
    cursor,
    organization_id: int,
    write: Optional[Callable[[str], None]] = None,
) -> dict:
    """Replace one organization's dbt-to-PBI bridge edges.

    ``organization_id`` is mandatory. The graph schema still uses global node
    and edge keys, so this function rejects any planned key already owned by a
    different organization before it deletes the caller's existing bridges.
    The caller manages the surrounding transaction.
    """
    write = write or _noop
    organization_id = _validated_organization_id(cursor, organization_id)
    write('Building cross-tool bridge edges (dbt to PowerBI)...')

    # Load candidates from the exact tenant. Identical names or FQNs in another
    # organization must never influence matching.
    cursor.execute("""
        SELECT item_id, item_name, database_name, schema_name, alias,
               table_name, dataset_id
        FROM catalog_item
        WHERE organization_id = %s
          AND service = 'dbt'
          AND item_type = 'DBT_MODEL'
          AND table_name IS NOT NULL
          AND deleted = FALSE;
    """, [organization_id])
    dbt_rows = cursor.fetchall()
    dbt_models = [
        DbtModelKey(
            item_id=item_id,
            item_name=item_name or '',
            database=database_name,
            schema=schema_name,
            alias=alias,
            table_name=table_name,
        )
        for (
            item_id, item_name, database_name, schema_name, alias,
            table_name, _dataset_id,
        ) in dbt_rows
    ]
    dbt_dataset_id_by_item = {
        item_id: dataset_id
        for (
            item_id, _name, _database, _schema, _alias, _table,
            dataset_id,
        ) in dbt_rows
    }
    dbt_name_by_item = {
        item_id: name
        for (
            item_id, name, _database, _schema, _alias, _table,
            _dataset_id,
        ) in dbt_rows
    }

    cursor.execute("""
        SELECT item_id, item_name, bq_project, bq_schema, bq_source_name
        FROM catalog_item
        WHERE organization_id = %s
          AND (service IS NULL OR service != 'dbt')
          AND item_type = 'PB_TABLE'
          AND item_name IS NOT NULL
          AND deleted = FALSE;
    """, [organization_id])
    pbi_rows = cursor.fetchall()
    pbi_tables = [
        PbiTableKey(
            item_id=item_id,
            item_name=item_name or '',
            bq_project=bq_project,
            bq_schema=bq_schema,
            bq_source_name=bq_source_name,
        )
        for (
            item_id, item_name, bq_project, bq_schema, bq_source_name,
        ) in pbi_rows
    ]
    pbi_name_by_item = {
        item_id: name
        for item_id, name, *_rest in pbi_rows
    }

    planned_nodes = {}
    planned_edges = {}
    by_reason = {}

    for match in iter_table_pairs(pbi_tables, dbt_models):
        dbt_id = match.dbt_item_id
        pbi_id = match.pbi_item_id
        reason = match.reason
        by_reason[reason] = by_reason.get(reason, 0) + 1

        src_node = f'DBT_MODEL::{dbt_id}'
        tgt_node = f'PB_TABLE::{pbi_id}'
        planned_nodes[src_node] = (
            dbt_name_by_item.get(dbt_id) or '', 'DBT_MODEL',
        )
        planned_nodes[tgt_node] = (
            pbi_name_by_item.get(pbi_id) or '', 'PB_TABLE',
        )
        planned_edges[(src_node, tgt_node)] = (
            reason, _TABLE_BRIDGE_KIND, _TABLE_BRIDGE_LEVEL, None,
        )

        cursor.execute("""
            SELECT item_id, item_name
            FROM catalog_item
            WHERE organization_id = %s
              AND item_type = 'DBT_COLUMN'
              AND dataset_id = %s
              AND deleted = FALSE;
        """, [
            organization_id, dbt_dataset_id_by_item.get(dbt_id),
        ])
        dbt_cols = cursor.fetchall()
        if not dbt_cols:
            continue

        cursor.execute("""
            SELECT item_id, item_name
            FROM catalog_item
            WHERE organization_id = %s
              AND item_type = 'PB_COLUMN'
              AND table_name = %s
              AND deleted = FALSE;
        """, [organization_id, pbi_name_by_item.get(pbi_id)])
        pbi_cols = cursor.fetchall()
        if not pbi_cols:
            continue

        dbt_col_name_by_id = {
            column_id: name for column_id, name in dbt_cols
        }
        pbi_col_name_by_id = {
            column_id: name for column_id, name in pbi_cols
        }
        for dbt_col_id, pbi_col_id in iter_column_pairs(
            pbi_cols, dbt_cols,
        ):
            col_src = f'DBT_COLUMN::{dbt_col_id}'
            col_tgt = f'PB_COLUMN::{pbi_col_id}'
            planned_nodes[col_src] = (
                dbt_col_name_by_id.get(dbt_col_id) or '', 'DBT_COLUMN',
            )
            planned_nodes[col_tgt] = (
                pbi_col_name_by_id.get(pbi_col_id) or '', 'PB_COLUMN',
            )
            planned_edges[(col_src, col_tgt)] = (
                reason, _COLUMN_BRIDGE_KIND, _COLUMN_BRIDGE_LEVEL,
                'pass-through',
            )

    # Preflight every planned global key before the first destructive write.
    _preflight_graph_keys(
        cursor, organization_id, planned_nodes, planned_edges,
    )
    _delete_existing_bridges(cursor, organization_id)

    for node_id, (name, group) in sorted(planned_nodes.items()):
        _ensure_node(
            cursor, organization_id, node_id, name, group,
        )
    for (source, target), edge in sorted(planned_edges.items()):
        reason, kind, level, lineage_type = edge
        _write_edge(
            cursor, organization_id, source, target,
            reason, kind, level, lineage_type,
        )

    table_bridges = sum(
        1
        for source, target in planned_edges
        if source.startswith('DBT_MODEL::')
        and target.startswith('PB_TABLE::')
    )
    column_bridges = sum(
        1
        for source, target in planned_edges
        if source.startswith('DBT_COLUMN::')
        and target.startswith('PB_COLUMN::')
    )

    if not dbt_models:
        write('  No dbt models found for bridging.')
    elif not pbi_tables:
        write('  No PowerBI tables found for bridging.')
    write(f'  -> {table_bridges} table-level bridge edges created.')
    write(f'  -> {column_bridges} column-level bridge edges created.')
    if by_reason:
        breakdown = ', '.join(
            f'{key}={value}' for key, value in sorted(by_reason.items())
        )
        write(f'  -> bridge reasons: {breakdown}')

    return {
        'table_bridges': table_bridges,
        'column_bridges': column_bridges,
        'by_reason': by_reason,
    }
