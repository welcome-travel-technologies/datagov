"""
Tests for integration_tasks.py — run_source_task and run_destination_task.
"""
import pytest
from unittest.mock import patch, MagicMock
from catalog.models import (
    IntegrationSource, IntegrationDestination, SourceRunLog, DestinationRunLog,
    Organization,
)


@pytest.mark.django_db
class TestRunSourceTask:
    """Tests for run_source_task."""

    @patch('catalog.integration_tasks._cleanup_local_files')
    @patch('catalog.integration_tasks._cleanup_old_run_logs')
    @patch('etl.hooks.slack.slack_alerts.send_slack_alert')
    @patch('etl.sources.fabric.extract_fabric.run_fabric_extraction')
    @patch('django.core.management.call_command')
    def test_passes_organization_id(self, mock_call_cmd, mock_extract, mock_slack,
                                     mock_cleanup_logs, mock_cleanup_files, source):
        """run_source_task passes organization_id to call_command('load_data', ...)."""
        from catalog.integration_tasks import run_source_task

        result = run_source_task(source.id, triggered_by='test')
        assert result == 'success'

        # Verify call_command was called with organization_id
        mock_call_cmd.assert_called_once()
        call_kwargs = mock_call_cmd.call_args[1]
        assert call_kwargs['organization_id'] == source.organization_id

    @patch('catalog.integration_tasks._cleanup_local_files')
    @patch('catalog.integration_tasks._cleanup_old_run_logs')
    @patch('etl.hooks.slack.slack_alerts.send_slack_alert')
    @patch('etl.sources.fabric.extract_fabric.run_fabric_extraction', side_effect=Exception('API fail'))
    def test_failed_source_sends_slack(self, mock_extract, mock_slack,
                                        mock_cleanup_logs, mock_cleanup_files, source):
        """Even on failure, Slack alert is sent (it's in the finally block)."""
        from catalog.integration_tasks import run_source_task

        result = run_source_task(source.id, triggered_by='test')
        assert result == 'failed'
        mock_slack.assert_called_once()

    def test_missing_source_returns_failed(self, db):
        from catalog.integration_tasks import run_source_task
        result = run_source_task(99999, triggered_by='test')
        assert result == 'failed'


@pytest.mark.django_db
class TestRunDestinationTask:
    """Tests for run_destination_task."""

    @patch('etl.hooks.slack.slack_alerts.send_slack_dest_alert')
    @patch('etl.destinations.bigquery.push_to_bigquery.push_to_bigquery')
    def test_slack_fires_on_success(self, mock_push, mock_slack_dest, org):
        """Slack dest alert fires in finally on success."""
        mock_push.return_value = {'status': 'success', 'duration': 10}

        dest = IntegrationDestination.objects.create(
            organization=org, name='BQ', destination_type='bigquery',
        )

        from catalog.integration_tasks import run_destination_task
        result = run_destination_task(dest.id, triggered_by='test')
        assert result == 'success'
        mock_slack_dest.assert_called_once()
        # Status should be 'success'
        assert mock_slack_dest.call_args[0][1] == 'success'

    @patch('etl.hooks.slack.slack_alerts.send_slack_dest_alert')
    @patch('etl.destinations.bigquery.push_to_bigquery.push_to_bigquery', side_effect=Exception('BQ fail'))
    def test_slack_fires_on_failure(self, mock_push, mock_slack_dest, org):
        """Slack dest alert fires in finally even on failure."""
        dest = IntegrationDestination.objects.create(
            organization=org, name='BQ', destination_type='bigquery',
        )

        from catalog.integration_tasks import run_destination_task
        result = run_destination_task(dest.id, triggered_by='test')
        assert result == 'failed'
        mock_slack_dest.assert_called_once()
        assert mock_slack_dest.call_args[0][1] == 'failed'

    def test_missing_destination_returns_failed(self, db):
        from catalog.integration_tasks import run_destination_task
        result = run_destination_task(99999, triggered_by='test')
        assert result == 'failed'
