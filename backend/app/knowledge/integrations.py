"""Reader-safe integration content. Credential/settings APIs stay admin-only."""
from collections import OrderedDict

from django.db.models import Count
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from catalog.models import IntegrationSource
from etl.sources.registry import SOURCE_REGISTRY
from .models import ExtractionArtifact, KnowledgeSource
from .permissions import can_manage, request_org, visible_documents, visible_sources


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def content_groups(request):
    org = request_org(request)
    manage = can_manage(org, request.user)
    sources = KnowledgeSource.objects.filter(organization=org) if manage else visible_sources(org, request.user)
    counts = dict(visible_documents(org, request.user).order_by().values('source_id')
                  .annotate(count=Count('id')).values_list('source_id', 'count'))
    groups = OrderedDict()
    for source in sources.select_related('integration').order_by('name', 'id'):
        connection = source.integration
        # An invalid cross-tenant relation must not reveal connection metadata.
        if connection and connection.organization_id == org.pk:
            key, name, provider = f'integration:{connection.pk}', connection.name, connection.source_type
        elif source.import_group:
            key, name, provider = f'import:{source.import_group}', source.import_group, 'snapshot'
        else:
            key = 'uploads' if source.kind == 'upload' else 'imports'
            name, provider = ('Uploaded documents', 'upload') if key == 'uploads' else ('Imported collections', 'snapshot')
        group = groups.setdefault(key, {'id': key, 'name': name, 'provider': provider,
                                       'document_count': 0, 'collections': []})
        count = counts.get(source.pk, 0)
        group['document_count'] += count
        group['collections'].append({
            'id': str(source.pk), 'name': source.name, 'kind': source.kind,
            'document_count': count, 'is_active': source.is_active,
            'refresh_paused': source.refresh_paused, 'pipeline_enabled': source.pipeline_enabled,
            'last_run_at': source.last_run_at,
        })
    return Response({'results': list(groups.values()), 'can_manage': manage})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def connections(request):
    org = request_org(request, manage=True)
    return Response({'results': [
        {'id': row.pk, 'name': row.name, 'provider': row.source_type,
         'capabilities': list(SOURCE_REGISTRY[row.source_type].capabilities) if row.source_type in SOURCE_REGISTRY else []}
        for row in IntegrationSource.objects.filter(organization=org)
    ]})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def indexing_outputs(request):
    org = request_org(request, manage=True)
    # Only metadata: immutable manifests may be large and remain private.
    rows = ExtractionArtifact.objects.filter(source__organization=org).order_by('-created_at').values(
        'id', 'source__name', 'upstream_key', 'profile', 'created_at', 'cancelled_at', 'run_id', 'run__status', 'manifest__error')[:50]
    return Response({'results': [{
        'id': row['id'], 'source_name': row['source__name'], 'upstream_key': row['upstream_key'],
        'profile': row['profile'], 'created_at': row['created_at'], 'run_id': row['run_id'],
        'status': 'cancelled' if row['cancelled_at'] else 'failed' if row['manifest__error'] else row['run__status'] or 'queued',
        'error': row['manifest__error'] or '',
    } for row in rows]})
