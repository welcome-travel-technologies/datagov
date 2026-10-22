"""Who may use the Document Analyzer (status and the page gate agree), the hourly cap, the
luccy client's options, earlier-turn bounds, the service tokens and ``/api/me``. luccy's
own page and its routes are covered in ``test_page_gate``."""
import json
import logging

import httpx
import pytest
from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.test import AsyncClient, Client
from rest_framework.test import APIClient

from document_analyzer import client as luccy_client
from document_analyzer import permissions, views

pytestmark = pytest.mark.django_db

BASE = '/api/document-analyzer'
CHATS = f'{BASE}/luccy/chats'
SAME = {'Origin': 'http://testserver', 'Sec-Fetch-Site': 'same-origin'}


def chats_reply(request):
    return httpx.Response(200, json={'chats': []})


def gate(user, path=CHATS, method='GET'):
    """Call luccy's page routes through DataGov's gate as ``user``; returns (status, JSON)."""
    browser = AsyncClient()
    if user is not None:
        browser.force_login(user)

    async def go():
        response = await getattr(browser, method.lower())(path, headers=SAME)
        content = b''.join([chunk async for chunk in response.streaming_content]) if response.streaming \
            else response.content
        return response.status_code, json.loads(content)
    return async_to_sync(go)()


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
    luccy['handler'] = chats_reply
    reader = setup['reader']
    assert api(reader).get(f'{BASE}/status/').json()['available'] is False
    assert gate(reader) == (404, {'error': 'not found'})
    assert gate(reader, f'{CHATS}/c1/ask', 'POST')[0] == 404
    assert luccy['requests'] == []


@pytest.mark.parametrize('who', ['outsider', 'superuser'])
def test_other_organization_and_unbound_superuser_get_404(setup, luccy, who):
    luccy['handler'] = chats_reply
    user = setup[who]
    assert api(user).get(f'{BASE}/status/').json()['available'] is False
    assert gate(user)[0] == 404
    assert luccy['requests'] == []


@pytest.mark.parametrize('who', ['plain', 'company_only'])
def test_member_without_analytics_access_gets_403(setup, luccy, who):
    luccy['handler'] = chats_reply
    status, body = gate(setup[who])
    assert status == 403 and 'Analytics access' in body['error']
    assert 'Analytics access' in api(setup[who]).get(f'{BASE}/status/').json()['reason']
    assert luccy['requests'] == []


def test_unauthenticated_is_refused(setup, luccy):
    assert Client().get(f'{BASE}/status/').status_code == 403  # DRF session auth: no challenge, so 403
    assert gate(None) == (401, {'error': 'Sign in to DataGov again.'})
    page = Client().get(f'{BASE}/luccy/')
    assert page.status_code == 302 and page['Location'] == '/login?next=/document-analyzer'
    assert luccy['requests'] == []


def test_org_admin_without_analytics_group_may_use_it(setup, luccy):
    luccy['handler'] = chats_reply
    assert gate(setup['admin']) == (200, {'chats': []})


def test_bound_superuser_member_needs_analytics_access_like_anyone(setup, luccy):
    from catalog.models import OrganizationMembership
    membership = OrganizationMembership.objects.create(organization=setup['org'], user=setup['superuser'])
    luccy['handler'] = chats_reply
    assert gate(setup['superuser'])[0] == 403
    assert luccy['requests'] == []
    membership.is_admin = True
    membership.save(update_fields=['is_admin'])
    assert gate(setup['superuser'])[0] == 200


@pytest.mark.parametrize('who', ['reader', 'admin', 'plain', 'company_only', 'outsider', 'superuser'])
def test_status_and_the_gate_agree(setup, luccy, who):
    luccy['handler'] = chats_reply
    available = api(setup[who]).get(f'{BASE}/status/').json()['available']
    assert available is (gate(setup[who])[0] == 200)


def test_wrong_method_is_refused(setup, luccy):
    assert api(setup['reader']).post(f'{BASE}/status/').status_code == 405
    assert gate(setup['reader'], method='PUT') == (404, {'error': 'not found'})
    assert luccy['requests'] == []


# ---- hourly cap ------------------------------------------------------------------------------


def test_hourly_cap_is_per_user(setup, settings):
    settings.LUCCY_MAX_ASKS_PER_HOUR = 2
    reader, admin = setup['reader'], setup['admin']
    assert permissions.take_ask(reader) and permissions.take_ask(reader)
    assert not permissions.take_ask(reader)
    # Other people keep their own allowance.
    assert permissions.take_ask(admin)


def test_a_returned_question_frees_its_place(setup, settings):
    settings.LUCCY_MAX_ASKS_PER_HOUR = 1
    reader = setup['reader']
    assert permissions.take_ask(reader)
    permissions.return_ask(reader)  # luccy never started on it
    assert permissions.take_ask(reader) and not permissions.take_ask(reader)


def test_return_ask_is_harmless_without_a_count(setup, settings):
    permissions.return_ask(setup['reader'])  # nothing counted this hour
    settings.LUCCY_MAX_ASKS_PER_HOUR = 0
    permissions.return_ask(setup['reader'])
    settings.LUCCY_MAX_ASKS_PER_HOUR = 1
    assert permissions.take_ask(setup['reader']) and not permissions.take_ask(setup['reader'])


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


# ---- client and earlier turns ----------------------------------------------------------------


def test_client_options_ignore_environment_and_redirects():
    options = luccy_client.options(330)
    assert options['trust_env'] is False and options['follow_redirects'] is False
    assert options['timeout'].read == 330 and options['timeout'].connect == 10


def test_failures_are_logged_without_ids(caplog):
    caplog.set_level(logging.WARNING, logger='document_analyzer')
    luccy_client.log_failure('GET', '/documents/conf:secret-page', 'HTTP 401')
    luccy_client.log_failure('GET', '/ask/stream/job-1', 'ConnectError')
    luccy_client.log_failure('POST', '/feedback', 'HTTP 500')
    assert [record.getMessage() for record in caplog.records] == [
        'Document Analyzer call GET /documents/<id> failed: HTTP 401.',
        'Document Analyzer call GET /ask/stream/<id> failed: ConnectError.',
        'Document Analyzer call POST /feedback failed: HTTP 500.',
    ]
    assert 'secret' not in caplog.text and 'job-1' not in caplog.text


@pytest.mark.parametrize('history, expected', [
    # Too many turns: the last 50.
    ([{'who': 'you', 'text': str(n)} for n in range(60)], [{'who': 'you', 'text': str(n)} for n in range(10, 60)]),
    # A turn too long: cut to 16,000 characters.
    ([{'who': 'luccy', 'text': 'y' * 16001}], [{'who': 'luccy', 'text': 'y' * 16000}]),
    # Too much in all: the oldest dropped until 64,000 characters remain.
    ([{'who': 'you', 'text': str(n) * 16000} for n in range(5)], [{'who': 'you', 'text': str(n) * 16000} for n in range(1, 5)]),
    # Within every bound: unchanged, extra keys dropped.
    ([{'who': 'you', 'text': 'x' * 16000, 'extra': 1}] * 4, [{'who': 'you', 'text': 'x' * 16000}] * 4),
])
def test_over_long_history_is_trimmed_not_refused(history, expected):
    assert views.bounded_history(history) == expected


def test_history_cap_may_be_lowered_or_off():
    turns = [{'who': 'you', 'text': str(n)} for n in range(5)]
    assert views.bounded_history(turns, cap=2) == turns[-2:]
    assert views.bounded_history(turns, cap=0) == []


# ---- status ----------------------------------------------------------------------------------


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
    ('plain', {}, 'Analytics access'),
    ('company_only', {}, 'Analytics access'),
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
    luccy['handler'] = chats_reply
    reader = setup['reader']
    assert permissions.token_problem('LUCCY_API_KEY') == 'LUCCY_API_KEY must be at least 32 characters'
    assert permissions.configured() is False
    status = api(reader).get(f'{BASE}/status/').json()
    assert status['configured'] is False and status['available'] is False
    assert gate(reader)[0] == 404
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
    luccy['handler'] = chats_reply
    assert gate(setup['reader'])[0] == 200
    lines = [record.getMessage() for record in caplog.records if record.name == 'document_analyzer.permissions']
    assert len(lines) == len(problems) and all(any(problem in line for line in lines) for problem in problems.values())
    assert all(value not in caplog.text for value in tokens.values())


def test_distinct_long_tokens_or_unset_ones_are_fine(setup, settings):
    settings.LUCCY_SYNC_TOKEN, settings.LUCCY_EVAL_TOKEN = 's' * 32, 'e' * 40
    assert all(permissions.token_problem(name) == '' for name in permissions.TOKEN_SETTINGS)
    assert permissions.configured() is True
    settings.LUCCY_SYNC_TOKEN, settings.LUCCY_EVAL_TOKEN = '', ''  # an unset token only turns its door off
    assert permissions.service_token('LUCCY_SYNC_TOKEN') == '' and permissions.configured() is True


# ---- /api/me ---------------------------------------------------------------------------------


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
