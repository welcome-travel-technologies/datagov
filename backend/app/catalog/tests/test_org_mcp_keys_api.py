"""
Verification of the SPA org-admin MCP-keys API (catalog/spa_auth.py) that backs
the Org Settings → MCP Keys tab.

Endpoints under test:
    GET  /api/org/mcp-keys/         -> keys + grantable scopes + members + endpoint
    POST /api/org/mcp-keys/create/  -> mint a key, return the raw token ONCE
    POST /api/org/mcp-keys/revoke/  -> deactivate a key
    POST /api/org/mcp-keys/delete/  -> hard-delete a key row

Admin-gated (is_admin / can_view_org_settings) like the rest of /api/org/.
"""
import pytest
from django.test import Client

from catalog.mcp import auth as mcp_auth
from catalog.models import CustomUser, McpApiKey, OrganizationMembership


@pytest.fixture
def admin_user(db, org):
    u = CustomUser.objects.create_user(
        username="orgadmin", email="orgadmin@example.com", password="testpass",
    )
    OrganizationMembership.objects.create(user=u, organization=org, is_admin=True)
    return u


@pytest.fixture
def admin_client(admin_user):
    c = Client()
    c.force_login(admin_user)
    return c


def _create(client, **body):
    return client.post(
        "/api/org/mcp-keys/create/", data=body, content_type="application/json"
    )


# ---- RBAC -----------------------------------------------------------------

@pytest.mark.django_db
def test_list_requires_admin(client, rw_user):
    client.force_login(rw_user)
    assert client.get("/api/org/mcp-keys/").status_code == 403


@pytest.mark.django_db
def test_list_requires_auth(client):
    assert client.get("/api/org/mcp-keys/").status_code == 401


@pytest.mark.django_db
def test_create_requires_admin(client, rw_user):
    client.force_login(rw_user)
    assert _create(client, name="x", scopes=["catalog:read"]).status_code == 403


# ---- list -----------------------------------------------------------------

@pytest.mark.django_db
def test_list_shape(admin_client, org):
    body = admin_client.get("/api/org/mcp-keys/").json()
    assert body["keys"] == []
    assert body["endpoint"].endswith("/api/mcp/")
    scope_values = {s["value"] for s in body["available_scopes"]}
    assert scope_values == set(mcp_auth.ALL_SCOPES)
    # catalog:read is always active; live scopes reflect the org flags (off here).
    active = {s["value"]: s["active"] for s in body["available_scopes"]}
    assert active["catalog:read"] is True
    assert active["powerbi:query"] is False
    # The acting admin is offered as a key owner.
    assert any(m["is_self"] for m in body["members"])


@pytest.mark.django_db
def test_live_scope_active_follows_org_flag(admin_client, org):
    org.powerbi_live_tools_enabled = True
    org.save()
    body = admin_client.get("/api/org/mcp-keys/").json()
    active = {s["value"]: s["active"] for s in body["available_scopes"]}
    assert active["powerbi:query"] is True


# ---- create ---------------------------------------------------------------

@pytest.mark.django_db
def test_create_returns_token_once_and_persists_only_hash(admin_client, admin_user, org):
    resp = _create(admin_client, name="Claude Desktop", scopes=["catalog:read"])
    assert resp.status_code == 201
    body = resp.json()
    token = body["token"]
    assert token.startswith("wdc_")
    assert body["key"]["name"] == "Claude Desktop"
    assert body["key"]["is_self"] is True

    key = McpApiKey.objects.get(id=body["key"]["id"])
    assert key.user_id == admin_user.id
    assert key.organization_id == org.id
    # Only the hash is stored; the raw token is nowhere in the row.
    assert key.key_hash == mcp_auth.hash_token(token)
    assert key.key_prefix == token[:12]
    # The minted token actually authenticates against the MCP endpoint.
    mcp = admin_client.post(
        "/api/mcp/",
        data='{"jsonrpc":"2.0","id":1,"method":"ping"}',
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    assert mcp.status_code == 200


@pytest.mark.django_db
def test_create_defaults_owner_to_acting_admin(admin_client, admin_user):
    body = _create(admin_client, name="mine", scopes=["catalog:read"]).json()
    assert McpApiKey.objects.get(id=body["key"]["id"]).user_id == admin_user.id


@pytest.mark.django_db
def test_create_for_other_org_member(admin_client, org):
    other = CustomUser.objects.create_user(
        username="member2", email="m2@example.com", password="x",
    )
    OrganizationMembership.objects.create(user=other, organization=org)
    body = _create(
        admin_client, name="for-m2", scopes=["catalog:read"], user_id=other.id
    ).json()
    key = McpApiKey.objects.get(id=body["key"]["id"])
    assert key.user_id == other.id
    assert body["key"]["is_self"] is False


@pytest.mark.django_db
def test_create_rejects_user_outside_org(admin_client):
    outsider = CustomUser.objects.create_user(
        username="outsider", email="out@example.com", password="x",
    )  # no membership in this org
    resp = _create(
        admin_client, name="bad", scopes=["catalog:read"], user_id=outsider.id
    )
    assert resp.status_code == 400
    assert not McpApiKey.objects.filter(name="bad").exists()


@pytest.mark.django_db
def test_create_requires_name(admin_client):
    assert _create(admin_client, name="  ", scopes=["catalog:read"]).status_code == 400


@pytest.mark.django_db
def test_create_requires_a_scope(admin_client):
    assert _create(admin_client, name="x", scopes=[]).status_code == 400


@pytest.mark.django_db
def test_create_rejects_unknown_scope(admin_client):
    resp = _create(admin_client, name="x", scopes=["catalog:read", "bogus:scope"])
    assert resp.status_code == 400
    assert not McpApiKey.objects.filter(name="x").exists()


# ---- revoke ---------------------------------------------------------------

@pytest.mark.django_db
def test_revoke_deactivates_and_kills_auth(admin_client, admin_user, org):
    key, raw = mcp_auth.mint_key(
        user=admin_user, organization=org, name="k", scopes=["catalog:read"],
    )
    resp = admin_client.post(
        "/api/org/mcp-keys/revoke/",
        data={"key_id": key.id},
        content_type="application/json",
    )
    assert resp.status_code == 200
    key.refresh_from_db()
    assert key.is_active is False
    # A revoked token no longer authenticates.
    mcp = admin_client.post(
        "/api/mcp/",
        data='{"jsonrpc":"2.0","id":1,"method":"ping"}',
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {raw}",
    )
    assert mcp.status_code == 401


@pytest.mark.django_db
def test_revoke_unknown_key_is_404(admin_client):
    resp = admin_client.post(
        "/api/org/mcp-keys/revoke/",
        data={"key_id": 999999},
        content_type="application/json",
    )
    assert resp.status_code == 404


@pytest.mark.django_db
def test_cannot_revoke_another_orgs_key(admin_client, db):
    """A key in a different org must not be revocable from this org's admin."""
    from catalog.models import Organization

    other_org = Organization.objects.create(name="Other Org")
    victim = CustomUser.objects.create_user(
        username="victim", email="v@example.com", password="x",
    )
    key, _ = mcp_auth.mint_key(
        user=victim, organization=other_org, name="theirs", scopes=["catalog:read"],
    )
    resp = admin_client.post(
        "/api/org/mcp-keys/revoke/",
        data={"key_id": key.id},
        content_type="application/json",
    )
    assert resp.status_code == 404
    key.refresh_from_db()
    assert key.is_active is True  # untouched


# ---- delete ---------------------------------------------------------------

@pytest.mark.django_db
def test_delete_removes_the_row(admin_client, admin_user, org):
    key, _ = mcp_auth.mint_key(
        user=admin_user, organization=org, name="k", scopes=["catalog:read"],
    )
    resp = admin_client.post(
        "/api/org/mcp-keys/delete/",
        data={"key_id": key.id},
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"
    assert not McpApiKey.objects.filter(id=key.id).exists()


@pytest.mark.django_db
def test_delete_unknown_key_is_404(admin_client):
    resp = admin_client.post(
        "/api/org/mcp-keys/delete/",
        data={"key_id": 999999},
        content_type="application/json",
    )
    assert resp.status_code == 404


@pytest.mark.django_db
def test_cannot_delete_another_orgs_key(admin_client, db):
    from catalog.models import Organization

    other_org = Organization.objects.create(name="Other Org")
    victim = CustomUser.objects.create_user(
        username="victim2", email="v2@example.com", password="x",
    )
    key, _ = mcp_auth.mint_key(
        user=victim, organization=other_org, name="theirs", scopes=["catalog:read"],
    )
    resp = admin_client.post(
        "/api/org/mcp-keys/delete/",
        data={"key_id": key.id},
        content_type="application/json",
    )
    assert resp.status_code == 404
    assert McpApiKey.objects.filter(id=key.id).exists()  # untouched
