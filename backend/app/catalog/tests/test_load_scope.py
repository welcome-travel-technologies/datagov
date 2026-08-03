import csv
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.test import override_settings
from django.utils import timezone

from catalog.models import (
    IntegrationSource,
    Item,
    NetworkEdge,
    NetworkNode,
    Organization,
    Summary,
)
from catalog.services.load_scope import (
    assert_incoming_identities_available,
    catalog_load_session_lock,
    read_incoming_identities,
    require_grouping_commit_boundary,
    require_exact_load_scope,
)


ITEM_HEADERS = [
    'item_id', 'lineage_tag', 'item_name', 'item_type', 'item_service',
    'description', 'workspace_id', 'workspace_name', 'dataset_id',
    'dataset_name', 'table_name', 'datatype', 'column_type', 'expression',
    'formatstring', 'is_unused', 'connected_reports',
    'connected_report_pages', 'connected_visuals', 'connected_measures',
    'connected_columns', 'connected_tables', 'web_url',
]
GRAPH_HEADERS = [
    'source_id', 'source', 'source_type',
    'target_id', 'target', 'target_type', 'workspace_id',
]
# The transforms also emit these two; they are optional so older CSVs load.
GRAPH_HEADERS_FULL = GRAPH_HEADERS + ['edge_kind', 'lineage_type']


def _source(org, source_type, name):
    return IntegrationSource.objects.create(
        organization=org,
        source_type=source_type,
        name=name,
        is_active=True,
    )


def _write_csv(path, headers, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def _item_row(item_id, name, item_type, service):
    return {
        header: (
            item_id if header == 'item_id'
            else name if header == 'item_name'
            else item_type if header == 'item_type'
            else service if header == 'item_service'
            else 'False' if header == 'is_unused'
            else '0' if header.startswith('connected_')
            else ''
        )
        for header in ITEM_HEADERS
    }


@pytest.mark.django_db
def test_scope_validation_is_exact_and_rejects_ambiguous_domains():
    org = Organization.objects.create(name='Org')
    other = Organization.objects.create(name='Other')
    source = _source(org, 'powerbi_fabric', 'Fabric')
    wrong_org_source = _source(other, 'powerbi_fabric', 'Other Fabric')
    wrong_type = _source(org, 'dbt', 'dbt')

    for organization_id, source_id in (
        (None, source.pk),
        (org.pk, None),
        (True, source.pk),
        (org.pk, False),
        (0, source.pk),
        (org.pk, 0),
        (org.pk, wrong_org_source.pk),
    ):
        with pytest.raises(CommandError):
            require_exact_load_scope(organization_id, source_id)

    with pytest.raises(CommandError, match='expected one of'):
        require_exact_load_scope(
            org.pk,
            wrong_type.pk,
            expected_source_types={'powerbi_fabric'},
        )

    wrong_type.is_active = False
    wrong_type.save(update_fields=['is_active'])
    with pytest.raises(CommandError, match='inactive'):
        require_exact_load_scope(
            org.pk, wrong_type.pk, expected_source_types={'dbt'},
        )

    _source(org, 'powerbi_fabric', 'Second Fabric')
    with pytest.raises(CommandError, match='multiple active'):
        require_exact_load_scope(
            org.pk,
            source.pk,
            expected_source_types={'powerbi_fabric'},
        )


@pytest.mark.django_db
def test_identity_preflight_rejects_foreign_and_legacy_global_keys():
    org = Organization.objects.create(name='Org')
    other = Organization.objects.create(name='Other')
    source = _source(org, 'powerbi_fabric', 'Fabric')
    other_source = _source(other, 'powerbi_fabric', 'Other Fabric')
    same_org_other_source = IntegrationSource.objects.create(
        organization=org,
        source_type='powerbi_fabric',
        name='Inactive old source',
        is_active=False,
    )

    Item.objects.create(
        item_id='foreign-item',
        item_name='Foreign',
        organization=other,
        integration_source=other_source,
    )
    Item.objects.create(
        item_id='other-source-item',
        item_name='Other source',
        organization=org,
        integration_source=same_org_other_source,
    )
    Item.objects.create(
        item_id='legacy-null-source',
        item_name='Legacy',
        organization=org,
        integration_source=None,
    )
    NetworkNode.objects.create(
        node_id='TABLE::foreign',
        organization=other,
    )
    NetworkEdge.objects.create(
        source='TABLE::foreign',
        target='COLUMN::foreign',
        organization=other,
    )

    for item_id in (
        'foreign-item', 'other-source-item', 'legacy-null-source',
    ):
        with pytest.raises(CommandError):
            assert_incoming_identities_available(
                organization_id=org.pk,
                source_id=source.pk,
                item_ids={item_id},
            )
    with pytest.raises(CommandError):
        assert_incoming_identities_available(
            organization_id=org.pk,
            source_id=source.pk,
            node_ids={'TABLE::foreign'},
        )
    with pytest.raises(CommandError):
        assert_incoming_identities_available(
            organization_id=org.pk,
            source_id=source.pk,
            edges={('TABLE::foreign', 'COLUMN::foreign')},
        )


def test_csv_identity_reader_normalizes_headers_and_values(tmp_path):
    items = tmp_path / 'items.csv'
    graph = tmp_path / 'graph.csv'
    _write_csv(items, [' Item_ID '], [{' Item_ID ': ' item-1 '}])
    _write_csv(
        graph,
        [' SOURCE_ID ', ' TARGET_ID '],
        [{' SOURCE_ID ': ' A ', ' TARGET_ID ': ' B '}],
    )

    item_ids, node_ids, edges = read_incoming_identities(items, graph)

    assert item_ids == {'item-1'}
    assert node_ids == {'A', 'B'}
    assert edges == {('A', 'B')}


@pytest.mark.django_db
def test_grouping_commit_boundary_rejects_an_inherited_transaction():
    assert connection.in_atomic_block
    with pytest.raises(RuntimeError, match='must commit'):
        require_grouping_commit_boundary()


@pytest.mark.django_db
def test_loader_rejects_inherited_transaction_before_catalog_mutation():
    org = Organization.objects.create(name='Org')
    source = _source(org, 'powerbi_fabric', 'Fabric')
    stale = NetworkNode.objects.create(
        node_id='TABLE::must-survive-atomic-guard',
        group='TABLE',
        organization=org,
    )

    with pytest.raises(RuntimeError, match='must commit'):
        call_command(
            'load_data',
            organization_id=org.pk,
            source_id=source.pk,
            stdout=StringIO(),
        )

    assert NetworkNode.objects.filter(pk=stale.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_session_load_lock_is_released_when_loader_body_raises():
    if connection.vendor != 'postgresql':
        pytest.skip('PostgreSQL advisory-lock semantics')

    def owned_session_lock_count():
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM pg_locks
                WHERE locktype = 'advisory'
                  AND pid = pg_backend_pid()
                  AND classid = %s
                  AND objid = %s
                  AND granted
                """,
                [0x574443, 1],
            )
            return cursor.fetchone()[0]

    before = owned_session_lock_count()
    with pytest.raises(RuntimeError, match='loader failed'):
        with catalog_load_session_lock():
            assert owned_session_lock_count() == before + 1
            raise RuntimeError('loader failed')
    assert owned_session_lock_count() == before


@pytest.mark.django_db(transaction=True)
def test_fabric_loader_scopes_lifecycle_graph_and_summary(tmp_path):
    org = Organization.objects.create(name='Org')
    other = Organization.objects.create(name='Other')
    source = _source(org, 'powerbi_fabric', 'Fabric')
    other_source = _source(other, 'powerbi_fabric', 'Other Fabric')
    old_time = timezone.now()

    current = Item.objects.create(
        item_id='current-fabric',
        item_name='Revenue',
        item_type='PB_MEASURE',
        service='powerbi',
        organization=org,
        integration_source=source,
        deleted=True,
        deleted_at=old_time,
    )
    missing = Item.objects.create(
        item_id='missing-fabric',
        item_name='Missing',
        item_type='PB_COLUMN',
        service='powerbi',
        organization=org,
        integration_source=source,
    )
    foreign = Item.objects.create(
        item_id='foreign-fabric',
        item_name='Foreign',
        item_type='PB_COLUMN',
        service='powerbi',
        organization=other,
        integration_source=other_source,
    )
    own_pb = NetworkNode.objects.create(
        node_id='TABLE::stale-own', group='TABLE', organization=org,
    )
    own_dbt_a = NetworkNode.objects.create(
        node_id='DBT_MODEL::keep-a', group='DBT_MODEL', organization=org,
    )
    own_dbt_b = NetworkNode.objects.create(
        node_id='DBT_MODEL::keep-b', group='DBT_MODEL', organization=org,
    )
    NetworkEdge.objects.create(
        source=own_dbt_a.pk, target=own_dbt_b.pk, organization=org,
    )
    foreign_node = NetworkNode.objects.create(
        node_id='TABLE::foreign-keep', group='TABLE', organization=other,
    )
    Summary.objects.create(
        total_measures=99,
        unused_measures=99,
        total_columns=99,
        unused_columns=99,
        total_reports=99,
        organization=other,
    )

    data = tmp_path / 'etl' / 'sources' / 'fabric' / 'data'
    _write_csv(
        data / 'fabric_info_items.csv',
        ITEM_HEADERS,
        [
            _item_row('current-fabric', 'Revenue', 'PB_MEASURE', 'powerbi'),
            _item_row('', 'Blank is ignored', 'PB_COLUMN', 'powerbi'),
        ],
    )
    _write_csv(data / 'fabric_info_graph.csv', GRAPH_HEADERS, [])

    with override_settings(BASE_DIR=tmp_path):
        call_command(
            'load_data',
            organization_id=org.pk,
            source_id=source.pk,
            stdout=StringIO(),
        )

    current.refresh_from_db()
    missing.refresh_from_db()
    foreign.refresh_from_db()
    assert current.deleted is False
    assert current.deleted_at is None
    assert missing.deleted is True
    assert missing.deleted_at is not None
    first_missing_time = missing.deleted_at
    assert foreign.deleted is False
    assert not NetworkNode.objects.filter(pk=own_pb.pk).exists()
    assert NetworkNode.objects.filter(pk=own_dbt_a.pk).exists()
    assert NetworkNode.objects.filter(pk=foreign_node.pk).exists()
    assert Summary.objects.filter(
        organization=other, total_measures=99,
    ).exists()
    assert Summary.objects.filter(organization=org).count() == 1

    with override_settings(BASE_DIR=tmp_path):
        call_command(
            'load_data',
            organization_id=org.pk,
            source_id=source.pk,
            stdout=StringIO(),
        )
    missing.refresh_from_db()
    assert missing.deleted_at == first_missing_time


@pytest.mark.django_db(transaction=True)
def test_loader_collision_and_missing_files_fail_before_cleanup(tmp_path):
    org = Organization.objects.create(name='Org')
    other = Organization.objects.create(name='Other')
    source = _source(org, 'powerbi_fabric', 'Fabric')
    other_source = _source(other, 'powerbi_fabric', 'Other Fabric')
    Item.objects.create(
        item_id='foreign-collision',
        item_name='Foreign',
        organization=other,
        integration_source=other_source,
    )
    stale = NetworkNode.objects.create(
        node_id='TABLE::must-survive',
        group='TABLE',
        organization=org,
    )
    data = tmp_path / 'etl' / 'sources' / 'fabric' / 'data'
    _write_csv(
        data / 'fabric_info_items.csv',
        ITEM_HEADERS,
        [_item_row(
            'foreign-collision', 'Collision', 'PB_COLUMN', 'powerbi',
        )],
    )
    _write_csv(data / 'fabric_info_graph.csv', GRAPH_HEADERS, [])

    with override_settings(BASE_DIR=tmp_path), pytest.raises(CommandError):
        call_command(
            'load_data',
            organization_id=org.pk,
            source_id=source.pk,
            stdout=StringIO(),
        )
    assert NetworkNode.objects.filter(pk=stale.pk).exists()

    (data / 'fabric_info_items.csv').unlink()
    with override_settings(BASE_DIR=tmp_path), pytest.raises(
            CommandError, match='Items CSV'):
        call_command(
            'load_data',
            organization_id=org.pk,
            source_id=source.pk,
            stdout=StringIO(),
        )


@pytest.mark.django_db(transaction=True)
def test_dbt_loader_scopes_lifecycle_and_graph_replacement(tmp_path):
    org = Organization.objects.create(name='Org')
    other = Organization.objects.create(name='Other')
    source = _source(org, 'dbt', 'dbt')
    other_source = _source(other, 'dbt', 'Other dbt')
    current = Item.objects.create(
        item_id='current-dbt',
        item_name='model',
        item_type='DBT_MODEL',
        service='dbt',
        organization=org,
        integration_source=source,
        deleted=True,
        deleted_at=timezone.now(),
    )
    missing = Item.objects.create(
        item_id='missing-dbt',
        item_name='missing',
        item_type='DBT_MODEL',
        service='dbt',
        organization=org,
        integration_source=source,
    )
    foreign = Item.objects.create(
        item_id='foreign-dbt',
        item_name='foreign',
        item_type='DBT_MODEL',
        service='dbt',
        organization=other,
        integration_source=other_source,
    )
    own_dbt = NetworkNode.objects.create(
        node_id='DBT_MODEL::stale', group='DBT_MODEL', organization=org,
    )
    own_pb = NetworkNode.objects.create(
        node_id='TABLE::keep', group='TABLE', organization=org,
    )
    foreign_dbt = NetworkNode.objects.create(
        node_id='DBT_MODEL::foreign', group='DBT_MODEL', organization=other,
    )

    data = tmp_path / 'etl' / 'sources' / 'dbt' / 'data'
    _write_csv(
        data / 'dbt_info_items.csv',
        ITEM_HEADERS,
        [_item_row('current-dbt', 'model', 'DBT_MODEL', 'dbt')],
    )
    _write_csv(data / 'dbt_info_graph.csv', GRAPH_HEADERS, [])

    with override_settings(BASE_DIR=tmp_path):
        call_command(
            'load_dbt_data',
            organization_id=org.pk,
            source_id=source.pk,
            stdout=StringIO(),
        )

    current.refresh_from_db()
    missing.refresh_from_db()
    foreign.refresh_from_db()
    assert current.deleted is False
    assert current.deleted_at is None
    assert missing.deleted is True
    assert missing.deleted_at is not None
    assert foreign.deleted is False
    assert not NetworkNode.objects.filter(pk=own_dbt.pk).exists()
    assert NetworkNode.objects.filter(pk=own_pb.pk).exists()
    assert NetworkNode.objects.filter(pk=foreign_dbt.pk).exists()


def _graph_row(source_id, source, target_id, target, **overrides):
    row = {
        'source_id': source_id, 'source': source, 'source_type': 'PB_MEASURE',
        'target_id': target_id, 'target': target, 'target_type': 'PB_VISUAL',
        'workspace_id': 'ws-1', 'edge_kind': '', 'lineage_type': '',
    }
    row.update(overrides)
    return row


# One node_id carrying several display names is normal, not corrupt input: the
# item_id hash for tables/columns/measures is built from (dataset_id,
# lineage_tag), so differently-named items that share a lineage_tag collapse
# onto one id — the transform logs this as "Found N duplicated item_ids".
# The loader must therefore de-duplicate on the ON CONFLICT key alone; when it
# de-duplicated whole rows instead, Postgres aborted the whole run with
# "ON CONFLICT DO UPDATE command cannot affect row a second time".
COLLIDING_GRAPH_ROWS = [
    # Same node_id + same edge pair, different name AND different lineage_type.
    _graph_row(
        'PB_MEASURE::dup', 'Revenue', 'PB_VISUAL::v1', 'Visual 1',
        lineage_type='transformation',
    ),
    _graph_row('PB_MEASURE::dup', 'Revenue Alias', 'PB_VISUAL::v1', 'Visual 1'),
    # Third name for the same node_id, this time on the target side.
    _graph_row(
        'PB_TABLE::t1', 'Fact', 'PB_MEASURE::dup', 'Revenue Third Name',
        source_type='PB_TABLE', target_type='PB_MEASURE',
    ),
]


@pytest.mark.django_db(transaction=True)
def test_fabric_loader_collapses_colliding_node_ids_and_edge_pairs(tmp_path):
    org = Organization.objects.create(name='Org')
    source = _source(org, 'powerbi_fabric', 'Fabric')

    data = tmp_path / 'etl' / 'sources' / 'fabric' / 'data'
    _write_csv(
        data / 'fabric_info_items.csv',
        ITEM_HEADERS,
        [
            _item_row('dup-measure', 'Revenue', 'PB_MEASURE', 'powerbi'),
            # Same id once trimmed — the transform's de-dup runs on the raw
            # value and would let this through to the upsert.
            _item_row('dup-measure ', 'Revenue Renamed', 'PB_MEASURE', 'powerbi'),
        ],
    )
    _write_csv(
        data / 'fabric_info_graph.csv', GRAPH_HEADERS_FULL, COLLIDING_GRAPH_ROWS,
    )

    with override_settings(BASE_DIR=tmp_path):
        call_command(
            'load_data',
            organization_id=org.pk,
            source_id=source.pk,
            stdout=StringIO(),
        )

    items = Item.objects.filter(organization=org, item_id='dup-measure')
    assert items.count() == 1
    # Last CSV occurrence wins, matching the transform's keep='last'.
    assert items.get().item_name == 'Revenue Renamed'

    nodes = NetworkNode.objects.filter(organization=org)
    assert nodes.count() == 3
    dup = nodes.get(node_id='PB_MEASURE::dup')
    # Deterministic winner: alphabetically first name, so re-runs are stable.
    assert dup.name == 'Revenue'
    assert dup.group == 'PB_MEASURE'

    edges = NetworkEdge.objects.filter(
        organization=org, source='PB_MEASURE::dup', target='PB_VISUAL::v1',
    )
    assert edges.count() == 1
    # A row carrying lineage provenance wins over a bare duplicate.
    assert edges.get().lineage_type == 'transformation'

    # Re-running is idempotent (and still lands on the same name).
    with override_settings(BASE_DIR=tmp_path):
        call_command(
            'load_data',
            organization_id=org.pk,
            source_id=source.pk,
            stdout=StringIO(),
        )
    assert NetworkNode.objects.filter(organization=org).count() == 3
    assert NetworkNode.objects.get(node_id='PB_MEASURE::dup').name == 'Revenue'


@pytest.mark.django_db(transaction=True)
def test_dbt_loader_collapses_colliding_node_ids_and_edge_pairs(tmp_path):
    org = Organization.objects.create(name='Org')
    source = _source(org, 'dbt', 'dbt')

    data = tmp_path / 'etl' / 'sources' / 'dbt' / 'data'
    _write_csv(
        data / 'dbt_info_items.csv',
        ITEM_HEADERS,
        [
            _item_row('dup-model', 'model', 'DBT_MODEL', 'dbt'),
            _item_row('dup-model ', 'model_renamed', 'DBT_MODEL', 'dbt'),
        ],
    )
    _write_csv(
        data / 'dbt_info_graph.csv', GRAPH_HEADERS_FULL, COLLIDING_GRAPH_ROWS,
    )

    with override_settings(BASE_DIR=tmp_path):
        call_command(
            'load_dbt_data',
            organization_id=org.pk,
            source_id=source.pk,
            stdout=StringIO(),
        )

    items = Item.objects.filter(organization=org, item_id='dup-model')
    assert items.count() == 1
    assert items.get().item_name == 'model_renamed'
    assert NetworkNode.objects.filter(
        organization=org, node_id='PB_MEASURE::dup',
    ).count() == 1
    assert NetworkEdge.objects.filter(
        organization=org, source='PB_MEASURE::dup', target='PB_VISUAL::v1',
    ).count() == 1
