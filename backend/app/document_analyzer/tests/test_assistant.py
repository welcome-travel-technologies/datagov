import json
import logging

import httpx
import pytest
from rest_framework.test import APIClient

from catalog import views
from catalog.models import ChatMessage, ChatSession, OrganizationMembership
from catalog.services.assistant_tools import member_policy, tool_allowed, validate_overrides
from catalog.tools import agent as agent_module
from document_analyzer import assistant, permissions

pytestmark = pytest.mark.django_db

ANSWER = {
    'answer': 'Refunds take five days ([Refund policy](https://wiki.example/refunds)).',
    'answer_html': '<p>Refunds</p>', 'footer': 'Answered in 20 s · ref 0a1b2c3d', 'log': '0a1b2c3d',
    'citations': [{'id': 'conf-1', 'title': 'Refund policy', 'url': 'https://wiki.example/refunds'},
                  {'id': 'wh:sales', 'title': '', 'url': None},
                  {'id': 'jira-9', 'title': 'Ticket', 'url': 'javascript:alert(1)'}],
    'abstained': False, 'gaps': ['the 2025 numbers'],
}


@pytest.fixture(autouse=True)
def _test_google_api_key(monkeypatch):
    # Agent construction validates provider configuration eagerly; no model is called.
    monkeypatch.setenv('GOOGLE_API_KEY', 'test-only-not-a-real-key')


def build(org, user, monkeypatch, surface='web'):
    """Build the real chat agent with a fake Agent class; returns (tools, system prompt)."""
    registered, captured = {}, {}

    class FakeAgent:
        def __init__(self, *args, system_prompt=None, **kwargs):
            captured['system_prompt'] = system_prompt

        def tool_plain(self, tool):
            registered[tool.__name__] = tool

    monkeypatch.setattr(agent_module, 'Agent', FakeAgent)
    views.build_chatbot_agent_for_org(org, user=user, surface=surface)
    return registered, captured['system_prompt']


def test_tool_group_is_known_and_mapped(setup):
    assert validate_overrides({'analyzer': False}) == {'analyzer': False}
    assert member_policy(setup['org'], setup['reader'])['analyzer'] is True
    assert tool_allowed(setup['org'], setup['reader'], 'safe_ask_document_analyzer') is True
    OrganizationMembership.objects.filter(user=setup['reader']).update(assistant_tool_access={'analyzer': False})
    assert tool_allowed(setup['org'], setup['reader'], 'ask_document_analyzer') is False


@pytest.mark.parametrize('who', ['reader', 'admin', 'plain', 'superuser'])
def test_chat_rollout_switch_disables_documents_for_every_user(setup, settings, monkeypatch, who):
    settings.LUCCY_CHAT_ENABLED = False
    user = setup[who]
    org = setup['org']
    assert member_policy(org, user)['analyzer'] is False
    assert tool_allowed(org, user, 'ask_document_analyzer') is False
    tools, prompt = build(org, user, monkeypatch)
    assert 'safe_ask_document_analyzer' not in tools
    assert '## Document Analyzer' not in prompt
    assert views._documents_mode_allowed(org, user) is False


def test_chat_rollout_switch_keeps_the_standalone_page(setup, settings):
    settings.LUCCY_CHAT_ENABLED = False
    browser = APIClient()
    browser.force_authenticate(setup['reader'])
    assert browser.get('/api/document-analyzer/status/').status_code == 200
    tools = browser.get('/api/me/assistant-tools/').json()['tools']
    documents = next(tool for tool in tools if tool['id'] == 'analyzer')
    assert documents['available'] is False and documents['effective'] is False
    assert 'Document Analyzer page' in documents['description']
    assert member_policy(setup['org'], None)['analyzer'] is False
    assert tool_allowed(setup['org'], None, 'ask_document_analyzer') is False


def test_tool_is_registered_for_a_reader_of_the_bound_organization(setup, luccy, monkeypatch):
    tools, prompt = build(setup['org'], setup['reader'], monkeypatch)
    assert 'safe_ask_document_analyzer' in tools
    assert '## Document Analyzer' in prompt
    luccy['handler'] = lambda request: httpx.Response(200, json=ANSWER)
    result = tools['safe_ask_document_analyzer']('  How do refunds work?  ')
    assert result == {'status': 'success', 'data': {
        'answer': ANSWER['answer'],
        'sources': [{'n': 1, 'title': 'Refund policy', 'url': 'https://wiki.example/refunds'},
                    {'n': 2, 'title': 'wh:sales', 'url': None},
                    {'n': 3, 'title': 'Ticket', 'url': None}],
        'abstained': False, 'gaps': ['the 2025 numbers'], 'ref': '0a1b2c3d', 'open_in': '/document-analyzer',
    }}
    [upstream] = luccy['requests']
    assert str(upstream.url) == 'http://luccy:8787/ask'
    assert json.loads(upstream.content) == {'question': 'How do refunds work?'}
    assert upstream.headers['remote-user'] == f'datagov-{setup["reader"].pk}'
    assert upstream.headers['authorization'] == 'Bearer test-luccy-key-with-no-real-access'


@pytest.mark.parametrize('field', ['assistant_tool_access', 'assistant_tool_preferences'])
def test_member_policy_removes_the_tool(setup, monkeypatch, field):
    OrganizationMembership.objects.filter(user=setup['reader']).update(**{field: {'analyzer': False}})
    tools, prompt = build(setup['org'], setup['reader'], monkeypatch)
    assert 'safe_ask_document_analyzer' not in tools
    assert 'Document Analyzer' not in prompt


@pytest.mark.parametrize('who, change', [
    ('outsider', {}),
    ('plain', {}),
    ('reader', {'LUCCY_ENABLED': False}),
    ('reader', {'LUCCY_ORGANIZATION_ID': ''}),
    ('reader', {'LUCCY_API_KEY': ''}),
])
def test_org_binding_and_access_remove_the_tool(setup, monkeypatch, settings, who, change):
    for key, value in change.items():
        setattr(settings, key, value)
    user = setup[who]
    org = setup['other'] if who == 'outsider' else setup['org']
    tools, _prompt = build(org, user, monkeypatch)
    assert 'safe_ask_document_analyzer' not in tools


def test_slack_agents_never_get_the_tool(setup, monkeypatch):
    tools, prompt = build(setup['org'], setup['reader'], monkeypatch, surface='slack')
    assert 'safe_ask_document_analyzer' not in tools
    assert 'Document Analyzer' not in prompt


def test_tool_needs_a_signed_in_user(setup):
    assert assistant.tool_for(setup['org'], None) is None
    # A member of the bound org must still be asked about in that org.
    assert assistant.tool_for(setup['other'], setup['reader']) is None


def test_execution_rechecks_policy_and_access(setup, luccy, monkeypatch):
    tools, _prompt = build(setup['org'], setup['reader'], monkeypatch)
    tool = tools['safe_ask_document_analyzer']
    OrganizationMembership.objects.filter(user=setup['reader']).update(assistant_tool_access={'analyzer': False})
    blocked = tool('How do refunds work?')
    assert blocked['status'] == 'error' and 'disabled' in blocked['message']
    OrganizationMembership.objects.filter(user=setup['reader']).update(assistant_tool_access={})
    setup['reader'].groups.clear()
    assert tool('How do refunds work?')['data'] == {'message': assistant.NO_ACCESS}
    assert luccy['requests'] == []


@pytest.mark.parametrize('reply', [
    lambda request: httpx.Response(500, json={'error': 'Traceback: SECRET upstream detail'}),
    lambda request: httpx.Response(200, json={'no': 'answer'}),
    lambda request: httpx.Response(200, content=b'not json SECRET'),
    lambda request: (_ for _ in ()).throw(httpx.ConnectError('SECRET host luccy:8787 refused')),
    lambda request: (_ for _ in ()).throw(RuntimeError('SECRET boom')),
])
def test_tool_failures_return_generic_text(setup, luccy, monkeypatch, reply):
    tools, _prompt = build(setup['org'], setup['reader'], monkeypatch)
    luccy['handler'] = reply
    result = tools['safe_ask_document_analyzer']('How do refunds work?')
    assert result == {'status': 'success', 'data': {'message': assistant.UNAVAILABLE}}
    assert 'SECRET' not in json.dumps(result)


def test_tool_failure_log_names_the_status_only(setup, luccy, caplog):
    caplog.set_level(logging.WARNING, logger='document_analyzer')
    luccy['handler'] = lambda request: httpx.Response(401, json={'error': 'SECRET bad key'})
    tool = assistant.tool_for(setup['org'], setup['reader'])
    assert tool.ask_document_analyzer('How do refunds work?') == {'message': assistant.UNAVAILABLE}
    assert [record.getMessage() for record in caplog.records if record.name.startswith('document_analyzer')] == [
        'Document Analyzer call POST /ask failed: HTTP 401.',
        'Document Analyzer tool call failed (UpstreamError, status 502).',
    ]
    assert 'SECRET' not in caplog.text and 'refunds' not in caplog.text


@pytest.mark.parametrize('reply, counted', [
    (lambda request: httpx.Response(503, json={'error': 'Luccy is busy.'}), False),
    (lambda request: httpx.Response(429, json={'error': 'Spending cap.'}), False),
    (lambda request: httpx.Response(400, json={'error': 'Bad question.'}), False),
    (lambda request: (_ for _ in ()).throw(httpx.ConnectError('down')), False),
    # A failure after luccy may have run the model still counts.
    (lambda request: httpx.Response(502, json={'error': 'failed'}), True),
    (lambda request: httpx.Response(504, json={'error': 'slow'}), True),
    (lambda request: httpx.Response(200, json={'no': 'answer'}), True),
])
def test_tool_gives_back_a_question_luccy_never_started(setup, luccy, settings, reply, counted):
    settings.LUCCY_MAX_ASKS_PER_HOUR = 1
    tool = assistant.tool_for(setup['org'], setup['reader'])
    luccy['handler'] = reply
    assert tool.ask_document_analyzer('How do refunds work?') == {'message': assistant.UNAVAILABLE}
    luccy['handler'] = lambda request: httpx.Response(200, json=ANSWER)
    second = tool.ask_document_analyzer('How do refunds work?')
    if counted:
        assert second == {'message': permissions.RATE_LIMITED} and len(luccy['requests']) == 1
    else:
        assert second['answer'] == ANSWER['answer'] and len(luccy['requests']) == 2


def test_a_short_busy_is_waited_out_once(setup, luccy, monkeypatch):
    waits = []
    monkeypatch.setattr(assistant.time, 'sleep', waits.append)
    replies = [httpx.Response(503, headers={'Retry-After': '5'}, json={'error': 'Busy.'}), httpx.Response(200, json=ANSWER)]
    luccy['handler'] = lambda request: replies.pop(0)
    tool = assistant.tool_for(setup['org'], setup['reader'])
    assert tool.ask_document_analyzer('How do refunds work?')['answer'] == ANSWER['answer']
    assert waits == [5] and len(luccy['requests']) == 2


@pytest.mark.parametrize('headers', [{}, {'Retry-After': '16'}])
def test_a_long_or_open_ended_busy_is_not_retried(setup, luccy, monkeypatch, headers):
    waits = []
    monkeypatch.setattr(assistant.time, 'sleep', waits.append)
    luccy['handler'] = lambda request: httpx.Response(503, headers=headers, json={'error': 'Busy.'})
    tool = assistant.tool_for(setup['org'], setup['reader'])
    assert tool.ask_document_analyzer('How do refunds work?') == {'message': assistant.UNAVAILABLE}
    assert waits == [] and len(luccy['requests']) == 1


def test_busy_twice_gives_the_question_back(setup, luccy, settings, monkeypatch):
    settings.LUCCY_MAX_ASKS_PER_HOUR = 1
    monkeypatch.setattr(assistant.time, 'sleep', lambda seconds: None)
    luccy['handler'] = lambda request: httpx.Response(503, headers={'Retry-After': '1'}, json={'error': 'Busy.'})
    tool = assistant.tool_for(setup['org'], setup['reader'])
    assert tool.ask_document_analyzer('How do refunds work?') == {'message': assistant.UNAVAILABLE}
    assert len(luccy['requests']) == 2
    luccy['handler'] = lambda request: httpx.Response(200, json=ANSWER)
    assert tool.ask_document_analyzer('How do refunds work?')['answer'] == ANSWER['answer']


def test_calls_never_outlast_the_agent(setup, monkeypatch, settings):
    settings.LUCCY_TOOL_TIMEOUT = 120
    timeouts = []
    monkeypatch.setattr(assistant.client, 'request_json',
                        lambda *args, read_timeout, **kwargs: timeouts.append(read_timeout) or dict(ANSWER))
    turn = assistant.ChatTurn('How do refunds work?')
    tool = assistant.tool_for(setup['org'], setup['reader'], turn=turn)
    tool.ask_document_analyzer('How do refunds work?')
    turn.set_budget(60)
    tool.ask_document_analyzer('How do refunds work?')
    turn.set_budget(20)  # 5 s left once the margin is kept: not worth starting
    assert tool.ask_document_analyzer('How do refunds work?') == {'message': assistant.UNAVAILABLE}
    assert len(timeouts) == 2 and timeouts[0] == 120 and 40 < timeouts[1] <= 45


def test_earlier_turns_are_built_on_the_first_call_only():
    built = []
    turn = assistant.ChatTurn('Q', earlier=lambda: built.append(1) or [{'who': 'you', 'text': 'before'}])
    assert built == []
    assert turn.payload('Q')['history'] == [{'who': 'you', 'text': 'before'}]
    turn.payload('again')
    assert built == [1]
    failing = assistant.ChatTurn('Q', earlier=lambda: 1 / 0)
    assert 'history' not in failing.payload('Q')


def test_tool_validates_the_question(setup, luccy):
    tool = assistant.tool_for(setup['org'], setup['reader'])
    assert 'at most' in tool.ask_document_analyzer('   ')['message']
    assert 'at most' in tool.ask_document_analyzer('x' * 10001)['message']
    assert luccy['requests'] == []


def test_tool_shares_the_hourly_cap_with_the_page(setup, luccy, settings):
    settings.LUCCY_MAX_ASKS_PER_HOUR = 1
    assert permissions.take_ask(setup['reader'])  # a page question
    tool = assistant.tool_for(setup['org'], setup['reader'])
    assert tool.ask_document_analyzer('How do refunds work?') == {'message': permissions.RATE_LIMITED}
    assert luccy['requests'] == []


def test_tool_call_is_capped_per_turn_and_has_a_status_line():
    assert agent_module._MAX_CALLS_OVERRIDE['ask_document_analyzer'] == 2
    assert views.TOOL_STATUS_MESSAGES['ask_document_analyzer'] == 'Asking the Document Analyzer...'


def client_tools(user):
    client = APIClient()
    client.force_authenticate(user)
    return client.get('/api/me/assistant-tools/').json()['tools']


def test_tool_settings_report_availability_by_organization(setup):
    def analyzer(user):
        client = APIClient()
        client.force_authenticate(user)
        response = client.get('/api/me/assistant-tools/')
        assert response.status_code == 200
        return next(tool for tool in response.json()['tools'] if tool['id'] == 'analyzer')
    reader = analyzer(setup['reader'])
    assert reader['label'] == 'Documents'
    assert reader['description'] == 'Answer from company documents with the Document Analyzer and cite its sources.'
    # The analyzer is the one documents tool: the former document search group is gone.
    assert 'documents' not in [tool['id'] for tool in client_tools(setup['reader'])]
    assert reader['available'] is True and reader['effective'] is True
    # A member without Company access sees "not enabled", never a switch that does nothing.
    plain = analyzer(setup['plain'])
    assert plain['available'] is False and plain['effective'] is False
    outsider = analyzer(setup['outsider'])
    assert outsider['available'] is False and outsider['effective'] is False


@pytest.mark.django_db(transaction=True)
def test_chat_answer_keeps_analyzer_sources_out_of_message_citations(setup, luccy, monkeypatch):
    """End to end through the web worker with an offline model: the analyzer's sources
    live only in the answer text, so the DataGov citation re-check never withholds it."""
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    def model(messages, info):
        returned = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not returned:
            return ModelResponse(parts=[ToolCallPart('safe_ask_document_analyzer', {'question': 'How do refunds work?'})])
        data = returned[0].content['data']
        return ModelResponse(parts=[TextPart(f"From the Document Analyzer: {data['answer']}")])

    real_agent = agent_module.Agent
    monkeypatch.setattr(agent_module, 'Agent', lambda **kwargs: real_agent(**{**kwargs, 'model': FunctionModel(model)}))
    monkeypatch.setattr(views, '_run_with_timeout', lambda function, timeout: function())
    luccy['handler'] = lambda request: httpx.Response(200, json=ANSWER)
    session = ChatSession.objects.create(user=setup['reader'], organization=setup['org'])
    ChatMessage.objects.create(session=session, role='user', content='How do refunds work?')
    views.run_chat_event_sync(session.id, 'How do refunds work?')
    answer = session.messages.get(role='assistant')
    assert answer.citations == []
    assert 'https://wiki.example/refunds' in answer.content
    assert views._serialize_chat_message(answer, session)['content'] == answer.content
    assert len(luccy['requests']) == 1
