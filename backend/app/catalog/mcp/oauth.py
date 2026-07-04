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
import re

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
        """Surface the acting user's org branding — name, uploaded icon, and
        primary colour — to the styled consent template so its logo/accent match
        the SPA login's ``BrandLogo`` exactly."""
        context = super().get_context_data(**kwargs)
        org = resolve_org(self.request.user)
        if org is not None:
            context['org_name'] = getattr(org, 'name', None)
            icon = getattr(org, 'icon', None)
            context['org_icon_url'] = icon.url if icon else None
            color = (getattr(org, 'primary_color', None) or '').strip()
            # Only a strict hex — it is injected into a <style> block below.
            if re.fullmatch(r'#[0-9A-Fa-f]{3,8}', color):
                context['org_primary_color'] = color
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
        'token_endpoint_auth_methods_supported': [
            'client_secret_post', 'client_secret_basic',
        ],
    })
