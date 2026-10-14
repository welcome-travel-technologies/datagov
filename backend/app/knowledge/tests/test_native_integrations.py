import hashlib
import json
import shutil

import httpx
import pytest
from django.utils import timezone

from catalog.models import IntegrationSource, SourceRunLog
from knowledge.jobs import JobConflict, enqueue
from knowledge.models import Document, DocumentVersion, ExtractionArtifact, IngestionRun, KnowledgeSource
from knowledge.pipeline import capture_extraction, cancel_indexing_output, dispatch_pending
from knowledge.retrieval import EvidenceContext, REVOKED_ANSWER
from knowledge.tests.test_document_explorer import setup, publish, citation, claim, completion

pytestmark = pytest.mark.django_db


def connection(setup):
    return IntegrationSource.objects.create(organization=setup['org'], name='dbt connection', source_type='dbt',
        github_repo_url='https://github.com/example/project', github_branch='main', github_token='must-not-leak')


def test_grouped_content_respects_acl_and_never_exposes_credentials(setup):
    provider = connection(setup)
    setup['source'].integration = provider
    setup['source'].save()
    publish(setup)
    private = KnowledgeSource.objects.create(organization=setup['org'], name='Hidden collection', kind='upload')
    private.allowed_users.add(setup['admin'])
    setup['client'].force_authenticate(setup['user'])
    response = setup['client'].get('/api/knowledge/groups/')
    assert response.status_code == 200
    assert response.data['can_manage'] is False
    assert len(response.data['results']) == 1
    group = response.data['results'][0]
    assert group['name'] == provider.name and group['document_count'] == 1
    assert 'must-not-leak' not in json.dumps(response.data, default=str)
    assert 'allowed_user_ids' not in json.dumps(response.data, default=str)
    assert setup['client'].get('/api/knowledge/sources/').status_code == 403
    assert setup['client'].get('/api/knowledge/connections/').status_code == 403
    assert setup['client'].get(f'/api/knowledge/documents/?group=integration:{provider.id}').data['count'] == 1
    setup['source'].allowed_users.remove(setup['user'])
    assert setup['client'].get('/api/knowledge/groups/').data['results'] == []


def test_connection_validation_and_refresh_pause_do_not_change_access(setup):
    foreign = IntegrationSource.objects.create(organization=setup['other_org'])
    url = f"/api/knowledge/sources/{setup['source'].id}/"
    assert setup['client'].patch(url, {'integration_id': foreign.id}, format='json').status_code == 400
    version, _, _ = publish(setup)
    assert setup['client'].patch(url, {'refresh_paused': True}, format='json').status_code == 200
    assert setup['client'].get(f'/api/knowledge/documents/{version.document_id}/').status_code == 200
    with pytest.raises(JobConflict):
        enqueue(setup['source'])
    # Existing inactive sources stay inaccessible even when refresh is resumed.
    setup['client'].patch(url, {'is_active': False, 'refresh_paused': False}, format='json')
    assert setup['client'].get(f'/api/knowledge/documents/{version.document_id}/').status_code == 404


def test_capture_survives_cleanup_and_dispatch_is_idempotent(setup, settings, tmp_path):
    settings.DOCUMENT_INDEXING_PIPELINE_ENABLED = True
    provider = connection(setup)
    source = setup['source']
    source.integration, source.pipeline_enabled = provider, True
    source.save()
    root = tmp_path / 'etl'
    model = root / 'dbt_artifacts/repo/models/orders.sql'
    model.parent.mkdir(parents=True)
    model.write_text('select order_id from orders', encoding='utf-8')
    # Git internals / provider credentials must never be indexed.
    (model.parent.parent / '.env').write_text('TOKEN=private')
    log = SourceRunLog.objects.create(source=provider)
    artifact = capture_extraction(provider, log, root)
    assert capture_extraction(provider, log, root).pk == artifact.pk
    shutil.rmtree(root)
    assert dispatch_pending() == 1
    assert dispatch_pending() == 0
    assert IngestionRun.objects.count() == 1
    doc = Document.objects.get(source=source)
    assert doc.external_id == 'models/orders.sql'
    assert doc.source_url == 'https://github.com/example/project/blob/main/models/orders.sql'
    job = claim(setup)
    download = setup['worker'].get(job['documents'][0]['download_url'])
    assert b''.join(download.streaming_content) == b'select order_id from orders'
    assert setup['worker'].post(f"/api/knowledge/internal/jobs/{job['id']}/complete/", completion(job), format='json').status_code == 200
    # Empty/incremental captures never retire previously published documents.
    artifact2 = capture_extraction(provider, SourceRunLog.objects.create(source=provider), root)
    assert dispatch_pending() == 1
    doc.refresh_from_db()
    assert doc.deleted_at is None and doc.current_version_id
    assert artifact2.manifest['complete'] is False


def test_busy_pipeline_retains_durable_intent_and_disabled_pipeline_is_noop(setup, settings, tmp_path):
    provider = connection(setup)
    log = SourceRunLog.objects.create(source=provider)
    assert capture_extraction(provider, log, tmp_path) is None
    settings.DOCUMENT_INDEXING_PIPELINE_ENABLED = True
    source = setup['source']
    source.integration, source.pipeline_enabled = provider, True
    source.save()
    existing = IngestionRun.objects.create(source=source, idempotency_key='busy')
    artifact = capture_extraction(provider, log, tmp_path)
    assert dispatch_pending() == 0
    artifact.refresh_from_db()
    assert artifact.run_id is None
    existing.status = 'succeeded'
    existing.save()
    assert dispatch_pending() == 1


def test_native_search_binds_scope_and_revalidates_all_evidence(setup, settings, monkeypatch):
    settings.DOCUMENT_NATIVE_AI_ENABLED = True
    version, _, _ = publish(setup)
    evidence = EvidenceContext(setup['org'], setup['user'])
    calls = []
    def post(client, url, **kwargs):
        calls.append((url, kwargs['json']))
        return httpx.Response(200, json={'citations': [{**citation(version), 'excerpt': 'FORGED', 'source_url': 'https://evil.test'}]})
    monkeypatch.setattr(httpx.Client, 'post', post)
    result = evidence.search_evidence('refund rules')
    assert calls[0][0].endswith('/v1/search')
    assert calls[0][1]['allowed_versions'] == [str(version.id)]
    assert result['citations'][0]['citation_id'] == 'E1'
    assert result['citations'][0]['excerpt'] != 'FORGED'
    setup['source'].allowed_users.remove(setup['user'])
    assert evidence.finalize('Protected paraphrase') == (REVOKED_ANSWER, [])
    assert evidence.remember(result['citations']) == []


def test_native_search_rejects_extra_versions_and_private_provider_errors(setup, settings, monkeypatch):
    settings.DOCUMENT_NATIVE_AI_ENABLED = True
    version, _, _ = publish(setup)
    evidence = EvidenceContext(setup['org'], setup['user'])
    monkeypatch.setattr(httpx.Client, 'post', lambda *a, **k: httpx.Response(200, json={
        'citations': [{**citation(version), 'version_id': 'other-tenant'}]}))
    assert evidence.search_evidence('test')['citations'] == []
    monkeypatch.setattr(httpx.Client, 'post', lambda *a, **k: httpx.Response(503, text='PRIVATE TOKEN'))
    assert 'PRIVATE' not in str(evidence.search_evidence('test'))


def test_explicit_upstream_cancellation_stops_pending_output(setup, settings, tmp_path):
    settings.DOCUMENT_INDEXING_PIPELINE_ENABLED = True
    provider = connection(setup)
    source = setup['source']
    source.integration, source.pipeline_enabled = provider, True
    source.save()
    log = SourceRunLog.objects.create(source=provider)
    artifact = capture_extraction(provider, log, tmp_path)
    cancel_indexing_output(log)
    assert dispatch_pending() == 0
    artifact.refresh_from_db()
    assert artifact.cancelled_at is not None and artifact.run_id is None
    # A catalog load failure is distinct and does not lose successful extraction.
    log2 = SourceRunLog.objects.create(source=provider, status='failed')
    capture_extraction(provider, log2, tmp_path)
    assert dispatch_pending() == 1


def test_new_extraction_can_revert_content_but_old_retry_cannot(setup, settings, tmp_path):
    settings.DOCUMENT_INDEXING_PIPELINE_ENABLED = True
    provider, source = connection(setup), setup['source']
    source.integration, source.pipeline_enabled = provider, True
    source.save()
    model = tmp_path / 'dbt_artifacts/repo/models/orders.sql'
    model.parent.mkdir(parents=True)
    jobs = []
    for text in ['select first_value', 'select second_value', 'select first_value']:
        model.write_text(text, encoding='utf-8')
        capture_extraction(provider, SourceRunLog.objects.create(source=provider), tmp_path)
        assert dispatch_pending() == 1
        job = claim(setup)
        assert setup['worker'].post(f"/api/knowledge/internal/jobs/{job['id']}/complete/", completion(job, text), format='json').status_code == 200
        jobs.append(job)
    doc = Document.objects.get(source=source)
    assert str(doc.current_version_id) == jobs[0]['documents'][0]['version_id']
    latest_artifact_id = doc.current_artifact_id
    response = setup['client'].post(f"/api/knowledge/runs/{jobs[1]['id']}/retry/", {}, format='json')
    assert response.status_code == 202
    job = claim(setup)
    assert setup['worker'].post(f"/api/knowledge/internal/jobs/{job['id']}/complete/", completion(job, 'select second_value'), format='json').status_code == 200
    doc.refresh_from_db()
    assert str(doc.current_version_id) == jobs[0]['documents'][0]['version_id']
    assert doc.current_artifact_id == latest_artifact_id


def test_mapping_preserves_ids_acl_and_schedule_and_rejects_foreign_sources(setup, tmp_path):
    from django.core.management import call_command
    from django.core.management.base import CommandError
    source = setup['source']
    version, _, _ = publish(setup)
    mapping = tmp_path / 'mapping.json'
    mapping.write_text(json.dumps({'organization_id': setup['org'].pk, 'collections': [
        {'id': str(source.pk), 'import_group': 'Reviewed company import', 'integration_id': None}]}))
    call_command('map_document_collections', mapping_file=str(mapping), apply=True)
    source.refresh_from_db()
    assert source.import_group == 'Reviewed company import'
    assert source.allowed_users.filter(pk=setup['user'].pk).exists()
    assert Document.objects.get(pk=version.document_id).current_version_id == version.pk
    mapping.write_text(json.dumps({'organization_id': setup['other_org'].pk, 'collections': [
        {'id': str(source.pk), 'import_group': 'Wrong tenant'}]}))
    with pytest.raises(CommandError):
        call_command('map_document_collections', mapping_file=str(mapping), apply=True)


def test_large_extraction_delivers_bounded_batches_without_refetch_or_duplicates(setup, settings, tmp_path, monkeypatch):
    from knowledge import pipeline
    settings.DOCUMENT_INDEXING_PIPELINE_ENABLED = True
    monkeypatch.setattr(pipeline, 'MAX_BATCH_FILES', 2)
    provider, source = connection(setup), setup['source']
    source.integration, source.pipeline_enabled = provider, True
    source.save()
    root = tmp_path / 'dbt_artifacts/repo/models'
    root.mkdir(parents=True)
    for index in range(5):
        (root / f'model{index}.sql').write_text(f'select {index}', encoding='utf-8')
    log = SourceRunLog.objects.create(source=provider)
    first = capture_extraction(provider, log, tmp_path)
    assert capture_extraction(provider, log, tmp_path).pk == first.pk
    assert ExtractionArtifact.objects.count() == 3
    shutil.rmtree(root)
    for _ in range(3):
        assert dispatch_pending() == 1
        job = claim(setup)
        assert len(job['documents']) <= 2
        assert setup['worker'].post(f"/api/knowledge/internal/jobs/{job['id']}/complete/", completion(job), format='json').status_code == 200
    assert dispatch_pending() == 0
    assert Document.objects.filter(source=source, current_version__isnull=False).count() == 5
