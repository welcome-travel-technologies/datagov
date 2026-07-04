"""
Verification of the SPA org-admin OAuth-clients API (catalog/spa_auth.py) that
backs the Org Settings → Connectors tab.

Endpoints under test:
    GET  /api/org/oauth-clients/         -> clients + endpoint + default redirect
    POST /api/org/oauth-clients/create/  -> create a client, return the secret ONCE
    POST /api/org/oauth-clients/revoke/  -> delete a client

Admin-gated like the rest of /api/org/. The client is a django-oauth-toolkit
``Application`` scoped to the org by owner (Application.user in the org).
"""
import pytest
from django.test import Client

from catalog.models import CustomUser, Organization, OrganizationMembership


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


def _application_model():
    from oauth2_provider.models import get_application_model
    return get_application_model()


def _create(client, **body):
    return client.post(
        "/api/org/oauth-clients/create/", data=body, content_type="application/json"
    )


# ---- RBAC -----------------------------------------------------------------

@pytest.mark.django_db
def test_list_requires_admin(client, rw_user):
    client.force_login(rw_user)
    assert client.get("/api/org/oauth-clients/").status_code == 403


@pytest.mark.django_db
def test_list_requires_auth(client):
    assert client.get("/api/org/oauth-clients/").status_code == 401


@pytest.mark.django_db
def test_create_requires_admin(client, rw_user):
    client.force_login(rw_user)
    assert _create(client, name="x").status_code == 403


# ---- list -----------------------------------------------------------------

@pytest.mark.django_db
def test_list_shape(admin_client):
    body = admin_client.get("/api/org/oauth-clients/").json()
    assert body["clients"] == []
    assert body["endpoint"].endswith("/api/mcp/")
    assert body["default_redirect_uri"] == "https://claude.ai/api/mcp/auth_callback"


@pytest.mark.django_db
def test_created_client_appears_in_list(admin_client):
    _create(admin_client, name="claude.ai")
    clients = admin_client.get("/api/org/oauth-clients/").json()["clients"]
    assert len(clients) == 1
    assert clients[0]["name"] == "claude.ai"
    assert clients[0]["redirect_uris"] == ["https://claude.ai/api/mcp/auth_callback"]


# ---- create ---------------------------------------------------------------

@pytest.mark.django_db
def test_create_returns_secret_once_and_persists_confidential_client(admin_client, admin_user):
    resp = _create(admin_client, name="claude.ai")
    assert resp.status_code == 201
    body = resp.json()
    assert body["client_id"]
    assert body["client_secret"]
    assert body["client"]["name"] == "claude.ai"

    Application = _application_model()
    app = Application.objects.get(id=body["client"]["id"])
    assert app.user_id == admin_user.id                       # owner → org scoping
    assert app.client_type == Application.CLIENT_CONFIDENTIAL
    assert app.authorization_grant_type == Application.GRANT_AUTHORIZATION_CODE
    assert app.skip_authorization is False                    # explicit consent
    # The secret is hashed at rest — the plaintext is not stored.
    assert app.client_secret != body["client_secret"]


@pytest.mark.django_db
def test_create_defaults_redirect_uri(admin_client):
    body = _create(admin_client, name="x").json()
    assert body["client"]["redirect_uris"] == ["https://claude.ai/api/mcp/auth_callback"]


@pytest.mark.django_db
def test_create_accepts_custom_https_redirect(admin_client):
    body = _create(
        admin_client, name="x", redirect_uris=["https://example.com/cb"]
    ).json()
    assert body["client"]["redirect_uris"] == ["https://example.com/cb"]


@pytest.mark.django_db
def test_create_rejects_non_https_redirect(admin_client):
    resp = _create(admin_client, name="x", redirect_uris=["http://insecure/cb"])
    assert resp.status_code == 400
    assert not _application_model().objects.filter(name="x").exists()


@pytest.mark.django_db
def test_create_requires_name(admin_client):
    assert _create(admin_client, name="  ").status_code == 400


# ---- revoke ---------------------------------------------------------------

@pytest.mark.django_db
def test_revoke_deletes_client(admin_client):
    app_id = _create(admin_client, name="claude.ai").json()["client"]["id"]
    resp = admin_client.post(
        "/api/org/oauth-clients/revoke/",
        data={"id": app_id},
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert not _application_model().objects.filter(id=app_id).exists()


@pytest.mark.django_db
def test_revoke_unknown_id_is_404(admin_client):
    resp = admin_client.post(
        "/api/org/oauth-clients/revoke/",
        data={"id": 999999},
        content_type="application/json",
    )
    assert resp.status_code == 404


@pytest.mark.django_db
def test_cannot_revoke_another_orgs_client(admin_client, db):
    """A client owned by a member of another org must not be deletable here."""
    other_org = Organization.objects.create(name="Other Org")
    victim = CustomUser.objects.create_user(
        username="victim", email="v@example.com", password="x",
    )
    OrganizationMembership.objects.create(user=victim, organization=other_org)
    Application = _application_model()
    app = Application.objects.create(
        name="theirs", user=victim,
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://claude.ai/api/mcp/auth_callback",
    )
    resp = admin_client.post(
        "/api/org/oauth-clients/revoke/",
        data={"id": app.id},
        content_type="application/json",
    )
    assert resp.status_code == 404
    assert Application.objects.filter(id=app.id).exists()  # untouched


@pytest.mark.django_db
def test_other_orgs_client_not_listed(admin_client, db):
    other_org = Organization.objects.create(name="Other Org 2")
    victim = CustomUser.objects.create_user(
        username="victim2", email="v2@example.com", password="x",
    )
    OrganizationMembership.objects.create(user=victim, organization=other_org)
    Application = _application_model()
    Application.objects.create(
        name="theirs", user=victim,
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://claude.ai/api/mcp/auth_callback",
    )
    clients = admin_client.get("/api/org/oauth-clients/").json()["clients"]
    assert clients == []
