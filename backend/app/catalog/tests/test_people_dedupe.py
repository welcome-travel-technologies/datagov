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
from io import StringIO

import pytest

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext

from catalog.access import DuplicateDataPersonName, upsert_data_person
from catalog.people_dedupe import dedupe_data_persons
from catalog.models import (
    CustomUser, DataPerson, Definition, Department, GovernanceTask, Item,
    ItemGroup, Organization,
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
        if c.name in (
            'uniq_dataperson_name_org', 'uniq_dataperson_user_org',
            'dataperson_name_not_blank',
        )
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
    return dedupe_data_persons(
        DataPerson, ItemGroup, GovernanceTask, Definition=Definition, apply=apply,
    )


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

    def test_login_less_namesake_is_never_merged(self, org, user, duplicates_allowed):
        """Namesakes survive independently and keep their existing references."""
        account = DataPerson.objects.create(
            name='Jane Doe', organization=org, user=user,
            is_owner=True, is_steward=False,
        )
        namesake = DataPerson.objects.create(
            name='Jane Doe', organization=org, is_owner=False, is_steward=True,
        )
        grp = _group(org)
        grp.ownership_person = namesake
        grp.steward = namesake
        grp.save(update_fields=['ownership_person', 'steward'])
        task = GovernanceTask.objects.create(
            organization=org, item_group=grp, assignee=namesake,
            reason='ATTENTION', title='Review "Revenue"',
        )

        summary = _dedupe()

        assert summary['clusters'] == 0
        assert summary['merged_rows'] == 0
        assert summary['name_conflicts'] == 1
        assert summary['renamed_rows'] == 1
        assert summary['repointed_groups'] == 0
        assert summary['repointed_tasks'] == 0

        assert DataPerson.objects.filter(id__in=[account.id, namesake.id]).count() == 2
        grp.refresh_from_db()
        task.refresh_from_db()
        namesake.refresh_from_db()
        assert grp.ownership_person_id == namesake.id
        assert grp.steward_id == namesake.id
        assert task.assignee_id == namesake.id
        assert namesake.name == f'Jane Doe (data person {namesake.id})'

    def test_merge_unions_departments_and_ors_role_flags(self, org, user, dept,
                                                         duplicates_allowed):
        """A merge must never shrink someone's reach, or they'd vanish from a
        dropdown they legitimately belonged in."""
        dept.organization = org
        dept.save(update_fields=['organization'])
        finance = Department.objects.create(name='Finance', organization=org)
        survivor = DataPerson.objects.create(
            name='Jane Doe', organization=org, user=user,
            slack_handle='@jane',
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
        uniq_dataperson_user_org unsatisfiable, so migration 0057 aborted with no
        way to re-run. Merge per org/login first, then rename what genuinely
        differs.
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
        # One row survives per org/login, so the constraint is satisfiable.
        assert not DataPerson.objects.filter(id=a2.id).exists()
        assert DataPerson.objects.filter(user=user).count() == 1
        assert DataPerson.objects.filter(user=other_login).count() == 1
        # ...and the two survivors no longer share a name.
        names = set(DataPerson.objects.filter(id__in=[a1.id, b1.id])
                    .values_list('name', flat=True))
        assert len(names) == 2

    def test_same_login_in_different_orgs_is_not_merged(self, org, user):
        other_org = Organization.objects.create(name='Other tenant')
        first = DataPerson.objects.create(
            name='Jane Doe', organization=org, user=user,
        )
        second = DataPerson.objects.create(
            name='Jane Doe', organization=other_org, user=user,
        )

        summary = _dedupe()

        assert summary['clusters'] == 0
        assert summary['merged_rows'] == 0
        assert DataPerson.objects.filter(pk__in=[first.pk, second.pk]).count() == 2

    def test_same_slack_handle_on_orgless_rows_is_not_merged(self):
        first = DataPerson.objects.create(
            name='Legacy Jane A', slack_handle='@jane',
        )
        second = DataPerson.objects.create(
            name='Legacy Jane B', slack_handle='@jane',
        )

        summary = _dedupe()

        assert summary['clusters'] == 0
        assert summary['merged_rows'] == 0
        assert DataPerson.objects.filter(pk__in=[first.pk, second.pk]).count() == 2

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
        survivor = DataPerson.objects.create(
            name='Jane Doe', organization=org, user=user, slack_handle='@jane',
        )
        twin = DataPerson.objects.create(
            name='Jane Doe', organization=org, slack_handle='@jane',
        )
        item = Item.objects.create(
            item_id='m_rev', item_name='Revenue', item_type='PB_MEASURE',
            organization=org,
        )
        _legacy_item_person(item.item_id, twin.id)

        summary = _dedupe()

        assert summary['repointed_legacy_items'] == 1
        assert not DataPerson.objects.filter(id=twin.id).exists()
        assert _legacy_item_person(item.item_id) == survivor.id

    def test_definition_ownership_is_repointed_before_delete(
            self, org, user, duplicates_allowed):
        survivor = DataPerson.objects.create(
            name='Jane Doe', organization=org, user=user, slack_handle='@jane',
        )
        duplicate = DataPerson.objects.create(
            name='J. Doe', organization=org, slack_handle='@jane',
        )
        definition = Definition.objects.create(
            name='Revenue', organization=org, ownership_person=duplicate,
        )

        summary = _dedupe()

        assert summary['repointed_definitions'] == 1
        assert not DataPerson.objects.filter(id=duplicate.id).exists()
        definition.refresh_from_db()
        assert definition.ownership_person_id == survivor.id

    def test_report_only_run_changes_nothing(self, org, user, duplicates_allowed):
        """apply=False is what the management command's default pass does — it
        has to be safe to run against production."""
        DataPerson.objects.create(
            name='Jane Doe', organization=org, user=user, slack_handle='@jane',
        )
        twin = DataPerson.objects.create(
            name='Jane Doe', organization=org, slack_handle='@jane',
        )

        summary = _dedupe(apply=False)

        assert summary['clusters'] == 1
        assert summary['merged_rows'] == 1
        assert DataPerson.objects.filter(id=twin.id).exists()

    def test_empty_and_null_org_names_are_repaired_without_merging(
            self, duplicates_allowed):
        rows = [
            DataPerson.objects.create(name='   '),
            DataPerson.objects.create(name='\t'),
            DataPerson.objects.create(name=' Same Name '),
            DataPerson.objects.create(name='same name'),
        ]

        summary = _dedupe()

        assert summary['merged_rows'] == 0
        assert DataPerson.objects.filter(id__in=[row.id for row in rows]).count() == 4
        names = list(
            DataPerson.objects.filter(id__in=[row.id for row in rows])
            .order_by('id')
            .values_list('name', flat=True)
        )
        assert all(name.strip() for name in names)
        assert len({name.strip().lower() for name in names}) == 4
        assert summary['name_conflicts'] == 1


@pytest.mark.django_db
class TestReviewedMergeCommand:

    @staticmethod
    def _csv(tmp_path, rows, header='survivor_id,loser_id'):
        path = tmp_path / 'reviewed-merges.csv'
        body = '\n'.join(f'{survivor},{loser}' for survivor, loser in rows)
        path.write_text(f'{header}\n{body}\n', encoding='utf-8')
        return path

    def test_apply_repoints_every_reference_and_unions_identity_data(
            self, org, user, tmp_path):
        engineering = Department.objects.create(
            name='Engineering', organization=org,
        )
        finance = Department.objects.create(name='Finance', organization=org)
        other_org = Organization.objects.create(name='Neighbour tenant')
        foreign_department = Department.objects.create(
            name='Foreign Secret', organization=other_org,
        )
        survivor = DataPerson.objects.create(
            name='Alice Canonical', organization=org, user=user,
            is_owner=True,
        )
        survivor.departments.add(engineering)
        loser = DataPerson.objects.create(
            name='Alice Legacy', organization=org, slack_handle='@alice',
            is_steward=True, is_other=True,
        )
        loser.departments.add(finance, foreign_department)

        group = _group(org)
        group.ownership_person = loser
        group.steward = loser
        group.save(update_fields=['ownership_person', 'steward'])
        task = GovernanceTask.objects.create(
            organization=org, item_group=group, assignee=loser,
            assignee_role='steward', reason='ATTENTION',
            title='Review Revenue',
        )
        definition = Definition.objects.create(
            name='Revenue', organization=org, ownership_person=loser,
        )
        item = Item.objects.create(
            item_id='reviewed_merge_item', item_name='Revenue',
            item_type='PB_MEASURE', organization=org,
        )
        _legacy_item_person(item.item_id, loser.id)

        output = StringIO()
        with CaptureQueriesContext(connection) as queries:
            call_command(
                'dedupe_data_persons',
                merge_csv=str(self._csv(tmp_path, [(survivor.id, loser.id)])),
                apply=True,
                stdout=output,
            )

        assert not DataPerson.objects.filter(id=loser.id).exists()
        assert any(
            'FOR UPDATE' in query['sql']
            and 'catalog_dataperson' in query['sql']
            for query in queries.captured_queries
        )
        survivor.refresh_from_db()
        group.refresh_from_db()
        task.refresh_from_db()
        definition.refresh_from_db()
        assert survivor.slack_handle == '@alice'
        assert survivor.is_owner is True
        assert survivor.is_steward is True
        assert survivor.is_other is True
        assert set(survivor.departments.values_list('id', flat=True)) == {
            engineering.id, finance.id,
        }
        assert group.ownership_person_id == survivor.id
        assert group.steward_id == survivor.id
        assert task.assignee_id == survivor.id
        assert definition.ownership_person_id == survivor.id
        assert _legacy_item_person(item.item_id) == survivor.id
        assert 'Merged: clusters=1, merged_rows=1' in output.getvalue()

    def test_default_is_a_real_dry_run(self, org, user, tmp_path):
        survivor = DataPerson.objects.create(
            name='Alice Canonical', organization=org, user=user,
        )
        loser = DataPerson.objects.create(
            name='Alice Legacy', organization=org,
        )
        group = _group(org)
        group.steward = loser
        group.save(update_fields=['steward'])

        output = StringIO()
        call_command(
            'dedupe_data_persons',
            merge_csv=str(self._csv(tmp_path, [(survivor.id, loser.id)])),
            stdout=output,
        )

        assert DataPerson.objects.filter(id=loser.id).exists()
        group.refresh_from_db()
        assert group.steward_id == loser.id
        assert 'Would merge: clusters=1, merged_rows=1' in output.getvalue()
        assert 'NOTHING WAS WRITTEN' in output.getvalue()

    def test_reviewed_merge_restores_plain_name_from_automatic_id_suffix(
            self, org, user, tmp_path):
        loser = DataPerson.objects.create(
            name='Aris Apostolopoulos', organization=org, is_owner=True,
        )
        survivor = DataPerson.objects.create(
            name='Temporary linked name', organization=org, user=user,
            is_owner=True,
        )
        suffixed = f'Aris Apostolopoulos (data person {survivor.id})'
        DataPerson.objects.filter(pk=survivor.pk).update(name=suffixed)

        group = _group(org)
        group.ownership_person = loser
        group.save(update_fields=['ownership_person'])

        output = StringIO()
        call_command(
            'dedupe_data_persons',
            org=org.id,
            merge_csv=str(self._csv(
                tmp_path, [(survivor.id, loser.id)],
            )),
            apply=True,
            stdout=output,
        )

        survivor.refresh_from_db()
        group.refresh_from_db()
        assert survivor.name == 'Aris Apostolopoulos'
        assert group.ownership_person_id == survivor.id
        assert not DataPerson.objects.filter(pk=loser.pk).exists()
        assert 'renamed_rows=1' in output.getvalue()
        assert 'restore plain name' in output.getvalue()

    def test_org_option_exactly_scopes_a_reviewed_plan(
            self, org, tmp_path):
        other_org = Organization.objects.create(name='Other tenant')
        matching_survivor = DataPerson.objects.create(
            name='Alice Canonical', organization=org,
        )
        matching_loser = DataPerson.objects.create(
            name='Alice Legacy', organization=org,
        )
        foreign_survivor = DataPerson.objects.create(
            name='Bob Canonical', organization=other_org,
        )
        foreign_loser = DataPerson.objects.create(
            name='Bob Legacy', organization=other_org,
        )

        matching_path = self._csv(
            tmp_path, [(matching_survivor.id, matching_loser.id)],
        )
        output = StringIO()
        call_command(
            'dedupe_data_persons',
            org=org.id,
            merge_csv=str(matching_path),
            stdout=output,
        )
        assert 'Would merge: clusters=1, merged_rows=1' in output.getvalue()

        foreign_path = self._csv(
            tmp_path, [(foreign_survivor.id, foreign_loser.id)],
        )
        with pytest.raises(
                CommandError, match=f'belong to organization #{org.id}'):
            call_command(
                'dedupe_data_persons',
                org=org.id,
                merge_csv=str(foreign_path),
                apply=True,
            )

        assert DataPerson.objects.filter(
            id__in=[
                matching_survivor.id, matching_loser.id,
                foreign_survivor.id, foreign_loser.id,
            ],
        ).count() == 4

    def test_org_option_rejects_an_unknown_organization(
            self, org, tmp_path):
        survivor = DataPerson.objects.create(
            name='Alice Canonical', organization=org,
        )
        loser = DataPerson.objects.create(
            name='Alice Legacy', organization=org,
        )
        path = self._csv(tmp_path, [(survivor.id, loser.id)])

        with pytest.raises(CommandError, match='Organization id=999999 not found'):
            call_command(
                'dedupe_data_persons',
                org=999999,
                merge_csv=str(path),
            )

    def test_rejects_cross_org_and_orgless_pairs(self, org, tmp_path):
        other_org = Organization.objects.create(name='Other tenant')
        survivor = DataPerson.objects.create(
            name='Alice Canonical', organization=org,
        )
        cross_org = DataPerson.objects.create(
            name='Alice Elsewhere', organization=other_org,
        )
        orgless_survivor = DataPerson.objects.create(name='Legacy Alice')
        orgless_loser = DataPerson.objects.create(name='Legacy Alice Alias')

        cross_path = self._csv(
            tmp_path, [(survivor.id, cross_org.id)],
        )
        with pytest.raises(CommandError, match='organizations differ'):
            call_command(
                'dedupe_data_persons', merge_csv=str(cross_path), apply=True,
            )

        orgless_path = self._csv(
            tmp_path, [(orgless_survivor.id, orgless_loser.id)],
        )
        with pytest.raises(CommandError, match='assigned to the same organization'):
            call_command(
                'dedupe_data_persons', merge_csv=str(orgless_path), apply=True,
            )
        assert DataPerson.objects.filter(
            id__in=[
                survivor.id, cross_org.id, orgless_survivor.id, orgless_loser.id,
            ],
        ).count() == 4

    def test_validates_complete_cluster_identity_before_writing(
            self, org, tmp_path):
        survivor = DataPerson.objects.create(
            name='Alice Canonical', organization=org,
        )
        first = DataPerson.objects.create(
            name='Alice First Alias', organization=org, slack_handle='@alice',
        )
        second = DataPerson.objects.create(
            name='Alice Second Alias', organization=org, slack_handle='@bob',
        )
        path = self._csv(
            tmp_path,
            [(survivor.id, first.id), (survivor.id, second.id)],
        )

        with pytest.raises(CommandError, match='complete reviewed cluster'):
            call_command(
                'dedupe_data_persons', merge_csv=str(path), apply=True,
            )

        assert DataPerson.objects.filter(
            id__in=[survivor.id, first.id, second.id],
        ).count() == 3
        survivor.refresh_from_db()
        assert survivor.slack_handle is None

    def test_rejects_login_conflicts_and_requires_linked_survivor(
            self, org, user, tmp_path):
        other_user = CustomUser.objects.create_user(
            username='other-reviewed',
            email='other-reviewed@example.com',
            password='testpass',
        )
        linked_survivor = DataPerson.objects.create(
            name='Alice Linked', organization=org, user=user,
        )
        other_linked = DataPerson.objects.create(
            name='Bob Linked', organization=org, user=other_user,
        )
        unlinked_survivor = DataPerson.objects.create(
            name='Alice Unlinked', organization=org,
        )

        conflict_path = self._csv(
            tmp_path, [(linked_survivor.id, other_linked.id)],
        )
        with pytest.raises(CommandError, match='different linked logins'):
            call_command(
                'dedupe_data_persons', merge_csv=str(conflict_path), apply=True,
            )

        wrong_survivor_path = self._csv(
            tmp_path, [(unlinked_survivor.id, linked_survivor.id)],
        )
        with pytest.raises(CommandError, match='choose the linked row as survivor'):
            call_command(
                'dedupe_data_persons',
                merge_csv=str(wrong_survivor_path),
                apply=True,
            )

    def test_rejects_an_inexact_csv_header(self, org, tmp_path):
        survivor = DataPerson.objects.create(
            name='Alice Canonical', organization=org,
        )
        loser = DataPerson.objects.create(
            name='Alice Legacy', organization=org,
        )
        path = self._csv(
            tmp_path, [(survivor.id, loser.id)],
            header='winner_id,loser_id',
        )

        with pytest.raises(CommandError, match='header must be exactly'):
            call_command('dedupe_data_persons', merge_csv=str(path))

    def test_rejects_extra_csv_cells_instead_of_silently_ignoring_them(
            self, org, tmp_path):
        survivor = DataPerson.objects.create(
            name='Alice Canonical', organization=org,
        )
        loser = DataPerson.objects.create(
            name='Alice Legacy', organization=org,
        )
        path = tmp_path / 'reviewed-merges.csv'
        path.write_text(
            'survivor_id,loser_id\n'
            f'{survivor.id},{loser.id},999999\n',
            encoding='utf-8',
        )

        with pytest.raises(CommandError, match='must contain exactly two columns'):
            call_command(
                'dedupe_data_persons',
                org=org.id,
                merge_csv=str(path),
                apply=True,
            )

        assert DataPerson.objects.filter(pk=loser.pk).exists()


@pytest.mark.django_db
class TestNameConstraints:

    @pytest.mark.parametrize('Model', [DataPerson, Definition])
    def test_whitespace_only_name_is_rejected(self, Model):
        with pytest.raises(IntegrityError), transaction.atomic():
            Model.objects.create(name=' \t ')

    @pytest.mark.parametrize('Model', [DataPerson, Definition])
    def test_null_org_name_uniqueness_is_trimmed_and_case_insensitive(self, Model):
        Model.objects.create(name=' Jane Doe ')
        with pytest.raises(IntegrityError), transaction.atomic():
            Model.objects.create(name='jane doe')


@pytest.mark.django_db
class TestUpsertDataPerson:

    def test_namesake_requires_explicit_link_instead_of_silent_adoption(self, org, user):
        """A name is not proof of identity, so a login-less row is not claimed."""
        existing = DataPerson.objects.create(
            name='Jane Doe', organization=org, is_owner=True,
        )

        with pytest.raises(DuplicateDataPersonName):
            upsert_data_person(user, org, 'jane doe', is_steward=True)

        existing.refresh_from_db()
        assert existing.user_id is None
        assert existing.is_steward is False
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

    def test_same_login_gets_an_independent_profile_in_each_org(self, org, user):
        first = upsert_data_person(user, org, 'Jane Doe')
        other_org = Organization.objects.create(name='Other tenant')
        second = upsert_data_person(user, other_org, 'Jane Doe')

        assert first.id != second.id
        assert first.organization_id == org.id
        assert second.organization_id == other_org.id
        assert DataPerson.objects.filter(user=user).count() == 2


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
