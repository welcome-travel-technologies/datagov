"""Per-key MCP tool registry.

Builds the list of tools an authenticated ``McpApiKey`` may see/call:
**key scopes ∩ org feature flags**, using the SAME tool functions the chat
agent registers (``catalog/tools/``) so there is exactly one tool layer.

Two MCP-specific differences from the chat agent:

- ``get_catalog_overview`` — the chat agent gets the catalog front-loaded
  into its system prompt; MCP clients have no system prompt, so the same
  front-loaded context (PowerBI measures/reports/tables, dbt models,
  BigQuery schema + workspace defaults) is exposed as a TOOL the client
  calls first to resolve exact names.
- Input schemas are introspected from each tool function's signature, so
  they can never drift from the implementations; descriptions are the same
  docstrings the chat LLM already relies on.
"""
import inspect
from dataclasses import dataclass, field

from . import auth

_JSON_TYPES = {str: 'string', int: 'integer', bool: 'boolean', float: 'number'}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    func: object = field(repr=False)


def _spec_for(fn, name: str = None) -> ToolSpec:
    """Introspect ``fn`` into a ToolSpec (JSON schema from the signature,
    description from the docstring)."""
    props, required = {}, []
    for pname, p in inspect.signature(fn).parameters.items():
        ann = p.annotation
        props[pname] = {'type': _JSON_TYPES.get(ann, 'string')}
        if p.default is inspect.Parameter.empty:
            required.append(pname)
    schema = {'type': 'object', 'properties': props}
    if required:
        schema['required'] = required
    return ToolSpec(
        name=name or fn.__name__,
        description=inspect.getdoc(fn) or '',
        input_schema=schema,
        func=fn,
    )


def _build_overview_tool(org, user, bigquery_client):
    """The front-loaded chat context, repackaged as an MCP tool."""

    def get_catalog_overview() -> str:
        """THE starting point — call this FIRST, before any other tool.

        Returns the authoritative catalog inventory for this organization in
        one call: every PowerBI measure (with owner/status), report and table
        name, every dbt model, the BigQuery schema for the in-scope datasets,
        and the default PowerBI workspace to use for live queries.

        All other tools resolve items by EXACT name — take names VERBATIM
        from this listing (do not guess or abbreviate), then call
        ``get_pb_item_details`` / ``get_dbt_item_details`` for one item's full
        profile, ``get_pb_usage_analytics`` for usage rollups, or the live
        query tools where enabled.
        """
        from ..tools.assistant import PROVIDERS

        pb_scope = getattr(org, 'assistant_powerbi_workspace_ids', None) or None
        bq_scope = getattr(org, 'assistant_bigquery_dataset_ids', None) or None
        sections = []

        if getattr(org, 'powerbi_tools_enabled', True):
            try:
                sections.append(PROVIDERS['powerbi'].build_context(
                    org, client=None, scope_ids=pb_scope))
            except Exception:
                pass
            # Default-workspace guidance (the chat agent gets this as a
            # prompt block; MCP callers need it for powerbi_run_dax_query).
            try:
                from ..services.workspaces import resolve_default_workspaces_for_org
                from ..tools.agent import _workspace_scope_block
                scope = resolve_default_workspaces_for_org(user, org)
                if scope:
                    sections.append(_workspace_scope_block(scope))
            except Exception:
                pass
        if getattr(org, 'dbt_tools_enabled', False):
            try:
                sections.append(PROVIDERS['dbt'].build_context(
                    org, client=None, scope_ids=None))
            except Exception:
                pass
        if getattr(org, 'bigquery_tools_enabled', False) and bigquery_client is not None:
            try:
                sections.append(PROVIDERS['bigquery'].build_context(
                    org, client=bigquery_client, scope_ids=bq_scope))
            except Exception:
                pass

        text = ''.join(s for s in sections if s).strip()
        return text or ('The catalog is empty or no integrations are enabled '
                        'for this organization.')

    return get_catalog_overview


def build_tool_specs(key) -> list:
    """All tools visible to ``key``: scopes ∩ org flags, mirroring the chat
    agent's assembly in ``build_chatbot_agent_for_org`` / ``get_agent``."""
    # Lazy imports: catalog.views pulls in the whole app; importing it at
    # module load would recurse through urls.py.
    from ..views import (
        _get_bigquery_client_for_org,
        _get_bigquery_live_enabled_for_org,
        _get_dbt_enabled_for_org,
        _get_powerbi_client_for_org,
        _get_powerbi_tools_enabled_for_org,
    )

    org, user = key.organization, key.user
    scopes = set(key.scopes or [])
    specs: list = []

    powerbi_enabled = _get_powerbi_tools_enabled_for_org(org)
    dbt_enabled = _get_dbt_enabled_for_org(org)
    bigquery_client = _get_bigquery_client_for_org(org)

    if auth.SCOPE_CATALOG_READ in scopes:
        from ..tools.analytics import get_pb_usage_analytics
        from ..tools.lineage import (
            get_dbt_item_details, get_lineage, get_pb_item_details,
        )

        specs.append(_spec_for(_build_overview_tool(org, user, bigquery_client),
                               name='get_catalog_overview'))
        specs.append(_spec_for(get_lineage))
        if powerbi_enabled:
            specs.append(_spec_for(get_pb_item_details))
            specs.append(_spec_for(get_pb_usage_analytics))
        if dbt_enabled:
            specs.append(_spec_for(get_dbt_item_details))

    if auth.SCOPE_POWERBI_QUERY in scopes:
        powerbi_client = _get_powerbi_client_for_org(org)  # None unless live tier on
        if powerbi_client is not None:
            from ..powerbi_tools import make_powerbi_tools
            for tool in make_powerbi_tools(powerbi_client):
                if tool.__name__ == 'powerbi_run_dax_query':
                    specs.append(_spec_for(tool))

    if auth.SCOPE_BIGQUERY_QUERY in scopes:
        if bigquery_client is not None and _get_bigquery_live_enabled_for_org(org):
            from ..bigquery_tools import make_bigquery_tools
            for tool in make_bigquery_tools(bigquery_client):
                if tool.__name__ == 'bigquery_execute_query':
                    specs.append(_spec_for(tool))

    return specs
