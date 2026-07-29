"""
Tests for the DataPerson dedupe (catalog/people_dedupe.py) and the upsert that
stops duplicates being minted in the first place (access.upsert_data_person).

The bug being guarded: DataPerson shipped without a uniqueness rule and the
member-save upsert matched on `user` alone, so a login-less row for someone
(Django admin / bulk import) was never found when they later got an account —
and a second row with the same name appeared beside it in every Owner / Steward
dropdown.

Note the fixture below: migration 0056 made those duplicates impossible to
create, so recreating the legacy state means taking the constraints off first.
"""
import pytest

from django.db import connection

from catalog.access import DuplicateDataPersonName, upsert_data_person
from catalog.people_dedupe import dedupe_data_persons
from catalog.models import (
    CustomUser, DataPerson, Department, GovernanceTask, Item, ItemGroup,
)


@pytest.fixture
def duplicates_allowed(db):
    """Drop the DataPerson uniqueness constraints for one test.

    The rows this module cleans up can no longer be created through the ORM, so
    a test of the cleaner has to reproduce the pre-0056 state by hand. Nothing
    is put back: pytest-django rolls the test's transaction back and Postgres
    DDL is transactional, so the schema restores itself.
    """
    targets = [
        c for c in DataPerson._meta.constraints
        if c.name in ('uniq_dataperson_name_org', 'uniq_dataperson_user')
    ]
    with connection.schema_editor(atomic=False) as editor:
        for constraint in targets:
            editor.remove_constraint(DataPerson, constraint)
    yield


def _group(org, key='grp::revenue'):
    return ItemGroup.objects.create(
        group_key=key, kind=ItemGroup.KIND_MEASURE_NAME, organization=org,
    )


def _dedupe(apply=True):
    return dedupe_data_persons(DataPerson, ItemGroup, GovernanceTask, apply=apply)


def _legacy_item_person(item_id, person_id=None):
    """Read (or write) ``catalog_item.ownership_person_id``.

    Migration 0029 removed the column from Django's model state only, so the ORM
    cannot reach it — raw SQL is the only way to set up and inspect it.
    """
    with connection.cursor() as cursor:
        if person_id is not None:
            cursor.execute(
                'UPDATE catalog_item SET ownership_person_id = %s WHERE item_id = %s',
                [person_id, item_id],
            )
            return person_id
        cursor.execute(
            'SELECT ownership_person_id FROM catalog_item WHERE item_id = %s', [item_id])
        return cursor.fetchone()[0]


@pytest.mark.django_db
class TestMergeDuplicates:

    def test_login_less_twin_merges_into_the_account(self, org, user, duplicates_allowed):
        """The row with a login survives — it's the one the member-save upsert
        keys on — and every reference to the twin is repointed at it."""
        survivor = DataPerson.objects.create(
            name='Jane Doe', organization=org, user=user,
            is_owner=True, is_steward=False,
        )
        twin = DataPerson.objects.create(
            name='Jane Doe', organization=org, is_owner=False, is_steward=True,
        )
        grp = _group(org)
        grp.ownership_person = twin
        grp.steward = twin
        grp.save(update_fields=['ownership_person', 'steward'])
        task = GovernanceTask.objects.create(
            organization=org, item_group=grp, assignee=twin,
            reason='ATTENTION', title='Review "Revenue"',
        )

        summary = _dedupe()

        assert summary['clusters'] == 1
        assert summary['merged_rows'] == 1
        assert summary['conflicts'] == 0
        # ownership_person and steward are counted separately — one group, two refs.
        assert summary['repointed_groups'] == 2
        assert summary['repointed_tasks'] == 1

        assert not DataPerson.objects.filter(id=twin.id).exists()
        grp.refresh_from_db()
        task.refresh_from_db()
        assert grp.ownership_person_id == survivor.id
        assert grp.steward_id == survivor.id
        assert task.assignee_id == survivor.id

    def test_merge_unions_departments_and_ors_role_flags(self, org, user, dept,
                                                         duplicates_allowed):
        """A merge must never shrink someone's reach, or they'd vanish from a
        dropdown they legitimately belonged in."""
        finance = Department.objects.create(name='Finance')
        survivor = DataPerson.objects.create(
            name='Jane Doe', organization=org, user=user,
            is_owner=True, is_steward=False, is_other=False,
        )
        survivor.departments.add(dept)
        twin = DataPerson.objects.create(
            name='Jane Doe', organization=org, slack_handle='@jane',
            is_owner=False, is_steward=True, is_other=True,
        )
        twin.departments.add(finance)

        _dedupe()

        survivor.refresh_from_db()
        assert set(survivor.departments.values_list('id', flat=True)) == {dept.id, finance.id}
        assert survivor.is_owner is True
        assert survivor.is_steward is True
        assert survivor.is_other is True
        assert survivor.slack_handle == '@jane'

    def test_same_name_different_logins_is_renamed_not_merged(self, org, user,
                                                              duplicates_allowed):
        """Two logins sharing a name are two humans, not a duplicate. They're
        disambiguated so both survive and the constraint still holds."""
        other_login = CustomUser.objects.create_user(
            username='jane2', email='jane2@example.com', password='testpass',
        )
        first = DataPerson.objects.create(name='Jane Doe', organization=org, user=user)
        second = DataPerson.objects.create(name='Jane Doe', organization=org, user=other_login)

        summary = _dedupe()

        assert summary['conflicts'] == 1
        assert summary['merged_rows'] == 0
        assert summary['renamed_rows'] == 1

        assert DataPerson.objects.filter(id__in=[first.id, second.id]).count() == 2
        first.refresh_from_db()
        second.refresh_from_db()
        assert first.name == 'Jane Doe'          # lowest pk keeps the plain name
        assert second.name == 'Jane Doe (jane2@example.com)'

    def test_conflict_cluster_still_merges_within_each_login(self, org, user,
                                                             duplicates_allowed):
        """"Two humans, don't merge" must not mean "do nothing".

        A conflicted cluster can still hold several rows for the SAME login, and
        those are unambiguously one person. Leaving them alone left
        uniq_dataperson_user unsatisfiable, so migration 0057 aborted with no way
        to re-run. Merge per login first, then rename what genuinely differs.
        """
        other_login = CustomUser.objects.create_user(
            username='jane2', email='jane2@example.com', password='testpass',
        )
        a1 = DataPerson.objects.create(name='Jane Doe', organization=org, user=user)
        a2 = DataPerson.objects.create(name='Jane Doe', organization=org, user=user)
        b1 = DataPerson.objects.create(name='Jane Doe', organization=org, user=other_login)

        summary = _dedupe()

        assert summary['conflicts'] == 1
        assert summary['merged_rows'] == 1          # a2 folded into a1
        # One row survives per login, so the per-login constraint is satisfiable.
        assert not DataPerson.objects.filter(id=a2.id).exists()
        assert DataPerson.objects.filter(user=user).count() == 1
        assert DataPerson.objects.filter(user=other_login).count() == 1
        # ...and the two survivors no longer share a name.
        names = set(DataPerson.objects.filter(id__in=[a1.id, b1.id])
                    .values_list('name', flat=True))
        assert len(names) == 2

    def test_disambiguated_name_fits_and_stays_distinct(self, org, user,
                                                        duplicates_allowed):
        """Truncating the FINISHED name could chop the suffix off and hand back
        the very name it was meant to differ from — re-colliding at max_length."""
        other_login = CustomUser.objects.create_user(
            username='u2', email='a-very-long-address@example-company.com',
            password='testpass',
        )
        long_name = 'X' * 255
        first = DataPerson.objects.create(name=long_name, organization=org, user=user)
        second = DataPerson.objects.create(name=long_name, organization=org, user=other_login)

        _dedupe()

        first.refresh_from_db()
        second.refresh_from_db()
        assert len(second.name) <= 255
        assert second.name.lower() != first.name.lower()

    def test_legacy_item_columns_are_repointed_before_the_delete(self, org, user,
                                                                 duplicates_allowed):
        """Migration 0029 moved governance to ItemGroup in Django's model *state*
        only: catalog_item.ownership_person_id and its FK to catalog_dataperson
        are still live in the database. A merge that ignored them would leave a
        dangling reference and blow up with a foreign key violation at COMMIT
        (the FK is DEFERRABLE INITIALLY DEFERRED) — which no test touching only
        the ORM-visible references would ever see."""
        survivor = DataPerson.objects.create(name='Jane Doe', organization=org, user=user)
        twin = DataPerson.objects.create(name='Jane Doe', organization=org)
        item = Item.objects.create(
            item_id='m_rev', item_name='Revenue', item_type='PB_MEASURE',
            organization=org,
        )
        _legacy_item_person(item.item_id, twin.id)

        summary = _dedupe()

        assert summary['repointed_legacy_items'] == 1
        assert not DataPerson.objects.filter(id=twin.id).exists()
        assert _legacy_item_person(item.item_id) == survivor.id

    def test_report_only_run_changes_nothing(self, org, user, duplicates_allowed):
        """apply=False is what the management command's default pass does — it
        has to be safe to run against production."""
        DataPerson.objects.create(name='Jane Doe', organization=org, user=user)
        twin = DataPerson.objects.create(name='Jane Doe', organization=org)

        summary = _dedupe(apply=False)

        assert summary['clusters'] == 1
        assert summary['merged_rows'] == 1
        assert DataPerson.objects.filter(id=twin.id).exists()


@pytest.mark.django_db
class TestUpsertDataPerson:

    def test_adopts_an_existing_login_less_namesake(self, org, user):
        """The fix at the source: adopt the namesake instead of minting a twin.
        Matching is case-insensitive because the row may have been typed by hand."""
        existing = DataPerson.objects.create(
            name='Jane Doe', organization=org, is_owner=True,
        )

        person = upsert_data_person(user, org, 'jane doe', is_steward=True)

        assert person.id == existing.id
        assert person.user_id == user.id
        assert person.is_steward is True
        assert DataPerson.objects.filter(organization=org).count() == 1

    def test_matches_on_user_before_name(self, org, user):
        """A rename must update the linked row, not create a second one."""
        person = upsert_data_person(user, org, 'Jane Doe')
        renamed = upsert_data_person(user, org, 'Jane Doe-Smith')

        assert renamed.id == person.id
        assert DataPerson.objects.filter(user=user).count() == 1
        assert renamed.name == 'Jane Doe-Smith'

    def test_someone_elses_name_raises_instead_of_an_integrity_error(self, org, user):
        """uniq_dataperson_name_org turns a name clash into a database error, which
        would surface as a 500 from the member-save view. Detect it first so the
        caller can say so in words."""
        other_login = CustomUser.objects.create_user(
            username='other', email='other@example.com', password='testpass',
        )
        upsert_data_person(other_login, org, 'Jane Doe')

        with pytest.raises(DuplicateDataPersonName):
            upsert_data_person(user, org, 'jane doe')

        assert DataPerson.objects.filter(user=user).count() == 0


@pytest.mark.django_db
class TestMemberSaveNameClash:
    """The view-level half of the same problem: a clash must be a 400 with a
    readable message, and must not leave a half-created member behind."""

    def _save(self, client, **payload):
        import json
        return client.post(
            '/api/org/members/save/',
            data=json.dumps(payload),
            content_type='application/json',
        )

    def test_duplicate_name_is_a_400_and_writes_nothing(self, client, org):
        from catalog.models import OrganizationMembership

        admin = CustomUser.objects.create_user(
            username='boss', email='boss@example.com', password='testpass')
        OrganizationMembership.objects.create(user=admin, organization=org, is_admin=True)

        taken = CustomUser.objects.create_user(
            username='taken', email='taken@example.com', password='testpass')
        OrganizationMembership.objects.create(user=taken, organization=org)
        upsert_data_person(taken, org, 'Jane Doe', is_owner=True)

        client.login(username='boss@example.com', password='testpass')
        resp = self._save(
            client, email='newbie@example.com', password='pw12345',
            name='Jane Doe', is_owner=True, department_ids=[], group_ids=[],
        )

        assert resp.status_code == 400
        assert 'already named' in resp.json()['error']
        # Nothing half-written: no user, no membership, no second person.
        assert not CustomUser.objects.filter(email='newbie@example.com').exists()
        assert DataPerson.objects.filter(organization=org, name__iexact='Jane Doe').count() == 1
