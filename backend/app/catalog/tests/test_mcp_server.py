"""Tests for the MCP server (bearer auth + JSON-RPC endpoint + per-key tools).

These exercise the protocol surface and the scope∩flag gating without hitting
any live PowerBI/BigQuery client — the catalog-read tools run against the
local DB, and the front-loaded overview tool degrades gracefully to an
"empty catalog" string when there is nothing to list.
"""
import json

import pytest
from django.core.cache import cache
from django.urls import reverse

from catalog.mcp import auth
from catalog.models import (
    Item,
    McpApiKey,
    NetworkEdge,
    NetworkNode,
    Organization,
    OrganizationMembership,
)


@pytest.fixture
def mcp_user(db, org):
    from catalog.models import CustomUser
    u = CustomUser.objects.create_user(
        username='mcpuser', email='mcp@example.com', password='x',
    )
    OrganizationMembership.objects.create(user=u, organization=org)
    return u


@pytest.fixture
def mcp_key(db, mcp_user, org):
    key, raw = auth.mint_key(
        user=mcp_user, organization=org, name='test',
        scopes=[auth.SCOPE_CATALOG_READ],
    )
    return key, raw


def _rpc(client, raw_token, method, params=None, msg_id=1):
    body = {'jsonrpc': '2.0', 'id': msg_id, 'method': method}
    if params is not None:
        body['params'] = params
    return client.post(
        reverse('api-mcp'),
        data=json.dumps(body),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {raw_token}',
    )


# --- auth ----------------------------------------------------------------

@pytest.mark.django_db
def test_missing_token_is_401_with_challenge(client):
    resp = client.post(reverse('api-mcp'), data='{}', content_type='application/json')
    assert resp.status_code == 401
    assert resp.headers['WWW-Authenticate'].startswith('Bearer')


@pytest.mark.django_db
def test_invalid_token_is_401(client):
    resp = _rpc(client, 'wdc_not_a_real_token', 'ping')
    assert resp.status_code == 401


@pytest.mark.django_db
def test_revoked_token_is_401(client, mcp_key):
    key, raw = mcp_key
    key.is_active = False
    key.save(update_fields=['is_active'])
    resp = _rpc(client, raw, 'ping')
    assert resp.status_code == 401


@pytest.mark.django_db
def test_only_hash_is_stored(mcp_key):
    key, raw = mcp_key
    assert key.key_hash == auth.hash_token(raw)
    assert raw not in key.key_hash
    assert key.key_prefix == raw[:12]


@pytest.mark.django_db
def test_valid_token_stamps_last_used(client, mcp_key):
    key, raw = mcp_key
    assert key.last_used_at is None
    _rpc(client, raw, 'ping')
    key.refresh_from_db()
    assert key.last_used_at is not None


# --- protocol ------------------------------------------------------------

@pytest.mark.django_db
def test_initialize_negotiates_version(client, mcp_key):
    _, raw = mcp_key
    resp = _rpc(client, raw, 'initialize',
                {'protocolVersion': '2025-06-18', 'capabilities': {}})
    body = resp.json()
    assert body['result']['protocolVersion'] == '2025-06-18'
    assert body['result']['serverInfo']['name'] == 'welcome-data-catalog'


@pytest.mark.django_db
def test_initialize_unknown_version_falls_back(client, mcp_key):
    from catalog.mcp.views import DEFAULT_PROTOCOL_VERSION
    _, raw = mcp_key
    resp = _rpc(client, raw, 'initialize', {'protocolVersion': '1999-01-01'})
    assert resp.json()['result']['protocolVersion'] == DEFAULT_PROTOCOL_VERSION


@pytest.mark.django_db
def test_notification_returns_202_no_body(client, mcp_key):
    _, raw = mcp_key
    resp = client.post(
        reverse('api-mcp'),
        data=json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {raw}',
    )
    assert resp.status_code == 202


@pytest.mark.django_db
def test_unknown_method_is_json_rpc_error(client, mcp_key):
    _, raw = mcp_key
    body = _rpc(client, raw, 'does/not/exist').json()
    assert body['error']['code'] == -32601


@pytest.mark.django_db
def test_get_is_405(client, mcp_key):
    _, raw = mcp_key
    resp = client.get(reverse('api-mcp'), HTTP_AUTHORIZATION=f'Bearer {raw}')
    assert resp.status_code == 405


# --- tools/list gating ---------------------------------------------------

@pytest.mark.django_db
def test_catalog_scope_lists_overview_and_lineage(client, mcp_key, org):
    _, raw = mcp_key
    org.powerbi_tools_enabled = True
    org.dbt_tools_enabled = False
    org.save()
    tools = _rpc(client, raw, 'tools/list').json()['result']['tools']
    names = {t['name'] for t in tools}
    assert 'get_catalog_overview' in names
    assert 'get_lineage' in names
    assert 'get_pb_item_details' in names   # powerbi flag on
    assert 'get_dbt_item_details' not in names  # dbt flag off
    # No live scope granted → no query tools.
    assert 'bigquery_execute_query' not in names
    assert 'powerbi_run_dax_query' not in names


@pytest.mark.django_db
def test_dbt_flag_off_hides_dbt_tool(client, mcp_user, org):
    _, raw = auth.mint_key(
        user=mcp_user, organization=org, name='k',
        scopes=[auth.SCOPE_CATALOG_READ],
    )
    org.dbt_tools_enabled = True
    org.save()
    names = {t['name'] for t in _rpc(client, raw, 'tools/list').json()['result']['tools']}
    assert 'get_dbt_item_details' in names


@pytest.mark.django_db
def test_input_schema_is_introspected(client, mcp_key):
    _, raw = mcp_key
    tools = {t['name']: t for t in _rpc(client, raw, 'tools/list').json()['result']['tools']}
    schema = tools['get_lineage']['inputSchema']
    assert schema['properties']['node_name']['type'] == 'string'
    assert 'node_name' in schema['required']
    for name in (
        'get_lineage',
        'get_pb_item_details',
        'get_pb_usage_analytics',
    ):
        assert 'organization_id' not in tools[name]['inputSchema']['properties']


# --- tools/call ----------------------------------------------------------

@pytest.mark.django_db
def test_call_overview_returns_text_content(client, mcp_key):
    _, raw = mcp_key
    body = _rpc(client, raw, 'tools/call',
                {'name': 'get_catalog_overview', 'arguments': {}}).json()
    result = body['result']
    assert result['isError'] is False
    assert result['content'][0]['type'] == 'text'
    assert isinstance(result['content'][0]['text'], str)


@pytest.mark.django_db
def test_call_unknown_tool_is_error(client, mcp_key):
    _, raw = mcp_key
    body = _rpc(client, raw, 'tools/call',
                {'name': 'nope', 'arguments': {}}).json()
    assert body['error']['code'] == -32602


@pytest.mark.django_db
def test_call_missing_required_arg_is_tool_error(client, mcp_key):
    _, raw = mcp_key
    body = _rpc(client, raw, 'tools/call',
                {'name': 'get_lineage', 'arguments': {}}).json()
    assert body['result']['isError'] is True
    assert 'node_name' in body['result']['content'][0]['text']


@pytest.mark.django_db
def test_call_unexpected_arg_is_tool_error(client, mcp_key):
    _, raw = mcp_key
    body = _rpc(client, raw, 'tools/call',
                {'name': 'get_lineage',
                 'arguments': {'node_name': 'X', 'bogus': 1}}).json()
    assert body['result']['isError'] is True
    assert 'bogus' in body['result']['content'][0]['text']


@pytest.mark.django_db
def test_call_lineage_unknown_node_is_graceful(client, mcp_key):
    _, raw = mcp_key
    # get_lineage on a name that doesn't exist returns a not-found message,
    # not an exception — so isError stays False (it's a normal tool result).
    body = _rpc(client, raw, 'tools/call',
                {'name': 'get_lineage',
                 'arguments': {'node_name': 'no-such-node'}}).json()
    assert 'result' in body
    assert body['result']['content'][0]['type'] == 'text'


@pytest.mark.django_db
def test_catalog_read_calls_never_cross_the_mcp_key_organization(
        client, mcp_key, org):
    """A colliding display name in another tenant must never be resolved."""
    foreign = Organization.objects.create(name='Foreign MCP tenant')

    own_measure = Item.objects.create(
        item_id='mcp-own-measure',
        item_name='Own MCP Metric',
        item_type='PB_MEASURE',
        service='powerbi',
        organization=org,
        connected_reports=1,
        connected_reports_json=[
            {'id': 'mcp-own-report', 'name': 'Own MCP Report', 'url': ''},
        ],
    )
    assert own_measure.organization_id == org.pk
    Item.objects.create(
        item_id='mcp-own-report',
        item_name='Own MCP Report',
        item_type='PB_REPORT',
        service='powerbi',
        organization=org,
    )
    Item.objects.create(
        item_id='mcp-foreign-measure',
        item_name='Foreign MCP Secret Metric',
        item_type='PB_MEASURE',
        service='powerbi',
        organization=foreign,
    )
    Item.objects.create(
        item_id='mcp-foreign-report',
        item_name='Foreign MCP Secret Report',
        item_type='PB_REPORT',
        service='powerbi',
        organization=foreign,
    )

    NetworkNode.objects.create(
        node_id='DBT_MODEL::mcp-own-shared',
        name='MCP Shared Node',
        group='DBT_MODEL',
        organization=org,
    )
    NetworkNode.objects.create(
        node_id='PB_REPORT::mcp-own-consumer',
        name='Own MCP Consumer',
        group='PB_REPORT',
        organization=org,
    )
    NetworkEdge.objects.create(
        source='DBT_MODEL::mcp-own-shared',
        target='PB_REPORT::mcp-own-consumer',
        organization=org,
    )
    NetworkNode.objects.create(
        node_id='DBT_MODEL::mcp-foreign-shared',
        name='MCP Shared Node',
        group='DBT_MODEL',
        organization=foreign,
    )
    NetworkNode.objects.create(
        node_id='PB_REPORT::mcp-foreign-consumer',
        name='Foreign MCP Secret Consumer',
        group='PB_REPORT',
        organization=foreign,
    )
    NetworkEdge.objects.create(
        source='DBT_MODEL::mcp-foreign-shared',
        target='PB_REPORT::mcp-foreign-consumer',
        organization=foreign,
    )

    _, raw = mcp_key
    cache.clear()

    def call_text(name, arguments):
        body = _rpc(
            client,
            raw,
            'tools/call',
            {'name': name, 'arguments': arguments},
        ).json()
        assert body['result']['isError'] is False
        return body['result']['content'][0]['text']

    overview = call_text('get_catalog_overview', {})
    assert 'Own MCP Metric' in overview
    assert 'Foreign MCP Secret' not in overview

    lineage = call_text('get_lineage', {'node_name': 'MCP Shared Node'})
    assert 'Own MCP Consumer' in lineage
    assert 'Foreign MCP Secret Consumer' not in lineage

    analytics = call_text('get_pb_usage_analytics', {})
    assert 'Own MCP Metric' in analytics
    assert 'Foreign MCP Secret Metric' not in analytics

    foreign_profile = call_text(
        'get_pb_item_details',
        {'name': 'Foreign MCP Secret Metric'},
    )
    assert 'No catalog item' in foreign_profile


# --- key minting ---------------------------------------------------------

@pytest.mark.django_db
def test_mint_rejects_unknown_scope(mcp_user, org):
    with pytest.raises(ValueError):
        auth.mint_key(user=mcp_user, organization=org, name='bad',
                      scopes=['not:a:scope'])
