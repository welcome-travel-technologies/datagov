"""OAuth 2.1 surface for the MCP server (Stage M5 of docs/mcp-server-plan.md).

Three thin pieces layered on top of django-oauth-toolkit (which provides the
security-critical authorize/token/PKCE/consent/refresh machinery):

- ``OrgAdminAuthorizationView`` — DOT's authorize view, restricted to org
  admins (login is inherited from DOT; the admin check runs once authenticated).
- ``oauth_protected_resource_metadata`` — RFC 9728 protected-resource metadata,
  served at ``/.well-known/oauth-protected-resource``. This is what the MCP
  endpoint's ``401 WWW-Authenticate: … resource_metadata=…`` points at, so a
  client (claude.ai) discovers which authorization server to use.
- ``oauth_authorization_server_metadata`` — RFC 8414 AS metadata, served at
  ``/.well-known/oauth-authorization-server``. Points at DOT's endpoints
  (mounted under ``/api/o/``). No ``registration_endpoint`` — clients are
  registered manually (``python manage.py create_oauth_client``), so there is
  no public Dynamic Client Registration surface.

Absolute URLs come from ``request.build_absolute_uri``; ``SECURE_PROXY_SSL_HEADER``
is set (settings), so behind nginx/Cloudflare these are ``https://…``.
"""
import ipaddress
from urllib.parse import urlparse

from django.http import JsonResponse
from django.views.decorators.http import require_GET
from oauth2_provider.views import AuthorizationView

from ..access import is_org_admin, resolve_org
from .auth import ALL_SCOPES

# RFC 8414/9728 well-known paths (relative to the site root — see config/urls.py
# and the nginx `^~ /.well-known/oauth-` block that routes them to Django).
PROTECTED_RESOURCE_METADATA_PATH = '/.well-known/oauth-protected-resource'
MCP_ENDPOINT_PATH = '/api/mcp/'
AUTHORIZE_PATH = '/api/o/authorize/'
TOKEN_PATH = '/api/o/token/'
REVOKE_PATH = '/api/o/revoke_token/'

# Loopback redirect URIs a native/CLI client registers against (RFC 8252 §7.3).
# Claude Code opens a local callback server and uses http://localhost:PORT/callback
# (it also declares the 127.0.0.1 form), so both are registered; the port must
# match Claude Code's `--callback-port`. Prefilled by the Connectors UI's CLI mode.
DEFAULT_CLI_CALLBACK_PORT = 8080
DEFAULT_CLI_REDIRECT_URIS = [
    f'http://localhost:{DEFAULT_CLI_CALLBACK_PORT}/callback',
    f'http://127.0.0.1:{DEFAULT_CLI_CALLBACK_PORT}/callback',
]


def is_loopback_redirect_uri(uri: str) -> bool:
    """True if ``uri``'s host is a loopback address — ``localhost`` or any IP in
    the 127/8 or ::1 loopback ranges (RFC 8252 §7.3 native-app redirect). Such an
    http redirect never leaves the user's machine, so it is safe without TLS."""
    try:
        host = (urlparse(uri).hostname or '').lower()
    except ValueError:
        return False
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def redirect_uri_error(uris):
    """Validate redirect URIs for a to-be-created OAuth client; return an error
    string if any is disallowed, else ``None``.

    Policy (RFC 8252-aligned): ``https`` for any host; ``http`` ONLY for a
    loopback host (CLI/native clients like Claude Code). This is the gate that
    keeps ``ALLOWED_REDIRECT_URI_SCHEMES=['https','http']`` from allowing a
    plaintext redirect to a public host — enforce it on EVERY creation path."""
    bad = []
    for u in uris:
        if u.startswith('https://'):
            continue
        if u.startswith('http://') and is_loopback_redirect_uri(u):
            continue
        bad.append(u)
    if bad:
        return (
            'Redirect URIs must be https, or http on a loopback host '
            f'(localhost / 127.0.0.1) for CLI clients: {", ".join(bad)}'
        )
    return None


class OrgAdminAuthorizationView(AuthorizationView):
    """DOT's authorization view, gated to org admins.

    DOT's ``LoginRequiredMixin`` still handles the unauthenticated case
    (redirect to ``LOGIN_URL`` — our SPA login — with ``?next=`` back here). Once
    authenticated we require ``is_org_admin`` for the acting user's org, matching
    the "org admins only" decision for who may grant MCP access.
    """

    def dispatch(self, request, *args, **kwargs):
        user = request.user
        if user.is_authenticated and not is_org_admin(user, resolve_org(user)):
            return JsonResponse(
                {'error': 'Organization admin access required to authorize MCP access.'},
                status=403,
            )
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        """Surface the acting user's org name to the styled consent template
        (templates/oauth2_provider/authorize.html) for the branded eyebrow."""
        context = super().get_context_data(**kwargs)
        org = resolve_org(self.request.user)
        if org is not None:
            context['org_name'] = getattr(org, 'name', None)
        return context


def _origin(request) -> str:
    """The scheme+host origin, no trailing slash (the OAuth issuer)."""
    return request.build_absolute_uri('/').rstrip('/')


@require_GET
def oauth_protected_resource_metadata(request):
    """RFC 9728 — advertises the MCP resource and its authorization server(s)."""
    return JsonResponse({
        'resource': request.build_absolute_uri(MCP_ENDPOINT_PATH),
        'authorization_servers': [_origin(request)],
        'scopes_supported': list(ALL_SCOPES),
        'bearer_methods_supported': ['header'],
    })


@require_GET
def oauth_authorization_server_metadata(request):
    """RFC 8414 — the authorization server (this Django app, via DOT)."""
    return JsonResponse({
        'issuer': _origin(request),
        'authorization_endpoint': request.build_absolute_uri(AUTHORIZE_PATH),
        'token_endpoint': request.build_absolute_uri(TOKEN_PATH),
        'revocation_endpoint': request.build_absolute_uri(REVOKE_PATH),
        'scopes_supported': list(ALL_SCOPES),
        'response_types_supported': ['code'],
        'grant_types_supported': ['authorization_code', 'refresh_token'],
        'code_challenge_methods_supported': ['S256'],
        # 'none' = a public (PKCE-only) client that authenticates with no secret —
        # what a native/CLI client (Claude Code) registers as; the two
        # client_secret_* methods are for confidential web clients (claude.ai).
        'token_endpoint_auth_methods_supported': [
            'client_secret_post', 'client_secret_basic', 'none',
        ],
    })
