"""HTTP calls to luccy's bearer API.

Every call carries the service key and the DataGov user as ``Remote-User``, ignores
proxy settings from the environment, never follows redirects, and reads reply bodies
only up to a size bound. Failures become ``UpstreamError`` with the status and words
DataGov shows; upstream bodies are never echoed except luccy's reader-facing
``{"error": ...}`` text for 400/429/503. Each failure is logged with its method,
route and status, never a body, a document id or a job id.
"""
import json
import logging
import re

import httpx
from django.conf import settings
from django.core.cache import cache

from .permissions import base_url

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 10
SMALL_REPLY_BYTES = 64 * 1024
LARGE_REPLY_BYTES = 2 * 1024 * 1024
ERROR_TEXT_CHARS = 300
HEALTH_CACHE_KEY = 'document_analyzer:health:v3'
HEALTH_CACHE_SECONDS = 30
HEALTH_TIMEOUT = 1.5  # Per phase (connect, read), so a probe stays near 3 s.

DEFAULT_ERRORS = {
    400: 'The Document Analyzer could not read that request.',
    404: 'Not found.',
    429: 'The Document Analyzer has reached its usage limit for now. Please try again later.',
    502: 'The Document Analyzer sent an unexpected reply. Please try again.',
    503: 'The Document Analyzer is busy or unavailable. Please try again in a minute.',
    504: 'The Document Analyzer took too long to reply. Please try again.',
}


class UpstreamError(Exception):
    """A failed luccy call, already mapped to the status and words DataGov shows."""

    def __init__(self, status, detail=None, retry_after=None):
        super().__init__(status)
        self.status = status
        self.detail = detail or DEFAULT_ERRORS[status]
        self.retry_after = retry_after


def options(read_timeout, connect=CONNECT_TIMEOUT):
    return {'timeout': httpx.Timeout(read_timeout, connect=connect), 'follow_redirects': False, 'trust_env': False}


def sync_client(read_timeout, connect=CONNECT_TIMEOUT):
    return httpx.Client(**options(read_timeout, connect))


def async_client(read_timeout):
    return httpx.AsyncClient(**options(read_timeout))


def headers(user, extra=None):
    return {'Authorization': f'Bearer {settings.LUCCY_API_KEY}', 'Remote-User': f'datagov-{user.pk}', **(extra or {})}


def url(path):
    return f'{base_url()}{path}'


def parse(raw):
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def retry_after(response):
    value = response.headers.get('retry-after', '')
    return value if re.fullmatch(r'\d{1,5}', value) else None


def log_failure(method, path, what):
    """One warning per failed luccy call, so a wrong key or a luccy fault is visible."""
    route = re.sub(r'^/(documents|ask/stream|records)/.+', r'/\1/<id>', path)
    logger.warning('Document Analyzer call %s %s failed: %s.', method, route, what)


def upstream_error(status, body, not_found=False, retry=None):
    """Map a non-200 luccy status to the error DataGov returns."""
    if status in (400, 429, 503):
        message = body.get('error') if isinstance(body, dict) else None
        detail = message.strip()[:ERROR_TEXT_CHARS] if isinstance(message, str) and message.strip() else None
        return UpstreamError(status, detail, None if status == 400 else retry)
    if status == 404 and not_found:
        return UpstreamError(404)
    return UpstreamError(504 if status == 504 else 502)


def transport_error(exc):
    if isinstance(exc, httpx.TimeoutException):
        return UpstreamError(504)
    if isinstance(exc, httpx.ConnectError):
        return UpstreamError(503)
    return UpstreamError(502)


def _too_large(response, limit):
    declared = response.headers.get('content-length', '')
    return declared.isdigit() and int(declared) > limit


def read_bounded(response, limit):
    """The body of a streamed sync reply, refused once it passes ``limit`` bytes."""
    if _too_large(response, limit):
        raise UpstreamError(502)
    body = bytearray()
    for chunk in response.iter_bytes():
        body += chunk
        if len(body) > limit:
            raise UpstreamError(502)
    return bytes(body)


async def aread_bounded(response, limit):
    if _too_large(response, limit):
        raise UpstreamError(502)
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > limit:
            raise UpstreamError(502)
    return bytes(body)


def request_json(method, path, user, *, read_timeout, limit=SMALL_REPLY_BYTES, not_found=False, **kwargs):
    """One luccy call; the parsed JSON object of a 200 reply, else ``UpstreamError``."""
    try:
        with sync_client(read_timeout) as http:
            with http.stream(method, url(path), headers=headers(user), **kwargs) as response:
                status, retry = response.status_code, retry_after(response)
                raw = read_bounded(response, limit)
    except httpx.HTTPError as exc:
        log_failure(method, path, type(exc).__name__)
        raise transport_error(exc) from None
    except UpstreamError:
        log_failure(method, path, f'reply over {limit} bytes')
        raise
    body = parse(raw)
    if status != 200:
        log_failure(method, path, f'HTTP {status}')
        raise upstream_error(status, body, not_found, retry)
    if not isinstance(body, dict):
        log_failure(method, path, 'reply is not a JSON object')
        raise UpstreamError(502)
    return body


def healthy():
    """Whether luccy's ``GET /health`` (no key) answers 200, remembered for 30 s so page
    status polls stay cheap. Any failure, including a slow reply, is False."""
    cached = cache.get(HEALTH_CACHE_KEY)
    if isinstance(cached, bool):
        return cached
    ok = False
    try:
        with sync_client(HEALTH_TIMEOUT, connect=HEALTH_TIMEOUT) as http:
            with http.stream('GET', url('/health')) as response:
                ok = response.status_code == 200
    except httpx.HTTPError:
        ok = False
    cache.set(HEALTH_CACHE_KEY, ok, timeout=HEALTH_CACHE_SECONDS)
    return ok
