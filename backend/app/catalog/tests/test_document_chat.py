"""Document chat tenant, citation and analyzer-only boundaries (no LLM or luccy calls)."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from catalog import views
from catalog.models import ChatMessage, ChatSession, Organization, OrganizationMembership
from document_analyzer import assistant
from document_analyzer import client as analyzer_client
from knowledge.models import Document, DocumentVersion, KnowledgeSource

pytestmark = pytest.mark.django_db
LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}


@pytest.fixture(autouse=True)
def document_settings(org):
    # The Document Analyzer is on and bound to ``org``; rw_user is in the Company group.
    with override_settings(DOCUMENT_EXPLORER_ENABLED=True, CACHES=LOCMEM, LUCCY_ENABLED=True, LUCCY_CHAT_ENABLED=True,
                           LUCCY_API_URL='http://luccy:8787/', LUCCY_API_KEY='test-luccy-key-with-no-real-access',
                           LUCCY_ORGANIZATION_ID=str(org.pk)):
        cache.clear()
        yield
        cache.clear()


@pytest.fixture
def client(rw_user):
    result = APIClient()
    result.force_authenticate(user=rw_user)
    result.force_login(rw_user)
    return result


@pytest.fixture
def luccy(monkeypatch):
    """luccy's ``POST /ask``, recorded; it answers ``state['body']``."""
    state = {'body': {'answer': 'Luccy answer', 'citations': [], 'log': '0a1b2c3d'}, 'asked': []}

    def request_json(method, path, user, **kwargs):
        state['asked'].append(kwargs['json'])
        return state['body']
    monkeypatch.setattr(analyzer_client, 'request_json', request_json)
    return state


@pytest.fixture
def evidence(org, rw_user):
    source = KnowledgeSource.objects.create(organization=org, name='Private PDFs', kind='upload')
    source.allowed_users.add(rw_user)
    document = Document.objects.create(source=source, external_id='file', title='Private PDF', mime_type='application/pdf')
    version = DocumentVersion.objects.create(document=document, content_hash='a' * 64,
        status='ready', published_at=timezone.now(), chunks=[{'id': 'chunk-1', 'text': 'Private evidence', 'page': 2}])
    document.current_version = version
    document.save()
    return source, {'document_id': str(document.id), 'version_id': str(version.id), 'chunk_id': 'chunk-1'}


def test_document_mode_enqueues_pinned_session(client, rw_user, org, monkeypatch):
    enqueue = Mock(return_value='document-task')
    monkeypatch.setattr(views, 'async_task', enqueue)
    response = client.post('/api/chat/', {'message': 'What does the PDF say?', 'mode': 'documents'}, format='json')
    assert response.status_code == 200
    session = ChatSession.objects.get(pk=response.json()['session_id'])
    assert session.mode == 'documents'
    assert session.organization_id == org.pk
    assert enqueue.call_count == 1
    assert client.get('/api/chat/sessions/').json()[0]['mode'] == 'documents'


@pytest.mark.parametrize('settings_change', [{'LUCCY_ENABLED': False}, {'LUCCY_ORGANIZATION_ID': '0'}])
def test_new_document_mode_needs_the_analyzer_not_the_library(client, monkeypatch, settings_change):
    enqueue = Mock(return_value='document-task')
    monkeypatch.setattr(views, 'async_task', enqueue)
    with override_settings(**settings_change):
        response = client.post('/api/chat/', {'message': 'Docs?', 'mode': 'documents'}, format='json')
    assert response.status_code == 403 and response.json()['error'] == assistant.NO_ACCESS
    with override_settings(DOCUMENT_EXPLORER_ENABLED=False):
        assert client.post('/api/chat/', {'message': 'Docs?', 'mode': 'documents'}, format='json').status_code == 200
    assert enqueue.call_count == 1


def test_document_job_timeout_covers_the_analyzers_budget_when_catalog_budget_is_short(client, org, monkeypatch, settings):
    org.chat_timeout_seconds = 30
    org.save(update_fields=['chat_timeout_seconds'])
    settings.LUCCY_TOOL_TIMEOUT = 200
    enqueue = Mock(return_value='document-task')
    monkeypatch.setattr(views, 'async_task', enqueue)
    response = client.post('/api/chat/', {'message': 'Document question', 'mode': 'documents'}, format='json')
    assert response.status_code == 200
    assert enqueue.call_args.kwargs['timeout'] == 200 + assistant.AGENT_MARGIN_SECONDS + 30


def test_document_answer_is_the_analyzers_without_catalog_rewrite(rw_user, org, luccy, monkeypatch):
    session = ChatSession.objects.create(user=rw_user, organization=org, mode='documents')
    ChatMessage.objects.create(session=session, role='user', content='PDF question')
    catalog = Mock(side_effect=AssertionError('Document answer must not go through catalog model'))
    monkeypatch.setattr(views, 'build_chatbot_agent_for_org', catalog)
    assert views.run_chat_event_sync(session.pk, 'PDF question') == 'Document Analyzer answer saved.'
    result = session.messages.get(role='assistant')
    assert result.content == 'Luccy answer\n\nAnswered by the Document Analyzer · ref 0a1b2c3d'
    assert result.citations == []
    assert result.debug_meta['document_analyzer']['verbatim'] is True
    assert luccy['asked'][0]['question'] == 'PDF question'
    catalog.assert_not_called()


def test_source_revocation_redacts_saved_answer_and_links(client, rw_user, org, evidence):
    source, citation = evidence
    session = ChatSession.objects.create(user=rw_user, organization=org, mode='documents')
    message = ChatMessage.objects.create(session=session, role='assistant', content='Private evidence answer', citations=[citation])
    url = f'/api/chat/sessions/{session.pk}/messages/'
    visible = client.get(url).json()[0]
    assert visible['id'] == message.pk
    assert visible['citations'][0]['datagov_url'].startswith('/knowledge/documents/')
    source.allowed_users.clear()
    redacted = client.get(url).json()[0]
    assert 'Private evidence' not in redacted['content']
    assert redacted['citations'] == []


def test_earlier_document_session_still_opens_and_hides_unreadable_sources(client, rw_user, org, evidence):
    _source, citation = evidence
    session = ChatSession.objects.create(user=rw_user, organization=org, mode='documents')
    ChatMessage.objects.create(session=session, role='assistant', content='Private evidence answer', citations=[citation])
    with override_settings(DOCUMENT_EXPLORER_ENABLED=False):
        response = client.get(f'/api/chat/sessions/{session.pk}/messages/')
    assert response.status_code == 200
    assert 'Private evidence' not in response.json()[0]['content'] and response.json()[0]['citations'] == []


def test_task_result_rechecks_sources_instead_of_returning_stale_queue_text(client, rw_user, org, evidence):
    from django_q.models import Task
    source, citation = evidence
    session = ChatSession.objects.create(user=rw_user, organization=org, mode='documents')
    ChatMessage.objects.create(session=session, role='assistant', content='Private evidence answer', citations=[citation])
    task = Task.objects.create(id='f' * 32, name='doc-answer', func='catalog.views.run_chat_event_sync',
        started=timezone.now(), stopped=timezone.now(), success=True, result='Private stale queue text')
    cache.set(f'chat_task_session_{task.pk}', session.pk)
    source.allowed_users.clear()
    response = client.get(f'/api/chat/task/{task.pk}/')
    assert response.status_code == 200
    assert response.json()['status'] == 'completed'
    assert 'Private' not in response.json()['result']


def test_cited_reply_and_everything_before_it_stay_out_of_agent_history(rw_user, org, evidence, monkeypatch):
    session = ChatSession.objects.create(user=rw_user, organization=org)
    ChatMessage.objects.create(session=session, role='user', content='Old question')
    ChatMessage.objects.create(session=session, role='assistant', content='PRIVATE HISTORICAL ANSWER', citations=[evidence[1]])
    ChatMessage.objects.create(session=session, role='user', content='Follow-up')
    model = Mock(return_value=SimpleNamespace(output='No document context available', new_messages=lambda: []))
    monkeypatch.setattr(views, 'build_chatbot_agent_for_org', lambda *a, **kw: SimpleNamespace(run_sync=model))
    monkeypatch.setattr(views, '_run_with_timeout', lambda function, timeout: function())
    views.run_chat_event_sync(session.id, 'Follow-up')
    assert 'PRIVATE' not in str(model.call_args)
    assert model.call_args.kwargs['message_history'] == []
    assert session.messages.filter(role='assistant').last().citations == []


def test_membership_revocation_hides_session_and_stops_worker(client, rw_user, org, luccy):
    session = ChatSession.objects.create(user=rw_user, organization=org, mode='documents')
    OrganizationMembership.objects.filter(user=rw_user).delete()
    assert client.get(f'/api/chat/sessions/{session.pk}/messages/').status_code == 404
    assert client.get('/api/chat/sessions/').json() == []
    views.run_chat_event_sync(session.pk, 'retry')
    assert luccy['asked'] == []


def test_existing_thread_cannot_switch_modes(client, rw_user, org, monkeypatch):
    enqueue = Mock()
    monkeypatch.setattr(views, 'async_task', enqueue)
    session = ChatSession.objects.create(user=rw_user, organization=org, mode='catalog')
    response = client.post('/api/chat/', {'session_id': session.pk, 'message': 'question', 'mode': 'documents'}, format='json')
    assert response.status_code == 400
    enqueue.assert_not_called()


def test_ambiguous_legacy_thread_is_not_bound_to_first_membership(client, rw_user, org):
    other = Organization.objects.create(name='Other tenant')
    OrganizationMembership.objects.create(user=rw_user, organization=other)
    session = ChatSession.objects.create(user=rw_user)
    assert views._get_org_for_session(session) is None
    session.refresh_from_db()
    assert session.organization_id is None
    assert client.get(f'/api/chat/sessions/{session.pk}/messages/').status_code == 404
    assert client.post('/api/chat/', {'message': 'new', 'mode': 'documents'}, format='json').status_code == 403


def test_bound_session_keeps_its_original_org_after_second_membership(rw_user, org):
    session = ChatSession.objects.create(user=rw_user, organization=org)
    other = Organization.objects.create(name='Second tenant')
    OrganizationMembership.objects.create(user=rw_user, organization=other)
    assert views._get_org_for_session(session) == org


def test_document_error_does_not_expose_service_secrets(rw_user, org, monkeypatch):
    session = ChatSession.objects.create(user=rw_user, organization=org, mode='documents')
    monkeypatch.setattr(views, '_documents_via_analyzer', Mock(side_effect=RuntimeError('service-secret-value')))
    result = views.run_chat_event_sync(session.pk, 'question')
    assert 'service-secret-value' not in result
    assert session.messages.get().content == assistant.UNAVAILABLE
