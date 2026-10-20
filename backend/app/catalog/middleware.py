import logging
import re

from django.http import JsonResponse
from django.utils.deprecation import MiddlewareMixin

from .models import UserActivityLog

logger = logging.getLogger(__name__)

# Machine endpoints only private callers reach, at http://web:8000 directly: the
# Document Explorer worker (/api/knowledge/internal/) and luccy's sync job and the
# comparison runner (/api/document-analyzer/internal/). Matched on the decoded path
# Django resolves, in any case and with repeated slashes, so no spelling slips past.
INTERNAL_PATH_RE = re.compile(r'^/+api/+(knowledge|document-analyzer)/+internal(?:/|$)', re.IGNORECASE)
# Headers a reverse proxy adds (nginx sets X-Forwarded-For and X-Forwarded-Proto on
# every request it passes on; Cloudflare adds X-Forwarded-For too). Read from the raw
# request, so SECURE_PROXY_SSL_HEADER, USE_X_FORWARDED_HOST or USE_X_FORWARDED_PORT
# never change the answer.
PROXY_HEADERS = ('HTTP_X_FORWARDED_FOR', 'HTTP_X_REAL_IP', 'HTTP_FORWARDED', 'HTTP_X_FORWARDED_HOST',
                 'HTTP_X_FORWARDED_PROTO')


# Path prefixes we never want to record as a "page view".
SKIP_PREFIXES = (
    '/static/',
    '/media/',
    '/admin/jsi18n/',
    '/__debug__/',
    '/api/',
    '/favicon.ico',
)


def _client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


class UserActivityMiddleware(MiddlewareMixin):
    """Logs one row per authenticated frontend page view (GET, HTML response)."""

    def process_response(self, request, response):
        try:
            user = getattr(request, 'user', None)
            if user is None or not user.is_authenticated:
                return response
            if request.method != 'GET':
                return response

            path = request.path or ''
            if any(path.startswith(p) for p in SKIP_PREFIXES):
                return response

            # Only record successful HTML page loads — skip redirects, partials, JSON, etc.
            if response.status_code >= 400:
                return response
            content_type = (response.get('Content-Type') or '').lower()
            if 'text/html' not in content_type:
                return response

            # Skip HTMX partial fragment requests (full-page boosts still hit here).
            if request.headers.get('HX-Request') == 'true' and request.headers.get('HX-Boosted') != 'true':
                return response

            UserActivityLog.objects.create(
                user=user,
                email=getattr(user, 'email', '') or '',
                event='pageview',
                path=path[:500],
                method=request.method,
                status_code=response.status_code,
                ip=_client_ip(request),
                user_agent=(request.META.get('HTTP_USER_AGENT', '') or '')[:500],
            )
        except Exception:
            # Never let activity logging break a real request.
            pass
        return response


class InternalEndpointGuardMiddleware(MiddlewareMixin):
    """Defense in depth behind nginx's ``return 404`` for the internal endpoints: a
    request for them that came through a proxy is answered 404 before anything else
    runs, whatever token it carries. Legitimate callers reach ``web:8000`` directly and
    send none of ``PROXY_HEADERS``. Keep this first in ``MIDDLEWARE``."""

    def process_request(self, request):
        found = INTERNAL_PATH_RE.match(request.path_info or '')
        if found is None or not any(header in request.META for header in PROXY_HEADERS):
            return None
        # The area only: the path may carry job, version or run ids.
        logger.warning('Refused a proxied request for the internal %s endpoints.', found.group(1).lower())
        return JsonResponse({'detail': 'Not found.'}, status=404)
