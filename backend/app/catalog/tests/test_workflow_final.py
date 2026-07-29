"""
Integration tests for the cross-tool bridge step.

Loads a small fixture of dbt + PowerBI Items into the test DB, runs
``build_cross_tool_bridges`` directly (the same code path that
``manage.py rebridge`` and the workflow final command exercise), and asserts
the right NetworkEdges show up with the expected ``bridge_reason``.
"""
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection

from catalog.models import (
    Item, NetworkEdge, NetworkNode, Organization, Summary,
)
from catalog.services.bridge_builder import (
    BridgeTenantCollision,
    build_cross_tool_bridges,
)


def _make_pbi_table(item_id, name, *, bq_project=None, bq_schema=None, bq_source_name=None, organization=None):
    return Item.objects.create(
        item_id=item_id,
        item_name=name,
        item_type='PB_TABLE',
        service='powerbi',
        bq_project=bq_project,
        bq_schema=bq_schema,
        bq_source_name=bq_source_name,
        organization=organization,
    )


def _make_pbi_column(item_id, name, table_name, *, organization=None):
    return Item.objects.create(
        item_id=item_id,
        item_name=name,
        item_type='PB_COLUMN',
        service='powerbi',
        table_name=table_name,
        organization=organization,
    )


def _make_dbt_model(item_id, name, *, database, schema, alias, dataset_id, organization=None):
    return Item.objects.create(
        item_id=item_id,
        item_name=name,
        item_type='DBT_MODEL',
        service='dbt',
        database_name=database,
        schema_name=schema,
        alias=alias,
        table_name=f'{schema}.{alias}',
        dataset_id=dataset_id,
        organization=organization,
    )


def _make_dbt_column(item_id, name, *, dataset_id, organization=None):
    return Item.objects.create(
        item_id=item_id,
        item_name=name,
        item_type='DBT_COLUMN',
        service='dbt',
        dataset_id=dataset_id,
        organization=organization,
    )


@pytest.mark.django_db
def test_renamed_pbi_table_still_bridges_via_bq_fqn(org):
    """The whole reason this plan exists: PBI display name 'Driver' must still
    bridge to dbt model 'dim_driver' because the BQ FQN matches.
    """
    _make_dbt_model('dbt_dim_driver', 'dim_driver',
                    database='proj', schema='analytics', alias='dim_driver',
                    dataset_id='ds_dim_driver', organization=org)
    _make_pbi_table('pbi_driver', 'Driver',
                    bq_project='proj', bq_schema='analytics',
                    bq_source_name='dim_driver', organization=org)

    with connection.cursor() as cursor:
        stats = build_cross_tool_bridges(cursor, org.id)

    assert stats['table_bridges'] == 1
    assert stats['by_reason'].get('bq_fqn') == 1

    edge = NetworkEdge.objects.get(
        source='DBT_MODEL::dbt_dim_driver',
        target='PB_TABLE::pbi_driver',
    )
    assert edge.bridge_reason == 'bq_fqn'


@pytest.mark.django_db
def test_column_bridges_emitted_when_table_matches(org):
    """Once the table-level bridge is established, every shared column name
    should produce a column-level bridge edge with the same reason.
    """
    _make_dbt_model('dbt_m', 'dim_driver',
                    database='proj', schema='analytics', alias='dim_driver',
                    dataset_id='ds_m', organization=org)
    _make_dbt_column(
        'dbt_c1', 'driver_id', dataset_id='ds_m', organization=org,
    )
    _make_dbt_column(
        'dbt_c2', 'name', dataset_id='ds_m', organization=org,
    )
    _make_dbt_column(
        'dbt_c3', 'orphan_col', dataset_id='ds_m', organization=org,
    )

    _make_pbi_table('pbi_t', 'Driver',
                    bq_project='proj', bq_schema='analytics',
                    bq_source_name='dim_driver', organization=org)
    # PBI columns reference parent table by display name, not item_id.
    _make_pbi_column(
        'pbi_c1', 'Driver_ID', table_name='Driver', organization=org,
    )
    _make_pbi_column(
        'pbi_c2', 'name', table_name='Driver', organization=org,
    )

    with connection.cursor() as cursor:
        stats = build_cross_tool_bridges(cursor, org.id)

    assert stats['column_bridges'] == 2  # driver_id + name; orphan_col has no PBI peer
    assert NetworkEdge.objects.filter(
        source='DBT_COLUMN::dbt_c1', target='PB_COLUMN::pbi_c1', bridge_reason='bq_fqn'
    ).exists()
    assert NetworkEdge.objects.filter(
        source='DBT_COLUMN::dbt_c2', target='PB_COLUMN::pbi_c2', bridge_reason='bq_fqn'
    ).exists()


@pytest.mark.django_db
def test_name_match_works_when_no_bq_fqn(org):
    """Regression: PBI tables with no bq_* triple still bridge by display name."""
    _make_dbt_model('dbt_m', 'dim_driver',
                    database='proj', schema='analytics', alias='dim_driver',
                    dataset_id='ds_m', organization=org)
    _make_pbi_table(
        'pbi_t', 'dim_driver', organization=org,
    )  # no bq_* triple

    with connection.cursor() as cursor:
        stats = build_cross_tool_bridges(cursor, org.id)

    assert stats['table_bridges'] == 1
    edge = NetworkEdge.objects.get(source='DBT_MODEL::dbt_m', target='PB_TABLE::pbi_t')
    assert edge.bridge_reason == 'name_tail'


@pytest.mark.django_db
def test_unmatched_pbi_yields_no_bridge(org):
    _make_dbt_model('dbt_m', 'dim_driver',
                    database='proj', schema='analytics', alias='dim_driver',
                    dataset_id='ds_m', organization=org)
    _make_pbi_table('pbi_t', 'totally_unrelated', organization=org)

    with connection.cursor() as cursor:
        stats = build_cross_tool_bridges(cursor, org.id)

    assert stats['table_bridges'] == 0
    assert not NetworkEdge.objects.filter(target='PB_TABLE::pbi_t').exists()


@pytest.mark.django_db
def test_rerun_replaces_old_bridge_edges(org):
    """The bridge step is idempotent: re-running it deletes prior bridges
    before building the fresh set.
    """
    dbt = _make_dbt_model('dbt_m', 'dim_driver',
                          database='proj', schema='analytics', alias='dim_driver',
                          dataset_id='ds_m', organization=org)
    pbi = _make_pbi_table('pbi_t', 'Driver',
                          bq_project='proj', bq_schema='analytics',
                          bq_source_name='dim_driver', organization=org)

    with connection.cursor() as cursor:
        build_cross_tool_bridges(cursor, org.id)
        # Mutate: drop the FQN so the next run should match by name only.
        Item.objects.filter(pk=pbi.pk).update(
            bq_project=None, bq_schema=None, bq_source_name=None,
            item_name='dim_driver',
        )
        stats = build_cross_tool_bridges(cursor, org.id)

    assert stats['table_bridges'] == 1
    edge = NetworkEdge.objects.get(source='DBT_MODEL::dbt_m', target='PB_TABLE::pbi_t')
    assert edge.bridge_reason == 'name_tail'
    # And there's only one bridge edge — the old one didn't pile up.
    assert NetworkEdge.objects.filter(
        source='DBT_MODEL::dbt_m', target='PB_TABLE::pbi_t',
    ).count() == 1


@pytest.mark.django_db
def test_bridge_rebuild_is_exactly_organization_scoped(org):
    other_org = Organization.objects.create(name='Other Org')
    _make_dbt_model(
        'dbt_scoped', 'dim_driver',
        database='proj', schema='analytics', alias='dim_driver',
        dataset_id='ds_scoped', organization=org,
    )
    _make_pbi_table(
        'pbi_scoped', 'Driver',
        bq_project='proj', bq_schema='analytics',
        bq_source_name='dim_driver', organization=org,
    )
    foreign_edge = NetworkEdge.objects.create(
        source='DBT_MODEL::foreign',
        target='PB_TABLE::foreign',
        organization=other_org,
        bridge_reason='name_tail',
    )

    with connection.cursor() as cursor:
        stats = build_cross_tool_bridges(cursor, org.id)

    assert stats['table_bridges'] == 1
    assert NetworkEdge.objects.filter(
        source='DBT_MODEL::dbt_scoped',
        target='PB_TABLE::pbi_scoped',
        organization=org,
    ).exists()
    assert NetworkEdge.objects.filter(pk=foreign_edge.pk).exists()


@pytest.mark.django_db
def test_bridge_match_never_crosses_organizations(org):
    other_org = Organization.objects.create(name='Other Org')
    _make_pbi_table(
        'pbi_scoped', 'Driver',
        bq_project='proj', bq_schema='analytics',
        bq_source_name='dim_driver', organization=org,
    )
    _make_dbt_model(
        'dbt_foreign', 'dim_driver',
        database='proj', schema='analytics', alias='dim_driver',
        dataset_id='ds_foreign', organization=other_org,
    )

    with connection.cursor() as cursor:
        stats = build_cross_tool_bridges(cursor, org.id)

    assert stats['table_bridges'] == 0
    assert not NetworkEdge.objects.filter(
        source='DBT_MODEL::dbt_foreign',
        target='PB_TABLE::pbi_scoped',
    ).exists()


@pytest.mark.django_db
def test_foreign_node_collision_aborts_before_bridge_cleanup(org):
    other_org = Organization.objects.create(name='Other Org')
    _make_dbt_model(
        'dbt_scoped', 'dim_driver',
        database='proj', schema='analytics', alias='dim_driver',
        dataset_id='ds_scoped', organization=org,
    )
    _make_pbi_table(
        'pbi_scoped', 'Driver',
        bq_project='proj', bq_schema='analytics',
        bq_source_name='dim_driver', organization=org,
    )
    NetworkNode.objects.create(
        node_id='DBT_MODEL::dbt_scoped',
        name='Foreign claim',
        group='DBT_MODEL',
        organization=other_org,
    )
    old_edge = NetworkEdge.objects.create(
        source='DBT_MODEL::old',
        target='PB_TABLE::old',
        organization=org,
        bridge_reason='name_tail',
    )

    with connection.cursor() as cursor:
        with pytest.raises(BridgeTenantCollision):
            build_cross_tool_bridges(cursor, org.id)

    assert NetworkEdge.objects.filter(pk=old_edge.pk).exists()


@pytest.mark.django_db
def test_foreign_edge_collision_aborts_before_bridge_cleanup(org):
    other_org = Organization.objects.create(name='Other Org')
    _make_dbt_model(
        'dbt_scoped', 'dim_driver',
        database='proj', schema='analytics', alias='dim_driver',
        dataset_id='ds_scoped', organization=org,
    )
    _make_pbi_table(
        'pbi_scoped', 'Driver',
        bq_project='proj', bq_schema='analytics',
        bq_source_name='dim_driver', organization=org,
    )
    NetworkEdge.objects.create(
        source='DBT_MODEL::dbt_scoped',
        target='PB_TABLE::pbi_scoped',
        organization=other_org,
        bridge_reason='name_tail',
    )
    old_edge = NetworkEdge.objects.create(
        source='DBT_MODEL::old',
        target='PB_TABLE::old',
        organization=org,
        bridge_reason='name_tail',
    )

    with connection.cursor() as cursor:
        with pytest.raises(BridgeTenantCollision):
            build_cross_tool_bridges(cursor, org.id)

    assert NetworkEdge.objects.filter(pk=old_edge.pk).exists()


@pytest.mark.django_db
def test_workflow_final_replaces_only_scoped_summary(org):
    other_org = Organization.objects.create(name='Other Org')
    Item.objects.create(
        item_id='measure_scoped',
        item_name='Scoped measure',
        item_type='PB_MEASURE',
        organization=org,
        deleted=False,
        is_unused=True,
    )
    Item.objects.create(
        item_id='report_scoped',
        item_name='Scoped report',
        item_type='PB_REPORT',
        organization=org,
        deleted=False,
    )
    Item.objects.create(
        item_id='measure_foreign',
        item_name='Foreign measure',
        item_type='PB_MEASURE',
        organization=other_org,
        deleted=False,
    )
    Summary.objects.create(
        organization=org,
        total_measures=99,
        unused_measures=99,
        total_columns=99,
        unused_columns=99,
        total_reports=99,
    )
    foreign_summary = Summary.objects.create(
        organization=other_org,
        total_measures=7,
        unused_measures=3,
        total_columns=8,
        unused_columns=4,
        total_reports=9,
    )

    call_command('run_workflow_final', organization_id=org.id)

    scoped = Summary.objects.get(organization=org)
    assert scoped.total_measures == 1
    assert scoped.unused_measures == 1
    assert scoped.total_columns == 0
    assert scoped.total_reports == 1
    foreign_summary.refresh_from_db()
    assert foreign_summary.total_measures == 7
    assert foreign_summary.total_reports == 9


@pytest.mark.django_db
def test_workflow_final_requires_existing_organization():
    with pytest.raises(CommandError):
        call_command('run_workflow_final')
    with pytest.raises(CommandError, match='does not exist'):
        call_command('run_workflow_final', organization_id=999999)
