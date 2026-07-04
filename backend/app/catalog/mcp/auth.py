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
from dataclasses import dataclass, field

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


@dataclass(frozen=True)
class OAuthPrincipal:
    """A resolved OAuth access token, shaped like the parts of ``McpApiKey``
    that ``registry.build_tool_specs`` reads (``organization``/``user``/
    ``scopes``) so the registry needs no OAuth-specific branch.

    An OAuth token is user-scoped; the org is resolved from the user's
    membership (``access.resolve_org``) — single-tenant in practice.
    """
    user: object
    organization: object
    scopes: list = field(default_factory=list)


def _unauthorized(detail: str, resource_metadata: str = None) -> JsonResponse:
    resp = JsonResponse({'error': detail}, status=401)
    # Bearer challenge per RFC 6750 + RFC 9728 resource_metadata pointer so an
    # OAuth client (claude.ai) discovers the authorization server from a 401.
    challenge = 'Bearer error="invalid_token"'
    if resource_metadata:
        challenge += f', resource_metadata="{resource_metadata}"'
    resp['WWW-Authenticate'] = challenge
    return resp


def authenticate_request(request):
    """Resolve the request's bearer token to a principal.

    Two token families, distinguished by prefix:
    - ``wdc_…`` → a static ``McpApiKey`` (stamps ``last_used_at``).
    - anything else → an OAuth 2.1 access token (django-oauth-toolkit), resolved
      to an ``OAuthPrincipal``.

    Returns ``(principal, None)`` on success or ``(None, JsonResponse-401)`` on
    any failure. Every 401 carries the RFC 9728 ``resource_metadata`` pointer.
    """
    # Absolute PRM URL for the WWW-Authenticate challenge (https behind proxy).
    prm = request.build_absolute_uri('/.well-known/oauth-protected-resource')

    header = request.headers.get('Authorization') or ''
    if not header.startswith('Bearer '):
        return None, _unauthorized('Missing Bearer token.', prm)
    raw = header[len('Bearer '):].strip()
    if not raw:
        return None, _unauthorized('Missing Bearer token.', prm)

    if raw.startswith(TOKEN_PREFIX):
        return _authenticate_mcp_key(raw, prm)
    return _authenticate_oauth_token(raw, prm)


def _authenticate_mcp_key(raw: str, prm: str):
    from ..models import McpApiKey

    key = (
        McpApiKey.objects.filter(key_hash=hash_token(raw), is_active=True)
        .select_related('organization', 'user')
        .first()
    )
    if key is None:
        return None, _unauthorized('Invalid or revoked token.', prm)

    McpApiKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now())
    return key, None


def _authenticate_oauth_token(raw: str, prm: str):
    from ..access import resolve_org

    try:
        from oauth2_provider.models import get_access_token_model
    except ImportError:  # OAuth provider not installed → treat as invalid token
        return None, _unauthorized('Invalid or expired token.', prm)

    token = (
        get_access_token_model().objects
        .select_related('user').filter(token=raw).first()
    )
    if token is None or not token.is_valid():
        return None, _unauthorized('Invalid or expired token.', prm)

    org = resolve_org(token.user)
    if org is None:
        return None, _unauthorized('Token user has no organization.', prm)

    scopes = token.scope.split() if token.scope else []
    return OAuthPrincipal(user=token.user, organization=org, scopes=scopes), None
