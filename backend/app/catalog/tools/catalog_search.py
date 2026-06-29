"""
PowerBI catalog read helpers — query the local Django catalog (``Item``).

Pure read functions: no live API calls, no LLM. The assistant does not
register these as agent *tools* (the PowerBI catalog is front-loaded into
the system prompt instead — see ``catalog/tools/assistant/powerbi.py``).
``_governance_lines`` is shared with ``catalog/tools/dbt.py``; ``search_pb_columns``
remains as a catalog-read utility exercised by the tests.
"""
from django.db.models import Q

from ..models import Item


def _person_label(p):
    """Render a DataPerson as 'Name (@slack)' or 'Name', or None when missing."""
    if not p:
        return None
    return f'{p.name} ({p.slack_handle})' if p.slack_handle else p.name


def _governance_lines(item) -> list[str]:
    """Owner / steward / department lines for chatbot output. Governance
    lives on the ItemGroup, so callers should have used
    select_related('item_group__ownership_department',
    'item_group__ownership_person', 'item_group__steward') to keep this
    read-cheap."""
    lines: list[str] = []
    if item.ownership_department:
        lines.append(f'Department: {item.ownership_department.name}')
    owner = _person_label(item.ownership_person)
    if owner:
        lines.append(f'Owner: {owner}')
    steward = _person_label(item.steward)
    if steward:
        lines.append(f'Steward: {steward}')
    return lines


def search_pb_columns(
    query: str = '',
    limit: int = 10,
    dataset_id: str = '',
    workspace_id: str = '',
    table_name: str = '',
) -> str:
    """
    Searches the local catalog for PowerBI semantic-model columns by name or
    description.

    Use this WHEN:
    - The user asks about columns, fields, or data types in a dataset table.
    - You need to find a dimension column for a SUMMARIZECOLUMNS DAX query
      (e.g. the user asks for a breakdown "by country").
    - You need to discover the exact date-column name (e.g. 'Date'[Date]) for
      a DATESBETWEEN / DATESYTD filter before building DAX.
    - The user asks to compare schemas or find common fields across tables.

    For live PowerBI DAX queries, pass the measure's ``dataset_id`` and
    ``workspace_id`` to avoid picking a similarly named column from a different
    semantic model.

    Returns: column name, DAX reference, data type, column type (Data /
    Calculated), parent table, dataset/workspace, usage flag, and status.

    Does NOT return column values or row-level data — use
    ``powerbi_run_dax_query`` for that.
    """
    qs = (Item.objects
          .filter(deleted=False, item_type='PB_COLUMN', service='powerbi')
          .select_related('item_group', 'item_group__ownership_department',
                          'item_group__ownership_person', 'item_group__steward'))
    if dataset_id:
        qs = qs.filter(dataset_id=dataset_id)
    if workspace_id:
        qs = qs.filter(workspace_id=workspace_id)
    if table_name:
        qs = qs.filter(table_name__iexact=table_name)
    if query:
        qs = qs.filter(Q(item_name__icontains=query) | Q(description__icontains=query))
    qs = qs.order_by('dataset_name', 'table_name', 'item_name')

    results = []
    for c in qs[:limit]:
        gov = _governance_lines(c)
        gov_block = ('\n' + '\n'.join(gov)) if gov else ''
        results.append(
            f'Column ID: {c.item_id}\n'
            f'Name: {c.item_name}\n'
            f'DAX Reference: \'{c.table_name}\'[{c.item_name}]\n'
            f'Type: {c.column_type} / {c.datatype}\n'
            f'Table: {c.table_name}\n'
            f'Dataset: {c.dataset_id or "?"} ({c.dataset_name or "?"})\n'
            f'Workspace: {c.workspace_name}\n'
            f'Unused: {c.is_unused}\n'
            f'Status: {c.status}'
            f'{gov_block}\n'
            f'Description: {c.description or "None"}\n'
        )
    if not results:
        scope = []
        if dataset_id:
            scope.append(f'dataset_id={dataset_id}')
        if workspace_id:
            scope.append(f'workspace_id={workspace_id}')
        if table_name:
            scope.append(f'table={table_name}')
        suffix = f' in scope ({", ".join(scope)})' if scope else ''
        return f'No columns found matching the query{suffix}.'
    return '\n---\n'.join(results)
