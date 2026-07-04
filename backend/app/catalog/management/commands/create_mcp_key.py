"""Mint an MCP bearer API key for a user.

    python manage.py create_mcp_key --email jason@example.com \
        [--name "Jason – Claude Desktop"] \
        [--scopes catalog:read powerbi:query bigquery:query] \
        [--org-id N]

Prints the raw token ONCE — it is stored only as a SHA-256 hash and cannot
be recovered later. Revoke keys in Django admin (Mcp api keys → is_active).
"""
from django.core.management.base import BaseCommand, CommandError

from catalog.mcp.auth import ALL_SCOPES, DEFAULT_SCOPES, mint_key
from catalog.models import CustomUser, Organization, OrganizationMembership


class Command(BaseCommand):
    help = 'Create an MCP API key for a user and print the raw token once.'

    def add_arguments(self, parser):
        parser.add_argument('--email', required=True, help='User email the key is bound to.')
        parser.add_argument('--name', default='default', help='Human label for the key.')
        parser.add_argument(
            '--scopes', nargs='+', default=list(DEFAULT_SCOPES), metavar='SCOPE',
            help=f'Scopes to grant (default: {" ".join(DEFAULT_SCOPES)}). '
                 f'Valid: {" ".join(ALL_SCOPES)}',
        )
        parser.add_argument(
            '--org-id', type=int, default=None,
            help="Organization id (default: the user's membership org).",
        )

    def handle(self, *args, **options):
        user = CustomUser.objects.filter(email__iexact=options['email']).first()
        if user is None:
            raise CommandError(f"No user with email {options['email']}")

        if options['org_id'] is not None:
            org = Organization.objects.filter(id=options['org_id']).first()
            if org is None:
                raise CommandError(f"No organization with id {options['org_id']}")
        else:
            mem = (
                OrganizationMembership.objects.filter(user=user)
                .select_related('organization')
                .first()
            )
            org = mem.organization if mem else Organization.objects.order_by('id').first()
            if org is None:
                raise CommandError('No organization found — pass --org-id.')

        try:
            key, raw = mint_key(
                user=user, organization=org,
                name=options['name'], scopes=options['scopes'],
            )
        except ValueError as exc:
            raise CommandError(str(exc))

        self.stdout.write(self.style.SUCCESS(
            f'Created MCP key "{key.name}" (prefix {key.key_prefix}…) for '
            f'{user.email} @ {org.name} with scopes: {", ".join(key.scopes)}'
        ))
        self.stdout.write('')
        self.stdout.write('Token (shown ONCE — store it now):')
        self.stdout.write(self.style.WARNING(f'  {raw}'))
