"""Optional indexing output of the existing connector pipeline.

Capture runs under the existing ETL file lock. The artifact row IS the durable
outbox: the scheduler retries delivery until the leased indexing queue accepts it.
Indexing is incremental: current extractors do not certify complete enumeration,
so an absent file must never delete published evidence.
"""
import hashlib
import json
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from .jobs import JobConflict, enqueue
from .models import Document, DocumentVersion, ExtractionArtifact, KnowledgeSource
from .storage import private_storage

PROFILE = 'connector-text-v1'
MAX_FILES = 10000
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_BATCH_FILES = 500  # Below the engine completion contract's 1000 document bound.
MAX_BATCH_BYTES = 12 * 1024 * 1024


def _files(connection, etl_dir):
    from etl.sources.registry import SOURCE_REGISTRY
    root = Path(etl_dir).resolve()
    adapter = SOURCE_REGISTRY.get(connection.source_type)
    inputs = adapter.get_indexing_inputs(root) if adapter else None
    if inputs is None:
        return
    base, roots, extensions = inputs
    for directory in roots:
        if not directory.exists():
            continue
        for path in sorted(directory.rglob('*')):
            if not path.is_file() or path.suffix.lower() not in extensions:
                continue
            # A repository/provider symlink must not read outside extraction.
            if path.is_symlink() or not path.resolve().is_relative_to(base.resolve()):
                raise ValueError('An extracted file points outside its artifact root.')
            relative = path.relative_to(base).as_posix()
            if len(relative) > 500:
                raise ValueError('Extracted document identity is too long.')
            if any(part.startswith('.') for part in Path(relative).parts):
                continue
            yield path, relative


def _source_url(connection, relative):
    if connection.source_type != 'dbt':
        return ''
    parsed = urlsplit(connection.github_repo_url or '')
    if parsed.scheme != 'https' or parsed.hostname != 'github.com' or parsed.username or parsed.password:
        return ''
    path = parsed.path.removesuffix('.git').rstrip('/')
    path += '/blob/' + quote(connection.github_branch or 'main', safe='') + '/' + quote(relative, safe='/')
    return urlunsplit(('https', 'github.com', path, '', ''))


def capture_extraction(connection, run_log, etl_dir):
    """Persist a completed extraction BEFORE load/cleanup. No provider requests.

    Catalog loading and indexing consume the same extraction independently.
    File writes precede the transaction; rollback can leave an unreferenced private
    blob but can never publish a partially written manifest.
    """
    if not getattr(settings, 'DOCUMENT_INDEXING_PIPELINE_ENABLED', False):
        return None
    source = KnowledgeSource.objects.filter(integration=connection, organization=connection.organization,
                                            pipeline_enabled=True, is_active=True, refresh_paused=False).first()
    if not source:
        return None
    upstream_key = f'source-run:{run_log.pk}'
    existing = ExtractionArtifact.objects.filter(source=source, upstream_key=upstream_key, profile=PROFILE).first()
    if existing:
        return existing
    storage, entries, total = private_storage(), [], 0
    for path, relative in _files(connection, etl_dir):
        if len(entries) >= MAX_FILES or path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError('Extraction exceeds the configured indexing artifact limits.')
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise ValueError('Extracted file exceeds the indexing limit.')
        text = data.decode('utf-8-sig')
        if '\x00' in text:
            raise ValueError('Extracted definition is not text.')
        if not text.strip():
            continue
        data = text.encode('utf-8')
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ValueError('Extraction exceeds the configured indexing artifact limits.')
        digest = hashlib.sha256(data).hexdigest()
        key = f'{connection.organization_id}/extractions/{source.pk}/{run_log.pk}/{digest}'
        if not storage.exists(key):
            key = storage.save(key, ContentFile(data))
        entries.append({'external_id': relative, 'title': relative[:500], 'content_hash': digest,
                        'storage_key': key, 'size': len(data), 'mime_type': 'text/plain',
                        'source_type': connection.source_type, 'source_url': _source_url(connection, relative)})
    batches, batch, batch_bytes = [], [], 0
    for entry in entries:
        if batch and (len(batch) >= MAX_BATCH_FILES or batch_bytes + entry['size'] > MAX_BATCH_BYTES):
            batches.append(batch)
            batch, batch_bytes = [], 0
        batch.append(entry)
        batch_bytes += entry['size']
    batches.append(batch)
    with transaction.atomic():
        # Match job/source lock ordering and recheck the policy after file I/O.
        locked = KnowledgeSource.objects.select_for_update().get(pk=source.pk)
        if not locked.pipeline_enabled or not locked.is_active or locked.integration_id != connection.pk:
            return None
        first = None
        for index, batch in enumerate(batches):
            manifest = {'profile': PROFILE, 'complete': False, 'integration_id': connection.pk,
                        'batch_index': index, 'batch_count': len(batches), 'entries': batch}
            digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
            key = upstream_key if index == 0 else f'{upstream_key}:batch:{index}'
            artifact, _ = ExtractionArtifact.objects.get_or_create(source=source, upstream_key=key, profile=PROFILE,
                defaults={'upstream_run': run_log, 'manifest': manifest, 'manifest_hash': digest})
            first = first or artifact
        return first


def capture_indexing_output(connection, run_log, etl_dir, log):
    """An optional output cannot turn a successful catalog load into a failure."""
    try:
        artifact = capture_extraction(connection, run_log, etl_dir)
        if artifact:
            log(f'[Indexing] Saved artifact {artifact.pk}; downstream processing is queued independently.')
    except Exception as exc:
        # Persist a failed stage, without private file contents or provider errors.
        source = KnowledgeSource.objects.filter(integration=connection, organization=connection.organization,
                                                pipeline_enabled=True).first()
        if source:
            ExtractionArtifact.objects.get_or_create(source=source, upstream_key=f'source-run:{run_log.pk}', profile=PROFILE,
                defaults={'upstream_run': run_log, 'manifest_hash': '',
                          'manifest': {'error': f'Artifact capture failed ({type(exc).__name__}). Run the integration again after checking its extracted files.'}})
        log(f'[Indexing] Artifact capture failed ({type(exc).__name__}); catalog processing continues.')


@transaction.atomic
def dispatch_artifact(artifact_id):
    source_id = ExtractionArtifact.objects.values_list('source_id', flat=True).get(pk=artifact_id)
    source = KnowledgeSource.objects.select_for_update().get(pk=source_id)
    artifact = ExtractionArtifact.objects.select_for_update().get(pk=artifact_id)
    if artifact.run_id or artifact.cancelled_at or artifact.manifest.get('error') or not source.is_active or source.refresh_paused or not source.pipeline_enabled:
        return False
    if artifact.manifest.get('integration_id') != source.integration_id:
        return False
    manifest_hash = hashlib.sha256(json.dumps(artifact.manifest, sort_keys=True).encode()).hexdigest()
    if manifest_hash != artifact.manifest_hash:
        raise ValueError('Immutable extraction manifest checksum mismatch.')
    # Check BEFORE writing document state; the scheduler coalesces busy sources
    # by retaining every pending artifact for subsequent ticks.
    if source.runs.filter(status__in=['queued', 'running', 'cancel_requested']).exists():
        return False
    versions = []
    for entry in artifact.manifest['entries']:
        doc, created = Document.objects.get_or_create(source=source, external_id=entry['external_id'], defaults={
            'title': entry['title'], 'mime_type': entry['mime_type'],
            'source_type': entry['source_type'], 'source_url': entry['source_url'],
        })
        if doc.deleted_at is not None:
            continue  # Explicit deletion stays revoked until an administrator restores it.
        version, _ = DocumentVersion.objects.defer('pages', 'chunks').get_or_create(document=doc, content_hash=entry['content_hash'],
            defaults={'storage_key': entry['storage_key'], 'size': entry['size']})
        versions.append(version)
    run, _ = enqueue(source, f'artifact:{artifact.pk}:{artifact.profile}', versions)
    run.configuration = {**run.configuration, 'artifact_id': str(artifact.pk),
                         'profile': artifact.profile, 'upstream_key': artifact.upstream_key}
    run.save(update_fields=['configuration'])
    artifact.run = run
    artifact.save(update_fields=['run'])
    return True


def dispatch_pending():
    if not getattr(settings, 'DOCUMENT_INDEXING_PIPELINE_ENABLED', False):
        return 0
    count = 0
    pending = ExtractionArtifact.objects.filter(run__isnull=True, cancelled_at__isnull=True, source__is_active=True,
                                               source__refresh_paused=False, source__pipeline_enabled=True).exclude(manifest__has_key='error')
    for artifact_id in pending.values_list('id', flat=True)[:100]:
        try:
            count += int(dispatch_artifact(artifact_id))
        except JobConflict:
            continue
    return count


def cancel_indexing_output(upstream_run):
    """Propagate explicit upstream cancellation; a load failure is independent."""
    if not getattr(settings, 'DOCUMENT_INDEXING_PIPELINE_ENABLED', False):
        return
    artifacts = ExtractionArtifact.objects.filter(upstream_run=upstream_run)
    for artifact_id, source_id in artifacts.values_list('id', 'source_id'):
        with transaction.atomic():
            KnowledgeSource.objects.select_for_update().get(pk=source_id)
            artifact = ExtractionArtifact.objects.select_for_update().get(pk=artifact_id)
            artifact.cancelled_at = timezone.now()
            artifact.save(update_fields=['cancelled_at'])
            if artifact.run_id:
                run = artifact.run
                if run.status == 'queued':
                    run.status, run.stage, run.finished_at = 'cancelled', 'cancelled', timezone.now()
                elif run.status == 'running':
                    run.status, run.stage = 'cancel_requested', 'cancel_requested'
                else:
                    continue
                run.save(update_fields=['status', 'stage', 'finished_at'])
