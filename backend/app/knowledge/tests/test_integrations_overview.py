"""Integrations overview: one plain-words row per source, how and when it runs, and in what order."""
import json
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_q.models import Schedule
from rest_framework.test import APIClient

from catalog.models import (DestinationSchedule, IntegrationDestination, IntegrationSource, Item, SourceRunLog, SourceSchedule,
                            WorkflowRun, WorkflowSchedule)
from knowledge.models import Document, IngestionRun, KnowledgeSource
from knowledge.overview import MAX_EVENTS, describe_cron
from knowledge.scheduling import next_occurrence
from knowledge.tests.test_document_explorer import setup  # shared organization/ACL fixture

pytestmark = pytest.mark.django_db
URL = '/api/knowledge/integrations/overview/'


def connector(setup, name='Warehouse models', source_type='dbt', **fields):
    credentials = ({'github_repo_url': 'https://github.com/example/project', 'github_token': 'must-not-leak'} if source_type == 'dbt'
                   else {'tenant_id': 'tenant', 'client_id': 'client', 'client_secret': 'must-not-leak'})
    return IntegrationSource.objects.create(organization=setup['org'], name=name, source_type=source_type,
                                            category=IntegrationSource.DEFAULT_CATEGORY_BY_TYPE[source_type], **{**credentials, **fields})


def part(setup, name, group='', kind='snapshot', documents=0, **fields):
    source = KnowledgeSource.objects.create(organization=setup['org'], name=name, kind=kind, import_group=group,
                                            config={'path': f'{name}/manifest.json'} if kind == 'snapshot' else {}, **fields)
    Document.objects.bulk_create([Document(source=source, external_id=f'{name}:{n}', title=f'{name} {n}', mime_type='text/plain')
                                  for n in range(documents)])
    return source


def q_schedule(name, cron, next_run):
    schedule = Schedule.objects.create(name=name, func='catalog.integration_tasks.run_source_task', schedule_type=Schedule.CRON, cron=cron)
    # django-q computes next_run from the cron on create; pin it like a live cluster would have.
    Schedule.objects.filter(pk=schedule.pk).update(next_run=next_run)
    return schedule


def workflow_schedule(setup, cron, next_run):
    WorkflowSchedule.objects.create(organization=setup['org'], frequency='custom', cron_expression=cron, is_enabled=True)
    q_schedule(f"workflow_run_{setup['org'].pk}", cron, next_run)


def overview(client):
    response = client.get(URL)
    assert response.status_code == 200, response.data
    return response.data


def row(data, key):
    return next(item for item in data['sources'] if item['key'] == key)


def blocks(timeline):
    """Run-order events grouped by moment, as the strip shows them."""
    grouped = {}
    for event in timeline:
        grouped.setdefault(event['at'], []).append(event['label'])
    return list(grouped.values())


@pytest.mark.parametrize('cron,text', [
    ('0 2 * * *', 'Every day at 02:00 (UTC)'),
    ('30 6 * * 1', 'Every Monday at 06:30 (UTC)'),
    ('0 7 * * 0', 'Every Sunday at 07:00 (UTC)'),
    ('15 8 * * 1-5', 'Every weekday at 08:15 (UTC)'),
    ('0 4 1 * *', 'On day 1 of every month at 04:00 (UTC)'),
    ('0 * * * *', 'Every hour'),
    ('*/15 * * * *', 'Every 15 minutes'),
    ('0 */6 * * *', 'Every 6 hours'),
    ('0 2 * 1 *', 'On a custom schedule (UTC)'),
    ('', 'On a custom schedule'),
])
def test_schedules_are_described_in_plain_words(cron, text):
    assert describe_cron(cron, 'UTC') == text


def test_only_organization_admins_see_their_own_sources(setup):
    connector(setup)
    assert APIClient().get(URL).status_code in (401, 403)
    setup['client'].force_authenticate(setup['user'])
    assert setup['client'].get(URL).status_code == 403
    # An admin of another organization sees none of this organization's sources.
    setup['client'].force_authenticate(setup['outsider'])
    assert overview(setup['client'])['sources'] == []


def test_connectors_still_show_when_documents_are_off(setup, settings):
    settings.DOCUMENT_EXPLORER_ENABLED = False
    provider = connector(setup)
    Item.objects.create(item_id='overview-model', item_name='orders', organization=setup['org'], integration_source=provider)
    data = overview(setup['client'])
    assert data['documents_enabled'] is False
    assert [item['key'] for item in data['sources']] == [f'integration:{provider.pk}']
    source = data['sources'][0]
    assert source['counts'] == {'documents': None, 'visible_documents': None, 'parts': None, 'catalog_items': 1}
    assert source['how'] == {'mode': 'manual', 'text': 'Manual only', 'next_run_at': None}
    assert source['detail']['steps'] == ['Fetch the dbt project from GitHub', 'Load into the catalog']
    # Manual-only sources have no place in the run order, but can still be run by hand.
    assert source['order'] is None and source['can_run_now'] is True and source['run_blocked'] == ''
    assert 'must-not-leak' not in json.dumps(data, default=str)


def test_many_snapshot_collections_fold_into_one_row_per_group(setup):
    provider = connector(setup)
    for n in range(40):
        part(setup, f'confluence-shard-{n:03}', 'Confluence', documents=2)
    for n in range(25):
        part(setup, f'jira-shard-{n:03}', 'Jira', documents=1, access='organization')
    # A Confluence-looking name without a reviewed label is never guessed into the group.
    part(setup, 'Confluence export 999', documents=3)
    part(setup, 'dbt definitions', kind='upload', documents=4, integration=provider)
    data = overview(setup['client'])
    assert sorted(item['key'] for item in data['sources']) == sorted(
        [f'integration:{provider.pk}', 'import:Confluence', 'import:Jira', 'imports', 'uploads'])
    confluence = row(data, 'import:Confluence')
    assert confluence['name'] == 'Confluence' and confluence['kind'] == 'import'
    # Restricted parts nobody listed this admin on: counted, but none of them readable.
    assert confluence['counts'] == {'documents': 80, 'visible_documents': 0, 'parts': 40, 'catalog_items': None}
    assert confluence['how'] == {'mode': 'imported', 'text': 'Imported export · not synced live', 'next_run_at': None}
    assert 'refreshed only when a new export is imported' in confluence['detail']['updates']
    assert confluence['order'] is None and confluence['can_run_now'] is False
    assert row(data, 'import:Jira')['counts'] == {'documents': 25, 'visible_documents': 25, 'parts': 25, 'catalog_items': None}
    assert row(data, 'imports')['counts'] == {'documents': 3, 'visible_documents': 0, 'parts': 1, 'catalog_items': None}
    assert row(data, f'integration:{provider.pk}')['counts'] == {'documents': 4, 'visible_documents': 0, 'parts': 1, 'catalog_items': 0}
    uploads = row(data, 'uploads')
    assert uploads['name'] == 'Uploaded documents' and uploads['how']['text'] == 'When someone uploads a file'
    # Parts are counted, never listed.
    assert 'shard' not in json.dumps(data, default=str) and 'Confluence export 999' not in json.dumps(data, default=str)


def test_document_counts_match_what_the_admin_can_open(setup):
    Document.objects.create(source=setup['source'], external_id='readable', title='Readable', mime_type='text/plain')
    hidden = part(setup, 'Board papers', kind='upload', documents=2)
    part(setup, 'Switched off', kind='upload', documents=1, access='organization', is_active=False)
    uploads = row(overview(setup['client']), 'uploads')
    # Only the collection this admin is a listed reader of opens in "View documents".
    assert uploads['counts'] == {'documents': 4, 'visible_documents': 1, 'parts': 3, 'catalog_items': None}
    hidden.allowed_users.set([setup['admin']])
    assert row(overview(setup['client']), 'uploads')['counts']['visible_documents'] == 3


def test_upload_collections_with_an_import_label_stay_uploads(setup):
    part(setup, 'Team handbook files', group='Team files', kind='upload', documents=2)
    part(setup, 'Confluence space', group='Confluence', documents=1)
    part(setup, 'Confluence attachments', group='Confluence', kind='upload', documents=1)
    data = overview(setup['client'])
    team = row(data, 'import:Team files')
    assert team['kind'] == 'upload' and team['provider_label'] == 'Uploaded files'
    assert team['how']['text'] == 'When someone uploads a file'
    assert team['detail']['steps'] == ['Someone uploads a file', 'Extract its text', 'Make it searchable']
    assert 'export' not in json.dumps(team, default=str).lower()
    # A group that holds an imported export is described as one.
    assert row(data, 'import:Confluence')['kind'] == 'import'


def test_schedules_next_runs_order_and_next_day_timeline(setup):
    now = timezone.now()
    dbt = connector(setup)
    fabric = connector(setup, 'Company reports', 'powerbi_fabric')
    destination = IntegrationDestination.objects.create(organization=setup['org'], name='Warehouse copy', is_active=True)
    workflow_schedule(setup, '0 2 * * *', now + timedelta(hours=3))
    SourceSchedule.objects.create(source=fabric, frequency='weekly', cron_expression='30 6 * * 1', is_enabled=True)
    q_schedule(f'source_run_{fabric.pk}', '30 6 * * 1', now + timedelta(hours=1))
    for n in range(2):
        part(setup, f'jira-{n}', 'Jira', cron='0 6 * * *', timezone='Europe/Athens', next_run_at=now + timedelta(hours=5))
    part(setup, 'confluence-0', 'Confluence')
    data = overview(setup['client'])

    assert data['workflow']['how'] == {'mode': 'scheduled', 'text': 'Every day at 02:00 (UTC)', 'next_run_at': now + timedelta(hours=3)}
    labels = [step['label'] for step in data['workflow']['steps']]
    assert labels == ['Check there is enough disk space', 'Warehouse models (dbt / GitHub)', 'Company reports (Power BI / Fabric)',
                      'Link the tools together and refresh summaries', 'Send to Warehouse copy']
    model_row = row(data, f'integration:{dbt.pk}')
    assert model_row['how']['text'] == 'With the catalog workflow, every day at 02:00 (UTC)'
    assert model_row['workflow_step'] == 2 and 'step 2 of 5' in model_row['detail']['updates']
    report_row = row(data, f'integration:{fabric.pk}')
    assert report_row['how'] == {'mode': 'scheduled', 'next_run_at': now + timedelta(hours=1),
                                 'text': 'Every Monday at 06:30 (UTC), and with the catalog workflow every day at 02:00 (UTC)'}
    jira = row(data, 'import:Jira')
    assert jira['how'] == {'mode': 'scheduled', 'text': 'Re-reads the export every day at 06:00 (Europe/Athens)',
                           'next_run_at': now + timedelta(hours=5)}
    # Order follows the next run: reports (1 h), models with the workflow (3 h), then Jira (5 h).
    assert [(item['key'], item['order']) for item in data['sources'] if item['order']] == [
        (f'integration:{fabric.pk}', 1), (f'integration:{dbt.pk}', 2), ('import:Jira', 3)]
    assert row(data, 'import:Confluence')['order'] is None

    timeline = data['run_order']
    times = [event['at'] for event in timeline]
    assert times == sorted(times) and all(now < at <= now + timedelta(hours=24) for at in times)
    assert timeline[0] == {'at': now + timedelta(hours=1), 'source_key': f'integration:{fabric.pk}', 'label': 'Company reports',
                           'kind': 'source', 'repeat': None}
    workflow = [event for event in timeline if event['kind'] == 'workflow'][:3]
    assert [event['source_key'] for event in workflow] == [f'integration:{dbt.pk}', f'integration:{fabric.pk}', None]
    assert [event['label'] for event in workflow] == ['Warehouse models', 'Company reports', f'Send to {destination.name}']
    assert {event['at'] for event in workflow} == {now + timedelta(hours=3)}
    # Document parts first run at their stored next run, as the "Next run" column says.
    jira_events = [event for event in timeline if event['source_key'] == 'import:Jira']
    assert jira_events[0]['at'] == now + timedelta(hours=5) and jira_events[0]['label'] == 'Update Jira (2 parts)'
    assert data['run_order_truncated'] is False


def test_own_schedule_and_workflow_are_both_named(setup):
    now = timezone.now()
    fabric = connector(setup, 'Company reports', 'powerbi_fabric')
    workflow_schedule(setup, '0 2 * * *', now + timedelta(hours=3))
    SourceSchedule.objects.create(source=fabric, frequency='weekly', cron_expression='30 6 * * 1', is_enabled=True)
    q_schedule(f'source_run_{fabric.pk}', '30 6 * * 1', now + timedelta(days=4))
    how = row(overview(setup['client']), f'integration:{fabric.pk}')['how']
    # The next run comes from the daily workflow, and the row says so.
    assert how == {'mode': 'scheduled', 'next_run_at': now + timedelta(hours=3),
                   'text': 'Every Monday at 06:30 (UTC), and with the catalog workflow every day at 02:00 (UTC)'}


def test_frequent_schedules_show_once_and_runs_are_never_cut_in_half(setup):
    now = timezone.now()
    connector(setup, 'Models', 'dbt')
    connector(setup, 'Reports', 'powerbi_fabric')
    IntegrationDestination.objects.create(organization=setup['org'], name='Warehouse copy', is_active=True)
    workflow_schedule(setup, '0 * * * *', now + timedelta(minutes=30))
    data = overview(setup['client'])
    # An hourly workflow is one entry that repeats, not a day of identical runs.
    assert [(event['label'], event['repeat']) for event in data['run_order']] == [
        ('Models', 'every hour'), ('Reports', 'every hour'), ('Send to Warehouse copy', 'every hour')]
    assert data['run_order_truncated'] is False

    # Four runs of twelve steps exceed the limit; the strip stops between runs, never inside one.
    for n in range(9):
        IntegrationDestination.objects.create(organization=setup['org'], name=f'Copy {n}', is_active=True)
    cron = '0 */6 * * *'
    WorkflowSchedule.objects.filter(organization=setup['org']).update(cron_expression=cron)
    Schedule.objects.filter(name=f"workflow_run_{setup['org'].pk}").update(cron=cron, next_run=next_occurrence(cron, 'UTC'))
    data = overview(setup['client'])
    runs = blocks(data['run_order'])
    assert data['run_order_truncated'] is True and len(data['run_order']) <= MAX_EVENTS
    assert len(runs) == 3 and all(len(run) == 12 and run[-1] == 'Send to Warehouse copy' for run in runs)
    assert all(event['repeat'] is None for event in data['run_order'])


def test_inactive_connectors_and_destinations_stay_out_of_the_run_order(setup):
    now = timezone.now()
    off = connector(setup, 'Old reports', 'powerbi_fabric', is_active=False)
    SourceSchedule.objects.create(source=off, frequency='daily', cron_expression='0 4 * * *', is_enabled=True)
    q_schedule(f'source_run_{off.pk}', '0 4 * * *', now + timedelta(hours=2))
    parked = IntegrationDestination.objects.create(organization=setup['org'], name='Parked copy', is_active=False)
    DestinationSchedule.objects.create(destination=parked, frequency='daily', cron_expression='0 5 * * *', is_enabled=True)
    q_schedule(f'dest_run_{parked.pk}', '0 5 * * *', now + timedelta(hours=2))
    live = IntegrationDestination.objects.create(organization=setup['org'], name='Live copy', is_active=True)
    DestinationSchedule.objects.create(destination=live, frequency='daily', cron_expression='0 5 * * *', is_enabled=True)
    q_schedule(f'dest_run_{live.pk}', '0 5 * * *', now + timedelta(hours=2))
    connector(setup)
    workflow_schedule(setup, '0 2 * * *', now + timedelta(hours=1))
    data = overview(setup['client'])
    labels = {event['label'] for event in data['run_order']}
    assert labels == {'Warehouse models', 'Send to Live copy'}
    assert 'Parked copy' not in json.dumps(data['workflow'], default=str)
    turned_off = row(data, f'integration:{off.pk}')
    assert turned_off['how'] == {'mode': 'manual', 'text': 'Turned off', 'next_run_at': None} and turned_off['order'] is None
    # Its own schedule still fires and fails on every run, so it needs attention even though it is off.
    assert turned_off['needs_attention'] is True and 'still on' in turned_off['attention'] and turned_off['fix_in'] == 'workflow'
    assert turned_off['can_run_now'] is False


def test_duplicate_active_connectors_are_flagged_and_not_runnable(setup):
    first = connector(setup, 'Models A')
    second = connector(setup, 'Models B')
    connector(setup, 'Models C', is_active=False)
    data = overview(setup['client'])
    for source in (row(data, f'integration:{first.pk}'), row(data, f'integration:{second.pk}')):
        assert source['how']['text'] == 'Manual only' and source['order'] is None
        assert source['needs_attention'] is True and source['fix_in'] == 'workflow'
        assert source['can_run_now'] is False
        assert source['run_blocked'] == source['attention'] == (
            'Another active dbt / GitHub connection blocks runs. Turn one of them off first.')


def test_last_run_status_and_needs_attention(setup):
    healthy = connector(setup)
    failing = connector(setup, 'Company reports', 'powerbi_fabric')
    off = connector(setup, 'Old reports', 'powerbi_fabric', is_active=False)
    incomplete = connector(setup, 'Second models', github_repo_url='', is_active=False)
    SourceRunLog.objects.create(source=healthy, status='failed')
    SourceRunLog.objects.create(source=healthy, status='success')
    SourceRunLog.objects.create(source=failing, status='failed')
    jira = [part(setup, f'jira-{n}', 'Jira') for n in range(3)]
    IngestionRun.objects.create(source=jira[0], idempotency_key='done', status='succeeded')
    partial_run = IngestionRun.objects.create(source=jira[1], idempotency_key='some', status='partial')
    IngestionRun.objects.create(source=jira[2], idempotency_key='stop', status='cancelled')
    part(setup, 'confluence-0', 'Confluence')
    IngestionRun.objects.create(source=setup['source'], idempotency_key='upload', status='running')
    data = overview(setup['client'])

    assert row(data, f'integration:{healthy.pk}')['last_run']['status'] == 'success'
    assert row(data, f'integration:{healthy.pk}')['needs_attention'] is False
    failed = row(data, f'integration:{failing.pk}')
    assert failed['last_run']['status'] == 'failed' and failed['needs_attention'] is True
    assert failed['attention'] == 'The last run failed.' and failed['fix_in'] == 'workflow' and failed['can_run_now'] is True
    turned_off = row(data, f'integration:{off.pk}')
    assert turned_off['how']['text'] == 'Turned off' and turned_off['order'] is None
    assert turned_off['can_run_now'] is False and turned_off['needs_attention'] is False and turned_off['fix_in'] is None
    partial = row(data, 'import:Jira')
    assert partial['last_run'] == {'status': 'partial', 'at': partial_run.created_at} and partial['needs_attention'] is True
    assert row(data, 'import:Confluence')['last_run'] == {'status': 'never', 'at': None}
    assert row(data, 'uploads')['last_run']['status'] == 'running'

    # Incomplete connection details are fixed where the connection is configured, not in Workflow.
    IntegrationSource.objects.filter(pk=incomplete.pk).update(is_active=True)
    IntegrationSource.objects.filter(pk=healthy.pk).update(is_active=False)
    missing = row(overview(setup['client']), f'integration:{incomplete.pk}')
    assert missing['last_run'] == {'status': 'never', 'at': None}
    assert missing['attention'] == 'Connection details are incomplete.' and missing['fix_in'] == 'sources'


def test_import_last_run_time_belongs_to_its_status(setup):
    old, fresh = part(setup, 'jira-old', 'Jira'), part(setup, 'jira-new', 'Jira')
    failed = IngestionRun.objects.create(source=old, idempotency_key='old', status='failed')
    IngestionRun.objects.filter(pk=failed.pk).update(created_at=timezone.now() - timedelta(days=90))
    IngestionRun.objects.create(source=fresh, idempotency_key='new', status='succeeded')
    jira = row(overview(setup['client']), 'import:Jira')
    # One part is still failed, so the group is; the time is that failure's, not today's success.
    failed.refresh_from_db()
    assert jira['last_run'] == {'status': 'failed', 'at': failed.created_at}
    assert jira['attention'] == '1 of 2 parts failed to update.' and jira['fix_in'] == 'workflow'


def test_document_schedules_use_the_stored_next_run_and_flag_a_stopped_worker(setup):
    now = timezone.now()
    part(setup, 'jira-0', 'Jira', cron='0 6 * * *', next_run_at=now + timedelta(hours=2))
    part(setup, 'confluence-0', 'Confluence', cron='0 6 * * *', next_run_at=now - timedelta(hours=2))
    part(setup, 'wiki-0', 'Wiki', cron='0 6 * * *')
    data = overview(setup['client'])
    first = next(event for event in data['run_order'] if event['source_key'] == 'import:Jira')
    assert first['at'] == now + timedelta(hours=2) == row(data, 'import:Jira')['how']['next_run_at']
    assert row(data, 'import:Jira')['needs_attention'] is False
    stopped = row(data, 'import:Confluence')
    assert stopped['needs_attention'] is True and 'overdue' in stopped['attention']
    # Nothing is running it, so the strip does not promise a run.
    assert not [event for event in data['run_order'] if event['source_key'] == 'import:Confluence']
    never = row(data, 'import:Wiki')
    assert never['needs_attention'] is True and 'not active' in never['attention'] and never['how']['next_run_at'] is None
    assert never['order'] is None


def test_inactive_or_overdue_schedules_need_attention(setup):
    provider = connector(setup)
    SourceSchedule.objects.create(source=provider, frequency='daily', cron_expression='0 2 * * *', is_enabled=True)
    workflow_schedule(setup, '0 3 * * *', timezone.now() - timedelta(hours=2))
    WorkflowRun.objects.create(organization=setup['org'], status='failed')
    data = overview(setup['client'])
    source = row(data, f'integration:{provider.pk}')
    # No django-q registration: the saved schedule never fires, so it is not shown as scheduled.
    assert source['how']['text'] == 'With the catalog workflow, every day at 03:00 (UTC)'
    assert source['needs_attention'] is True and 'not active' in source['attention'] and source['fix_in'] == 'workflow'
    assert data['workflow']['needs_attention'] is True and 'overdue' in data['workflow']['attention']
    assert data['workflow']['last_run']['status'] == 'failed'
    # An overdue workflow is not promised in the run order.
    assert data['run_order'] == []


def test_query_count_does_not_grow_with_collections(setup):
    provider = connector(setup)
    connector(setup, 'Company reports', 'powerbi_fabric')
    workflow_schedule(setup, '0 2 * * *', timezone.now() + timedelta(hours=1))
    part(setup, 'dbt definitions', kind='upload', documents=2, integration=provider, pipeline_enabled=True)

    def add(count, offset):
        for n in range(offset, offset + count):
            source = part(setup, f'shard-{n}', 'Confluence' if n % 2 else 'Jira', documents=2, access='organization' if n % 4 else 'restricted',
                          cron='0 6 * * *' if n % 3 == 0 else '', timezone='Europe/Athens',
                          next_run_at=timezone.now() + timedelta(hours=n % 7 + 1) if n % 3 == 0 else None)
            IngestionRun.objects.create(source=source, idempotency_key=f'run-{n}', status='succeeded' if n % 5 else 'failed')

    add(3, 0)
    with CaptureQueriesContext(connection) as small:
        overview(setup['client'])
    add(80, 100)
    with CaptureQueriesContext(connection) as large:
        data = overview(setup['client'])
    assert len(small) == len(large) <= 25
    assert row(data, 'import:Confluence')['counts']['parts'] + row(data, 'import:Jira')['counts']['parts'] == 83
