"""Slack request verification and audience isolation without Slack/LLM calls."""
import hashlib
import hmac
import json
import time
from unittest.mock import Mock

import pytest
from django.core.cache import cache
from django.test import Client, RequestFactory, override_settings
from rest_framework.test import APIRequestFactory

from catalog import slack_views
from catalog.models import DataPerson, Organization, OrganizationMembership
from catalog.serializers import DataPersonSerializer

pytestmark = pytest.mark.django_db
SECRET = 'offline-test-signing-secret'
LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}


@pytest.fixture(autouse=True)
def slack_settings():
    with override_settings(SLACK_SIGNING_SECRET=SECRET, CACHES=LOCMEM,
                           DOCUMENT_EXPLORER_ENABLED=True,
                           DOCUMENT_EXPLORER_PUBLIC_URL='https://datagov.example'):
        cache.clear()
        yield
        cache.clear()


def _signed_request(data, timestamp=None, signature=None):
    body = json.dumps(data).encode()
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    signature = signature or 'v0=' + hmac.new(SECRET.encode(), b'v0:' + timestamp.encode() + b':' + body, hashlib.sha256).hexdigest()
    return RequestFactory().post('/api/slack/events/', body, content_type='application/json',
        HTTP_X_SLACK_REQUEST_TIMESTAMP=timestamp, HTTP_X_SLACK_SIGNATURE=signature)


def _event(team='TONE', event_id='Ev001'):
    return {'type': 'event_callback', 'team_id': team, 'event_id': event_id,
            'event': {'type': 'message', 'channel_type': 'im', 'channel': 'D123', 'user': 'U123', 'text': 'documents: policy', 'ts': '1.1'}}


@pytest.mark.parametrize('timestamp,signature', [(1, None), (None, 'v0=forged')])
def test_invalid_signature_or_stale_timestamp_never_enqueues(timestamp, signature, monkeypatch):
    enqueue = Mock()
    monkeypatch.setattr(slack_views, 'async_task', enqueue)
    assert slack_views.slack_events(_signed_request(_event(), timestamp, signature)).status_code == 403
    enqueue.assert_not_called()


def test_unsigned_challenge_is_rejected():
    response = Client().post('/api/slack/events/', {'type': 'url_verification', 'challenge': 'test'}, content_type='application/json')
    assert response.status_code == 403


def test_signed_challenge_is_accepted():
    response = slack_views.slack_events(_signed_request({'type': 'url_verification', 'challenge': 'test'}))
    assert json.loads(response.content)['challenge'] == 'test'


def test_workspace_mapping_and_event_deduplication(org, monkeypatch):
    org.slack_team_id, org.slack_bot_token = 'TONE', 'token-one'
    org.save()
    Organization.objects.create(name='Other', slack_team_id='TTWO', slack_bot_token='token-two')
    enqueue = Mock()
    monkeypatch.setattr(slack_views, 'async_task', enqueue)
    assert slack_views.slack_events(_signed_request(_event('TTWO'))).status_code == 200
    assert slack_views.slack_events(_signed_request(_event('TTWO'))).status_code == 200
    assert enqueue.call_count == 1
    assert enqueue.call_args.args[2] == 'token-two'
    assert enqueue.call_args.args[1]['_slack_team_id'] == 'TTWO'


def test_unknown_workspace_does_not_fall_back_to_first_token(org, monkeypatch):
    org.slack_bot_token = 'token-one'
    org.save()
    enqueue = Mock()
    monkeypatch.setattr(slack_views, 'async_task', enqueue)
    assert slack_views.slack_events(_signed_request(_event('UNKNOWN'))).status_code == 200
    enqueue.assert_not_called()


def test_queue_failure_allows_signed_retry(org, monkeypatch):
    org.slack_team_id, org.slack_bot_token = 'TONE', 'token'
    org.save()
    enqueue = Mock(side_effect=[RuntimeError('queue unavailable'), 'task'])
    monkeypatch.setattr(slack_views, 'async_task', enqueue)
    assert slack_views.slack_events(_signed_request(_event())).status_code == 503
    assert slack_views.slack_events(_signed_request(_event())).status_code == 200
    assert enqueue.call_count == 2


@pytest.mark.parametrize('event', [
    {'channel_type': 'channel', 'channel': 'C123', 'user': 'U123'},
    {'channel_type': 'mpim', 'channel': 'G123', 'user': 'U123'},
    {'channel_type': 'im', 'channel': 'D123', 'user': 'UNMAPPED'},
])
def test_document_channel_and_unmapped_requests_never_retrieve(org, event, monkeypatch):
    import knowledge.chat
    retrieve = Mock(side_effect=AssertionError('Private retrieval must not run'))
    monkeypatch.setattr(knowledge.chat, 'answer_document_question', retrieve)
    output = slack_views._slack_document_reply(org, event, 'private question')
    assert 'https://datagov.example/knowledge' in output
    retrieve.assert_not_called()


def test_dm_identity_and_citations_are_rechecked(org, rw_user, monkeypatch):
    import knowledge.chat
    import knowledge.permissions
    DataPerson.objects.create(organization=org, user=rw_user, name='Mapped', slack_user_id='U123')
    retrieve = Mock(return_value={'answer': 'Private answer', 'citations': [{'version_id': 'revoked'}]})
    monkeypatch.setattr(knowledge.chat, 'answer_document_question', retrieve)
    monkeypatch.setattr(knowledge.permissions, 'filter_citations', Mock(return_value=[]))
    output = slack_views._slack_document_reply(org, {'channel_type': 'im', 'channel': 'D123', 'user': 'U123'}, 'question')
    assert 'Private answer' not in output
    assert retrieve.call_args.args == (org, rw_user, 'question')


def test_dm_uses_only_validated_datagov_citation_urls(org, rw_user, monkeypatch):
    import knowledge.chat
    import knowledge.permissions
    DataPerson.objects.create(organization=org, user=rw_user, name='Mapped', slack_user_id='U123')
    citations = [{'datagov_url': '/knowledge/documents/abc?version=xyz&page=2', 'source_url': 'https://source.example/private'}]
    monkeypatch.setattr(knowledge.chat, 'answer_document_question', Mock(return_value={'answer': 'Grounded answer', 'citations': citations}))
    monkeypatch.setattr(knowledge.permissions, 'filter_citations', Mock(return_value=citations))
    output = slack_views._slack_document_reply(org, {'channel_type': 'im', 'channel': 'D123', 'user': 'U123'}, 'question')
    assert 'https://datagov.example/knowledge/documents/abc?version=xyz&page=2' in output
    assert 'source.example' not in output


def test_worker_dispatches_document_dm_without_catalog_model_or_history(org, monkeypatch):
    org.slack_team_id, org.slack_bot_token = 'TONE', 'token-one'
    org.save()
    slack = Mock()
    slack.auth_test.return_value = {'team_id': 'TONE', 'user_id': 'UBOT'}
    monkeypatch.setattr(slack_views, 'WebClient', Mock(return_value=slack))
    monkeypatch.setattr(slack_views, '_slack_document_reply', Mock(return_value='Verified answer'))
    catalog = Mock(side_effect=AssertionError('No catalog rewrite'))
    monkeypatch.setattr(slack_views, 'build_chatbot_agent_for_org', catalog)
    event = _event()['event'] | {'_slack_team_id': 'TONE'}
    assert slack_views.process_slack_event(event, 'token-one') == 'Document Explorer reply sent.'
    assert slack.chat_postMessage.call_args.kwargs['text'] == 'Verified answer'
    assert slack.chat_postMessage.call_args.kwargs['unfurl_links'] is False
    slack.conversations_replies.assert_not_called()
    catalog.assert_not_called()


def test_worker_token_team_mismatch_never_posts(org, monkeypatch):
    org.slack_team_id, org.slack_bot_token = 'TONE', 'token-one'
    org.save()
    slack = Mock()
    slack.auth_test.return_value = {'team_id': 'TOTHER', 'user_id': 'UBOT'}
    monkeypatch.setattr(slack_views, 'WebClient', Mock(return_value=slack))
    event = _event()['event'] | {'_slack_team_id': 'TONE'}
    assert 'do not match' in slack_views.process_slack_event(event, 'token-one')
    slack.chat_postMessage.assert_not_called()
    slack.conversations_replies.assert_not_called()


def test_slack_identity_mapping_is_not_writable_by_regular_member(org, rw_user):
    person = DataPerson.objects.create(organization=org, user=rw_user, name='Mapped')
    request = APIRequestFactory().patch('/')
    request.user = rw_user
    serializer = DataPersonSerializer(person, data={'slack_user_id': 'U123'}, partial=True, context={'request': request})
    assert not serializer.is_valid()
    assert 'slack_user_id' in serializer.errors


def test_mapped_person_cannot_be_reassigned_by_regular_member(org, rw_user):
    person = DataPerson.objects.create(organization=org, user=rw_user, name='Mapped', slack_user_id='U123')
    request = APIRequestFactory().patch('/')
    request.user = rw_user
    serializer = DataPersonSerializer(person, data={'user': None}, partial=True, context={'request': request})
    assert not serializer.is_valid()
    assert 'slack_user_id' in serializer.errors


def test_admin_can_save_workspace_mapping_and_disconnect_clears_it(org, rw_user):
    OrganizationMembership.objects.filter(user=rw_user, organization=org).update(is_admin=True)
    client = Client()
    client.force_login(rw_user)
    response = client.post('/api/integrations/hooks/save/',
        {'name': 'Slack', 'slack_team_id': 'T123', 'slack_bot_token': 'test-token'}, content_type='application/json')
    assert response.status_code == 200
    org.refresh_from_db()
    assert org.slack_team_id == 'T123'
    assert client.get('/api/integrations/').json()['hooks'][0]['slack_team_id'] == 'T123'
    response = client.post('/api/integrations/hooks/save/', {'id': response.json()['id'], 'disconnect': True}, content_type='application/json')
    assert response.status_code == 200
    org.refresh_from_db()
    assert org.slack_team_id is None
    assert not org.slack_bot_token


def test_workspace_mapping_cannot_be_claimed_from_another_org(org, rw_user):
    OrganizationMembership.objects.filter(user=rw_user, organization=org).update(is_admin=True)
    Organization.objects.create(name='Existing installation', slack_team_id='T123')
    client = Client()
    client.force_login(rw_user)
    response = client.post('/api/integrations/hooks/save/',
        {'name': 'Slack', 'slack_team_id': 'T123', 'slack_bot_token': 'test-token'}, content_type='application/json')
    assert response.status_code == 409
    org.refresh_from_db()
    assert not org.slack_bot_token
