"""Tests for the MCP OAuth 2.1 surface (M5): discovery metadata, the OAuth
access-token auth path on ``/api/mcp/``, and the org-admin gate on the
authorization view.

These do not run a full browser OAuth handshake — they construct a DOT
``AccessToken`` directly (the handshake is DOT's, already tested upstream) and
assert our integration: token → org via membership, scopes ∩ flags, and the
RFC 9728 / 8414 metadata documents.
"""
import json
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from catalog.mcp import auth
from catalog.models import CustomUser, OrganizationMembership


# --- fixtures ------------------------------------------------------------

@pytest.fixture
def member(db, org):
    u = CustomUser.objects.create_user(
        username='oauthmember', email='oauth-member@example.com', password='x',
    )
    OrganizationMembership.objects.create(user=u, organization=org)
    return u


@pytest.fixture
def admin_member(db, org):
    u = CustomUser.objects.create_user(
        username='oauthadmin', email='oauth-admin@example.com', password='x',
    )
    OrganizationMembership.objects.create(user=u, organization=org, is_admin=True)
    return u


@pytest.fixture
def oauth_app(db):
    from oauth2_provider.models import get_application_model

    Application = get_application_model()
    return Application.objects.create(
        name='test-claude',
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris='https://claude.ai/api/mcp/auth_callback',
        skip_authorization=False,
    )


def _access_token(user, application, scope='catalog:read', token='oauth-test-token',
                  expires_in=3600):
    from oauth2_provider.models import get_access_token_model

    return get_access_token_model().objects.create(
        user=user, application=application, token=token, scope=scope,
        expires=timezone.now() + timedelta(seconds=expires_in),
    )


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


# --- discovery metadata --------------------------------------------------

@pytest.mark.django_db
def test_protected_resource_metadata(client):
    data = client.get('/.well-known/oauth-protected-resource').json()
    assert data['resource'].endswith('/api/mcp/')
    assert data['scopes_supported'] == list(auth.ALL_SCOPES)
    assert data['bearer_methods_supported'] == ['header']
    assert data['authorization_servers']  # non-empty origin


@pytest.mark.django_db
def test_protected_resource_metadata_path_suffixed_probe(client):
    # RFC 9728 clients may probe …/oauth-protected-resource/api/mcp — same doc.
    resp = client.get('/.well-known/oauth-protected-resource/api/mcp')
    assert resp.status_code == 200
    assert resp.json()['resource'].endswith('/api/mcp/')


@pytest.mark.django_db
def test_authorization_server_metadata(client):
    data = client.get('/.well-known/oauth-authorization-server').json()
    assert data['authorization_endpoint'].endswith('/api/o/authorize/')
    assert data['token_endpoint'].endswith('/api/o/token/')
    assert data['code_challenge_methods_supported'] == ['S256']
    assert 'authorization_code' in data['grant_types_supported']
    assert 'registration_endpoint' not in data  # manual clients only — no DCR


@pytest.mark.django_db
def test_scopes_match_mcp_scopes(settings):
    assert set(settings.OAUTH2_PROVIDER['SCOPES'].keys()) == set(auth.ALL_SCOPES)


# --- 401 challenge -------------------------------------------------------

@pytest.mark.django_db
def test_missing_token_401_points_at_resource_metadata(client):
    resp = client.post(reverse('api-mcp'), data='{}', content_type='application/json')
    assert resp.status_code == 401
    challenge = resp.headers['WWW-Authenticate']
    assert challenge.startswith('Bearer')
    assert 'resource_metadata=' in challenge
    assert '/.well-known/oauth-protected-resource' in challenge


# --- OAuth access-token auth on /api/mcp/ --------------------------------

@pytest.mark.django_db
def test_oauth_token_lists_tools(client, member, oauth_app, org):
    org.powerbi_tools_enabled = True
    org.dbt_tools_enabled = False
    org.save()
    _access_token(member, oauth_app, scope='catalog:read')

    names = {t['name'] for t in _rpc(client, 'oauth-test-token', 'tools/list')
             .json()['result']['tools']}
    assert 'get_catalog_overview' in names
    assert 'get_lineage' in names
    assert 'get_pb_item_details' in names       # powerbi flag on
    assert 'get_dbt_item_details' not in names  # dbt flag off


@pytest.mark.django_db
def test_oauth_scope_intersects_org_flags(client, member, oauth_app, org):
    # Token carries bigquery:query, but the org's live BigQuery flag is off →
    # the query tool must NOT appear (effective = scopes ∩ flags).
    org.bigquery_live_tools_enabled = False
    org.save()
    _access_token(member, oauth_app, scope='catalog:read bigquery:query')
    names = {t['name'] for t in _rpc(client, 'oauth-test-token', 'tools/list')
             .json()['result']['tools']}
    assert 'bigquery_execute_query' not in names


@pytest.mark.django_db
def test_expired_oauth_token_is_401(client, member, oauth_app):
    _access_token(member, oauth_app, expires_in=-10)
    assert _rpc(client, 'oauth-test-token', 'ping').status_code == 401


@pytest.mark.django_db
def test_unknown_oauth_token_is_401(client):
    assert _rpc(client, 'no-such-oauth-token', 'ping').status_code == 401


@pytest.mark.django_db
def test_oauth_token_user_without_org_is_401(client, db, oauth_app):
    # A token whose user has no membership can't resolve an org → 401.
    orphan = CustomUser.objects.create_user(
        username='orphan', email='orphan@example.com', password='x',
    )
    _access_token(orphan, oauth_app)
    assert _rpc(client, 'oauth-test-token', 'ping').status_code == 401


@pytest.mark.django_db
def test_wdc_key_still_works_alongside_oauth(client, member, org):
    # Regression: the static bearer key path is untouched by the OAuth branch.
    _, raw = auth.mint_key(
        user=member, organization=org, name='desktop',
        scopes=[auth.SCOPE_CATALOG_READ],
    )
    assert _rpc(client, raw, 'ping').json() == {'jsonrpc': '2.0', 'id': 1, 'result': {}}


# --- authorize view: org-admin gate -------------------------------------

def _authorize_url(app):
    from urllib.parse import urlencode
    return '/api/o/authorize/?' + urlencode({
        'response_type': 'code',
        'client_id': app.client_id,
        'redirect_uri': 'https://claude.ai/api/mcp/auth_callback',
        'scope': 'catalog:read',
        'code_challenge': 'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM',
        'code_challenge_method': 'S256',
        'state': 'xyz',
    })


@pytest.mark.django_db
def test_authorize_blocks_non_admin(client, member, oauth_app):
    client.force_login(member)
    resp = client.get(_authorize_url(oauth_app))
    assert resp.status_code == 403


@pytest.mark.django_db
def test_authorize_allows_admin(client, admin_member, oauth_app):
    client.force_login(admin_member)
    resp = client.get(_authorize_url(oauth_app))
    # Admin passes the gate → DOT renders the consent screen (200), not 403.
    assert resp.status_code == 200


@pytest.mark.django_db
def test_authorize_unauthenticated_redirects_to_login(client, oauth_app):
    resp = client.get(_authorize_url(oauth_app))
    assert resp.status_code == 302
    assert '/login/' in resp.headers['Location']
