"""Document Analyzer endpoints: a thin backend-for-frontend over luccy's bearer API.

The two answer streams are plain async Django views: DRF has no async views, and a
``StreamingHttpResponse`` over a sync iterator is fully buffered under ASGI. CSRF is
still enforced on their POST by ``CsrfViewMiddleware``. The rest are DRF views. The
browser never sees the service key, and luccy's status is checked before the first
byte is relayed so failures arrive as JSON ``{"detail": ...}`` rather than a broken
event stream. A question that fails before its stream starts does not count against
the hourly cap, and only the person who started an answer may re-attach to it.

luccy keeps a thread's memory by conversation id alone, so DataGov never forwards an id
it did not make: the page's id is namespaced per user (``page_conversation``), the web
chat's is derived from its session (``chat_conversation``), and both are keyed with
``SECRET_KEY`` so nobody can name another person's thread. Client ids starting with
"dg" are refused, so they can never collide with DataGov's own.
"""
import hashlib
import hmac
import json
import logging
import re
from urllib.parse import quote, urlparse

import httpx
from asgiref.sync import sync_to_async
from django.conf import settings
from django.http import HttpResponseNotAllowed, JsonResponse, StreamingHttpResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import client, permissions

logger = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 10000
MAX_HISTORY_TURNS = 50
MAX_TURN_CHARS = 16000
MAX_HISTORY_CHARS = 64000
MAX_NOTE_CHARS = 500
REQUEST_TIMEOUT = 30
CONVERSATION_RE = re.compile(r'[A-Za-z0-9_-]{1,64}')
# DataGov's own conversation ids: "dg<kind>-" and 40 hex characters (at most 47).
RESERVED_CONVERSATION_PREFIX = 'dg'
CONVERSATION_DIGEST_CHARS = 40
JOB_RE = re.compile(r'[A-Za-z0-9_-]{1,64}')
# luccy's own rule (a leading letter or digit, never ".."), so no id is a dot segment.
DOC_ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:@-]{0,299}')
ANSWER_ID_RE = re.compile(r'[0-9a-f]{8}')
LAST_EVENT_ID_RE = re.compile(r'[0-9]{1,12}')
STREAM_HEADERS = {'Accept': 'text/event-stream', 'Accept-Encoding': 'identity'}
# The first event is ``stage`` with the job id to re-attach with.
STREAM_JOB_RE = re.compile(rb'"job":\s*"([A-Za-z0-9_-]{1,64})"')
STREAM_HEAD_BYTES = 4096


def conversation_id(kind, *parts):
    """``dg<kind>-`` + the first 40 hex of HMAC-SHA256(SECRET_KEY, "<kind>:<parts>")."""
    message = ':'.join([kind, *(str(part) for part in parts)]).encode('utf-8')
    digest = hmac.new(settings.SECRET_KEY.encode('utf-8'), message, hashlib.sha256).hexdigest()
    return f'{RESERVED_CONVERSATION_PREFIX}{kind}-{digest[:CONVERSATION_DIGEST_CHARS]}'


def chat_conversation(session):
    """The web chat session's luccy conversation id."""
    return conversation_id('chat', session.pk)


def page_conversation(user, client_id):
    """The page's luccy conversation id for ``user``'s own thread ``client_id``."""
    return conversation_id('page', user.pk, client_id)


def client_conversation(value):
    """A client's conversation id, validated: None when absent; ValueError when malformed
    or in DataGov's reserved "dg" namespace."""
    if value in (None, ''):
        return None
    if not isinstance(value, str) or not CONVERSATION_RE.fullmatch(value):
        raise ValueError('Invalid conversation id.')
    if value[:len(RESERVED_CONVERSATION_PREFIX)].lower() == RESERVED_CONVERSATION_PREFIX:
        raise ValueError(f'Conversation ids starting with "{RESERVED_CONVERSATION_PREFIX}" are reserved.')
    return value


def ask_payload(data, user):
    """luccy's ``POST /ask/stream`` body, built only from validated fields. The client's
    conversation id is sent namespaced to ``user`` (``page_conversation``)."""
    if not isinstance(data, dict):
        raise ValueError('Send a JSON object.')
    question = data.get('question')
    if not isinstance(question, str) or not question.strip():
        raise ValueError('Ask a question.')
    if len(question) > MAX_QUESTION_CHARS:
        raise ValueError(f'Keep the question to at most {MAX_QUESTION_CHARS} characters.')
    payload = {'question': question}
    conversation = client_conversation(data.get('conversation'))
    if conversation is not None:
        payload['conversation'] = page_conversation(user, conversation)
    sources = data.get('sources')
    if sources is not None:
        if not isinstance(sources, list) or not sources or any(s not in permissions.SOURCES for s in sources):
            raise ValueError(f'Choose one or more sources from: {", ".join(permissions.SOURCES)}.')
        payload['sources'] = sources
    style = data.get('style')
    if style is not None:
        if style not in ('', 'plain'):
            raise ValueError('The answer style must be "" or "plain".')
        payload['style'] = style
    history = data.get('history')
    if history is not None:
        if not isinstance(history, list):
            raise ValueError('Send the earlier turns as a list.')
        for turn in history:
            if (not isinstance(turn, dict) or turn.get('who') not in ('you', 'luccy')
                    or not isinstance(turn.get('text'), str)):
                raise ValueError('Each earlier turn needs "who" ("you" or "luccy") and "text".')
        payload['history'] = bounded_history(history)
    return payload


def bounded_history(turns, cap=MAX_HISTORY_TURNS):
    """Earlier turns ``[{"who", "text"}]`` held to luccy's request bounds: each cut to
    ``MAX_TURN_CHARS``, the last ``cap`` kept, then the oldest dropped until they hold at
    most ``MAX_HISTORY_CHARS`` in all."""
    out = [{'who': turn['who'], 'text': turn['text'][:MAX_TURN_CHARS]} for turn in turns]
    out = out[-cap:] if cap > 0 else []
    while out and sum(len(turn['text']) for turn in out) > MAX_HISTORY_CHARS:
        out.pop(0)
    return out


def _detail(status, detail, retry_after=None):
    response = JsonResponse({'detail': detail}, status=status)
    if retry_after:
        response['Retry-After'] = retry_after
    return response


def _upstream_response(err):
    response = Response({'detail': err.detail}, status=err.status)
    if err.retry_after:
        response['Retry-After'] = err.retry_after
    return response


async def _authorize(request):
    """``(user, None)`` when this user may ask, else ``(None, JSON error response)``."""
    user = await request.auser()
    if not user.is_authenticated:
        return None, _detail(401, 'Authentication credentials were not provided.')
    _org, status, reason = await sync_to_async(permissions.evaluate)(user)
    if status != 200:
        return None, _detail(status, 'Not found.' if status == 404 else reason)
    return user, None


async def _remember_job(head, owner):
    """Record the job id found in the stream's first bytes as ``owner``'s; True once
    the scan is over (found, or the head is past the first event)."""
    found = STREAM_JOB_RE.search(head)
    if found:
        try:
            await sync_to_async(permissions.remember_job)(found.group(1).decode(), owner)
        except Exception:  # The answer still streams; only a re-attach is lost.
            logger.warning('Document Analyzer could not record a job owner.')
    return bool(found) or len(head) > STREAM_HEAD_BYTES


async def _relay(http, upstream, owner=None):
    """luccy's event-stream bytes, unchanged. A browser that leaves closes the upstream
    stream too; luccy keeps the job running for a later re-attach by ``owner``."""
    head = b'' if owner is not None else None
    try:
        async for chunk in upstream.aiter_raw():
            if head is not None:
                # Recorded before the job id reaches the browser, which may re-attach at once.
                head += chunk
                if await _remember_job(head, owner):
                    head = None
            yield chunk
    except httpx.HTTPError:
        logger.warning('Document Analyzer stream ended early.')
    finally:
        await upstream.aclose()
        await http.aclose()


async def _stream(method, path, user, *, not_found=False, extra_headers=None, owner=None, **kwargs):
    http = client.async_client(settings.LUCCY_STREAM_TIMEOUT)
    try:
        upstream = await http.send(http.build_request(
            method, client.url(path), headers=client.headers(user, {**STREAM_HEADERS, **(extra_headers or {})}),
            **kwargs), stream=True)
    except httpx.HTTPError as exc:
        await http.aclose()
        client.log_failure(method, path, type(exc).__name__)
        err = client.transport_error(exc)
        return _detail(err.status, err.detail)
    if upstream.status_code != 200 or 'text/event-stream' not in upstream.headers.get('content-type', ''):
        client.log_failure(method, path, f'HTTP {upstream.status_code}' if upstream.status_code != 200
                           else 'reply is not an event stream')
        try:
            raw = await client.aread_bounded(upstream, client.SMALL_REPLY_BYTES)
        except (client.UpstreamError, httpx.HTTPError):
            raw = b''
        finally:
            await upstream.aclose()
            await http.aclose()
        status = upstream.status_code if upstream.status_code != 200 else 502
        err = client.upstream_error(status, client.parse(raw), not_found, client.retry_after(upstream))
        return _detail(err.status, err.detail, err.retry_after)
    response = StreamingHttpResponse(_relay(http, upstream, owner), content_type='text/event-stream; charset=utf-8')
    response['Cache-Control'] = 'no-cache, no-transform'
    response['X-Accel-Buffering'] = 'no'
    return response


async def ask_stream(request):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    user, denied = await _authorize(request)
    if denied:
        return denied
    try:
        data = json.loads(request.body or b'null')
    except (ValueError, UnicodeDecodeError):
        return _detail(400, 'Send a JSON object.')
    try:
        payload = ask_payload(data, user)
    except ValueError as exc:
        return _detail(400, str(exc))
    if not await sync_to_async(permissions.take_ask)(user):
        return _detail(429, permissions.RATE_LIMITED)
    response = await _stream('POST', '/ask/stream', user, owner=user, json=payload)
    if response.status_code != 200:
        # luccy refuses before it starts on a question, so a failed try does not count.
        await sync_to_async(permissions.return_ask)(user)
    return response


async def ask_stream_attach(request, job):
    if request.method != 'GET':
        return HttpResponseNotAllowed(['GET'])
    user, denied = await _authorize(request)
    if denied:
        return denied
    # luccy re-attaches any bearer caller, so the owner check lives here.
    if not JOB_RE.fullmatch(job) or not await sync_to_async(permissions.owns_job)(user, job):
        return _detail(404, 'Not found.')
    last = request.headers.get('Last-Event-ID', '')
    extra = {'Last-Event-ID': last} if LAST_EVENT_ID_RE.fullmatch(last) else None
    return await _stream('GET', f'/ask/stream/{job}', user, not_found=True, extra_headers=extra)


def _require_access(user):
    _org, status, reason = permissions.evaluate(user)
    if status == 404:
        raise NotFound()
    if status != 200:
        raise PermissionDenied(reason)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def status(request):
    org, code, reason = permissions.evaluate(request.user)
    available = code == 200
    tool_available = False
    if available:
        from catalog.services.assistant_tools import member_policy
        tool_available = bool(member_policy(org, request.user)['analyzer'])
    return Response({
        'enabled': permissions.enabled(),
        'configured': permissions.configured(),
        'available': available,
        # Probed only for people who may use it; null otherwise.
        'healthy': client.healthy() if available else None,
        'reason': reason,
        'sources': list(permissions.SOURCES),
        'tool_available': tool_available,
    })


def _source_link(value):
    return value if isinstance(value, str) and urlparse(value).scheme in ('http', 'https') else None


def _document_view(body):
    """luccy's DocumentView, keeping only its known fields."""
    path = body.get('path') if isinstance(body.get('path'), list) else []
    passages = body.get('passages') if isinstance(body.get('passages'), list) else []
    return {
        'id': str(body.get('id') or ''), 'title': str(body.get('title') or ''),
        'source': str(body.get('source') or ''), 'modified': str(body.get('modified') or ''),
        'url': _source_link(body.get('url')),
        'path': [p for p in path if isinstance(p, str)],
        'body_html': str(body.get('body_html') or ''),
        'passages': [{'heading': str(p.get('heading') or ''), 'text': str(p.get('text') or '')}
                     for p in passages if isinstance(p, dict)],
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def document(request, doc_id):
    _require_access(request.user)
    if not DOC_ID_RE.fullmatch(doc_id) or '..' in doc_id:
        return Response({'detail': 'Invalid document id.'}, status=400)
    answer = request.query_params.get('answer', '')
    if answer and not ANSWER_ID_RE.fullmatch(answer):
        return Response({'detail': 'Invalid answer reference.'}, status=400)
    try:
        body = client.request_json(
            'GET', f'/documents/{quote(doc_id, safe="")}', request.user, read_timeout=REQUEST_TIMEOUT,
            limit=client.LARGE_REPLY_BYTES, not_found=True, params={'answer': answer} if answer else None)
    except client.UpstreamError as err:
        return _upstream_response(err)
    return Response(_document_view(body))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def feedback(request):
    _require_access(request.user)
    data = request.data if isinstance(request.data, dict) else {}
    answer_id, verdict, note = data.get('answer_id'), data.get('verdict'), data.get('note')
    if not isinstance(answer_id, str) or not ANSWER_ID_RE.fullmatch(answer_id):
        return Response({'detail': 'Invalid answer reference.'}, status=400)
    if verdict not in ('right', 'wrong', 'cleared'):
        return Response({'detail': 'The verdict must be right, wrong or cleared.'}, status=400)
    if note is not None and not isinstance(note, str):
        return Response({'detail': 'The note must be text.'}, status=400)
    # Cut, not refused, like luccy itself.
    note = '' if verdict == 'cleared' else (note or '').strip()[:MAX_NOTE_CHARS]
    try:
        client.request_json('POST', '/feedback', request.user, read_timeout=REQUEST_TIMEOUT, not_found=True,
                            json={'answer_id': answer_id, 'verdict': verdict, 'note': note})
    except client.UpstreamError as err:
        return _upstream_response(err)
    return Response({'ok': True})
