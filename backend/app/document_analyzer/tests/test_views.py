import hashlib
import hmac
import json
import logging

import httpx
import pytest
from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.middleware.csrf import _get_new_csrf_string
from django.test import AsyncClient, Client
from rest_framework.test import APIClient

from django.conf import settings as django_settings

from document_analyzer import client as luccy_client
from document_analyzer import permissions

pytestmark = pytest.mark.django_db

BASE = '/api/document-analyzer'
EVENTS = [
    b'event: stage\r\nid: 0\r\ndata: {"stage": "starting", "job": "job-1"}\r\n\r\n',
    b': ping\r\n\r\n',
    b'event: answer\r\nid: 1\r\ndata: {"answer": "Refunds take five days.", "early": true}\r\n\r\n',
    b'event: done\r\nid: 2\r\ndata: {"answer": "Refunds take five days.", "log": "0a1b2c3d"}\r\n\r\n',
]


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


def sse_reply(request, events=EVENTS):
    return httpx.Response(200, headers={'content-type': 'text/event-stream; charset=utf-8'}, stream=Chunks(events))


def call(async_client, method, path, body=None, headers=None):
    """Drive the async views through Django's AsyncClient; returns (response, all bytes)."""
    async def go():
        if method == 'POST':
            data = body if isinstance(body, (str, bytes)) else json.dumps(body)
            response = await async_client.post(path, data=data, content_type='application/json', headers=headers)
        else:
            response = await async_client.get(path, headers=headers)
        if response.streaming:
            return response, b''.join([chunk async for chunk in response.streaming_content])
        return response, response.content
    return async_to_sync(go)()


def logged_in(user, **kwargs):
    async_client = AsyncClient(**kwargs)
    async_client.force_login(user)
    return async_client


def api(user):
    api_client = APIClient()
    api_client.force_authenticate(user)
    return api_client


# ---- access ----------------------------------------------------------------------------------


@pytest.mark.parametrize('change', [
    {'LUCCY_ENABLED': False},
    {'LUCCY_API_KEY': ''},
    {'LUCCY_API_URL': 'ftp://luccy'},
    {'LUCCY_ORGANIZATION_ID': ''},
])
def test_disabled_or_unconfigured_is_404_everywhere(setup, luccy, settings, change):
    for key, value in change.items():
        setattr(settings, key, value)
    luccy['handler'] = sse_reply
    reader = setup['reader']
    response, _ = call(logged_in(reader), 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?'})
    assert response.status_code == 404
    assert json.loads(response.content) == {'detail': 'Not found.'}
    assert call(logged_in(reader), 'GET', f'{BASE}/ask/stream/job-1/')[0].status_code == 404
    assert api(reader).get(f'{BASE}/documents/conf-1/').status_code == 404
    assert api(reader).post(f'{BASE}/feedback/', {'answer_id': '0a1b2c3d', 'verdict': 'right'}, format='json').status_code == 404
    assert luccy['requests'] == []


@pytest.mark.parametrize('who', ['outsider', 'superuser'])
def test_other_organization_and_unbound_superuser_get_404(setup, luccy, who):
    luccy['handler'] = sse_reply
    user = setup[who]
    assert call(logged_in(user), 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?'})[0].status_code == 404
    assert api(user).get(f'{BASE}/documents/conf-1/').status_code == 404
    assert luccy['requests'] == []


def test_member_without_company_access_gets_403(setup, luccy):
    luccy['handler'] = sse_reply
    response, content = call(logged_in(setup['plain']), 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?'})
    assert response.status_code == 403
    assert 'Company access' in json.loads(content)['detail']
    assert api(setup['plain']).get(f'{BASE}/documents/conf-1/').status_code == 403
    assert api(setup['plain']).post(f'{BASE}/feedback/', {'answer_id': '0a1b2c3d', 'verdict': 'right'}, format='json').status_code == 403
    assert luccy['requests'] == []


def test_unauthenticated_stream_is_401(setup, luccy):
    response, content = call(AsyncClient(), 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?'})
    assert response.status_code == 401
    assert call(AsyncClient(), 'GET', f'{BASE}/ask/stream/job-1/')[0].status_code == 401
    assert luccy['requests'] == []


def test_org_admin_without_company_group_may_ask(setup, luccy):
    luccy['handler'] = sse_reply
    assert call(logged_in(setup['admin']), 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?'})[0].status_code == 200


def test_bound_superuser_member_needs_company_access_like_anyone(setup, luccy):
    from catalog.models import OrganizationMembership
    membership = OrganizationMembership.objects.create(organization=setup['org'], user=setup['superuser'])
    luccy['handler'] = sse_reply
    assert call(logged_in(setup['superuser']), 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?'})[0].status_code == 403
    assert luccy['requests'] == []
    membership.is_admin = True
    membership.save(update_fields=['is_admin'])
    assert call(logged_in(setup['superuser']), 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?'})[0].status_code == 200


def test_wrong_method_is_405(setup, luccy):
    assert call(logged_in(setup['reader']), 'GET', f'{BASE}/ask/stream/')[0].status_code == 405
    assert call(logged_in(setup['reader']), 'POST', f'{BASE}/ask/stream/job-1/', {})[0].status_code == 405


# ---- asking ----------------------------------------------------------------------------------


def test_stream_passes_luccy_bytes_through_with_sse_headers(setup, luccy):
    luccy['handler'] = sse_reply
    reader = setup['reader']
    body = {'question': 'How do refunds work?', 'conversation': 'thread_1-a', 'sources': ['jira', 'drive'],
            'style': 'plain', 'history': [{'who': 'you', 'text': 'Hello', 'extra': 'dropped'}, {'who': 'luccy', 'text': 'Hi'}],
            'unknown': 'not forwarded'}
    response, content = call(logged_in(reader), 'POST', f'{BASE}/ask/stream/', body)
    assert response.status_code == 200
    assert response.streaming and response.is_async  # an async iterator: never buffered under ASGI
    assert content == b''.join(EVENTS)
    assert response['Content-Type'] == 'text/event-stream; charset=utf-8'
    assert response['Cache-Control'] == 'no-cache, no-transform'
    assert response['X-Accel-Buffering'] == 'no'
    [upstream] = luccy['requests']
    assert upstream.method == 'POST'
    assert str(upstream.url) == 'http://luccy:8787/ask/stream'
    assert upstream.headers['authorization'] == 'Bearer test-luccy-key-with-no-real-access'
    assert upstream.headers['remote-user'] == f'datagov-{reader.pk}'
    assert upstream.headers['accept-encoding'] == 'identity'
    assert json.loads(upstream.content) == {
        'question': 'How do refunds work?', 'conversation': page_id(reader, 'thread_1-a'), 'sources': ['jira', 'drive'],
        'style': 'plain', 'history': [{'who': 'you', 'text': 'Hello'}, {'who': 'luccy', 'text': 'Hi'}],
    }


def page_id(user, client_id):
    digest = hmac.new(django_settings.SECRET_KEY.encode(), f'page:{user.pk}:{client_id}'.encode(), hashlib.sha256)
    return 'dgpage-' + digest.hexdigest()[:40]


def test_page_conversations_are_namespaced_per_user(setup, luccy):
    """luccy keys a thread's memory by conversation id alone: the same client id from two
    people must be two threads, and nobody can name a DataGov-made id."""
    luccy['handler'] = sse_reply
    reader, admin = setup['reader'], setup['admin']
    for user in (reader, reader, admin):
        assert call(logged_in(user), 'POST', f'{BASE}/ask/stream/',
                    {'question': 'Hi?', 'conversation': 'shared-id'})[0].status_code == 200
    sent = [json.loads(request.content)['conversation'] for request in luccy['requests']]
    assert sent == [page_id(reader, 'shared-id'), page_id(reader, 'shared-id'), page_id(admin, 'shared-id')]
    assert sent[0] != sent[2] and all(len(value) <= 64 and 'shared-id' not in value for value in sent)
    # Even the longest client id stays within luccy's 64 characters.
    call(logged_in(reader), 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?', 'conversation': 'c' * 64})
    assert len(json.loads(luccy['requests'][-1].content)['conversation']) == 47


@pytest.mark.parametrize('conversation', ['dgchat-1', 'dgpage-0123456789abcdef', 'DGx', 'dG', 'dg'])
def test_client_conversation_ids_may_not_use_datagovs_namespace(setup, luccy, conversation):
    luccy['handler'] = sse_reply
    response, content = call(logged_in(setup['reader']), 'POST', f'{BASE}/ask/stream/',
                             {'question': 'Hi?', 'conversation': conversation})
    assert response.status_code == 400 and 'reserved' in json.loads(content)['detail']
    assert luccy['requests'] == []


def test_minimal_question_sends_only_the_question(setup, luccy):
    luccy['handler'] = sse_reply
    call(logged_in(setup['reader']), 'POST', f'{BASE}/ask/stream/', {'question': 'Why?', 'conversation': ''})
    assert json.loads(luccy['requests'][0].content) == {'question': 'Why?'}


@pytest.mark.parametrize('body', [
    'not json',
    [],
    {},
    {'question': '   '},
    {'question': 7},
    {'question': 'x' * 10001},
    {'question': 'Hi?', 'conversation': 'has space'},
    {'question': 'Hi?', 'conversation': 'c' * 65},
    {'question': 'Hi?', 'sources': []},
    {'question': 'Hi?', 'sources': 'jira'},
    {'question': 'Hi?', 'sources': ['jira', 'slack']},
    {'question': 'Hi?', 'style': 'fancy'},
    {'question': 'Hi?', 'history': {'who': 'you'}},
    {'question': 'Hi?', 'history': [{'who': 'me', 'text': 'x'}]},
    {'question': 'Hi?', 'history': [{'who': 'you', 'text': 3}]},
])
def test_invalid_questions_are_400_and_never_reach_luccy(setup, luccy, body):
    luccy['handler'] = sse_reply
    response, content = call(logged_in(setup['reader']), 'POST', f'{BASE}/ask/stream/', body)
    assert response.status_code == 400
    assert isinstance(json.loads(content)['detail'], str)
    assert luccy['requests'] == []


def test_question_limits_are_inclusive(setup, luccy):
    luccy['handler'] = sse_reply
    body = {'question': 'x' * 10000, 'conversation': 'c' * 64,
            'history': [{'who': 'you', 'text': 'x' * 16000}] * 4}
    assert call(logged_in(setup['reader']), 'POST', f'{BASE}/ask/stream/', body)[0].status_code == 200


@pytest.mark.parametrize('history, expected', [
    # Too many turns: the last 50.
    ([{'who': 'you', 'text': str(n)} for n in range(60)], [{'who': 'you', 'text': str(n)} for n in range(10, 60)]),
    # A turn too long: cut to 16,000 characters.
    ([{'who': 'luccy', 'text': 'y' * 16001}], [{'who': 'luccy', 'text': 'y' * 16000}]),
    # Too much in all: the oldest dropped until 64,000 characters remain.
    ([{'who': 'you', 'text': str(n) * 16000} for n in range(5)], [{'who': 'you', 'text': str(n) * 16000} for n in range(1, 5)]),
])
def test_over_long_history_is_trimmed_not_refused(setup, luccy, history, expected):
    luccy['handler'] = sse_reply
    response, _content = call(logged_in(setup['reader']), 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?', 'history': history})
    assert response.status_code == 200
    assert json.loads(luccy['requests'][0].content)['history'] == expected


@pytest.mark.parametrize('status, error, expected_status, expected_detail', [
    (429, 'The daily spending cap is reached.', 429, 'The daily spending cap is reached.'),
    (503, 'Luccy is busy answering other questions.', 503, 'Luccy is busy answering other questions.'),
    (400, 'unknown source: slack', 400, 'unknown source: slack'),
    (503, 'x' * 400, 503, 'x' * 300),
    (500, 'Traceback: secret internals', 502, luccy_client.DEFAULT_ERRORS[502]),
    (401, 'bad key', 502, luccy_client.DEFAULT_ERRORS[502]),
    (404, 'not here', 502, luccy_client.DEFAULT_ERRORS[502]),
    (504, 'slow', 504, luccy_client.DEFAULT_ERRORS[504]),
])
def test_upstream_errors_map_to_json_before_streaming(setup, luccy, status, error, expected_status, expected_detail):
    luccy['handler'] = lambda request: httpx.Response(status, json={'error': error}, headers={'Retry-After': '30'})
    response, content = call(logged_in(setup['reader']), 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?'})
    assert response.status_code == expected_status
    assert not response.streaming
    assert json.loads(content) == {'detail': expected_detail}
    if expected_status in (429, 503):
        assert response['Retry-After'] == '30'
    else:
        assert not response.has_header('Retry-After')


def test_upstream_failures_are_logged_without_bodies_or_ids(setup, luccy, caplog):
    caplog.set_level(logging.WARNING, logger='document_analyzer')
    luccy['handler'] = lambda request: httpx.Response(401, json={'error': 'SECRET bad key'})
    call(logged_in(setup['reader']), 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?'})
    api(setup['reader']).get(f'{BASE}/documents/conf:secret-page/')
    permissions.remember_job('job-1', setup['reader'])
    call(logged_in(setup['reader']), 'GET', f'{BASE}/ask/stream/job-1/')
    luccy['handler'] = lambda request: (_ for _ in ()).throw(httpx.ConnectError('down'))
    api(setup['reader']).post(f'{BASE}/feedback/', {'answer_id': '0a1b2c3d', 'verdict': 'right'}, format='json')
    # Django's own request log still names the URL, as the access log does.
    ours = [record for record in caplog.records if record.name.startswith('document_analyzer')]
    assert [record.getMessage() for record in ours] == [
        'Document Analyzer call POST /ask/stream failed: HTTP 401.',
        'Document Analyzer call GET /documents/<id> failed: HTTP 401.',
        'Document Analyzer call GET /ask/stream/<id> failed: HTTP 401.',
        'Document Analyzer call POST /feedback failed: ConnectError.',
    ]
    assert all(record.levelno == logging.WARNING for record in ours)
    assert 'SECRET' not in caplog.text


def test_upstream_reply_without_event_stream_is_502(setup, luccy):
    luccy['handler'] = lambda request: httpx.Response(200, json={'answer': 'not a stream'})
    response, content = call(logged_in(setup['reader']), 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?'})
    assert response.status_code == 502
    assert b'not a stream' not in content


def test_redirects_are_not_followed(setup, luccy):
    luccy['handler'] = lambda request: httpx.Response(302, headers={'Location': 'http://elsewhere/'})
    response, _ = call(logged_in(setup['reader']), 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?'})
    assert response.status_code == 502
    assert len(luccy['requests']) == 1


@pytest.mark.parametrize('exc, expected', [
    (httpx.ReadTimeout('slow'), 504), (httpx.ConnectError('down'), 503), (httpx.RemoteProtocolError('odd'), 502),
])
def test_transport_failures_are_mapped(setup, luccy, exc, expected):
    def fail(request):
        raise exc
    luccy['handler'] = fail
    response, content = call(logged_in(setup['reader']), 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?'})
    assert response.status_code == expected
    assert json.loads(content) == {'detail': luccy_client.DEFAULT_ERRORS[expected]}


def test_client_options_ignore_environment_and_redirects():
    options = luccy_client.options(330)
    assert options['trust_env'] is False and options['follow_redirects'] is False
    assert options['timeout'].read == 330 and options['timeout'].connect == 10


# ---- re-attach -------------------------------------------------------------------------------


def test_reattach_forwards_last_event_id(setup, luccy):
    luccy['handler'] = sse_reply
    reader = setup['reader']
    permissions.remember_job('job-1', reader)
    response, content = call(logged_in(reader), 'GET', f'{BASE}/ask/stream/job-1/', headers={'Last-Event-ID': '4'})
    assert response.status_code == 200 and content == b''.join(EVENTS)
    assert response['X-Accel-Buffering'] == 'no'
    [upstream] = luccy['requests']
    assert upstream.method == 'GET'
    assert str(upstream.url) == 'http://luccy:8787/ask/stream/job-1'
    assert upstream.headers['last-event-id'] == '4'
    assert upstream.headers['remote-user'] == f'datagov-{reader.pk}'


def test_reattach_drops_a_malformed_last_event_id(setup, luccy):
    luccy['handler'] = sse_reply
    permissions.remember_job('job-1', setup['reader'])
    call(logged_in(setup['reader']), 'GET', f'{BASE}/ask/stream/job-1/', headers={'Last-Event-ID': '4x'})
    call(logged_in(setup['reader']), 'GET', f'{BASE}/ask/stream/job-1/', headers={'Last-Event-ID': 'abc'})
    assert all('last-event-id' not in request.headers for request in luccy['requests'])


def test_reattach_unknown_job_is_404(setup, luccy):
    luccy['handler'] = lambda request: httpx.Response(404, json={'error': 'unknown job'})
    permissions.remember_job('job-1', setup['reader'])
    response, content = call(logged_in(setup['reader']), 'GET', f'{BASE}/ask/stream/job-1/')
    assert response.status_code == 404
    assert json.loads(content) == {'detail': 'Not found.'}
    assert len(luccy['requests']) == 1


def test_only_the_asker_may_reattach(setup, luccy):
    luccy['handler'] = sse_reply
    reader, admin = setup['reader'], setup['admin']
    assert call(logged_in(reader), 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?'})[0].status_code == 200
    # Another person with access, holding the job id, gets nothing and luccy is not asked.
    response, content = call(logged_in(admin), 'GET', f'{BASE}/ask/stream/job-1/')
    assert response.status_code == 404 and json.loads(content) == {'detail': 'Not found.'}
    assert call(logged_in(reader), 'GET', f'{BASE}/ask/stream/job-2/')[0].status_code == 404
    assert len(luccy['requests']) == 1
    assert call(logged_in(reader), 'GET', f'{BASE}/ask/stream/job-1/')[0].status_code == 200
    assert len(luccy['requests']) == 2


def test_job_owner_is_found_across_chunk_boundaries(setup, luccy):
    first = EVENTS[0].replace(b'job-1', b'job-2')
    split = [first[:40], first[40:52], first[52:], *EVENTS[1:]]
    luccy['handler'] = lambda request: sse_reply(request, split)
    response, content = call(logged_in(setup['reader']), 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?'})
    assert content == b''.join(split)
    assert permissions.owns_job(setup['reader'], 'job-2')
    assert not permissions.owns_job(setup['admin'], 'job-2')


def test_answer_still_streams_when_the_owner_cannot_be_recorded(setup, luccy, monkeypatch):
    def broken(job, user):
        raise RuntimeError('cache down')
    monkeypatch.setattr(permissions, 'remember_job', broken)
    luccy['handler'] = sse_reply
    response, content = call(logged_in(setup['reader']), 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?'})
    assert response.status_code == 200 and content == b''.join(EVENTS)


def test_reattach_rejects_a_bad_job_id_without_calling_luccy(setup, luccy):
    luccy['handler'] = sse_reply
    assert call(logged_in(setup['reader']), 'GET', f'{BASE}/ask/stream/bad.job/')[0].status_code == 404
    assert call(logged_in(setup['reader']), 'GET', f'{BASE}/ask/stream/{"j" * 65}/')[0].status_code == 404
    assert luccy['requests'] == []


# ---- CSRF and rate limit ---------------------------------------------------------------------


def test_csrf_is_enforced_on_the_stream_post(setup, luccy):
    luccy['handler'] = sse_reply
    guarded = logged_in(setup['reader'], enforce_csrf_checks=True)
    response, _ = call(guarded, 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?'})
    assert response.status_code == 403
    assert luccy['requests'] == []
    token = _get_new_csrf_string()
    guarded.cookies['csrftoken'] = token
    response, _ = call(guarded, 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?'}, headers={'X-CSRFToken': token})
    assert response.status_code == 200
    assert len(luccy['requests']) == 1


def test_hourly_cap_is_per_user(setup, luccy, settings):
    settings.LUCCY_MAX_ASKS_PER_HOUR = 2
    luccy['handler'] = sse_reply
    reader = setup['reader']
    for _ in range(2):
        assert call(logged_in(reader), 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?'})[0].status_code == 200
    response, content = call(logged_in(reader), 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?'})
    assert response.status_code == 429
    assert json.loads(content) == {'detail': permissions.RATE_LIMITED}
    assert len(luccy['requests']) == 2
    # Re-attaching is not a new question, and other people keep their own allowance.
    assert call(logged_in(reader), 'GET', f'{BASE}/ask/stream/job-1/')[0].status_code == 200
    assert call(logged_in(setup['admin']), 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?'})[0].status_code == 200


@pytest.mark.parametrize('first', [
    lambda request: httpx.Response(503, json={'error': 'Luccy is busy.'}),
    lambda request: httpx.Response(429, json={'error': 'The daily spending cap is reached.'}),
    lambda request: httpx.Response(500),
    lambda request: (_ for _ in ()).throw(httpx.ConnectError('down')),
])
def test_a_question_luccy_never_started_does_not_use_the_allowance(setup, luccy, settings, first):
    settings.LUCCY_MAX_ASKS_PER_HOUR = 1
    luccy['handler'] = first
    assert call(logged_in(setup['reader']), 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?'})[0].status_code != 200
    luccy['handler'] = sse_reply
    assert call(logged_in(setup['reader']), 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?'})[0].status_code == 200
    assert call(logged_in(setup['reader']), 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?'})[0].status_code == 429
    assert len(luccy['requests']) == 2


def test_return_ask_is_harmless_without_a_count(setup, settings):
    permissions.return_ask(setup['reader'])  # nothing counted this hour
    settings.LUCCY_MAX_ASKS_PER_HOUR = 0
    permissions.return_ask(setup['reader'])
    settings.LUCCY_MAX_ASKS_PER_HOUR = 1
    assert permissions.take_ask(setup['reader']) and not permissions.take_ask(setup['reader'])


def test_invalid_questions_do_not_use_the_allowance(setup, luccy, settings):
    settings.LUCCY_MAX_ASKS_PER_HOUR = 1
    luccy['handler'] = sse_reply
    for _ in range(3):
        call(logged_in(setup['reader']), 'POST', f'{BASE}/ask/stream/', {'question': ''})
    assert call(logged_in(setup['reader']), 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?'})[0].status_code == 200


def test_cap_survives_a_cache_that_resets_ttl_on_incr(setup, settings, monkeypatch):
    settings.LUCCY_MAX_ASKS_PER_HOUR = 2
    touched = []
    monkeypatch.setattr(permissions.cache, 'touch', lambda key, timeout: touched.append((key, timeout)))
    assert permissions.take_ask(setup['reader']) and permissions.take_ask(setup['reader'])
    assert not permissions.take_ask(setup['reader'])
    assert len(touched) == 2 and all(timeout == 3600 for _key, timeout in touched)


def test_zero_cap_turns_the_limit_off(setup, settings):
    settings.LUCCY_MAX_ASKS_PER_HOUR = 0
    assert all(permissions.take_ask(setup['reader']) for _ in range(5))


# ---- documents -------------------------------------------------------------------------------


def document_reply(request):
    return httpx.Response(200, json={
        'id': 'conf:123@v2', 'title': 'Refund policy', 'source': 'Confluence', 'modified': '2026-09-01',
        'url': 'https://wiki.example/refunds', 'path': ['Finance', 'Policies'], 'body_html': '<p>Refunds</p>',
        'passages': [{'heading': 'Timing', 'text': 'Five days.'}], 'staff_only': 'dropped',
    })


def test_document_is_proxied_with_an_encoded_id(setup, luccy):
    luccy['handler'] = document_reply
    reader = setup['reader']
    response = api(reader).get(f'{BASE}/documents/conf:123@v2/', {'answer': '0a1b2c3d'})
    assert response.status_code == 200
    assert response.json() == {
        'id': 'conf:123@v2', 'title': 'Refund policy', 'source': 'Confluence', 'modified': '2026-09-01',
        'url': 'https://wiki.example/refunds', 'path': ['Finance', 'Policies'], 'body_html': '<p>Refunds</p>',
        'passages': [{'heading': 'Timing', 'text': 'Five days.'}],
    }
    [upstream] = luccy['requests']
    assert upstream.url.raw_path == b'/documents/conf%3A123%40v2?answer=0a1b2c3d'
    assert upstream.headers['remote-user'] == f'datagov-{reader.pk}'
    assert upstream.headers['authorization'] == 'Bearer test-luccy-key-with-no-real-access'


def test_document_without_answer_and_unsafe_link(setup, luccy):
    luccy['handler'] = lambda request: httpx.Response(200, json={'id': 'wh:1', 'url': 'javascript:alert(1)', 'path': 'x'})
    response = api(setup['reader']).get(f'{BASE}/documents/wh:1/')
    assert response.status_code == 200
    assert response.json()['url'] is None and response.json()['path'] == []
    assert luccy['requests'][0].url.query == b''


@pytest.mark.parametrize('doc_id, params', [
    ('has space', {}), ('x' * 301, {}), ('a+b', {}), ('conf-1', {'answer': 'ABCDEF12'}), ('conf-1', {'answer': '123'}),
    # Dot segments would be resolved by httpx into another luccy path.
    ('..', {}), ('.', {}), ('%2E%2E', {}), ('%2e', {}), ('a..b', {}), ('-conf', {}), ('_conf', {}),
])
def test_document_rejects_bad_ids(setup, luccy, doc_id, params):
    luccy['handler'] = document_reply
    response = api(setup['reader']).get(f'{BASE}/documents/{doc_id}/', params)
    assert response.status_code == 400
    assert luccy['requests'] == []


def test_document_id_limits_are_inclusive(setup, luccy):
    luccy['handler'] = document_reply
    assert api(setup['reader']).get(f'{BASE}/documents/{"x" * 300}/').status_code == 200
    assert api(setup['reader']).get(f'{BASE}/documents/1.x/').status_code == 200
    assert [request.url.raw_path for request in luccy['requests']] == [f'/documents/{"x" * 300}'.encode(), b'/documents/1.x']


def test_unknown_document_is_404(setup, luccy):
    luccy['handler'] = lambda request: httpx.Response(404, json={'error': 'not found'})
    assert api(setup['reader']).get(f'{BASE}/documents/conf-1/').status_code == 404


def test_oversized_document_is_refused(setup, luccy):
    luccy['handler'] = lambda request: httpx.Response(200, content=b'{"body_html": "' + b'x' * (2 * 1024 * 1024) + b'"}')
    response = api(setup['reader']).get(f'{BASE}/documents/conf-1/')
    assert response.status_code == 502
    assert response.json() == {'detail': luccy_client.DEFAULT_ERRORS[502]}


# ---- feedback --------------------------------------------------------------------------------


def test_feedback_note_is_cut_not_refused(setup, luccy):
    luccy['handler'] = lambda request: httpx.Response(200, json={'ok': True})
    response = api(setup['reader']).post(f'{BASE}/feedback/', {
        'answer_id': '0a1b2c3d', 'verdict': 'wrong', 'note': '  ' + 'n' * 600}, format='json')
    assert response.status_code == 200 and response.json() == {'ok': True}
    sent = json.loads(luccy['requests'][0].content)
    assert sent == {'answer_id': '0a1b2c3d', 'verdict': 'wrong', 'note': 'n' * 500}
    assert str(luccy['requests'][0].url) == 'http://luccy:8787/feedback'


def test_cleared_feedback_sends_no_note(setup, luccy):
    luccy['handler'] = lambda request: httpx.Response(200, json={'ok': True})
    api(setup['reader']).post(f'{BASE}/feedback/', {'answer_id': '0a1b2c3d', 'verdict': 'cleared', 'note': 'old'}, format='json')
    assert json.loads(luccy['requests'][0].content)['note'] == ''


@pytest.mark.parametrize('body', [
    {'answer_id': '0A1B2C3D', 'verdict': 'right'},
    {'answer_id': '0a1b2c3', 'verdict': 'right'},
    {'answer_id': 12345678, 'verdict': 'right'},
    {'answer_id': '0a1b2c3d', 'verdict': 'maybe'},
    {'answer_id': '0a1b2c3d'},
    {'answer_id': '0a1b2c3d', 'verdict': 'right', 'note': ['x']},
    [],
])
def test_feedback_validation(setup, luccy, body):
    luccy['handler'] = lambda request: httpx.Response(200, json={'ok': True})
    response = api(setup['reader']).post(f'{BASE}/feedback/', body, format='json')
    assert response.status_code == 400
    assert isinstance(response.json()['detail'], str)
    assert luccy['requests'] == []


def test_feedback_for_an_unknown_answer_is_404(setup, luccy):
    luccy['handler'] = lambda request: httpx.Response(404, json={'error': 'unknown answer'})
    response = api(setup['reader']).post(f'{BASE}/feedback/', {'answer_id': '0a1b2c3d', 'verdict': 'right'}, format='json')
    assert response.status_code == 404


# ---- status and /api/me ----------------------------------------------------------------------


def test_status_for_a_reader_probes_health_once(setup, luccy):
    luccy['handler'] = lambda request: httpx.Response(200, json={'status': 'ok'})
    for _ in range(2):
        response = api(setup['reader']).get(f'{BASE}/status/')
        assert response.status_code == 200
        assert response.json() == {
            'enabled': True, 'configured': True, 'available': True, 'healthy': True, 'reason': '',
            'sources': ['confluence', 'jira', 'drive', 'github'], 'tool_available': True,
        }
    [probe] = luccy['requests']
    assert str(probe.url) == 'http://luccy:8787/health'
    assert 'authorization' not in probe.headers


def test_status_reports_unhealthy_and_member_tool_policy(setup, luccy):
    from catalog.models import OrganizationMembership
    OrganizationMembership.objects.filter(user=setup['reader']).update(assistant_tool_preferences={'analyzer': False})
    luccy['handler'] = lambda request: httpx.Response(503, json={'status': 'unhealthy'})
    data = api(setup['reader']).get(f'{BASE}/status/').json()
    assert data['available'] is True and data['healthy'] is False and data['tool_available'] is False


@pytest.mark.parametrize('who, change, reason_part', [
    ('plain', {}, 'Company access'),
    ('outsider', {}, 'organization'),
    ('reader', {'LUCCY_ENABLED': False}, 'turned off'),
    ('reader', {'LUCCY_ORGANIZATION_ID': ''}, 'not set up'),
])
def test_status_explains_why_it_is_unavailable(setup, luccy, settings, who, change, reason_part):
    for key, value in change.items():
        setattr(settings, key, value)
    response = api(setup[who]).get(f'{BASE}/status/')
    assert response.status_code == 200
    data = response.json()
    assert data['available'] is False and data['healthy'] is None and data['tool_available'] is False
    assert reason_part in data['reason']
    assert data['enabled'] is change.get('LUCCY_ENABLED', True)
    assert luccy['requests'] == []


# ---- service tokens --------------------------------------------------------------------------


LONG = 'x' * 32


def test_a_short_api_key_turns_the_analyzer_off(setup, luccy, settings, caplog, monkeypatch):
    monkeypatch.setattr(permissions, '_reported_token_problems', set())
    caplog.set_level(logging.ERROR, logger='document_analyzer')
    settings.LUCCY_API_KEY = 'k' * 31
    luccy['handler'] = sse_reply
    reader = setup['reader']
    assert permissions.token_problem('LUCCY_API_KEY') == 'LUCCY_API_KEY must be at least 32 characters'
    assert permissions.configured() is False
    status = api(reader).get(f'{BASE}/status/').json()
    assert status['configured'] is False and status['available'] is False
    assert call(logged_in(reader), 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?'})[0].status_code == 404
    assert luccy['requests'] == []
    # One clear line per problem, naming the setting, never a token.
    lines = [record.getMessage() for record in caplog.records if record.name == 'document_analyzer.permissions']
    assert lines == ['Document Analyzer: LUCCY_API_KEY must be at least 32 characters. Every analyzer endpoint '
                     'answers 404 until it is fixed.']
    assert 'k' * 31 not in caplog.text


@pytest.mark.parametrize('tokens, problems', [
    ({'LUCCY_SYNC_TOKEN': 's' * 31}, {'LUCCY_SYNC_TOKEN': 'LUCCY_SYNC_TOKEN must be at least 32 characters'}),
    ({'LUCCY_EVAL_TOKEN': 'short'}, {'LUCCY_EVAL_TOKEN': 'LUCCY_EVAL_TOKEN must be at least 32 characters'}),
    ({'LUCCY_API_KEY': LONG, 'LUCCY_SYNC_TOKEN': LONG}, {'LUCCY_SYNC_TOKEN': 'LUCCY_SYNC_TOKEN must differ from LUCCY_API_KEY'}),
    ({'LUCCY_SYNC_TOKEN': LONG, 'LUCCY_EVAL_TOKEN': LONG}, {
        'LUCCY_SYNC_TOKEN': 'LUCCY_SYNC_TOKEN must differ from LUCCY_EVAL_TOKEN',
        'LUCCY_EVAL_TOKEN': 'LUCCY_EVAL_TOKEN must differ from LUCCY_SYNC_TOKEN'}),
    ({'LUCCY_API_KEY': LONG, 'LUCCY_EVAL_TOKEN': LONG}, {'LUCCY_EVAL_TOKEN': 'LUCCY_EVAL_TOKEN must differ from LUCCY_API_KEY'}),
])
def test_a_weak_or_shared_sync_or_eval_token_closes_only_its_own_door(setup, luccy, settings, caplog, monkeypatch,
                                                                       tokens, problems):
    monkeypatch.setattr(permissions, '_reported_token_problems', set())
    caplog.set_level(logging.ERROR, logger='document_analyzer')
    for key, value in tokens.items():
        setattr(settings, key, value)
    for name in permissions.TOKEN_SETTINGS:
        assert permissions.token_problem(name) == problems.get(name, '')
        assert permissions.service_token(name) == ('' if name in problems else str(getattr(settings, name)))
    # The page and the assistant tool depend only on LUCCY_API_KEY.
    assert permissions.configured() is True
    luccy['handler'] = sse_reply
    assert call(logged_in(setup['reader']), 'POST', f'{BASE}/ask/stream/', {'question': 'Hi?'})[0].status_code == 200
    lines = [record.getMessage() for record in caplog.records if record.name == 'document_analyzer.permissions']
    assert len(lines) == len(problems) and all(any(problem in line for line in lines) for problem in problems.values())
    assert all(value not in caplog.text for value in tokens.values())


def test_distinct_long_tokens_or_unset_ones_are_fine(setup, settings):
    settings.LUCCY_SYNC_TOKEN, settings.LUCCY_EVAL_TOKEN = 's' * 32, 'e' * 40
    assert all(permissions.token_problem(name) == '' for name in permissions.TOKEN_SETTINGS)
    assert permissions.configured() is True
    settings.LUCCY_SYNC_TOKEN, settings.LUCCY_EVAL_TOKEN = '', ''  # an unset token only turns its door off
    assert permissions.service_token('LUCCY_SYNC_TOKEN') == '' and permissions.configured() is True


def test_me_permission_is_explicit(setup, settings):
    def perm(user=None):
        browser = Client()
        if user is not None:
            browser.force_login(user)
        return browser.get('/api/me/').json()['perms']['can_view_document_analyzer']
    assert perm() is False
    assert perm(setup['reader']) is True
    assert perm(setup['admin']) is True
    assert perm(setup['plain']) is False
    assert perm(setup['outsider']) is False
    assert perm(setup['superuser']) is False
    settings.LUCCY_ENABLED = False
    cache.clear()
    assert perm(setup['reader']) is False


def test_a_failing_analyzer_check_never_breaks_api_me(setup, monkeypatch, caplog):
    def broken(user):
        raise RuntimeError('database hiccup')
    monkeypatch.setattr(permissions, 'can_use', broken)
    browser = Client()
    browser.force_login(setup['reader'])
    response = browser.get('/api/me/')
    assert response.status_code == 200
    assert response.json()['perms']['can_view_document_analyzer'] is False
    assert 'Document Analyzer access check failed (RuntimeError)' in caplog.text
