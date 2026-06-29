"""
Tests for the governance Task Manager: task creation on status changes,
dedupe, the `done` action, the deleted-items history, and the Slack task alert.
"""
import json
import pytest
from unittest.mock import patch, MagicMock

from catalog.models import ItemGroup, GovernanceTask, DataPerson


def _patch_group_status(client, group_pk, status):
    return client.patch(
        f'/api/item-groups/{group_pk}/',
        data=json.dumps({'status': status}),
        content_type='application/json',
    )


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
        assert tasks.first().trigger_status == status

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

    def test_routing_policy_is_extensible_to_owner(self, item_with_org):
        """The routing policy is a single, ordered list of roles. Flipping it to
        owner (a future "tasks can be for others too") routes with no other
        change — guards the extensibility the design promises."""
        owner = DataPerson.objects.create(
            name='Olivia Owner', is_owner=True,
            organization=item_with_org.organization,
        )
        grp = ItemGroup.objects.get(pk=item_with_org.item_group_id)
        grp.ownership_person = owner
        grp.save(update_fields=['ownership_person'])

        from catalog import governance_tasks
        original = governance_tasks.ASSIGNEE_ROLES
        try:
            governance_tasks.ASSIGNEE_ROLES = ('steward', 'owner')
            task = governance_tasks.sync_status_task(grp, 'ATTENTION', None)
        finally:
            governance_tasks.ASSIGNEE_ROLES = original

        assert task.assignee_id == owner.id
        assert task.assignee_role == 'owner'

    def test_unassigned_when_no_steward(self, client, rw_user, item_with_org):
        client.login(username='writer@example.com', password='testpass')
        gpk = item_with_org.item_group_id
        _patch_group_status(client, gpk, 'ATTENTION')
        task = GovernanceTask.objects.get(item_group_id=gpk, state='open')
        assert task.assignee_id is None

    def test_no_duplicate_open_task(self, client, rw_user, item_with_org):
        """Repeated flips refresh the single open task instead of spawning more."""
        client.login(username='writer@example.com', password='testpass')
        gpk = item_with_org.item_group_id
        _patch_group_status(client, gpk, 'ATTENTION')
        _patch_group_status(client, gpk, 'DELETED')

        open_tasks = GovernanceTask.objects.filter(item_group_id=gpk, state='open')
        assert open_tasks.count() == 1
        assert open_tasks.first().trigger_status == 'DELETED'

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

        item_with_org.refresh_from_db()
        assert item_with_org.deleted is True
        assert item_with_org.deleted_at is not None

        # Auto-DEPRECATE also stamps the group-level deleted_at (coupled to status).
        grp = ItemGroup.objects.get(pk=gpk)
        assert grp.status == 'DELETED'
        assert grp.deleted_at is not None


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

        # Default feed (open only) excludes the completed task...
        ids = [t['id'] for t in client.get('/api/tasks/').json()['results']]
        assert task.id not in ids
        # ...but it's still reachable via ?state=done.
        done_ids = [t['id'] for t in client.get('/api/tasks/?state=done').json()['results']]
        assert task.id in done_ids


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
