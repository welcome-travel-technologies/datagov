"""
Tests for column-level lineage in the network API (catalog.views).

``_edge_kind`` is pure; ``_column_ego`` needs the DB (it queries Network* + Item).
"""
import pytest

from catalog.views import _edge_kind, _column_ego
from catalog.models import NetworkNode, NetworkEdge


# ── _edge_kind (pure) ──────────────────────────────────────────────────────────
def test_edge_kind_contains():
    assert _edge_kind('DBT_MODEL::m', 'DBT_COLUMN::c') == 'contains'
    assert _edge_kind('PB_TABLE::t', 'PB_COLUMN::c') == 'contains'
    assert _edge_kind('PB_TABLE::t', 'PB_MEASURE::x') == 'contains'


def test_edge_kind_column():
    assert _edge_kind('DBT_COLUMN::a', 'DBT_COLUMN::b') == 'column'
    assert _edge_kind('DBT_COLUMN::a', 'PB_COLUMN::b') == 'column'   # cross-tool bridge
    assert _edge_kind('PB_MEASURE::m', 'PB_COLUMN::c') == 'column'   # DAX dependency


def test_edge_kind_model():
    assert _edge_kind('DBT_SOURCE::s', 'DBT_MODEL::m') == 'model'
    assert _edge_kind('DBT_MODEL::a', 'DBT_MODEL::b') == 'model'
    assert _edge_kind('PB_REPORT::r', 'PB_PAGE::p') == 'model'


# ── _column_ego (DB-backed) ─────────────────────────────────────────────────────
def _seed_graph(org):
    """source.amount -> stg_orders.amount -> (bridge) orders_tbl.amount, plus
    containment edges and an asset-level source->model edge."""
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
        ('DBT_SOURCE::s', 'DBT_COLUMN::sc'),    # contains
        ('DBT_MODEL::m', 'DBT_COLUMN::mc'),      # contains
        ('PB_TABLE::t', 'PB_COLUMN::pc'),        # contains
        ('DBT_SOURCE::s', 'DBT_MODEL::m'),       # model-level lineage (asset)
        ('DBT_COLUMN::sc', 'DBT_COLUMN::mc'),    # column lineage (sqlglot)
        ('DBT_COLUMN::mc', 'PB_COLUMN::pc'),     # cross-tool bridge
    ]
    for s, t in edges:
        NetworkEdge.objects.create(source=s, target=t, organization=org)


@pytest.mark.django_db
def test_column_ego_traces_columns_and_attaches_parents(org):
    _seed_graph(org)
    resp = _column_ego('DBT_COLUMN::sc', depth=3, direction='both')
    links = resp.data['links']
    col = {(l['source'], l['target']) for l in links if l['kind'] == 'column'}
    contains = {(l['source'], l['target']) for l in links if l['kind'] == 'contains'}
    node_ids = {n['id'] for n in resp.data['nodes']}

    # full column chain, including the cross-tool bridge
    assert ('DBT_COLUMN::sc', 'DBT_COLUMN::mc') in col
    assert ('DBT_COLUMN::mc', 'PB_COLUMN::pc') in col
    # every touched column has its container box attached
    assert ('DBT_SOURCE::s', 'DBT_COLUMN::sc') in contains
    assert ('DBT_MODEL::m', 'DBT_COLUMN::mc') in contains
    assert ('PB_TABLE::t', 'PB_COLUMN::pc') in contains
    assert {'DBT_SOURCE::s', 'DBT_MODEL::m', 'PB_TABLE::t'} <= node_ids
    # the model-level edge never leaks into the column view
    assert all(l['kind'] != 'model' for l in links)


@pytest.mark.django_db
def test_column_ego_seeds_from_container_members(org):
    _seed_graph(org)
    # centering on a model expands to its member columns, then traces them.
    resp = _column_ego('DBT_MODEL::m', depth=3, direction='both')
    col = {(l['source'], l['target']) for l in resp.data['links'] if l['kind'] == 'column'}
    assert ('DBT_COLUMN::sc', 'DBT_COLUMN::mc') in col
    assert ('DBT_COLUMN::mc', 'PB_COLUMN::pc') in col


@pytest.mark.django_db
def test_column_ego_upstream_only(org):
    _seed_graph(org)
    # from the PB column, upstream should walk back to the dbt source column.
    resp = _column_ego('PB_COLUMN::pc', depth=3, direction='upstream')
    col = {(l['source'], l['target']) for l in resp.data['links'] if l['kind'] == 'column'}
    assert ('DBT_COLUMN::mc', 'PB_COLUMN::pc') in col
    assert ('DBT_COLUMN::sc', 'DBT_COLUMN::mc') in col


def _seed_chain(org, n=5):
    """Linear column chain c0 → c1 → … → c{n-1}, each column in its own model so
    a full trace must cross `n-1` column hops (and `depth=1` cannot)."""
    for i in range(n):
        NetworkNode.objects.create(node_id=f'DBT_MODEL::m{i}', name=f'm{i}',
                                   group='DBT_MODEL', organization=org)
        NetworkNode.objects.create(node_id=f'DBT_COLUMN::c{i}', name=f'c{i}',
                                   group='DBT_COLUMN', organization=org)
        NetworkEdge.objects.create(source=f'DBT_MODEL::m{i}', target=f'DBT_COLUMN::c{i}',
                                   kind='contains', organization=org)
    for i in range(n - 1):
        NetworkEdge.objects.create(source=f'DBT_COLUMN::c{i}', target=f'DBT_COLUMN::c{i+1}',
                                   kind='column', organization=org)


@pytest.mark.django_db
def test_column_ego_depth_bounds_chain(org):
    """A depth-bounded trace stops after `depth` hops (the old behavior)."""
    _seed_chain(org, 5)
    resp = _column_ego('DBT_COLUMN::c0', depth=1, direction='downstream')
    col = {(l['source'], l['target']) for l in resp.data['links'] if l['kind'] == 'column'}
    assert ('DBT_COLUMN::c0', 'DBT_COLUMN::c1') in col
    assert ('DBT_COLUMN::c1', 'DBT_COLUMN::c2') not in col   # cut off at depth 1


@pytest.mark.django_db
def test_column_ego_depth_zero_focus_only(org):
    """depth=0 is the canvas's initial "open this element" load: just the focused
    column and its container card, with no neighbour columns traversed."""
    _seed_chain(org, 5)
    resp = _column_ego('DBT_COLUMN::c0', depth=0, direction='both')
    node_ids = {n['id'] for n in resp.data['nodes']}
    col = {(l['source'], l['target']) for l in resp.data['links'] if l['kind'] == 'column'}
    assert node_ids == {'DBT_COLUMN::c0', 'DBT_MODEL::m0'}  # element + its container only
    assert col == set()                                    # no neighbour columns pulled in


@pytest.mark.django_db
def test_column_ego_full_traverses_entire_chain(org):
    """full=True ignores the depth clamp and follows the chain to its end."""
    _seed_chain(org, 5)
    resp = _column_ego('DBT_COLUMN::c0', depth=1, direction='downstream', full=True)
    col = {(l['source'], l['target']) for l in resp.data['links'] if l['kind'] == 'column'}
    node_ids = {n['id'] for n in resp.data['nodes']}
    for i in range(4):                                       # every hop present
        assert (f'DBT_COLUMN::c{i}', f'DBT_COLUMN::c{i+1}') in col
    assert {f'DBT_COLUMN::c{i}' for i in range(5)} <= node_ids
    assert {f'DBT_MODEL::m{i}' for i in range(5)} <= node_ids  # containers attached


@pytest.mark.django_db
def test_column_ego_frontier_flags_on_bounded_chain(org):
    """A depth-bounded response flags the boundary members that still have
    unloaded column lineage (per direction), and only those — this powers the
    per-card "load one more level" buttons."""
    _seed_chain(org, 5)
    resp = _column_ego('DBT_COLUMN::c2', depth=1, direction='both')
    flags = {n['id']: (n.get('hasMoreUp', False), n.get('hasMoreDown', False))
             for n in resp.data['nodes']}
    # Loaded: c1, c2, c3. c0 is hidden upstream of c1; c4 hidden downstream of c3.
    assert flags['DBT_COLUMN::c1'] == (True, False)
    assert flags['DBT_COLUMN::c3'] == (False, True)
    assert flags['DBT_COLUMN::c2'] == (False, False)   # interior: nothing hidden


@pytest.mark.django_db
def test_column_ego_asymmetric_radii(org):
    """depth_up/depth_down walk each direction independently in ONE response,
    so the frontier flags are exact — the center must not be flagged for a
    direction that is already loaded."""
    _seed_chain(org, 5)
    resp = _column_ego('DBT_COLUMN::c2', depth=1, direction='both',
                       depth_up=1, depth_down=2)
    node_ids = {n['id'] for n in resp.data['nodes']}
    assert {'DBT_COLUMN::c1', 'DBT_COLUMN::c2', 'DBT_COLUMN::c3', 'DBT_COLUMN::c4'} <= node_ids
    assert 'DBT_COLUMN::c0' not in node_ids          # upstream bounded to 1 hop
    flags = {n['id']: (n.get('hasMoreUp', False), n.get('hasMoreDown', False))
             for n in resp.data['nodes']}
    assert flags['DBT_COLUMN::c2'] == (False, False)  # both sides loaded — no dead buttons
    assert flags['DBT_COLUMN::c1'] == (True, False)   # c0 still hidden upstream
    assert flags['DBT_COLUMN::c4'] == (False, False)  # true chain end


@pytest.mark.django_db
def test_column_ego_no_frontier_flags_when_fully_loaded(org):
    """With the entire chain loaded (full=True) nothing is flagged."""
    _seed_chain(org, 5)
    resp = _column_ego('DBT_COLUMN::c0', depth=1, direction='downstream', full=True)
    assert all(not n.get('hasMoreUp') and not n.get('hasMoreDown')
               for n in resp.data['nodes'])


def _seed_report_graph(org):
    """A measure that is consumed downstream by a report:
    PB_TABLE -> (contains) PB_MEASURE -> PB_VISUAL -> PB_PAGE -> PB_REPORT."""
    nodes = [
        ('PB_TABLE::t', 'Sales', 'PB_TABLE'),
        ('PB_MEASURE::m', 'Revenue', 'PB_MEASURE'),
        ('PB_VISUAL::v', 'Bar chart', 'PB_VISUAL'),
        ('PB_PAGE::pg', 'Overview', 'PB_PAGE'),
        ('PB_REPORT::r', 'Exec Report', 'PB_REPORT'),
    ]
    for nid, name, grp in nodes:
        NetworkNode.objects.create(node_id=nid, name=name, group=grp, organization=org)
    mk = lambda **kw: NetworkEdge.objects.create(organization=org, **kw)
    mk(source='PB_TABLE::t', target='PB_MEASURE::m', kind='contains')
    mk(source='PB_MEASURE::m', target='PB_VISUAL::v', kind='model')   # measure used in visual
    mk(source='PB_VISUAL::v', target='PB_PAGE::pg', kind='model')     # visual on page
    mk(source='PB_PAGE::pg', target='PB_REPORT::r', kind='model')     # page in report


@pytest.mark.django_db
def test_unified_ego_collapses_report_hierarchy_to_direct_consumers(org):
    """An expanded unified load resolves member -> visual -> page -> report to a
    DIRECT member -> report edge; the unnamed visual/page intermediates never
    enter the payload (a hub measure used to flood the canvas with them)."""
    _seed_report_graph(org)
    resp = _column_ego('PB_MEASURE::m', depth=2, direction='both', unified=True)
    node_ids = {n['id'] for n in resp.data['nodes']}
    model_edges = {(l['source'], l['target']) for l in resp.data['links'] if l['kind'] == 'model'}

    assert 'PB_REPORT::r' in node_ids
    assert 'PB_VISUAL::v' not in node_ids
    assert 'PB_PAGE::pg' not in node_ids
    assert model_edges == {('PB_MEASURE::m', 'PB_REPORT::r')}
    # the measure's own container is still attached
    assert ('PB_TABLE::t', 'PB_MEASURE::m') in {
        (l['source'], l['target']) for l in resp.data['links'] if l['kind'] == 'contains'
    }
    # everything the measure consumes downstream is loaded — no dangling flag
    flags = {n['id']: n.get('hasMoreDown', False) for n in resp.data['nodes']}
    assert flags['PB_MEASURE::m'] is False
    assert resp.data['mode'] == 'unified'


@pytest.mark.django_db
def test_column_mode_excludes_report_hierarchy(org):
    """Without unified=True the report hierarchy must NOT leak into the view."""
    _seed_report_graph(org)
    resp = _column_ego('PB_MEASURE::m', depth=2, direction='both')
    node_ids = {n['id'] for n in resp.data['nodes']}
    assert 'PB_VISUAL::v' not in node_ids
    assert all(l['kind'] != 'model' for l in resp.data['links'])


@pytest.mark.django_db
def test_unified_ego_upstream_only_skips_reports(org):
    """Reports are downstream consumers, so an upstream-only trace omits them."""
    _seed_report_graph(org)
    resp = _column_ego('PB_MEASURE::m', depth=2, direction='upstream', unified=True)
    node_ids = {n['id'] for n in resp.data['nodes']}
    assert 'PB_VISUAL::v' not in node_ids


@pytest.mark.django_db
def test_unified_ego_depth_zero_skips_report_hierarchy(org):
    """Opening a measure (depth-0 focus) must show only the measure + its
    container — NOT the downstream report consumers. But the measure IS flagged
    hasMoreDown, so its card renders the "+" whose one level is those reports."""
    _seed_report_graph(org)
    resp = _column_ego('PB_MEASURE::m', depth=0, direction='both', unified=True)
    node_ids = {n['id'] for n in resp.data['nodes']}
    assert node_ids == {'PB_MEASURE::m', 'PB_TABLE::t'}
    assert all(l['kind'] != 'model' for l in resp.data['links'])
    measure = next(n for n in resp.data['nodes'] if n['id'] == 'PB_MEASURE::m')
    assert measure.get('hasMoreDown') is True   # unloaded report consumers


@pytest.mark.django_db
def test_column_ego_serializes_lineage_type_bridge_and_join(org):
    """Column links carry lineage_type + bridge; relationships surface as 'join'
    edges (one hop out); column nodes carry lineageType/hasLineage."""
    nodes = [
        ('DBT_COLUMN::a', 'amt', 'DBT_COLUMN'),
        ('DBT_COLUMN::b', 'amt_usd', 'DBT_COLUMN'),
        ('DBT_MODEL::m', 'fct', 'DBT_MODEL'),
        ('PB_COLUMN::p', 'Amount', 'PB_COLUMN'),
        ('PB_COLUMN::q', 'Key', 'PB_COLUMN'),
        ('PB_TABLE::t', 'Sales', 'PB_TABLE'),
        ('PB_TABLE::u', 'Dim', 'PB_TABLE'),
    ]
    for nid, name, grp in nodes:
        NetworkNode.objects.create(node_id=nid, name=name, group=grp, organization=org)
    mk = lambda **kw: NetworkEdge.objects.create(organization=org, **kw)
    mk(source='DBT_COLUMN::a', target='DBT_COLUMN::b', kind='column', lineage_type='transformation')
    mk(source='DBT_MODEL::m', target='DBT_COLUMN::a', kind='contains')
    mk(source='DBT_MODEL::m', target='DBT_COLUMN::b', kind='contains')
    mk(source='DBT_COLUMN::b', target='PB_COLUMN::p', kind='column',
       lineage_type='pass-through', bridge_reason='bq_fqn')
    mk(source='PB_TABLE::t', target='PB_COLUMN::p', kind='contains')
    mk(source='PB_COLUMN::p', target='PB_COLUMN::q', kind='join')   # FK→PK relationship
    mk(source='PB_TABLE::u', target='PB_COLUMN::q', kind='contains')

    resp = _column_ego('DBT_COLUMN::a', depth=4, direction='both')
    by_pair = {(l['source'], l['target']): l for l in resp.data['links']}

    assert by_pair[('DBT_COLUMN::a', 'DBT_COLUMN::b')]['lineage_type'] == 'transformation'
    bridge = by_pair[('DBT_COLUMN::b', 'PB_COLUMN::p')]
    assert bridge.get('bridge') is True and bridge['lineage_type'] == 'pass-through'
    # The join partner PB_COLUMN::q is reachable ONLY via the FK→PK join, not via
    # data lineage, so it must NOT be pulled in — a structural edge is context
    # between present nodes, never a way to expand the graph.
    node_ids = {n['id'] for n in resp.data['nodes']}
    assert 'PB_COLUMN::q' not in node_ids
    assert 'PB_TABLE::u' not in node_ids
    assert ('PB_COLUMN::p', 'PB_COLUMN::q') not in by_pair

    nodes_by_id = {n['id']: n for n in resp.data['nodes']}
    assert nodes_by_id['DBT_COLUMN::b'].get('lineageType') == 'transformation'
    assert nodes_by_id['DBT_COLUMN::b'].get('hasLineage') is True


@pytest.mark.django_db
def test_column_ego_structural_edge_shown_between_present_nodes(org):
    """A join edge IS rendered when BOTH endpoints are already on the canvas from
    data lineage — it's useful context, it just can't expand the graph."""
    # a → b (data lineage) puts both columns on the canvas; a join relates them
    # too. (source,target) is unique per row, so the join uses the reverse pair.
    for nid, grp in [('DBT_COLUMN::a', 'DBT_COLUMN'), ('DBT_COLUMN::b', 'DBT_COLUMN'),
                     ('DBT_MODEL::ma', 'DBT_MODEL'), ('DBT_MODEL::mb', 'DBT_MODEL')]:
        NetworkNode.objects.create(node_id=nid, name=nid, group=grp, organization=org)
    mk = lambda **kw: NetworkEdge.objects.create(organization=org, **kw)
    mk(source='DBT_MODEL::ma', target='DBT_COLUMN::a', kind='contains')
    mk(source='DBT_MODEL::mb', target='DBT_COLUMN::b', kind='contains')
    mk(source='DBT_COLUMN::a', target='DBT_COLUMN::b', kind='column', lineage_type='pass-through')
    mk(source='DBT_COLUMN::b', target='DBT_COLUMN::a', kind='join')

    resp = _column_ego('DBT_COLUMN::a', depth=4, direction='both')
    by_pair = {(l['source'], l['target'], l['kind']): l for l in resp.data['links']}
    assert ('DBT_COLUMN::b', 'DBT_COLUMN::a', 'join') in by_pair       # context kept
    assert ('DBT_COLUMN::a', 'DBT_COLUMN::b', 'column') in by_pair


@pytest.mark.django_db
def test_column_ego_hub_dimension_does_not_flood(org):
    """The core clutter fix: a shared dimension column that many facts join must
    not drag every joined fact table into a measure's lineage."""
    # measure m derives from fact column f; f joins a shared date dimension d,
    # which 8 unrelated fact columns also join. None of those facts feed m.
    NetworkNode.objects.create(node_id='PB_TABLE::sales', name='Sales', group='PB_TABLE', organization=org)
    NetworkNode.objects.create(node_id='PB_COLUMN::f', name='amount', group='PB_COLUMN', organization=org)
    NetworkNode.objects.create(node_id='PB_MEASURE::m', name='Revenue', group='PB_MEASURE', organization=org)
    NetworkNode.objects.create(node_id='PB_TABLE::dim', name='Date', group='PB_TABLE', organization=org)
    NetworkNode.objects.create(node_id='PB_COLUMN::d', name='date', group='PB_COLUMN', organization=org)
    mk = lambda **kw: NetworkEdge.objects.create(organization=org, **kw)
    mk(source='PB_TABLE::sales', target='PB_COLUMN::f', kind='contains')
    mk(source='PB_TABLE::sales', target='PB_MEASURE::m', kind='contains')
    mk(source='PB_COLUMN::f', target='PB_MEASURE::m', kind='column', lineage_type='transformation')
    mk(source='PB_TABLE::dim', target='PB_COLUMN::d', kind='contains')
    mk(source='PB_COLUMN::f', target='PB_COLUMN::d', kind='join')  # sales joins date
    for i in range(8):
        NetworkNode.objects.create(node_id=f'PB_TABLE::fact{i}', name=f'Fact{i}', group='PB_TABLE', organization=org)
        NetworkNode.objects.create(node_id=f'PB_COLUMN::fc{i}', name='date', group='PB_COLUMN', organization=org)
        mk(source=f'PB_TABLE::fact{i}', target=f'PB_COLUMN::fc{i}', kind='contains')
        mk(source=f'PB_COLUMN::fc{i}', target='PB_COLUMN::d', kind='join')  # each joins the same date

    resp = _column_ego('PB_MEASURE::m', depth=3, direction='upstream', unified=True)
    node_ids = {n['id'] for n in resp.data['nodes']}
    # none of the 8 unrelated fact tables (or their columns) leak in
    assert not any(f'PB_TABLE::fact{i}' in node_ids for i in range(8))
    assert not any(f'PB_COLUMN::fc{i}' in node_ids for i in range(8))
