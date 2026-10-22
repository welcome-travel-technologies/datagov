"""Who may use the Document Analyzer, DataGov's door to luccy (the original document engine).

luccy holds ONE company's corpus and knows nothing of DataGov organizations, so the
binding lives here: only active members of ``LUCCY_ORGANIZATION_ID`` who are that
organization's admins or in the Analytics group may ask. An empty organization id means nobody, and
superusers are not exempt from the binding or the access rule.

luccy has no per-document access, so it only ever holds the organization's collections
that every member may read (``internal.exported_documents``): what it holds, every user
of it may read.

The three service tokens are each a different door. ``LUCCY_API_KEY`` (the page and the
assistant tool) must be at least 32 characters, or the analyzer counts as not set up.
``LUCCY_SYNC_TOKEN`` (the document list) and ``LUCCY_EVAL_TOKEN`` (the comparison) must
also differ from the other two: a weak or shared one closes only its own endpoints (404),
never the page or the tool. An empty token closes its own door too. ``token_problem``
logs why, once per process.
"""
import hmac
import logging
import time
from urllib.parse import urlparse

from django.conf import settings
from django.core.cache import cache

from catalog.access import ANALYTICS
from catalog.models import OrganizationMembership

logger = logging.getLogger(__name__)

SOURCES = ['confluence', 'jira', 'drive', 'github']
MIN_TOKEN_CHARS = 32
TOKEN_SETTINGS = ('LUCCY_API_KEY', 'LUCCY_SYNC_TOKEN', 'LUCCY_EVAL_TOKEN')
# What a token that cannot be trusted closes.
TOKEN_DOORS = {'LUCCY_API_KEY': 'every analyzer endpoint', 'LUCCY_SYNC_TOKEN': 'the sync endpoints',
               'LUCCY_EVAL_TOKEN': 'the comparison endpoints'}
_reported_token_problems = set()
ASK_WINDOW_SECONDS = 3600
RATE_LIMITED = 'You have reached the hourly limit of Document Analyzer questions. Please try again later.'


def enabled():
    return bool(getattr(settings, 'LUCCY_ENABLED', False))


def organization_id():
    """The one organization whose members may ask; None when unset or malformed."""
    value = str(getattr(settings, 'LUCCY_ORGANIZATION_ID', '') or '').strip()
    return int(value) if value.isascii() and value.isdigit() else None


def base_url():
    url = str(getattr(settings, 'LUCCY_API_URL', '') or '').strip().rstrip('/')
    parsed = urlparse(url)
    return url if parsed.scheme in ('http', 'https') and parsed.netloc else ''


def _token(name):
    return str(getattr(settings, name, '') or '')


def token_problem(name):
    """Why the service token ``name`` cannot be trusted, in plain words, else ``""``
    (also for an unset one, which simply leaves its door closed).

    A set token must be at least ``MIN_TOKEN_CHARS`` characters; the sync and eval
    tokens must also differ from the other two. Each distinct problem is logged once per
    process (never a token)."""
    value = _token(name)
    if not value:
        return ''
    if len(value) < MIN_TOKEN_CHARS:
        problem = f'{name} must be at least {MIN_TOKEN_CHARS} characters'
    elif name == 'LUCCY_API_KEY':
        problem = ''  # A sync or eval token equal to it closes their own doors instead.
    else:
        same = [other for other in TOKEN_SETTINGS if other != name and _token(other)
                and hmac.compare_digest(value.encode('utf-8'), _token(other).encode('utf-8'))]
        problem = f'{name} must differ from {" and ".join(same)}' if same else ''
    if problem and problem not in _reported_token_problems:
        _reported_token_problems.add(problem)
        logger.error('Document Analyzer: %s. %s answers 404 until it is fixed.', problem,
                     TOKEN_DOORS[name][0].upper() + TOKEN_DOORS[name][1:])
    return problem


def service_token(name):
    """The value of service token ``name`` when it is set and can be trusted, else ``""``."""
    return '' if token_problem(name) else _token(name)


def configured():
    return bool(base_url() and service_token('LUCCY_API_KEY') and organization_id() is not None)


def org_available(org):
    """The feature is on, set up, and bound to this organization."""
    return bool(enabled() and configured() and org is not None and org.pk == organization_id())


def evaluate(user):
    """``(organization, status, reason)`` for this user.

    200 with an empty reason when they may use the analyzer; 404 when it is off, not
    set up, or bound to another organization; 403 when they are a member without
    access. ``reason`` is the plain-words text the page shows.
    """
    if not enabled():
        return None, 404, 'The Document Analyzer is turned off.'
    if not configured():
        return None, 404, 'The Document Analyzer is not set up yet. Ask an administrator to connect it.'
    if not getattr(user, 'is_authenticated', False) or not user.is_active:
        return None, 404, 'Sign in to use the Document Analyzer.'
    # Resolved explicitly in the bound organization, never "the first membership".
    membership = OrganizationMembership.objects.filter(
        organization_id=organization_id(), user=user, user__is_active=True,
    ).select_related('organization').first()
    if membership is None:
        return None, 404, "The Document Analyzer only answers for the organization whose documents it holds."
    if not (membership.is_admin or user.groups.filter(name=ANALYTICS).exists()):
        return (membership.organization, 403,
                'You need Analytics access to use the Document Analyzer. Ask an organization administrator.')
    return membership.organization, 200, ''


def can_use(user):
    return evaluate(user)[1] == 200


def _ask_limit():
    return int(getattr(settings, 'LUCCY_MAX_ASKS_PER_HOUR', 60))


def _ask_key(user):
    return f'document_analyzer:asks:{user.pk}:{int(time.time() // ASK_WINDOW_SECONDS)}'


def take_ask(user):
    """Count one question against the user's hourly cap, shared by the page and the
    assistant tool. False once the cap is reached; a cap of 0 or less turns it off.

    The key names the clock hour, and ``touch`` restores the TTL because
    ``DatabaseCache.incr`` rewrites the entry with the default timeout.
    """
    limit = _ask_limit()
    if limit <= 0:
        return True
    key = _ask_key(user)
    if cache.add(key, 1, timeout=ASK_WINDOW_SECONDS):
        return True
    try:
        count = cache.incr(key)
    except ValueError:  # Expired between add and incr.
        cache.add(key, 1, timeout=ASK_WINDOW_SECONDS)
        return True
    cache.touch(key, ASK_WINDOW_SECONDS)
    return count <= limit


def return_ask(user):
    """Give back a question luccy never started on (it refused, was busy, or could not
    be reached), so a failed try does not use up the hour."""
    if _ask_limit() <= 0:
        return
    key = _ask_key(user)
    try:
        cache.decr(key)
    except ValueError:  # Its hour has ended.
        return
    cache.touch(key, ASK_WINDOW_SECONDS)
