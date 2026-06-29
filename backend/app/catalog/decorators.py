"""Shared view decorators.

``api_login_required`` is the API-only replacement for Django's
``@login_required``: it returns a 401 JSON response for unauthenticated
requests instead of redirecting to a login page. The React app owns the login
UI (``/login`` -> ``POST /api/auth/login/``); Django serves only the API, so
there is no server-rendered login page to redirect to.
"""
from functools import wraps

from django.http import JsonResponse


def api_login_required(view):
    @wraps(view)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"detail": "Authentication required."}, status=401)
        return view(request, *args, **kwargs)

    return _wrapped
