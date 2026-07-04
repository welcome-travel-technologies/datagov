"""Register an OAuth2 client (django-oauth-toolkit ``Application``) so a browser
MCP client — claude.ai's custom connector — can authenticate to ``/api/mcp/``
via OAuth 2.1 (docs/mcp-server-plan.md M5).

    python manage.py create_oauth_client --name "claude.ai" \
        [--redirect-uri https://claude.ai/api/mcp/auth_callback] \
        [--public]

Prints ``client_id`` and ``client_secret`` ONCE. The secret is hashed on save
and cannot be recovered — to rotate, create a new client and delete the old one
(Django admin → Applications). Paste client_id/secret into claude.ai's
connector dialog → Advanced settings.

Default is a CONFIDENTIAL client (client_secret + PKCE). Pass ``--public`` for a
PKCE-only public client (no secret) — e.g. a native/CLI client that can't keep
one. For the **Claude Code CLI** (loopback OAuth callback):

    python manage.py create_oauth_client --name "Claude Code" --public \
        --redirect-uri http://localhost:8080/callback \
        --redirect-uri http://127.0.0.1:8080/callback

then ``claude mcp add --transport http --client-id <id> --callback-port 8080
<name> https://<host>/api/mcp/``. http redirect URIs are allowed only on a
loopback host (localhost / 127.0.0.1).
"""
from django.core.management.base import BaseCommand

# claude.ai (web/desktop/mobile/Cowork) posts the authorization response here.
DEFAULT_REDIRECT_URIS = ['https://claude.ai/api/mcp/auth_callback']


class Command(BaseCommand):
    help = 'Create an OAuth2 client (Application) for the MCP OAuth connector.'

    def add_arguments(self, parser):
        parser.add_argument('--name', required=True, help='Human label for the client.')
        parser.add_argument(
            '--redirect-uri', action='append', dest='redirect_uris', default=None,
            metavar='URL',
            help=f'Allowed redirect URI (repeatable). Default: {DEFAULT_REDIRECT_URIS[0]}',
        )
        parser.add_argument(
            '--public', action='store_true',
            help='Public PKCE client (no client_secret). Default is confidential.',
        )

    def handle(self, *args, **options):
        from django.core.management.base import CommandError
        from oauth2_provider.generators import (
            generate_client_id, generate_client_secret,
        )
        from oauth2_provider.models import get_application_model

        from catalog.mcp.oauth import redirect_uri_error

        Application = get_application_model()
        redirect_uris = options['redirect_uris'] or DEFAULT_REDIRECT_URIS

        # https anywhere; http only on a loopback host (CLI clients). The global
        # ALLOWED_REDIRECT_URI_SCHEMES permits http, so enforce loopback here too.
        scheme_err = redirect_uri_error(redirect_uris)
        if scheme_err:
            raise CommandError(scheme_err)

        client_id = generate_client_id()
        is_public = options['public']
        client_secret = '' if is_public else generate_client_secret()

        app = Application(
            name=options['name'],
            client_id=client_id,
            client_type=(
                Application.CLIENT_PUBLIC if is_public else Application.CLIENT_CONFIDENTIAL
            ),
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris=' '.join(redirect_uris),
            # Explicit consent screen (org admin approves scopes) — the M5 decision.
            skip_authorization=False,
        )
        if client_secret:
            # DOT hashes this on save; the plaintext printed below is the only copy.
            app.client_secret = client_secret
        app.save()

        self.stdout.write(self.style.SUCCESS(
            f'Created OAuth client "{app.name}" '
            f'({"public" if is_public else "confidential"}); '
            f'redirect URIs: {", ".join(redirect_uris)}'
        ))
        self.stdout.write('')
        self.stdout.write('Client ID:')
        self.stdout.write(self.style.WARNING(f'  {client_id}'))
        if client_secret:
            self.stdout.write('Client secret (shown ONCE — store it now):')
            self.stdout.write(self.style.WARNING(f'  {client_secret}'))
        else:
            self.stdout.write('Public client — no client secret (PKCE only).')
