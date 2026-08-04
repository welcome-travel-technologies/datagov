"""
dbt assistant provider.

Uniform contract (see ``assistant/__init__.py``):
  scope_options(org)                     -> [] (dbt has no scope selector)
  build_context(org, *, client, scope_ids) -> front-loaded model+column catalog
  build_tools(org, *, client)            -> [get_dbt_model_schema]

The agent answers dbt questions WITHOUT searching: every model and its
columns are dumped into the system prompt, and ``get_dbt_model_schema``
gives one-shot depth on a specific model (SQL, materialization, columns,
upstream lineage with BigQuery FQNs, direct downstream consumers).
"""
from __future__ import annotations

from .cache import cached_context

_MODEL_TYPES = ['DBT_MODEL', 'DBT_SEED', 'DBT_SNAPSHOT']


def scope_options(org) -> list[dict]:
    """dbt is not scoped — the whole project is front-loaded."""
    return []


def build_context(org, *, client=None, scope_ids=None) -> str:
    """Front-loaded dbt catalog: every model with its materialization, FQN,
    owner, category, description, and columns (name + datatype + description).
    Cached per org."""
    org_id = getattr(org, 'pk', None)
    if org_id is None:
        return ''
    return cached_context(
        f'asst_ctx_dbt_v3_{org_id}',
        lambda: _build(org_id),
    )


def _build(organization_id) -> str:
    from django.db.models import Q

    from ...models import Item

    # Governance lives on the ItemGroup; join it here so ownership questions
    # ("who owns model X", "which models are in the Finance category") are
    # answerable straight from this listing with NO tool call — the same way
    # the PowerBI context surfaces group-level owner/category. Cross-org FKs
    # are filtered out rather than leaking another tenant's taxonomy.
    # dbt models are singleton groups, which the
    # ``itemgroup_definition_measure_only`` constraint forbids from carrying a
    # business Definition — so there is nothing to show for that field here.
    models = list(
        Item.objects.filter(
            organization_id=organization_id,
            deleted=False,
            service='dbt',
            item_type__in=_MODEL_TYPES,
        ).filter(
            Q(item_group__organization_id=organization_id) |
            Q(item_group__isnull=True)
        ).filter(
            Q(item_group__ownership_person__organization_id=organization_id) |
            Q(item_group__ownership_person__isnull=True),
            Q(item_group__category__organization_id=organization_id) |
            Q(item_group__category__isnull=True),
        ).select_related(
            'item_group', 'item_group__ownership_person', 'item_group__category',
        ).order_by('database_name', 'schema_name', 'item_name')
    )
    if not models:
        return ''

    # Columns are linked to their model by ``dataset_id`` (the dbt node
    # unique_id) — NOT table_name, which differs between a model row and
    # its column rows. Fetch once and group in Python.
    by_dataset: dict = {}
    for c in Item.objects.filter(
        organization_id=organization_id,
        deleted=False,
        service='dbt',
        item_type='DBT_COLUMN',
    ).values('item_name', 'datatype', 'description', 'dataset_id'):
        by_dataset.setdefault(c['dataset_id'], []).append(c)

    lines = [
        '\n\n## dbt catalog (authoritative — the full model & column list is '
        'here; do NOT search the catalog)\n'
    ]
    lines.append(
        f'### Models ({len(models)}) — `owner:` and `category:` are group-level '
        'governance (answer "who owns model X" / "which models are in category Y" '
        'straight from here, no tool call).')
    for mdl in models:
        fqn = '.'.join(
            p for p in [mdl.database_name, mdl.schema_name, mdl.alias or mdl.table_name] if p
        ) or '(no FQN)'
        mat = mdl.column_type or 'model'
        desc = (mdl.description or '').strip().replace('\n', ' ')
        group = mdl.item_group if mdl.item_group_id else None
        owner = getattr(getattr(group, 'ownership_person', None), 'name', None)
        category = getattr(getattr(group, 'category', None), 'name', None)
        meta = []
        if owner:
            meta.append(f'owner: {owner}')
        if category:
            meta.append(f'category: {category}')
        lines.append(
            f'- **{mdl.item_name}** ({mat}, `{fqn}`)'
            + (f' — {desc}' if desc else '')
            + ('  ·  ' + ' · '.join(meta) if meta else '')
        )
        for c in sorted(by_dataset.get(mdl.dataset_id, []), key=lambda x: x['item_name'] or ''):
            cdesc = (c['description'] or '').strip().replace('\n', ' ')
            dt = c['datatype'] or '?'
            lines.append(f'    - {c["item_name"]} ({dt})' + (f' — {cdesc}' if cdesc else ''))
    return '\n'.join(lines) + '\n'


def build_tools(org, *, client=None) -> list:
    # The dbt item profiler: full model depth (FQN, columns, SQL, upstream
    # tree, downstream consumers) plus ownership / usage stats, in one call.
    from ..lineage import get_dbt_item_details
    from ..organization_scope import bind_organization_read_tool

    return [bind_organization_read_tool(get_dbt_item_details, org)]
