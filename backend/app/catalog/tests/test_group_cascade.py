"""Tests for the ItemGroup -> Items cascade.

A group's status (and the group-level `deleted` flag) is the single source of
truth; the API mirrors it onto every Item in the group. These guard that a
status edit and a mark-to-delete propagate to all instances of a measure group.
"""
import json
from datetime import timedelta

import pytest
from django.utils import timezone

from catalog.models import (
    Category, DataPerson, Definition, Department, GovernanceTask, Item, ItemGroup,
    Organization,
)
from catalog.services.item_groups import (
    ItemGroupTenantCollision, ensure_item_groups,
)


def _measure_group(org, n=2):
    """Create ``n`` PB_MEASURE instances sharing one measure_name ItemGroup."""
    items = []
    for i in range(n):
        items.append(Item.objects.create(
            item_id=f'm_{i}',
            item_name='Revenue',
            item_type='PB_MEASURE',
            group_id='grp::revenue',
            organization=org,
            workspace_name=f'WS{i}',
            dataset_name=f'DS{i}',
            service='powerbi',
        ))
    return items


@pytest.mark.django_db
class TestGroupCascade:

    def test_instances_collapse_into_one_group(self, org):
        items = _measure_group(org, 2)
        assert items[0].item_group_id == items[1].item_group_id

    def test_group_patch_rejects_non_object_body(
            self, client, rw_user, org):
        group = _measure_group(org, 1)[0].item_group
        client.login(username='writer@example.com', password='testpass')

        response = client.patch(
            f'/api/item-groups/{group.pk}/',
            data=json.dumps([]),
            content_type='application/json',
        )

        assert response.status_code == 400
        assert response.json()['error'] == 'Expected an object payload.'

    def test_status_change_cascades_to_all_items(self, client, rw_user, org):
        items = _measure_group(org, 3)
        grp = items[0].item_group

        client.login(username='writer@example.com', password='testpass')
        resp = client.patch(
            f'/api/item-groups/{grp.pk}/',
            data=json.dumps({'status': 'ATTENTION'}),
            content_type='application/json',
        )
        assert resp.status_code == 200
        for it in items:
            it.refresh_from_db()
            assert it.status == 'ATTENTION'

    def test_mark_group_deleted_cascades_delete_and_deprecate(self, client, rw_user, org):
        items = _measure_group(org, 3)
        grp = items[0].item_group

        client.login(username='writer@example.com', password='testpass')
        resp = client.patch(
            f'/api/item-groups/{grp.pk}/',
            data=json.dumps({'deleted': True}),
            content_type='application/json',
        )
        assert resp.status_code == 200

        grp.refresh_from_db()
        assert grp.deleted is True
        assert grp.status == 'DELETED'
        assert grp.deleted_at is not None

        for it in items:
            it.refresh_from_db()
            assert it.deleted is True
            assert it.deleted_at is not None
            assert it.status == 'DELETED'

    def test_restore_group_undeletes_items(self, client, rw_user, org):
        items = _measure_group(org, 2)
        grp = items[0].item_group

        client.login(username='writer@example.com', password='testpass')
        client.patch(
            f'/api/item-groups/{grp.pk}/',
            data=json.dumps({'deleted': True}),
            content_type='application/json',
        )
        client.patch(
            f'/api/item-groups/{grp.pk}/',
            data=json.dumps({'deleted': False, 'status': 'UNVERIFIED'}),
            content_type='application/json',
        )

        grp.refresh_from_db()
        assert grp.deleted is False
        for it in items:
            it.refresh_from_db()
            assert it.deleted is False
            assert it.deleted_at is None

    def test_restore_preserves_source_obsolete_child_and_uses_fresh_marker(
            self, client, rw_user, org):
        items = _measure_group(org, 2)
        group = items[0].item_group
        source_deleted_at = timezone.now() - timedelta(days=4)
        stale_group_marker = timezone.now() - timedelta(days=2)
        Item.objects.filter(pk=items[0].pk).update(
            deleted=True,
            deleted_at=source_deleted_at,
        )
        # A status-only DELETED episode can leave a marker even though the
        # group is not soft-deleted. The next soft-delete episode must not
        # reuse that marker or restore unrelated source-obsolete children.
        ItemGroup.objects.filter(pk=group.pk).update(
            status='DELETED',
            deleted=False,
            deleted_at=stale_group_marker,
        )

        client.login(username='writer@example.com', password='testpass')
        deleted = client.patch(
            f'/api/item-groups/{group.pk}/',
            data=json.dumps({'deleted': True}),
            content_type='application/json',
        )

        assert deleted.status_code == 200
        group.refresh_from_db()
        assert group.deleted_at is not None
        assert group.deleted_at > stale_group_marker
        source_child = Item.objects.get(pk=items[0].pk)
        episode_child = Item.objects.get(pk=items[1].pk)
        assert source_child.deleted is True
        assert source_child.deleted_at == source_deleted_at
        assert episode_child.deleted is True
        assert episode_child.deleted_at == group.deleted_at

        restored = client.patch(
            f'/api/item-groups/{group.pk}/',
            data=json.dumps({
                'deleted': False,
                'status': 'UNVERIFIED',
            }),
            content_type='application/json',
        )

        assert restored.status_code == 200
        group.refresh_from_db()
        source_child.refresh_from_db()
        episode_child.refresh_from_db()
        assert group.deleted is False
        assert group.deleted_at is None
        assert source_child.deleted is True
        assert source_child.deleted_at == source_deleted_at
        assert episode_child.deleted is False
        assert episode_child.deleted_at is None

    def test_soft_deleted_group_cannot_change_status_without_restoring(
            self, client, rw_user, org):
        items = _measure_group(org, 2)
        grp = items[0].item_group
        client.login(username='writer@example.com', password='testpass')
        assert client.patch(
            f'/api/item-groups/{grp.pk}/',
            data=json.dumps({'deleted': True}),
            content_type='application/json',
        ).status_code == 200

        rejected = client.patch(
            f'/api/item-groups/{grp.pk}/',
            data=json.dumps({'status': 'VERIFIED'}),
            content_type='application/json',
        )

        assert rejected.status_code == 400
        grp.refresh_from_db()
        assert grp.deleted is True
        assert grp.status == 'DELETED'
        assert grp.deleted_at is not None
        for item in items:
            item.refresh_from_db()
            assert item.deleted is True
            assert item.status == 'DELETED'

    def test_restore_requires_a_non_deleted_status_in_the_same_patch(
            self, client, rw_user, org):
        items = _measure_group(org, 2)
        grp = items[0].item_group
        client.login(username='writer@example.com', password='testpass')
        client.patch(
            f'/api/item-groups/{grp.pk}/',
            data=json.dumps({'deleted': True}),
            content_type='application/json',
        )

        rejected = client.patch(
            f'/api/item-groups/{grp.pk}/',
            data=json.dumps({'deleted': False}),
            content_type='application/json',
        )

        assert rejected.status_code == 400
        grp.refresh_from_db()
        assert grp.deleted is True
        assert grp.status == 'DELETED'
        assert all(
            Item.objects.get(pk=item.pk).deleted is True
            for item in items
        )

    def test_deleted_true_rejects_a_contradictory_status(
            self, client, rw_user, org):
        item = _measure_group(org, 1)[0]
        grp = item.item_group
        client.login(username='writer@example.com', password='testpass')

        rejected = client.patch(
            f'/api/item-groups/{grp.pk}/',
            data=json.dumps({'deleted': True, 'status': 'ATTENTION'}),
            content_type='application/json',
        )

        assert rejected.status_code == 400
        grp.refresh_from_db()
        item.refresh_from_db()
        assert grp.deleted is False
        assert grp.status == 'UNVERIFIED'
        assert item.deleted is False
        assert item.status == 'UNVERIFIED'

    def test_deleted_status_without_soft_delete_is_a_lifecycle_flag(
            self, client, rw_user, org):
        item = _measure_group(org, 1)[0]
        grp = item.item_group
        client.login(username='writer@example.com', password='testpass')

        response = client.patch(
            f'/api/item-groups/{grp.pk}/',
            data=json.dumps({'status': 'DELETED'}),
            content_type='application/json',
        )

        assert response.status_code == 200
        grp.refresh_from_db()
        item.refresh_from_db()
        assert grp.status == 'DELETED'
        assert grp.deleted is False
        assert grp.deleted_at is not None
        assert item.status == 'DELETED'
        assert item.deleted is False

    def test_undo_resets_status_to_unverified(self, client, rw_user, org):
        """The PowerBI Cleanup "Undo" PATCHes deleted=False + status=UNVERIFIED;
        both must cascade to every item (group -> items)."""
        items = _measure_group(org, 3)
        grp = items[0].item_group

        client.login(username='writer@example.com', password='testpass')
        client.patch(
            f'/api/item-groups/{grp.pk}/',
            data=json.dumps({'deleted': True}),
            content_type='application/json',
        )
        resp = client.patch(
            f'/api/item-groups/{grp.pk}/',
            data=json.dumps({'deleted': False, 'status': 'UNVERIFIED'}),
            content_type='application/json',
        )
        assert resp.status_code == 200

        grp.refresh_from_db()
        assert grp.deleted is False
        assert grp.status == 'UNVERIFIED'
        assert grp.deleted_at is None
        for it in items:
            it.refresh_from_db()
            assert it.deleted is False
            assert it.deleted_at is None
            assert it.status == 'UNVERIFIED'

    def test_include_deleted_surfaces_marked_groups_on_deprecated_tab(self, client, rw_user, org):
        """The Deprecated tab queries status=DELETED&include_deleted=true so
        the (hidden) marked-to-delete groups still appear there for undo. Without
        include_deleted they stay hidden."""
        items = _measure_group(org, 2)
        grp = items[0].item_group

        client.login(username='writer@example.com', password='testpass')
        client.patch(
            f'/api/item-groups/{grp.pk}/',
            data=json.dumps({'deleted': True}),
            content_type='application/json',
        )

        # Deprecated tab fetch: include_deleted surfaces the marked group.
        resp = client.get('/api/items/?service=powerbi&status=DELETED&include_deleted=true&limit=5000')
        assert resp.status_code == 200
        ids = {r['item_id'] for r in resp.json()['results']}
        assert ids == {'m_0', 'm_1'}

        # Default (no include_deleted): soft-deleted items stay hidden.
        resp2 = client.get('/api/items/?service=powerbi&status=DELETED&limit=5000')
        ids2 = {r['item_id'] for r in resp2.json()['results']}
        assert 'm_0' not in ids2 and 'm_1' not in ids2


@pytest.mark.django_db
class TestRenamedMeasureRelink:
    """A measure renamed in Power BI gets a fresh group_id from the ETL upsert
    but stays linked to its OLD group, splitting it from the other instances of
    its new name. ensure_item_groups must re-file it under the group for its
    current name."""

    def _measure(self, org, item_id, name, group_id):
        return Item.objects.create(
            item_id=item_id, item_name=name, item_type='PB_MEASURE',
            group_id=group_id, organization=org,
            workspace_name='WS', dataset_name=item_id, service='powerbi',
        )

    def test_renamed_instance_relinks_to_group_for_new_name(self, org):
        from catalog.services.item_groups import ensure_item_groups

        # Two correctly-grouped instances of the (new) name "Revenue".
        a = self._measure(org, 'm_a', 'Revenue', f'{org.id}::revenue')
        b = self._measure(org, 'm_b', 'Revenue', f'{org.id}::revenue')
        good_group = a.item_group_id
        assert b.item_group_id == good_group

        # A third instance whose name was just changed TO "Revenue": its
        # group_id already points at the new key, but it is still linked to its
        # old group (key "old name") — simulate by updating group_id only.
        c = self._measure(org, 'm_c', 'Old Name', f'{org.id}::old name')
        old_group = c.item_group_id
        assert old_group != good_group
        Item.objects.filter(pk=c.pk).update(group_id=f'{org.id}::revenue')

        moved = ensure_item_groups(org.id)

        c.refresh_from_db()
        assert c.item_group_id == good_group        # re-filed with its siblings
        assert moved >= 0
        # Idempotent: a second pass moves nothing.
        c.refresh_from_db()
        before = c.item_group_id
        ensure_item_groups(org.id)
        c.refresh_from_db()
        assert c.item_group_id == before

    def test_rename_with_no_existing_sibling_creates_group(self, org):
        from catalog.services.item_groups import ensure_item_groups

        c = self._measure(org, 'm_solo', 'Old Name', f'{org.id}::old name')
        old_group = c.item_group_id
        Item.objects.filter(pk=c.pk).update(group_id=f'{org.id}::new name')

        ensure_item_groups(org.id)

        c.refresh_from_db()
        assert c.item_group_id != old_group
        assert c.item_group.group_key == f'{org.id}::new name'

    @pytest.mark.parametrize(
        'status,reason,assignee_field,expected_role',
        [
            ('UNVERIFIED', 'UNVERIFIED', 'ownership_person', 'owner'),
            ('ATTENTION', 'ATTENTION', 'steward', 'steward'),
            ('DELETED', 'DELETED', 'steward', 'steward'),
            ('VERIFIED', 'NO_CATEGORY', 'ownership_person', 'owner'),
        ],
    )
    def test_new_rename_destination_reconciles_tasks_without_alert_spam(
            self, org, status, reason, assignee_field, expected_role,
            monkeypatch):
        owner = DataPerson.objects.create(name='Owner', organization=org)
        steward = DataPerson.objects.create(name='Steward', organization=org)
        category = (
            None
            if reason == 'NO_CATEGORY'
            else Category.objects.create(name=f'Category {reason}', organization=org)
        )
        item = self._measure(
            org, f'rename-{reason}', 'Old Name',
            f'{org.id}::old-{reason.lower()}',
        )
        source = item.item_group
        ItemGroup.objects.filter(pk=source.pk).update(
            status=status,
            ownership_person=owner,
            steward=steward,
            category=category,
        )
        assignee = owner if assignee_field == 'ownership_person' else steward
        old_task = GovernanceTask.objects.create(
            organization=org,
            item_group=source,
            assignee=assignee,
            assignee_role=expected_role,
            reason=reason,
            trigger_status=None if reason == 'NO_CATEGORY' else status,
            title='Old source task',
            state=GovernanceTask.STATE_OPEN,
        )
        alert_calls = []
        monkeypatch.setattr(
            'catalog.governance_tasks._send_task_alert_after_commit',
            lambda task_id: alert_calls.append(task_id),
        )
        Item.objects.filter(pk=item.pk).update(
            group_id=f'{org.id}::new-{reason.lower()}',
        )

        ensure_item_groups(org.id)

        item.refresh_from_db()
        destination = item.item_group
        assert destination.pk != source.pk
        assert destination.status == status
        old_task.refresh_from_db()
        assert old_task.item_group_id is None
        assert old_task.state == GovernanceTask.STATE_DONE
        assert old_task.closed_reason == GovernanceTask.CLOSED_RESOLVED
        fresh = GovernanceTask.objects.get(
            item_group=destination,
            reason=reason,
            state=GovernanceTask.STATE_OPEN,
        )
        assert fresh.assignee_id == assignee.pk
        assert fresh.assignee_role == expected_role
        assert alert_calls == []

        task_count = GovernanceTask.objects.count()
        assert ensure_item_groups(org.id) == 0
        assert GovernanceTask.objects.count() == task_count

    def test_existing_rename_destination_keeps_its_own_curation(self, org):
        source_owner = DataPerson.objects.create(
            name='Source Owner', organization=org,
        )
        destination_owner = DataPerson.objects.create(
            name='Destination Owner', organization=org,
        )
        source_category = Category.objects.create(
            name='Source Category', organization=org,
        )
        destination_category = Category.objects.create(
            name='Destination Category', organization=org,
        )
        destination_item = self._measure(
            org, 'existing-destination', 'New Name', f'{org.id}::new-name',
        )
        destination = destination_item.item_group
        ItemGroup.objects.filter(pk=destination.pk).update(
            status='VERIFIED',
            ownership_person=destination_owner,
            category=destination_category,
            custom_description='Destination wins',
        )
        moved = self._measure(
            org, 'moving-source', 'Old Name', f'{org.id}::old-name',
        )
        source = moved.item_group
        ItemGroup.objects.filter(pk=source.pk).update(
            status='ATTENTION',
            ownership_person=source_owner,
            category=source_category,
            custom_description='Must not overwrite',
        )
        Item.objects.filter(pk=moved.pk).update(
            group_id=destination.group_key,
            deleted=True,
            deleted_at=timezone.now(),
        )

        ensure_item_groups(org.id)

        moved.refresh_from_db()
        destination.refresh_from_db()
        assert moved.item_group_id == destination.pk
        assert destination.status == 'VERIFIED'
        assert destination.ownership_person_id == destination_owner.pk
        assert destination.category_id == destination_category.pk
        assert destination.custom_description == 'Destination wins'
        assert moved.status == 'VERIFIED'
        assert moved.deleted is True
        assert moved.deleted_at is not None
        assert not GovernanceTask.objects.filter(
            item_group=destination,
            reason=GovernanceTask.REASON_ATTENTION,
            state=GovernanceTask.STATE_OPEN,
        ).exists()

    def test_manual_done_survives_pure_rename_then_clear_and_relapse(
            self, org):
        steward = DataPerson.objects.create(name='Steward', organization=org)
        category = Category.objects.create(name='Finance', organization=org)
        item = self._measure(
            org, 'dismissed-rename', 'Old Name', f'{org.id}::dismissed-old',
        )
        source = item.item_group
        ItemGroup.objects.filter(pk=source.pk).update(
            status='ATTENTION', steward=steward, category=category,
        )
        dismissed = GovernanceTask.objects.create(
            organization=org,
            item_group=source,
            assignee=steward,
            assignee_role='steward',
            reason=GovernanceTask.REASON_ATTENTION,
            trigger_status='ATTENTION',
            title='Dismissed current episode',
            state=GovernanceTask.STATE_DONE,
            closed_reason=GovernanceTask.CLOSED_MANUAL,
            completed_at=timezone.now(),
        )
        Item.objects.filter(pk=item.pk).update(
            group_id=f'{org.id}::dismissed-new',
        )

        ensure_item_groups(org.id)

        item.refresh_from_db()
        destination = item.item_group
        dismissed.refresh_from_db()
        assert dismissed.item_group_id == destination.pk
        assert dismissed.condition_cleared_at is None
        assert not GovernanceTask.objects.filter(
            item_group=destination,
            reason=GovernanceTask.REASON_ATTENTION,
            state=GovernanceTask.STATE_OPEN,
        ).exists()

        from catalog.governance_tasks import sync_status_task

        ItemGroup.objects.filter(pk=destination.pk).update(status='VERIFIED')
        destination.refresh_from_db()
        sync_status_task(destination, 'VERIFIED', notify=False)
        dismissed.refresh_from_db()
        assert dismissed.condition_cleared_at is not None

        ItemGroup.objects.filter(pk=destination.pk).update(status='ATTENTION')
        destination.refresh_from_db()
        fresh = sync_status_task(destination, 'ATTENTION', notify=False)
        assert fresh is not None
        assert fresh.pk != dismissed.pk
        assert fresh.state == GovernanceTask.STATE_OPEN

    def test_manual_done_is_cloned_when_rename_source_keeps_members(
            self, org):
        steward = DataPerson.objects.create(name='Steward', organization=org)
        category = Category.objects.create(name='Finance', organization=org)
        first = self._measure(
            org, 'split-first', 'Old Name', f'{org.id}::split-old',
        )
        second = self._measure(
            org, 'split-second', 'Old Name', f'{org.id}::split-old',
        )
        source = first.item_group
        ItemGroup.objects.filter(pk=source.pk).update(
            status='ATTENTION', steward=steward, category=category,
        )
        original = GovernanceTask.objects.create(
            organization=org,
            item_group=source,
            assignee=steward,
            assignee_role='steward',
            reason=GovernanceTask.REASON_ATTENTION,
            trigger_status='ATTENTION',
            title='Dismissed split source',
            state=GovernanceTask.STATE_DONE,
            closed_reason=GovernanceTask.CLOSED_MANUAL,
            completed_at=timezone.now(),
        )
        Item.objects.filter(pk=first.pk).update(
            group_id=f'{org.id}::split-new',
        )

        ensure_item_groups(org.id)

        first.refresh_from_db()
        second.refresh_from_db()
        original.refresh_from_db()
        assert second.item_group_id == source.pk
        assert first.item_group_id != source.pk
        assert original.item_group_id == source.pk
        clone = GovernanceTask.objects.get(
            item_group=first.item_group,
            reason=GovernanceTask.REASON_ATTENTION,
            state=GovernanceTask.STATE_DONE,
            closed_reason=GovernanceTask.CLOSED_MANUAL,
            condition_cleared_at__isnull=True,
        )
        assert clone.pk != original.pk
        assert not GovernanceTask.objects.filter(
            item_group__in=[source, first.item_group],
            reason=GovernanceTask.REASON_ATTENTION,
            state=GovernanceTask.STATE_OPEN,
        ).exists()

    def test_first_member_returning_to_preserved_group_reconciles_tasks(
            self, org):
        steward = DataPerson.objects.create(name='Steward', organization=org)
        category = Category.objects.create(name='Finance', organization=org)
        preserved = ItemGroup.objects.create(
            group_key=f'{org.id}::returning',
            kind=ItemGroup.KIND_MEASURE_NAME,
            organization=org,
            status='ATTENTION',
            steward=steward,
            category=category,
        )
        Item.objects.bulk_create([
            Item(
                item_id='returning-member',
                item_name='Returning',
                item_type='PB_MEASURE',
                group_id=preserved.group_key,
                organization=org,
                item_group=None,
                service='powerbi',
            ),
        ])

        ensure_item_groups(org.id)

        item = Item.objects.get(pk='returning-member')
        assert item.item_group_id == preserved.pk
        task = GovernanceTask.objects.get(
            item_group=preserved,
            reason=GovernanceTask.REASON_ATTENTION,
            state=GovernanceTask.STATE_OPEN,
        )
        assert task.assignee_id == steward.pk
        assert task.assignee_role == 'steward'

    def test_foreign_destination_collision_rolls_back_curated_rename(
            self, org):
        other = Organization.objects.create(name='Neighbour')
        owner = DataPerson.objects.create(name='Owner', organization=org)
        category = Category.objects.create(name='Finance', organization=org)
        item = self._measure(
            org, 'collision-rename', 'Old Name', f'{org.id}::collision-old',
        )
        source = item.item_group
        ItemGroup.objects.filter(pk=source.pk).update(
            status='ATTENTION',
            ownership_person=owner,
            category=category,
            custom_description='Preserve me',
            primary_item=item,
        )
        task = GovernanceTask.objects.create(
            organization=org,
            item_group=source,
            assignee=owner,
            assignee_role='owner',
            reason=GovernanceTask.REASON_NO_CATEGORY,
            title='Preserve task',
            state=GovernanceTask.STATE_OPEN,
        )
        destination_key = f'{org.id}::collision-new'
        ItemGroup.objects.create(
            group_key=destination_key,
            kind=ItemGroup.KIND_MEASURE_NAME,
            organization=other,
        )
        Item.objects.filter(pk=item.pk).update(group_id=destination_key)

        with pytest.raises(ItemGroupTenantCollision):
            ensure_item_groups(org.id)

        item.refresh_from_db()
        source.refresh_from_db()
        task.refresh_from_db()
        assert item.item_group_id == source.pk
        assert source.ownership_person_id == owner.pk
        assert source.category_id == category.pk
        assert source.status == 'ATTENTION'
        assert source.custom_description == 'Preserve me'
        assert source.primary_item_id == item.pk
        assert task.item_group_id == source.pk
        assert task.state == GovernanceTask.STATE_OPEN

    def test_active_destination_does_not_resurrect_obsolete_source_item(
            self, org):
        destination_item = self._measure(
            org, 'active-sibling', 'New Name', f'{org.id}::new name',
        )
        destination = destination_item.item_group
        destination.status = 'VERIFIED'
        destination.save(update_fields=['status'])

        moved = self._measure(
            org, 'obsolete-source', 'Old Name', f'{org.id}::old name',
        )
        deleted_at = timezone.now()
        Item.objects.filter(pk=moved.pk).update(
            group_id=destination.group_key,
            status='DELETED',
            deleted=True,
            deleted_at=deleted_at,
        )

        ensure_item_groups(org.id)

        moved.refresh_from_db()
        assert moved.item_group_id == destination.pk
        # Status is group-owned, but deletion is asymmetric. Only a deleted
        # destination may force the item deleted; an active group cannot prove
        # this source-obsolete row has reappeared.
        assert moved.status == 'VERIFIED'
        assert moved.deleted is True
        assert moved.deleted_at == deleted_at

    def test_noncarried_quarantined_pending_item_is_not_resurrected(
            self, org):
        destination_item = self._measure(
            org, 'active-destination', 'New Name', f'{org.id}::new name',
        )
        destination = destination_item.item_group
        ItemGroup.objects.filter(pk=destination.pk).update(status='VERIFIED')
        deleted_at = timezone.now()
        Item.objects.bulk_create([
            Item(
                item_id='quarantined-obsolete',
                item_name='New Name',
                item_type='PB_MEASURE',
                group_id=destination.group_key,
                organization=org,
                item_group=None,
                status='DELETED',
                deleted=True,
                deleted_at=deleted_at,
            ),
        ])

        ensure_item_groups(org.id)

        item = Item.objects.get(pk='quarantined-obsolete')
        assert item.item_group_id == destination.pk
        assert item.status == 'VERIFIED'
        assert item.deleted is True
        assert item.deleted_at == deleted_at

    def test_linker_reasserts_soft_deleted_group_after_etl_child_reset(
            self, org):
        item = self._measure(
            org, 'soft-deleted-refresh', 'Old Name', f'{org.id}::old name',
        )
        group = item.item_group
        deleted_at = timezone.now()
        source_deleted_at = deleted_at - timedelta(days=5)
        Item.objects.bulk_create([
            Item(
                item_id='source-obsolete-in-soft-deleted-group',
                item_name='Old Name',
                item_type='PB_MEASURE',
                group_id=group.group_key,
                organization=org,
                item_group=group,
                status='DELETED',
                deleted=True,
                deleted_at=source_deleted_at,
            ),
        ])
        ItemGroup.objects.filter(pk=group.pk).update(
            status='DELETED',
            deleted=True,
            deleted_at=deleted_at,
        )
        # Both loaders set a currently returned source row deleted=False on
        # conflict. The group-level curation must win after their link pass.
        Item.objects.filter(pk=item.pk).update(
            deleted=False,
            deleted_at=None,
        )

        ensure_item_groups(org.id)

        item.refresh_from_db()
        assert item.item_group_id == group.pk
        assert item.deleted is True
        assert item.deleted_at == deleted_at
        source_obsolete = Item.objects.get(
            pk='source-obsolete-in-soft-deleted-group',
        )
        assert source_obsolete.deleted is True
        assert source_obsolete.deleted_at == source_deleted_at

    def test_migration_detached_item_is_not_relinked_to_foreign_global_key(
            self, org):
        other = Organization.objects.create(name='Neighbour')
        key = f'{org.id}::revenue'
        foreign = ItemGroup.objects.create(
            group_key=key,
            kind=ItemGroup.KIND_MEASURE_NAME,
            organization=other,
            status='ATTENTION',
        )
        # This is the exact state left by migration 0062: the unsafe FK was
        # removed, while the ingestion-owned grouping key was retained.
        Item.objects.bulk_create([
            Item(
                item_id='detached-after-0062',
                item_name='Revenue',
                item_type='PB_MEASURE',
                group_id=key,
                organization=org,
            ),
        ])

        with pytest.raises(
                ItemGroupTenantCollision,
                match=r'belongs to organization'):
            ensure_item_groups(organization_id=org.id)

        item = Item.objects.get(pk='detached-after-0062')
        foreign.refresh_from_db()
        assert item.item_group_id is None
        assert foreign.organization_id == other.pk
        assert foreign.status == 'ATTENTION'
        assert not ItemGroup.objects.filter(
            group_key=key,
            organization=org,
        ).exists()

    def test_foreign_source_is_quarantined_without_carrying_its_metadata(
            self, org):
        other = Organization.objects.create(name='Neighbour')
        foreign_owner = DataPerson.objects.create(
            name='Foreign owner', organization=other,
        )
        foreign = ItemGroup.objects.create(
            group_key='foreign::old',
            kind=ItemGroup.KIND_MEASURE_NAME,
            organization=other,
            ownership_person=foreign_owner,
            status='VERIFIED',
            custom_description='Foreign curation',
        )
        destination_key = f'{org.id}::new name'
        Item.objects.bulk_create([
            Item(
                item_id='cross-tenant-source',
                item_name='New Name',
                item_type='PB_MEASURE',
                group_id=destination_key,
                organization=org,
                item_group=foreign,
            ),
        ])

        ensure_item_groups(organization_id=org.id)

        item = Item.objects.select_related('item_group').get(
            pk='cross-tenant-source',
        )
        assert item.item_group.organization_id == org.id
        assert item.item_group.group_key == destination_key
        assert item.item_group.ownership_person_id is None
        assert item.item_group.status == 'UNVERIFIED'
        assert item.item_group.custom_description is None

    def test_exact_org_source_drops_foreign_related_fks_from_rename_carry(
            self, org):
        other = Organization.objects.create(name='Neighbour')
        foreign_definition = Definition.objects.create(
            name='Foreign definition', organization=other,
        )
        foreign_department = Department.objects.create(
            name='Foreign department', organization=other,
        )
        foreign_owner = DataPerson.objects.create(
            name='Foreign owner', organization=other,
        )
        foreign_steward = DataPerson.objects.create(
            name='Foreign steward', organization=other,
        )
        foreign_category = Category.objects.create(
            name='Foreign category', organization=other,
        )
        source = ItemGroup.objects.create(
            group_key=f'{org.id}::old name',
            kind=ItemGroup.KIND_MEASURE_NAME,
            organization=org,
            definition=foreign_definition,
            ownership_department=foreign_department,
            ownership_person=foreign_owner,
            steward=foreign_steward,
            category=foreign_category,
            status='VERIFIED',
            custom_description='Tenant-neutral curation',
        )
        destination_key = f'{org.id}::new name'
        Item.objects.bulk_create([
            Item(
                item_id='rename-with-corrupt-related-fks',
                item_name='New Name',
                item_type='PB_MEASURE',
                group_id=destination_key,
                organization=org,
                item_group=source,
            ),
        ])

        ensure_item_groups(organization_id=org.id)

        item = Item.objects.select_related('item_group').get(
            pk='rename-with-corrupt-related-fks',
        )
        destination = item.item_group
        assert destination.organization_id == org.id
        assert destination.group_key == destination_key
        assert destination.definition_id is None
        assert destination.ownership_department_id is None
        assert destination.ownership_person_id is None
        assert destination.steward_id is None
        assert destination.category_id is None
        assert destination.status == 'VERIFIED'
        assert destination.custom_description == 'Tenant-neutral curation'


@pytest.mark.django_db
class TestDbtCleanupPayload:
    """The dbt Cleanup page reuses the same group cascade + Deprecated/Undo as
    PowerBI Cleanup, fed by /api/dbt-insights enriched rows."""

    def _dbt_model(self, org, item_id='dbt_m1'):
        return Item.objects.create(
            item_id=item_id, item_name='stg_orders', item_type='DBT_MODEL',
            service='dbt', organization=org, is_unused=True,
            database_name='analytics', schema_name='staging',
        )

    def test_cleanup_rows_carry_status_group_deleted(self, client, rw_user, org):
        self._dbt_model(org)
        client.login(username='writer@example.com', password='testpass')
        data = client.get('/api/dbt-insights/?section=cleanup').json()
        rows = data['unused_models']
        assert len(rows) == 1
        row = rows[0]
        for key in ('status', 'item_group', 'deleted', 'item_type'):
            assert key in row
        assert row['status'] == 'UNVERIFIED'
        assert row['deleted'] is False
        assert 'attention' in data['totals'] and 'deprecated' in data['totals']

    def test_mark_delete_moves_to_deprecated_with_undo(self, client, rw_user, org):
        item = self._dbt_model(org)
        grp = item.item_group
        client.login(username='writer@example.com', password='testpass')

        client.patch(
            f'/api/item-groups/{grp.pk}/',
            data=json.dumps({'deleted': True}),
            content_type='application/json',
        )

        # Gone from the unused list; counted as deprecated.
        data = client.get('/api/dbt-insights/?section=cleanup').json()
        assert data['unused_models'] == []
        assert data['totals']['deprecated'] == 1

        # Visible on the Deprecated tab (include_deleted) for undo.
        dep = client.get('/api/items/?service=dbt&status=DELETED&include_deleted=true').json()
        assert {r['item_id'] for r in dep['results']} == {'dbt_m1'}

        # Undo restores + resets to UNVERIFIED.
        client.patch(
            f'/api/item-groups/{grp.pk}/',
            data=json.dumps({'deleted': False, 'status': 'UNVERIFIED'}),
            content_type='application/json',
        )
        item.refresh_from_db()
        assert item.deleted is False and item.status == 'UNVERIFIED'
