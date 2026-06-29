"""
Tests for the persisted edge classification (catalog.services.network_classify)
and that the network API filters on the stored ``kind``/``level`` columns —
asset mode shows only asset-level edges, column mode only column-level ones.
"""
import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from catalog.services.network_classify import (
    classify_edge, classify_node_ids, kind_case_sql, level_case_sql,
)
from catalog.models import NetworkNode, NetworkEdge
from catalog.views import get_network


# ── classifier (pure) ───────────────────────────────────────────────────────────
def test_classify_edge_contains():
    assert classify_edge('DBT_MODEL', 'DBT_COLUMN') == ('contains', 'column')
    assert classify_edge('PB_TABLE', 'PB_COLUMN') == ('contains', 'column')
    # a measure's box is a hinge — it appears in both views.
    assert classify_edge('PB_TABLE', 'PB_MEASURE') == ('contains', 'both')


def test_classify_edge_column():
    assert classify_edge('DBT_COLUMN', 'DBT_COLUMN') == ('column', 'column')
    assert classify_edge('DBT_COLUMN', 'PB_COLUMN') == ('column', 'column')   # bridge
    assert classify_edge('PB_MEASURE', 'PB_COLUMN') == ('column', 'column')   # DAX
    assert classify_edge('PB_FIELD', 'PB_COLUMN') == ('column', 'column')     # field ref


def test_classify_edge_field_measure_is_both():
    # field↔measure bridges the report-field world to a measure → both views.
    assert classify_edge('PB_FIELD', 'PB_MEASURE') == ('column', 'both')
    assert classify_edge('PB_MEASURE', 'PB_FIELD') == ('column', 'both')


def test_classify_edge_usage_and_structure_are_asset():
    assert classify_edge('DBT_SOURCE', 'DBT_MODEL') == ('model', 'asset')
    assert classify_edge('DBT_MODEL', 'DBT_MODEL') == ('model', 'asset')
    assert classify_edge('PB_REPORT', 'PB_PAGE') == ('model', 'asset')
    assert classify_edge('DBT_MODEL', 'PB_TABLE') == ('model', 'asset')       # table bridge
    # report/visual usage stays in the asset view, out of column derivation.
    assert classify_edge('PB_COLUMN', 'PB_VISUAL') == ('model', 'asset')
    assert classify_edge('PB_VISUAL', 'PB_FIELD') == ('model', 'asset')
    assert classify_edge('PB_MEASURE', 'PB_VISUAL') == ('model', 'asset')


def test_classify_node_ids_parses_prefix():
    assert classify_node_ids('DBT_MODEL::m', 'DBT_COLUMN::c') == ('contains', 'column')
    assert classify_node_ids('DBT_COLUMN::a', 'PB_COLUMN::b') == ('column', 'column')
    assert classify_node_ids('DBT_SOURCE::s', 'DBT_MODEL::m') == ('model', 'asset')


def test_case_sql_builders_are_self_consistent():
    # The SQL fragments reference the configured type columns and the literal
    # values, so a smoke check that they mention both endpoints + each label.
    k = kind_case_sql('source_type', 'target_type')
    lvl = level_case_sql('source_type', 'target_type')
    assert 'source_type' in k and 'target_type' in k
    assert "'contains'" in k and "'column'" in k and "'model'" in k
    assert "'asset'" in lvl and "'column'" in lvl


# ── network API filters on stored level/kind ────────────────────────────────────
def _seed_tagged(org):
    """source.amount -> stg_orders.amount -> (bridge) orders_tbl.amount, with
    edges explicitly tagged (kind/level) the way the loaders now write them."""
    nodes = [
        ('DBT_SOURCE::s', 'orders', 'DBT_SOURCE'),
        ('DBT_COLUMN::sc', 'amount', 'DBT_COLUMN'),
        ('DBT_MODEL::m', 'stg_orders', 'DBT_MODEL'),
        ('DBT_COLUMN::mc', 'amount', 'DBT_COLUMN'),
        ('PB_TABLE::t', 'orders_tbl', 'PB_TABLE'),
        ('PB_COLUMN::pc', 'amount', 'PB_COLUMN'),
    ]
    for nid, name, grp in nodes:
        NetworkNode.objects.create(node_id=nid, name=name, group=grp, organization=org)
    edges = [
        ('DBT_SOURCE::s', 'DBT_COLUMN::sc'),
        ('DBT_MODEL::m', 'DBT_COLUMN::mc'),
        ('PB_TABLE::t', 'PB_COLUMN::pc'),
        ('DBT_SOURCE::s', 'DBT_MODEL::m'),      # asset-level
        ('DBT_COLUMN::sc', 'DBT_COLUMN::mc'),   # column-level
        ('DBT_COLUMN::mc', 'PB_COLUMN::pc'),    # column-level (bridge)
    ]
    for s, t in edges:
        kind, level = classify_node_ids(s, t)
        NetworkEdge.objects.create(source=s, target=t, organization=org, kind=kind, level=level)


def _call(user, **params):
    factory = APIRequestFactory()
    request = factory.get('/api/network/', params)
    force_authenticate(request, user=user)
    return get_network(request)


@pytest.mark.django_db
def test_asset_mode_excludes_column_and_contains_edges(rw_user, org):
    _seed_tagged(org)
    resp = _call(rw_user, node_id='DBT_MODEL::m', depth=3, direction='both', mode='asset')
    edges = {(l['source'], l['target']) for l in resp.data['links']}
    # asset view keeps the model-level edge ...
    assert ('DBT_SOURCE::s', 'DBT_MODEL::m') in edges
    # ... and drops column lineage + containment (those belong to the column view)
    assert ('DBT_COLUMN::sc', 'DBT_COLUMN::mc') not in edges
    assert ('DBT_MODEL::m', 'DBT_COLUMN::mc') not in edges


@pytest.mark.django_db
def test_list_assets_returns_only_asset_level_nodes(rw_user, org):
    """`list=assets` powers the sidebar directory: every model/table-level node,
    no columns, no edges."""
    _seed_tagged(org)
    resp = _call(rw_user, list='assets')
    ids = {n['id'] for n in resp.data['nodes']}
    groups = {n['group'] for n in resp.data['nodes']}
    # model / source / table nodes are present ...
    assert {'DBT_SOURCE::s', 'DBT_MODEL::m', 'PB_TABLE::t'} <= ids
    # ... columns are excluded, and the directory carries no edges
    assert not any('COLUMN' in g for g in groups)
    assert resp.data['links'] == []


@pytest.mark.django_db
def test_column_mode_uses_stored_kind(rw_user, org):
    _seed_tagged(org)
    resp = _call(rw_user, node_id='DBT_COLUMN::sc', depth=3, direction='both', mode='column')
    col = {(l['source'], l['target']) for l in resp.data['links'] if l['kind'] == 'column'}
    contains = {(l['source'], l['target']) for l in resp.data['links'] if l['kind'] == 'contains'}
    assert ('DBT_COLUMN::sc', 'DBT_COLUMN::mc') in col
    assert ('DBT_COLUMN::mc', 'PB_COLUMN::pc') in col          # bridge traversed
    assert ('DBT_MODEL::m', 'DBT_COLUMN::mc') in contains      # parent box attached
    # the asset-level edge never leaks into the column view
    assert all(l['kind'] != 'model' for l in resp.data['links'])
