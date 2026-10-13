"""Real PostgreSQL races between source pause and worker publication."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from django.db import close_old_connections, connection, transaction
from rest_framework.test import APIClient

from knowledge import views
from knowledge.jobs import claim as claim_run
from knowledge.models import Document, DocumentVersion, IngestionRun, KnowledgeSource
from .test_document_explorer import setup, upload, claim, completion

pytestmark = pytest.mark.django_db(transaction=True)


def _postgres_only():
    if connection.vendor != 'postgresql':
        pytest.skip('This test exercises PostgreSQL row lock ordering.')


def _client(user=None, lease=None):
    client = APIClient()
    client.raise_request_exception = False
    if user:
        client.force_authenticate(user)
    else:
        client.credentials(HTTP_AUTHORIZATION='Bearer test-worker-key-with-no-real-service-access',
                           HTTP_X_LEASE_TOKEN=lease)
    return client


def _bounded_transaction():
    # Each thread has its own connection. A broken lock order must fail the
    # test rather than leave a worker waiting indefinitely.
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL lock_timeout = '5s'")


def test_source_pause_wins_without_worker_holding_a_run_lock(setup):
    _postgres_only()
    assert upload(setup).status_code == 201
    job = claim(setup)
    source_locked, worker_waiting = Event(), Event()

    def pause():
        close_old_connections()
        try:
            with transaction.atomic():
                _bounded_transaction()
                KnowledgeSource.objects.select_for_update().get(pk=setup['source'].pk)
                source_locked.set()
                assert worker_waiting.wait(5), 'Worker never attempted its first lifecycle lock'
                response = _client(setup['admin']).patch(
                    f"/api/knowledge/sources/{setup['source'].pk}/", {'is_active': False}, format='json')
                return response.status_code
        finally:
            connection.close()

    def publish():
        close_old_connections()
        try:
            assert source_locked.wait(5)

            def observe_locks(execute, sql, params, many, context):
                # Correct implementation waits on source before locking run.
                # A regressed implementation locks run first; signal after that
                # lock so the source owner attempts cancellation concurrently.
                source_lock = 'FOR UPDATE' in sql and 'FROM "knowledge_knowledgesource"' in sql
                run_lock = 'FOR UPDATE' in sql and 'FROM "knowledge_ingestionrun"' in sql
                if source_lock:
                    worker_waiting.set()
                result = execute(sql, params, many, context)
                if run_lock:
                    worker_waiting.set()
                return result

            with transaction.atomic(), connection.execute_wrapper(observe_locks):
                _bounded_transaction()
                response = _client(lease=job['lease_token']).post(
                    f"/api/knowledge/internal/jobs/{job['id']}/complete/", completion(job), format='json')
                return response.status_code
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        paused = pool.submit(pause)
        published = pool.submit(publish)
        assert paused.result(timeout=15) == 200
        assert published.result(timeout=15) == 409
    assert IngestionRun.objects.get(pk=job['id']).status == 'cancel_requested'
    assert not DocumentVersion.objects.filter(published_at__isnull=False).exists()


def test_publication_wins_then_pause_revokes_source_without_deadlock(setup, monkeypatch):
    _postgres_only()
    assert upload(setup).status_code == 201
    job = claim(setup)
    validating, pausing = Event(), Event()
    validate = views._validate_result

    def hold_publication(item, version):
        checked = validate(item, version)
        validating.set()
        assert pausing.wait(5), 'Pause never attempted the source lock'
        return checked

    monkeypatch.setattr(views, '_validate_result', hold_publication)

    def publish():
        close_old_connections()
        try:
            with transaction.atomic():
                _bounded_transaction()
                response = _client(lease=job['lease_token']).post(
                    f"/api/knowledge/internal/jobs/{job['id']}/complete/", completion(job), format='json')
                return response.status_code
        finally:
            connection.close()

    def pause():
        close_old_connections()
        try:
            assert validating.wait(5)

            def observe_source(execute, sql, params, many, context):
                if 'FOR UPDATE' in sql and 'FROM "knowledge_knowledgesource"' in sql:
                    pausing.set()
                return execute(sql, params, many, context)

            with transaction.atomic(), connection.execute_wrapper(observe_source):
                _bounded_transaction()
                response = _client(setup['admin']).patch(
                    f"/api/knowledge/sources/{setup['source'].pk}/", {'is_active': False}, format='json')
                return response.status_code
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        published = pool.submit(publish)
        paused = pool.submit(pause)
        assert published.result(timeout=15) == 200
        assert paused.result(timeout=15) == 200
    assert IngestionRun.objects.get(pk=job['id']).status == 'succeeded'
    assert Document.objects.get(pk=job['documents'][0]['id']).current_version_id is not None
    assert setup['client'].get('/api/knowledge/documents/').data['count'] == 0


def test_claim_does_not_skip_run_because_source_metadata_is_locked(setup):
    _postgres_only()
    assert upload(setup).status_code == 201

    def claim_from_another_connection():
        close_old_connections()
        try:
            with transaction.atomic():
                _bounded_transaction()
                run = claim_run('parallel-worker')
                return str(run.id) if run else None
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as pool, transaction.atomic():
        KnowledgeSource.objects.select_for_update().get(pk=setup['source'].pk)
        claimed_id = pool.submit(claim_from_another_connection).result(timeout=10)
        assert claimed_id is not None
    assert IngestionRun.objects.get(pk=claimed_id).status == 'running'
