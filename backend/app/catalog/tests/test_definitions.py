"""Definitions (the layer above ItemGroup) and the rename-carry that keeps a
renamed measure's curation.

Two separate promises are under test here:

1. A Definition does NOTHING on its own. Assigning groups to it, or editing it,
   must leave their governance untouched — metadata moves only when someone
   runs the explicit ``apply`` action.
2. Renaming a measure in Power BI must not cost its curation. The ETL reshuffles
   ``Item.group_id`` from the new name, which detaches the item from its group;
   the metadata has to travel with it onto whatever group it lands in.
"""
import json

import pytest

from catalog.models import (
    Category, DataPerson, Definition, Department, Item, ItemGroup,
    Organization, OrganizationMembership, CustomUser,
)
from catalog.services.item_groups import ensure_item_groups


def _measure(item_id, name, org, group_key=None, **kw):
    """A PB_MEASURE whose group_id follows the ETL's convention."""
    return Item.objects.create(
        item_id=item_id, item_name=name, item_type='PB_MEASURE',
        service='powerbi', organization=org,
        group_id=group_key or f'{org.id}::{name.strip().lower()}',
        workspace_name='WS', dataset_name='DS', **kw,
    )


def _curated_group(org, key, **kw):
    return ItemGroup.objects.create(
        group_key=key, kind=ItemGroup.KIND_MEASURE_NAME, organization=org, **kw)


@pytest.fixture
def people(db, org):
    return (
        DataPerson.objects.create(name='Alice', is_owner=True, organization=org),
        DataPerson.objects.create(name='Bob', is_steward=True, organization=org),
    )


@pytest.fixture
def dept(db, org):
    return Department.objects.create(name='Finance', organization=org)


@pytest.fixture
def admin_client(db, org, client):
    user = CustomUser.objects.create_user(
        username='defadmin', email='defadmin@example.com', password='testpass')
    OrganizationMembership.objects.create(user=user, organization=org, is_admin=True)
    client.login(username='defadmin@example.com', password='testpass')
    return client


# --------------------------------------------------------------------------
# 1. A definition is inert until you act on it
# --------------------------------------------------------------------------

@pytest.mark.django_db
class TestDefinitionIsInert:

    def test_assigning_a_group_changes_no_governance(self, org, people, dept):
        alice, bob = people
        definition = Definition.objects.create(
            name='Revenue', organization=org, ownership_person=alice,
            ownership_department=dept)
        group = _curated_group(org, '1::gross revenue', ownership_person=bob)

        group.definition = definition
        group.save(update_fields=['definition'])

        group.refresh_from_db()
        assert group.ownership_person_id == bob.id      # untouched
        assert group.ownership_department_id is None

    def test_editing_a_definition_changes_no_governance(self, org, people, dept):
        alice, bob = people
        definition = Definition.objects.create(name='Revenue', organization=org)
        group = _curated_group(org, '1::gross revenue', ownership_person=bob,
                               definition=definition)

        definition.ownership_person = alice
        definition.ownership_department = dept
        definition.save()

        group.refresh_from_db()
        assert group.ownership_person_id == bob.id
        assert group.ownership_department_id is None

    def test_deleting_a_definition_keeps_its_groups_and_their_metadata(
            self, org, people, dept):
        alice, _ = people
        definition = Definition.objects.create(name='Revenue', organization=org)
        group = _curated_group(org, '1::gross revenue', ownership_person=alice,
                               ownership_department=dept, definition=definition)

        definition.delete()

        group.refresh_from_db()
        assert group.pk is not None
        assert group.definition_id is None
        assert group.ownership_person_id == alice.id
        assert group.ownership_department_id == dept.id


# --------------------------------------------------------------------------
# 2. The apply action
# --------------------------------------------------------------------------

@pytest.mark.django_db
class TestApplyAction:

    def _apply(self, client, definition, **body):
        return client.post(
            f'/api/definitions/{definition.id}/apply/',
            data=json.dumps(body), content_type='application/json')

    def test_apply_pushes_owner_and_department_to_every_group(
            self, admin_client, org, people, dept):
        alice, bob = people
        definition = Definition.objects.create(
            name='Revenue', organization=org, ownership_person=alice,
            ownership_department=dept)
        a = _curated_group(org, '1::net revenue', definition=definition)
        b = _curated_group(org, '1::gross revenue', ownership_person=bob,
                           definition=definition)
        outside = _curated_group(org, '1::unrelated', ownership_person=bob)

        resp = self._apply(admin_client, definition)

        assert resp.status_code == 200
        assert resp.json()['updated'] == 2
        for group in (a, b):
            group.refresh_from_db()
            assert group.ownership_person_id == alice.id
            assert group.ownership_department_id == dept.id
        outside.refresh_from_db()
        assert outside.ownership_person_id == bob.id     # not a member, untouched

    def test_dry_run_writes_nothing(self, admin_client, org, people, dept):
        alice, bob = people
        definition = Definition.objects.create(
            name='Revenue', organization=org, ownership_person=alice)
        group = _curated_group(org, '1::net revenue', ownership_person=bob,
                               definition=definition)

        resp = self._apply(admin_client, definition, dry_run=True)

        assert resp.status_code == 200
        assert resp.json()['would_update'] == 1
        group.refresh_from_db()
        assert group.ownership_person_id == bob.id

    def test_unset_fields_are_skipped_not_blanked(self, admin_client, org, people, dept):
        """An empty field on the definition means "not specified". Treating it as
        "erase" would let assigning groups to a fresh definition wipe curation."""
        alice, bob = people
        definition = Definition.objects.create(
            name='Revenue', organization=org, ownership_person=alice)  # no dept
        group = _curated_group(org, '1::net revenue', ownership_person=bob,
                               ownership_department=dept, definition=definition)

        resp = self._apply(admin_client, definition)

        assert 'ownership_department' in resp.json()['skipped_unset']
        group.refresh_from_db()
        assert group.ownership_person_id == alice.id     # applied
        assert group.ownership_department_id == dept.id  # preserved

    def test_apply_with_nothing_set_is_a_no_op(self, admin_client, org, people):
        _, bob = people
        definition = Definition.objects.create(name='Revenue', organization=org)
        group = _curated_group(org, '1::net revenue', ownership_person=bob,
                               definition=definition)

        resp = self._apply(admin_client, definition)

        assert resp.json()['updated'] == 0
        group.refresh_from_db()
        assert group.ownership_person_id == bob.id

    def test_rerunning_apply_reports_zero_changed(self, admin_client, org, people, dept):
        alice, _ = people
        definition = Definition.objects.create(
            name='Revenue', organization=org, ownership_person=alice,
            ownership_department=dept)
        _curated_group(org, '1::net revenue', definition=definition)

        assert self._apply(admin_client, definition).json()['updated'] == 1
        assert self._apply(admin_client, definition).json()['updated'] == 0

    def test_apply_rejects_an_unknown_field(self, admin_client, org, people):
        alice, _ = people
        definition = Definition.objects.create(
            name='Revenue', organization=org, ownership_person=alice)
        resp = self._apply(admin_client, definition, fields=['status'])
        assert resp.status_code == 400


@pytest.mark.django_db
class TestDefinitionCrud:

    def test_duplicate_name_is_a_400_not_a_500(self, admin_client, org):
        """uniq_definition_name_org is an *expression* constraint (Lower(name)),
        and DRF's automatic uniqueness validation only covers unique_together —
        so without an explicit check the clash escapes as an IntegrityError."""
        Definition.objects.create(name='Revenue', organization=org)

        resp = admin_client.post(
            '/api/definitions/', data=json.dumps({'name': 'revenue'}),
            content_type='application/json')

        assert resp.status_code == 400
        assert 'already exists' in resp.json()['name'][0]
        assert Definition.objects.filter(organization=org).count() == 1

    def test_renaming_onto_another_definitions_name_is_a_400(self, admin_client, org):
        Definition.objects.create(name='Revenue', organization=org)
        other = Definition.objects.create(name='Bookings', organization=org)

        resp = admin_client.patch(
            f'/api/definitions/{other.id}/', data=json.dumps({'name': 'Revenue'}),
            content_type='application/json')

        assert resp.status_code == 400

    def test_a_definition_can_keep_its_own_name_on_update(self, admin_client, org):
        """The clash check must exclude the row being edited, or saving a
        definition without touching its name would reject itself."""
        definition = Definition.objects.create(name='Revenue', organization=org)

        resp = admin_client.patch(
            f'/api/definitions/{definition.id}/',
            data=json.dumps({'name': 'Revenue', 'description': 'Money in'}),
            content_type='application/json')

        assert resp.status_code == 200
        definition.refresh_from_db()
        assert definition.description == 'Money in'


@pytest.mark.django_db
class TestAssignFromTheDictionary:
    """The Data Dictionary assigns by PATCHing the ItemGroup directly, which is a
    different write path from the Definitions page's assign endpoint."""

    def test_patching_a_group_assigns_it(self, admin_client, org, people):
        alice, bob = people
        definition = Definition.objects.create(
            name='Revenue', organization=org, ownership_person=alice)
        group = _curated_group(org, '1::net revenue', ownership_person=bob)

        resp = admin_client.patch(
            f'/api/item-groups/{group.id}/',
            data=json.dumps({'definition': definition.id}),
            content_type='application/json')

        assert resp.status_code == 200
        group.refresh_from_db()
        assert group.definition_id == definition.id
        # Membership only — the definition's owner did NOT come with it.
        assert group.ownership_person_id == bob.id

    def test_patching_null_unassigns(self, admin_client, org):
        definition = Definition.objects.create(name='Revenue', organization=org)
        group = _curated_group(org, '1::net revenue', definition=definition)

        admin_client.patch(
            f'/api/item-groups/{group.id}/',
            data=json.dumps({'definition': None}), content_type='application/json')

        group.refresh_from_db()
        assert group.definition_id is None

    def test_definition_is_exposed_on_the_item_rows(self, admin_client, org):
        """The dictionary lists Items, so the group's definition has to surface
        through the item serializer for the column to render."""
        definition = Definition.objects.create(name='Revenue', organization=org)
        group = _curated_group(org, f'{org.id}::revenue', definition=definition)
        item = _measure('m1', 'Revenue', org)
        item.item_group = group
        item.save(update_fields=['item_group'])

        resp = admin_client.get('/api/items/?item_type=PB_MEASURE&limit=10')
        row = [r for r in resp.json()['results'] if r['item_id'] == 'm1'][0]

        assert row['definition'] == definition.id
        assert row['definition_name'] == 'Revenue'

    def test_items_can_be_filtered_by_definition(self, admin_client, org):
        """Sibling of item_group__category / __ownership_person, so other clients
        can narrow to a definition without pulling the whole catalogue."""
        definition = Definition.objects.create(name='Revenue', organization=org)
        inside = _curated_group(org, f'{org.id}::revenue', definition=definition)
        outside = _curated_group(org, f'{org.id}::costs')
        for item_id, group in (('m1', inside), ('m2', outside)):
            item = _measure(item_id, item_id, org)
            item.item_group = group
            item.save(update_fields=['item_group'])

        resp = admin_client.get(
            f'/api/items/?item_group__definition={definition.id}&limit=50')

        assert resp.status_code == 200
        assert [r['item_id'] for r in resp.json()['results']] == ['m1']


@pytest.mark.django_db
class TestAssignEndpoint:

    def test_assign_adds_and_removes(self, admin_client, org):
        definition = Definition.objects.create(name='Revenue', organization=org)
        a = _curated_group(org, '1::a')
        b = _curated_group(org, '1::b')

        resp = admin_client.post(
            f'/api/definitions/{definition.id}/assign/',
            data=json.dumps({'add': [a.id, b.id]}), content_type='application/json')
        assert resp.json()['added'] == 2
        assert resp.json()['group_count'] == 2

        resp = admin_client.post(
            f'/api/definitions/{definition.id}/assign/',
            data=json.dumps({'remove': [a.id]}), content_type='application/json')
        assert resp.json()['removed'] == 1
        a.refresh_from_db()
        assert a.definition_id is None

    def test_cannot_assign_another_orgs_group(self, admin_client, org):
        other = Organization.objects.create(name='Neighbour')
        definition = Definition.objects.create(name='Revenue', organization=org)
        foreign = _curated_group(other, 'x::foreign')

        resp = admin_client.post(
            f'/api/definitions/{definition.id}/assign/',
            data=json.dumps({'add': [foreign.id]}), content_type='application/json')

        assert resp.json()['added'] == 0
        foreign.refresh_from_db()
        assert foreign.definition_id is None

    def test_a_group_belongs_to_one_definition_reassignment_moves_it(
            self, admin_client, org):
        first = Definition.objects.create(name='Revenue', organization=org)
        second = Definition.objects.create(name='Bookings', organization=org)
        group = _curated_group(org, '1::a', definition=first)

        admin_client.post(
            f'/api/definitions/{second.id}/assign/',
            data=json.dumps({'add': [group.id]}), content_type='application/json')

        group.refresh_from_db()
        assert group.definition_id == second.id
        assert first.item_groups.count() == 0


# --------------------------------------------------------------------------
# 3. Rename-carry — the scenarios that actually bite
# --------------------------------------------------------------------------

@pytest.mark.django_db
class TestRenameCarriesMetadata:

    def _rename(self, item, new_name, org):
        """What the ETL upsert does: refresh the name and the derived group_id."""
        item.item_name = new_name
        item.group_id = f'{org.id}::{new_name.strip().lower()}'
        item.save(update_fields=['item_name', 'group_id'])

    def test_renamed_measure_seeds_its_new_group_with_the_old_metadata(
            self, org, people, dept):
        alice, bob = people
        category = Category.objects.create(name='Finance', organization=org)
        definition = Definition.objects.create(name='Revenue', organization=org)
        group = _curated_group(
            org, f'{org.id}::revenue', ownership_person=alice, steward=bob,
            ownership_department=dept, category=category, status='VERIFIED',
            custom_description='Money in', definition=definition)
        item = _measure('m1', 'Revenue', org)
        item.item_group = group
        item.save(update_fields=['item_group'])

        self._rename(item, 'Revenue v2', org)
        ensure_item_groups(organization_id=org.id)

        item.refresh_from_db()
        new_group = item.item_group
        assert new_group.group_key == f'{org.id}::revenue v2'
        assert new_group.id != group.id
        # Everything travelled.
        assert new_group.ownership_person_id == alice.id
        assert new_group.steward_id == bob.id
        assert new_group.ownership_department_id == dept.id
        assert new_group.category_id == category.id
        assert new_group.status == 'VERIFIED'
        assert new_group.custom_description == 'Money in'
        assert new_group.definition_id == definition.id
        # The item that supplied the metadata represents the new group.
        assert new_group.primary_item_id == item.item_id
        # And the item's own status mirror is intact, not reset to UNVERIFIED.
        assert item.status == 'VERIFIED'

    def test_existing_curated_destination_wins(self, org, people, dept):
        """A renamed instance must not rewrite a group somebody else curated."""
        alice, bob = people
        source = _curated_group(org, f'{org.id}::revenue', ownership_person=alice,
                                status='VERIFIED')
        destination = _curated_group(org, f'{org.id}::revenue v2',
                                     ownership_person=bob, status='ATTENTION')
        item = _measure('m1', 'Revenue', org)
        item.item_group = source
        item.save(update_fields=['item_group'])

        self._rename(item, 'Revenue v2', org)
        ensure_item_groups(organization_id=org.id)

        item.refresh_from_db()
        destination.refresh_from_db()
        assert item.item_group_id == destination.id
        assert destination.ownership_person_id == bob.id    # untouched
        assert destination.status == 'ATTENTION'
        assert item.status == 'ATTENTION'                   # item adopts the group

    def test_rename_back_rejoins_the_original_group(self, org, people):
        alice, _ = people
        group = _curated_group(org, f'{org.id}::revenue', ownership_person=alice,
                               status='VERIFIED')
        item = _measure('m1', 'Revenue', org)
        item.item_group = group
        item.save(update_fields=['item_group'])

        self._rename(item, 'Revenue v2', org)
        ensure_item_groups(organization_id=org.id)
        self._rename(item, 'Revenue', org)
        ensure_item_groups(organization_id=org.id)

        item.refresh_from_db()
        assert item.item_group_id == group.id
        assert item.item_group.ownership_person_id == alice.id

    def test_two_items_renamed_into_the_same_name_are_deterministic(self, org, people):
        """Lowest item_id seeds the shared new group, whatever order rows arrive."""
        alice, bob = people
        first = _curated_group(org, f'{org.id}::alpha', ownership_person=alice)
        second = _curated_group(org, f'{org.id}::beta', ownership_person=bob)
        a = _measure('m_a', 'Alpha', org)
        a.item_group = first
        a.save(update_fields=['item_group'])
        b = _measure('m_b', 'Beta', org)
        b.item_group = second
        b.save(update_fields=['item_group'])

        self._rename(a, 'Merged', org)
        self._rename(b, 'Merged', org)
        ensure_item_groups(organization_id=org.id)

        a.refresh_from_db()
        b.refresh_from_db()
        assert a.item_group_id == b.item_group_id
        # 'm_a' < 'm_b', so alpha's owner wins.
        assert a.item_group.ownership_person_id == alice.id

    def test_group_left_empty_survives_the_rename(self, org, people):
        """Keeping the emptied group is what lets a rename-back recover."""
        alice, _ = people
        group = _curated_group(org, f'{org.id}::revenue', ownership_person=alice)
        item = _measure('m1', 'Revenue', org)
        item.item_group = group
        item.save(update_fields=['item_group'])

        self._rename(item, 'Revenue v2', org)
        ensure_item_groups(organization_id=org.id)

        group.refresh_from_db()
        assert group.pk is not None
        assert group.items.count() == 0
        assert group.primary_item_id is None   # cleared; it left with the item

    def test_only_the_renamed_instance_moves(self, org, people):
        """A measure in several datasets: renaming one instance must leave the
        others — and their group — exactly where they were."""
        alice, _ = people
        group = _curated_group(org, f'{org.id}::revenue', ownership_person=alice,
                               status='VERIFIED')
        moved = _measure('m1', 'Revenue', org)
        stayed = _measure('m2', 'Revenue', org)
        for item in (moved, stayed):
            item.item_group = group
            item.save(update_fields=['item_group'])

        self._rename(moved, 'Revenue v2', org)
        ensure_item_groups(organization_id=org.id)

        moved.refresh_from_db()
        stayed.refresh_from_db()
        assert moved.item_group_id != group.id
        assert stayed.item_group_id == group.id
        assert stayed.status == 'VERIFIED'
        assert moved.item_group.ownership_person_id == alice.id

    def test_a_plain_reimport_moves_nothing(self, org, people):
        """Idempotence: running the linker again with no rename is a no-op."""
        alice, _ = people
        group = _curated_group(org, f'{org.id}::revenue', ownership_person=alice)
        item = _measure('m1', 'Revenue', org)
        item.item_group = group
        item.save(update_fields=['item_group'])

        ensure_item_groups(organization_id=org.id)
        ensure_item_groups(organization_id=org.id)

        item.refresh_from_db()
        assert item.item_group_id == group.id
        assert ItemGroup.objects.filter(organization=org).count() == 1
