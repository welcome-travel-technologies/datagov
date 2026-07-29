"""
Tests for the governance Task Manager.

Two creation paths, both guarded here:

  * **event** — ``sync_status_task`` fires from the status-change sites in
    ``views.py`` when a group flips to Attention / To Be Deleted;
  * **sweep** — ``generate_tasks`` reconciles the whole catalog: it creates the
    missing tasks, re-resolves assignees as ownership gets filled in, and closes
    tasks whose gap has been fixed.

Plus the API surface the Task Manager page actually calls (scope, bulk-done,
the open-by-default feed) and the Slack alert.
"""
import json
import pytest
from io import StringIO
from unittest.mock import patch, MagicMock

from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from catalog.governance_tasks import generate_tasks, sync_group_metadata_tasks
from catalog.models import (
    Category, CustomUser, DataPerson, GovernanceTask, Item, ItemGroup,
    Organization, OrganizationMembership,
)


def _patch_group_status(client, group_pk, status):
    return client.patch(
        f'/api/item-groups/{group_pk}/',
        data=json.dumps({'status': status}),
        content_type='application/json',
    )


def _measure_group(org, name='Revenue', item_id='m_rev'):
    """A PB_MEASURE instance plus the measure_name ItemGroup it collapses into.

    A fresh group is UNVERIFIED with no category, so it qualifies for *both*
    sweep-only reasons — which is the state most of these tests start from.
    """
    return Item.objects.create(
        item_id=item_id, item_name=name, item_type='PB_MEASURE',
        group_id=f'{org.id}::{name.lower()}', organization=org,
        workspace_name='WS1', dataset_name='DS1', service='powerbi',
    ).item_group


def _singleton_group(org, item_id='dbt_1'):
    """A non-measure item gets its own 1-item group (kind='singleton')."""
    return Item.objects.create(
        item_id=item_id, item_name='stg_orders', item_type='DBT_MODEL',
        service='dbt', organization=org,
    ).item_group


def _person(org, name, **flags):
    return DataPerson.objects.create(name=name, organization=org, **flags)


def _bare_task(org, **kwargs):
    """A task with no ItemGroup — enough for the feed tests, which are about
    scoping and state, not about the asset behind the row."""
    kwargs.setdefault('reason', 'ATTENTION')
    kwargs.setdefault('title', 'Review something')
    return GovernanceTask.objects.create(organization=org, **kwargs)


def _open_keys():
    """``(item_group_id, reason)`` for every open task — i.e. the dedupe key."""
    return list(
        GovernanceTask.objects.filter(state=GovernanceTask.STATE_OPEN)
        .values_list('item_group_id', 'reason')
    )


def test_deleted_status_label_changed_but_value_did_not():
    """The dropdown reads "To Be Deleted"; the STORED value stays 'DELETED'.
    Every reason / trigger_status / ETL comparison keys off the value, so a
    well-meaning rename of the constant would silently break routing."""
    assert dict(Item.STATUS_CHOICES)['DELETED'] == 'To Be Deleted'
    assert GovernanceTask.REASON_DELETED == 'DELETED'


@pytest.mark.django_db
class TestTaskCreation:
    """A task is created when a group's status flips to ATTENTION/DELETED."""

    @pytest.mark.parametrize('status', ['ATTENTION', 'DELETED'])
    def test_status_change_creates_task(self, client, rw_user, item_with_org, status):
        client.login(username='writer@example.com', password='testpass')
        gpk = item_with_org.item_group_id
        resp = _patch_group_status(client, gpk, status)
        assert resp.status_code == 200

        tasks = GovernanceTask.objects.filter(item_group_id=gpk, state='open')
        expected_reasons = (
            {'ATTENTION', 'NO_CATEGORY'}
            if status == 'ATTENTION'
            else {'DELETED'}
        )
        assert set(tasks.values_list('reason', flat=True)) == expected_reasons
        task = tasks.get(reason=status)
        assert task.trigger_status == status
        # The two status-derived reasons deliberately share the status value.
        assert task.reason == status

    def test_verified_still_creates_missing_category_task(
            self, client, rw_user, item_with_org):
        client.login(username='writer@example.com', password='testpass')
        gpk = item_with_org.item_group_id
        resp = _patch_group_status(client, gpk, 'VERIFIED')
        assert resp.status_code == 200
        assert set(
            GovernanceTask.objects.filter(
                item_group_id=gpk,
                state=GovernanceTask.STATE_OPEN,
            ).values_list('reason', flat=True)
        ) == {GovernanceTask.REASON_NO_CATEGORY}

    def test_task_assigned_to_steward(self, client, rw_user, item_with_org):
        steward = DataPerson.objects.create(
            name='Sam Steward', is_steward=True, slack_handle='@sam',
            organization=item_with_org.organization,
        )
        grp = ItemGroup.objects.get(pk=item_with_org.item_group_id)
        grp.steward = steward
        grp.save(update_fields=['steward'])

        client.login(username='writer@example.com', password='testpass')
        resp = _patch_group_status(client, grp.pk, 'ATTENTION')
        assert resp.status_code == 200

        task = GovernanceTask.objects.get(
            item_group=grp,
            reason=GovernanceTask.REASON_ATTENTION,
            state=GovernanceTask.STATE_OPEN,
        )
        assert task.assignee_id == steward.id
        assert task.assignee_role == 'steward'

    def test_routing_order_comes_from_reason_policy(self, item_with_org):
        """Who a task lands on is one ordered tuple per reason in REASON_POLICY.
        Flipping that tuple re-routes with no other change — guards the
        extensibility the design promises, and that the event path reads the
        same policy the sweep does."""
        owner = DataPerson.objects.create(
            name='Olivia Owner', is_owner=True,
            organization=item_with_org.organization,
        )
        steward = DataPerson.objects.create(
            name='Sam Steward', is_steward=True,
            organization=item_with_org.organization,
        )
        grp = ItemGroup.objects.get(pk=item_with_org.item_group_id)
        grp.ownership_person = owner
        grp.steward = steward
        grp.save(update_fields=['ownership_person', 'steward'])

        from catalog import governance_tasks
        policy = governance_tasks.REASON_POLICY[governance_tasks.REASON_ATTENTION]
        assert policy['roles'][0] == 'steward'      # the shipped policy
        original = policy['roles']
        try:
            policy['roles'] = ('owner', 'steward')
            task = governance_tasks.sync_status_task(grp, 'ATTENTION', None)
        finally:
            policy['roles'] = original

        assert task.assignee_id == owner.id
        assert task.assignee_role == 'owner'

    def test_event_path_is_not_kind_scoped(self, org):
        """Attention applies to every kind, including singleton assets."""
        from catalog.governance_tasks import sync_status_task
        grp = _singleton_group(org)
        grp.status = 'ATTENTION'
        grp.save(update_fields=['status'])

        task = sync_status_task(grp, 'ATTENTION', None)

        assert task is not None
        assert task.item_group_id == grp.id
        assert task.reason == 'ATTENTION'

    def test_unassigned_when_no_steward(self, client, rw_user, item_with_org):
        client.login(username='writer@example.com', password='testpass')
        gpk = item_with_org.item_group_id
        _patch_group_status(client, gpk, 'ATTENTION')
        task = GovernanceTask.objects.get(
            item_group_id=gpk,
            reason=GovernanceTask.REASON_ATTENTION,
            state=GovernanceTask.STATE_OPEN,
        )
        assert task.assignee_id is None

    def test_corrupt_cross_tenant_steward_is_never_routed(
            self, client, rw_user, item_with_org):
        other = Organization.objects.create(name='Neighbour')
        foreign = DataPerson.objects.create(
            name='Foreign steward', organization=other, is_steward=True,
        )
        group = item_with_org.item_group
        ItemGroup.objects.filter(pk=group.pk).update(steward=foreign)
        client.login(username='writer@example.com', password='testpass')

        response = _patch_group_status(client, group.pk, 'ATTENTION')

        assert response.status_code == 200
        task = GovernanceTask.objects.get(
            item_group_id=group.pk, reason='ATTENTION', state='open',
        )
        assert task.assignee_id is None
        assert task.assignee_role is None

    def test_condition_clear_then_relapse_opens_a_fresh_task(
            self, client, rw_user, item_with_org):
        """A resolved episode stays as audit history; relapse gets a new row."""
        client.login(username='writer@example.com', password='testpass')
        gpk = item_with_org.item_group_id
        _patch_group_status(client, gpk, 'ATTENTION')
        first_id = GovernanceTask.objects.get(
            item_group_id=gpk,
            reason=GovernanceTask.REASON_ATTENTION,
            state=GovernanceTask.STATE_OPEN,
        ).id

        _patch_group_status(client, gpk, 'VERIFIED')
        _patch_group_status(client, gpk, 'ATTENTION')

        open_tasks = GovernanceTask.objects.filter(
            item_group_id=gpk, reason='ATTENTION', state='open')
        assert open_tasks.count() == 1
        assert open_tasks.first().id != first_id
        first = GovernanceTask.objects.get(pk=first_id)
        assert first.state == GovernanceTask.STATE_DONE
        assert first.closed_reason == GovernanceTask.CLOSED_RESOLVED

    def test_different_status_opens_its_own_reason(self, client, rw_user, item_with_org):
        """A status transition resolves the old episode and opens the new one."""
        client.login(username='writer@example.com', password='testpass')
        gpk = item_with_org.item_group_id
        _patch_group_status(client, gpk, 'ATTENTION')
        _patch_group_status(client, gpk, 'DELETED')

        reasons = set(GovernanceTask.objects.filter(item_group_id=gpk, state='open')
                      .values_list('reason', flat=True))
        assert reasons == {'DELETED'}

        generate_tasks(item_with_org.organization, reasons=['ATTENTION'])

        stale = GovernanceTask.objects.get(item_group_id=gpk, reason='ATTENTION')
        assert stale.state == 'done'
        assert stale.closed_reason == 'resolved'
        assert GovernanceTask.objects.filter(
            item_group_id=gpk, reason='DELETED', state='open').exists()

    def test_mark_deleted_creates_task_and_stamps_time(self, client, rw_user, item_with_org):
        """Marking an item deleted auto-DEPRECATEs its group → a task is created
        and the item's deleted_at is stamped (for the Deleted Items history)."""
        client.login(username='writer@example.com', password='testpass')
        gpk = item_with_org.item_group_id
        resp = client.patch(
            f'/api/item-groups/{gpk}/',
            data=json.dumps({'deleted': True}),
            content_type='application/json',
        )
        assert resp.status_code == 200

        task = GovernanceTask.objects.filter(item_group_id=gpk, state='open').first()
        assert task is not None
        assert task.trigger_status == 'DELETED'
        assert task.reason == 'DELETED'

        item_with_org.refresh_from_db()
        assert item_with_org.deleted is True
        assert item_with_org.deleted_at is not None

        # Auto-DEPRECATE also stamps the group-level deleted_at (coupled to status).
        grp = ItemGroup.objects.get(pk=gpk)
        assert grp.status == 'DELETED'
        assert grp.deleted_at is not None


@pytest.mark.django_db
class TestSweepCreation:
    """``generate_tasks`` — the reconciler behind the "Generate tasks" button."""

    def test_one_task_per_group_and_reason(self, org):
        grp = _measure_group(org)
        result = generate_tasks(org)

        # A fresh measure group is both Unverified and uncategorised.
        assert result['reasons']['UNVERIFIED']['created'] == 1
        assert result['reasons']['NO_CATEGORY']['created'] == 1
        assert result['totals']['created'] == 2
        keys = _open_keys()
        assert len(keys) == len(set(keys))
        assert set(keys) == {(grp.id, 'UNVERIFIED'), (grp.id, 'NO_CATEGORY')}
        # The title names the asset, not the group key.
        assert all('Revenue' in t.title for t in GovernanceTask.objects.all())

    def test_command_requires_explicit_confirmation_for_broad_apply(self, org):
        group = _singleton_group(org)
        ItemGroup.objects.filter(pk=group.pk).update(status='ATTENTION')

        args = (
            'generate_governance_tasks',
            '--org', str(org.pk),
            '--reasons', 'ATTENTION',
            '--kind-scope', 'all',
        )
        with pytest.raises(CommandError, match='--confirm-broad'):
            call_command(*args, stdout=StringIO())
        assert not GovernanceTask.objects.filter(item_group=group).exists()

        preview_out = StringIO()
        call_command(*args, '--dry-run', stdout=preview_out)
        assert 'DRY RUN' in preview_out.getvalue()
        assert not GovernanceTask.objects.filter(item_group=group).exists()

        call_command(*args, '--confirm-broad', stdout=StringIO())
        assert GovernanceTask.objects.filter(
            item_group=group,
            reason=GovernanceTask.REASON_ATTENTION,
            state=GovernanceTask.STATE_OPEN,
        ).exists()

    def test_second_run_creates_nothing(self, org):
        """Idempotence is the whole reason the sweep is a reconciler: an admin
        can press Generate as often as they like."""
        _measure_group(org)
        generate_tasks(org)
        second = generate_tasks(org)

        assert second['totals']['created'] == 0
        assert second['totals']['closed'] == 0
        assert second['totals']['reassigned'] == 0
        assert GovernanceTask.objects.filter(state='open').count() == 2

    def test_unverified_routes_to_owner(self, org):
        grp = _measure_group(org)
        owner = _person(org, 'Olivia Owner', is_owner=True)
        grp.ownership_person = owner
        grp.steward = _person(org, 'Sam Steward', is_steward=True)
        grp.save(update_fields=['ownership_person', 'steward'])

        generate_tasks(org, reasons=['UNVERIFIED'])

        task = GovernanceTask.objects.get(item_group=grp, reason='UNVERIFIED')
        assert task.assignee_id == owner.id
        assert task.assignee_role == 'owner'

    def test_sweep_never_routes_to_a_cross_tenant_owner(self, org):
        other = Organization.objects.create(name='Neighbour')
        foreign = DataPerson.objects.create(
            name='Foreign owner', organization=other, is_owner=True,
        )
        group = _measure_group(org)
        ItemGroup.objects.filter(pk=group.pk).update(
            ownership_person=foreign,
        )

        result = generate_tasks(org, reasons=['UNVERIFIED'])

        task = GovernanceTask.objects.get(
            item_group=group, reason='UNVERIFIED',
        )
        assert result['totals']['unassigned'] == 1
        assert task.assignee_id is None
        assert task.assignee_role is None

    def test_attention_routes_to_steward(self, org):
        grp = _measure_group(org)
        steward = _person(org, 'Sam Steward', is_steward=True)
        grp.ownership_person = _person(org, 'Olivia Owner', is_owner=True)
        grp.steward = steward
        grp.status = 'ATTENTION'
        grp.save(update_fields=['ownership_person', 'steward', 'status'])

        generate_tasks(org, reasons=['ATTENTION'])

        task = GovernanceTask.objects.get(item_group=grp, reason='ATTENTION')
        assert task.assignee_id == steward.id
        assert task.assignee_role == 'steward'

    def test_source_obsolete_member_skips_hygiene_but_attention_still_routes(
            self, org):
        group = _measure_group(org)
        owner = _person(org, 'Olivia Owner', is_owner=True)
        steward = _person(org, 'Sam Steward', is_steward=True)
        ItemGroup.objects.filter(pk=group.pk).update(
            ownership_person=owner,
            steward=steward,
        )
        Item.objects.filter(item_group=group).update(deleted=True)

        hygiene = generate_tasks(
            org, reasons=['UNVERIFIED', 'NO_CATEGORY'],
        )
        assert hygiene['totals']['target'] == 0
        assert hygiene['totals']['created'] == 0

        ItemGroup.objects.filter(pk=group.pk).update(status='ATTENTION')
        attention = generate_tasks(org, reasons=['ATTENTION'])

        assert attention['totals']['target'] == 1
        task = GovernanceTask.objects.get(
            item_group=group,
            reason=GovernanceTask.REASON_ATTENTION,
            state=GovernanceTask.STATE_OPEN,
        )
        assert task.assignee_id == steward.pk
        assert task.assignee_role == 'steward'

    def test_unverified_is_unassigned_when_no_owner(self, org):
        """Routing is strict: a measure with no Owner remains unassigned even
        when it has a Steward."""
        grp = _measure_group(org)
        steward = _person(org, 'Sam Steward', is_steward=True)
        grp.steward = steward
        grp.save(update_fields=['steward'])

        result = generate_tasks(org, reasons=['UNVERIFIED'])

        assert result['reasons']['UNVERIFIED']['unassigned'] == 1
        task = GovernanceTask.objects.get(item_group=grp, reason='UNVERIFIED')
        assert task.assignee_id is None
        assert task.assignee_role is None

    def test_rerun_picks_up_ownership_added_later(self, org):
        """"Now that we have full ownership, re-run it" — assignees are
        re-resolved on every sweep, so tasks that opened unassigned self-heal
        instead of needing a special backfill."""
        grp = _measure_group(org)
        first = generate_tasks(org, reasons=['UNVERIFIED'])
        assert first['reasons']['UNVERIFIED']['unassigned'] == 1

        task = GovernanceTask.objects.get(item_group=grp, reason='UNVERIFIED')
        assert task.assignee_id is None

        owner = _person(org, 'Olivia Owner', is_owner=True)
        grp.ownership_person = owner
        grp.save(update_fields=['ownership_person'])

        second = generate_tasks(org, reasons=['UNVERIFIED'])

        assert second['reasons']['UNVERIFIED']['reassigned'] >= 1
        assert second['reasons']['UNVERIFIED']['created'] == 0

        task.refresh_from_db()      # the SAME row, now routed
        assert task.assignee_id == owner.id
        assert task.assignee_role == 'owner'
        assert GovernanceTask.objects.filter(item_group=grp, reason='UNVERIFIED').count() == 1

    def test_default_sweep_includes_singleton_attention(self, org):
        """The default is all-assets so literal Attention coverage is complete."""
        grp = _singleton_group(org)
        assert grp.kind == ItemGroup.KIND_SINGLETON
        ItemGroup.objects.filter(pk=grp.pk).update(status='ATTENTION')

        default_run = generate_tasks(org, reasons=['ATTENTION'])

        assert default_run['kind_scope'] == 'all'
        assert default_run['reasons']['ATTENTION']['created'] == 1
        assert GovernanceTask.objects.filter(
            item_group=grp,
            reason='ATTENTION',
            state='open',
        ).exists()

    @pytest.mark.parametrize('kind_scope', ['singleton', 'all'])
    def test_reason_policy_still_limits_singleton_unverified(
            self, org, kind_scope):
        group = _singleton_group(org)
        owner = _person(org, 'Singleton Owner', is_owner=True)
        ItemGroup.objects.filter(pk=group.pk).update(
            ownership_person=owner,
        )

        result = generate_tasks(
            org,
            reasons=['UNVERIFIED', 'NO_CATEGORY'],
            kind_scope=kind_scope,
        )

        assert result['totals']['target'] == 1
        assert result['totals']['created'] == 1
        tasks = GovernanceTask.objects.filter(
            item_group=group,
            state=GovernanceTask.STATE_OPEN,
        )
        assert set(tasks.values_list('reason', flat=True)) == {
            GovernanceTask.REASON_NO_CATEGORY,
        }
        assert set(tasks.values_list('assignee_role', flat=True)) == {'owner'}
        assert set(tasks.values_list('assignee_id', flat=True)) == {owner.pk}

    def test_metadata_helper_creates_singleton_category_work(self, org):
        group = _singleton_group(org)

        result = sync_group_metadata_tasks(
            [group.pk],
            create_missing_category=True,
            create_missing_status=False,
        )

        assert result['created'] == 1
        assert GovernanceTask.objects.filter(
            item_group=group,
            reason=GovernanceTask.REASON_NO_CATEGORY,
            state=GovernanceTask.STATE_OPEN,
        ).exists()

    def test_narrow_sweep_leaves_out_of_scope_tasks_alone(self, org):
        """Auto-close only applies inside the swept scope. A measures-only run
        must not quietly resolve a singleton's task just because that group was
        never in its target set."""
        grp = _singleton_group(org)
        ItemGroup.objects.filter(pk=grp.pk).update(status='ATTENTION')
        generate_tasks(org, reasons=['ATTENTION'], kind_scope='all')
        task = GovernanceTask.objects.get(item_group=grp, reason='ATTENTION')

        result = generate_tasks(
            org, reasons=['ATTENTION'], kind_scope='measure_name',
        )

        assert result['reasons']['ATTENTION']['closed'] == 0
        task.refresh_from_db()
        assert task.state == 'open'

    def test_dry_run_counts_without_writing(self, org):
        _measure_group(org)
        result = generate_tasks(org, dry_run=True)

        assert result['dry_run'] is True
        assert result['totals']['created'] == 2
        assert result['totals']['target'] == 2
        assert GovernanceTask.objects.count() == 0

    def test_duplicate_open_task_is_rejected_by_the_database(self, org):
        """The dedupe rule is a DB invariant, not a convention — that's what lets
        the sweep bulk_create with ignore_conflicts instead of racing a
        read-then-write check."""
        grp = _measure_group(org)
        GovernanceTask.objects.create(
            organization=org, item_group=grp, reason='UNVERIFIED',
            title='Verify "Revenue"', state='open',
        )

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                GovernanceTask.objects.create(
                    organization=org, item_group=grp, reason='UNVERIFIED',
                    title='Verify "Revenue" again', state='open',
                )

        # The constraint is partial and reason-scoped: a closed twin and another
        # reason for the same group are both legal.
        GovernanceTask.objects.create(
            organization=org, item_group=grp, reason='UNVERIFIED',
            title='Verify "Revenue" (history)', state='done',
        )
        GovernanceTask.objects.create(
            organization=org, item_group=grp, reason='NO_CATEGORY',
            title='Set a category', state='open',
        )


@pytest.mark.django_db
class TestSweepAutoClose:
    """Finished work leaves the board without anyone pressing Done."""

    def test_empty_reason_selection_is_rejected_at_service_boundary(self, org):
        with pytest.raises(ValueError, match='At least one'):
            generate_tasks(org, reasons=[])

    def test_empty_group_is_not_targeted_and_existing_work_is_closed(
            self, org):
        group = ItemGroup.objects.create(
            group_key=f'{org.id}::preserved-empty',
            kind=ItemGroup.KIND_MEASURE_NAME,
            organization=org,
            status='UNVERIFIED',
        )
        task = GovernanceTask.objects.create(
            organization=org,
            item_group=group,
            reason=GovernanceTask.REASON_UNVERIFIED,
            trigger_status='UNVERIFIED',
            title='Verify preserved empty group',
        )

        result = generate_tasks(org, reasons=['UNVERIFIED'])

        assert result['reasons']['UNVERIFIED']['target'] == 0
        assert result['reasons']['UNVERIFIED']['created'] == 0
        assert result['reasons']['UNVERIFIED']['closed'] == 1
        task.refresh_from_db()
        assert task.state == GovernanceTask.STATE_DONE
        assert task.closed_reason == GovernanceTask.CLOSED_RESOLVED

    def test_linking_item_into_preserved_group_creates_applicable_tasks(
            self, org):
        owner = _person(org, 'Preserved Owner', is_owner=True)
        group = ItemGroup.objects.create(
            group_key=f'{org.id}::preserved-reappeared',
            kind=ItemGroup.KIND_MEASURE_NAME,
            organization=org,
            ownership_person=owner,
            status='UNVERIFIED',
        )
        assert generate_tasks(
            org, reasons=['UNVERIFIED', 'NO_CATEGORY'],
        )['totals']['target'] == 0

        # ETL inserts raw Item rows and the post-load linker attaches them.
        Item.objects.bulk_create([
            Item(
                item_id='preserved-reappeared-item',
                item_name='Preserved Reappeared',
                item_type='PB_MEASURE',
                group_id=group.group_key,
                organization=org,
                service='powerbi',
            ),
        ])
        from catalog.services.item_groups import ensure_item_groups

        ensure_item_groups(organization_id=org.id)

        item = Item.objects.get(pk='preserved-reappeared-item')
        assert item.item_group_id == group.pk
        tasks = GovernanceTask.objects.filter(
            item_group=group,
            state=GovernanceTask.STATE_OPEN,
        ).order_by('reason')
        assert set(tasks.values_list('reason', flat=True)) == {
            GovernanceTask.REASON_UNVERIFIED,
            GovernanceTask.REASON_NO_CATEGORY,
        }
        assert set(tasks.values_list('assignee_id', flat=True)) == {owner.pk}
        assert set(tasks.values_list('assignee_role', flat=True)) == {'owner'}

    def test_verifying_closes_the_unverified_task(self, org):
        grp = _measure_group(org)
        generate_tasks(org, reasons=['UNVERIFIED'])
        task = GovernanceTask.objects.get(item_group=grp, reason='UNVERIFIED')

        ItemGroup.objects.filter(pk=grp.pk).update(status='VERIFIED')
        result = generate_tasks(org, reasons=['UNVERIFIED'])

        assert result['reasons']['UNVERIFIED']['closed'] == 1
        task.refresh_from_db()
        assert task.state == 'done'
        # 'resolved', not 'manual': nobody did this by hand, so "who closed it"
        # stays answerable.
        assert task.closed_reason == 'resolved'
        assert task.completed_by_id is None
        assert task.completed_at is not None

    def test_relapse_opens_a_fresh_task(self, org):
        """Back to Unverified → a NEW task, not the old one reopened, so the
        closed row stays an accurate record of the first round."""
        grp = _measure_group(org)
        generate_tasks(org, reasons=['UNVERIFIED'])
        first = GovernanceTask.objects.get(item_group=grp, reason='UNVERIFIED')

        ItemGroup.objects.filter(pk=grp.pk).update(status='VERIFIED')
        generate_tasks(org, reasons=['UNVERIFIED'])
        ItemGroup.objects.filter(pk=grp.pk).update(status='UNVERIFIED')
        result = generate_tasks(org, reasons=['UNVERIFIED'])

        assert result['reasons']['UNVERIFIED']['created'] == 1
        fresh = GovernanceTask.objects.get(
            item_group=grp, reason='UNVERIFIED', state='open')
        assert fresh.id != first.id
        first.refresh_from_db()
        assert first.state == 'done'

    def test_setting_a_category_closes_the_category_task(self, org):
        grp = _measure_group(org)
        generate_tasks(org, reasons=['NO_CATEGORY'])
        task = GovernanceTask.objects.get(item_group=grp, reason='NO_CATEGORY')

        category = Category.objects.create(name='Finance', organization=org)
        ItemGroup.objects.filter(pk=grp.pk).update(category=category)
        result = generate_tasks(org, reasons=['NO_CATEGORY'])

        assert result['reasons']['NO_CATEGORY']['closed'] == 1
        task.refresh_from_db()
        assert task.state == 'done'
        assert task.closed_reason == 'resolved'


@pytest.mark.django_db
class TestDurableManualEpisodes:

    def test_done_stays_dismissed_until_clear_then_relapse(self, org):
        grp = _measure_group(org)
        generate_tasks(org, reasons=['UNVERIFIED'])
        first = GovernanceTask.objects.get(
            item_group=grp, reason='UNVERIFIED', state='open',
        )
        first.state = GovernanceTask.STATE_DONE
        first.closed_reason = GovernanceTask.CLOSED_MANUAL
        first.completed_at = timezone.now()
        first.save(update_fields=['state', 'closed_reason', 'completed_at'])

        same_episode = generate_tasks(org, reasons=['UNVERIFIED'])
        assert same_episode['totals']['created'] == 0
        assert not GovernanceTask.objects.filter(
            item_group=grp, reason='UNVERIFIED', state='open',
        ).exists()

        ItemGroup.objects.filter(pk=grp.pk).update(status='VERIFIED')
        generate_tasks(org, reasons=['UNVERIFIED'])
        first.refresh_from_db()
        assert first.condition_cleared_at is not None

        ItemGroup.objects.filter(pk=grp.pk).update(status='UNVERIFIED')
        relapse = generate_tasks(org, reasons=['UNVERIFIED'])
        assert relapse['totals']['created'] == 1
        fresh = GovernanceTask.objects.get(
            item_group=grp, reason='UNVERIFIED', state='open',
        )
        assert fresh.pk != first.pk

    def test_null_org_groups_are_never_adopted_by_a_sweep(self, org):
        ItemGroup.objects.create(
            group_key='legacy-null', kind=ItemGroup.KIND_MEASURE_NAME,
            organization=None, status='UNVERIFIED',
        )

        result = generate_tasks(org, reasons=['UNVERIFIED'])

        assert result['totals']['target'] == 0
        assert GovernanceTask.objects.count() == 0


@pytest.mark.django_db
class TestImmediateMetadataReconciliation:

    def test_owner_change_reassigns_existing_unverified_task(
            self, client, rw_user, org):
        grp = _measure_group(org)
        generate_tasks(org, reasons=['UNVERIFIED'])
        task = GovernanceTask.objects.get(item_group=grp, reason='UNVERIFIED')
        assert task.assignee_id is None
        owner = _person(org, 'New Owner', is_owner=True)

        client.login(username='writer@example.com', password='testpass')
        response = client.patch(
            f'/api/item-groups/{grp.pk}/',
            data=json.dumps({'ownership_person': owner.pk}),
            content_type='application/json',
        )

        assert response.status_code == 200
        task.refresh_from_db()
        assert task.assignee_id == owner.pk
        assert task.assignee_role == 'owner'

    def test_category_add_closes_and_later_removal_opens_fresh_task(
            self, client, rw_user, org):
        grp = _measure_group(org)
        generate_tasks(org, reasons=['NO_CATEGORY'])
        first = GovernanceTask.objects.get(
            item_group=grp, reason='NO_CATEGORY', state='open',
        )
        category = Category.objects.create(name='Finance', organization=org)
        client.login(username='writer@example.com', password='testpass')

        added = client.patch(
            f'/api/item-groups/{grp.pk}/',
            data=json.dumps({'category': category.pk}),
            content_type='application/json',
        )
        assert added.status_code == 200
        first.refresh_from_db()
        assert first.state == GovernanceTask.STATE_DONE
        assert first.closed_reason == GovernanceTask.CLOSED_RESOLVED

        removed = client.patch(
            f'/api/item-groups/{grp.pk}/',
            data=json.dumps({'category': None}),
            content_type='application/json',
        )
        assert removed.status_code == 200
        fresh = GovernanceTask.objects.get(
            item_group=grp, reason='NO_CATEGORY', state='open',
        )
        assert fresh.pk != first.pk

    @pytest.mark.parametrize(
        'restore_status', ['VERIFIED', 'UNVERIFIED', 'ATTENTION'],
    )
    def test_deleted_status_immediately_suspends_category_task(
            self, client, rw_user, org, restore_status):
        grp = _measure_group(org)
        generate_tasks(org, reasons=['NO_CATEGORY'])
        first = GovernanceTask.objects.get(
            item_group=grp, reason='NO_CATEGORY', state='open',
        )
        client.login(username='writer@example.com', password='testpass')

        deleted = _patch_group_status(client, grp.pk, 'DELETED')
        assert deleted.status_code == 200
        first.refresh_from_db()
        assert first.state == GovernanceTask.STATE_DONE
        assert first.closed_reason == GovernanceTask.CLOSED_RESOLVED

        restored = _patch_group_status(client, grp.pk, restore_status)
        assert restored.status_code == 200
        fresh = GovernanceTask.objects.get(
            item_group=grp, reason='NO_CATEGORY', state='open',
        )
        assert fresh.pk != first.pk

    def test_deleted_transition_clears_manual_category_episode(
            self, org):
        grp = _measure_group(org)
        generate_tasks(org, reasons=['NO_CATEGORY'])
        dismissed = GovernanceTask.objects.get(
            item_group=grp, reason='NO_CATEGORY', state='open',
        )
        dismissed.state = GovernanceTask.STATE_DONE
        dismissed.closed_reason = GovernanceTask.CLOSED_MANUAL
        dismissed.completed_at = timezone.now()
        dismissed.save(
            update_fields=['state', 'closed_reason', 'completed_at'],
        )
        from catalog.governance_tasks import sync_status_task

        ItemGroup.objects.filter(pk=grp.pk).update(status='DELETED')
        grp.refresh_from_db()
        sync_status_task(grp, 'DELETED', notify=False)
        dismissed.refresh_from_db()
        assert dismissed.condition_cleared_at is not None

        ItemGroup.objects.filter(pk=grp.pk).update(status='VERIFIED')
        grp.refresh_from_db()
        sync_status_task(grp, 'VERIFIED', notify=False)
        fresh = GovernanceTask.objects.get(
            item_group=grp, reason='NO_CATEGORY', state='open',
        )
        assert fresh.pk != dismissed.pk


@pytest.mark.django_db
class TestSweepNotifications:
    """A sweep must never post one Slack message per task."""

    @patch('etl.hooks.slack.slack_alerts.send_slack_task_digest')
    @patch('etl.hooks.slack.slack_alerts.send_slack_task_alert')
    def test_notify_false_is_silent(self, mock_alert, mock_digest, org, slack_hook):
        _measure_group(org)
        result = generate_tasks(org, notify=False)

        assert result['totals']['created'] == 2
        mock_alert.assert_not_called()
        mock_digest.assert_not_called()

    @patch('etl.hooks.slack.slack_alerts.send_slack_task_digest')
    @patch('etl.hooks.slack.slack_alerts.send_slack_task_alert')
    def test_reassigned_only_run_sends_one_digest(
            self, mock_alert, mock_digest, org, slack_hook):
        group = _measure_group(org)
        generate_tasks(org, reasons=['UNVERIFIED'], notify=False)
        owner = _person(org, 'Owner', is_owner=True)
        ItemGroup.objects.filter(pk=group.pk).update(
            ownership_person=owner,
        )

        result = generate_tasks(
            org, reasons=['UNVERIFIED'], notify=True,
        )

        assert result['totals']['created'] == 0
        assert result['totals']['closed'] == 0
        assert result['totals']['reassigned'] == 1
        mock_digest.assert_called_once()
        mock_alert.assert_not_called()

    @patch('etl.hooks.slack.slack_alerts.send_slack_task_digest')
    @patch('etl.hooks.slack.slack_alerts.send_slack_task_alert')
    def test_notify_true_sends_one_digest(self, mock_alert, mock_digest, org, slack_hook):
        """Two tasks, one message — per-task pings stay on the event path where
        a human action bounds the volume."""
        _measure_group(org)
        generate_tasks(org, notify=True)

        mock_alert.assert_not_called()
        mock_digest.assert_called_once()


@pytest.mark.django_db
class TestDoneAction:

    def _open_task(self, client, item_with_org, rw_user):
        steward = DataPerson.objects.create(
            name='Writer Steward',
            organization=item_with_org.organization,
            user=rw_user,
            is_steward=True,
        )
        group = item_with_org.item_group
        group.steward = steward
        group.save(update_fields=['steward'])
        _patch_group_status(client, item_with_org.item_group_id, 'ATTENTION')
        return GovernanceTask.objects.get(
            item_group_id=item_with_org.item_group_id,
            reason=GovernanceTask.REASON_ATTENTION,
            state=GovernanceTask.STATE_OPEN,
        )

    def test_done_marks_and_hides(self, client, rw_user, item_with_org):
        client.login(username='writer@example.com', password='testpass')
        task = self._open_task(client, item_with_org, rw_user)

        resp = client.post(f'/api/tasks/{task.id}/done/')
        assert resp.status_code == 200

        task.refresh_from_db()
        assert task.state == 'done'
        assert task.completed_at is not None
        assert task.completed_by_id == rw_user.id
        assert task.closed_reason == 'manual'

        # Default feed (open only) excludes the completed task...
        ids = [t['id'] for t in client.get('/api/tasks/').json()['results']]
        assert task.id not in ids
        # ...but it's still reachable via ?state=done.
        done_ids = [t['id'] for t in client.get('/api/tasks/?state=done').json()['results']]
        assert task.id in done_ids

    def test_bulk_done_rejects_non_object_body(self, client, rw_user):
        client.login(username='writer@example.com', password='testpass')

        response = client.post(
            '/api/tasks/bulk-done/',
            data=json.dumps([]),
            content_type='application/json',
        )

        assert response.status_code == 400
        assert response.json()['error'] == 'Expected an object payload.'


@pytest.mark.django_db
class TestTaskFeedApi:
    """The Task Manager page's own calls: scoping, bulk close, default state."""

    def test_scope_mine_returns_only_my_tasks(self, client, rw_user, org):
        me = DataPerson.objects.create(name='Me Myself', organization=org, user=rw_user)
        someone_else = _person(org, 'Other Person')
        mine = _bare_task(org, assignee=me, title='Mine')
        theirs = _bare_task(org, assignee=someone_else, reason='DELETED', title='Theirs')

        client.login(username='writer@example.com', password='testpass')
        ids = [t['id'] for t in client.get('/api/tasks/?scope=mine').json()['results']]

        assert ids == [mine.id]
        assert theirs.id not in ids

    def test_scope_mine_without_a_data_person_is_empty(self, client, ro_user, org):
        """A login with no DataPerson has no governance identity, so "my tasks"
        is empty — showing everyone else's would be worse than showing none."""
        _bare_task(org, assignee=_person(org, 'Other Person'), title='Not yours')

        client.login(username='readonly@example.com', password='testpass')
        results = client.get('/api/tasks/?scope=mine').json()['results']

        assert results == []

    def test_default_list_excludes_done_tasks(self, client, rw_user, org):
        me = DataPerson.objects.create(
            name='Writer', organization=org, user=rw_user,
        )
        still_open = _bare_task(org, assignee=me, title='Open one')
        closed = _bare_task(org, reason='DELETED', title='Closed one',
                            assignee=me, state='done', closed_reason='manual')

        client.login(username='writer@example.com', password='testpass')
        ids = [t['id'] for t in client.get('/api/tasks/').json()['results']]
        assert ids == [still_open.id]

        all_ids = [t['id'] for t in client.get('/api/tasks/?state=all').json()['results']]
        assert {still_open.id, closed.id} <= set(all_ids)

    def test_bulk_done_closes_the_given_ids(self, client, rw_user, org):
        me = DataPerson.objects.create(
            name='Writer', organization=org, user=rw_user,
        )
        first = _bare_task(org, assignee=me, title='One')
        second = _bare_task(org, assignee=me, reason='DELETED', title='Two')
        untouched = _bare_task(
            org, assignee=me, reason='UNVERIFIED', title='Three',
        )

        client.login(username='writer@example.com', password='testpass')
        resp = client.post(
            '/api/tasks/bulk-done/',
            data=json.dumps({'ids': [first.id, second.id]}),
            content_type='application/json',
        )
        assert resp.status_code == 200
        assert resp.json()['updated'] == 2

        for task in (first, second):
            task.refresh_from_db()
            assert task.state == 'done'
            assert task.closed_reason == 'manual'
            assert task.completed_by_id == rw_user.id
        untouched.refresh_from_db()
        assert untouched.state == 'open'

    def test_bulk_done_cannot_close_another_orgs_task(self, client, rw_user, org):
        """Ids come from the client, so bulk-done runs through the same org
        filter as the feed — guessing an id can't reach a neighbour's board."""
        other_org = Organization.objects.create(name='Neighbour Org')
        foreign = _bare_task(other_org, title='Not yours')

        client.login(username='writer@example.com', password='testpass')
        resp = client.post(
            '/api/tasks/bulk-done/',
            data=json.dumps({'ids': [foreign.id]}),
            content_type='application/json',
        )
        assert resp.status_code == 200
        assert resp.json()['updated'] == 0

        foreign.refresh_from_db()
        assert foreign.state == 'open'
        assert foreign.closed_reason is None

    def test_bulk_done_rejects_an_oversized_batch(self, client, rw_user, org):
        """The cap used to be a silent `[:1000]` slice: the endpoint answered 200
        with a smaller `updated` nothing read, so a user pressed "Mark 1200 done",
        watched the list shrink, and 200 tasks stayed open with no indication.
        Reject loudly instead — the client chunks, so reaching this is API misuse."""
        from catalog.views import GovernanceTaskViewSet

        limit = GovernanceTaskViewSet.BULK_DONE_LIMIT
        client.login(username='writer@example.com', password='testpass')
        resp = client.post(
            '/api/tasks/bulk-done/',
            data=json.dumps({'ids': list(range(1, limit + 2))}),
            content_type='application/json',
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body['limit'] == limit
        assert body['requested'] == limit + 1

    def test_bulk_done_reports_requested_alongside_updated(self, client, rw_user, org):
        """`updated` alone can't be compared to what the user selected — an
        already-done task lowers it. Returning both lets the UI say so."""
        me = DataPerson.objects.create(
            name='Writer', organization=org, user=rw_user,
        )
        open_task = _bare_task(org, assignee=me, title='Open')
        already = _bare_task(
            org, assignee=me, reason='DELETED', title='Already done',
        )
        already.state = 'done'
        already.save(update_fields=['state'])

        client.login(username='writer@example.com', password='testpass')
        resp = client.post(
            '/api/tasks/bulk-done/',
            data=json.dumps({'ids': [open_task.id, already.id]}),
            content_type='application/json',
        )
        assert resp.status_code == 200
        assert resp.json() == {'status': 'ok', 'requested': 2, 'updated': 1}


@pytest.mark.django_db
class TestTaskApiSecurity:

    def test_nonadmin_cannot_widen_or_complete_someone_elses_task(
            self, client, rw_user, org):
        me = DataPerson.objects.create(
            name='Writer', organization=org, user=rw_user,
        )
        other = _person(org, 'Other')
        mine = _bare_task(org, assignee=me, title='Mine')
        theirs = _bare_task(
            org, assignee=other, reason='DELETED', title='Theirs',
        )
        client.login(username='writer@example.com', password='testpass')

        listed = client.get('/api/tasks/?scope=all').json()['results']
        assert [row['id'] for row in listed] == [mine.pk]
        assert client.post(f'/api/tasks/{theirs.pk}/done/').status_code == 404
        bulk = client.post(
            '/api/tasks/bulk-done/',
            data=json.dumps({'ids': [theirs.pk]}),
            content_type='application/json',
        )
        assert bulk.json()['updated'] == 0
        theirs.refresh_from_db()
        assert theirs.state == GovernanceTask.STATE_OPEN

    def test_done_rechecks_current_assignee_after_initial_selection(
            self, client, rw_user, org):
        """A stale pre-lock selection cannot authorize the former assignee."""
        from catalog.views import GovernanceTaskViewSet

        DataPerson.objects.create(
            name='Writer', organization=org, user=rw_user,
        )
        other = _person(org, 'Other')
        reassigned = _bare_task(
            org, assignee=other, reason='DELETED', title='Reassigned',
        )
        client.login(username='writer@example.com', password='testpass')

        # Simulate a row that passed an earlier assignee-scoped lookup before a
        # concurrent metadata reconciliation reassigned it.
        stale_scope = GovernanceTask.objects.filter(organization=org)
        with patch.object(
            GovernanceTaskViewSet,
            '_action_queryset',
            return_value=stale_scope,
        ):
            single = client.post(f'/api/tasks/{reassigned.pk}/done/')
            bulk = client.post(
                '/api/tasks/bulk-done/',
                data=json.dumps({'ids': [reassigned.pk]}),
                content_type='application/json',
            )

        assert single.status_code == 404
        assert bulk.status_code == 200
        assert bulk.json()['updated'] == 0
        reassigned.refresh_from_db()
        assert reassigned.state == GovernanceTask.STATE_OPEN

    def test_unlinked_member_gets_empty_feed_and_identity_signal(
            self, client, ro_user, org):
        _bare_task(org, assignee=_person(org, 'Other'))
        client.login(username='readonly@example.com', password='testpass')

        assert client.get('/api/tasks/?scope=all').json()['results'] == []
        summary = client.get('/api/tasks/summary/').json()
        assert summary['identity_required'] is True
        assert summary['total_open'] == 0
        assert summary['mine_open'] == 0

    def test_generic_task_crud_is_not_exposed(self, client, rw_user, org):
        me = DataPerson.objects.create(
            name='Writer', organization=org, user=rw_user,
        )
        task = _bare_task(org, assignee=me)
        client.login(username='writer@example.com', password='testpass')

        assert client.post(
            '/api/tasks/',
            data=json.dumps({'title': 'Forged'}),
            content_type='application/json',
        ).status_code == 405
        assert client.patch(
            f'/api/tasks/{task.pk}/',
            data=json.dumps({'assignee': None}),
            content_type='application/json',
        ).status_code == 405
        assert client.delete(f'/api/tasks/{task.pk}/').status_code == 405

    def test_anonymous_and_orgless_callers_fail_closed(self, client, org):
        anonymous = client.get('/api/tasks/')
        assert anonymous.status_code in (401, 403)

        orgless = CustomUser.objects.create_user(
            username='orgless', email='orgless@example.com', password='testpass',
        )
        client.login(username=orgless.email, password='testpass')
        assert client.get('/api/tasks/').status_code == 403
        assert client.get('/api/items/').status_code == 403

    def test_null_org_task_is_not_visible_even_to_admin(self, client, org):
        admin = CustomUser.objects.create_user(
            username='admin', email='admin@example.com', password='testpass',
        )
        OrganizationMembership.objects.create(
            user=admin, organization=org, is_admin=True,
        )
        linked = DataPerson.objects.create(
            name='Admin', user=admin, organization=org,
        )
        visible = _bare_task(org, assignee=linked, title='Visible')
        _bare_task(None, assignee=linked, reason='DELETED', title='Quarantined')
        client.login(username=admin.email, password='testpass')

        ids = [
            row['id']
            for row in client.get('/api/tasks/?scope=all').json()['results']
        ]
        assert ids == [visible.pk]

    def test_company_page_tier_is_enforced_by_task_and_definition_apis(
            self, client, org):
        user = CustomUser.objects.create_user(
            username='no-tier', email='no-tier@example.com', password='testpass',
        )
        OrganizationMembership.objects.create(user=user, organization=org)
        client.login(username=user.email, password='testpass')

        assert client.get('/api/tasks/').status_code == 403
        assert client.get('/api/definitions/').status_code == 403

        company, _ = Group.objects.get_or_create(name='Company')
        user.groups.add(company)
        assert client.get('/api/tasks/').status_code == 200
        assert client.get('/api/definitions/').status_code == 200


@pytest.mark.django_db
class TestGenerateApiPreview:

    def test_explicit_empty_reason_list_is_rejected_without_writes(
            self, client, org):
        admin = CustomUser.objects.create_user(
            username='empty-reasons-admin',
            email='empty-reasons-admin@example.com',
            password='testpass',
        )
        OrganizationMembership.objects.create(
            user=admin, organization=org, is_admin=True,
        )
        _measure_group(org)
        client.login(
            username='empty-reasons-admin@example.com',
            password='testpass',
        )

        response = client.post(
            '/api/tasks/generate/',
            data=json.dumps({'reasons': []}),
            content_type='application/json',
        )

        assert response.status_code == 400
        assert GovernanceTask.objects.count() == 0

    @pytest.mark.parametrize('invalid_scope', ['', 0, False])
    def test_provided_falsy_kind_scope_is_rejected(
            self, client, org, invalid_scope):
        admin = CustomUser.objects.create_user(
            username=f'invalid-scope-{invalid_scope!r}',
            email=f'invalid-scope-{str(invalid_scope).lower() or "blank"}@example.com',
            password='testpass',
        )
        OrganizationMembership.objects.create(
            user=admin, organization=org, is_admin=True,
        )
        _measure_group(org)
        client.force_login(admin)

        response = client.post(
            '/api/tasks/generate/',
            data=json.dumps({'kind_scope': invalid_scope}),
            content_type='application/json',
        )

        assert response.status_code == 400
        assert GovernanceTask.objects.count() == 0

    def test_generate_rejects_non_object_body(self, client, org):
        admin = CustomUser.objects.create_user(
            username='array-body-admin',
            email='array-body-admin@example.com',
            password='testpass',
        )
        OrganizationMembership.objects.create(
            user=admin, organization=org, is_admin=True,
        )
        client.force_login(admin)

        response = client.post(
            '/api/tasks/generate/',
            data=json.dumps([]),
            content_type='application/json',
        )

        assert response.status_code == 400
        assert response.json()['error'] == 'Expected an object payload.'

    def test_broad_commit_requires_exact_signed_preview(self, client, org):
        admin = CustomUser.objects.create_user(
            username='admin', email='admin@example.com', password='testpass',
        )
        OrganizationMembership.objects.create(
            user=admin, organization=org, is_admin=True,
        )
        grp = _singleton_group(org)
        ItemGroup.objects.filter(pk=grp.pk).update(status='ATTENTION')
        client.login(username=admin.email, password='testpass')
        base = {
            'reasons': ['ATTENTION'],
            'kind_scope': 'all',
            'require_assignee': False,
        }

        no_preview = client.post(
            '/api/tasks/generate/',
            data=json.dumps(base),
            content_type='application/json',
        )
        assert no_preview.status_code == 400

        preview = client.post(
            '/api/tasks/generate/',
            data=json.dumps({**base, 'dry_run': True}),
            content_type='application/json',
        )
        assert preview.status_code == 200
        token = preview.json()['preview_token']

        mismatch = client.post(
            '/api/tasks/generate/',
            data=json.dumps({
                **base,
                'reasons': ['DELETED'],
                'preview_token': token,
            }),
            content_type='application/json',
        )
        assert mismatch.status_code == 400

        committed = client.post(
            '/api/tasks/generate/',
            data=json.dumps({**base, 'preview_token': token}),
            content_type='application/json',
        )
        assert committed.status_code == 200
        assert committed.json()['totals']['created'] == 1

    def test_broad_commit_rejects_a_catalog_snapshot_that_widened(
            self, client, org):
        admin = CustomUser.objects.create_user(
            username='snapshot-admin',
            email='snapshot-admin@example.com',
            password='testpass',
        )
        OrganizationMembership.objects.create(
            user=admin, organization=org, is_admin=True,
        )
        first = _singleton_group(org, 'dbt_first')
        ItemGroup.objects.filter(pk=first.pk).update(status='ATTENTION')
        client.login(username=admin.email, password='testpass')
        options = {
            'reasons': ['ATTENTION'],
            'kind_scope': 'all',
            'require_assignee': False,
        }
        preview = client.post(
            '/api/tasks/generate/',
            data=json.dumps({**options, 'dry_run': True}),
            content_type='application/json',
        )

        second = _singleton_group(org, 'dbt_second')
        ItemGroup.objects.filter(pk=second.pk).update(status='ATTENTION')
        commit = client.post(
            '/api/tasks/generate/',
            data=json.dumps({
                **options,
                'preview_token': preview.json()['preview_token'],
            }),
            content_type='application/json',
        )

        assert commit.status_code == 409
        assert commit.json()['code'] == 'preview_stale'
        assert not GovernanceTask.objects.filter(
            organization=org, reason='ATTENTION',
        ).exists()

    def test_all_scope_snapshot_avoids_a_monolithic_id_list(
            self, org):
        for index in range(3):
            group = _singleton_group(org, f'boundary_{index}')
            ItemGroup.objects.filter(pk=group.pk).update(status='ATTENTION')

        with CaptureQueriesContext(connection) as queries:
            generate_tasks(
                org,
                reasons=['ATTENTION'],
                kind_scope='all',
                dry_run=True,
            )

        sql = '\n'.join(query['sql'] for query in queries.captured_queries)
        assert '"catalog_governancetask"."item_group_id" IN (' not in sql
        assert '"catalog_itemgroup"."id" IN (' not in sql

    def test_nonadmin_cannot_generate(self, client, rw_user, org):
        client.login(username='writer@example.com', password='testpass')
        response = client.post(
            '/api/tasks/generate/',
            data=json.dumps({'dry_run': True}),
            content_type='application/json',
        )
        assert response.status_code == 403


@pytest.mark.django_db
class TestSlackTaskAlert:

    @patch('slack_sdk.WebClient')
    def test_alert_tags_steward_handle(
            self, MockWebClient, item_with_org, slack_hook,
            django_capture_on_commit_callbacks):
        mock_client = MagicMock()
        MockWebClient.return_value = mock_client

        steward = DataPerson.objects.create(
            name='Sam Steward', is_steward=True, slack_handle='@sam',
            organization=item_with_org.organization,
        )
        grp = ItemGroup.objects.get(pk=item_with_org.item_group_id)
        grp.steward = steward
        grp.status = 'ATTENTION'
        grp.save(update_fields=['steward', 'status'])

        from catalog.governance_tasks import sync_status_task
        with django_capture_on_commit_callbacks(execute=True):
            task = sync_status_task(grp, 'ATTENTION', None)

        assert task is not None
        mock_client.chat_postMessage.assert_called_once()
        text = mock_client.chat_postMessage.call_args[1]['text']
        assert '@sam' in text
        assert 'governance task' in text.lower()

    @patch('slack_sdk.WebClient')
    def test_no_handle_no_tag(
            self, MockWebClient, item_with_org, slack_hook,
            django_capture_on_commit_callbacks):
        """Unassigned task posts an alert but no @handle line."""
        mock_client = MagicMock()
        MockWebClient.return_value = mock_client

        grp = ItemGroup.objects.get(pk=item_with_org.item_group_id)
        grp.status = 'DELETED'
        grp.save(update_fields=['status'])
        from catalog.governance_tasks import sync_status_task
        with django_capture_on_commit_callbacks(execute=True):
            sync_status_task(grp, 'DELETED', None)

        mock_client.chat_postMessage.assert_called_once()
        text = mock_client.chat_postMessage.call_args[1]['text']
        assert '@' not in text

    def test_no_hook_skips_gracefully(self, item_with_org):
        """No active slack hook → task still created, no exception."""
        grp = ItemGroup.objects.get(pk=item_with_org.item_group_id)
        grp.status = 'ATTENTION'
        grp.save(update_fields=['status'])
        from catalog.governance_tasks import sync_status_task
        task = sync_status_task(grp, 'ATTENTION', None)
        assert task is not None
