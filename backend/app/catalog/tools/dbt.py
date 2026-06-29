"""
dbt catalog tools — search, SQL retrieval, and upstream-tree traversal.
Pure read functions over the local catalog (``Item`` + lineage tables).
"""
from django.db.models import Q

from ..models import Item, NetworkEdge
from .catalog_search import _governance_lines


def _dbt_fqn(item) -> str:
    """Fully-qualified name (db.schema.table) for an Item, or a placeholder."""
    parts = [item.database_name, item.schema_name, item.alias or item.table_name]
    parts = [p for p in parts if p]
    return '.'.join(parts) if parts else '(no FQN)'


def search_dbt_models(query: str = '', limit: int = 10) -> str:
    """
    Searches dbt models, seeds, and snapshots in the local catalog.

    Use this WHEN the user asks about dbt models, transformations,
    materializations, SQL definitions, tags, owners, or model documentation.

    Returns model name, materialized table, database, materialization, tags,
    metadata, description, and a short SQL preview when available.
    """
    qs = (Item.objects
          .filter(deleted=False, service='dbt', item_type__in=['DBT_MODEL', 'DBT_SEED'])
          .select_related('item_group', 'item_group__ownership_department',
                          'item_group__ownership_person', 'item_group__steward'))
    if query:
        qs = qs.filter(
            Q(item_name__icontains=query) |
            Q(description__icontains=query) |
            Q(table_name__icontains=query) |
            Q(database_name__icontains=query)
        )
    qs = qs.order_by('database_name', 'table_name', 'item_name')

    results = []
    for model in qs[:limit]:
        parts = [f'Name: {model.item_name}', f'Type: {model.item_type}']
        if model.database_name:
            parts.append(f'Database: {model.database_name}')
        if model.table_name:
            parts.append(f'Table: {model.table_name}')
        if model.column_type:
            parts.append(f'Materialization: {model.column_type}')
        if model.tags:
            parts.append(f'Tags: {", ".join(model.tags)}')
        if model.meta:
            parts.append(f'Meta: {model.meta}')
        parts.extend(_governance_lines(model))
        if model.description:
            parts.append(f'Desc: {model.description[:180]}')
        if model.expression:
            parts.append(f'SQL: {model.expression[:240]}')
        results.append('\n'.join(parts))
    if not results:
        return 'No dbt models found matching the query.'
    return '\n---\n'.join(results)


def search_dbt_sources(query: str = '', limit: int = 10) -> str:
    """
    Searches dbt source definitions in the local catalog.

    Use this WHEN the user asks about upstream dbt sources, source tables,
    loaders, source schemas, or source documentation.
    """
    qs = Item.objects.filter(deleted=False, service='dbt', item_type='DBT_SOURCE')
    if query:
        qs = qs.filter(
            Q(item_name__icontains=query) |
            Q(description__icontains=query) |
            Q(table_name__icontains=query) |
            Q(database_name__icontains=query)
        )
    qs = qs.order_by('database_name', 'table_name', 'item_name')

    results = []
    for src in qs[:limit]:
        parts = [f'Source: {src.item_name}']
        if src.database_name:
            parts.append(f'Database: {src.database_name}')
        if src.table_name:
            parts.append(f'Table: {src.table_name}')
        if src.meta:
            parts.append(f'Meta: {src.meta}')
        if src.description:
            parts.append(f'Desc: {src.description[:180]}')
        results.append('\n'.join(parts))
    if not results:
        return 'No dbt sources found matching the query.'
    return '\n---\n'.join(results)


def search_dbt_tests(query: str = '', limit: int = 10) -> str:
    """
    Searches dbt tests in the local catalog.

    Use this WHEN the user asks about dbt tests, constraints, data quality checks,
    uniqueness/not-null/relationship tests, or test SQL definitions.
    """
    qs = Item.objects.filter(deleted=False, service='dbt', item_type='DBT_TEST')
    if query:
        qs = qs.filter(Q(item_name__icontains=query) | Q(description__icontains=query))
    qs = qs.order_by('item_name')

    results = []
    for test in qs[:limit]:
        parts = [f'Test: {test.item_name}']
        if test.column_type:
            parts.append(f'Test type: {test.column_type}')
        if test.tags:
            parts.append(f'Tags: {", ".join(test.tags)}')
        if test.description:
            parts.append(f'Desc: {test.description[:180]}')
        if test.expression:
            parts.append(f'SQL: {test.expression[:220]}')
        results.append('\n'.join(parts))
    if not results:
        return 'No dbt tests found matching the query.'
    return '\n---\n'.join(results)


def get_dbt_sql(model_name_or_id: str) -> str:
    """
    Returns the full stored SQL expression for one dbt model, seed, or test.

    Use this WHEN the user asks for the SQL, definition, source code, compiled
    query, or implementation of a dbt model/test. If the lookup is ambiguous,
    this tool returns candidates; ask the user to pick one before continuing.
    """
    query = (model_name_or_id or '').strip()
    if not query:
        return 'Please provide a dbt model/test name or item_id.'

    qs = Item.objects.filter(
        deleted=False,
        service='dbt',
        item_type__in=['DBT_MODEL', 'DBT_SEED', 'DBT_TEST'],
    ).exclude(expression__isnull=True).exclude(expression='')
    matches = list(qs.filter(item_id=query)[:2])
    if not matches:
        matches = list(qs.filter(item_name__iexact=query)[:10])
    if not matches:
        matches = list(qs.filter(
            Q(item_name__icontains=query) |
            Q(table_name__icontains=query) |
            Q(database_name__icontains=query)
        )[:10])

    if not matches:
        return f"No dbt SQL found for '{query}'."
    if len(matches) > 1:
        rows = '\n'.join(
            f'- {m.item_name} ({m.item_type}) table={m.table_name or "?"} id={m.item_id}'
            for m in matches[:10]
        )
        return f"'{query}' matches multiple dbt assets. Re-run with the exact item_id:\n{rows}"

    item = matches[0]
    sql = item.expression or ''
    meta = [f'Name: {item.item_name}', f'Type: {item.item_type}', f'ID: {item.item_id}']
    if item.database_name:
        meta.append(f'Database: {item.database_name}')
    if item.table_name:
        meta.append(f'Table: {item.table_name}')
    if item.column_type:
        meta.append(f'Materialization: {item.column_type}')
    return '\n'.join(meta) + f'\n\n```sql\n{sql}\n```'


def get_dbt_upstream_tree(model_name_or_id: str, max_depth: int = 5) -> str:
    """
    Returns the upstream lineage tree for a dbt model/seed/source: every
    asset it transitively depends on, grouped by depth, with the BigQuery
    FQN (database.schema.alias) attached so live BigQuery queries can use
    real table names instead of guessing from display labels.

    Use this in STEP 2 of SQL FLOW, AFTER ``search_dbt_models`` /
    ``search_dbt_sources`` has resolved a single asset. The FQNs returned
    here are exactly what ``bigquery_run_query`` should reference. When
    multiple assets match the input, the tool returns a disambiguation list
    and refuses to pick.
    """
    query = (model_name_or_id or '').strip()
    if not query:
        return 'Please provide a dbt model/source name or item_id.'
    max_depth = max(1, min(int(max_depth or 5), 10))

    qs = Item.objects.filter(
        deleted=False, service='dbt',
        item_type__in=['DBT_MODEL', 'DBT_SEED', 'DBT_SOURCE', 'DBT_SNAPSHOT'],
    )
    matches = (
        list(qs.filter(item_id=query)[:2])
        or list(qs.filter(item_name__iexact=query)[:10])
        or list(qs.filter(item_name__icontains=query)[:10])
    )
    if not matches:
        return f"No dbt asset matched '{query}'."
    if len(matches) > 1:
        rows = '\n'.join(
            f'- **{m.item_name}** ({m.item_type}) [id={m.item_id}]'
            for m in matches[:10]
        )
        return f"'{query}' matches multiple dbt assets. Re-run with item_id:\n{rows}"

    root = matches[0]
    root_node_id = f'{root.item_type}::{root.item_id}'

    visited = {root_node_id: 0}
    levels: dict = {0: [root_node_id]}
    frontier = [root_node_id]
    for depth in range(1, max_depth + 1):
        next_frontier = []
        edges = NetworkEdge.objects.filter(
            target__in=frontier, source__startswith='DBT_',
        ).values_list('source', 'target')
        for src, _tgt in edges:
            if src in visited:
                continue
            visited[src] = depth
            levels.setdefault(depth, []).append(src)
            next_frontier.append(src)
        if not next_frontier:
            break
        frontier = next_frontier

    item_ids = [n.split('::', 1)[1] for n in visited if '::' in n]
    items_by_id = {i.item_id: i for i in Item.objects.filter(item_id__in=item_ids)}

    def _line(node_id: str) -> str:
        ihash = node_id.split('::', 1)[1] if '::' in node_id else node_id
        item = items_by_id.get(ihash)
        if not item:
            return f'  - {node_id}'
        return (
            f'  - **{item.item_name}** ({item.item_type}) — '
            f'`{_dbt_fqn(item)}` [id={item.item_id}]'
        )

    out = [
        f'Upstream tree for **{root.item_name}** ({root.item_type})',
        f'Root FQN: `{_dbt_fqn(root)}`',
        '',
    ]
    found_any = False
    for depth in sorted(d for d in levels if d > 0):
        out.append(f'Depth {depth}:')
        for nid in levels[depth]:
            out.append(_line(nid))
        found_any = True
    if not found_any:
        out.append('No upstream dependencies found in the lineage graph.')
    return '\n'.join(out)


def get_dbt_model_schema(model_name_or_id: str) -> str:
    """
    Returns everything about ONE dbt model/seed/snapshot in a single shot:
    materialization + BigQuery FQN, description, its columns (name, type,
    description), the SQL definition, the upstream lineage tree (with
    BigQuery FQNs), and the direct downstream consumers.

    Use this WHEN the user asks about a specific dbt model — its
    definition, SQL, columns, lineage, or how it connects. The full list
    of models and columns is already in your context, so you do NOT need
    to search first; pass the model name (or item_id) directly. If the
    name is ambiguous the tool returns the candidates — ask the user which
    one (or re-run with the exact item_id).
    """
    query = (model_name_or_id or '').strip()
    if not query:
        return 'Please provide a dbt model/seed name or item_id.'

    qs = Item.objects.filter(
        deleted=False, service='dbt',
        item_type__in=['DBT_MODEL', 'DBT_SEED', 'DBT_SNAPSHOT'],
    )
    matches = (
        list(qs.filter(item_id=query)[:2])
        or list(qs.filter(item_name__iexact=query)[:10])
        or list(qs.filter(
            Q(item_name__icontains=query) | Q(table_name__icontains=query)
        )[:10])
    )
    if not matches:
        return f"No dbt model matched '{query}'."
    if len(matches) > 1:
        rows = '\n'.join(
            f'- {m.item_name} ({m.item_type}) [id={m.item_id}]'
            for m in matches[:10]
        )
        return (
            f"'{query}' matches multiple dbt models. Re-run with the exact "
            f"item_id:\n{rows}"
        )

    model = matches[0]
    fqn = _dbt_fqn(model)

    parts = [
        f'# dbt model: {model.item_name}',
        f'Type: {model.item_type}  |  Materialization: '
        f'{model.column_type or "?"}  |  FQN: `{fqn}`  |  id={model.item_id}',
    ]
    if model.description:
        parts.append(f'\nDescription: {model.description}')

    # Columns are linked to the model by ``dataset_id`` (the dbt unique_id).
    cols = list(
        Item.objects.filter(
            deleted=False, service='dbt', item_type='DBT_COLUMN',
            dataset_id=model.dataset_id,
        ).order_by('item_name').values('item_name', 'datatype', 'description')
    )
    if cols:
        parts.append('\n## Columns')
        for c in cols:
            d = (c['description'] or '').strip().replace('\n', ' ')
            parts.append(
                f'- {c["item_name"]} ({c["datatype"] or "?"})'
                + (f' — {d}' if d else '')
            )

    if model.expression:
        parts.append('\n## SQL\n```sql\n' + model.expression + '\n```')

    parts.append('\n## Upstream lineage')
    parts.append(get_dbt_upstream_tree(model.item_id))

    # Direct downstream consumers (one hop): edges whose source is this node.
    node = f'{model.item_type}::{model.item_id}'
    down_targets = (
        NetworkEdge.objects.filter(source=node)
        .values_list('target', flat=True)[:100]
    )
    down_ids = [
        t.split('::', 1)[1] for t in down_targets
        if '::' in t and not t.startswith('DBT_COLUMN')
    ]
    if down_ids:
        downs = list(
            Item.objects.filter(item_id__in=down_ids)
            .values('item_name', 'item_type')
        )
        if downs:
            parts.append('\n## Direct downstream consumers')
            for d in downs:
                parts.append(f'- {d["item_name"]} ({d["item_type"]})')

    return '\n'.join(parts)
