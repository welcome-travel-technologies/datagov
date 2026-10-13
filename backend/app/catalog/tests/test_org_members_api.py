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


@pytest.mark.django_db
def test_members_list_hides_cross_org_profile_departments(
        admin_client, admin_user, org):
    local = Department.objects.create(name="Local Finance", organization=org)
    other_org = Organization.objects.create(name="Neighbour tenant")
    foreign = Department.objects.create(
        name="Foreign Secret Department",
        organization=other_org,
    )
    profile = DataPerson.objects.create(
        name="Org Admin", organization=org, user=admin_user,
    )
    profile.departments.add(local, foreign)

    resp = admin_client.get("/api/org/members/")

    assert resp.status_code == 200
    member = next(
        row for row in resp.json()["members"]
        if row["user_id"] == admin_user.id
    )
    assert member["department_ids"] == [local.id]
    assert foreign.id not in member["department_ids"]
    assert "Foreign Secret Department" not in resp.content.decode()


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


# Slack IDs authorize private document answers; handles remain display-only.

def _save_slack_member(client, **overrides):
    payload = {
        "email": "slack-member@example.com", "password": "original-password",
        "name": "Slack Member", "is_owner": True, "slack_handle": "@display-name",
        "slack_user_id": "U0123456789", "group_ids": [], "department_ids": [],
    }
    payload.update(overrides)
    return client.post("/api/org/members/save/", data=json.dumps(payload),
                       content_type="application/json")


@pytest.mark.django_db
def test_member_slack_identity_create_and_list(admin_client, admin_user, org):
    response = _save_slack_member(admin_client)
    assert response.status_code == 200, response.content
    profile = DataPerson.objects.get(user_id=response.json()["user_id"], organization=org)
    assert profile.slack_user_id == "U0123456789"
    assert profile.slack_handle == "@display-name"
    members = admin_client.get("/api/org/members/").json()["members"]
    assert next(m for m in members if m["user_id"] == profile.user_id)["slack_user_id"] == "U0123456789"
    assert next(m for m in members if m["user_id"] == admin_user.pk)["slack_user_id"] == ""


@pytest.mark.django_db
def test_member_slack_identity_edit_uses_linked_profile_in_current_org(admin_client, org):
    user_id = _save_slack_member(admin_client).json()["user_id"]
    other_org = Organization.objects.create(name="Another Slack workspace")
    OrganizationMembership.objects.create(user_id=user_id, organization=other_org)
    foreign_profile = DataPerson.objects.create(
        organization=other_org, user_id=user_id, name="Same login elsewhere", slack_user_id="UFOREIGN123",
    )
    response = _save_slack_member(admin_client, user_id=user_id, name="Changed name", slack_user_id="WUPDATED123")
    assert response.status_code == 200, response.content
    assert DataPerson.objects.get(user_id=user_id, organization=org).slack_user_id == "WUPDATED123"
    foreign_profile.refresh_from_db()
    assert foreign_profile.slack_user_id == "UFOREIGN123"
    assert foreign_profile.name == "Same login elsewhere"


@pytest.mark.django_db
def test_member_slack_identity_omitted_preserves_and_empty_clears(admin_client, org):
    user_id = _save_slack_member(admin_client).json()["user_id"]
    response = admin_client.post("/api/org/members/save/", data=json.dumps({
        "user_id": user_id, "name": "Slack Member", "is_owner": True,
    }), content_type="application/json")
    assert response.status_code == 200, response.content
    assert DataPerson.objects.get(user_id=user_id, organization=org).slack_user_id == "U0123456789"
    response = _save_slack_member(admin_client, user_id=user_id, slack_user_id="")
    assert response.status_code == 200, response.content
    assert DataPerson.objects.get(user_id=user_id, organization=org).slack_user_id == ""


@pytest.mark.django_db
@pytest.mark.parametrize("invalid_id", ["@nickname", "U1", "U" + "1" * 80, None, []])
def test_invalid_member_slack_identity_rolls_back_new_account(admin_client, org, invalid_id):
    response = _save_slack_member(admin_client, slack_user_id=invalid_id)
    assert response.status_code == 400, response.content
    assert "slack_user_id" in response.json()["fields"]
    assert not CustomUser.objects.filter(email="slack-member@example.com").exists()
    assert not DataPerson.objects.filter(organization=org, name="Slack Member").exists()


@pytest.mark.django_db
@pytest.mark.parametrize("concurrent_assignment", [False, True])
def test_duplicate_member_slack_identity_rolls_back_all_edits(
        admin_client, org, monkeypatch, concurrent_assignment):
    from catalog.serializers import DataPersonSerializer

    user_id = _save_slack_member(admin_client).json()["user_id"]
    member = CustomUser.objects.get(pk=user_id)
    company = Group.objects.get_or_create(name="Company")[0]
    member.groups.add(company)
    DataPerson.objects.create(organization=org, name="Already mapped", slack_user_id="UTAKEN123")
    if concurrent_assignment:
        # Simulate a competing assignment after validation has passed: the
        # real PostgreSQL unique constraint must still roll back this request.
        monkeypatch.setattr(DataPersonSerializer, "validate", lambda self, attrs: attrs)
        monkeypatch.setattr(DataPersonSerializer, "get_validators", lambda self: [])
    response = _save_slack_member(
        admin_client, user_id=user_id, name="Must roll back", password="changed-password",
        is_admin=True, group_ids=[], slack_user_id="UTAKEN123",
    )
    assert response.status_code == 400, response.content
    assert "already linked" in response.json()["error"]
    member.refresh_from_db()
    assert member.check_password("original-password")
    assert list(member.groups.values_list("pk", flat=True)) == [company.pk]
    assert not OrganizationMembership.objects.get(user=member, organization=org).is_admin
    profile = DataPerson.objects.get(user=member, organization=org)
    assert profile.name == "Slack Member"
    assert profile.slack_user_id == "U0123456789"


@pytest.mark.django_db
def test_member_slack_identity_duplicates_are_scoped_to_org(admin_client, org):
    other_org = Organization.objects.create(name="Different workspace")
    DataPerson.objects.create(organization=other_org, name="Foreign member", slack_user_id="U0123456789")
    response = _save_slack_member(admin_client)
    assert response.status_code == 200, response.content


@pytest.mark.django_db
@pytest.mark.parametrize("edit_by_id", [False, True])
def test_member_slack_identity_cannot_claim_foreign_account(admin_client, org, edit_by_id):
    other_org = Organization.objects.create(name="Foreign account org")
    foreign = CustomUser.objects.create_user(username="foreign", email="foreign@example.com", password="private")
    OrganizationMembership.objects.create(user=foreign, organization=other_org)
    profile = DataPerson.objects.create(organization=other_org, user=foreign, name="Foreign", slack_user_id="UFOREIGN123")
    overrides = {"email": foreign.email}
    if edit_by_id:
        overrides["user_id"] = foreign.pk
    response = _save_slack_member(admin_client, **overrides)
    assert response.status_code == (404 if edit_by_id else 409), response.content
    assert not OrganizationMembership.objects.filter(user=foreign, organization=org).exists()
    assert not DataPerson.objects.filter(user=foreign, organization=org).exists()
    foreign.refresh_from_db()
    profile.refresh_from_db()
    assert foreign.check_password("private")
    assert profile.slack_user_id == "UFOREIGN123"


@pytest.mark.django_db
def test_member_slack_identity_requires_active_account_but_allows_clearing(admin_client, org):
    user_id = _save_slack_member(admin_client).json()["user_id"]
    CustomUser.objects.filter(pk=user_id).update(is_active=False)
    response = _save_slack_member(admin_client, user_id=user_id, slack_user_id="UUPDATED123")
    assert response.status_code == 400
    assert "active member" in response.json()["error"]
    assert DataPerson.objects.get(user_id=user_id, organization=org).slack_user_id == "U0123456789"
    response = _save_slack_member(admin_client, user_id=user_id, slack_user_id="")
    assert response.status_code == 200, response.content
    assert DataPerson.objects.get(user_id=user_id, organization=org).slack_user_id == ""


@pytest.mark.django_db
def test_member_removal_clears_only_current_org_slack_identity(admin_client, org):
    user_id = _save_slack_member(admin_client).json()["user_id"]
    other_org = Organization.objects.create(name="Remaining membership")
    OrganizationMembership.objects.create(user_id=user_id, organization=other_org)
    foreign = DataPerson.objects.create(organization=other_org, user_id=user_id, name="Foreign", slack_user_id="UFOREIGN123")
    response = admin_client.post("/api/org/members/remove/", data=json.dumps({"user_id": user_id}),
                                 content_type="application/json")
    assert response.status_code == 200, response.content
    assert not OrganizationMembership.objects.filter(user_id=user_id, organization=org).exists()
    assert DataPerson.objects.get(user_id=user_id, organization=org).slack_user_id == ""
    foreign.refresh_from_db()
    assert foreign.slack_user_id == "UFOREIGN123"
    assert OrganizationMembership.objects.filter(user_id=user_id, organization=other_org).exists()


@pytest.mark.django_db
def test_member_slack_identity_update_requires_org_admin(client, rw_user):
    client.force_login(rw_user)
    response = _save_slack_member(client, user_id=rw_user.pk)
    assert response.status_code == 403
    assert not DataPerson.objects.filter(user=rw_user, slack_user_id="U0123456789").exists()
