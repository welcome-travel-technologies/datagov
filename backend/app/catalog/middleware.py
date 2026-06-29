from django.utils.deprecation import MiddlewareMixin

from .models import UserActivityLog


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
