"""
Tests for Slack alert helper functions.
All Slack SDK calls are mocked — no real API calls.
"""
import pytest
from unittest.mock import patch, MagicMock, PropertyMock
from catalog.models import Item, Organization, IntegrationHook, IntegrationSource, SourceRunLog


@pytest.mark.django_db
class TestSendSlackItemAlert:
    """Tests for send_slack_item_alert (status / deleted changes)."""

    @patch('slack_sdk.WebClient')
    def test_status_change_sends_message(self, MockWebClient, item_with_org, rw_user, slack_hook):
        mock_client = MagicMock()
        MockWebClient.return_value = mock_client

        from etl.hooks.slack.slack_alerts import send_slack_item_alert
        send_slack_item_alert(item_with_org, rw_user, 'status', 'UNVERIFIED', 'VERIFIED')

        mock_client.chat_postMessage.assert_called_once()
        call_kwargs = mock_client.chat_postMessage.call_args[1]
        assert call_kwargs['channel'] == '#alerts'
        assert 'status changed' in call_kwargs['text']
        assert 'VERIFIED' in call_kwargs['text']

    @patch('slack_sdk.WebClient')
    def test_deleted_sends_message(self, MockWebClient, item_with_org, rw_user, slack_hook):
        mock_client = MagicMock()
        MockWebClient.return_value = mock_client

        from etl.hooks.slack.slack_alerts import send_slack_item_alert
        send_slack_item_alert(item_with_org, rw_user, 'deleted', False, True)

        mock_client.chat_postMessage.assert_called_once()
        call_kwargs = mock_client.chat_postMessage.call_args[1]
        assert 'marked for deletion' in call_kwargs['text']

    def test_no_org_skips_gracefully(self, item, rw_user):
        """If item has no organization_id, alert is skipped (no exception)."""
        from etl.hooks.slack.slack_alerts import send_slack_item_alert
        # Should not raise
        send_slack_item_alert(item, rw_user, 'status', 'UNVERIFIED', 'VERIFIED')

    def test_no_hook_skips_gracefully(self, item_with_org, rw_user):
        """If there's no active slack_alerts hook, alert is skipped."""
        from etl.hooks.slack.slack_alerts import send_slack_item_alert
        # No slack_hook fixture → no IntegrationHook exists
        send_slack_item_alert(item_with_org, rw_user, 'status', 'UNVERIFIED', 'VERIFIED')

    @patch('slack_sdk.WebClient')
    def test_exception_swallowed(self, MockWebClient, item_with_org, rw_user, slack_hook):
        """Exceptions inside send_slack_item_alert never propagate."""
        mock_client = MagicMock()
        mock_client.chat_postMessage.side_effect = Exception('API error')
        MockWebClient.return_value = mock_client

        from etl.hooks.slack.slack_alerts import send_slack_item_alert
        # Should not raise
        send_slack_item_alert(item_with_org, rw_user, 'status', 'UNVERIFIED', 'VERIFIED')

    @patch('slack_sdk.WebClient')
    def test_deleted_false_skips(self, MockWebClient, item_with_org, rw_user, slack_hook):
        """change_type='deleted' with new_value=False sends nothing."""
        mock_client = MagicMock()
        MockWebClient.return_value = mock_client

        from etl.hooks.slack.slack_alerts import send_slack_item_alert
        send_slack_item_alert(item_with_org, rw_user, 'deleted', False, False)
        mock_client.chat_postMessage.assert_not_called()


@pytest.mark.django_db
class TestSendSlackSourceAlert:
    """Tests for the existing send_slack_alert (source run)."""

    @patch('slack_sdk.WebClient')
    def test_source_run_alert(self, MockWebClient, source, slack_hook, db):
        mock_client = MagicMock()
        MockWebClient.return_value = mock_client

        run_log = SourceRunLog.objects.create(source=source, status='success', triggered_by='manual')

        from etl.hooks.slack.slack_alerts import send_slack_alert
        send_slack_alert(source, run_log)

        mock_client.chat_postMessage.assert_called_once()
        text = mock_client.chat_postMessage.call_args[1]['text']
        assert 'success' in text
        assert source.name in text


@pytest.mark.django_db
class TestSendSlackDestAlert:
    """Tests for send_slack_dest_alert."""

    @patch('slack_sdk.WebClient')
    def test_dest_alert_success(self, MockWebClient, org, slack_hook, db):
        from catalog.models import IntegrationDestination
        mock_client = MagicMock()
        MockWebClient.return_value = mock_client

        dest = IntegrationDestination.objects.create(
            organization=org, name='BQ Dest', destination_type='bigquery',
            bq_dataset_id='my_dataset',
        )

        from etl.hooks.slack.slack_alerts import send_slack_dest_alert
        send_slack_dest_alert(dest, 'success', 42)

        mock_client.chat_postMessage.assert_called_once()
        text = mock_client.chat_postMessage.call_args[1]['text']
        assert 'success' in text
        assert '42s' in text

    @patch('slack_sdk.WebClient')
    def test_dest_alert_failure(self, MockWebClient, org, slack_hook, db):
        from catalog.models import IntegrationDestination
        mock_client = MagicMock()
        MockWebClient.return_value = mock_client

        dest = IntegrationDestination.objects.create(
            organization=org, name='BQ Dest', destination_type='bigquery',
        )

        from etl.hooks.slack.slack_alerts import send_slack_dest_alert
        send_slack_dest_alert(dest, 'failed', None)

        mock_client.chat_postMessage.assert_called_once()
        text = mock_client.chat_postMessage.call_args[1]['text']
        assert 'failed' in text
