import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import APIException

from .models import DocumentVersion, IngestionRun, KnowledgeSource


class JobConflict(APIException):
    status_code = 409
    default_detail = 'This job is no longer owned by this worker.'


def source_configuration(source):
    return {'id': str(source.id), 'kind': source.kind, 'config': source.config}


@transaction.atomic
def enqueue(source, key=None, versions=None):
    source = KnowledgeSource.objects.select_for_update().get(pk=source.pk)
    if not source.is_active:
        raise JobConflict('This source is paused.')
    key = key or f'manual:{uuid.uuid4()}'
    existing = IngestionRun.objects.filter(source=source, idempotency_key=key).first()
    if existing:
        return existing, False
    active = IngestionRun.objects.filter(source=source, status__in=IngestionRun.ACTIVE).first()
    if active:
        raise JobConflict('A run is already active for this source. Wait for it to finish or cancel it.')
    run = IngestionRun.objects.create(source=source, idempotency_key=key, configuration=source_configuration(source))
    if versions is None and source.kind == 'upload':
        versions = DocumentVersion.objects.filter(document__source=source, document__deleted_at__isnull=True, published_at__isnull=True, status__in=['queued', 'failed'])
    run.versions.set(versions or [])
    if source.kind == 'upload' and not run.versions.exists():
        run.status, run.stage, run.progress = 'succeeded', 'unchanged', 100
        run.finished_at = timezone.now()
        run.counters = {'documents': 0, 'ready': 0, 'unchanged': True}
        run.save()
    return run, True


@transaction.atomic
def reconcile_expired():
    now = timezone.now()
    runs = IngestionRun.objects.select_for_update(skip_locked=True).filter(status__in=['running', 'cancel_requested'], lease_expires_at__lte=now)
    count = 0
    for run in runs:
        if run.status == 'cancel_requested' or not run.source.is_active:
            run.status = 'cancelled'
            run.finished_at = now
        elif run.attempt >= settings.DOCUMENT_EXPLORER_MAX_ATTEMPTS:
            run.status = 'failed'
            run.error = 'The processing worker stopped responding. Retry this run after checking the service.'
            run.finished_at = now
        else:
            run.status = 'queued'
        run.stage = run.status
        run.lease_token = None
        run.lease_expires_at = None
        run.save()
        if run.status in ['failed', 'cancelled']:
            run.versions.filter(published_at__isnull=True).update(status='failed', error=run.error or 'Processing was cancelled.')
        count += 1
    return count


def lease_deadline():
    return timezone.now() + timedelta(seconds=settings.DOCUMENT_EXPLORER_LEASE_SECONDS)


@transaction.atomic
def claim(worker_id):
    # Source permissions/configuration are authoritative in DataGov. Configuration
    # is frozen at enqueue; disabling the source still prevents a claim.
    # The source join only filters eligibility. Locking that joined row here
    # would invert the source -> run order used by source pause/publication.
    run = IngestionRun.objects.select_for_update(of=('self',), skip_locked=True).filter(status='queued', source__is_active=True).order_by('created_at').first()
    if run is None:
        return None
    if run.attempt and run.source.kind == 'snapshot':
        # A reclaimed import re-enumerates the manifest. Earlier attempts cannot
        # require publication of a document no longer present in that manifest.
        run.versions.clear()
    run.status, run.stage, run.worker_id = 'running', 'fetch', worker_id
    run.lease_token = uuid.uuid4()
    run.lease_expires_at = lease_deadline()
    run.attempt += 1
    run.started_at = run.started_at or timezone.now()
    run.save()
    run.versions.filter(published_at__isnull=True).update(status='processing')
    return run


def locked_run(run_id, organization=None):
    """Lock source before run inside the caller's atomic transaction.

    Source pause locks the source and then cancels its runs. Publication also
    changes source metadata, so merely excluding the source from a joined row
    lock would still leave a run -> source inversion at its final UPDATE.
    """
    candidates = IngestionRun.objects.filter(id=run_id)
    if organization is not None:
        candidates = candidates.filter(source__organization=organization)
    source_id = candidates.values_list('source_id', flat=True).first()
    if source_id is None:
        return None
    sources = KnowledgeSource.objects.select_for_update().filter(pk=source_id)
    if organization is not None:
        sources = sources.filter(organization=organization)
    source = sources.first()
    if source is None:
        return None
    run = IngestionRun.objects.select_for_update(of=('self',)).filter(id=run_id, source=source).first()
    if run is not None:
        run.source = source
    return run


def leased_run(run_id, token, lock=True, allow_cancel=False):
    run = locked_run(run_id) if lock else IngestionRun.objects.filter(id=run_id).select_related('source').first()
    states = ['running', 'cancel_requested'] if allow_cancel else ['running']
    if not run or not token or str(run.lease_token) != str(token) or run.status not in states or not run.lease_expires_at or run.lease_expires_at <= timezone.now():
        raise JobConflict()
    if not run.source.is_active and not allow_cancel:
        raise JobConflict('The source has been paused.')
    return run
