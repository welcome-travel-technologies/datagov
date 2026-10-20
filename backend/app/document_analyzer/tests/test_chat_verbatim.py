"""C6: web chat threads its turn into the Document Analyzer and stores luccy's answer
verbatim when the analyzer was the turn's only tool. The real worker and agent run with
an offline pydantic-ai FunctionModel; luccy is a MockTransport."""
import hashlib
import hmac
import json

import httpx
import pytest
from django.conf import settings
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from rest_framework.test import APIClient

from catalog import views
from catalog.models import ChatMessage, ChatSession
from catalog.tools import agent as agent_module
from document_analyzer import assistant
from document_analyzer import views as analyzer_views

pytestmark = pytest.mark.django_db(transaction=True)

ANSWER = {
    'answer': 'Refunds take **five days** ([Refund policy](https://wiki.example/refunds)).\n\nSee also the FAQ.',
    'answer_html': '<p>x</p>', 'footer': 'Answered in 20 s · ref 0a1b2c3d', 'log': '0a1b2c3d',
    'citations': [{'id': 'conf-1', 'title': 'Refund policy', 'url': 'https://wiki.example/refunds'},
                  {'id': 'wh:sales', 'title': '', 'url': None},
                  {'id': 'jira-9', 'title': 'Ticket [old] *draft*', 'url': 'javascript:alert(1)'}],
    'abstained': False, 'gaps': ['the 2025 numbers'],
}
VERBATIM = (ANSWER['answer'] + '\n\n**Sources**\n- [Refund policy](https://wiki.example/refunds)\n- wh\\:sales\n'
            '- Ticket \\[old\\] \\*draft\\*\n\nAnswered by the Document Analyzer · ref 0a1b2c3d')
AGENT_TEXT = 'AGENT OWN WORDING'
# How the worker marks a stored verbatim analyzer reply.
VERBATIM_META = {'document_analyzer': {'refs': ['0a1b2c3d'], 'verbatim': True, 'ref': '0a1b2c3d'}}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv('GOOGLE_API_KEY', 'test-only-not-a-real-key')
    monkeypatch.setattr(views, '_run_with_timeout', lambda function, timeout: function())


def scripted(*steps):
    """A model that plays ``steps`` in order, one per tool result it has seen:
    ``('call', tool, args)`` or ``('text', words)``."""
    seen = {'tools': None}

    def model(messages, info):
        if seen['tools'] is None:
            seen['tools'] = sorted(tool.name for tool in info.function_tools)
        done = sum(1 for message in messages for part in message.parts if isinstance(part, ToolReturnPart))
        kind, *rest = steps[min(done, len(steps) - 1)]
        if kind == 'call':
            return ModelResponse(parts=[ToolCallPart(rest[0], rest[1])])
        return ModelResponse(parts=[TextPart(rest[0])])
    return model, seen


def use_model(monkeypatch, model):
    monkeypatch.setattr(agent_module, 'Agent', lambda **kwargs: Agent(**{**kwargs, 'model': FunctionModel(model)}))


def chat(setup, message, earlier=(), user='reader'):
    """Run one web-chat turn after ``earlier`` messages: ``(role, content)`` or
    ``(role, content, {"citations": ..., "debug_meta": ...})``."""
    session = ChatSession.objects.create(user=setup[user], organization=setup['org'])
    for role, content, *extra in earlier:
        ChatMessage.objects.create(session=session, role=role, content=content, **(extra[0] if extra else {}))
    ChatMessage.objects.create(session=session, role='user', content=message)
    views.run_chat_event_sync(session.id, message)
    return session, session.messages.filter(role='assistant').order_by('-created_at', '-id').first()


def verbatim(content):
    """An earlier assistant reply the worker stored as the analyzer's verbatim answer."""
    return ('assistant', content, {'debug_meta': VERBATIM_META})


def sent(luccy):
    return [json.loads(request.content) for request in luccy['requests'] if request.url.path == '/ask']


def answers(*bodies):
    replies = list(bodies)

    def handler(request):
        return httpx.Response(200, json=replies.pop(0) if len(replies) > 1 else replies[0])
    return handler


def expected_conversation(session):
    digest = hmac.new(settings.SECRET_KEY.encode(), f'chat:{session.pk}'.encode(), hashlib.sha256).hexdigest()
    return 'dgchat-' + digest[:40]


# ---- the stored reply --------------------------------------------------------------------------


def test_only_the_analyzer_stores_its_answer_verbatim(setup, luccy, monkeypatch):
    model, seen = scripted(('call', 'safe_ask_document_analyzer', {'question': 'agent paraphrase'}), ('text', AGENT_TEXT))
    use_model(monkeypatch, model)
    luccy['handler'] = answers(ANSWER)
    session, reply = chat(setup, 'How long do refunds take?')
    assert reply.content == VERBATIM
    assert reply.citations == []
    assert views._serialize_chat_message(reply, session)['content'] == VERBATIM
    assert reply.debug_meta['document_analyzer'] == {'refs': ['0a1b2c3d'], 'verbatim': True, 'ref': '0a1b2c3d'}
    # Merged into the debug payload, never replacing it.
    assert reply.debug_meta['stats']['tool_call_count'] == 1
    assert [call['tool'] for call in reply.debug_meta['tool_calls']] == ['ask_document_analyzer']
    assert 'safe_ask_document_analyzer' in seen['tools'] and 'safe_get_lineage' in seen['tools']


def test_first_call_asks_the_users_exact_words_in_the_chats_conversation(setup, luccy, monkeypatch):
    model, _seen = scripted(('call', 'safe_ask_document_analyzer', {'question': 'agent paraphrase'}), ('text', AGENT_TEXT))
    use_model(monkeypatch, model)
    luccy['handler'] = answers(ANSWER)
    message = '  how long   do refunds take?? '
    session, _reply = chat(setup, message)
    assert sent(luccy) == [{'question': message, 'conversation': expected_conversation(session)}]


def test_later_calls_ask_the_agents_question_with_the_turn_so_far(setup, luccy, monkeypatch):
    second = {**ANSWER, 'answer': 'Second answer.\nWith more.', 'log': '1b2c3d4e', 'citations': []}
    model, _seen = scripted(('call', 'safe_ask_document_analyzer', {'question': 'paraphrase one'}),
                            ('call', 'safe_ask_document_analyzer', {'question': 'agent follow-up'}), ('text', AGENT_TEXT))
    use_model(monkeypatch, model)
    luccy['handler'] = answers(ANSWER, second)
    long_answer = 'First line B\n' + 'b' * 6000
    earlier = [('user', 'Old question one'), verbatim('First line A\nmore of A'),
               ('user', 'Old question two'), verbatim(long_answer)]
    session, reply = chat(setup, 'New question', earlier)
    first, later = sent(luccy)
    conversation = expected_conversation(session)
    assert first == {'question': 'New question', 'conversation': conversation, 'history': [
        {'who': 'you', 'text': 'Old question one'}, {'who': 'luccy', 'text': 'First line A'},
        {'who': 'you', 'text': 'Old question two'}, {'who': 'luccy', 'text': long_answer[:4000]}]}
    assert later == {'question': 'agent follow-up', 'conversation': conversation, 'history': [
        {'who': 'you', 'text': 'Old question one'}, {'who': 'luccy', 'text': 'First line A'},
        {'who': 'you', 'text': 'Old question two'}, {'who': 'luccy', 'text': 'First line B'},
        {'who': 'you', 'text': 'New question'}, {'who': 'luccy', 'text': ANSWER['answer'][:4000]}]}
    # The last answer is the one stored; both refs are kept.
    assert reply.content == 'Second answer.\nWith more.\n\nAnswered by the Document Analyzer · ref 1b2c3d4e'
    assert reply.debug_meta['document_analyzer'] == {'refs': ['0a1b2c3d', '1b2c3d4e'], 'verbatim': True,
                                                     'ref': '1b2c3d4e'}


def test_mixed_tools_keep_the_agents_text(setup, luccy, monkeypatch):
    model, _seen = scripted(('call', 'safe_get_lineage', {'node_name': 'orders'}),
                            ('call', 'safe_ask_document_analyzer', {'question': 'refunds?'}), ('text', AGENT_TEXT))
    use_model(monkeypatch, model)
    luccy['handler'] = answers(ANSWER)
    _session, reply = chat(setup, 'Refunds and lineage?')
    assert reply.content == AGENT_TEXT
    assert reply.citations == []
    assert reply.debug_meta['document_analyzer'] == {'refs': ['0a1b2c3d'], 'verbatim': False, 'ref': None}
    assert [call['tool'] for call in reply.debug_meta['tool_calls']] == ['get_lineage', 'ask_document_analyzer']


@pytest.mark.parametrize('reply', [
    lambda request: httpx.Response(500, json={'error': 'boom'}),
    lambda request: httpx.Response(200, json={'no': 'answer'}),
    lambda request: httpx.Response(429, json={'error': 'Spending cap.'}),
])
def test_a_failed_analyzer_keeps_the_agents_text(setup, luccy, monkeypatch, reply):
    model, _seen = scripted(('call', 'safe_ask_document_analyzer', {'question': 'refunds?'}), ('text', AGENT_TEXT))
    use_model(monkeypatch, model)
    luccy['handler'] = reply
    _session, stored = chat(setup, 'Refunds?')
    assert stored.content == AGENT_TEXT
    assert 'document_analyzer' not in stored.debug_meta


def test_a_turn_without_tools_keeps_the_agents_text(setup, luccy, monkeypatch):
    model, _seen = scripted(('text', AGENT_TEXT))
    use_model(monkeypatch, model)
    _session, stored = chat(setup, 'Hello')
    assert stored.content == AGENT_TEXT and luccy['requests'] == []


def test_a_failure_then_an_answer_is_still_verbatim(setup, luccy, monkeypatch):
    model, _seen = scripted(('call', 'safe_ask_document_analyzer', {'question': 'one'}),
                            ('call', 'safe_ask_document_analyzer', {'question': 'two'}), ('text', AGENT_TEXT))
    use_model(monkeypatch, model)
    replies = [httpx.Response(502, json={'error': 'x'}), httpx.Response(200, json=ANSWER)]
    luccy['handler'] = lambda request: replies.pop(0)
    _session, stored = chat(setup, 'Refunds?')
    assert stored.content == VERBATIM
    assert [body['question'] for body in sent(luccy)] == ['Refunds?', 'two']


def test_debug_responses_never_follow_a_verbatim_footer(setup, luccy, monkeypatch):
    setup['org'].debug_responses_enabled = True
    setup['org'].save(update_fields=['debug_responses_enabled'])
    model, _seen = scripted(('call', 'safe_ask_document_analyzer', {'question': 'q'}), ('text', AGENT_TEXT))
    use_model(monkeypatch, model)
    luccy['handler'] = answers(ANSWER)
    _session, stored = chat(setup, 'Refunds?')
    assert stored.content == VERBATIM
    # Not verbatim (the analyzer failed): the debug section is appended as before.
    luccy['handler'] = lambda request: httpx.Response(500)
    _session, stored = chat(setup, 'Refunds again?')
    assert stored.content.startswith(AGENT_TEXT + '\n\n---\n') and 'Debug' in stored.content


def test_a_verbatim_answer_is_capped_like_any_answer(setup, luccy, monkeypatch):
    model, _seen = scripted(('call', 'safe_ask_document_analyzer', {'question': 'q'}), ('text', AGENT_TEXT))
    use_model(monkeypatch, model)
    luccy['handler'] = answers({'answer': 'A' * 200000, 'log': '0a1b2c3d', 'citations': []})
    _session, stored = chat(setup, 'Everything?')
    assert stored.content == 'A' * assistant.MAX_ANSWER_CHARS + '\n\nAnswered by the Document Analyzer · ref 0a1b2c3d'


@pytest.mark.parametrize('log', [None, 'not-a-ref', '0A1B2C3D'])
def test_the_footer_always_names_a_ref(setup, luccy, monkeypatch, log):
    model, _seen = scripted(('call', 'safe_ask_document_analyzer', {'question': 'q'}), ('text', AGENT_TEXT))
    use_model(monkeypatch, model)
    luccy['handler'] = answers({'answer': 'Five days.', 'log': log, 'citations': []})
    session, stored = chat(setup, 'Refunds?')
    assert stored.content == 'Five days.\n\nAnswered by the Document Analyzer · ref -'
    assert stored.debug_meta['document_analyzer'] == {'refs': [], 'verbatim': True, 'ref': None}
    assert views._serialize_chat_message(stored, session)['analyzer'] == {'verbatim': True, 'ref': None}


# ---- retries -----------------------------------------------------------------------------------


def test_a_retried_turn_asks_the_users_words_again(setup, luccy, monkeypatch):
    """A transient model error re-runs the whole agent turn; the new attempt's first
    analyzer call must again send the user's exact message, and only its answer is kept."""
    monkeypatch.setattr(views, 'LLM_RETRY_BACKOFF_SECONDS', (0, 0))
    attempts = {'n': 0}

    def model(messages, info):
        returned = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not returned:
            attempts['n'] += 1
            return ModelResponse(parts=[ToolCallPart('safe_ask_document_analyzer', {'question': f'paraphrase {attempts["n"]}'})])
        if attempts['n'] == 1:
            raise ModelHTTPError(status_code=503, model_name='test-model', body=None)
        return ModelResponse(parts=[TextPart(AGENT_TEXT)])
    use_model(monkeypatch, model)
    second = {**ANSWER, 'answer': 'Second attempt answer.', 'log': '1b2c3d4e', 'citations': []}
    luccy['handler'] = answers(ANSWER, second)
    session, stored = chat(setup, 'Refunds, exactly?')
    first, retried = sent(luccy)
    assert attempts['n'] == 2
    assert first['question'] == 'Refunds, exactly?' and retried['question'] == 'Refunds, exactly?'
    # The retry starts the turn over: no exchange of the failed attempt in its history.
    assert 'history' not in retried
    assert stored.content == 'Second attempt answer.\n\nAnswered by the Document Analyzer · ref 1b2c3d4e'
    assert stored.debug_meta['document_analyzer'] == {'refs': ['0a1b2c3d', '1b2c3d4e'], 'verbatim': True,
                                                      'ref': '1b2c3d4e'}


def test_a_retry_without_the_analyzer_keeps_the_agents_text(setup, luccy, monkeypatch):
    """Verbatim is decided on the last attempt's calls only."""
    monkeypatch.setattr(views, 'LLM_RETRY_BACKOFF_SECONDS', (0, 0))
    attempts = {'n': 0}

    def model(messages, info):
        returned = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not returned:
            attempts['n'] += 1
            if attempts['n'] == 1:
                return ModelResponse(parts=[ToolCallPart('safe_ask_document_analyzer', {'question': 'q'})])
            return ModelResponse(parts=[TextPart(AGENT_TEXT)])
        raise ModelHTTPError(status_code=502, model_name='test-model', body=None)
    use_model(monkeypatch, model)
    luccy['handler'] = answers(ANSWER)
    _session, stored = chat(setup, 'Refunds?')
    assert stored.content == AGENT_TEXT
    assert stored.debug_meta['document_analyzer'] == {'refs': ['0a1b2c3d'], 'verbatim': False, 'ref': None}


# ---- history -----------------------------------------------------------------------------------


def test_history_is_reset_at_a_reply_whose_sources_are_no_longer_readable(setup, luccy, monkeypatch):
    model, _seen = scripted(('call', 'safe_ask_document_analyzer', {'question': 'q'}), ('text', AGENT_TEXT))
    use_model(monkeypatch, model)
    luccy['handler'] = answers(ANSWER)
    revoked = [{'document_id': '00000000-0000-0000-0000-000000000001',
                'version_id': '00000000-0000-0000-0000-000000000002', 'chunk_id': 'c1'}]
    earlier = [('user', 'Secret question'), ('assistant', 'PRIVATE ANSWER', {'citations': revoked}),
               ('user', 'Kept question'), verbatim('{"parts": [{"content": "Kept answer"}]}')]
    chat(setup, 'Now?', earlier)
    [body] = sent(luccy)
    assert body['history'] == [{'who': 'you', 'text': 'Kept question'}, {'who': 'luccy', 'text': 'Kept answer'}]
    assert 'PRIVATE' not in json.dumps(body) and 'Secret' not in json.dumps(body)


def test_history_keeps_the_last_few_messages(setup, luccy, monkeypatch):
    model, _seen = scripted(('call', 'safe_ask_document_analyzer', {'question': 'q'}), ('text', AGENT_TEXT))
    use_model(monkeypatch, model)
    luccy['handler'] = answers(ANSWER)
    earlier = [('user', f'turn {n}') if n % 2 == 0 else verbatim(f'turn {n}') for n in range(70)]
    chat(setup, 'Latest', earlier)
    [body] = sent(luccy)
    assert len(body['history']) == views.ANALYZER_HISTORY_MESSAGES == 8
    assert body['history'][-1] == {'who': 'luccy', 'text': 'turn 69'}
    assert body['history'][0] == {'who': 'you', 'text': 'turn 62'}


def test_earlier_turns_are_read_only_when_the_analyzer_is_asked(setup, luccy, monkeypatch):
    read = []
    real = assistant.reply_text
    monkeypatch.setattr(assistant, 'reply_text', lambda text: read.append(text) or real(text))
    earlier = [('user', 'Old question'), verbatim('Old answer')]
    model, _seen = scripted(('text', AGENT_TEXT))
    use_model(monkeypatch, model)
    chat(setup, 'Hello', earlier)
    assert read == [] and luccy['requests'] == []
    model, _seen = scripted(('call', 'safe_ask_document_analyzer', {'question': 'q'}), ('text', AGENT_TEXT))
    use_model(monkeypatch, model)
    luccy['handler'] = answers(ANSWER)
    chat(setup, 'Now?', earlier)
    assert read == ['Old answer']
    assert sent(luccy)[0]['history'] == [{'who': 'you', 'text': 'Old question'}, {'who': 'luccy', 'text': 'Old answer'}]


def test_an_analyzer_failure_never_breaks_the_chat_turn(setup, luccy, monkeypatch):
    from document_analyzer import permissions as analyzer_permissions

    def broken(*args, **kwargs):
        raise RuntimeError('analyzer settings exploded')
    model, seen = scripted(('text', AGENT_TEXT))
    use_model(monkeypatch, model)
    monkeypatch.setattr(analyzer_permissions, 'org_available', broken)
    monkeypatch.setattr(assistant, 'tool_for', broken)
    _session, stored = chat(setup, 'Hello')
    assert stored.content == AGENT_TEXT
    assert 'safe_ask_document_analyzer' not in seen['tools'] and 'safe_get_lineage' in seen['tools']


def test_an_earlier_verbatim_reply_reaches_luccy_as_its_own_answer(setup, luccy, monkeypatch):
    model, _seen = scripted(('call', 'safe_ask_document_analyzer', {'question': 'q'}), ('text', AGENT_TEXT))
    use_model(monkeypatch, model)
    luccy['handler'] = answers(ANSWER)
    chat(setup, 'Next?', [('user', 'How long do refunds take?'), verbatim(VERBATIM)])
    [body] = sent(luccy)
    assert body['history'] == [{'who': 'you', 'text': 'How long do refunds take?'},
                               {'who': 'luccy', 'text': ANSWER['answer']}]


def test_history_never_carries_another_tools_reply(setup, luccy, monkeypatch, settings):
    """Only the user's turns and the analyzer's own verbatim answers go to luccy: an
    earlier native-search answer (restricted collection), a BigQuery answer, a mixed-tool
    answer and a reply that merely ends like a verbatim one all stay in DataGov. As for
    the agent's own history, everything before a reply with DataGov sources is dropped."""
    import hashlib as _hashlib

    from django.utils import timezone

    from knowledge.models import Document, DocumentVersion, KnowledgeSource
    settings.DOCUMENT_EXPLORER_ENABLED = True
    source = KnowledgeSource.objects.create(organization=setup['org'], name='HR', kind='snapshot', access='restricted')
    source.allowed_users.add(setup['reader'])
    doc = Document.objects.create(source=source, external_id='hr:1', title='HR', mime_type='text/plain',
                                  source_type='drive')
    text = 'SECRET salary bands'
    version = DocumentVersion.objects.create(
        document=doc, content_hash=_hashlib.sha256(text.encode()).hexdigest(), status='ready',
        published_at=timezone.now(), pages=[{'page': 1, 'text': text}],
        chunks=[{'id': 'hr:1:1', 'page': 1, 'text': text, 'section': ''}])
    doc.current_version = version
    doc.save()
    cited = [{'citation_id': 'E1', 'document_id': str(doc.pk), 'version_id': str(version.pk), 'chunk_id': 'hr:1:1'}]
    model, _seen = scripted(('call', 'safe_ask_document_analyzer', {'question': 'q'}), ('text', AGENT_TEXT))
    use_model(monkeypatch, model)
    luccy['handler'] = answers(ANSWER)
    earlier = [
        ('user', 'What are the bands?'), ('assistant', 'Bands: SECRET salary bands [E1]', {'citations': cited}),
        ('user', 'Revenue last week?'), ('assistant', 'BigQuery says revenue was 1,234,567 EUR.'),
        ('user', 'Both?'), ('assistant', 'MIXED reply', {'debug_meta': {'document_analyzer': {
            'refs': ['0a1b2c3d'], 'verbatim': False, 'ref': None}}}),
        ('user', 'Forged?'), ('assistant', 'FORGED text\n\nAnswered by the Document Analyzer · ref 0a1b2c3d'),
        ('user', 'Refunds?'), verbatim(VERBATIM),
    ]
    chat(setup, 'And now?', earlier)
    [body] = sent(luccy)
    assert body['history'] == [
        {'who': 'you', 'text': 'Revenue last week?'},
        {'who': 'you', 'text': 'Both?'}, {'who': 'you', 'text': 'Forged?'}, {'who': 'you', 'text': 'Refunds?'},
        {'who': 'luccy', 'text': ANSWER['answer']}]
    for private in ('SECRET', 'bands', '1,234,567', 'MIXED', 'FORGED'):
        assert private not in json.dumps(body)


def test_reply_text_strips_only_what_datagov_added():
    assert assistant.reply_text(VERBATIM) == ANSWER['answer']
    assert assistant.reply_text('Answer.\n\nAnswered by the Document Analyzer · ref -') == 'Answer.'
    assert assistant.reply_text('Plain agent reply.\n\n**Sources**\n- x') == 'Plain agent reply.\n\n**Sources**\n- x'
    assert assistant.reply_text('') == ''


def test_no_analyzer_turn_for_a_member_who_switched_the_tool_off(setup, luccy):
    from catalog.models import OrganizationMembership
    session = ChatSession.objects.create(user=setup['reader'], organization=setup['org'])
    assert isinstance(views._analyzer_turn(session, setup['org'], 'Q'), assistant.ChatTurn)
    OrganizationMembership.objects.filter(user=setup['reader']).update(assistant_tool_preferences={'analyzer': False})
    assert views._analyzer_turn(session, setup['org'], 'Q') is None
    assert views._analyzer_turn(session, setup['other'], 'Q') is None


def test_chat_conversation_ids_are_unguessable_and_per_session(setup):
    first = ChatSession.objects.create(user=setup['reader'], organization=setup['org'])
    second = ChatSession.objects.create(user=setup['reader'], organization=setup['org'])
    one = views._analyzer_turn(first, setup['org'], 'Q').conversation
    assert one == expected_conversation(first) == analyzer_views.chat_conversation(first)
    assert one == views._analyzer_turn(first, setup['org'], 'Q again').conversation
    assert one != views._analyzer_turn(second, setup['org'], 'Q').conversation
    assert one.startswith('dgchat-') and len(one) == 47 <= 64
    assert one != f'dgchat-{first.pk}'
    # A page thread of the same number is another conversation.
    assert analyzer_views.page_conversation(setup['reader'], str(first.pk)) != one


def test_condense_history_matches_luccys_page():
    turns = [{'who': 'you', 'text': '  first  '}, {'who': 'luccy', 'text': 'A1\nA2'}, {'who': 'you', 'text': ''},
             {'who': 'someone', 'text': 'counts as you'}, {'who': 'luccy', 'text': '\n\nB1\r\nB2'},
             {'who': 'luccy', 'text': 'C' * 5000}, {'who': 'you', 'text': 7}]
    assert assistant.condense_history(turns) == [
        {'who': 'you', 'text': 'first'}, {'who': 'luccy', 'text': 'A1'}, {'who': 'you', 'text': 'counts as you'},
        {'who': 'luccy', 'text': 'B1'}, {'who': 'luccy', 'text': 'C' * 4000}]
    assert assistant.condense_history(turns, cap=2) == [{'who': 'luccy', 'text': 'B1'}, {'who': 'luccy', 'text': 'C' * 4000}]


def test_condense_history_stays_inside_the_request_bounds():
    turns = [{'who': 'you', 'text': 'x' * 20000} for _ in range(6)]
    history = assistant.condense_history(turns)
    assert all(len(turn['text']) == 16000 for turn in history)
    assert len(history) == 4 and sum(len(turn['text']) for turn in history) <= 64000


# ---- serialization -----------------------------------------------------------------------------


def test_serialized_messages_say_whether_the_analyzer_answered(setup, luccy, monkeypatch):
    model, _seen = scripted(('call', 'safe_ask_document_analyzer', {'question': 'q'}), ('text', AGENT_TEXT))
    use_model(monkeypatch, model)
    luccy['handler'] = answers(ANSWER)
    session, _stored = chat(setup, 'Refunds?', [
        ('user', 'Lineage?'), ('assistant', 'Plain reply that ends with Answered by the Document Analyzer · ref 0a1b2c3d'),
        ('user', 'Mixed?'), ('assistant', 'Mixed', {'debug_meta': {'document_analyzer': {'refs': ['0a1b2c3d'],
                                                                                          'verbatim': False}}}),
        ('user', 'Old?'), ('assistant', 'Old verbatim', {'debug_meta': {'document_analyzer': {
            'refs': ['0a1b2c3d', '1b2c3d4e'], 'verbatim': True}}}),
    ])
    client = APIClient()
    client.force_login(setup['reader'])
    messages = client.get(f'/api/chat/sessions/{session.pk}/messages/').json()
    assert [message['analyzer'] for message in messages] == [
        None, None, None, {'verbatim': False, 'ref': None}, None, {'verbatim': True, 'ref': '1b2c3d4e'},
        None, {'verbatim': True, 'ref': '0a1b2c3d'}]


def test_serialized_analyzer_meta_ignores_odd_values():
    assert assistant.analyzer_meta(None) is None
    assert assistant.analyzer_meta({'document_analyzer': 'x'}) is None
    assert assistant.analyzer_meta({'document_analyzer': {'verbatim': 'yes', 'ref': '<script>'}}) == {
        'verbatim': False, 'ref': None}


# ---- rendering ---------------------------------------------------------------------------------


@pytest.mark.parametrize('source, line', [
    ({'title': 'Plain', 'url': 'https://x.example/a(b)c<d>é'}, '[Plain](https://x.example/a%28b%29c%3Cd%3E%C3%A9)'),
    ({'title': 'Spaced', 'url': 'https://x.example/a b'}, 'Spaced'),
    ({'title': '# Heading', 'url': None}, '\\# Heading'),
    ({'title': '1. First', 'url': None}, '1\\. First'),
    ({'title': 'a\nb  <c>', 'url': None}, 'a b \\<c\\>'),
    ({'title': 'Creds', 'url': 'https://user:pw@x.example/'}, 'Creds'),
    ({'title': 'Data', 'url': 'data:text/html,hi'}, 'Data'),
    ({'title': 'Rel', 'url': '/relative'}, 'Rel'),
    ({'title': 'No host', 'url': 'https:///path'}, 'No host'),
    ({'title': 'Port only', 'url': 'http://:80/x'}, 'Port only'),
    ({'title': '', 'url': 'http://x.example'}, '[Untitled](http://x.example)'),
    # Titles never become GFM autolinks (scheme, www. or e-mail literals).
    ({'title': 'https://evil.example/x', 'url': None}, 'https\\:\\/\\/evil\\.example\\/x'),
    ({'title': 'See www.evil.example now', 'url': None}, 'See www\\.evil\\.example now'),
    ({'title': 'mail a@b.example', 'url': None}, 'mail a\\@b\\.example'),
    ({'title': 'mailto:a@b.example', 'url': 'https://x.example/'}, '[mailto\\:a\\@b\\.example](https://x.example/)'),
    ({'title': '![img](https://evil.example/p.png)', 'url': None},
     '\\!\\[img\\]\\(https\\:\\/\\/evil\\.example\\/p\\.png\\)'),
])
def test_source_lines_are_safe_markdown(source, line):
    text = assistant.render_verbatim({'answer': 'A', 'sources': [source], 'ref': None})
    assert text == f'A\n\n**Sources**\n- {line}\n\nAnswered by the Document Analyzer · ref -'


# ---- documents mode ----------------------------------------------------------------------------
def documents_chat(setup, message, user='reader'):
    """One documents-mode turn: the Document Analyzer alone answers it, never the agent."""
    session = ChatSession.objects.create(user=setup[user], organization=setup['org'], mode='documents')
    ChatMessage.objects.create(session=session, role='user', content=message)
    result = views.run_chat_event_sync(session.id, message)
    return result, session.messages.filter(role='assistant').order_by('-created_at', '-id').first()


@pytest.fixture
def no_agent(monkeypatch):
    monkeypatch.setattr(views, 'build_chatbot_agent_for_org',
                        lambda *a, **k: pytest.fail('Documents mode never runs the catalog agent'))


def test_documents_mode_answers_with_the_analyzer_verbatim(setup, luccy, no_agent):
    luccy['handler'] = answers(ANSWER)
    result, reply = documents_chat(setup, 'How long do refunds take?')
    assert reply.content == VERBATIM and reply.citations == []
    assert reply.debug_meta == VERBATIM_META
    assert result == 'Document Analyzer answer saved.'           # no document text in Task.result
    assert sent(luccy)[0]['question'] == 'How long do refunds take?'


def test_documents_mode_stores_the_analyzers_own_message_when_it_cannot_answer(setup, luccy, no_agent):
    luccy['handler'] = lambda request: httpx.Response(503, json={'detail': 'busy'})
    _result, reply = documents_chat(setup, 'How long do refunds take?')
    assert reply.content == assistant.UNAVAILABLE and reply.citations == []
    assert len(sent(luccy)) == 1


def test_documents_mode_without_analyzer_access_never_asks_luccy(setup, luccy, no_agent):
    from catalog.models import OrganizationMembership
    OrganizationMembership.objects.filter(user=setup['reader']).update(assistant_tool_access={'analyzer': False})
    _result, reply = documents_chat(setup, 'How long do refunds take?')
    assert reply.content == assistant.NO_ACCESS
    _result, reply = documents_chat(setup, 'How long do refunds take?', user='plain')
    assert reply.content == assistant.NO_ACCESS
    assert luccy['requests'] == []


def test_documents_mode_calls_stay_inside_the_workers_limit(setup, luccy, no_agent, settings, monkeypatch):
    settings.LUCCY_TOOL_TIMEOUT = 200
    seen = []
    real = assistant.DocumentAnalyzerTool._seconds
    monkeypatch.setattr(assistant.DocumentAnalyzerTool, '_seconds', lambda self: seen.append(real(self)) or seen[-1])
    luccy['handler'] = answers(ANSWER)
    documents_chat(setup, 'How long do refunds take?')
    assert seen and all(180 < value <= 200 for value in seen)
    assert views._documents_timeout(setup['org']) == 200 + assistant.AGENT_MARGIN_SECONDS


def test_a_new_documents_chat_needs_the_analyzer(setup, luccy, monkeypatch):
    queued = []
    monkeypatch.setattr(views, 'async_task', lambda *args, **kwargs: queued.append(kwargs) or 'task-1')
    client = APIClient()
    client.force_authenticate(setup['plain'])                   # a member without analyzer access
    response = client.post('/api/chat/', {'message': 'Docs?', 'mode': 'documents'}, format='json')
    assert response.status_code == 403 and response.json()['error'] == assistant.NO_ACCESS
    client.force_authenticate(setup['reader'])
    response = client.post('/api/chat/', {'message': 'Docs?', 'mode': 'documents'}, format='json')
    assert response.status_code == 200
    assert ChatSession.objects.get(pk=response.json()['session_id']).mode == 'documents'
    assert queued[-1]['timeout'] == views._documents_timeout(setup['org']) + 30
    from catalog.models import OrganizationMembership
    OrganizationMembership.objects.filter(user=setup['reader']).update(assistant_tool_preferences={'analyzer': False})
    assert client.post('/api/chat/', {'message': 'Docs?', 'mode': 'documents'}, format='json').status_code == 403
