"""
Precise verification of the SPA org-admin management API (catalog/spa_auth.py),
which lets the React Org Settings page do member CRUD + settings natively
instead of bouncing the admin into the classic Django UI.

Endpoints under test:
    GET  /api/org/members/        -> members + groups/departments/models/settings
    POST /api/org/members/save/   -> create or edit a member
    POST /api/org/members/remove/ -> remove a member (never yourself)
    POST /api/org/settings/       -> update bot + display settings

These are plain Django session views (RBAC gated on can_view_org_settings /
is_admin). The Django test client disables CSRF enforcement, mirroring how the
React app sends the X-CSRFToken header through the Next proxy in production.
"""
import json

import pytest
from django.contrib.auth.models import Group
from django.test import Client

from catalog.access import ASSIGNABLE_GROUPS
from catalog.models import (
    ChatbotModel, CustomUser, DataPerson, Department,
    Organization, OrganizationMembership,
)


@pytest.fixture
def admin_user(db, org):
    """An org admin (membership.is_admin=True -> can_view_org_settings)."""
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


# ---- RBAC -----------------------------------------------------------------

@pytest.mark.django_db
def test_members_list_requires_admin(client, rw_user):
    """rw_user has Company+Analytics page access but is NOT an org admin, so the
    member API must reject it (matches Integrations being Admin-only)."""
    client.force_login(rw_user)
    resp = client.get("/api/org/members/")
    assert resp.status_code == 403


@pytest.mark.django_db
def test_members_list_requires_auth(client):
    resp = client.get("/api/org/members/")
    assert resp.status_code == 401


# ---- list / support data --------------------------------------------------

@pytest.mark.django_db
def test_members_list_returns_members_and_support_data(admin_client, admin_user, org):
    Department.objects.create(name="Analytics Dept", organization=org)
    ChatbotModel.objects.create(
        identifier="test:opus-members-list", display_name="Claude Opus", is_active=True,
    )

    resp = admin_client.get("/api/org/members/")
    assert resp.status_code == 200
    body = resp.json()

    assert body["organization"]["id"] == org.id
    # The admin themselves is a member, flagged is_self + is_admin.
    me = next(m for m in body["members"] if m["user_id"] == admin_user.id)
    assert me["is_self"] is True
    assert me["is_admin"] is True

    # Support data the add/edit dialog needs. Admin is NOT a self-service group
    # anymore (it's the membership.is_admin toggle), so only the feature tiers
    # appear in available_groups.
    group_names = {g["name"] for g in body["available_groups"]}
    assert set(ASSIGNABLE_GROUPS).issubset(group_names)
    assert "Admin" not in group_names
    assert any(d["name"] == "Analytics Dept" for d in body["departments"])
    assert any(m["display_name"] == "Claude Opus" for m in body["chatbot_models"])
    assert set(body["settings"]).issuperset(
        {"powerbi_tools_enabled", "show_deleted_items", "chatbot_model_id"}
    )


# ---- create ----------------------------------------------------------------

@pytest.mark.django_db
def test_create_member_creates_account_membership_profile_and_groups(admin_client, org):
    dept = Department.objects.create(name="Eng", organization=org)
    analytics = Group.objects.get_or_create(name="Analytics")[0]

    payload = {
        "email": "newbie@example.com",
        "password": "s3cret-pass",
        "name": "New Bie",
        "slack_handle": "@newbie",
        "is_owner": True,
        "is_steward": False,
        "is_other": False,
        "department_ids": [dept.id],
        "group_ids": [analytics.id],
    }
    resp = admin_client.post(
        "/api/org/members/save/", data=json.dumps(payload), content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    uid = resp.json()["user_id"]

    user = CustomUser.objects.get(id=uid)
    assert user.check_password("s3cret-pass")
    assert OrganizationMembership.objects.filter(user=user, organization=org).exists()
    assert list(user.groups.values_list("name", flat=True)) == ["Analytics"]

    dp = DataPerson.objects.get(user=user)
    assert dp.name == "New Bie"
    assert dp.is_owner and not dp.is_steward
    assert dp.slack_handle == "@newbie"
    assert list(dp.departments.values_list("id", flat=True)) == [dept.id]


@pytest.mark.django_db
def test_create_member_validation_errors(admin_client, org):
    # Missing password on create.
    resp = admin_client.post(
        "/api/org/members/save/",
        data=json.dumps({"email": "x@example.com", "name": "X", "is_owner": True}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert "password" in resp.json()["error"].lower()

    # No role selected.
    resp = admin_client.post(
        "/api/org/members/save/",
        data=json.dumps({"email": "y@example.com", "password": "p", "name": "Y"}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert "role" in resp.json()["error"].lower()


# ---- edit ------------------------------------------------------------------

@pytest.mark.django_db
def test_edit_member_updates_profile_and_groups_keeps_password(admin_client, org):
    user = CustomUser.objects.create_user(
        username="edith", email="edith@example.com", password="original-pass",
    )
    OrganizationMembership.objects.create(user=user, organization=org)
    analytics = Group.objects.get_or_create(name="Analytics")[0]

    payload = {
        "user_id": user.id,
        "name": "Edith Updated",
        "is_owner": False,
        "is_steward": True,
        "is_other": False,
        "department_ids": [],
        "group_ids": [analytics.id],
        # no password -> must keep the original
    }
    resp = admin_client.post(
        "/api/org/members/save/", data=json.dumps(payload), content_type="application/json",
    )
    assert resp.status_code == 200, resp.content

    user.refresh_from_db()
    assert user.check_password("original-pass")  # unchanged
    assert list(user.groups.values_list("name", flat=True)) == ["Analytics"]
    dp = DataPerson.objects.get(user=user)
    assert dp.name == "Edith Updated"
    assert dp.is_steward and not dp.is_owner


# ---- org-admin flag (lives on the membership, not a group) -----------------

@pytest.mark.django_db
def test_create_member_can_grant_org_admin(admin_client, org):
    payload = {
        "email": "boss@example.com",
        "password": "p",
        "name": "Boss",
        "is_owner": True,
        "is_steward": False,
        "is_other": False,
        "is_admin": True,
        "department_ids": [],
        "group_ids": [],
    }
    resp = admin_client.post(
        "/api/org/members/save/", data=json.dumps(payload), content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    uid = resp.json()["user_id"]
    mem = OrganizationMembership.objects.get(user_id=uid, organization=org)
    assert mem.is_admin is True


@pytest.mark.django_db
def test_admin_cannot_demote_self_via_save(admin_client, admin_user, org):
    """Editing yourself with is_admin=False must NOT strip your admin (lockout
    guard) — the server ignores is_admin for the requesting user."""
    payload = {
        "user_id": admin_user.id,
        "name": "Org Admin",
        "is_owner": True,
        "is_steward": False,
        "is_other": False,
        "is_admin": False,
        "department_ids": [],
        "group_ids": [],
    }
    resp = admin_client.post(
        "/api/org/members/save/", data=json.dumps(payload), content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    mem = OrganizationMembership.objects.get(user=admin_user, organization=org)
    assert mem.is_admin is True


@pytest.mark.django_db
def test_admin_group_user_without_is_admin_is_rejected(client, org):
    """A user in the legacy 'Admin' auth Group but WITHOUT membership.is_admin is
    no longer an org admin — the gate is is_admin only now."""
    u = CustomUser.objects.create_user(
        username="legacyadmin", email="legacy@example.com", password="p",
    )
    OrganizationMembership.objects.create(user=u, organization=org, is_admin=False)
    g = Group.objects.get_or_create(name="Admin")[0]
    u.groups.add(g)

    client.force_login(u)
    assert client.get("/api/org/members/").status_code == 403


# ---- remove ----------------------------------------------------------------

@pytest.mark.django_db
def test_remove_member(admin_client, org):
    victim = CustomUser.objects.create_user(
        username="victim", email="victim@example.com", password="p",
    )
    OrganizationMembership.objects.create(user=victim, organization=org)

    resp = admin_client.post(
        "/api/org/members/remove/",
        data=json.dumps({"user_id": victim.id}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert not OrganizationMembership.objects.filter(user=victim, organization=org).exists()


@pytest.mark.django_db
def test_cannot_remove_self(admin_client, admin_user, org):
    resp = admin_client.post(
        "/api/org/members/remove/",
        data=json.dumps({"user_id": admin_user.id}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert "yourself" in resp.json()["error"].lower()
    assert OrganizationMembership.objects.filter(user=admin_user, organization=org).exists()


# ---- settings --------------------------------------------------------------

@pytest.mark.django_db
def test_update_settings_toggles_flags_and_model(admin_client, org):
    model = ChatbotModel.objects.create(
        identifier="test:opus-settings", display_name="Opus", is_active=True,
    )
    payload = {
        "powerbi_tools_enabled": True,
        "bigquery_tools_enabled": False,
        "dbt_tools_enabled": True,
        "debug_responses_enabled": False,
        "show_deleted_items": True,
        "chatbot_model_id": model.id,
    }
    resp = admin_client.post(
        "/api/org/settings/", data=json.dumps(payload), content_type="application/json",
    )
    assert resp.status_code == 200

    org.refresh_from_db()
    assert org.powerbi_tools_enabled is True
    assert org.bigquery_tools_enabled is False
    assert org.dbt_tools_enabled is True
    assert org.show_deleted_items is True
    assert org.chatbot_model_id == model.id
