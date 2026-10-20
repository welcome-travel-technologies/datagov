"""C5: the evaluation answer arm and private comparison-run storage.
Agents run for real with an offline FunctionModel; luccy is a MockTransport."""
import json
import re

import httpx
import pytest
from django.core.management import call_command
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from rest_framework.test import APIClient

from catalog import views
from catalog.models import ChatMessage, ChatSession, CustomUser, OrganizationMembership
from catalog.tools import agent as agent_module
from document_analyzer import evaluation, permissions
from document_analyzer.models import AnalyzerComparisonRun

ANSWER_URL = '/api/document-analyzer/internal/eval/answer/'
RUNS_URL = '/api/document-analyzer/internal/eval/runs/'
LIST_URL = '/api/document-analyzer/comparisons/'
TOKEN = 'eval-token-for-tests-' + 'e' * 40
LUCCY_ANSWER = {
    'answer': 'Refunds take five days.', 'answer_html': '<p>x</p>', 'footer': 'f', 'log': '0a1b2c3d',
    'citations': [{'id': '4435476587', 'title': 'Refund policy', 'url': 'https://wiki.example/pages/4435476587'},
                  {'id': 'AP-320', 'title': '', 'url': 'javascript:alert(1)'},
                  {'id': '../etc', 'title': 'Odd id', 'url': None}],
    'abstained': True, 'gaps': ['the 2025 numbers'],
}
GENERATED_RUN_ID = re.compile(r'run-\d{8}T\d{6}Z-[0-9a-f]{6}')
RECORD = {'id': '0a1b2c3d', 'cost': {'usd': 0.0123, 'tokens': 4567}}


def luccy_routes(ask=lambda request: httpx.Response(200, json=LUCCY_ANSWER), records=None):
    """luccy's /ask and its free /records/<log> lookups."""
    records = {'0a1b2c3d': RECORD} if records is None else records

    def handler(request):
        if request.url.path.startswith('/records/'):
            record = records.get(request.url.path.rsplit('/', 1)[1])
            return httpx.Response(200, json=record) if record else httpx.Response(404, json={'error': 'no record'})
        return ask(request)
    return handler


def asks(luccy):
    return [request for request in luccy['requests'] if request.url.path == '/ask']


@pytest.fixture
def evalsetup(setup, settings, monkeypatch):
    settings.LUCCY_EVAL_ENABLED = True
    settings.LUCCY_EVAL_TOKEN = TOKEN
    settings.LUCCY_EVAL_USER_ID = str(setup['reader'].pk)
    monkeypatch.setenv('GOOGLE_API_KEY', 'test-only-not-a-real-key')
    monkeypatch.setattr(views, '_run_with_timeout', lambda function, timeout: function())
    return setup


def harness(token=TOKEN):
    client = APIClient()
    if token is not None:
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    return client


def ask(arm, question='How long do refunds take?', **extra):
    return harness().post(ANSWER_URL, {'arm': arm, 'question': question, **extra}, format='json')


def use_model(monkeypatch, *steps):
    """Offline model playing ``steps`` (('call', tool, args) or ('text', words)); returns
    what it saw: the tools offered and every tool result."""
    seen = {'tools': None, 'returns': []}

    def model(messages, info):
        if seen['tools'] is None:
            seen['tools'] = sorted(tool.name for tool in info.function_tools)
        returns = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        seen['returns'] = [part.content for part in returns]
        kind, *rest = steps[min(len(returns), len(steps) - 1)]
        if kind == 'call':
            return ModelResponse(parts=[ToolCallPart(rest[0], rest[1])])
        return ModelResponse(parts=[TextPart(rest[0])])
    monkeypatch.setattr(agent_module, 'Agent', lambda **kwargs: Agent(**{**kwargs, 'model': FunctionModel(model)}))
    return seen


# ---- gate --------------------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize('change', [{'LUCCY_EVAL_ENABLED': False}, {'LUCCY_EVAL_TOKEN': ''}])
@pytest.mark.parametrize('url', [ANSWER_URL, RUNS_URL])
def test_eval_endpoints_are_404_unless_enabled_with_a_token(evalsetup, settings, change, url):
    for key, value in change.items():
        setattr(settings, key, value)
    response = harness().post(url, {'arm': 'chat_analyzer_only', 'question': 'Q', 'status': 'done'}, format='json')
    assert response.status_code == 404


@pytest.mark.django_db
@pytest.mark.parametrize('url', [ANSWER_URL, RUNS_URL])
@pytest.mark.parametrize('token', [None, 'wrong', TOKEN + 'x'])
def test_eval_endpoints_need_the_eval_token(evalsetup, settings, url, token):
    settings.LUCCY_SYNC_TOKEN = TOKEN + 'x'  # another service's token is not this one
    response = harness(token).post(url, {'arm': 'chat_analyzer_only', 'question': 'Q', 'status': 'done'}, format='json')
    assert response.status_code == 401 and response['WWW-Authenticate'] == 'Bearer'
    assert AnalyzerComparisonRun.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize('body', [
    {'arm': 'preflight-unknown-arm', 'question': 'preflight'},
    {'arm': ['chat_analyzer_only'], 'question': 'Q'},
    {'arm': 'chat_analyzer_only'},
    {'arm': 'chat_analyzer_only', 'question': '   '},
    {'arm': 'chat_analyzer_only', 'question': 'x' * 10001},
    {'arm': 'chat_analyzer_only', 'question': 'Q', 'conversation': 'has space'},
    {'arm': 'chat_analyzer_only', 'question': 'Q', 'conversation': 'c' * 65},
    {'arm': 'chat_analyzer_only', 'question': 'Q', 'conversation': 5},
])
def test_invalid_eval_requests_are_400_before_anything_runs(evalsetup, luccy, settings, body):
    settings.LUCCY_EVAL_USER_ID = ''  # validation comes first, so the harness can probe cheaply
    response = harness().post(ANSWER_URL, body, format='json')
    assert response.status_code == 400 and isinstance(response.json()['detail'], str)
    assert luccy['requests'] == []


@pytest.mark.django_db
@pytest.mark.parametrize('arm', ['engine_chunks', 'engine_pages', 'native_chunks', 'native_pages'])
def test_datagovs_own_document_arms_are_gone(evalsetup, luccy, arm):
    """DataGov answers documents only through the Document Analyzer: its former engine
    and native-search arms are unknown arms now."""
    response = ask(arm)
    assert response.status_code == 400
    assert response.json()['detail'] == 'Choose an arm from: chat_analyzer_only.'
    assert evaluation.ARMS == ('chat_analyzer_only',) and luccy['requests'] == []


@pytest.mark.django_db
def test_non_object_and_oversized_bodies_are_refused(evalsetup):
    assert harness().post(ANSWER_URL, [], format='json').status_code == 400
    assert harness().post(ANSWER_URL, 'not json', content_type='application/json').status_code == 400
    big = {'arm': 'chat_analyzer_only', 'question': 'Q', 'pad': 'x' * (64 * 1024)}
    assert harness().post(ANSWER_URL, big, format='json').status_code == 413


@pytest.mark.django_db
@pytest.mark.parametrize('who', ['', 'abc', 'outsider', 'inactive', 'missing', 'gone'])
def test_eval_user_must_be_an_active_member_of_the_bound_organization(evalsetup, settings, who):
    if who == 'outsider':
        settings.LUCCY_EVAL_USER_ID = str(evalsetup['outsider'].pk)
    elif who == 'inactive':
        CustomUser.objects.filter(pk=evalsetup['reader'].pk).update(is_active=False)
    elif who == 'missing':
        settings.LUCCY_EVAL_USER_ID = '987654321'
    elif who == 'gone':
        settings.LUCCY_ORGANIZATION_ID = '987654321'  # set up, but no such organization
    else:
        settings.LUCCY_EVAL_USER_ID = who
    response = ask('chat_analyzer_only')
    assert response.status_code == 503
    assert 'LUCCY_' in response.json()['detail']


@pytest.mark.django_db
@pytest.mark.parametrize('change', [
    {'LUCCY_ENABLED': False}, {'LUCCY_ORGANIZATION_ID': ''}, {'LUCCY_API_KEY': ''}, {'LUCCY_API_URL': ''},
    {'LUCCY_API_KEY': 'short-key'},
])
def test_analyzer_off_or_not_set_up_hides_eval_and_comparisons(evalsetup, luccy, settings, change):
    """The rollback switch (LUCCY_ENABLED=False) and an unconfigured analyzer turn the
    eval endpoints and the comparisons list/detail into 404 too."""
    AnalyzerComparisonRun.objects.create(organization=evalsetup['org'], run_id='kept', status='done', rows=[1])
    for key, value in change.items():
        setattr(settings, key, value)
    assert ask('chat_analyzer_only').status_code == 404
    assert harness().post(RUNS_URL, {'status': 'done'}, format='json').status_code == 404
    admin = browser(evalsetup['admin'])
    assert admin.get(LIST_URL).status_code == 404
    assert admin.get(f'{LIST_URL}kept/').status_code == 404
    assert luccy['requests'] == []
    assert AnalyzerComparisonRun.objects.count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize('change', [{'LUCCY_EVAL_TOKEN': 'short-eval-token'}, {'LUCCY_SYNC_TOKEN': TOKEN},
                                    {'LUCCY_API_KEY': TOKEN}])
def test_a_weak_or_shared_eval_token_closes_eval_and_keeps_stored_runs_private(evalsetup, luccy, settings, change):
    AnalyzerComparisonRun.objects.create(organization=evalsetup['org'], run_id='kept', status='done', rows=[1])
    for key, value in change.items():
        setattr(settings, key, value)
    assert harness(settings.LUCCY_EVAL_TOKEN).post(RUNS_URL, {'status': 'done'}, format='json').status_code == 404
    assert harness(settings.LUCCY_EVAL_TOKEN).post(
        ANSWER_URL, {'arm': 'chat_analyzer_only', 'question': 'Q'}, format='json').status_code == 404
    # Stored runs stay private and intact even when evaluation is unavailable.
    assert browser(evalsetup['admin']).get(f'{LIST_URL}kept/').status_code == 404
    assert AnalyzerComparisonRun.objects.get(run_id='kept').rows == [1]
    assert luccy['requests'] == []


# ---- arms --------------------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('conversation', [None, 'gold-mh-007'])
def test_chat_analyzer_only_runs_the_real_chat_path_with_only_the_analyzer(evalsetup, luccy, monkeypatch, settings,
                                                                          conversation):
    settings.LUCCY_CHAT_ENABLED = False  # evaluation remains available during the page-only rollout
    reader = evalsetup['reader']
    stored = {'assistant_tool_access': {'analyzer': False, 'lineage': True}, 'assistant_tool_preferences': {'dbt': True}}
    OrganizationMembership.objects.filter(user=reader).update(**stored)
    settings.LUCCY_MAX_ASKS_PER_HOUR = 1
    assert permissions.take_ask(reader)  # the eval user's own hour is used up
    seen = use_model(monkeypatch, ('call', 'safe_ask_document_analyzer', {'question': 'agent paraphrase'}),
                     ('text', 'AGENT WORDING'))
    luccy['handler'] = luccy_routes()
    question = 'How long do  refunds take?'
    response = ask('chat_analyzer_only', question, **({'conversation': conversation} if conversation else {}))
    assert response.status_code == 200
    body = response.json()
    assert seen['tools'] == ['safe_ask_document_analyzer']
    [upstream] = asks(luccy)
    assert json.loads(upstream.content) == {'question': question, **({'conversation': conversation} if conversation else {})}
    # The offline FunctionModel has no price, so only luccy's part is measured.
    assert body == {
        'answer': ('Refunds take five days.\n\n**Sources**\n- [Refund policy](https://wiki.example/pages/4435476587)\n'
                   '- AP\\-320\n- Odd id\n\nAnswered by the Document Analyzer · ref 0a1b2c3d'),
        'judged_answer': 'Refunds take five days.',
        'citations': [
            {'document_id': None, 'luccy_id': '4435476587', 'title': 'Refund policy', 'url': 'https://wiki.example/pages/4435476587'},
            {'document_id': None, 'luccy_id': 'AP-320', 'title': 'AP-320', 'url': None},
            {'document_id': None, 'luccy_id': None, 'title': 'Odd id', 'url': None}],
        'abstained': True, 'gaps': ['the 2025 numbers'], 'latency_s': body['latency_s'],
        'tool_calls': ['ask_document_analyzer'], 'verbatim': True, 'tool_error': False, 'error': None,
        'cost_usd': 0.0123, 'cost_source': 'estimate', 'cost_detail': body['cost_detail'],
    }
    assert body['cost_detail']['luccy_usd'] == 0.0123 and body['cost_detail']['agent_usd'] is None
    assert "cannot price the agent's model" in body['cost_detail']['note']
    assert isinstance(body['latency_s'], float) and body['latency_s'] >= 0
    # The forced policy was never saved, and nothing of the chat is left behind.
    membership = OrganizationMembership.objects.get(user=reader)
    assert {'assistant_tool_access': membership.assistant_tool_access,
            'assistant_tool_preferences': membership.assistant_tool_preferences} == stored
    assert ChatSession.objects.count() == 0 and ChatMessage.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_chat_analyzer_only_reports_a_failed_analyzer_as_the_agents_text(evalsetup, luccy, monkeypatch):
    use_model(monkeypatch, ('call', 'safe_ask_document_analyzer', {'question': 'q'}), ('text', 'No answer, sorry.'))
    luccy['handler'] = lambda request: httpx.Response(500)
    body = ask('chat_analyzer_only').json()
    assert body['answer'] == 'No answer, sorry.' and body['verbatim'] is False
    assert body['citations'] == [] and body['tool_calls'] == ['ask_document_analyzer'] and body['error'] is None
    assert body['tool_error'] is True and body['judged_answer'] == 'No answer, sorry.'


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('reply', [
    lambda request: httpx.Response(429, json={'error': 'The daily spending cap is reached.'}),
    lambda request: httpx.Response(503, json={'error': 'Luccy is busy.'}),
    lambda request: (_ for _ in ()).throw(httpx.ReadTimeout('slow')),
    lambda request: httpx.Response(200, json={'no': 'answer'}),
])
def test_tool_error_marks_every_analyzer_failure(evalsetup, luccy, monkeypatch, reply):
    use_model(monkeypatch, ('call', 'safe_ask_document_analyzer', {'question': 'q'}), ('text', 'Agent fallback.'))
    luccy['handler'] = reply
    body = ask('chat_analyzer_only').json()
    assert body['tool_error'] is True and body['verbatim'] is False and body['error'] is None
    assert body['judged_answer'] == body['answer'] == 'Agent fallback.'


@pytest.mark.django_db(transaction=True)
def test_no_tool_error_when_the_agent_never_asked(evalsetup, luccy, monkeypatch):
    use_model(monkeypatch, ('text', 'I can answer that myself.'))
    body = ask('chat_analyzer_only').json()
    assert body['tool_error'] is False and body['tool_calls'] == [] and body['verbatim'] is False
    assert body['judged_answer'] == 'I can answer that myself.'
    assert luccy['requests'] == []


@pytest.mark.django_db(transaction=True)
def test_eval_answers_never_carry_the_debug_section(evalsetup, luccy, monkeypatch):
    evalsetup['org'].debug_responses_enabled = True
    evalsetup['org'].save(update_fields=['debug_responses_enabled'])
    use_model(monkeypatch, ('call', 'safe_ask_document_analyzer', {'question': 'q'}), ('text', 'Agent words.'))
    luccy['handler'] = lambda request: httpx.Response(500)
    body = ask('chat_analyzer_only').json()
    assert body['answer'] == body['judged_answer'] == 'Agent words.'


@pytest.mark.django_db(transaction=True)
def test_eval_chat_uses_the_stream_timeouts(evalsetup, luccy, monkeypatch, settings):
    settings.LUCCY_STREAM_TIMEOUT = 333
    settings.LUCCY_TOOL_TIMEOUT = 44
    turn_timeouts = []
    monkeypatch.setattr(views, '_run_with_timeout', lambda function, timeout: turn_timeouts.append(timeout) or function())
    use_model(monkeypatch, ('call', 'safe_ask_document_analyzer', {'question': 'q'}), ('text', 'x'))
    luccy['handler'] = luccy_routes()
    ask('chat_analyzer_only')
    [upstream] = asks(luccy)
    assert upstream.extensions['timeout']['read'] == 333
    assert turn_timeouts == [333 + 60]


@pytest.mark.django_db(transaction=True)
def test_cost_is_measured_when_the_agent_model_has_a_price(evalsetup, luccy, monkeypatch):
    from pydantic_ai.messages import ModelResponse as Response

    class Price:
        total_price = 0.002
    monkeypatch.setattr(Response, 'cost', lambda self: Price())
    use_model(monkeypatch, ('call', 'safe_ask_document_analyzer', {'question': 'q'}), ('text', 'x'))
    luccy['handler'] = luccy_routes()
    body = ask('chat_analyzer_only').json()
    # Two agent model calls (the tool call, then the final text) and one luccy answer.
    assert body['cost_source'] == 'measured' and body['cost_usd'] == pytest.approx(0.0123 + 2 * 0.002)
    assert body['cost_detail']['agent_usd'] == pytest.approx(0.004) and body['cost_detail']['note'] is None
    [record] = [request for request in luccy['requests'] if request.url.path.startswith('/records/')]
    assert record.url.path == '/records/0a1b2c3d' and record.method == 'GET'
    assert record.headers['authorization'] == 'Bearer test-luccy-key-with-no-real-access'


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('records', [{}, {'0a1b2c3d': {'id': '0a1b2c3d'}}])
def test_a_missing_luccy_record_makes_the_cost_an_estimate(evalsetup, luccy, monkeypatch, records):
    from pydantic_ai.messages import ModelResponse as Response

    class Price:
        total_price = 0.002
    monkeypatch.setattr(Response, 'cost', lambda self: Price())
    use_model(monkeypatch, ('call', 'safe_ask_document_analyzer', {'question': 'q'}), ('text', 'x'))
    luccy['handler'] = luccy_routes(records=records)
    body = ask('chat_analyzer_only').json()
    assert body['cost_source'] == 'estimate' and body['cost_usd'] == pytest.approx(0.004)
    assert 'no cost record' in body['cost_detail']['note']


@pytest.mark.django_db
def test_eval_conversation_ids_may_not_use_datagovs_namespace(evalsetup, luccy):
    for conversation in ('dgchat-0123456789abcdef0123456789abcdef01234567', 'dgpage-x', 'DG-thread'):
        response = ask('chat_analyzer_only', conversation=conversation)
        assert response.status_code == 400 and 'reserved' in response.json()['detail']
    assert luccy['requests'] == []


@pytest.mark.django_db
def test_chat_analyzer_only_needs_analyzer_access(evalsetup, luccy):
    evalsetup['reader'].groups.clear()
    body = ask('chat_analyzer_only').json()
    assert 'Document Analyzer' in body['error'] and body['answer'] == '' and luccy['requests'] == []


# ---- runs --------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_run_is_created_with_a_generated_id_then_updated(evalsetup):
    response = harness().post(RUNS_URL, {'status': 'running', 'params': {'arms': ['A']}, 'summary': {},
                                         'rows': [{'id': 'q1'}]}, format='json')
    assert response.status_code == 201
    run_id = response.json()['run_id']
    assert GENERATED_RUN_ID.fullmatch(run_id) and evaluation.valid_run_id(run_id)
    run = AnalyzerComparisonRun.objects.get(run_id=run_id)
    assert run.organization == evalsetup['org'] and run.rows == [{'id': 'q1'}]
    response = harness().post(RUNS_URL, {'run_id': run_id, 'status': 'done', 'summary': {'questions': 1}}, format='json')
    assert response.status_code == 200 and response.json() == {'run_id': run_id}
    run.refresh_from_db()
    assert (run.status, run.summary, run.params, run.rows) == ('done', {'questions': 1}, {'arms': ['A']}, [{'id': 'q1'}])


@pytest.mark.django_db
def test_a_named_run_is_created_once(evalsetup):
    body = {'run_id': 'gold-2026-10-16.a:1', 'status': 'failed', 'params': {}, 'summary': {}, 'rows': []}
    assert harness().post(RUNS_URL, body, format='json').status_code == 201
    assert harness().post(RUNS_URL, body, format='json').status_code == 200
    assert AnalyzerComparisonRun.objects.count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize('run_id', ['..', 'a..b', '-run', '_run', '.run', 'a/b', 'a b', 'x' * 129, 'é', 5, ['a']])
def test_invalid_run_ids_are_400(evalsetup, run_id):
    response = harness().post(RUNS_URL, {'run_id': run_id, 'status': 'done'}, format='json')
    assert response.status_code == 400
    assert AnalyzerComparisonRun.objects.count() == 0


@pytest.mark.django_db
def test_longest_run_id_is_accepted(evalsetup):
    assert harness().post(RUNS_URL, {'run_id': 'r' * 128, 'status': 'done'}, format='json').status_code == 201


@pytest.mark.django_db
@pytest.mark.parametrize('body', [
    {}, {'status': 'finished'}, {'status': 'done', 'params': []}, {'status': 'done', 'summary': 'x'},
    {'status': 'done', 'rows': {}},
])
def test_invalid_run_bodies_are_400(evalsetup, body):
    assert harness().post(RUNS_URL, body, format='json').status_code == 400


@pytest.mark.django_db
def test_runs_over_two_megabytes_are_refused(evalsetup):
    fits = {'status': 'done', 'rows': ['x' * 1000] * 2000}
    assert len(json.dumps(fits)) < 2 * 1024 * 1024
    assert harness().post(RUNS_URL, fits, format='json').status_code == 201
    too_big = {'status': 'done', 'rows': ['x' * 1000] * 2100}
    assert len(json.dumps(too_big)) > 2 * 1024 * 1024
    assert harness().post(RUNS_URL, too_big, format='json').status_code == 413
    assert AnalyzerComparisonRun.objects.count() == 1


@pytest.mark.django_db
def test_a_run_of_another_organization_is_not_overwritten(evalsetup):
    AnalyzerComparisonRun.objects.create(organization=evalsetup['other'], run_id='shared', status='done', rows=[1])
    response = harness().post(RUNS_URL, {'run_id': 'shared', 'status': 'running', 'rows': []}, format='json')
    assert response.status_code == 409
    assert AnalyzerComparisonRun.objects.get(run_id='shared').rows == [1]


@pytest.mark.django_db
def test_runs_need_a_bound_organization(evalsetup, settings):
    settings.LUCCY_ORGANIZATION_ID = '987654321'
    assert harness().post(RUNS_URL, {'status': 'done'}, format='json').status_code == 503
    settings.LUCCY_ORGANIZATION_ID = ''  # not set up at all: the endpoint is gone
    assert harness().post(RUNS_URL, {'status': 'done'}, format='json').status_code == 404


# ---- comparisons -------------------------------------------------------------------------------


def browser(user):
    client = APIClient()
    if user is not None:
        client.force_authenticate(user)
    return client


@pytest.fixture
def runs(evalsetup):
    older = AnalyzerComparisonRun.objects.create(organization=evalsetup['org'], run_id='older', status='done',
                                                 summary={'questions': 1}, rows=[{'id': 'q1'}])
    newer = AnalyzerComparisonRun.objects.create(organization=evalsetup['org'], run_id='newer', status='running',
                                                 params={'arms': ['A']}, rows=[{'id': 'q2'}])
    AnalyzerComparisonRun.objects.create(organization=evalsetup['other'], run_id='foreign', status='done', rows=[])
    return {'older': older, 'newer': newer}


@pytest.mark.django_db
@pytest.mark.parametrize('who', [None, 'admin', 'reader', 'plain', 'outsider', 'superuser'])
@pytest.mark.parametrize('path', ['', 'older/', 'newer/', 'foreign/', 'unknown/', 'a..b/'])
def test_comparison_browser_routes_are_gone_for_every_user(evalsetup, runs, who, path):
    user = evalsetup[who] if who else None
    assert browser(user).get(f'{LIST_URL}{path}').status_code == 404
    # Removing access does not remove the saved benchmark results.
    assert AnalyzerComparisonRun.objects.count() == 3
    runs['older'].refresh_from_db()
    assert runs['older'].summary == {'questions': 1} and runs['older'].rows == [{'id': 'q1'}]


@pytest.mark.django_db
@pytest.mark.parametrize('path', ['', 'older/'])
def test_the_eval_token_does_not_open_a_comparison_browser_route(evalsetup, runs, path):
    assert harness().get(f'{LIST_URL}{path}').status_code == 404


# The runner's summary of a full golden-set run (analyzer/luccy_qdrant/compare): the set's identity,
# luccy's recorded run, the configuration drift and the expected documents DataGov does not hold.
GOLDEN_SUMMARY = {
    'questions': 121, 'complete': True, 'spent_usd': 24.1, 'excluded': {'infra_error': []},
    'gold': {'rows': 121, 'selected': 121, 'digest': '2670070ea44a', 'baseline_digest': '2670070ea44a',
             'same_as_recorded': True, 'changed_ids': []},
    'luccy_recorded': {'ts': '2026-09-28T19:58:41+00:00', 'n': 121, 'n_pass': 74, 'pass_rate': 0.6116,
                       'fact_ratio': 0.7059, 'judge_model': 'gemini-3.5-flash-lite', 'judge_votes': 1,
                       'gold_digest': '2670070ea44a', 'latency_p50_s': 25.4, 'latency_p90_s': 39.0, 'usd': 5.5707,
                       'pass_by_id': {'sh-001': True, 'sh-003': False},
                       'on_this_run': {'label': 'Luccy, recorded', 'n': 121, 'pass': 74, 'pass_rate': 0.6116}},
    'config_drift': {'same': False, 'differences': [{'field': 'prompts.route', 'recorded': '97c2d7d7ba83',
                                                     'current': '4cb66d8881db',
                                                     'text': 'The router prompt (route) changed since the recorded run.'}],
                     'documents': {'recorded': {'index_dir': '/pins/x', 'index_sig': 's'}, 'current': {'documents': 88},
                                   'text': 'Documents: ...'}},
    'expected_docs_missing': [{'id': 'sh-001', 'question': 'Starbucks ride?', 'missing': ['4435476587']}],
}


@pytest.mark.django_db
def test_a_golden_set_summary_is_stored_privately_as_sent(evalsetup, runs):
    """The protected runner keeps full metadata and rows without exposing browser endpoints."""
    rows = [{'id': 'sh-001', 'kind': 'single_hop', 'question': 'Starbucks ride?', 'order': ['A', 'C'], 'arms': {},
             'expected_docs_missing': ['4435476587'], 'recorded': 'pass'}]
    body = {'run_id': 'gold-full', 'status': 'done', 'params': {'gold_rows': 121, 'ids': None},
            'summary': GOLDEN_SUMMARY, 'rows': rows}
    assert harness().post(RUNS_URL, body, format='json').status_code == 201
    stored = AnalyzerComparisonRun.objects.get(run_id='gold-full')
    assert stored.summary == GOLDEN_SUMMARY and stored.rows == rows and stored.params['gold_rows'] == 121
    assert runs['older'].summary == {'questions': 1}
    admin = browser(evalsetup['admin'])
    assert admin.get(LIST_URL).status_code == admin.get(f'{LIST_URL}gold-full/').status_code == 404


# ---- migrations --------------------------------------------------------------------------------


@pytest.mark.django_db
def test_models_and_migrations_agree():
    call_command('makemigrations', 'document_analyzer', check=True, dry_run=True, verbosity=0)
