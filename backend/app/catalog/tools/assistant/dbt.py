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
    description, and columns (name + datatype + description). Cached per org."""
    org_id = getattr(org, 'id', 'x')
    return cached_context(f'asst_ctx_dbt_{org_id}', _build)


def _build() -> str:
    from ...models import Item

    models = list(
        Item.objects.filter(
            deleted=False, service='dbt', item_type__in=_MODEL_TYPES,
        ).order_by('database_name', 'schema_name', 'item_name')
    )
    if not models:
        return ''

    # Columns are linked to their model by ``dataset_id`` (the dbt node
    # unique_id) — NOT table_name, which differs between a model row and
    # its column rows. Fetch once and group in Python.
    by_dataset: dict = {}
    for c in Item.objects.filter(
        deleted=False, service='dbt', item_type='DBT_COLUMN',
    ).values('item_name', 'datatype', 'description', 'dataset_id'):
        by_dataset.setdefault(c['dataset_id'], []).append(c)

    lines = [
        '\n\n## dbt catalog (authoritative — the full model & column list is '
        'here; do NOT search the catalog)\n'
    ]
    lines.append(f'### Models ({len(models)})')
    for mdl in models:
        fqn = '.'.join(
            p for p in [mdl.database_name, mdl.schema_name, mdl.alias or mdl.table_name] if p
        ) or '(no FQN)'
        mat = mdl.column_type or 'model'
        desc = (mdl.description or '').strip().replace('\n', ' ')
        lines.append(
            f'- **{mdl.item_name}** ({mat}, `{fqn}`)' + (f' — {desc}' if desc else '')
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
    return [get_dbt_item_details]
