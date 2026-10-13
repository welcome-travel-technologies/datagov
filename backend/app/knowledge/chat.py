from urllib.parse import urlparse

import httpx
from django.conf import settings
from rest_framework.exceptions import PermissionDenied

from .permissions import can_read, filter_citations, visible_documents


def _history(org, user, session, question, allowed_versions):
    if session is None or session.user_id != user.pk or session.organization_id != org.pk or session.mode != 'documents':
        return []
    messages = list(session.messages.order_by('-created_at', '-pk')[:9])[::-1]
    if messages and messages[-1].role == 'user' and messages[-1].content == question:
        messages.pop()  # The worker has already persisted this current prompt.
    turns = []
    for message in messages:
        if message.role not in {'user', 'assistant'}:
            continue
        if message.role == 'assistant':
            if not message.citations or any(str(c.get('version_id')) not in allowed_versions for c in message.citations if isinstance(c, dict)) or len(filter_citations(org, user, message.citations)) != len(message.citations):
                # Drop context that depended on changed/revoked documents.
                turns = []
                continue
        turns.append({'role': message.role, 'content': message.content[:8000]})
    turns = turns[-8:]
    while sum(len(turn['content']) for turn in turns) > 16000:
        turns.pop(0)
    return turns


def answer_document_question(org, user, question, session=None):
    if not can_read(org, user):
        raise PermissionDenied('Document Explorer access is required.')
    question = str(question).strip()
    if not question or len(question) > 10000:
        raise ValueError('Use a question between 1 and 10000 characters.')
    versions = list(visible_documents(org, user).exclude(current_version=None).filter(current_version__status='ready').values_list('current_version_id', flat=True)[:5001])
    if not versions:
        return {'answer': 'No indexed documents are available to you yet. Add documents to Document Explorer or ask an administrator for access.', 'citations': [], 'abstained': True, 'gaps': ['No accessible indexed documents.']}
    if len(versions) > 5000:
        raise ValueError('This document scope exceeds the current 5000-document search limit. Ask an administrator to reduce the active sources.')
    base = settings.DOCUMENT_EXPLORER_API_URL.rstrip('/')
    allowed = {str(v) for v in versions}
    history = _history(org, user, session, question, allowed)
    if urlparse(base).scheme not in ['http', 'https'] or not settings.DOCUMENT_EXPLORER_API_KEY:
        raise RuntimeError('Document Explorer is not configured.')
    # Exactly one paid request. A timeout does not establish whether the engine
    # executed it, so the application must not blindly repeat this call.
    with httpx.Client(timeout=httpx.Timeout(settings.DOCUMENT_EXPLORER_ANSWER_TIMEOUT, connect=10), follow_redirects=False, trust_env=False) as client:
        response = client.post(f'{base}/v1/answer', headers={'Authorization': f'Bearer {settings.DOCUMENT_EXPLORER_API_KEY}'}, json={
            'organization_id': str(org.pk), 'allowed_versions': [str(v) for v in versions],
            'question': question,
            'history': history,
        })
        if response.status_code != 200:
            raise RuntimeError('The document service could not complete this answer. Please try again later.')
        if len(response.content) > 2_000_000:
            raise RuntimeError('The document service returned an oversized answer.')
        result = response.json()
    if not isinstance(result, dict):
        raise RuntimeError('The document service returned an invalid answer.')
    answer = result.get('answer')
    citations = result.get('citations', [])
    if not isinstance(answer, str) or len(answer) > 100000 or not isinstance(citations, list) or len(citations) > 100:
        raise RuntimeError('The document service returned an invalid answer.')
    # A result can be authorized at query time but revoked while the model runs.
    # Filter against current permissions and the exact versions sent to retrieval.
    if any(not isinstance(c, dict) or str(c.get('version_id')) not in allowed for c in citations):
        raise RuntimeError('The document service returned evidence outside this question scope.')
    validated = filter_citations(org, user, citations)
    if len(validated) != len(citations):
        return {'answer': 'Access to the supporting documents changed while this answer was being prepared. Please ask again.', 'citations': [], 'abstained': True, 'gaps': ['Document access changed.']}
    abstained = result.get('abstained') is True
    if not validated and not abstained:
        return {'answer': 'I could not find verified document evidence to answer that question.', 'citations': [], 'abstained': True, 'gaps': ['No verified evidence.']}
    gaps = result.get('gaps', [])
    return {'answer': answer, 'citations': validated, 'abstained': abstained,
            'gaps': [g[:500] for g in gaps[:20] if isinstance(g, str)] if isinstance(gaps, list) else []}
