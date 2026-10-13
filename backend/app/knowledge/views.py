import hashlib
import json
import re
import secrets
import uuid
from urllib.parse import urlparse

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view, authentication_classes, parser_classes, permission_classes
from rest_framework.exceptions import MethodNotAllowed, NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .jobs import JobConflict, claim, enqueue, lease_deadline, leased_run, locked_run, reconcile_expired
from .models import Document, DocumentVersion, IngestionRun, KnowledgeSource
from .permissions import can_manage, document_url, enabled, request_org, visible_documents
from .parsers import BoundedJSONParser
from .scheduling import next_occurrence
from .serializers import SourceSerializer
from .storage import private_storage


def _doc(doc):
    latest = doc.versions.first()
    return {
        'id': str(doc.id), 'title': doc.title, 'mime_type': doc.mime_type,
        'source_id': str(doc.source_id), 'source_name': doc.source.name,
        'source_type': doc.source_type, 'source_url': doc.source_url or None,
        'status': latest.status if latest else 'queued',
        'current_version_id': str(doc.current_version_id) if doc.current_version_id else None,
        'created_at': doc.created_at, 'updated_at': doc.updated_at,
    }


def _run(run):
    return {
        'id': str(run.id), 'source_id': str(run.source_id), 'source_name': run.source.name,
        'status': run.status, 'stage': run.stage, 'progress': run.progress,
        'counters': run.counters, 'error': run.error, 'attempt': run.attempt,
        'created_at': run.created_at, 'started_at': run.started_at,
        'finished_at': run.finished_at, 'cancel_requested': run.status == 'cancel_requested',
    }


def _idempotency(request):
    key = request.headers.get('Idempotency-Key')
    if key and (len(key) > 150 or not re.fullmatch(r'[A-Za-z0-9_.:-]+', key)):
        raise ValidationError('Invalid idempotency key.')
    return f'manual:{key}' if key else None


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def status(request):
    from catalog.access import resolve_org
    org = resolve_org(request.user)
    return Response({
        'enabled': enabled(),
        'configured': bool(settings.DOCUMENT_EXPLORER_API_URL and settings.DOCUMENT_EXPLORER_API_KEY and settings.DOCUMENT_EXPLORER_WORKER_TOKEN),
        'can_manage': can_manage(org, request.user),
        'supported_sources': ['upload', 'snapshot'],
    })


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def sources(request):
    org = request_org(request, manage=True)
    if request.method == 'GET':
        return Response({'results': SourceSerializer(KnowledgeSource.objects.filter(organization=org), many=True).data})
    serializer = SourceSerializer(data=request.data, context={'org': org, 'request': request})
    serializer.is_valid(raise_exception=True)
    with transaction.atomic():
        source = serializer.save()
        source.next_run_at = next_occurrence(source.cron, source.timezone) if source.is_active else None
        source.save(update_fields=['next_run_at'])
    return Response(SourceSerializer(source).data, status=201)


@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
def source_detail(request, source_id):
    org = request_org(request, manage=True)
    with transaction.atomic():
        source = get_object_or_404(KnowledgeSource.objects.select_for_update(), pk=source_id, organization=org)
        serializer = SourceSerializer(source, data=request.data, partial=True, context={'org': org, 'request': request})
        serializer.is_valid(raise_exception=True)
        source = serializer.save()
        source.next_run_at = next_occurrence(source.cron, source.timezone) if source.is_active else None
        source.save(update_fields=['next_run_at'])
        if not source.is_active:
            source.runs.filter(status='queued').update(status='cancelled', stage='cancelled', finished_at=timezone.now())
            source.runs.filter(status='running').update(status='cancel_requested', stage='cancel_requested')
    return Response(SourceSerializer(source).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def sync_source(request, source_id):
    org = request_org(request, manage=True)
    source = get_object_or_404(KnowledgeSource, pk=source_id, organization=org)
    run, created = enqueue(source, _idempotency(request))
    return Response(_run(run), status=202 if created else 200)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def upload(request):
    org = request_org(request, manage=True)
    source = get_object_or_404(KnowledgeSource, pk=_uuid(request.data.get('source_id')), organization=org, kind='upload', is_active=True)
    incoming = request.FILES.get('file')
    if incoming is None or not incoming.size:
        raise ValidationError({'file': 'Choose a nonempty PDF or UTF-8 text file.'})
    if incoming.size > settings.DOCUMENT_EXPLORER_MAX_UPLOAD_BYTES:
        raise ValidationError({'file': 'This document exceeds the configured upload limit.'})
    data = incoming.read(settings.DOCUMENT_EXPLORER_MAX_UPLOAD_BYTES + 1)
    if len(data) > settings.DOCUMENT_EXPLORER_MAX_UPLOAD_BYTES:
        raise ValidationError({'file': 'This document exceeds the configured upload limit.'})
    filename = str(incoming.name).replace('\\', '/').rsplit('/', 1)[-1][:500]
    if data[:1024].lstrip().startswith(b'%PDF-'):
        mime = 'application/pdf'
    elif filename.lower().endswith(('.txt', '.md')) and b'\x00' not in data:
        try:
            data.decode('utf-8-sig')
        except UnicodeDecodeError:
            raise ValidationError({'file': 'Text files must use UTF-8 encoding.'})
        mime = 'text/plain'
    else:
        raise ValidationError({'file': 'Supported uploads are PDF, .txt and .md files.'})
    digest = hashlib.sha256(data).hexdigest()
    key = _idempotency(request)
    storage, saved_key = private_storage(), None
    try:
        with transaction.atomic():
            source = KnowledgeSource.objects.select_for_update().get(pk=source.pk)
            replacement = request.data.get('document_id')
            previous = source.runs.filter(idempotency_key=key).first() if key else None
            if previous:
                previous_version = previous.versions.select_related('document').first()
                if not previous_version or previous_version.content_hash != digest or (replacement and str(previous_version.document_id) != str(replacement)):
                    raise JobConflict('This idempotency key belongs to a different upload.')
                return Response({'document': _doc(previous_version.document), 'version_id': str(previous_version.id), 'run': _run(previous), 'unchanged': True})
            if replacement:
                doc = get_object_or_404(Document.objects.select_for_update(), pk=_uuid(replacement), source=source, deleted_at__isnull=True)
                if doc.mime_type != mime:
                    raise ValidationError('A replacement must have the same document type.')
            else:
                # Deduplication is confined to this source and never discloses
                # another organization's or restricted source's content.
                version = DocumentVersion.objects.filter(document__source=source, document__deleted_at__isnull=True, content_hash=digest).select_related('document').first()
                if version:
                    return Response({'document': _doc(version.document), 'version_id': str(version.id), 'unchanged': True})
                doc = Document.objects.create(source=source, external_id=f'upload:{uuid.uuid4()}', title=filename or 'Document', mime_type=mime)
            existing = doc.versions.filter(content_hash=digest).first()
            if existing:
                return Response({'document': _doc(doc), 'version_id': str(existing.id), 'unchanged': True})
            version = DocumentVersion.objects.create(document=doc, content_hash=digest, size=len(data))
            saved_key = storage.save(f'{org.pk}/{doc.id}/{version.id}/original', ContentFile(data))
            version.storage_key = saved_key
            version.save(update_fields=['storage_key'])
            run, _ = enqueue(source, key, [version])
            return Response({'document': _doc(doc), 'version_id': str(version.id), 'run': _run(run), 'unchanged': False}, status=201)
    except Exception:
        if saved_key:
            storage.delete(saved_key)
        raise


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError('A valid UUID is required.')


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def documents(request):
    org = request_org(request)
    qs = visible_documents(org, request.user).select_related('source', 'current_version').prefetch_related('versions')
    if request.query_params.get('search'):
        qs = qs.filter(title__icontains=request.query_params['search'][:200])
    if request.query_params.get('source_id'):
        qs = qs.filter(source_id=_uuid(request.query_params['source_id']))
    if request.query_params.get('status'):
        from django.db.models import OuterRef, Subquery
        qs = qs.annotate(latest_status=Subquery(DocumentVersion.objects.filter(document=OuterRef('pk')).values('status')[:1])).filter(latest_status=request.query_params['status'])
    try:
        page_size = max(1, min(int(request.query_params.get('page_size', 25)), 100))
        page_number = max(1, int(request.query_params.get('page', 1)))
    except ValueError:
        raise ValidationError('Invalid pagination.')
    paginator = Paginator(qs, page_size)
    page = paginator.get_page(page_number)
    return Response({'count': paginator.count, 'results': [_doc(doc) for doc in page], 'next': page.next_page_number() if page.has_next() else None, 'previous': page.previous_page_number() if page.has_previous() else None})


def _resolve_document(request, document_id):
    org = request_org(request)
    doc = get_object_or_404(visible_documents(org, request.user).select_related('source', 'current_version'), pk=document_id)
    version_id = request.query_params.get('version')
    version = get_object_or_404(doc.versions, pk=_uuid(version_id)) if version_id else doc.current_version or doc.versions.first()
    if version is None:
        raise NotFound('This document has no version yet.')
    # Failed/unpublished text is not evidence. Admins can inspect processing state;
    # originals are still protected by the same explicit source access rules.
    return org, doc, version


@api_view(['GET', 'DELETE'])
@permission_classes([IsAuthenticated])
def document_detail(request, document_id):
    org, doc, version = _resolve_document(request, document_id)
    if request.method == 'DELETE':
        if not can_manage(org, request.user):
            raise PermissionDenied()
        doc.deleted_at = timezone.now()
        doc.deletion_reason = 'manual'
        doc.save(update_fields=['deleted_at', 'deletion_reason', 'updated_at'])
        return Response(status=204)
    ready = version.status == 'ready' and version.published_at is not None
    return Response({**_doc(doc), 'version_id': str(version.id), 'status': version.status,
        'error': version.error if can_manage(org, request.user) else '',
        'pages': version.pages if ready else [], 'chunks': version.chunks if ready else [],
        'content_url': f'/api/knowledge/documents/{doc.id}/content/?version={version.id}' if version.storage_key else None,
        'versions': [{'id': str(v.id), 'created_at': v.created_at, 'status': v.status} for v in doc.versions.all() if v.published_at or can_manage(org, request.user)],
        'datagov_url': document_url(doc.id, version.id),
    })


def _file_response(request, version):
    storage = private_storage()
    if not version.storage_key or not storage.exists(version.storage_key):
        raise NotFound('The original file is unavailable; extracted text may still be readable.')
    stream = storage.open(version.storage_key, 'rb')
    size = storage.size(version.storage_key)
    range_header = request.headers.get('Range', '')
    if range_header:
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', range_header)
        if not match or not any(match.groups()):
            stream.close()
            return HttpResponse(status=416, headers={'Content-Range': f'bytes */{size}'})
        start_s, end_s = match.groups()
        start = int(start_s) if start_s else max(0, size - int(end_s))
        end = min(int(end_s), size - 1) if start_s and end_s else size - 1
        if start >= size or start > end:
            stream.close()
            return HttpResponse(status=416, headers={'Content-Range': f'bytes */{size}'})
        def iterator():
            try:
                stream.seek(start)
                remaining = end - start + 1
                while remaining:
                    block = stream.read(min(65536, remaining))
                    if not block:
                        break
                    remaining -= len(block)
                    yield block
            finally:
                stream.close()
        response = StreamingHttpResponse(iterator(), status=206, content_type=version.document.mime_type)
        response['Content-Range'] = f'bytes {start}-{end}/{size}'
        response['Content-Length'] = str(end - start + 1)
    else:
        response = FileResponse(stream, content_type=version.document.mime_type, filename=version.document.title)
    response['Accept-Ranges'] = 'bytes'
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    response['X-Frame-Options'] = 'SAMEORIGIN'
    response['Content-Security-Policy'] = "sandbox; default-src 'none'"
    return response


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def document_content(request, document_id):
    _, _, version = _resolve_document(request, document_id)
    return _file_response(request, version)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def runs(request):
    org = request_org(request, manage=True)
    qs = IngestionRun.objects.filter(source__organization=org).select_related('source')
    if request.query_params.get('source_id'):
        qs = qs.filter(source_id=_uuid(request.query_params['source_id']))
    return Response({'results': [_run(run) for run in qs[:100]]})


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def run_detail(request, run_id, action=None):
    if (action is not None and request.method != 'POST') or (action is None and request.method != 'GET'):
        raise MethodNotAllowed(request.method)
    org = request_org(request, manage=True)
    with transaction.atomic():
        run = locked_run(run_id, organization=org)
        if run is None:
            raise NotFound('Run not found.')
        if request.method == 'GET' and action is None:
            return Response(_run(run))
        if action == 'cancel':
            if run.status == 'queued':
                run.status, run.stage, run.finished_at = 'cancelled', 'cancelled', timezone.now()
                run.versions.filter(published_at__isnull=True).update(status='failed', error='Processing was cancelled.')
            elif run.status == 'running':
                run.status, run.stage = 'cancel_requested', 'cancel_requested'
            run.save()
        elif action == 'retry':
            if run.status in IngestionRun.ACTIVE:
                raise JobConflict('This run is still active.')
            new, _ = enqueue(run.source, _idempotency(request), run.versions.filter(published_at__isnull=True))
            return Response(_run(new), status=202)
        else:
            raise ValidationError('Unknown run action.')
    return Response(_run(run))


def _worker(request):
    if not enabled():
        raise NotFound()
    expected = settings.DOCUMENT_EXPLORER_WORKER_TOKEN
    auth = request.headers.get('Authorization', '')
    if not expected or not auth.startswith('Bearer ') or not secrets.compare_digest(auth[7:], expected):
        raise PermissionDenied('Invalid service credentials.')


def _job_payload(run):
    return {'id': str(run.id), 'lease_token': str(run.lease_token),
        'organization_id': str(run.source.organization_id), 'source': run.configuration,
        'documents': [{'id': str(v.document_id), 'version_id': str(v.id), 'title': v.document.title,
            'mime_type': v.document.mime_type, 'content_hash': v.content_hash,
            'source_type': v.document.source_type, 'source_url': v.document.source_url or '',
            'download_url': f'/api/knowledge/internal/jobs/{run.id}/documents/{v.id}/content/',
        } for v in run.versions.select_related('document').all()]}


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
@parser_classes([BoundedJSONParser])
def claim_job(request):
    _worker(request)
    worker_id = request.data.get('worker_id', '')
    if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 200:
        raise ValidationError('worker_id is required.')
    reconcile_expired()
    run = claim(worker_id)
    return Response({'job': _job_payload(run) if run else None})


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
@parser_classes([BoundedJSONParser])
def heartbeat(request, run_id):
    _worker(request)
    with transaction.atomic():
        run = leased_run(run_id, request.headers.get('X-Lease-Token'), allow_cancel=True)
        if not run.source.is_active:
            run.status = 'cancel_requested'
        stage = request.data.get('stage', run.stage)
        progress = request.data.get('progress', run.progress)
        counters = request.data.get('counters', run.counters)
        if not isinstance(stage, str) or len(stage) > 80 or type(progress) is not int or not 0 <= progress <= 100 or not isinstance(counters, dict) or len(json.dumps(counters)) > 10000:
            raise ValidationError('Invalid progress payload.')
        run.stage, run.progress, run.counters = stage, progress, counters
        run.lease_expires_at = lease_deadline()
        run.save()
        return Response({'cancel_requested': run.status == 'cancel_requested', 'lease_expires_at': run.lease_expires_at})


@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
def worker_content(request, run_id, version_id):
    _worker(request)
    with transaction.atomic():
        run = leased_run(run_id, request.headers.get('X-Lease-Token'))
        version = get_object_or_404(run.versions.select_related('document'), pk=version_id, document__deleted_at__isnull=True)
        return _file_response(request, version)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
@parser_classes([BoundedJSONParser])
def register_document(request, run_id):
    _worker(request)
    data = request.data
    fields = {name: data.get(name, '') for name in ['external_id', 'title', 'mime_type', 'source_type', 'source_url', 'content_hash']}
    limits = {'external_id': 500, 'title': 500, 'mime_type': 150, 'source_type': 40, 'source_url': 2000, 'content_hash': 64}
    if any(not isinstance(value, str) or len(value) > limits[name] for name, value in fields.items()) or not fields['external_id'] or not fields['title'] or not re.fullmatch('[0-9a-f]{64}', fields['content_hash']):
        raise ValidationError('Invalid document metadata.')
    if fields['source_url']:
        try:
            url = urlparse(fields['source_url'])
            if url.scheme not in ['http', 'https'] or not url.hostname or url.username or url.password or any(ord(c) < 32 for c in fields['source_url']):
                raise ValueError()
        except ValueError:
            raise ValidationError('Invalid source URL.')
    with transaction.atomic():
        run = leased_run(run_id, request.headers.get('X-Lease-Token'))
        if run.source.kind != 'snapshot':
            raise PermissionDenied('This source does not support document registration.')
        doc, _ = Document.objects.get_or_create(source=run.source, external_id=fields['external_id'], defaults={k: fields[k] for k in ['title', 'mime_type', 'source_type', 'source_url']})
        if doc.deleted_at and doc.deletion_reason != 'source_missing':
            raise JobConflict('This document has been deleted in DataGov.')
        version, _ = DocumentVersion.objects.get_or_create(document=doc, content_hash=fields['content_hash'], defaults={'status': 'processing'})
        run.versions.add(version)
        return Response({'id': str(doc.id), 'version_id': str(version.id), 'unchanged': version.published_at is not None})


def _validate_result(item, version):
    if not isinstance(item, dict) or str(item.get('document_id')) != str(version.document_id):
        raise ValidationError('Document result does not match the claimed version.')
    status = item.get('status')
    if status not in ['ready', 'needs_ocr', 'failed']:
        raise ValidationError('Invalid document result status.')
    pages, chunks = item.get('pages', []), item.get('chunks', [])
    if not isinstance(pages, list) or not isinstance(chunks, list) or len(pages) > 10000 or len(chunks) > 20000:
        raise ValidationError('Invalid document evidence.')
    numbers, ids, chars = set(), set(), 0
    for page in pages:
        if not isinstance(page, dict) or set(page) - {'page', 'text'} or type(page.get('page')) is not int or page['page'] < 1 or page['page'] in numbers or not isinstance(page.get('text'), str):
            raise ValidationError('Invalid or duplicate document page.')
        numbers.add(page['page'])
        chars += len(page['text'])
    for chunk in chunks:
        if not isinstance(chunk, dict) or set(chunk) - {'id', 'page', 'text', 'section'} or not isinstance(chunk.get('id'), str) or not chunk['id'] or len(chunk['id']) > 160 or chunk['id'] in ids or type(chunk.get('page')) is not int or chunk.get('page') not in numbers or not isinstance(chunk.get('text'), str) or not chunk['text'].strip() or len(chunk['text']) > 20000:
            raise ValidationError('Invalid or duplicate evidence chunk.')
        if not isinstance(chunk.get('section', ''), str) or len(chunk.get('section', '')) > 500:
            raise ValidationError('Invalid section.')
        ids.add(chunk['id'])
        chars += len(chunk['text'])
    if chars > settings.DOCUMENT_EXPLORER_MAX_TEXT_CHARS or (status == 'ready' and not chunks):
        raise ValidationError('Ready documents require bounded, nonempty evidence.')
    error = item.get('error', '')
    if not isinstance(error, str) or len(error) > 1000:
        raise ValidationError('Invalid error message.')
    return status, pages, chunks, error


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
@parser_classes([BoundedJSONParser])
def complete_job(request, run_id):
    _worker(request)
    results = request.data.get('documents')
    generation = request.data.get('index_generation', '')
    payload_hash = hashlib.sha256(json.dumps(request.data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if not isinstance(results, list) or len(results) > 1000 or not isinstance(generation, str) or len(generation) > 200:
        raise ValidationError('Invalid completion payload.')
    with transaction.atomic():
        previous = locked_run(run_id)
        if previous and previous.status in ['succeeded', 'partial', 'failed'] and previous.result_hash:
            if str(previous.lease_token) == request.headers.get('X-Lease-Token') and previous.result_hash == payload_hash:
                return Response(_run(previous))
            raise JobConflict('This run already completed with another result.')
        run = leased_run(run_id, request.headers.get('X-Lease-Token'))
        versions = {str(v.id): v for v in run.versions.select_related('document').all()}
        if any(not isinstance(item, dict) for item in results) or len({str(item.get('version_id')) for item in results}) != len(results) or set(versions) != {str(item.get('version_id')) for item in results}:
            raise ValidationError('Completion must cover exactly the versions owned by this run.')
        checked = [(versions[str(item['version_id'])], _validate_result(item, versions[str(item['version_id'])])) for item in results]
        retire_ids = []
        if run.source.kind == 'snapshot' and all(state[0] == 'ready' for _, state in checked):
            present = {version.document_id for version, _ in checked}
            live = Document.objects.filter(source=run.source, deleted_at__isnull=True, current_version__isnull=False)
            retire_ids = list(live.exclude(id__in=present).values_list('id', flat=True))
            if retire_ids and len(retire_ids) / live.count() > 0.25:
                raise JobConflict('Snapshot would remove more than 25% of published documents. Review its scope before retrying.')
        now, ready = timezone.now(), 0
        for version, (state, pages, chunks, error) in checked:
            publishable = Document.objects.filter(Q(deleted_at__isnull=True) | Q(deletion_reason='source_missing'), pk=version.document_id)
            if run.source.kind == 'upload':
                # A late retry of a failed older upload cannot replace a newer
                # published upload. Snapshots can intentionally revert content.
                publishable = publishable.exclude(current_version__created_at__gt=version.created_at)
            if version.document.deleted_at and version.document.deletion_reason != 'source_missing':
                continue
            if version.published_at:
                if state != 'ready' or version.pages != pages or version.chunks != chunks:
                    raise JobConflict('Published document versions are immutable.')
                ready += 1
                publishable.update(current_version=version, deleted_at=None, deletion_reason='', updated_at=now)
                continue
            version.status, version.pages, version.chunks, version.error = state, pages, chunks, error
            version.index_generation = generation
            if state == 'ready':
                version.published_at = now
                ready += 1
            version.save()
            if state == 'ready':
                publishable.update(current_version=version, deleted_at=None, deletion_reason='', updated_at=now)
        run.status = 'succeeded' if ready == len(checked) else 'partial' if ready else 'failed'
        run.stage, run.progress, run.finished_at = run.status, 100, now
        run.result_hash = payload_hash
        run.counters = {**run.counters, 'documents': len(checked), 'ready': ready, 'not_ready': len(checked) - ready}
        run.save()
        if run.status == 'succeeded':
            KnowledgeSource.objects.filter(pk=run.source_id).update(last_run_at=now)
            Document.objects.filter(id__in=retire_ids, deleted_at__isnull=True).update(deleted_at=now, deletion_reason='source_missing', updated_at=now)
    return Response(_run(run))


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
@parser_classes([BoundedJSONParser])
def fail_job(request, run_id):
    _worker(request)
    with transaction.atomic():
        run = leased_run(run_id, request.headers.get('X-Lease-Token'), allow_cancel=True)
        run.status = 'cancelled' if run.status == 'cancel_requested' else 'failed'
        run.stage, run.finished_at = run.status, timezone.now()
        # Worker exceptions can contain provider URLs/credentials. Persist a
        # stable error code rather than copying arbitrary exception text.
        code = request.data.get('code', '')
        run.error = f'Processing failed ({code}). Check the document service logs.' if isinstance(code, str) and re.fullmatch('[A-Za-z0-9_]{1,80}', code) else 'Processing did not complete. Check the document service and retry.'
        run.versions.filter(published_at__isnull=True).update(status='failed', error=run.error)
        run.save()
    return Response(_run(run))
