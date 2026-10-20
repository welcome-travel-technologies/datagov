"""Machine endpoints under ``internal/``, called with a bearer token and never a session.

The corpus export (contract C2) tells luccy's sync job which DataGov documents it may
copy: the current, ready, published version of every live document of the bound
organization, in its collections every member may read (luccy has no per-document
access). It carries identities only, never document text. Pages are ordered by document
id and continue after an opaque cursor naming the last id served (keyset paging), so a
document added or removed between two pages never shifts or repeats the rest.

``pipeline_version`` is the extraction profile of the connector artifact that produced
the current version (``ExtractionArtifact.profile``, e.g. ``connector-text-v1``). DataGov
keeps no pipeline version for uploads or imported snapshots, so theirs is ``""``.
(``DocumentVersion.index_generation`` is a per-run hash of the engine's results, not a
version of the pipeline, so it is not used.) ``modified`` is when DataGov published
the current version: DataGov does not keep the source system's own edit time.

Every endpoint here is also refused (404) when the request came through a proxy:
``catalog.middleware.InternalEndpointGuardMiddleware``.
"""
import base64
import binascii
import hashlib
import secrets
import uuid

from django.db.models import F
from django.utils import timezone
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from catalog.models import Organization
from knowledge.models import Document

from . import permissions

DEFAULT_LIMIT = 200
MAX_LIMIT = 500
CURSOR_PREFIX = 'd1:'
# The labels knowledge.integrations.content_groups gives collections with no connection
# and no reviewed import group.
UPLOADS_GROUP = 'Uploaded documents'
IMPORTS_GROUP = 'Imported collections'


def not_found():
    return Response({'detail': 'Not found.'}, status=404)


def bearer_denied(request, expected):
    """None when the request carries ``Authorization: Bearer <expected>``, else a 401.

    The comparison is constant-time over the token bytes; ``expected`` must be non-empty.
    """
    header = request.headers.get('Authorization', '')
    given = header[7:] if header.startswith('Bearer ') else ''
    if expected and given and secrets.compare_digest(given.encode('utf-8'), expected.encode('utf-8')):
        return None
    response = Response({'detail': 'Invalid service credentials.'}, status=401)
    response['WWW-Authenticate'] = 'Bearer'
    return response


def _sync_gate(request):
    """``(organization, None)`` for an authorized sync request, else ``(None, response)``.

    404 while the analyzer is off, not set up or has no usable sync token
    (``permissions.token_problem``), or its organization no longer exists; 401 for a
    missing or wrong token.
    """
    token = permissions.service_token('LUCCY_SYNC_TOKEN')
    if not (permissions.enabled() and permissions.configured() and token):
        return None, not_found()
    denied = bearer_denied(request, token)
    if denied:
        return None, denied
    org = Organization.objects.filter(pk=permissions.organization_id()).first()
    if org is None:
        # Never an empty list: a sync job could read that as "delete everything".
        return None, not_found()
    return org, None


def exported_documents(org):
    """The documents luccy may hold for ``org``: live, current, ready and published, in
    active collections every member of the organization may read."""
    return Document.objects.filter(
        source__organization=org, source__is_active=True, source__access='organization',
        deleted_at__isnull=True, current_version__isnull=False, current_version__document_id=F('id'),
        current_version__status='ready', current_version__published_at__isnull=False,
    )


def encode_cursor(document_id):
    return base64.urlsafe_b64encode(f'{CURSOR_PREFIX}{document_id}'.encode()).decode().rstrip('=')


def decode_cursor(cursor):
    """The document id a cursor continues after; ValueError when it is not one of ours."""
    try:
        raw = base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)).decode('ascii')
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise ValueError('Invalid cursor.') from None
    if not raw.startswith(CURSOR_PREFIX):
        raise ValueError('Invalid cursor.')
    try:
        return uuid.UUID(raw[len(CURSOR_PREFIX):])
    except ValueError:
        raise ValueError('Invalid cursor.') from None


def _limit(value):
    if value in (None, ''):
        return DEFAULT_LIMIT
    if not (value.isascii() and value.isdigit()) or not 1 <= int(value) <= MAX_LIMIT:
        raise ValueError(f'The limit must be a whole number from 1 to {MAX_LIMIT}.')
    return int(value)


def _group(row, org_id):
    """The collection's group, by the same explicit provenance as
    ``knowledge.integrations.content_groups``: its connection, else its reviewed import
    group, else uploads or imports. A cross-tenant connection is never named."""
    if row['source__integration_id'] and row['source__integration__organization_id'] == org_id:
        return row['source__integration__name']
    if row['source__import_group']:
        return row['source__import_group']
    return UPLOADS_GROUP if row['source__kind'] == 'upload' else IMPORTS_GROUP


def _document(row, org_id):
    published = row['current_version__published_at']
    return {
        'document_id': str(row['id']),
        'version_id': str(row['current_version_id']),
        'content_hash': row['current_version__content_hash'],
        'pipeline_version': row['current_artifact__profile'] or '',
        'external_id': row['external_id'],
        'title': row['title'],
        'source_type': row['source_type'],
        'source_url': row['source_url'] or None,
        'collection_id': str(row['source_id']),
        'collection_name': row['source__name'],
        'group': _group(row, org_id),
        'modified': published.isoformat() if published else None,
    }


@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
def documents(request):
    org, denied = _sync_gate(request)
    if denied:
        return denied
    try:
        limit = _limit(request.query_params.get('limit'))
        cursor = request.query_params.get('cursor') or ''
        after = decode_cursor(cursor) if cursor else None
    except ValueError as exc:
        return Response({'detail': str(exc)}, status=400)
    exported = exported_documents(org)
    page = exported.order_by('id')
    if after is not None:
        page = page.filter(id__gt=after)
    rows = list(page.values(
        'id', 'current_version_id', 'current_version__content_hash', 'current_version__published_at',
        'current_artifact__profile', 'external_id', 'title', 'source_type', 'source_url',
        'source_id', 'source__name', 'source__kind', 'source__import_group', 'source__integration_id',
        'source__integration__organization_id', 'source__integration__name',
    )[:limit + 1])
    more = len(rows) > limit
    rows = rows[:limit]
    return Response({
        'organization_id': str(org.pk),
        'generated_at': timezone.now().isoformat(),
        'total': exported.count(),
        'next_cursor': encode_cursor(rows[-1]['id']) if more else None,
        'documents': [_document(row, org.pk) for row in rows],
    })


@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
def digest(request):
    """A cheap change check: the sha256 of the exported version ids, sorted and joined
    by newlines. It changes exactly when the export's set of versions does."""
    org, denied = _sync_gate(request)
    if denied:
        return denied
    count, value = export_digest(org)
    return Response({'organization_id': str(org.pk), 'count': count, 'digest': value})


def export_digest(org):
    """``(count, sha256)`` of the exported version ids, sorted and newline-joined."""
    versions = sorted(str(v) for v in exported_documents(org).values_list('current_version_id', flat=True))
    return len(versions), hashlib.sha256('\n'.join(versions).encode('utf-8')).hexdigest()
