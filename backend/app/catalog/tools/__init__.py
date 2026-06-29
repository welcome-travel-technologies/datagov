"""
Public API for the chatbot tools package.

Re-exports the symbols that views, tests, and management commands import
under the old monolithic ``catalog.tools`` name so the split is invisible
to callers. New code should prefer importing from the specific submodule.
"""
from .agent import DEFAULT_CHATBOT_MODEL, get_agent
from .analytics import (
    get_pb_usage_analytics,
)
from .catalog_search import (
    search_pb_columns,
)
from .dbt import (
    get_dbt_sql,
    get_dbt_upstream_tree,
    search_dbt_models,
    search_dbt_sources,
    search_dbt_tests,
)
from .lineage import (
    get_dbt_bigquery_lineage,
    get_lineage,
    get_pb_measure_dependencies,
    preview_pb_dbt_bridge,
)
from .pb_schema_bundle import (
    get_pb_measure_schema,
    verify_pb_measure_dimension_link,
)
from .prompts import (
    build_date_context_block,
    build_format_reminder_block,
)
from .safe_wrapper import make_safe_tool


__all__ = [
    'DEFAULT_CHATBOT_MODEL',
    'build_date_context_block',
    'build_format_reminder_block',
    'get_agent',
    'get_dbt_bigquery_lineage',
    'get_dbt_sql',
    'get_dbt_upstream_tree',
    'get_lineage',
    'get_pb_measure_dependencies',
    'get_pb_measure_schema',
    'get_pb_usage_analytics',
    'make_safe_tool',
    'preview_pb_dbt_bridge',
    'search_dbt_models',
    'search_dbt_sources',
    'search_dbt_tests',
    'search_pb_columns',
    'verify_pb_measure_dimension_link',
]
