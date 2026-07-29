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
from unittest.mock import patch, MagicMock

from django.db import IntegrityError, transaction

from catalog.governance_tasks import generate_tasks
from catalog.models import (
    Category, DataPerson, GovernanceTask, Item, ItemGroup, Organization,
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
        assert tasks.count() == 1
        task = tasks.first()
        assert task.trigger_status == status
        # The two status-derived reasons deliberately share the status value.
        assert task.reason == status

    def test_verified_creates_no_task(self, client, rw_user, item_with_org):
        client.login(username='writer@example.com', password='testpass')
        gpk = item_with_org.item_group_id
        resp = _patch_group_status(client, gpk, 'VERIFIED')
        assert resp.status_code == 200
        assert not GovernanceTask.objects.filter(item_group_id=gpk).exists()

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

        task = GovernanceTask.objects.get(item_group=grp, state='open')
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
        """The sweep is bounded to measure groups (see KIND_SCOPES); the event
        path deliberately is not, so hand-flagging a table / report / dbt model
        still raises a task for it."""
        from catalog.governance_tasks import sync_status_task
        grp = _singleton_group(org)

        task = sync_status_task(grp, 'ATTENTION', None)

        assert task is not None
        assert task.item_group_id == grp.id
        assert task.reason == 'ATTENTION'

    def test_unassigned_when_no_steward(self, client, rw_user, item_with_org):
        client.login(username='writer@example.com', password='testpass')
        gpk = item_with_org.item_group_id
        _patch_group_status(client, gpk, 'ATTENTION')
        task = GovernanceTask.objects.get(item_group_id=gpk, state='open')
        assert task.assignee_id is None

    def test_repeat_flip_refreshes_the_same_task(self, client, rw_user, item_with_org):
        """Landing on the same status twice refreshes the one open task for that
        reason instead of spawning a second."""
        client.login(username='writer@example.com', password='testpass')
        gpk = item_with_org.item_group_id
        _patch_group_status(client, gpk, 'ATTENTION')
        first_id = GovernanceTask.objects.get(item_group_id=gpk, state='open').id

        _patch_group_status(client, gpk, 'VERIFIED')
        _patch_group_status(client, gpk, 'ATTENTION')

        open_tasks = GovernanceTask.objects.filter(
            item_group_id=gpk, reason='ATTENTION', state='open')
        assert open_tasks.count() == 1
        assert open_tasks.first().id == first_id

    def test_different_status_opens_its_own_reason(self, client, rw_user, item_with_org):
        """`reason` is half the dedupe key, so ATTENTION -> DELETED leaves two
        open tasks. Retiring the one that no longer applies is the sweep's job,
        not the event path's — the event path only knows about the new status."""
        client.login(username='writer@example.com', password='testpass')
        gpk = item_with_org.item_group_id
        _patch_group_status(client, gpk, 'ATTENTION')
        _patch_group_status(client, gpk, 'DELETED')

        reasons = set(GovernanceTask.objects.filter(item_group_id=gpk, state='open')
                      .values_list('reason', flat=True))
        assert reasons == {'ATTENTION', 'DELETED'}

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
            f'/api/items/{item_with_org.item_id}/',
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

    def test_unverified_falls_back_to_steward_when_no_owner(self, org):
        """Owner-first, but a measure with no owner still has to land somewhere —
        the ordered-roles fallback is what stops it going unassigned."""
        grp = _measure_group(org)
        steward = _person(org, 'Sam Steward', is_steward=True)
        grp.steward = steward
        grp.save(update_fields=['steward'])

        result = generate_tasks(org, reasons=['UNVERIFIED'])

        assert result['reasons']['UNVERIFIED']['unassigned'] == 0
        task = GovernanceTask.objects.get(item_group=grp, reason='UNVERIFIED')
        assert task.assignee_id == steward.id
        assert task.assignee_role == 'steward'

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

    def test_singleton_is_outside_the_default_sweep_scope(self, org):
        """Every non-measure item has its own singleton group; sweeping them
        would mint tens of thousands of tasks nobody asked for, so the default
        kind_scope is measures-only. Reaching them is a caller's explicit choice
        (the Generate dialog's dropdown / --kind-scope), not a code change."""
        grp = _singleton_group(org)
        assert grp.kind == ItemGroup.KIND_SINGLETON
        ItemGroup.objects.filter(pk=grp.pk).update(status='ATTENTION')

        default_run = generate_tasks(org)
        assert default_run['kind_scope'] == 'measure_name'
        assert default_run['totals']['created'] == 0
        assert not GovernanceTask.objects.filter(item_group=grp).exists()

        widened = generate_tasks(org, reasons=['ATTENTION'], kind_scope='all')
        assert widened['reasons']['ATTENTION']['created'] == 1
        assert GovernanceTask.objects.filter(
            item_group=grp, reason='ATTENTION', state='open').exists()

    def test_narrow_sweep_leaves_out_of_scope_tasks_alone(self, org):
        """Auto-close only applies inside the swept scope. A measures-only run
        must not quietly resolve a singleton's task just because that group was
        never in its target set."""
        grp = _singleton_group(org)
        ItemGroup.objects.filter(pk=grp.pk).update(status='ATTENTION')
        generate_tasks(org, reasons=['ATTENTION'], kind_scope='all')
        task = GovernanceTask.objects.get(item_group=grp, reason='ATTENTION')

        result = generate_tasks(org, reasons=['ATTENTION'])

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
    def test_notify_true_sends_one_digest(self, mock_alert, mock_digest, org, slack_hook):
        """Two tasks, one message — per-task pings stay on the event path where
        a human action bounds the volume."""
        _measure_group(org)
        generate_tasks(org, notify=True)

        mock_alert.assert_not_called()
        mock_digest.assert_called_once()


@pytest.mark.django_db
class TestDoneAction:

    def _open_task(self, client, item_with_org):
        _patch_group_status(client, item_with_org.item_group_id, 'ATTENTION')
        return GovernanceTask.objects.get(item_group_id=item_with_org.item_group_id, state='open')

    def test_done_marks_and_hides(self, client, rw_user, item_with_org):
        client.login(username='writer@example.com', password='testpass')
        task = self._open_task(client, item_with_org)

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
        still_open = _bare_task(org, title='Open one')
        closed = _bare_task(org, reason='DELETED', title='Closed one',
                            state='done', closed_reason='manual')

        client.login(username='writer@example.com', password='testpass')
        ids = [t['id'] for t in client.get('/api/tasks/').json()['results']]
        assert ids == [still_open.id]

        all_ids = [t['id'] for t in client.get('/api/tasks/?state=all').json()['results']]
        assert {still_open.id, closed.id} <= set(all_ids)

    def test_bulk_done_closes_the_given_ids(self, client, rw_user, org):
        first = _bare_task(org, title='One')
        second = _bare_task(org, reason='DELETED', title='Two')
        untouched = _bare_task(org, reason='UNVERIFIED', title='Three')

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
        open_task = _bare_task(org, title='Open')
        already = _bare_task(org, reason='DELETED', title='Already done')
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
class TestSlackTaskAlert:

    @patch('slack_sdk.WebClient')
    def test_alert_tags_steward_handle(self, MockWebClient, item_with_org, slack_hook):
        mock_client = MagicMock()
        MockWebClient.return_value = mock_client

        steward = DataPerson.objects.create(
            name='Sam Steward', is_steward=True, slack_handle='@sam',
            organization=item_with_org.organization,
        )
        grp = ItemGroup.objects.get(pk=item_with_org.item_group_id)
        grp.steward = steward
        grp.save(update_fields=['steward'])

        from catalog.governance_tasks import sync_status_task
        task = sync_status_task(grp, 'ATTENTION', None)

        assert task is not None
        mock_client.chat_postMessage.assert_called_once()
        text = mock_client.chat_postMessage.call_args[1]['text']
        assert '@sam' in text
        assert 'governance task' in text.lower()

    @patch('slack_sdk.WebClient')
    def test_no_handle_no_tag(self, MockWebClient, item_with_org, slack_hook):
        """Unassigned task posts an alert but no @handle line."""
        mock_client = MagicMock()
        MockWebClient.return_value = mock_client

        grp = ItemGroup.objects.get(pk=item_with_org.item_group_id)
        from catalog.governance_tasks import sync_status_task
        sync_status_task(grp, 'DELETED', None)

        mock_client.chat_postMessage.assert_called_once()
        text = mock_client.chat_postMessage.call_args[1]['text']
        assert '@' not in text

    def test_no_hook_skips_gracefully(self, item_with_org):
        """No active slack hook → task still created, no exception."""
        grp = ItemGroup.objects.get(pk=item_with_org.item_group_id)
        from catalog.governance_tasks import sync_status_task
        task = sync_status_task(grp, 'ATTENTION', None)
        assert task is not None
