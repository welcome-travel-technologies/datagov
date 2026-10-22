"""luccy's own chat page behind DataGov's sign-in (page_gate)."""
import json
import logging

import httpx
import pytest
from asgiref.sync import async_to_sync
from django.test import AsyncClient

from document_analyzer import page_gate

pytestmark = pytest.mark.django_db

GATE = '/api/document-analyzer/luccy/'
PAGE = b'<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><title>Luccy</title></head><body>%USER%</body></html>'
SAME = {'Origin': 'http://testserver', 'Sec-Fetch-Site': 'same-origin'}
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


def call(user, method, path, body=None, headers=None):
    browser = AsyncClient()
    if user is not None:
        browser.force_login(user)

    async def go():
        send = getattr(browser, method.lower())
        kwargs = {'headers': headers}
        if body is not None:
            kwargs.update(data=json.dumps(body), content_type='application/json')
        response = await send(path, **kwargs)
        if response.streaming:
            return response, b''.join([chunk async for chunk in response.streaming_content])
        return response, response.content
    return async_to_sync(go)()


def test_the_page_is_luccys_own_with_only_the_prefix_script_added(setup, luccy):
    luccy['handler'] = lambda request: httpx.Response(200, headers={'content-type': 'text/html; charset=utf-8'}, content=PAGE)
    response, body = call(setup['reader'], 'GET', GATE)
    assert response.status_code == 200
    assert body == PAGE.replace(b'<head>', b'<head>' + page_gate.PREFIX_SCRIPT.encode())
    assert response['X-Frame-Options'] == 'SAMEORIGIN' and response['Cache-Control'] == 'no-store'
    [request] = luccy['requests']
    assert request.url == 'http://luccy:8787/' and request.method == 'GET'
    # The person, named by DataGov alone; no key, cookie or browser origin reaches luccy.
    assert request.headers['Remote-User'] == 'reader@analyzer.test'
    assert not {'authorization', 'cookie', 'origin'} & set(request.headers.keys())


def test_routes_are_relayed_under_the_gate_with_their_query(setup, luccy):
    luccy['handler'] = lambda request: httpx.Response(200, json={'chats': []})
    response, body = call(setup['reader'], 'GET', GATE + 'chats', headers=SAME)
    assert response.status_code == 200 and json.loads(body) == {'chats': []}
    call(setup['reader'], 'GET', GATE + 'documents/123-part-2?answer=0a1b2c3d', headers=SAME)
    assert [str(r.url) for r in luccy['requests']] == [
        'http://luccy:8787/chats', 'http://luccy:8787/documents/123-part-2?answer=0a1b2c3d']


def test_an_answer_streams_through_unchanged(setup, luccy):
    luccy['handler'] = lambda request: httpx.Response(200, headers={'content-type': 'text/event-stream; charset=utf-8'},
                                                      stream=Chunks(EVENTS))
    response, body = call(setup['reader'], 'GET', GATE + 'ask/stream/job-1', headers={**SAME, 'Last-Event-ID': '1'})
    assert response.status_code == 200 and body == b''.join(EVENTS)
    assert response['X-Accel-Buffering'] == 'no'
    assert luccy['requests'][0].headers['Last-Event-ID'] == '1'


def test_a_question_counts_against_the_hourly_cap(setup, luccy, settings):
    settings.LUCCY_MAX_ASKS_PER_HOUR = 1
    luccy['handler'] = lambda request: httpx.Response(200, json={'job': 'job-1'})
    assert call(setup['reader'], 'POST', GATE + 'chats/c1/ask', {'question': 'Hi?'}, SAME)[0].status_code == 200
    response, body = call(setup['reader'], 'POST', GATE + 'chats/c1/ask', {'question': 'Again?'}, SAME)
    assert response.status_code == 429 and 'hourly limit' in json.loads(body)['error']
    assert json.loads(luccy['requests'][0].content) == {'question': 'Hi?'}


def test_writes_must_come_from_this_origin(setup, luccy):
    luccy['handler'] = lambda request: httpx.Response(200, json={})
    for headers in ({}, {'Origin': 'https://evil.example'}, {'Origin': 'http://testserver', 'Sec-Fetch-Site': 'cross-site'}):
        assert call(setup['reader'], 'DELETE', GATE + 'chats/c1', headers=headers)[0].status_code == 403
    assert call(setup['reader'], 'DELETE', GATE + 'chats/c1', headers=SAME)[0].status_code == 200
    assert len(luccy['requests']) == 1


@pytest.mark.parametrize('path', ['ask', 'health', 'conversation/diagnostics', 'chats/../health', 'fonts/x.js'])
def test_only_luccys_page_routes_are_relayed(setup, luccy, path):
    assert call(setup['reader'], 'GET', GATE + path, headers=SAME)[0].status_code == 404
    assert luccy['requests'] == []


def test_signed_out_and_no_access(setup, luccy):
    response, _ = call(None, 'GET', GATE)
    assert response.status_code == 302 and response['Location'] == '/login?next=/document-analyzer'
    assert call(None, 'GET', GATE + 'chats', headers=SAME)[0].status_code == 401
    response, body = call(setup['plain'], 'GET', GATE + 'chats', headers=SAME)
    assert response.status_code == 403 and 'Analytics access' in json.loads(body)['error']
    # Another organization's member, and a superuser who is not a member, find nothing.
    for who in ('outsider', 'superuser'):
        for path in ('', 'chats'):
            assert call(setup[who], 'GET', GATE + path, headers=SAME)[0].status_code == 404
    assert luccy['requests'] == []


@pytest.mark.parametrize('change', [
    {'LUCCY_ENABLED': False},
    {'LUCCY_API_KEY': ''},
    {'LUCCY_API_KEY': 'k' * 31},
    {'LUCCY_API_URL': 'ftp://luccy'},
    {'LUCCY_ORGANIZATION_ID': ''},
])
def test_switched_off_or_not_set_up_is_404(setup, luccy, settings, change):
    for key, value in change.items():
        setattr(settings, key, value)
    luccy['handler'] = lambda request: httpx.Response(200, json={})
    for method, path in (('GET', ''), ('GET', 'chats'), ('POST', 'chats/c1/ask')):
        response, body = call(setup['reader'], method, GATE + path, {'question': 'Hi?'} if method == 'POST' else None, SAME)
        assert response.status_code == 404 and json.loads(body) == {'error': 'not found'}
    assert luccy['requests'] == []


def test_org_admins_get_in_and_superusers_only_as_members(setup, luccy):
    from catalog.models import OrganizationMembership
    luccy['handler'] = lambda request: httpx.Response(200, json={'chats': []})
    assert call(setup['admin'], 'GET', GATE + 'chats', headers=SAME)[0].status_code == 200
    membership = OrganizationMembership.objects.create(organization=setup['org'], user=setup['superuser'])
    assert call(setup['superuser'], 'GET', GATE + 'chats', headers=SAME)[0].status_code == 403
    membership.is_admin = True
    membership.save(update_fields=['is_admin'])
    assert call(setup['superuser'], 'GET', GATE + 'chats', headers=SAME)[0].status_code == 200
    assert [r.headers['Remote-User'] for r in luccy['requests']] == ['orgadmin@analyzer.test', 'root@analyzer.test']


@pytest.mark.parametrize('first', [
    lambda request: httpx.Response(503, json={'error': 'Luccy is busy.'}),
    lambda request: httpx.Response(429, json={'error': 'The daily spending cap is reached.'}),
    lambda request: httpx.Response(500),
    lambda request: (_ for _ in ()).throw(httpx.ConnectError('down')),
])
def test_a_question_luccy_never_started_does_not_use_the_allowance(setup, luccy, settings, first):
    settings.LUCCY_MAX_ASKS_PER_HOUR = 1
    luccy['handler'] = first
    assert call(setup['reader'], 'POST', GATE + 'chats/c1/ask', {'question': 'Hi?'}, SAME)[0].status_code != 200
    luccy['handler'] = lambda request: httpx.Response(200, json={'job': 'job-1'})
    assert call(setup['reader'], 'POST', GATE + 'chats/c1/ask', {'question': 'Hi?'}, SAME)[0].status_code == 200
    assert call(setup['reader'], 'POST', GATE + 'chats/c1/ask', {'question': 'Hi?'}, SAME)[0].status_code == 429
    # Re-attaching is not a new question, and other people keep their own allowance.
    assert call(setup['reader'], 'GET', GATE + 'ask/stream/job-1', headers=SAME)[0].status_code == 200
    assert call(setup['admin'], 'POST', GATE + 'chats/c2/ask', {'question': 'Hi?'}, SAME)[0].status_code == 200
    assert len(luccy['requests']) == 4


def test_unreachable_luccy_is_logged_without_ids(setup, luccy, caplog):
    caplog.set_level(logging.WARNING, logger='document_analyzer')
    luccy['handler'] = lambda request: (_ for _ in ()).throw(httpx.ConnectError('SECRET host'))
    for path in ('documents/conf:secret-page', 'ask/stream/job-1'):
        response, body = call(setup['reader'], 'GET', GATE + path, headers=SAME)
        assert response.status_code == 502 and 'could not be reached' in json.loads(body)['error']
    ours = [record.getMessage() for record in caplog.records if record.name.startswith('document_analyzer')]
    assert ours == ['Document Analyzer call GET /documents/<id> failed: ConnectError.',
                    'Document Analyzer call GET /ask/stream/<id> failed: ConnectError.']
    assert 'SECRET' not in caplog.text  # Django's own request log still names the URL, as the access log does.


def test_owner_names_follow_luccys_rule(setup):
    user = setup['reader']
    assert page_gate.owner_of(user) == 'reader@analyzer.test'
    user.email = 'Ünïcode@example.com'
    assert page_gate.owner_of(user) == 'reader'
    user.username = 'x' * 70
    assert page_gate.owner_of(user) == f'datagov-{user.pk}'
