"""Metadata endpoints stay bounded as collections and version histories grow."""
import hashlib

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from knowledge.models import Document, DocumentVersion, KnowledgeSource
from knowledge.tests.test_document_explorer import setup  # shared organization/ACL fixture

pytestmark = pytest.mark.django_db


def get_with_queries(client, url):
    with CaptureQueriesContext(connection) as queries:
        response = client.get(url)
        assert response.status_code == 200, response.data
    return response.data, list(queries)


def add_version(document, number, status='failed', published=False):
    return DocumentVersion.objects.create(
        document=document, content_hash=hashlib.sha256(str(number).encode()).hexdigest(),
        status=status, published_at=timezone.now() if published else None,
        pages=[{'page': 1, 'text': 'Historical text ' * 1000}],
        chunks=[{'id': str(number), 'page': 1, 'text': 'Historical chunk ' * 1000}],
    )


def test_source_acl_queries_do_not_grow_with_collections(setup):
    group = Group.objects.get(name='Company')
    setup['source'].allowed_groups.add(group)
    first, small = get_with_queries(setup['client'], '/api/knowledge/sources/')
    for number in range(20):
        source = KnowledgeSource.objects.create(organization=setup['org'], name=f'Collection {number}', kind='upload')
        source.allowed_users.add(setup['user'])
        source.allowed_groups.add(group)
    KnowledgeSource.objects.create(organization=setup['other_org'], name='Other organization', kind='upload')
    result, large = get_with_queries(setup['client'], '/api/knowledge/sources/')
    assert len(result['results']) == 21
    assert len(small) == len(large) <= 8
    assert next(row for row in result['results'] if row['id'] == first['results'][0]['id']) == first['results'][0]
    for row in result['results']:
        assert setup['user'].id in row['allowed_user_ids']
        assert row['allowed_group_ids'] == [group.id]


def test_document_cards_preserve_latest_status_without_loading_evidence(setup):
    document = Document.objects.create(source=setup['source'], external_id='guide', title='Guide', mime_type='text/plain')
    published = add_version(document, 0, status='ready', published=True)
    document.current_version = published
    document.save()
    _, small = get_with_queries(setup['client'], '/api/knowledge/documents/')
    for number in range(1, 21):
        add_version(document, number)
    result, large = get_with_queries(setup['client'], '/api/knowledge/documents/')
    assert len(small) == len(large) <= 8
    assert result['results'][0]['status'] == 'failed'
    assert result['results'][0]['current_version_id'] == str(published.id)
    assert all('"pages"' not in query['sql'] and '"chunks"' not in query['sql'] for query in large)
    failed = setup['client'].get('/api/knowledge/documents/?status=failed').data
    ready = setup['client'].get('/api/knowledge/documents/?status=ready').data
    assert failed['count'] == 1 and ready['count'] == 0


def test_history_queries_and_permissions_are_independent_of_unpublished_versions(setup):
    document = Document.objects.create(source=setup['source'], external_id='history', title='History', mime_type='text/plain')
    published = add_version(document, 0, status='ready', published=True)
    document.current_version = published
    document.save()
    url = f'/api/knowledge/documents/{document.id}/'
    setup['client'].force_authenticate(setup['user'])
    _, small = get_with_queries(setup['client'], url)
    for number in range(1, 21):
        add_version(document, number)
    result, large = get_with_queries(setup['client'], url)
    assert len(small) == len(large)
    assert [v['id'] for v in result['versions']] == [str(published.id)]
    assert result['pages'] == published.pages
    # Only the selected reader version loads evidence, never the version menu.
    assert sum('"pages"' in query['sql'] for query in large) == 1
    setup['client'].force_authenticate(setup['admin'])
    result, queries = get_with_queries(setup['client'], url)
    assert len(result['versions']) == 21
    assert sum('"pages"' in query['sql'] for query in queries) == 1
