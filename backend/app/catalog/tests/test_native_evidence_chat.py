"""Exercise the real web worker boundaries without calling a model provider."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.core.cache import cache
from django.test import override_settings

from catalog import views
from catalog.models import ChatMessage, ChatSession
from catalog.tests.test_document_chat import evidence, client, LOCMEM
from knowledge.permissions import filter_citations
from knowledge.retrieval import REVOKED_ANSWER

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def native_settings():
    with override_settings(DOCUMENT_EXPLORER_ENABLED=True, DOCUMENT_NATIVE_AI_ENABLED=True, CACHES=LOCMEM):
        cache.clear()
        yield
        cache.clear()


def test_native_answer_stays_in_existing_agent_with_verified_citations(rw_user, org, evidence, monkeypatch, client):
    session = ChatSession.objects.create(user=rw_user, organization=org)
    ChatMessage.objects.create(session=session, role='user', content='Explain this definition and policy')
    def build(*args, **kwargs):
        ledger = kwargs['evidence_context']
        def answer(*args, **kw):
            ledger.remember(filter_citations(org, rw_user, [evidence[1]]))
            return SimpleNamespace(output='A catalog definition with private evidence [E1]')
        return SimpleNamespace(run_sync=answer)
    monkeypatch.setattr(views, 'build_chatbot_agent_for_org', build)
    monkeypatch.setattr(views, '_run_with_timeout', lambda function, timeout: function())
    assert views.run_chat_event_sync(session.id, 'Explain this definition and policy') == 'Assistant answer saved.'
    answer = session.messages.get(role='assistant')
    assert answer.citations[0]['citation_id'] == 'E1'
    assert client.get(f'/api/chat/sessions/{session.pk}/messages/').json()[-1]['citations'][0]['citation_id'] == 'E1'
    assert answer.debug_meta.get('calls', []) == []
    evidence[0].allowed_users.clear()
    result = client.get(f'/api/chat/sessions/{session.pk}/messages/').json()
    assert 'private evidence' not in str(result).lower()


def test_revocation_during_native_generation_withholds_whole_answer(rw_user, org, evidence, monkeypatch):
    session = ChatSession.objects.create(user=rw_user, organization=org)
    ChatMessage.objects.create(session=session, role='user', content='Explain policy')
    def build(*args, **kwargs):
        ledger = kwargs['evidence_context']
        def answer(*args, **kw):
            ledger.remember(filter_citations(org, rw_user, [evidence[1]]))
            evidence[0].allowed_users.clear()
            return SimpleNamespace(output='PRIVATE SOURCE PARAPHRASE without an explicit citation')
        return SimpleNamespace(run_sync=answer)
    monkeypatch.setattr(views, 'build_chatbot_agent_for_org', build)
    monkeypatch.setattr(views, '_run_with_timeout', lambda function, timeout: function())
    views.run_chat_event_sync(session.id, 'Explain policy')
    answer = session.messages.get(role='assistant')
    assert answer.content == REVOKED_ANSWER
    assert answer.citations == []


def test_revoked_native_history_never_reaches_model(rw_user, org, evidence, monkeypatch):
    session = ChatSession.objects.create(user=rw_user, organization=org)
    ChatMessage.objects.create(session=session, role='user', content='Old question')
    ChatMessage.objects.create(session=session, role='assistant', content='PRIVATE HISTORICAL ANSWER', citations=[evidence[1]])
    ChatMessage.objects.create(session=session, role='user', content='Follow-up')
    evidence[0].allowed_users.clear()
    model = Mock(return_value=SimpleNamespace(output='No document context available'))
    monkeypatch.setattr(views, 'build_chatbot_agent_for_org', lambda *a, **kw: SimpleNamespace(run_sync=model))
    monkeypatch.setattr(views, '_run_with_timeout', lambda function, timeout: function())
    views.run_chat_event_sync(session.id, 'Follow-up')
    assert 'PRIVATE' not in str(model.call_args)
    assert model.call_args.kwargs['message_history'] == []
