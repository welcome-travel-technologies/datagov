"""
Tests for integration_tasks.py — run_source_task and run_destination_task.
"""
from contextlib import contextmanager

import pytest
from unittest.mock import patch, MagicMock
from catalog.models import (
    IntegrationSource, IntegrationDestination, SourceRunLog, DestinationRunLog,
    Organization, WorkflowRun,
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

    def test_inactive_source_is_rejected_before_extraction(
            self, source, monkeypatch):
        from catalog import integration_tasks

        source.is_active = False
        source.save(update_fields=['is_active'])
        events = []

        monkeypatch.setattr(
            'etl.sources.registry.get_source',
            lambda _source: events.append('get-source'),
        )
        monkeypatch.setattr(
            integration_tasks,
            '_cleanup_local_files',
            lambda log: events.append('cleanup'),
        )
        monkeypatch.setattr(
            integration_tasks, '_cleanup_old_run_logs',
            lambda *args: None,
        )
        monkeypatch.setattr(
            'etl.hooks.slack.slack_alerts.send_slack_alert',
            lambda *args: None,
        )

        assert integration_tasks.run_source_task(
            source.id, triggered_by='test',
        ) == 'failed'
        assert events == []
        assert 'inactive' in SourceRunLog.objects.get(
            source=source,
        ).log_output

    def test_pipeline_lock_wraps_extract_load_and_cleanup(
            self, source, monkeypatch):
        from catalog import integration_tasks

        events = []

        @contextmanager
        def fake_lock():
            events.append('lock-enter')
            try:
                yield
            finally:
                events.append('lock-exit')

        class FakeSource:
            load_command = 'load_data'

            @classmethod
            def get_etl_dir(cls):
                return 'shared-etl-dir'

            def extract(self, *, etl_dir, log):
                assert etl_dir == 'shared-etl-dir'
                events.append('extract')

        monkeypatch.setattr(
            'etl.sources.registry.get_source',
            lambda _source: FakeSource(),
        )
        monkeypatch.setattr(
            'catalog.services.load_scope.require_exact_load_scope',
            lambda *args, **kwargs: events.append('scope'),
        )
        monkeypatch.setattr(
            integration_tasks, 'static_etl_files_lock', fake_lock,
        )
        monkeypatch.setattr(
            integration_tasks,
            'call_command',
            lambda *args, **kwargs: events.append('load'),
        )
        monkeypatch.setattr(
            integration_tasks,
            '_cleanup_local_files',
            lambda log: events.append('cleanup'),
        )
        monkeypatch.setattr(
            integration_tasks, '_cleanup_old_run_logs',
            lambda *args: None,
        )
        monkeypatch.setattr(
            'etl.hooks.slack.slack_alerts.send_slack_alert',
            lambda *args: None,
        )

        assert integration_tasks.run_source_task(
            source.id, triggered_by='test',
        ) == 'success'
        assert events == [
            'scope', 'lock-enter', 'cleanup', 'extract', 'load', 'cleanup',
            'lock-exit',
        ]

    def test_pipeline_contention_does_not_extract_or_cleanup(
            self, source, monkeypatch):
        from catalog import integration_tasks
        from catalog.services.pipeline_lock import PipelineLockUnavailable

        events = []

        @contextmanager
        def contended_lock():
            raise PipelineLockUnavailable('pipeline busy')
            yield

        class FakeSource:
            load_command = 'load_data'

            @classmethod
            def get_etl_dir(cls):
                return 'shared-etl-dir'

            def extract(self, *, etl_dir, log):
                events.append('extract')

        monkeypatch.setattr(
            'etl.sources.registry.get_source',
            lambda _source: FakeSource(),
        )
        monkeypatch.setattr(
            integration_tasks, 'static_etl_files_lock', contended_lock,
        )
        monkeypatch.setattr(
            integration_tasks,
            'call_command',
            lambda *args, **kwargs: events.append('load'),
        )
        monkeypatch.setattr(
            integration_tasks,
            '_cleanup_local_files',
            lambda log: events.append('cleanup'),
        )
        monkeypatch.setattr(
            integration_tasks, '_cleanup_old_run_logs',
            lambda *args: None,
        )
        monkeypatch.setattr(
            'etl.hooks.slack.slack_alerts.send_slack_alert',
            lambda *args: None,
        )

        assert integration_tasks.run_source_task(
            source.id, triggered_by='test',
        ) == 'failed'
        assert events == []
        assert 'pipeline busy' in SourceRunLog.objects.get(
            source=source,
        ).log_output


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


@pytest.mark.django_db
def test_workflow_pipeline_lock_wraps_each_source_files(
        source, monkeypatch):
    from catalog import integration_tasks

    events = []

    @contextmanager
    def fake_lock():
        events.append('lock-enter')
        try:
            yield
        finally:
            events.append('lock-exit')

    class FakeSource:
        load_command = 'load_data'

        @classmethod
        def get_etl_dir(cls):
            return 'shared-etl-dir'

        def extract(self, *, etl_dir, log):
            assert etl_dir == 'shared-etl-dir'
            events.append('extract')

    def fake_call_command(command, *args, **kwargs):
        events.append('final' if command == 'run_workflow_final' else 'load')

    monkeypatch.setattr(
        'etl.sources.registry.get_source',
        lambda _source: FakeSource(),
    )
    monkeypatch.setattr(
        'catalog.services.load_scope.require_exact_load_scope',
        lambda *args, **kwargs: events.append('scope'),
    )
    monkeypatch.setattr(
        integration_tasks, 'static_etl_files_lock', fake_lock,
    )
    monkeypatch.setattr(
        integration_tasks, 'call_command', fake_call_command,
    )
    monkeypatch.setattr(
        integration_tasks,
        '_cleanup_local_files',
        lambda log: events.append('cleanup'),
    )
    monkeypatch.setattr(
        'catalog.health.check_disk',
        lambda: {'status': 'ok', 'detail': 'enough space'},
    )
    monkeypatch.setattr(
        'etl.hooks.slack.slack_alerts.send_slack_alert',
        lambda *args: None,
    )
    monkeypatch.setattr(
        'etl.hooks.slack.slack_alerts.send_slack_dest_alert',
        lambda *args: None,
    )

    workflow = WorkflowRun.objects.create(
        organization=source.organization,
        triggered_by='test',
    )

    assert integration_tasks.run_workflow_task(
        workflow.id, triggered_by='test',
    ) == 'success'
    assert events == [
        'scope', 'lock-enter', 'cleanup', 'extract', 'load', 'cleanup',
        'lock-exit', 'final',
    ]
