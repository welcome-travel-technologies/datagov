"""Tests for the ItemGroup -> Items cascade.

A group's status (and the group-level `deleted` flag) is the single source of
truth; the API mirrors it onto every Item in the group. These guard that a
status edit and a mark-to-delete propagate to all instances of a measure group.
"""
import json
import pytest

from catalog.models import Item, ItemGroup


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
            data=json.dumps({'deleted': False}),
            content_type='application/json',
        )

        grp.refresh_from_db()
        assert grp.deleted is False
        for it in items:
            it.refresh_from_db()
            assert it.deleted is False
            assert it.deleted_at is None

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
