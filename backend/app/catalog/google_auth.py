"""Google OpenID Connect sign-in and automatic Company-tier registration."""
import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpResponseRedirect, JsonResponse
from django.utils.crypto import constant_time_compare
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2 import id_token

from .access import COMPANY
from .models import CustomUser, Organization, OrganizationMembership

_SESSION_KEY = 'google_oauth'
_LINK_SESSION_KEY = 'google_pending_link'
_FLOW_TTL = 600


class GoogleSignInError(Exception):
    """A public error code that the login page can render safely."""


class GoogleLinkRequired(GoogleSignInError):
    """Google verified identity, but the existing account needs confirmation."""

    def __init__(self, user_id):
        super().__init__('link_required')
        self.user_id = user_id


def _configured():
    return bool(settings.GOOGLE_OAUTH_CLIENT_ID and settings.GOOGLE_OAUTH_CLIENT_SECRET
                and settings.GOOGLE_OAUTH_REDIRECT_URI)


def _safe_next(value):
    if (isinstance(value, str) and value.startswith('/')
            and not value.startswith('//') and '\\' not in value
            and not any(ord(c) < 32 or ord(c) == 127 for c in value)
            and url_has_allowed_host_and_scheme(value, allowed_hosts=set())):
        return value
    return '/dashboard'


def _redirect(path):
    response = HttpResponseRedirect(path)
    response['Referrer-Policy'] = 'no-referrer'
    return response


def _error(code, next_path='/dashboard'):
    return _redirect('/login?' + urlencode({'google_error': code, 'next': next_path}))


def _pending_link(request):
    pending = request.session.get(_LINK_SESSION_KEY)
    if pending and 0 <= time.time() - pending['started_at'] <= _FLOW_TTL:
        return pending
    request.session.pop(_LINK_SESSION_KEY, None)
    return None


@never_cache
@ensure_csrf_cookie
@require_GET
def google_config_view(request):
    # Credentials and Google tokens never need to reach the browser.
    enabled = _configured()
    pending = _pending_link(request) if enabled else None
    return JsonResponse({
        'enabled': enabled,
        'pending_link': {'email': pending['email']} if pending else None,
    })


@never_cache
@require_POST
def google_start_view(request):
    pending = request.session.pop(_LINK_SESSION_KEY, None)
    if not _configured():
        if pending:
            # SessionMiddleware skips saving sessions on 5xx responses. Persist
            # revocation in the database so re-enabling Google cannot revive it.
            request.session.save()
        return JsonResponse({'error': 'Google sign-in is not configured.'}, status=503)
    try:
        data = json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Invalid JSON body.'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'error': 'Invalid JSON body.'}, status=400)

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    request.session[_SESSION_KEY] = {
        'state': state, 'nonce': nonce, 'verifier': verifier,
        'started_at': time.time(), 'next': _safe_next(data.get('next')),
        'redirect_uri': settings.GOOGLE_OAUTH_REDIRECT_URI,
    }
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode({
        'client_id': settings.GOOGLE_OAUTH_CLIENT_ID,
        'redirect_uri': settings.GOOGLE_OAUTH_REDIRECT_URI,
        'response_type': 'code', 'scope': 'openid email profile',
        'state': state, 'nonce': nonce, 'prompt': 'select_account',
        'code_challenge': challenge, 'code_challenge_method': 'S256',
    })
    return JsonResponse({'url': url})


class _GoogleRequest(Request):
    def __call__(self, *args, **kwargs):
        kwargs['timeout'] = 10
        return super().__call__(*args, **kwargs)


def _verified_claims(code, flow):
    # No refresh tokens or API permissions: this exchange is only for identity.
    with requests.Session() as http:
        response = http.post('https://oauth2.googleapis.com/token', data={
            'code': code,
            'client_id': settings.GOOGLE_OAUTH_CLIENT_ID,
            'client_secret': settings.GOOGLE_OAUTH_CLIENT_SECRET,
            'redirect_uri': flow['redirect_uri'],
            'grant_type': 'authorization_code',
            'code_verifier': flow['verifier'],
        }, timeout=10)
        response.raise_for_status()
        token = response.json().get('id_token')
        if not isinstance(token, str) or not token:
            raise GoogleSignInError('failed')
        # google-auth verifies signature, audience, expiration and Google issuer.
        claims = id_token.verify_oauth2_token(
            token, _GoogleRequest(session=http), settings.GOOGLE_OAUTH_CLIENT_ID,
        )
    if (not isinstance(claims.get('nonce'), str)
            or not constant_time_compare(claims['nonce'], flow['nonce'])
            or claims.get('email_verified') is not True):
        raise GoogleSignInError('failed')
    subject, email = claims.get('sub'), claims.get('email')
    if (not isinstance(subject, str) or not subject or len(subject) > 255
            or not isinstance(email, str) or len(email) > 254):
        raise GoogleSignInError('failed')
    try:
        validate_email(email)
    except ValidationError:
        raise GoogleSignInError('failed') from None
    return claims


def _signup_org():
    configured_id = settings.GOOGLE_SIGNUP_ORGANIZATION_ID
    if configured_id:
        try:
            org = Organization.objects.filter(pk=int(configured_id)).first()
        except (TypeError, ValueError, OverflowError):
            org = None
    else:
        # Never guess the tenant when a deployment contains several orgs.
        orgs = list(Organization.objects.order_by('pk')[:2])
        org = orgs[0] if len(orgs) == 1 else None
    if org is None:
        raise GoogleSignInError('unavailable')
    return org


def _username(email):
    base = email.split('@', 1)[0]
    limit = CustomUser._meta.get_field('username').max_length
    candidate = base[:limit]
    suffix = 1
    while CustomUser.objects.filter(username__iexact=candidate).exists():
        tail = f'_{suffix}'
        candidate = base[:limit - len(tail)] + tail
        suffix += 1
    return candidate


def _google_user(claims):
    subject = claims['sub']
    email = claims['email'].strip().lower()
    # Retry an actual uniqueness race without leaving a user lacking membership.
    for attempt in range(5):
        try:
            with transaction.atomic():
                matches = list(CustomUser.objects.select_for_update().filter(
                    Q(google_subject=subject) | Q(email__iexact=email),
                ))
                linked = next((u for u in matches if u.google_subject == subject), None)
                if linked is not None:
                    if not linked.is_active:
                        raise GoogleSignInError('inactive')
                    return linked
                if matches:
                    if len(matches) != 1 or matches[0].google_subject:
                        raise GoogleSignInError('account_conflict')
                    user = matches[0]
                    if not user.is_active:
                        raise GoogleSignInError('inactive')
                    # Gmail/Workspace ownership is current. Other Google-account
                    # email verification can be stale, so confirm the existing
                    # account password once before adding the second method.
                    authoritative = email.endswith('@gmail.com') or bool(claims.get('hd'))
                    if not authoritative:
                        if not user.has_usable_password():
                            raise GoogleSignInError('account_conflict')
                        raise GoogleLinkRequired(user.pk)
                    user.google_subject = subject
                    user.save(update_fields=['google_subject'])
                    return user

                org = _signup_org()
                user = CustomUser.objects.create_user(
                    username=_username(email), email=email, password=None,
                    google_subject=subject,
                    first_name=str(claims.get('given_name') or '')[:150],
                    last_name=str(claims.get('family_name') or '')[:150],
                    is_staff=False, is_superuser=False,
                )
                OrganizationMembership.objects.create(user=user, organization=org, is_admin=False)
                group, _ = Group.objects.get_or_create(name=COMPANY)
                user.groups.add(group)
                return user
        except IntegrityError:
            if attempt == 4:
                raise GoogleSignInError('account_conflict') from None


@never_cache
@require_GET
def google_callback_view(request):
    # Consume state even on failure; a callback is single-use and session-bound.
    flow = request.session.pop(_SESSION_KEY, None)
    request.session.pop(_LINK_SESSION_KEY, None)
    if not flow:
        return _error('expired')
    next_path = _safe_next(flow.get('next'))
    if (not constant_time_compare(request.GET.get('state', ''), flow['state'])
            or not 0 <= time.time() - flow['started_at'] <= _FLOW_TTL):
        return _error('expired', next_path)
    if not _configured():
        return _error('unavailable', next_path)
    if request.GET.get('error'):
        code = 'cancelled' if request.GET['error'] == 'access_denied' else 'failed'
        return _error(code, next_path)
    code = request.GET.get('code')
    if not code:
        return _error('failed', next_path)
    try:
        claims = _verified_claims(code, flow)
        user = _google_user(claims)
    except GoogleLinkRequired as exc:
        request.session[_LINK_SESSION_KEY] = {
            'subject': claims['sub'], 'email': claims['email'].strip().lower(),
            'user_id': exc.user_id, 'started_at': time.time(),
            'next': next_path,
        }
        return _redirect('/login?' + urlencode({'google_link': '1', 'next': next_path}))
    except GoogleSignInError as exc:
        return _error(str(exc), next_path)
    except (requests.RequestException, GoogleAuthError, ValueError, TypeError, KeyError, AttributeError):
        # Provider errors can contain codes/tokens: never reflect them to the UI.
        return _error('failed', next_path)
    login(request, user, backend='catalog.backends.EmailOrUsernameModelBackend')
    return _redirect(next_path)


_LINK_ERRORS = {
    'expired': 'Google account linking expired. Please continue with Google again.',
    'unavailable': 'Google sign-in is currently unavailable. Please try again later.',
    'invalid_password': 'The password did not match your existing account. Please try again.',
    'inactive': 'Your account is inactive. Please contact an administrator.',
    'account_conflict': 'This Google account could not be linked. Please sign in with your existing account or contact an administrator.',
}


def _link_error(request, code, status=400, clear=True):
    if clear:
        pending = request.session.pop(_LINK_SESSION_KEY, None)
        if pending and status >= 500:
            # The database-backed session must retain revocation on 5xx too.
            request.session.save()
    return JsonResponse({'error': _LINK_ERRORS[code], 'code': code}, status=status)


@never_cache
@require_POST
def google_link_view(request):
    """Confirm an existing password once, then retain both login methods.

    The target account and Google identity come only from a previously verified,
    session-bound callback. The browser supplies the existing password only.
    """
    if not _configured():
        return _link_error(request, 'unavailable', status=503)
    pending = _pending_link(request)
    if pending is None:
        return _link_error(request, 'expired')
    try:
        data = json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Invalid JSON body.'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'error': 'Invalid JSON body.'}, status=400)
    password = data.get('password')
    if not isinstance(password, str) or not password:
        return JsonResponse({'error': 'Your existing account password is required.'}, status=400)

    try:
        with transaction.atomic():
            user = CustomUser.objects.select_for_update().filter(
                pk=pending['user_id'], email__iexact=pending['email'],
            ).first()
            if user is None:
                return _link_error(request, 'account_conflict', status=409)
            if not user.is_active:
                return _link_error(request, 'inactive', status=403)
            if ((user.google_subject and user.google_subject != pending['subject'])
                    or CustomUser.objects.exclude(pk=user.pk).filter(
                        Q(google_subject=pending['subject']) | Q(email__iexact=pending['email']),
                    ).exists()):
                return _link_error(request, 'account_conflict', status=409)
            if not user.check_password(password):
                return _link_error(request, 'invalid_password', clear=False)
            user.google_subject = pending['subject']
            user.save(update_fields=['google_subject'])
    except IntegrityError:
        return _link_error(request, 'account_conflict', status=409)

    request.session.pop(_LINK_SESSION_KEY, None)
    login(request, user, backend='catalog.backends.EmailOrUsernameModelBackend')
    return JsonResponse({'url': _safe_next(pending['next'])})
