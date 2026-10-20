"""C2: the document list luccy's sync job reads (bearer token, no document text)."""
import base64
import hashlib
import uuid
from datetime import datetime

import httpx
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from catalog.models import IntegrationSource
from document_analyzer import internal
from knowledge.models import Document, DocumentVersion, ExtractionArtifact, KnowledgeSource

pytestmark = pytest.mark.django_db

URL = '/api/document-analyzer/internal/documents/'
DIGEST_URL = '/api/document-analyzer/internal/documents/digest/'
TOKEN = 'sync-token-for-tests-' + 'k' * 40
SECRET_TEXT = 'PRIVATE BODY TEXT that must never leave DataGov'


@pytest.fixture
def sync(setup, settings):
    settings.LUCCY_SYNC_TOKEN = TOKEN
    return setup


def caller(token=TOKEN):
    client = APIClient()
    if token is not None:
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    return client


def collection(org, name, access='organization', kind='snapshot', **extra):
    return KnowledgeSource.objects.create(organization=org, name=name, kind=kind, access=access, **extra)


def document(source, external_id, text=SECRET_TEXT, pages=None, status='ready', published=True, **extra):
    doc = Document.objects.create(
        source=source, external_id=external_id, title=extra.pop('title', f'Title {external_id}'),
        mime_type='text/plain', source_type=extra.pop('source_type', 'confluence'),
        source_url=extra.pop('source_url', ''), **extra)
    pages = pages if pages is not None else [{'page': 1, 'text': text}]
    version = DocumentVersion.objects.create(
        document=doc, content_hash=hashlib.sha256(f'{external_id}:{text}'.encode()).hexdigest(), status=status,
        published_at=timezone.now() if published else None, pages=pages,
        chunks=[{'id': f'{external_id}:1', 'page': 1, 'text': text, 'section': ''}])
    doc.current_version = version
    doc.save()
    return doc


def fetch_all(limit):
    """Follow every cursor; returns (first page, all documents, number of pages)."""
    first, documents, cursor, pages = None, [], None, 0
    while True:
        params = {'limit': limit, **({'cursor': cursor} if cursor else {})}
        response = caller().get(URL, params)
        assert response.status_code == 200, response.content
        body = response.json()
        first = first or body
        documents += body['documents']
        pages += 1
        cursor = body['next_cursor']
        if not cursor:
            return first, documents, pages


# ---- access ----------------------------------------------------------------------------------


@pytest.mark.parametrize('change', [
    {'LUCCY_ENABLED': False},
    {'LUCCY_API_KEY': ''},
    {'LUCCY_ORGANIZATION_ID': ''},
    {'LUCCY_SYNC_TOKEN': ''},
])
@pytest.mark.parametrize('url', [URL, DIGEST_URL])
def test_off_or_unconfigured_is_404_even_with_the_token(sync, settings, change, url):
    for key, value in change.items():
        setattr(settings, key, value)
    response = caller().get(url)
    assert response.status_code == 404
    assert response.json() == {'detail': 'Not found.'}


@pytest.mark.parametrize('change', [
    {'LUCCY_SYNC_TOKEN': 's' * 31},
    {'LUCCY_SYNC_TOKEN': 'test-luccy-key-with-no-real-access'},  # the API key
    {'LUCCY_EVAL_TOKEN': TOKEN},
])
@pytest.mark.parametrize('url', [URL, DIGEST_URL])
def test_a_weak_or_shared_sync_token_closes_only_the_sync(sync, settings, luccy, change, url):
    for key, value in change.items():
        setattr(settings, key, value)
    assert caller(settings.LUCCY_SYNC_TOKEN).get(url).status_code == 404
    # The page and the assistant tool depend only on LUCCY_API_KEY.
    client = APIClient()
    client.force_authenticate(sync['reader'])
    luccy['handler'] = lambda request: httpx.Response(200, json={'status': 'ok'})
    assert client.get('/api/document-analyzer/status/').json()['available'] is True


def test_a_bound_organization_that_does_not_exist_is_404(sync, settings):
    settings.LUCCY_ORGANIZATION_ID = '987654321'
    assert caller().get(URL).status_code == 404


@pytest.mark.parametrize('url', [URL, DIGEST_URL])
@pytest.mark.parametrize('header', [None, 'Bearer wrong-token', f'Basic {TOKEN}', f'Bearer {TOKEN}x', 'Bearer ', f'bearer {TOKEN}'])
def test_missing_or_wrong_token_is_401(sync, url, header):
    client = APIClient()
    if header is not None:
        client.credentials(HTTP_AUTHORIZATION=header)
    response = client.get(url)
    assert response.status_code == 401
    assert response['WWW-Authenticate'] == 'Bearer'
    assert TOKEN not in response.content.decode()


def test_a_signed_in_admin_without_the_token_gets_nothing(sync):
    client = APIClient()
    client.force_authenticate(sync['admin'])
    assert client.get(URL).status_code == 401


def test_token_check_is_constant_time(sync, monkeypatch):
    seen = []
    real = internal.secrets.compare_digest
    monkeypatch.setattr(internal.secrets, 'compare_digest', lambda a, b: seen.append((a, b)) or real(a, b))
    assert caller('nope').get(URL).status_code == 401
    assert seen == [(b'nope', TOKEN.encode())]


# ---- what is listed ----------------------------------------------------------------------------


@pytest.fixture
def corpus(sync):
    org, other = sync['org'], sync['other']
    open_source = collection(org, 'Company wiki', import_group='Atlassian export')
    restricted = collection(org, 'Board papers', access='restricted')
    inactive = collection(org, 'Retired', is_active=False)
    foreign = collection(other, 'Elsewhere')
    docs = {
        'open': document(open_source, 'confluence:1'),
        'restricted': document(restricted, 'confluence:2'),
        'inactive': document(inactive, 'confluence:3'),
        'foreign': document(foreign, 'confluence:4'),
        'deleted': document(open_source, 'confluence:5', deleted_at=timezone.now(), deletion_reason='manual'),
        'processing': document(open_source, 'confluence:6', status='processing', published=False),
    }
    Document.objects.create(source=open_source, external_id='no-version', title='Never published', mime_type='text/plain')
    return {**sync, 'docs': docs, 'open_source': open_source, 'restricted_source': restricted}


def ids(documents):
    return {doc['document_id'] for doc in documents}


def test_only_live_ready_documents_of_open_collections_are_listed(corpus, settings):
    """Never a restricted collection: luccy has no per-document access. An old
    LUCCY_SYNC_SCOPE=all setting changes nothing."""
    settings.LUCCY_SYNC_SCOPE = 'all'
    body = caller().get(URL).json()
    assert body['organization_id'] == str(corpus['org'].pk)
    assert body['total'] == 1 and body['next_cursor'] is None
    assert ids(body['documents']) == {str(corpus['docs']['open'].pk)}
    assert 'scope' not in body


def test_document_fields_and_no_text(corpus):
    connection_row = IntegrationSource.objects.create(organization=corpus['org'], name='dbt repository', source_type='dbt')
    connected = collection(corpus['org'], 'dbt models', integration=connection_row)
    artifact = ExtractionArtifact.objects.create(source=connected, upstream_key='source-run:1',
                                                 profile='connector-text-v1', manifest={}, manifest_hash='')
    doc = document(connected, 'models/orders.sql', source_type='dbt', source_url='https://github.com/acme/dbt/blob/main/models/orders.sql',
                   pages=[{'page': 2, 'text': 'é' * 7}, {'page': 1, 'text': SECRET_TEXT}], current_artifact=artifact)
    body = caller().get(URL).json()
    row = next(item for item in body['documents'] if item['document_id'] == str(doc.pk))
    version = doc.current_version
    assert row == {
        'document_id': str(doc.pk), 'version_id': str(version.pk), 'content_hash': version.content_hash,
        'pipeline_version': 'connector-text-v1', 'external_id': 'models/orders.sql', 'title': 'Title models/orders.sql',
        'source_type': 'dbt', 'source_url': 'https://github.com/acme/dbt/blob/main/models/orders.sql',
        'collection_id': str(connected.pk), 'collection_name': 'dbt models', 'group': 'dbt repository',
        'modified': version.published_at.isoformat(),
    }
    plain = next(item for item in body['documents'] if item['document_id'] == str(corpus['docs']['open'].pk))
    assert plain['pipeline_version'] == '' and plain['source_url'] is None
    assert plain['group'] == 'Atlassian export'
    assert SECRET_TEXT not in caller().get(URL).content.decode()
    assert datetime.fromisoformat(body['generated_at']).tzinfo is not None


@pytest.mark.parametrize('kind, import_group, expected', [
    ('upload', '', internal.UPLOADS_GROUP),
    ('snapshot', '', internal.IMPORTS_GROUP),
    ('upload', 'Reviewed label', 'Reviewed label'),
])
def test_group_follows_the_integrations_grouping(sync, kind, import_group, expected):
    source = collection(sync['org'], f'{kind}-{import_group}', kind=kind, import_group=import_group)
    document(source, 'doc')
    assert caller().get(URL).json()['documents'][0]['group'] == expected


def test_a_cross_tenant_connection_is_never_named(sync):
    foreign = IntegrationSource.objects.create(organization=sync['other'], name='SECRET other connection', source_type='dbt')
    source = collection(sync['org'], 'Odd', kind='snapshot', integration=foreign)
    document(source, 'doc')
    body = caller().get(URL).json()
    assert body['documents'][0]['group'] == internal.IMPORTS_GROUP
    assert 'SECRET' not in str(body)


# ---- paging ----------------------------------------------------------------------------------


@pytest.fixture
def many(sync):
    source = collection(sync['org'], 'Wiki')
    return [document(source, f'confluence:{n}') for n in range(7)]


def test_cursor_pages_cover_every_document_once_in_id_order(many):
    first, documents, pages = fetch_all(limit=3)
    assert pages == 3 and first['total'] == 7
    assert [doc['document_id'] for doc in documents] == sorted(str(doc.pk) for doc in many)


def test_default_and_largest_limits(many):
    assert len(caller().get(URL).json()['documents']) == 7
    assert caller().get(URL, {'limit': 500}).status_code == 200


def test_a_page_ending_exactly_at_the_end_has_no_cursor(many):
    body = caller().get(URL, {'limit': 7}).json()
    assert len(body['documents']) == 7 and body['next_cursor'] is None


def test_cursor_is_stable_when_documents_change_between_pages(many):
    first = caller().get(URL, {'limit': 3}).json()
    cursor = first['next_cursor']
    expected = caller().get(URL, {'limit': 3, 'cursor': cursor}).json()['documents']
    source = KnowledgeSource.objects.get(name='Wiki')
    # Added before the cursor, and one served on the first page removed: the next
    # page neither shifts nor repeats.
    document(source, 'sorts-first', id=uuid.UUID(int=0))
    Document.objects.filter(pk=first['documents'][0]['document_id']).update(deleted_at=timezone.now())
    again = caller().get(URL, {'limit': 3, 'cursor': cursor}).json()
    assert again['documents'] == expected
    assert again['total'] == 7
    # Added after it: served later, once.
    last = document(source, 'sorts-last', id=uuid.UUID(int=2 ** 128 - 1))
    _first, documents, _pages = fetch_all(limit=3)
    assert [doc['document_id'] for doc in documents].count(str(last.pk)) == 1
    assert documents[-1]['document_id'] == str(last.pk)


def test_cursor_is_opaque_and_validated(many):
    cursor = caller().get(URL, {'limit': 2}).json()['next_cursor']
    with pytest.raises(ValueError):
        uuid.UUID(cursor)
    assert internal.decode_cursor(cursor) == uuid.UUID(caller().get(URL, {'limit': 2}).json()['documents'][-1]['document_id'])
    for bad in ['not-a-cursor', base64.urlsafe_b64encode(b'd1:not-a-uuid').decode(),
                base64.urlsafe_b64encode(f'x9:{uuid.uuid4()}'.encode()).decode(), '%%%', 'Zm9v']:
        response = caller().get(URL, {'cursor': bad})
        assert response.status_code == 400 and response.json() == {'detail': 'Invalid cursor.'}


@pytest.mark.parametrize('limit', ['0', '501', '-1', 'abc', '2.5', '²'])
def test_invalid_limits_are_400(many, limit):
    response = caller().get(URL, {'limit': limit})
    assert response.status_code == 400
    assert 'limit' in response.json()['detail']


def test_queries_do_not_grow_with_the_page(sync):
    source = collection(sync['org'], 'Big')
    connection_row = IntegrationSource.objects.create(organization=sync['org'], name='Conn', source_type='dbt')
    connected = collection(sync['org'], 'Connected', integration=connection_row)
    artifact = ExtractionArtifact.objects.create(source=connected, upstream_key='k', profile='connector-text-v1',
                                                 manifest={}, manifest_hash='')
    for n in range(3):
        document(source, f'a{n}')
        document(connected, f'b{n}', current_artifact=artifact)
    with CaptureQueriesContext(connection) as small:
        assert len(caller().get(URL).json()['documents']) == 6
    for n in range(3, 30):
        document(source, f'a{n}')
        document(connected, f'b{n}', current_artifact=artifact)
    with CaptureQueriesContext(connection) as large:
        assert len(caller().get(URL).json()['documents']) == 60
    assert len(large.captured_queries) == len(small.captured_queries) <= 3
    with CaptureQueriesContext(connection) as digest:
        assert caller().get(DIGEST_URL).json()['count'] == 60
    assert len(digest.captured_queries) <= 2


# ---- digest ----------------------------------------------------------------------------------


def test_digest_matches_the_export_and_changes_with_it(corpus, settings):
    body = caller().get(DIGEST_URL).json()
    versions = sorted(str(doc['version_id']) for doc in caller().get(URL).json()['documents'])
    assert body == {'organization_id': str(corpus['org'].pk), 'count': 1,
                    'digest': hashlib.sha256('\n'.join(versions).encode()).hexdigest()}
    # A new current version of a listed document changes the digest.
    doc = corpus['docs']['open']
    newer = DocumentVersion.objects.create(document=doc, content_hash='e' * 64, status='ready',
                                           published_at=timezone.now(), pages=[{'page': 1, 'text': 'v2'}])
    doc.current_version = newer
    doc.save()
    assert caller().get(DIGEST_URL).json()['digest'] == hashlib.sha256(str(newer.pk).encode()).hexdigest()


def test_digest_of_nothing_is_the_hash_of_an_empty_string(sync):
    assert caller().get(DIGEST_URL).json() == {'organization_id': str(sync['org'].pk), 'count': 0,
                                               'digest': hashlib.sha256(b'').hexdigest()}


# ---- proxied requests (defense in depth behind nginx) ----------------------------------------


PROXY_HEADERS = {
    'X-Forwarded-For': 'HTTP_X_FORWARDED_FOR', 'X-Real-IP': 'HTTP_X_REAL_IP', 'Forwarded': 'HTTP_FORWARDED',
    'X-Forwarded-Host': 'HTTP_X_FORWARDED_HOST', 'X-Forwarded-Proto': 'HTTP_X_FORWARDED_PROTO',
}
WORKER_TOKEN = 'worker-token-for-tests-' + 'w' * 40


@pytest.fixture
def worker(sync, settings):
    settings.DOCUMENT_EXPLORER_ENABLED = True
    settings.DOCUMENT_EXPLORER_WORKER_TOKEN = WORKER_TOKEN
    return sync


@pytest.mark.parametrize('header', sorted(PROXY_HEADERS))
@pytest.mark.parametrize('method, url, token', [
    ('get', URL, TOKEN), ('get', DIGEST_URL, TOKEN),
    ('post', '/api/document-analyzer/internal/eval/answer/', TOKEN),
    ('post', '/api/knowledge/internal/jobs/claim/', WORKER_TOKEN),
])
def test_internal_endpoints_refuse_anything_that_came_through_a_proxy(worker, header, method, url, token, caplog):
    client = caller(token)
    response = getattr(client, method)(url, {'worker_id': 'w1'} if method == 'post' else None, format='json',
                                       **{PROXY_HEADERS[header]: '203.0.113.7'})
    assert response.status_code == 404 and response.json() == {'detail': 'Not found.'}
    # One line naming the area only: never the path (it holds job and run ids) or a token.
    area = 'knowledge' if '/knowledge/' in url else 'document-analyzer'
    ours = [record.getMessage() for record in caplog.records if record.name == 'catalog.middleware']
    assert ours == [f'Refused a proxied request for the internal {area} endpoints.']
    assert token not in caplog.text


def test_direct_callers_still_reach_the_internal_endpoints(worker):
    assert caller().get(URL).status_code == 200
    assert caller(WORKER_TOKEN).post('/api/knowledge/internal/jobs/claim/', {'worker_id': 'w1'},
                                     format='json').json() == {'job': None}


@pytest.mark.parametrize('path', [
    '/api/document-analyzer/INTERNAL/documents/', '/API/Document-Analyzer/internal/documents/',
    '/api//document-analyzer//internal/documents/', '/api/document-analyzer/internal',
    '/api/knowledge/Internal/jobs/claim/',
])
def test_every_spelling_of_the_internal_paths_is_refused_when_proxied(worker, path):
    assert caller().get(path, HTTP_X_FORWARDED_FOR='203.0.113.7').status_code == 404


def test_proxy_settings_do_not_change_the_refusal(worker, settings):
    """Django's own proxy settings read these headers for other purposes; the guard
    reads the raw request, so they neither open nor break it."""
    settings.USE_X_FORWARDED_HOST = True
    settings.USE_X_FORWARDED_PORT = True
    settings.SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    settings.ALLOWED_HOSTS = ['*']
    assert caller().get(URL, HTTP_X_FORWARDED_HOST='web:8000').status_code == 404
    assert caller().get(URL, HTTP_X_FORWARDED_PROTO='https').status_code == 404
    assert caller().get(URL).status_code == 200


def test_the_guard_runs_first(settings):
    assert settings.MIDDLEWARE[0] == 'catalog.middleware.InternalEndpointGuardMiddleware'


def test_other_api_paths_are_not_affected_by_proxy_headers(sync, luccy):
    client = APIClient()
    client.force_authenticate(sync['reader'])
    assert client.get('/api/document-analyzer/status/', HTTP_X_FORWARDED_FOR='203.0.113.7').status_code == 200


def test_there_is_no_sync_completion_report(sync):
    response = caller().post('/api/document-analyzer/internal/sync/complete/', {}, format='json')
    assert response.status_code == 404
