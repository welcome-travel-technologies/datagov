import pytest

from catalog.models import Item, NetworkEdge, NetworkNode, Organization
from catalog.services.network_path import (
    find_reachable_nodes,
    find_shortest_path,
    resolve_node_id_by_name,
)
from catalog.views import _column_ego


def _node(org, node_id, *, name=None, group='DBT_MODEL'):
    return NetworkNode.objects.create(
        node_id=node_id,
        name=name or node_id,
        group=group,
        organization=org,
    )


def _edge(org, source, target, *, kind='model', level='asset'):
    return NetworkEdge.objects.create(
        organization=org,
        source=source,
        target=target,
        kind=kind,
        level=level,
    )


@pytest.mark.django_db
@pytest.mark.parametrize('url', [
    '/api/network/?node_id=ALL',
    '/api/network/path/?from=DBT_MODEL%3Aa&to=DBT_MODEL%3Ab',
    '/api/network/reachable/?from=DBT_MODEL%3Aa',
])
def test_lineage_endpoints_reject_authenticated_user_without_org(
        client, user, url):
    client.force_login(user)

    response = client.get(url)

    assert response.status_code == 403


@pytest.mark.django_db
def test_network_directories_search_and_all_are_exactly_org_scoped(
        client, rw_user, org):
    other = Organization.objects.create(name='Foreign lineage org')
    own_parent = 'DBT_MODEL::own-parent'
    own_member = 'DBT_COLUMN::own-member'
    foreign_parent = 'DBT_MODEL::foreign-parent'
    foreign_member = 'DBT_COLUMN::foreign-member'
    unowned = 'DBT_MODEL::unowned'

    _node(org, own_parent, name='Shared asset')
    _node(org, own_member, name='Shared member', group='DBT_COLUMN')
    _edge(org, own_parent, own_member, kind='contains', level='column')
    _node(other, foreign_parent, name='Shared asset')
    _node(other, foreign_member, name='Shared member', group='DBT_COLUMN')
    _edge(other, foreign_parent, foreign_member, kind='contains', level='column')
    _node(None, unowned, name='Shared asset')
    _edge(None, unowned, 'DBT_COLUMN::unowned', kind='contains', level='column')

    client.force_login(rw_user)

    all_payload = client.get('/api/network/?node_id=ALL').json()
    assert {row['id'] for row in all_payload['nodes']} == {
        own_parent, own_member,
    }
    assert {
        (row['source'], row['target']) for row in all_payload['links']
    } == {(own_parent, own_member)}

    assets = client.get('/api/network/?list=assets').json()
    assert {row['id'] for row in assets['nodes']} == {own_parent}

    search = client.get('/api/network/?q=Shared').json()
    assert {row['id'] for row in search['nodes']} == {
        own_parent, own_member,
    }

    own_members = client.get(
        '/api/network/', {'list': 'members', 'parent': own_parent},
    ).json()
    assert {row['id'] for row in own_members['nodes']} == {own_member}
    foreign_members = client.get(
        '/api/network/', {'list': 'members', 'parent': foreign_parent},
    ).json()
    assert foreign_members == {'nodes': [], 'links': []}

    member_search = client.get(
        '/api/network/', {'list': 'member_search', 'q': 'Shared member'},
    ).json()
    assert {row['id'] for row in member_search['nodes']} == {own_member}
    assert member_search['nodes'][0]['container'] == own_parent


@pytest.mark.django_db
def test_network_metadata_hydration_never_uses_foreign_item(
        client, rw_user, org):
    other = Organization.objects.create(name='Foreign metadata org')
    node_id = 'PB_COLUMN::foreign-item-hash'
    _node(org, node_id, name='Visible node', group='PB_COLUMN')
    Item.objects.create(
        item_id='foreign-item-hash',
        item_name='Secret foreign column',
        item_type='PB_COLUMN',
        service='powerbi',
        organization=other,
        workspace_id='secret-workspace',
        workspace_name='Secret workspace',
        table_name='secret_table',
        datatype='secret_type',
    )
    client.force_login(rw_user)

    payload = client.get('/api/network/?node_id=ALL').json()
    node = next(row for row in payload['nodes'] if row['id'] == node_id)

    assert node == {
        'id': node_id,
        'label': 'Visible node',
        'group': 'PB_COLUMN',
    }


@pytest.mark.django_db
def test_asset_column_path_and_reachable_ignore_foreign_edges(
        client, rw_user, org):
    other = Organization.objects.create(name='Foreign traversal org')
    own_a = 'DBT_MODEL::own-a'
    own_b = 'DBT_MODEL::own-b'
    own_column = 'DBT_COLUMN::own-column'
    foreign = 'DBT_MODEL::foreign'
    foreign_column = 'DBT_COLUMN::foreign-column'

    _node(org, own_a)
    _node(org, own_b)
    _node(org, own_column, group='DBT_COLUMN')
    _node(other, foreign)
    _node(other, foreign_column, group='DBT_COLUMN')
    _edge(org, own_a, own_b)
    # These deliberately reference an own-org endpoint while belonging to the
    # foreign org. A global traversal would cross the boundary.
    _edge(other, foreign, own_b)
    _edge(
        other, foreign_column, own_column,
        kind='column', level='column',
    )

    client.force_login(rw_user)

    ego = client.get(
        '/api/network/',
        {'node_id': own_b, 'depth': 1, 'direction': 'upstream'},
    ).json()
    assert {row['id'] for row in ego['nodes']} == {own_a, own_b}
    assert {(row['source'], row['target']) for row in ego['links']} == {
        (own_a, own_b),
    }

    column = client.get(
        '/api/network/',
        {
            'node_id': own_column,
            'depth': 1,
            'direction': 'upstream',
            'mode': 'column',
        },
    ).json()
    assert {row['id'] for row in column['nodes']} == {own_column}
    assert column['links'] == []

    path = client.get(
        '/api/network/path/', {'from': own_a, 'to': own_b},
    ).json()
    assert path['found'] is True
    assert {row['id'] for row in path['nodes']} == {own_a, own_b}

    foreign_path = client.get(
        '/api/network/path/', {'from': own_a, 'to': foreign},
    ).json()
    assert foreign_path['found'] is False
    assert foreign_path['nodes'] == []

    reachable = client.get(
        '/api/network/reachable/',
        {'from': own_b, 'direction': 'upstream'},
    ).json()
    assert {row['id'] for row in reachable['nodes']} == {own_a}

    foreign_center = client.get(
        '/api/network/', {'node_id': foreign, 'depth': 1},
    ).json()
    assert foreign_center == {'nodes': [], 'links': []}


@pytest.mark.django_db
def test_network_services_fail_closed_without_organization(org):
    _node(org, 'DBT_MODEL::a')
    _node(org, 'DBT_MODEL::b')
    _edge(org, 'DBT_MODEL::a', 'DBT_MODEL::b')

    with pytest.raises(ValueError, match='organization_id'):
        find_shortest_path('DBT_MODEL::a', 'DBT_MODEL::b')
    with pytest.raises(ValueError, match='organization_id'):
        find_reachable_nodes('DBT_MODEL::b')
    with pytest.raises(ValueError, match='organization_id'):
        _column_ego('DBT_MODEL::a', depth=1, direction='both')
    assert resolve_node_id_by_name('DBT_MODEL::a') == []


@pytest.mark.django_db
def test_network_services_are_exactly_org_scoped(org):
    other = Organization.objects.create(name='Foreign service org')
    own_a = 'DBT_MODEL::service-own-a'
    own_b = 'DBT_MODEL::service-own-b'
    foreign = 'DBT_MODEL::service-foreign'
    _node(org, own_a, name='Shared service name')
    _node(org, own_b)
    _node(other, foreign, name='Shared service name')
    _edge(org, own_a, own_b)
    _edge(other, foreign, own_b)

    path = find_shortest_path(
        own_a, own_b, organization_id=org.id,
    )
    assert path.found is True
    assert {node.id for node in path.nodes} == {own_a, own_b}

    reachable = find_reachable_nodes(
        own_b, organization_id=org.id,
    )
    assert {node.id for node in reachable.nodes} == {own_a}

    matches = resolve_node_id_by_name(
        'Shared service name', organization_id=org.id,
    )
    assert [node.node_id for node in matches] == [own_a]
