"""Lifecycle regressions found by independent Document Explorer review."""
import hashlib
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
import pytest

from knowledge.jobs import enqueue, reconcile_expired
from knowledge.models import Document, DocumentVersion, IngestionRun
from knowledge import views
from .test_document_explorer import setup, claim, completion, upload

pytestmark = pytest.mark.django_db


def _snapshot_source(setup):
    source = setup['source']
    source.kind, source.config = 'snapshot', {'path': 'snapshot.json'}
    source.save()
    return source


def _register(setup, job, external_id, text):
    return setup['worker'].post(f"/api/knowledge/internal/jobs/{job['id']}/documents/", {
        'external_id': external_id, 'title': external_id, 'mime_type': 'text/plain',
        'source_type': 'snapshot', 'source_url': '', 'content_hash': hashlib.sha256(text.encode()).hexdigest(),
    }, format='json')


def _snapshot(setup, documents):
    enqueue(setup['source'])
    job = claim(setup)
    results = []
    for external_id, text in documents.items():
        response = _register(setup, job, external_id, text)
        assert response.status_code == 200, response.data
        result = completion({'documents': [response.data]}, text)['documents'][0]
        results.append(result)
    response = setup['worker'].post(f"/api/knowledge/internal/jobs/{job['id']}/complete/",
        {'documents': results, 'index_generation': 'regression'}, format='json')
    assert response.status_code == 200, response.data
    return results


def test_upload_idempotency_key_reuse_with_different_content_does_not_create_orphan(setup):
    first = setup['client'].post('/api/knowledge/uploads/', {
        'source_id': str(setup['source'].pk), 'file': SimpleUploadedFile('one.txt', b'first content'),
    }, format='multipart', HTTP_IDEMPOTENCY_KEY='same-upload-key')
    assert first.status_code == 201, first.data
    second = setup['client'].post('/api/knowledge/uploads/', {
        'source_id': str(setup['source'].pk), 'file': SimpleUploadedFile('two.txt', b'different content'),
    }, format='multipart', HTTP_IDEMPOTENCY_KEY='same-upload-key')
    assert second.status_code == 409, second.data
    assert DocumentVersion.objects.count() == 1
    assert Document.objects.count() == 1
    run = IngestionRun.objects.get()
    assert run.versions.count() == 1


def test_snapshot_reverting_to_published_hash_updates_current_version(setup):
    _snapshot_source(setup)
    first = _snapshot(setup, {'guide': 'Version one'})
    _snapshot(setup, {'guide': 'Version two'})
    _snapshot(setup, {'guide': 'Version one'})
    document = Document.objects.get(external_id='guide')
    assert str(document.current_version_id) == first[0]['version_id']
    assert DocumentVersion.objects.count() == 2


def test_snapshot_missing_document_can_reappear_without_resurrecting_manual_deletes(setup):
    _snapshot_source(setup)
    all_documents = {'one': 'One', 'two': 'Two', 'three': 'Three', 'four': 'Four'}
    _snapshot(setup, all_documents)
    _snapshot(setup, {k: v for k, v in all_documents.items() if k != 'four'})
    removed = Document.objects.get(external_id='four')
    assert removed.deleted_at is not None
    _snapshot(setup, all_documents)
    removed.refresh_from_db()
    assert removed.deleted_at is None
    assert removed.current_version_id is not None


def test_reclaimed_snapshot_does_not_require_registrations_from_expired_attempt(setup):
    source = _snapshot_source(setup)
    enqueue(source)
    first = claim(setup)
    assert _register(setup, first, 'vanished-between-attempts', 'Old manifest').status_code == 200
    IngestionRun.objects.filter(pk=first['id']).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    reconcile_expired()
    second = claim(setup)
    response = _register(setup, second, 'current-manifest', 'Current content')
    assert response.status_code == 200
    body = completion({'documents': [response.data]}, 'Current content')
    response = setup['worker'].post(f"/api/knowledge/internal/jobs/{second['id']}/complete/", body, format='json')
    assert response.status_code == 200, response.data
    assert Document.objects.get(external_id='current-manifest').current_version_id is not None
    assert Document.objects.get(external_id='vanished-between-attempts').current_version_id is None


def test_upload_sync_with_no_pending_documents_finishes_without_worker(setup):
    response = setup['client'].post(f"/api/knowledge/sources/{setup['source'].pk}/sync/", {}, format='json')
    assert response.status_code in (200, 202), response.data
    run = IngestionRun.objects.get()
    assert run.status == 'succeeded'
    assert not run.versions.exists()


def test_completion_unknown_large_fields_cannot_bypass_text_budget(setup, settings):
    source = _snapshot_source(setup)
    enqueue(source)
    job = claim(setup)
    response = _register(setup, job, 'oversized-metadata', 'Content')
    assert response.status_code == 200
    body = completion({'documents': [response.data]}, 'Content')
    settings.DOCUMENT_EXPLORER_MAX_TEXT_CHARS = 100
    body['documents'][0]['pages'][0]['unvalidated_blob'] = 'x' * 10000
    response = setup['worker'].post(f"/api/knowledge/internal/jobs/{job['id']}/complete/", body, format='json')
    assert response.status_code == 400, response.data
    assert not DocumentVersion.objects.filter(published_at__isnull=False).exists()


@pytest.mark.parametrize('source_url', ['https://reader:private-password@example.com/page', 'https:missing-host'])
def test_snapshot_registration_rejects_credential_or_nonabsolute_source_url(setup, source_url):
    _snapshot_source(setup)
    enqueue(setup['source'])
    job = claim(setup)
    response = setup['worker'].post(f"/api/knowledge/internal/jobs/{job['id']}/documents/", {
        'external_id': 'unsafe-source-url', 'title': 'Page', 'mime_type': 'text/plain',
        'source_type': 'snapshot', 'source_url': source_url, 'content_hash': 'a' * 64,
    }, format='json')
    assert response.status_code == 400, response.data
    assert not Document.objects.exists()


@pytest.mark.parametrize('action', ['cancel', 'retry'])
def test_run_actions_do_not_mutate_on_get(setup, action):
    _snapshot_source(setup)
    run, _ = enqueue(setup['source'])
    if action == 'retry':
        run.status = 'failed'
        run.save(update_fields=['status'])
    original_status = run.status
    response = setup['client'].get(f'/api/knowledge/runs/{run.pk}/{action}/')
    assert response.status_code == 405, response.data
    run.refresh_from_db()
    assert run.status == original_status
    assert IngestionRun.objects.count() == 1


def test_worker_non_object_json_is_validation_error(setup):
    setup['worker'].raise_request_exception = False
    response = setup['worker'].post('/api/knowledge/internal/jobs/claim/', [], format='json')
    assert response.status_code == 400


def test_failed_snapshot_refresh_does_not_remove_omitted_published_document(setup):
    source = _snapshot_source(setup)
    original = _snapshot(setup, {'one': 'One', 'two': 'Two', 'three': 'Three', 'four': 'Four'})
    old_current = {row['document_id']: row['version_id'] for row in original}
    enqueue(source)
    job = claim(setup)
    results = []
    for external_id in ('one', 'two', 'three'):
        response = _register(setup, job, external_id, 'Updated ' + external_id)
        assert response.status_code == 200
        results.append(completion({'documents': [response.data]}, 'Updated ' + external_id)['documents'][0])
    results[0].update(status='failed', pages=[], chunks=[], error='Extraction failed.')
    response = setup['worker'].post(f"/api/knowledge/internal/jobs/{job['id']}/complete/",
        {'documents': results, 'index_generation': 'partial'}, format='json')
    assert response.status_code == 200, response.data
    omitted = Document.objects.get(external_id='four')
    failed = Document.objects.get(external_id='one')
    assert omitted.deleted_at is None
    assert str(omitted.current_version_id) == old_current[str(omitted.pk)]
    assert str(failed.current_version_id) == old_current[str(failed.pk)]


def test_manual_delete_during_completion_is_never_resurrected(setup, monkeypatch):
    _snapshot_source(setup)
    enqueue(setup['source'])
    job = claim(setup)
    registered = _register(setup, job, 'deleted-during-publication', 'Content')
    assert registered.status_code == 200
    body = completion({'documents': [registered.data]}, 'Content')
    validate = views._validate_result

    def deleted_after_document_was_read(item, version):
        checked = validate(item, version)
        # Simulate a deletion committed after select_related hydrated the old
        # document but before the final publication UPDATE evaluates its guard.
        Document.objects.filter(pk=version.document_id).update(
            deleted_at=timezone.now(), deletion_reason='manual')
        return checked

    monkeypatch.setattr(views, '_validate_result', deleted_after_document_was_read)
    response = setup['worker'].post(f"/api/knowledge/internal/jobs/{job['id']}/complete/", body, format='json')
    assert response.status_code in (200, 409), response.data
    doc = Document.objects.get(pk=registered.data['id'])
    assert doc.deleted_at is not None
    assert doc.deletion_reason == 'manual'
    assert doc.current_version_id is None


def test_retry_of_old_failed_upload_does_not_replace_newer_published_revision(setup):
    first = upload(setup, b'Old content')
    assert first.status_code == 201
    document_id = first.data['document']['id']
    old_job = claim(setup)
    assert setup['worker'].post(f"/api/knowledge/internal/jobs/{old_job['id']}/fail/", {'code': 'EXTRACTION_FAILED'}, format='json').status_code == 200
    new_upload = setup['client'].post('/api/knowledge/uploads/', {
        'source_id': str(setup['source'].pk), 'document_id': document_id,
        'file': SimpleUploadedFile('new.txt', b'New content'),
    }, format='multipart')
    assert new_upload.status_code == 201
    new_job = claim(setup)
    assert setup['worker'].post(f"/api/knowledge/internal/jobs/{new_job['id']}/complete/", completion(new_job, 'New content'), format='json').status_code == 200
    new_version_id = new_job['documents'][0]['version_id']
    retry = setup['client'].post(f"/api/knowledge/runs/{old_job['id']}/retry/", {}, format='json')
    assert retry.status_code == 202
    retry_job = claim(setup)
    assert setup['worker'].post(f"/api/knowledge/internal/jobs/{retry_job['id']}/complete/", completion(retry_job, 'Old content'), format='json').status_code == 200
    assert str(Document.objects.get(pk=document_id).current_version_id) == new_version_id
