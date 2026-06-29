"""
BigQuery assistant provider.

Uniform contract (see ``assistant/__init__.py``):
  scope_options(org)                     -> datasets available to scope on (live)
  build_context(org, *, client, scope_ids) -> front-loaded schema for selected datasets
  build_tools(org, *, client)            -> [bigquery_execute_query]

The agent answers BigQuery questions WITHOUT listing/describing: the full
schema (tables → columns → types → descriptions) for the selected datasets
is dumped into the system prompt, and the agent writes SQL against those
exact tables/columns and runs it with ``bigquery_execute_query``.
"""
from __future__ import annotations

import logging

from .cache import cached_context, scope_key

logger = logging.getLogger(__name__)


def scope_options(org) -> list[dict]:
    """Datasets selectable for the BigQuery context (live ``list_datasets``).
    Returns ``[{"id","name"}]``; empty when no client/datasets."""
    from ...bigquery_client import build_bigquery_client_for_org

    try:
        client = build_bigquery_client_for_org(org)
    except Exception:
        return []
    if client is None:
        return []
    try:
        datasets = client.list_datasets(max_results=200)
    except Exception:
        logger.exception('bigquery scope_options: list_datasets failed')
        return []
    out = []
    for ds in datasets:
        ref = ds.reference
        out.append({'id': ref.dataset_id, 'name': ref.dataset_id})
    return out


def build_context(org, *, client=None, scope_ids=None) -> str:
    """Front-loaded BigQuery schema for the selected datasets. Cached per
    org + scope (live API calls are slow). Empty when nothing is selected."""
    if client is None or not scope_ids:
        return ''
    org_id = getattr(org, 'id', 'x')
    key = f'asst_ctx_bq_{org_id}_{scope_key(scope_ids)}'
    return cached_context(key, lambda: _build(client, scope_ids))


def _build(client, scope_ids) -> str:
    from ...bigquery_tools import _schema_lines

    blocks = [
        '\n\n## BigQuery schema (authoritative — the full schema for the '
        'in-scope datasets is here; do NOT list or describe tables)\n'
    ]
    any_table = False
    for ds in scope_ids:
        try:
            tables = client.list_tables(ds, max_results=1000)
        except Exception as exc:
            blocks.append(f'### dataset `{ds}` — could not list tables: {exc}')
            continue
        blocks.append(f'### dataset `{ds}`')
        for t in tables:
            fqn = t.full_table_id.replace(':', '.')
            try:
                table = client.get_table(fqn)
            except Exception:
                continue
            any_table = True
            blocks.append(f'\n**`{fqn}`** ({getattr(t, "table_type", "TABLE")})')
            blocks.append('| Column | Type | Mode | Description |')
            blocks.append('| --- | --- | --- | --- |')
            blocks.extend(_schema_lines(table.schema))
    if not any_table:
        return ''
    return '\n'.join(blocks) + '\n'


def build_tools(org, *, client=None) -> list:
    if client is None:
        return []
    from ...bigquery_tools import make_bigquery_tools
    return [
        tool for tool in make_bigquery_tools(client)
        if tool.__name__ == 'bigquery_execute_query'
    ]
