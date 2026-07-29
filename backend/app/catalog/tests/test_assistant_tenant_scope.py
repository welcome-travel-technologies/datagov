import inspect

import pytest
from django.core.cache import cache

from catalog.models import (
    DataPerson,
    Department,
    Item,
    ItemGroup,
    NetworkEdge,
    NetworkNode,
    Organization,
)
from catalog.tools.catalog_search import search_pb_columns
from catalog.tools.dbt import get_dbt_sql
from catalog.tools.lineage import get_lineage
from catalog.tools.organization_scope import OrganizationScopeRequired
from catalog.tools.pb_schema_bundle import get_pb_measure_schema


def _item(org, item_id, name, item_type, service):
    return Item.objects.create(
        item_id=item_id,
        item_name=name,
        item_type=item_type,
        service=service,
        organization=org,
        connected_reports_json=[],
    )


@pytest.mark.django_db
def test_chat_read_tools_and_frontloaded_context_are_exactly_org_scoped(
        org, monkeypatch):
    """The actual chat-agent assembly must capture org without a tool argument."""
    foreign = Organization.objects.create(name='Foreign tenant')

    own_measure = _item(org, 'own-measure', 'Own Metric', 'PB_MEASURE', 'powerbi')
    own_measure.connected_reports_json = [
        {'id': 'own-report', 'name': 'Own Report', 'url': ''},
    ]
    own_measure.connected_reports = 1
    own_measure.save(update_fields=['connected_reports_json', 'connected_reports'])
    _item(org, 'own-report', 'Own Report', 'PB_REPORT', 'powerbi')
    _item(org, 'own-dbt', 'Own Model', 'DBT_MODEL', 'dbt')
    corrupt_measure = _item(
        org, 'corrupt-measure', 'Corrupt Governance Metric',
        'PB_MEASURE', 'powerbi',
    )

    _item(
        foreign, 'foreign-measure', 'Foreign Secret Metric',
        'PB_MEASURE', 'powerbi',
    )
    _item(
        foreign, 'foreign-report', 'Foreign Secret Report',
        'PB_REPORT', 'powerbi',
    )
    _item(
        foreign, 'foreign-dbt', 'Foreign Secret Model',
        'DBT_MODEL', 'dbt',
    )
    own_owner = DataPerson.objects.create(
        name='Own Catalog Owner',
        organization=org,
    )
    assert own_measure.item_group_id is not None
    ItemGroup.objects.filter(pk=own_measure.item_group_id).update(
        ownership_person=own_owner,
    )
    foreign_owner = DataPerson.objects.create(
        name='Foreign Owner Secret',
        organization=foreign,
    )
    foreign_steward = DataPerson.objects.create(
        name='Foreign Steward Secret',
        organization=foreign,
    )
    foreign_department = Department.objects.create(
        name='Foreign Department Secret',
        organization=foreign,
    )
    assert corrupt_measure.item_group_id is not None
    # Simulate legacy/corrupt data that bypassed application validation. The
    # item and group belong to this tenant, but the related owner does not.
    ItemGroup.objects.filter(pk=corrupt_measure.item_group_id).update(
        ownership_person=foreign_owner,
        steward=foreign_steward,
        ownership_department=foreign_department,
        custom_description='Same-tenant custom description remains visible.',
    )

    NetworkNode.objects.create(
        node_id='DBT_MODEL::own-node',
        name='Shared Node',
        group='DBT_MODEL',
        organization=org,
    )
    NetworkNode.objects.create(
        node_id='PB_REPORT::own-consumer',
        name='Own Consumer',
        group='PB_REPORT',
        organization=org,
    )
    NetworkEdge.objects.create(
        source='DBT_MODEL::own-node',
        target='PB_REPORT::own-consumer',
        organization=org,
    )
    NetworkNode.objects.create(
        node_id='DBT_MODEL::foreign-node',
        name='Shared Node',
        group='DBT_MODEL',
        organization=foreign,
    )
    NetworkNode.objects.create(
        node_id='PB_REPORT::foreign-consumer',
        name='Foreign Secret Consumer',
        group='PB_REPORT',
        organization=foreign,
    )
    NetworkEdge.objects.create(
        source='DBT_MODEL::foreign-node',
        target='PB_REPORT::foreign-consumer',
        organization=foreign,
    )

    from catalog.tools import agent as agent_module

    registered = {}
    captured = {}

    class FakeAgent:
        def __init__(self, *args, system_prompt=None, **kwargs):
            captured['system_prompt'] = system_prompt

        def tool_plain(self, tool):
            registered[tool.__name__] = tool

    cache.clear()
    monkeypatch.setattr(agent_module, 'Agent', FakeAgent)
    agent_module.get_agent(
        org=org,
        powerbi_tools_enabled=True,
        dbt_enabled=True,
    )

    prompt = captured['system_prompt']
    assert 'Own Metric' in prompt
    assert 'Own Model' in prompt
    assert 'Foreign Secret' not in prompt
    assert 'Foreign Owner Secret' not in prompt

    for name in (
        'safe_get_lineage',
        'safe_get_pb_item_details',
        'safe_get_pb_usage_analytics',
        'safe_get_dbt_item_details',
    ):
        assert name in registered
        assert 'organization_id' not in inspect.signature(
            registered[name],
        ).parameters

    lineage = registered['safe_get_lineage']('Shared Node')
    assert lineage['status'] == 'success'
    assert 'Own Consumer' in lineage['data']
    assert 'Foreign Secret Consumer' not in lineage['data']

    analytics = registered['safe_get_pb_usage_analytics']()
    assert analytics['status'] == 'success'
    assert 'Own Metric' in analytics['data']
    assert 'Foreign Secret Metric' not in analytics['data']
    assert 'Foreign Owner Secret' not in analytics['data']

    own_usage = registered['safe_get_pb_usage_analytics'](
        measure_name='Own Metric',
    )
    assert own_usage['status'] == 'success'
    assert 'owner: Own Catalog Owner' in own_usage['data']
    assert 'Foreign Owner Secret' not in own_usage['data']

    corrupt_usage = registered['safe_get_pb_usage_analytics'](
        measure_name='Corrupt Governance Metric',
    )
    assert corrupt_usage['status'] == 'success'
    assert 'Foreign Owner Secret' not in corrupt_usage['data']

    foreign_pb = registered['safe_get_pb_item_details'](
        'Foreign Secret Metric',
    )
    assert foreign_pb['status'] == 'success'
    assert 'No catalog item' in foreign_pb['data']

    corrupt_profile = registered['safe_get_pb_item_details'](
        'Corrupt Governance Metric',
    )
    assert corrupt_profile['status'] == 'success'
    assert 'Foreign Owner Secret' not in corrupt_profile['data']
    assert 'Foreign Steward Secret' not in corrupt_profile['data']
    assert 'Foreign Department Secret' not in corrupt_profile['data']
    assert 'Same-tenant custom description remains visible.' in corrupt_profile['data']

    foreign_dbt = registered['safe_get_dbt_item_details'](
        'Foreign Secret Model',
    )
    assert foreign_dbt['status'] == 'success'
    assert 'No dbt model' in foreign_dbt['data']


@pytest.mark.django_db
@pytest.mark.parametrize(
    'call',
    [
        lambda: get_lineage('anything'),
        lambda: search_pb_columns('anything'),
        lambda: get_dbt_sql('anything'),
        lambda: get_pb_measure_schema('anything'),
    ],
    ids=['lineage', 'catalog-search', 'dbt', 'pb-schema'],
)
def test_unbound_catalog_read_helpers_fail_before_querying(
        django_assert_num_queries, call):
    with django_assert_num_queries(0):
        with pytest.raises(OrganizationScopeRequired):
            call()


def test_bigquery_context_without_org_never_reads_shared_cache(monkeypatch):
    from catalog.tools.assistant import bigquery

    def cache_must_not_be_used(*args, **kwargs):
        raise AssertionError('no-org context must fail before cache lookup')

    monkeypatch.setattr(bigquery, 'cached_context', cache_must_not_be_used)
    assert bigquery.build_context(
        None,
        client=object(),
        scope_ids=['tenant-dataset'],
    ) == ''
