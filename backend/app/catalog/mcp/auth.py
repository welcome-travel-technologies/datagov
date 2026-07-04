"""Bearer-token authentication for the MCP endpoint (Stage A of the plan).

Keys are ``McpApiKey`` rows (user + org + scopes) minted with
``python manage.py create_mcp_key``. The raw token (``wdc_…``) is shown once
at mint time; only its SHA-256 hex digest is stored, so a DB leak does not
leak usable tokens and verification is a single indexed lookup by digest.

Scope names are deliberately OAuth-style strings so the M5 OAuth 2.1 upgrade
(RFC 9728/8414/8707 — see docs/mcp-server-plan.md) can map them 1:1 onto real
OAuth scopes without renaming anything. Failures return ``401`` with a
``WWW-Authenticate: Bearer`` header — the exact shape RFC 9728's
``resource_metadata`` pointer later slots into.
"""
import hashlib
import secrets

from django.http import JsonResponse
from django.utils import timezone

TOKEN_PREFIX = 'wdc_'

SCOPE_CATALOG_READ = 'catalog:read'
SCOPE_POWERBI_QUERY = 'powerbi:query'
SCOPE_BIGQUERY_QUERY = 'bigquery:query'
ALL_SCOPES = (SCOPE_CATALOG_READ, SCOPE_POWERBI_QUERY, SCOPE_BIGQUERY_QUERY)
DEFAULT_SCOPES = [SCOPE_CATALOG_READ]


def generate_token() -> str:
    """A fresh raw bearer token (~47 chars, 256 bits of entropy)."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def mint_key(*, user, organization, name: str, scopes=None):
    """Create an ``McpApiKey`` and return ``(key, raw_token)``.

    The raw token is returned ONLY here — the caller must show it once and
    forget it (it is not recoverable from the row).
    """
    from ..models import McpApiKey

    scopes = list(scopes) if scopes else list(DEFAULT_SCOPES)
    unknown = [s for s in scopes if s not in ALL_SCOPES]
    if unknown:
        raise ValueError(f'Unknown scope(s): {", ".join(unknown)}. '
                         f'Valid: {", ".join(ALL_SCOPES)}')
    raw = generate_token()
    key = McpApiKey.objects.create(
        organization=organization,
        user=user,
        name=name,
        key_prefix=raw[:12],
        key_hash=hash_token(raw),
        scopes=scopes,
    )
    return key, raw


def _unauthorized(detail: str) -> JsonResponse:
    resp = JsonResponse({'error': detail}, status=401)
    # Bearer challenge per RFC 6750; M5 appends resource_metadata (RFC 9728).
    resp['WWW-Authenticate'] = 'Bearer error="invalid_token"'
    return resp


def authenticate_request(request):
    """Resolve the request's bearer token to an active ``McpApiKey``.

    Returns ``(key, None)`` on success or ``(None, JsonResponse-401)`` on any
    failure. ``last_used_at`` is stamped on every successful auth (a single
    UPDATE — cheap at MCP call rates).
    """
    from ..models import McpApiKey

    header = request.headers.get('Authorization') or ''
    if not header.startswith('Bearer '):
        return None, _unauthorized('Missing Bearer token.')
    raw = header[len('Bearer '):].strip()
    if not raw:
        return None, _unauthorized('Missing Bearer token.')

    key = (
        McpApiKey.objects.filter(key_hash=hash_token(raw), is_active=True)
        .select_related('organization', 'user')
        .first()
    )
    if key is None:
        return None, _unauthorized('Invalid or revoked token.')

    McpApiKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now())
    return key, None
