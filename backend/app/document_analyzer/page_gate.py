"""Luccy's own chat page, served unchanged inside DataGov.

luccy ships its chat page (``serving/chat.html``: chats, history, sources, feedback) for use
behind a login gate. With ``LUCCY_DEV_UI=1`` its page routes take no key, and the gate names
the signed-in person in ``Remote-User``; luccy keeps each person's saved chats under that
name. DataGov is that gate: ``/api/document-analyzer/luccy/<path>`` checks the DataGov
session and Document Analyzer access, then relays the request to the analyzer with
``Remote-User`` set here and nothing else of the browser's identity (no cookies, no Origin).

The page calls its routes by absolute path (``/chats``, ``/ask/stream/<job>`` ...). One
script put first in its ``<head>`` prefixes those calls with this gate's path; nothing else
in the page changes. Only luccy's page routes are relayed, and a write must come from this
origin: the page sends no CSRF token, so the Origin check stands in for it.
"""
import logging
import re
from urllib.parse import urlsplit

import httpx
from asgiref.sync import sync_to_async
from django.conf import settings
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse, StreamingHttpResponse
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.csrf import csrf_exempt

from . import client, permissions

logger = logging.getLogger(__name__)

# luccy's page routes (serving/api/page_routes.py, chat_routes.py, app.py); "" is the page.
PAGE_ROUTE_RE = re.compile(
    r'|fonts/[A-Za-z0-9_.-]{1,80}\.woff2|chats|chats/import|chats/[A-Za-z0-9_-]{1,80}(?:/ask)?'
    r'|ask/stream(?:/[A-Za-z0-9_-]{1,64})?|cancel/[A-Za-z0-9_-]{1,64}|feedback|notes|page-errors'
    r'|documents/[A-Za-z0-9][A-Za-z0-9_.:@-]{0,299}|records/[0-9a-f]{8}')
ASK_RE = re.compile(r'chats/[A-Za-z0-9_-]{1,80}/ask')
METHODS = {'GET', 'POST', 'PATCH', 'DELETE'}
# luccy's login-name rule (serving/chats.py OWNER_RE): the name its chat store files under.
OWNER_RE = re.compile(r'[A-Za-z0-9_.@-]{1,64}')
FORWARDED = ('Content-Type', 'Accept', 'Last-Event-ID')
RELAYED = ('Content-Type', 'Cache-Control', 'Retry-After')
MAX_PAGE_BYTES = 2 * 1024 * 1024
# Prefixes the page's absolute calls ("/chats") with the gate's own path.
PREFIX_SCRIPT = (
    "<script>(()=>{const base=location.pathname.replace(/[^/]*$/,'');const send=window.fetch.bind(window);"
    "window.fetch=(u,o)=>send(typeof u==='string'&&u[0]==='/'&&u[1]!=='/'?base+u.slice(1):u,o);})();</script>")


def owner_of(user):
    """The name luccy files this person's chats under: their email, else their username,
    else a stable id (luccy refuses names outside OWNER_RE)."""
    for name in ((user.email or '').strip().lower(), getattr(user, 'username', '')):
        if OWNER_RE.fullmatch(name or ''):
            return name
    return f'datagov-{user.pk}'


def same_origin(request, write):
    """Not demonstrably cross-origin; a write must also name this origin."""
    site = request.headers.get('Sec-Fetch-Site', '').lower()
    if site and site not in ('same-origin', 'none'):
        return False
    origin = request.headers.get('Origin', '')
    if origin:
        return urlsplit(origin).netloc.lower() == request.get_host().lower()
    return not write


def with_prefix_script(html):
    head = re.search(rb'<head[^>]*>', html, re.IGNORECASE)
    at = head.end() if head else 0
    return html[:at] + PREFIX_SCRIPT.encode() + html[at:]


def _error(status, message):
    return JsonResponse({'error': message}, status=status)


async def _relay(http, upstream):
    try:
        # Decoded bytes: Content-Encoding is not relayed.
        async for chunk in upstream.aiter_bytes():
            yield chunk
    except httpx.HTTPError:
        logger.warning('Luccy page stream ended early.')
    finally:
        await upstream.aclose()
        await http.aclose()


@csrf_exempt
@xframe_options_sameorigin
async def gate(request, path=''):
    user = await request.auser()
    if not user.is_authenticated:
        if path == '' and request.method == 'GET':
            return HttpResponseRedirect('/login?next=/document-analyzer')
        return _error(401, 'Sign in to DataGov again.')
    _org, status, reason = await sync_to_async(permissions.evaluate)(user)
    if status != 200:
        return _error(status, 'not found' if status == 404 else reason)
    if request.method not in METHODS or not PAGE_ROUTE_RE.fullmatch(path):
        return _error(404, 'not found')
    write = request.method != 'GET'
    if not same_origin(request, write):
        return _error(403, 'unauthorized')
    asking = request.method == 'POST' and ASK_RE.fullmatch(path)
    if asking and not await sync_to_async(permissions.take_ask)(user):
        return _error(429, permissions.RATE_LIMITED)

    headers = {'Remote-User': owner_of(user), 'Accept-Encoding': 'identity'}
    headers.update({name: request.headers[name] for name in FORWARDED if name in request.headers})
    query = request.META.get('QUERY_STRING', '')
    http = client.async_client(settings.LUCCY_STREAM_TIMEOUT)
    try:
        upstream = await http.send(http.build_request(
            request.method, client.url('/' + path) + (f'?{query}' if query else ''),
            headers=headers, content=request.body if write else None), stream=True)
    except httpx.HTTPError as exc:
        await http.aclose()
        client.log_failure(request.method, '/' + path, type(exc).__name__)
        if asking:
            await sync_to_async(permissions.return_ask)(user)
        return _error(502, 'The Document Analyzer could not be reached. Please try again.')
    if asking and upstream.status_code != 200:
        await sync_to_async(permissions.return_ask)(user)

    kind = upstream.headers.get('content-type', '')
    if path == '' and upstream.status_code == 200 and kind.startswith('text/html'):
        try:
            page = await client.aread_bounded(upstream, MAX_PAGE_BYTES)
        except (client.UpstreamError, httpx.HTTPError):
            return _error(502, 'The Document Analyzer page could not be loaded. Please try again.')
        finally:
            await upstream.aclose()
            await http.aclose()
        response = HttpResponse(with_prefix_script(page), content_type=kind)
        response['Cache-Control'] = 'no-store'
        return response

    response = StreamingHttpResponse(_relay(http, upstream), status=upstream.status_code)
    for name in RELAYED:
        if name in upstream.headers:
            response[name] = upstream.headers[name]
    if 'text/event-stream' in kind:
        response['X-Accel-Buffering'] = 'no'
    return response
