import pytest

from catalog.management.commands.link_data_persons import Command
from catalog.models import (
    CustomUser, DataPerson, Organization, OrganizationMembership,
)


def _user(email):
    return CustomUser.objects.create_user(
        username=email, email=email, password='testpass',
    )


@pytest.mark.django_db
def test_apply_plan_rechecks_a_login_link_created_after_planning():
    org = Organization.objects.create(name='Org')
    user = _user('member@example.com')
    OrganizationMembership.objects.create(user=user, organization=org)
    planned_person = DataPerson.objects.create(
        name='Planned', organization=org,
    )

    # This appears after the plan was computed.
    current = DataPerson.objects.create(
        name='Current', organization=org, user=user,
    )
    applied, already, conflicts = Command()._apply_plan(
        [(planned_person, user)], org,
    )

    assert applied == []
    assert already == 0
    assert len(conflicts) == 1
    planned_person.refresh_from_db()
    current.refresh_from_db()
    assert planned_person.user_id is None
    assert current.user_id == user.pk


@pytest.mark.django_db
def test_same_login_can_have_an_independent_profile_in_each_org():
    first_org = Organization.objects.create(name='First')
    second_org = Organization.objects.create(name='Second')
    user = _user('member@example.com')
    OrganizationMembership.objects.create(user=user, organization=first_org)
    OrganizationMembership.objects.create(user=user, organization=second_org)
    first = DataPerson.objects.create(
        name='First profile', organization=first_org, user=user,
    )
    second = DataPerson.objects.create(
        name='Second profile', organization=second_org,
    )
    command = Command()

    plan, already, conflicts = command._reject_conflicts(
        [(second, user)], second_org,
    )
    applied, late_already, late_conflicts = command._apply_plan(
        plan, second_org,
    )

    assert already == 0
    assert conflicts == []
    assert late_already == 0
    assert late_conflicts == []
    assert [(person.pk, login.pk) for person, login in applied] == [
        (second.pk, user.pk),
    ]
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.user_id == user.pk
    assert second.user_id == user.pk
