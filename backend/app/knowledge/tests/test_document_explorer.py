import hashlib
import uuid
from datetime import timedelta

import pytest
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APIClient

from catalog.models import CustomUser, Organization, OrganizationMembership
from knowledge.jobs import JobConflict, enqueue, reconcile_expired
from knowledge.models import Document, DocumentVersion, IngestionRun, KnowledgeSource
from knowledge.permissions import filter_citations
from knowledge.scheduling import tick

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup(settings, tmp_path):
    settings.DOCUMENT_EXPLORER_ENABLED = True
    settings.DOCUMENT_EXPLORER_WORKER_TOKEN = 'test-worker-key-with-no-real-service-access'
    settings.DOCUMENT_EXPLORER_API_KEY = 'test-engine-key-with-no-real-service-access'
    settings.DOCUMENT_EXPLORER_API_URL = 'http://document-api:8788'
    settings.DOCUMENT_EXPLORER_STORAGE_ROOT = tmp_path / 'private'
    org = Organization.objects.create(name='Documents test organization')
    other_org = Organization.objects.create(name='Another organization')
    admin = CustomUser.objects.create_user(username='document-admin', email='admin@documents.test')
    user = CustomUser.objects.create_user(username='document-reader', email='reader@documents.test')
    outsider = CustomUser.objects.create_user(username='document-outsider', email='outsider@documents.test')
    company, _ = Group.objects.get_or_create(name='Company')
    for u in [admin, user, outsider]:
        u.groups.add(company)
    OrganizationMembership.objects.create(organization=org, user=admin, is_admin=True)
    OrganizationMembership.objects.create(organization=org, user=user)
    OrganizationMembership.objects.create(organization=other_org, user=outsider, is_admin=True)
    source = KnowledgeSource.objects.create(organization=org, name='Private uploads', kind='upload', access='restricted')
    source.allowed_users.set([admin, user])
    client, worker = APIClient(), APIClient()
    client.force_authenticate(admin)
    worker.credentials(HTTP_AUTHORIZATION=f'Bearer {settings.DOCUMENT_EXPLORER_WORKER_TOKEN}')
    return {'org': org, 'other_org': other_org, 'admin': admin, 'user': user, 'outsider': outsider, 'source': source, 'client': client, 'worker': worker}


def upload(setup, data=b'A source document about refunds.', filename='Guide.txt'):
    return setup['client'].post('/api/knowledge/uploads/', {'source_id': str(setup['source'].id), 'file': SimpleUploadedFile(filename, data)}, format='multipart')


def claim(setup):
    response = setup['worker'].post('/api/knowledge/internal/jobs/claim/', {'worker_id': 'fixture-worker'}, format='json')
    assert response.status_code == 200, response.data
    job = response.data['job']
    assert job
    # Engine claim schemas require strings for absent native source URLs.
    assert all(isinstance(document['source_url'], str) for document in job['documents'])
    setup['worker'].credentials(HTTP_AUTHORIZATION='Bearer test-worker-key-with-no-real-service-access', HTTP_X_LEASE_TOKEN=job['lease_token'])
    return job


def completion(job, text='A source document about refunds.'):
    return {'index_generation': 'fixture-v1', 'documents': [
        {'document_id': d['id'], 'version_id': d['version_id'], 'status': 'ready',
         'pages': [{'page': 1, 'text': text}],
         'chunks': [{'id': f"{d['version_id']}:1", 'page': 1, 'text': text, 'section': 'Refunds'}], 'error': ''}
        for d in job['documents']]}


def publish(setup):
    response = upload(setup)
    assert response.status_code == 201, response.data
    job = claim(setup)
    body = completion(job)
    response = setup['worker'].post(f"/api/knowledge/internal/jobs/{job['id']}/complete/", body, format='json')
    assert response.status_code == 200, response.data
    return DocumentVersion.objects.get(id=job['documents'][0]['version_id']), job, body


def citation(version):
    return {'document_id': str(version.document_id), 'version_id': str(version.id), 'chunk_id': version.chunks[0]['id']}


def test_upload_claim_publish_and_open_exact_version(setup):
    version, job, _ = publish(setup)
    doc = version.document
    doc.refresh_from_db()
    assert doc.current_version_id == version.id
    assert version.published_at
    response = setup['client'].get(f'/api/knowledge/documents/{doc.id}/?version={version.id}')
    assert response.status_code == 200
    assert response.data['pages'][0]['page'] == 1
    assert response.data['chunks'][0]['text'].startswith('A source')
    assert response.data['content_url'].startswith('/api/knowledge/')
    assert IngestionRun.objects.get(id=job['id']).status == 'succeeded'


def test_duplicate_upload_does_not_embed_again(setup):
    version, _, _ = publish(setup)
    response = upload(setup)
    assert response.status_code == 200 and response.data['unchanged']
    assert DocumentVersion.objects.count() == 1
    assert IngestionRun.objects.count() == 1


def test_org_and_document_acl_cover_listing_metadata_content_and_citations(setup):
    version, _, _ = publish(setup)
    for user in [setup['outsider'], setup['user']]:
        if user == setup['user']:
            setup['source'].allowed_users.remove(user)
        setup['client'].force_authenticate(user)
        assert setup['client'].get('/api/knowledge/documents/').data['count'] == 0
        assert setup['client'].get(f'/api/knowledge/documents/{version.document_id}/').status_code == 404
        assert setup['client'].get(f'/api/knowledge/documents/{version.document_id}/content/?version={version.id}').status_code == 404
        assert filter_citations(setup['org'], user, [citation(version)]) == []


def test_admin_does_not_bypass_source_acl(setup):
    version, _, _ = publish(setup)
    setup['source'].allowed_users.remove(setup['admin'])
    assert setup['client'].get(f'/api/knowledge/documents/{version.document_id}/').status_code == 404
    assert setup['client'].get('/api/knowledge/sources/').status_code == 200


def test_deleted_document_is_immediately_unreadable_including_old_citations(setup):
    version, _, _ = publish(setup)
    assert setup['client'].delete(f'/api/knowledge/documents/{version.document_id}/').status_code == 204
    assert filter_citations(setup['org'], setup['admin'], [citation(version)]) == []
    assert setup['client'].get(f'/api/knowledge/documents/{version.document_id}/content/').status_code == 404


def test_disabled_feature_fails_closed(setup, settings):
    settings.DOCUMENT_EXPLORER_ENABLED = False
    assert setup['client'].get('/api/knowledge/status/').data['enabled'] is False
    assert setup['client'].get('/api/knowledge/documents/').status_code == 404
    assert setup['worker'].post('/api/knowledge/internal/jobs/claim/', {'worker_id': 'x'}, format='json').status_code == 404


def test_worker_requires_bearer_and_current_lease(setup):
    upload(setup)
    rogue = APIClient()
    assert rogue.post('/api/knowledge/internal/jobs/claim/', {'worker_id': 'x'}, format='json').status_code == 403
    job = claim(setup)
    setup['worker'].credentials(HTTP_AUTHORIZATION='Bearer test-worker-key-with-no-real-service-access', HTTP_X_LEASE_TOKEN=str(uuid.uuid4()))
    assert setup['worker'].post(f"/api/knowledge/internal/jobs/{job['id']}/complete/", completion(job), format='json').status_code == 409
    assert setup['worker'].get(job['documents'][0]['download_url']).status_code == 409
    assert Document.objects.get(id=job['documents'][0]['id']).current_version_id is None


def test_worker_download_is_confined_to_claim(setup):
    upload(setup)
    job = claim(setup)
    response = setup['worker'].get(job['documents'][0]['download_url'])
    assert response.status_code == 200
    assert b''.join(response.streaming_content).startswith(b'A source')
    foreign = Document.objects.create(source=setup['source'], external_id='unclaimed', title='Other', mime_type='text/plain')
    version = DocumentVersion.objects.create(document=foreign, content_hash='0' * 64)
    assert setup['worker'].get(f"/api/knowledge/internal/jobs/{job['id']}/documents/{version.id}/content/").status_code == 404


def test_lost_completion_response_can_be_replayed_without_republication(setup):
    version, job, body = publish(setup)
    timestamp = version.published_at
    response = setup['worker'].post(f"/api/knowledge/internal/jobs/{job['id']}/complete/", body, format='json')
    assert response.status_code == 200
    version.refresh_from_db()
    assert version.published_at == timestamp
    body['documents'][0]['pages'][0]['text'] = 'Changed after publication'
    assert setup['worker'].post(f"/api/knowledge/internal/jobs/{job['id']}/complete/", body, format='json').status_code == 409


def test_cancelled_worker_cannot_publish(setup):
    upload(setup)
    job = claim(setup)
    assert setup['client'].post(f"/api/knowledge/runs/{job['id']}/cancel/", {}, format='json').data['status'] == 'cancel_requested'
    heartbeat = setup['worker'].post(f"/api/knowledge/internal/jobs/{job['id']}/heartbeat/", {'stage': 'embedding'}, format='json')
    assert heartbeat.data['cancel_requested'] is True
    assert setup['worker'].post(f"/api/knowledge/internal/jobs/{job['id']}/complete/", completion(job), format='json').status_code == 409
    assert setup['worker'].post(f"/api/knowledge/internal/jobs/{job['id']}/fail/", {'code': 'CANCELLED'}, format='json').data['status'] == 'cancelled'


def test_expired_lease_reclaim_fences_previous_worker(setup):
    upload(setup)
    first = claim(setup)
    IngestionRun.objects.filter(id=first['id']).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    reconcile_expired()
    second = claim(setup)
    assert first['id'] == second['id'] and first['lease_token'] != second['lease_token']
    setup['worker'].credentials(HTTP_AUTHORIZATION='Bearer test-worker-key-with-no-real-service-access', HTTP_X_LEASE_TOKEN=first['lease_token'])
    assert setup['worker'].post(f"/api/knowledge/internal/jobs/{first['id']}/complete/", completion(first), format='json').status_code == 409


def test_pausing_source_revokes_read_and_publish(setup):
    upload(setup)
    job = claim(setup)
    assert setup['client'].patch(f"/api/knowledge/sources/{setup['source'].id}/", {'is_active': False}, format='json').status_code == 200
    assert setup['worker'].post(f"/api/knowledge/internal/jobs/{job['id']}/complete/", completion(job), format='json').status_code == 409
    assert setup['client'].get('/api/knowledge/documents/').data['count'] == 0


def test_retry_preserves_published_version_on_failure(setup):
    version, _, _ = publish(setup)
    doc = version.document
    response = setup['client'].post('/api/knowledge/uploads/', {'source_id': str(setup['source'].id), 'document_id': str(doc.id), 'file': SimpleUploadedFile('Guide.txt', b'New revision')}, format='multipart')
    assert response.status_code == 201
    job = claim(setup)
    body = completion(job)
    body['documents'][0].update(status='failed', pages=[], chunks=[], error='Unable to extract text.')
    assert setup['worker'].post(f"/api/knowledge/internal/jobs/{job['id']}/complete/", body, format='json').status_code == 200
    doc.refresh_from_db()
    assert doc.current_version_id == version.id
    assert setup['client'].post(f"/api/knowledge/runs/{job['id']}/retry/", {}, format='json').status_code == 202


@pytest.mark.parametrize('path', ['../secret', '/absolute/file', 'C:/secret', 'folder\\secret'])
def test_snapshot_path_cannot_escape_import_root(setup, path):
    response = setup['client'].post('/api/knowledge/sources/', {'name': 'Snapshot', 'kind': 'snapshot', 'config': {'path': path}}, format='json')
    assert response.status_code == 400


def test_source_configuration_rejects_foreign_users_and_credentials(setup):
    source_url = f"/api/knowledge/sources/{setup['source'].id}/"
    assert setup['client'].patch(source_url, {'allowed_user_ids': [setup['outsider'].pk]}, format='json').status_code == 400
    assert setup['client'].patch(source_url, {'config': {'token': 'should-never-persist'}}, format='json').status_code == 400
    assert setup['client'].patch(source_url, {'cron': 'not a cron'}, format='json').status_code == 400
    assert setup['client'].patch(source_url, {'timezone': 'Invalid/Timezone'}, format='json').status_code == 400


def test_source_creator_default_and_reader_cannot_manage(setup):
    response = setup['client'].post('/api/knowledge/sources/', {'name': 'New uploads', 'kind': 'upload'}, format='json')
    assert response.status_code == 201, response.data
    assert response.data['allowed_user_ids'] == [setup['admin'].id]
    setup['client'].force_authenticate(setup['user'])
    assert setup['client'].get('/api/knowledge/sources/').status_code == 403
    assert upload(setup).status_code == 403


def test_missed_schedules_coalesce_and_manual_overlap_is_blocked(setup):
    source = setup['source']
    source.kind, source.config = 'snapshot', {'path': 'snapshot.json'}
    source.cron, source.timezone, source.next_run_at = '0 * * * *', 'Europe/Athens', timezone.now() - timedelta(days=3)
    source.save()
    assert tick() == 1
    assert tick() == 0
    source.refresh_from_db()
    assert source.next_run_at > timezone.now()
    with pytest.raises(JobConflict):
        enqueue(source)
    assert IngestionRun.objects.count() == 1


def test_range_request_and_private_headers(setup):
    version, _, _ = publish(setup)
    response = setup['client'].get(f'/api/knowledge/documents/{version.document_id}/content/?version={version.id}', HTTP_RANGE='bytes=2-9')
    assert response.status_code == 206
    assert b''.join(response.streaming_content) == b'source d'
    assert response['Cache-Control'] == 'private, no-store'
    assert response['X-Content-Type-Options'] == 'nosniff'
    assert setup['client'].get(f'/api/knowledge/documents/{version.document_id}/content/', HTTP_RANGE='bytes=999999-').status_code == 416


def test_invalid_files_and_oversize_are_rejected_before_job_creation(setup, settings):
    assert upload(setup, b'<html>active content</html>', 'bad.html').status_code == 400
    settings.DOCUMENT_EXPLORER_MAX_UPLOAD_BYTES = 5
    assert upload(setup).status_code == 400
    assert Document.objects.count() == 0 and IngestionRun.objects.count() == 0


def test_empty_ready_and_cross_version_completion_fail_closed(setup):
    upload(setup)
    job = claim(setup)
    body = completion(job)
    body['documents'][0]['chunks'] = []
    assert setup['worker'].post(f"/api/knowledge/internal/jobs/{job['id']}/complete/", body, format='json').status_code == 400
    body = completion(job)
    body['documents'][0]['version_id'] = str(uuid.uuid4())
    assert setup['worker'].post(f"/api/knowledge/internal/jobs/{job['id']}/complete/", body, format='json').status_code == 400


def test_snapshot_registration_and_mass_delete_guard(setup):
    source = setup['source']
    source.kind, source.config = 'snapshot', {'path': 'sample.json'}
    source.save()
    enqueue(source)
    job = claim(setup)
    response = setup['worker'].post(f"/api/knowledge/internal/jobs/{job['id']}/documents/", {'external_id': 'native-123', 'title': 'Guide', 'mime_type': 'text/plain', 'source_type': 'confluence', 'source_url': 'https://example.com/123', 'content_hash': hashlib.sha256(b'fixture').hexdigest()}, format='json')
    assert response.status_code == 200, response.data
    job['documents'] = [response.data]
    body = completion(job)
    assert setup['worker'].post(f"/api/knowledge/internal/jobs/{job['id']}/complete/", body, format='json').status_code == 200
    enqueue(source)
    empty = claim(setup)
    assert setup['worker'].post(f"/api/knowledge/internal/jobs/{empty['id']}/complete/", {'documents': [], 'index_generation': 'empty'}, format='json').status_code == 409
    assert Document.objects.filter(deleted_at__isnull=True).count() == 1


def test_citations_use_record_metadata_and_reject_forged_chunks(setup):
    version, _, _ = publish(setup)
    forged = {**citation(version), 'title': 'fake', 'datagov_url': 'javascript:alert(1)', 'excerpt': 'fake', 'page': 88}
    result = filter_citations(setup['org'], setup['user'], [forged])
    assert result[0]['title'] == version.document.title and result[0]['page'] == 1
    assert result[0]['datagov_url'].startswith('/knowledge/documents/')
    assert filter_citations(setup['org'], setup['user'], [{**forged, 'chunk_id': 'not-evidence'}]) == []
