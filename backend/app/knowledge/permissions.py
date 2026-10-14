from urllib.parse import urlencode

from django.conf import settings
from django.db.models import Q
from rest_framework.exceptions import NotFound, PermissionDenied

from catalog.models import OrganizationMembership
from .models import Document, DocumentVersion, KnowledgeSource


def enabled():
    return bool(getattr(settings, 'DOCUMENT_EXPLORER_ENABLED', False))


def can_read(org, user):
    if not enabled() or org is None or not getattr(user, 'is_authenticated', False) or not user.is_active:
        return False
    membership = OrganizationMembership.objects.filter(organization=org, user=user, user__is_active=True).first()
    return bool(membership and (membership.is_admin or user.groups.filter(name='Company').exists()))


def can_manage(org, user):
    return bool(can_read(org, user) and OrganizationMembership.objects.filter(organization=org, user=user, is_admin=True).exists())


def request_org(request, manage=False):
    if not enabled():
        raise NotFound('Document Explorer is not enabled.')
    memberships = list(OrganizationMembership.objects.filter(user=request.user).select_related('organization')[:2])
    if len(memberships) != 1:
        raise PermissionDenied('Document Explorer requires one unambiguous organization membership.')
    org = memberships[0].organization
    if not (can_manage(org, request.user) if manage else can_read(org, request.user)):
        raise PermissionDenied('You do not have access to Document Explorer.')
    return org


def visible_sources(org, user):
    if not can_read(org, user):
        return KnowledgeSource.objects.none()
    return KnowledgeSource.objects.filter(organization=org, is_active=True).filter(
        Q(access='organization') | Q(allowed_users=user) | Q(allowed_groups__in=user.groups.all())
    ).distinct()


def visible_documents(org, user):
    return Document.objects.filter(source__in=visible_sources(org, user), deleted_at__isnull=True)


def document_url(document_id, version_id, page=None, chunk_id=None):
    query = {'version': str(version_id)}
    if page is not None:
        query['page'] = str(page)
    if chunk_id:
        query['chunk'] = str(chunk_id)
    return f'/knowledge/documents/{document_id}?{urlencode(query)}'


def filter_citations(org, user, citations):
    """Rebuild citations from allowed immutable records, including historical versions.

    Current source ACL and tombstones always win over saved answer metadata.
    Never echo an engine/model-provided URL, title, excerpt or page unverified.
    """
    if not isinstance(citations, list) or not can_read(org, user):
        return []
    from uuid import UUID
    ids = []
    for citation in citations[:100]:
        try:
            ids.append(UUID(str(citation['version_id'])))
        except (ValueError, TypeError, KeyError):
            continue
    versions = {str(v.id): v for v in DocumentVersion.objects.filter(
        id__in=ids, document__in=visible_documents(org, user), status='ready', published_at__isnull=False,
    ).select_related('document')}
    result, seen = [], set()
    for citation in citations[:100]:
        if not isinstance(citation, dict):
            continue
        version = versions.get(str(citation.get('version_id', '')))
        if not version or str(version.document_id) != str(citation.get('document_id', '')):
            continue
        chunk_id = str(citation.get('chunk_id', ''))
        chunk = next((c for c in version.chunks if str(c.get('id')) == chunk_id), None)
        key = (str(version.id), chunk_id)
        if chunk is None or key in seen:
            continue
        seen.add(key)
        doc = version.document
        result.append({
            'citation_id': f'c{len(result) + 1}', 'document_id': str(doc.id),
            'version_id': str(version.id), 'chunk_id': chunk_id, 'title': doc.title,
            'page': chunk.get('page'), 'section': chunk.get('section', ''),
            'excerpt': chunk.get('text', '')[:1600], 'source_type': doc.source_type,
            'source_url': doc.source_url or None,
            'datagov_url': document_url(doc.id, version.id, chunk.get('page'), chunk_id),
        })
    return result
