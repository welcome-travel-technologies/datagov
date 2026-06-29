import json

import pytest

from catalog.bigquery_tools import make_bigquery_tools, validate_read_only_sql
from catalog.models import Item, NetworkEdge, NetworkNode, OrganizationMembership
from catalog.powerbi_tools import make_powerbi_tools
from catalog.tools import get_agent, get_dbt_bigquery_lineage, get_dbt_sql


def _registered_tool_names(agent):
    return set(agent._function_toolset.tools.keys())  # noqa: SLF001 - intentional agent introspection in tests


@pytest.mark.django_db
def test_org_bot_settings_persist_all_tool_flags(client, org):
    user = org.memberships.model._meta.get_field('user').remote_field.model.objects.create_user(
        username='orgadmin', email='orgadmin@example.com', password='testpass'
    )
    OrganizationMembership.objects.create(user=user, organization=org, is_admin=True)

    # Bot settings now persist via the React-facing SPA API; the classic
    # org_settings page was decommissioned.
    client.force_login(user)
    response = client.post('/api/org/settings/', data=json.dumps({
        'powerbi_tools_enabled': True,
        'powerbi_live_tools_enabled': True,
        'dbt_tools_enabled': True,
        'bigquery_tools_enabled': True,
        'bigquery_live_tools_enabled': True,
    }), content_type='application/json')

    assert response.status_code == 200
    org.refresh_from_db()
    assert org.powerbi_tools_enabled is True
    assert org.powerbi_live_tools_enabled is True
    assert org.dbt_tools_enabled is True
    assert org.bigquery_tools_enabled is True
    assert org.bigquery_live_tools_enabled is True


def test_agent_registers_dbt_and_bigquery_tools_conditionally():
    # Context-first, tool-light: no catalog *search* tools at all; each
    # integration contributes a single schema/connection tool (+ run tool).
    base_names = _registered_tool_names(get_agent())
    assert 'safe_get_dbt_item_details' not in base_names
    assert 'safe_bigquery_execute_query' not in base_names
    # The legacy search tools are gone entirely.
    assert 'safe_search_dbt_models' not in base_names
    assert 'safe_search_pb_measures' not in base_names

    class DummyBigQueryClient:
        pass

    # dbt catalog tool registers when dbt is on; the BigQuery SQL tool only when
    # the live tier is on.
    enabled_names = _registered_tool_names(
        get_agent(bigquery_client=DummyBigQueryClient(), dbt_enabled=True,
                  bigquery_live_enabled=True)
    )
    assert 'safe_get_dbt_item_details' in enabled_names
    assert 'safe_bigquery_execute_query' in enabled_names
    # Schema/list tools are no longer registered — schema is front-loaded.
    assert 'safe_bigquery_get_table_schema' not in enabled_names
    assert 'safe_search_dbt_models' not in enabled_names

    # Catalog-only BigQuery (live tier off) front-loads schema but registers NO
    # execution tool.
    catalog_only = _registered_tool_names(
        get_agent(bigquery_client=DummyBigQueryClient(), bigquery_live_enabled=False)
    )
    assert 'safe_bigquery_execute_query' not in catalog_only


def test_bigquery_sql_guardrails_reject_write_queries():
    ok, cleaned = validate_read_only_sql('SELECT 1')
    assert ok is True
    assert cleaned == 'SELECT 1'

    ok, message = validate_read_only_sql('DELETE FROM `p.d.t` WHERE TRUE')
    assert ok is False
    assert 'Only read-only' in message

    ok, message = validate_read_only_sql('SELECT 1; DROP TABLE `p.d.t`')
    assert ok is False
    assert 'forbidden' in message or 'Only one' in message


def test_bigquery_execute_query_dry_runs_and_caps_rows():
    class DryRunJob:
        total_bytes_processed = 123

    class FakeClient:
        def __init__(self):
            self.dry_run_called = False

        def dry_run_query(self, sql, maximum_bytes_billed):
            self.dry_run_called = True
            assert sql == 'SELECT 1 AS answer'
            assert maximum_bytes_billed == 1_000_000_000
            return DryRunJob()

        def execute_query(self, sql, maximum_bytes_billed):
            assert self.dry_run_called is True
            return [{'answer': 1}]

    tool = make_bigquery_tools(FakeClient())[0]
    output = tool('SELECT 1 AS answer')
    assert 'Dry-run bytes processed: 123' in output
    assert '| answer |' in output
    assert '| 1 |' in output


@pytest.mark.django_db
def test_search_pb_columns_can_scope_to_measure_dataset(org):
    Item.objects.create(
        item_id='col_1',
        item_name='Date',
        item_type='PB_COLUMN',
        service='powerbi',
        organization=org,
        dataset_id='dataset_a',
        workspace_id='workspace_a',
        dataset_name='Dataset A',
        workspace_name='Workspace A',
        table_name='Calendar',
    )
    Item.objects.create(
        item_id='col_2',
        item_name='Date',
        item_type='PB_COLUMN',
        service='powerbi',
        organization=org,
        dataset_id='dataset_b',
        workspace_id='workspace_b',
        dataset_name='Dataset B',
        workspace_name='Workspace B',
        table_name='DimDate',
    )

    from catalog.tools import search_pb_columns

    output = search_pb_columns(query='date', dataset_id='dataset_a', workspace_id='workspace_a')
    assert "DAX Reference: 'Calendar'[Date]" in output
    assert 'Dataset B' not in output


def test_powerbi_dax_guard_rejects_placeholders():
    class FakePowerBIClient:
        def execute_dax_query(self, dataset_id, dax_query, workspace_id=None):
            return {'results': [{'tables': [{'rows': [{'[Result]': 1}]}]}]}

        def get_datasets(self, workspace_id=None):
            return []

        def get_dataset_tables(self, dataset_id, workspace_id=None):
            return []

        def get_workspaces(self):
            return []

        def get_refresh_history(self, dataset_id, workspace_id=None, top=5):
            return []

        def refresh_dataset(self, dataset_id, workspace_id=None):
            return {}

    tools = {tool.__name__: tool for tool in make_powerbi_tools(FakePowerBIClient())}
    assert 'powerbi_run_dax_query' in tools
    output = tools['powerbi_run_dax_query']('dataset_a', 'EVALUATE ROW("Result", [MeasureName])')
    assert 'DAX query rejected' in output
    assert 'placeholder' in output


@pytest.mark.django_db
def test_get_dbt_sql_returns_full_sql(org):
    Item.objects.create(
        item_id='dbt_model_1',
        item_name='stg_orders',
        item_type='DBT_MODEL',
        service='dbt',
        organization=org,
        table_name='analytics.stg_orders',
        database_name='warehouse',
        expression='select * from raw.orders',
    )

    output = get_dbt_sql('stg_orders')
    assert 'Name: stg_orders' in output
    assert '```sql' in output
    assert 'select * from raw.orders' in output


@pytest.mark.django_db
def test_get_dbt_bigquery_lineage_reports_cross_system_edge(org):
    NetworkNode.objects.create(
        node_id='DBT_MODEL::m1', name='stg_orders', group='DBT_MODEL', organization=org,
    )
    NetworkNode.objects.create(
        node_id='PB_TABLE::t1', name='orders_table', group='PB_TABLE', organization=org,
    )
    NetworkEdge.objects.create(source='DBT_MODEL::m1', target='PB_TABLE::t1', organization=org)

    output = get_dbt_bigquery_lineage('DBT_MODEL::m1')
    assert 'DBT ↔ BigQuery/BI lineage for stg_orders' in output
    assert 'stg_orders (DBT_MODEL) → orders_table (PB_TABLE)' in output


# ---------------------------------------------------------------------------
# Date / future-date prompt guardrails
# ---------------------------------------------------------------------------
#
# Production regression: the bot returned future months (May–Sep) when asked
# for "transfers operated in <current_year> per month" — DAX upper bound was
# Dec 31 instead of today, so PowerBI surfaced forward bookings as if they
# were operated history. Fix lives in the system prompt as a future-date
# prohibition rule plus fully dynamic example dates.


def _capture_powerbi_system_prompt():
    """Build the agent with a stub PowerBI client and capture the system prompt.

    Monkey-patches ``Agent`` and ``make_powerbi_tools`` so we don't pull in the
    Pydantic AI runtime or hit the real PowerBI factories during a unit test.
    """
    from catalog import tools as tools_mod
    from catalog.tools import agent as agent_mod
    from catalog import powerbi_tools as powerbi_mod

    captured = {}
    original_agent = agent_mod.Agent
    original_make = powerbi_mod.make_powerbi_tools

    class FakeAgent:
        def __init__(self, *args, system_prompt=None, **kwargs):
            captured['system_prompt'] = system_prompt

        def tool_plain(self, *_a, **_k):
            pass

    agent_mod.Agent = FakeAgent
    powerbi_mod.make_powerbi_tools = lambda _client: []
    try:
        tools_mod.get_agent(powerbi_client=object())
    finally:
        agent_mod.Agent = original_agent
        powerbi_mod.make_powerbi_tools = original_make
    return captured['system_prompt']


@pytest.mark.django_db
def test_future_date_prohibition_rule_is_present():
    prompt = _capture_powerbi_system_prompt()
    # Cap-at-today rule (re-worded in the tool-light prompt).
    assert 'MUST NEVER exceed' in prompt
    for phrase in ['this year', 'YTD', 'operated']:
        assert phrase in prompt, f'missing trigger phrase: {phrase!r}'
    # And the explicit forecast opt-in list.
    for phrase in ['forecast', 'scheduled', 'upcoming']:
        assert phrase in prompt, f'missing forecast phrase: {phrase!r}'
    # Hard rule against the buggy DAX shape we saw in production.
    assert 'DATE(<year>,12,31)' in prompt


@pytest.mark.django_db
def test_powerbi_prompt_is_context_first():
    """The PowerBI addendum must tell the agent the catalog is already listed
    (no search) and route depth/live work through the two kept tools."""
    prompt = _capture_powerbi_system_prompt()
    assert 'Do NOT search' in prompt
    assert 'PowerBI catalog' in prompt
    assert 'get_pb_item_details' in prompt
    assert 'powerbi_run_dax_query' in prompt


@pytest.mark.django_db
def test_date_context_block_uses_todays_actual_date():
    """Regression: the date-context block must reflect the live UTC date so
    the model never falls back to a year from training data."""
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc)
    prompt = _capture_powerbi_system_prompt()
    assert today.strftime('%Y-%m-%d') in prompt
    assert f'The current year is {today.year}' in prompt


def test_make_safe_tool_records_args_kwargs_and_duration():
    """The capture hook must run after every tool call with the structured
    payload the debug renderer needs."""
    from catalog.tools import make_safe_tool

    captured = []

    def my_tool(query: str, limit: int = 5):
        return f'rows for {query} (limit {limit})'

    safe = make_safe_tool(my_tool, record_call=captured.append)
    result = safe('transfers operated', limit=3)

    assert result['status'] == 'success'
    assert len(captured) == 1
    entry = captured[0]
    assert entry['tool'] == 'my_tool'
    assert entry['args'] == {'query': 'transfers operated'}
    assert entry['kwargs'] == {'limit': 3}
    assert entry['status'] == 'success'
    assert entry['error'] is None
    assert isinstance(entry['duration_ms'], int)
    assert entry['duration_ms'] >= 0


def test_make_safe_tool_records_errors_with_exception_info():
    from catalog.tools import make_safe_tool

    captured = []

    def broken_tool(x: int):
        raise ValueError('boom')

    safe = make_safe_tool(broken_tool, record_call=captured.append)
    result = safe(x=1)

    assert result['status'] == 'error'
    assert len(captured) == 1
    entry = captured[0]
    assert entry['status'] == 'error'
    assert 'ValueError: boom' in entry['error']


def test_make_safe_tool_awaits_async_tools():
    import asyncio

    from catalog.tools import make_safe_tool

    captured = []

    async def my_tool(query: str):
        return f'rows for {query}'

    safe = make_safe_tool(my_tool, record_call=captured.append)
    result = asyncio.run(safe('transfers operated'))

    assert result == {'status': 'success', 'data': 'rows for transfers operated'}
    assert len(captured) == 1
    assert captured[0]['tool'] == 'my_tool'
    assert captured[0]['args'] == {'query': 'transfers operated'}
    assert captured[0]['status'] == 'success'


def test_render_debug_section_emits_dax_sql_and_stats():
    from catalog.services.debug_render import build_debug_payload, render_debug_section

    calls = [
        {
            'tool': 'search_pb_measures',
            'args': {'query': 'transfers operated'},
            'kwargs': {'limit': 10},
            'duration_ms': 12,
            'status': 'success',
            'error': None,
        },
        {
            'tool': 'powerbi_run_dax_query',
            'args': {
                'dataset_id': 'driver_operations',
                'workspace_id': 'ws-92',
                'dax_query': 'EVALUATE ROW("Result", [Transfers Operated {O}])',
            },
            'kwargs': {},
            'duration_ms': 1204,
            'status': 'success',
            'error': None,
        },
        {
            'tool': 'bigquery_execute_query',
            'args': {'sql': 'SELECT COUNT(*) FROM `p.d.t`'},
            'kwargs': {},
            'duration_ms': 320,
            'status': 'success',
            'error': None,
        },
    ]

    payload = build_debug_payload(calls)
    assert payload['stats']['tool_call_count'] == 3
    assert payload['stats']['dax_query_count'] == 1
    assert payload['stats']['sql_query_count'] == 1
    assert payload['stats']['total_duration_ms'] == 12 + 1204 + 320
    assert payload['stats']['error_count'] == 0
    assert payload['dax_queries'][0]['dataset_id'] == 'driver_operations'
    assert payload['sql_queries'][0]['sql'].startswith('SELECT COUNT')

    rendered = render_debug_section(payload)
    assert '**🔧 Debug**' in rendered
    assert '`search_pb_measures`' in rendered
    assert '```dax' in rendered
    assert 'EVALUATE ROW("Result", [Transfers Operated {O}])' in rendered
    assert '```sql' in rendered
    assert 'SELECT COUNT(*) FROM `p.d.t`' in rendered
    assert '3 tool calls' in rendered
    assert '1 DAX' in rendered


def test_render_debug_section_is_empty_when_no_calls():
    """No tools fired (e.g. greeting) → no debug block. Avoids littering
    every reply."""
    from catalog.services.debug_render import build_debug_payload, render_debug_section
    assert render_debug_section(build_debug_payload([])) == ''


def _capture_bigquery_system_prompt():
    """Same trick as ``_capture_powerbi_system_prompt`` but for BigQuery — used
    to assert that the BigQuery addendum carries the multi-metric STEP 0 rules.
    """
    from catalog import tools as tools_mod
    from catalog.tools import agent as agent_mod
    from catalog import bigquery_tools as bq_mod

    captured = {}
    original_agent = agent_mod.Agent
    original_make = bq_mod.make_bigquery_tools

    class FakeAgent:
        def __init__(self, *args, system_prompt=None, **kwargs):
            captured['system_prompt'] = system_prompt

        def tool_plain(self, *_a, **_k):
            pass

    agent_mod.Agent = FakeAgent
    bq_mod.make_bigquery_tools = lambda _client: []
    try:
        tools_mod.get_agent(bigquery_client=object(), dbt_enabled=True,
                            bigquery_live_enabled=True)
    finally:
        agent_mod.Agent = original_agent
        bq_mod.make_bigquery_tools = original_make
    return captured['system_prompt']


@pytest.mark.django_db
def test_powerbi_handles_multi_metric_requests():
    """The PowerBI addendum must still teach the agent to resolve each metric
    of a "compare A and B" request separately before running any DAX."""
    prompt = _capture_powerbi_system_prompt()
    assert 'Multiple metrics' in prompt
    assert 'compare A and B' in prompt
    assert 'Do not silently drop' in prompt


@pytest.mark.django_db
def test_powerbi_year_sanity_check_catches_typos():
    """A user typo like "2006" when today is 2026 should trigger a question,
    not a silent year substitution."""
    prompt = _capture_powerbi_system_prompt()
    assert 'Did you mean' in prompt


@pytest.mark.django_db
def test_bigquery_prompt_is_context_first():
    """The BigQuery addendum must tell the agent the schema is already listed
    (no list/describe) and route execution through bigquery_execute_query."""
    prompt = _capture_bigquery_system_prompt()
    assert 'NOT list or describe' in prompt
    assert 'BigQuery schema' in prompt
    assert 'bigquery_execute_query' in prompt


@pytest.mark.django_db
def test_bigquery_year_sanity_check_catches_typos():
    prompt = _capture_bigquery_system_prompt()
    assert 'Did you mean' in prompt


def _make_measure(org, item_id, dataset_id, dataset_name, expression,
                  *, name='Failed Quotes', workspace_id='ws_comm',
                  workspace_name='05. Commercial', connected_reports=0,
                  connected_visuals=0, description='', is_primary=False):
    return Item.objects.create(
        item_id=item_id,
        item_name=name,
        item_type='PB_MEASURE',
        service='powerbi',
        organization=org,
        dataset_id=dataset_id,
        workspace_id=workspace_id,
        dataset_name=dataset_name,
        workspace_name=workspace_name,
        table_name='Measures Table',
        expression=expression,
        connected_reports=connected_reports,
        connected_visuals=connected_visuals,
        description=description,
        is_group_primary=is_primary,
    )


@pytest.mark.django_db
def test_get_pb_measure_schema_collapses_copies_to_group_primary(org):
    """A name that recurs across datasets (one source + EXTERNALMEASURE
    re-exports) must resolve straight to the group's primary_item — no staged
    workspace/dataset disambiguation that would burn the agent's tool calls."""
    from catalog.tools import get_pb_measure_schema

    # The curated source measure (real DAX) is the group's primary_item.
    _make_measure(
        org, 'm_src', 'ds_zoe', 'Zoe',
        "CALCULATE(DISTINCTCOUNT('01__Quotes_analysis'[session_id]))",
        connected_reports=8, connected_visuals=19,
        description='Distinct count of failed quote sessions.', is_primary=True,
    )
    # Two thin DirectQuery re-exports sharing the same name → same ItemGroup.
    _make_measure(
        org, 'm_mkt', 'ds_mkt', 'marketing',
        'EXTERNALMEASURE("Failed Quotes", INTEGER, "DirectQuery to AS - Zoe")',
        connected_reports=8,
    )
    _make_measure(
        org, 'm_seo', 'ds_seo', 'SEO_reporting',
        'EXTERNALMEASURE("Failed Quotes", INTEGER, "DirectQuery to AS - Zoe")',
    )

    out = get_pb_measure_schema('Failed Quotes')

    assert '## Measure: **Failed Quotes**' in out
    assert 'DISTINCTCOUNT' in out                 # the primary's real DAX
    assert 'matches measures in' not in out       # no workspace/dataset cascade
    assert 'Ask the user which' not in out


@pytest.mark.django_db
def test_get_pb_measure_schema_falls_back_to_top_priority_without_primary(org):
    """When a group has no pinned primary_item, the collapse still yields ONE
    representative — the highest-priority member (most connected reports)."""
    from catalog.tools import get_pb_measure_schema

    _make_measure(
        org, 'm_a', 'ds_a', 'Dataset A', 'SUM(FactA[failed])',
        name='Cutoff Rate', connected_reports=11,
    )
    _make_measure(
        org, 'm_b', 'ds_b', 'Dataset B', 'SUM(FactB[failed])',
        name='Cutoff Rate', connected_reports=3,
    )

    out = get_pb_measure_schema('Cutoff Rate')

    assert '## Measure: **Cutoff Rate**' in out
    assert 'SUM(FactA[failed])' in out            # the more-used member wins
    assert 'SUM(FactB[failed])' not in out
    assert 'matches measures in' not in out


@pytest.mark.django_db
def test_format_reminder_example_dates_track_today():
    """The Last Week / Previous Week strings in the example must be computed
    from today, not hardcoded — otherwise they drift relative to "Today (UTC)"
    and confuse the model."""
    from datetime import datetime, timedelta, timezone

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    iso_wd = today.isoweekday()
    this_week_mon = today - timedelta(days=iso_wd - 1)
    last_week_mon = this_week_mon - timedelta(days=7)
    last_week_sun = this_week_mon - timedelta(days=1)

    prompt = _capture_powerbi_system_prompt()
    assert last_week_mon.strftime('%d %b %Y') in prompt
    assert last_week_sun.strftime('%d %b %Y') in prompt


# ---------------------------------------------------------------------------
# Analytics tool: get_pb_usage_analytics
# ---------------------------------------------------------------------------


def _usage_measure(org, item_id, name, reports, *, dataset='DS', workspace='WS A',
                   visuals=0):
    """A PB_MEASURE whose connected_reports_json lists ``reports`` (list of
    (id, name) tuples)."""
    return Item.objects.create(
        item_id=item_id,
        item_name=name,
        item_type='PB_MEASURE',
        service='powerbi',
        organization=org,
        dataset_name=dataset,
        workspace_name=workspace,
        connected_reports=len(reports),
        connected_visuals=visuals,
        is_unused=(len(reports) == 0),
        connected_reports_json=[
            {'id': rid, 'name': rname, 'url': ''} for rid, rname in reports
        ],
    )


@pytest.mark.django_db
def test_pb_usage_analytics_report_mode_lists_measures(org):
    """Given a report name, the tool lists every measure that feeds it —
    inverted straight from each measure's connected_reports_json."""
    from catalog.tools import get_pb_usage_analytics

    Item.objects.create(
        item_id='rep_sales', item_name='Sales Report', item_type='PB_REPORT',
        service='powerbi', organization=org, workspace_name='WS A',
        connected_report_pages=4, connected_visuals=30,
    )
    _usage_measure(org, 'm_rev', 'Revenue', [('rep_sales', 'Sales Report')], visuals=12)
    _usage_measure(org, 'm_mar', 'Margin', [('rep_sales', 'Sales Report')], visuals=5)
    _usage_measure(org, 'm_other', 'Headcount', [('rep_hr', 'HR Report')])

    out = get_pb_usage_analytics(report_name='Sales Report')

    assert 'Report usage: **Sales Report**' in out
    assert 'Measures used in this report (2)' in out
    assert 'Revenue' in out
    assert 'Margin' in out
    assert 'Headcount' not in out          # feeds a different report


@pytest.mark.django_db
def test_pb_usage_analytics_measure_mode_lists_reports(org):
    """Given a measure name, the tool lists every report that uses it,
    unioned across the measure group's instances."""
    from catalog.tools import get_pb_usage_analytics

    _usage_measure(org, 'm_a', 'Revenue',
                   [('r1', 'Sales Report'), ('r2', 'Exec Dashboard')],
                   dataset='Sales', visuals=9)
    # Same name in another dataset → same measure group; union its reports.
    _usage_measure(org, 'm_b', 'Revenue', [('r3', 'Finance Report')],
                   dataset='Finance')

    out = get_pb_usage_analytics(measure_name='Revenue')

    assert 'Measure usage: **Revenue**' in out
    assert 'Reports using this measure (3)' in out
    for rep in ('Sales Report', 'Exec Dashboard', 'Finance Report'):
        assert rep in out


@pytest.mark.django_db
def test_pb_usage_analytics_overview_ranks_and_flags_unused(org):
    """No args → catalog-wide overview: top measures by report coverage, an
    unused section, and the full measure index."""
    from catalog.tools import get_pb_usage_analytics

    Item.objects.create(
        item_id='rep1', item_name='Sales Report', item_type='PB_REPORT',
        service='powerbi', organization=org, workspace_name='WS A',
    )
    _usage_measure(org, 'top', 'Revenue',
                   [('rep1', 'Sales Report'), ('rep2', 'Exec Dashboard')], visuals=20)
    _usage_measure(org, 'mid', 'Margin', [('rep1', 'Sales Report')], visuals=4)
    _usage_measure(org, 'dead', 'Legacy KPI', [])    # unused

    out = get_pb_usage_analytics()

    assert 'PowerBI usage analytics' in out
    assert 'Top' in out and 'measures by report coverage' in out
    # Revenue (2 reports) must rank above Margin (1 report).
    assert out.index('Revenue') < out.index('Margin')
    assert 'Unused measures (1)' in out
    assert 'Legacy KPI' in out
    assert 'All measures (3)' in out


@pytest.mark.django_db
def test_pb_usage_analytics_workspace_filter(org):
    """The workspace substring scopes the overview to one workspace."""
    from catalog.tools import get_pb_usage_analytics

    _usage_measure(org, 'a', 'Alpha', [('r1', 'Rep One')], workspace='Commercial')
    _usage_measure(org, 'b', 'Beta', [('r2', 'Rep Two')], workspace='Operations')

    out = get_pb_usage_analytics(workspace='Commercial')
    assert 'Alpha' in out
    assert 'Beta' not in out


@pytest.mark.django_db
def test_pb_usage_analytics_unknown_report_is_honest(org):
    from catalog.tools import get_pb_usage_analytics
    out = get_pb_usage_analytics(report_name='Nope')
    assert 'No PowerBI report matches' in out


def test_pb_usage_analytics_tool_registered_with_powerbi_provider():
    """The PowerBI provider must expose get_pb_usage_analytics as a tool."""
    from catalog.tools.assistant import powerbi as pb_provider
    names = {t.__name__ for t in pb_provider.build_tools(None, client=None)}
    assert 'get_pb_usage_analytics' in names
    assert 'get_pb_item_details' in names


def test_base_agent_has_only_lineage_and_no_resolve_tool():
    """With no integration enabled the only shared tool is get_lineage. Name
    resolution is done by reading the front-loaded listing, so there is no
    resolve tool to loop on."""
    names = _registered_tool_names(get_agent())
    assert 'safe_get_lineage' in names
    assert 'safe_resolve_catalog_items' not in names


def test_powerbi_catalog_tools_register_without_live_client():
    """`powerbi_tools_enabled=True` is the on-by-default PowerBI *catalog* tier:
    it registers the DB-only profiler + usage-analytics tools WITHOUT a live
    client, so the agent can answer "measures used in the Ops reports" with no
    PowerBI REST access. The live DAX tool stays gated on the live client."""
    names = _registered_tool_names(get_agent(powerbi_tools_enabled=True))
    assert 'safe_get_pb_item_details' in names
    assert 'safe_get_pb_usage_analytics' in names
    # No live client passed → live DAX must NOT be registered.
    assert 'safe_powerbi_run_dax_query' not in names


def test_powerbi_catalog_off_drops_profiler_tools():
    """With the catalog tier off and no client, the PowerBI profiler/usage tools
    are absent — only the shared resolver/lineage remain. This is the "everything
    untoggled" state that left the agent unable to profile."""
    names = _registered_tool_names(get_agent(powerbi_tools_enabled=False))
    assert 'safe_get_pb_item_details' not in names
    assert 'safe_get_pb_usage_analytics' not in names
    # Only the shared lineage tool remains.
    assert 'safe_get_lineage' in names


@pytest.mark.django_db
def test_prompts_mention_the_new_analytics_tools():
    prompt = _capture_powerbi_system_prompt()
    assert 'get_pb_usage_analytics' in prompt
    # Name resolution is via the front-loaded listing — no resolve tool.
    assert 'resolve_catalog_items' not in prompt
