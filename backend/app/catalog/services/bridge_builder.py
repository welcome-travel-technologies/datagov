"""
Build the cross-tool bridge edges (dbt ↔ PowerBI) at table and column level.

This module owns the SQL I/O. The matching decision is delegated to
``bridge_matching`` so it stays pure-python and unit-testable.

Public entry point: :func:`build_cross_tool_bridges`.
"""
from typing import Callable, Optional

from .bridge_matching import (
    DbtModelKey,
    PbiTableKey,
    iter_column_pairs,
    iter_table_pairs,
)
from .network_classify import classify_edge

# Bridges are deterministic by type: a table bridge (DBT_MODEL → PB_TABLE) is
# model/asset-level; a column bridge (DBT_COLUMN → PB_COLUMN) is column-level.
_TABLE_BRIDGE_KIND, _TABLE_BRIDGE_LEVEL = classify_edge('DBT_MODEL', 'PB_TABLE')
_COLUMN_BRIDGE_KIND, _COLUMN_BRIDGE_LEVEL = classify_edge('DBT_COLUMN', 'PB_COLUMN')


def _noop(_msg: str) -> None:  # pragma: no cover
    pass


def build_cross_tool_bridges(
    cursor,
    org_id_literal: str,
    write: Optional[Callable[[str], None]] = None,
) -> dict:
    """Replace the existing dbt → PBI bridge edges with a fresh set built by
    the FQN-first matcher.

    Args:
        cursor: An open Django DB cursor (caller manages the transaction).
        org_id_literal: Either ``'NULL'`` or a stringified integer — interpolated
            directly into SQL the same way the rest of the loaders do it.
        write: Optional callable for status messages. Defaults to a noop.

    Returns:
        Dict with keys ``table_bridges`` and ``column_bridges`` (counts) plus
        a per-reason breakdown under ``by_reason``.
    """
    write = write or _noop

    write('Building cross-tool bridge edges (dbt → PowerBI)...')

    # 1. Clean old bridge edges (DBT_* ↔ non-DBT_*).
    cursor.execute(r"""
        DELETE FROM catalog_networkedge
        WHERE (source LIKE 'DBT\_%%' AND target NOT LIKE 'DBT\_%%')
           OR (target LIKE 'DBT\_%%' AND source NOT LIKE 'DBT\_%%');
    """)
    cursor.execute("""
        DELETE FROM catalog_networkedge
        WHERE source IN (
            SELECT 'DBT_COLUMN::' || item_id FROM catalog_item
            WHERE item_type = 'DBT_COLUMN' AND deleted = FALSE
        )
        AND target IN (
            SELECT 'PB_COLUMN::' || item_id FROM catalog_item
            WHERE item_type = 'PB_COLUMN' AND deleted = FALSE
        );
    """)

    # 2. Load dbt models with the fields the matcher needs.
    cursor.execute("""
        SELECT item_id, item_name, database_name, schema_name, alias, table_name, dataset_id
        FROM catalog_item
        WHERE service = 'dbt'
          AND item_type = 'DBT_MODEL'
          AND table_name IS NOT NULL
          AND deleted = FALSE;
    """)
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
        for (item_id, item_name, database_name, schema_name, alias, table_name, _ds) in dbt_rows
    ]
    # dataset_id is needed for the column-level join below; keep a side map.
    dbt_dataset_id_by_item: dict = {
        item_id: ds for (item_id, _n, _db, _s, _a, _t, ds) in dbt_rows
    }
    dbt_name_by_item: dict = {
        item_id: name for (item_id, name, _db, _s, _a, _t, _ds) in dbt_rows
    }

    if not dbt_models:
        write('  No dbt models found for bridging.')
        return {'table_bridges': 0, 'column_bridges': 0, 'by_reason': {}}

    # 3. Load PowerBI tables with the bq_* triple.
    cursor.execute("""
        SELECT item_id, item_name, bq_project, bq_schema, bq_source_name
        FROM catalog_item
        WHERE (service IS NULL OR service != 'dbt')
          AND item_type = 'PB_TABLE'
          AND item_name IS NOT NULL
          AND deleted = FALSE;
    """)
    pbi_rows = cursor.fetchall()
    pbi_tables = [
        PbiTableKey(
            item_id=item_id,
            item_name=item_name or '',
            bq_project=bq_project,
            bq_schema=bq_schema,
            bq_source_name=bq_source_name,
        )
        for (item_id, item_name, bq_project, bq_schema, bq_source_name) in pbi_rows
    ]
    pbi_name_by_item: dict = {item_id: name for (item_id, name, *_rest) in pbi_rows}

    if not pbi_tables:
        write('  No PowerBI tables found for bridging.')
        return {'table_bridges': 0, 'column_bridges': 0, 'by_reason': {}}

    table_bridges = 0
    column_bridges = 0
    by_reason: dict = {}

    for match in iter_table_pairs(pbi_tables, dbt_models):
        dbt_id = match.dbt_item_id
        pbi_id = match.pbi_item_id
        reason = match.reason
        by_reason[reason] = by_reason.get(reason, 0) + 1

        src_node = f'DBT_MODEL::{dbt_id}'
        tgt_node = f'PB_TABLE::{pbi_id}'

        cursor.execute(f"""
            INSERT INTO catalog_networknode (node_id, name, "group", organization_id)
            VALUES (%s, %s, 'DBT_MODEL', {org_id_literal})
            ON CONFLICT (node_id) DO NOTHING;
        """, [src_node, dbt_name_by_item.get(dbt_id) or ''])
        cursor.execute(f"""
            INSERT INTO catalog_networknode (node_id, name, "group", organization_id)
            VALUES (%s, %s, 'PB_TABLE', {org_id_literal})
            ON CONFLICT (node_id) DO NOTHING;
        """, [tgt_node, pbi_name_by_item.get(pbi_id) or ''])
        cursor.execute(f"""
            INSERT INTO catalog_networkedge (source, target, organization_id, bridge_reason, kind, level)
            VALUES (%s, %s, {org_id_literal}, %s, %s, %s)
            ON CONFLICT (source, target) DO UPDATE SET
                bridge_reason = EXCLUDED.bridge_reason,
                kind = EXCLUDED.kind, level = EXCLUDED.level;
        """, [src_node, tgt_node, reason, _TABLE_BRIDGE_KIND, _TABLE_BRIDGE_LEVEL])
        table_bridges += 1

        # Column-level bridge — same reason carried through.
        cursor.execute("""
            SELECT item_id, item_name
            FROM catalog_item
            WHERE item_type = 'DBT_COLUMN'
              AND dataset_id = %s
              AND deleted = FALSE;
        """, [dbt_dataset_id_by_item.get(dbt_id)])
        dbt_cols = cursor.fetchall()
        if not dbt_cols:
            continue

        cursor.execute("""
            SELECT item_id, item_name
            FROM catalog_item
            WHERE item_type = 'PB_COLUMN'
              AND table_name = %s
              AND deleted = FALSE;
        """, [pbi_name_by_item.get(pbi_id)])
        pbi_cols = cursor.fetchall()
        if not pbi_cols:
            continue

        # Build name lookups for node-row inserts.
        dbt_col_name_by_id = {cid: cname for cid, cname in dbt_cols}
        pbi_col_name_by_id = {cid: cname for cid, cname in pbi_cols}

        for dbt_col_id, pbi_col_id in iter_column_pairs(pbi_cols, dbt_cols):
            col_src = f'DBT_COLUMN::{dbt_col_id}'
            col_tgt = f'PB_COLUMN::{pbi_col_id}'
            cursor.execute(f"""
                INSERT INTO catalog_networknode (node_id, name, "group", organization_id)
                VALUES (%s, %s, 'DBT_COLUMN', {org_id_literal})
                ON CONFLICT (node_id) DO NOTHING;
            """, [col_src, dbt_col_name_by_id.get(dbt_col_id) or ''])
            cursor.execute(f"""
                INSERT INTO catalog_networknode (node_id, name, "group", organization_id)
                VALUES (%s, %s, 'PB_COLUMN', {org_id_literal})
                ON CONFLICT (node_id) DO NOTHING;
            """, [col_tgt, pbi_col_name_by_id.get(pbi_col_id) or ''])
            # The dbt model column feeds the PowerBI table column 1:1, so the
            # cross-tool bridge is a column-level pass-through.
            cursor.execute(f"""
                INSERT INTO catalog_networkedge (source, target, organization_id, bridge_reason, kind, level, lineage_type)
                VALUES (%s, %s, {org_id_literal}, %s, %s, %s, 'pass-through')
                ON CONFLICT (source, target) DO UPDATE SET
                    bridge_reason = EXCLUDED.bridge_reason,
                    kind = EXCLUDED.kind, level = EXCLUDED.level,
                    lineage_type = EXCLUDED.lineage_type;
            """, [col_src, col_tgt, reason, _COLUMN_BRIDGE_KIND, _COLUMN_BRIDGE_LEVEL])
            column_bridges += 1

    write(f'  → {table_bridges} table-level bridge edges created.')
    write(f'  → {column_bridges} column-level bridge edges created.')
    if by_reason:
        breakdown = ', '.join(f'{k}={v}' for k, v in sorted(by_reason.items()))
        write(f'  → bridge reasons: {breakdown}')

    return {
        'table_bridges': table_bridges,
        'column_bridges': column_bridges,
        'by_reason': by_reason,
    }
