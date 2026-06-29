"""
Assistant integration providers — one module per integration, all the
same shape so the agent factory treats them uniformly.

Every provider module (``powerbi``, ``dbt``, ``bigquery``) exposes:

    scope_options(org) -> list[dict]                          # {"id","name"} for settings selectors ([] if unscoped)
    build_context(org, *, client=None, scope_ids=None) -> str  # front-loaded markdown, cached per org+scope
    build_tools(org, *, client=None) -> list                  # the slim tool callables

The agent factory (``catalog/tools/agent.py``) loops ``PROVIDERS``: for
each enabled integration it appends ``build_context(...)`` to the system
prompt and registers the callables from ``build_tools(...)``. One shape,
three implementations — the catalog is front-loaded so the agent never
has to *search*; each integration keeps just its one schema/connection
tool (plus run-DAX / run-SQL where applicable).
"""
from . import bigquery, dbt, powerbi
from .cache import cached_context, scope_key

# Ordered so the system prompt reads PowerBI → dbt → BigQuery.
PROVIDERS = {
    'powerbi': powerbi,
    'dbt': dbt,
    'bigquery': bigquery,
}

__all__ = ['PROVIDERS', 'cached_context', 'scope_key', 'powerbi', 'dbt', 'bigquery']
