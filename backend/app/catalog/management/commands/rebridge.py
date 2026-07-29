"""
Re-run only the cross-tool bridging step (dbt ↔ PowerBI).

Today rebuilding bridges requires a full ETL re-run; this command isolates
the step so an operator can refresh edges after fixing a dbt manifest or a
PowerBI source binding without re-extracting anything.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from catalog.models import Organization
from catalog.services.bridge_builder import build_cross_tool_bridges
from catalog.services.load_scope import acquire_catalog_load_lock


class Command(BaseCommand):
    help = 'Rebuild dbt ↔ PowerBI bridge edges using the FQN-first matcher.'

    def add_arguments(self, parser):
        parser.add_argument('--organization-id', type=int, required=True,
                            help='Organization PK to scope the rebridge')

    def handle(self, *args, **kwargs):
        organization_id = kwargs.get('organization_id')
        if (
            isinstance(organization_id, bool)
            or not isinstance(organization_id, int)
            or organization_id <= 0
        ):
            raise CommandError('--organization-id must be a positive integer.')
        if not Organization.objects.filter(pk=organization_id).exists():
            raise CommandError(
                f'Organization {organization_id} does not exist.'
            )

        with transaction.atomic():
            acquire_catalog_load_lock()
            with connection.cursor() as cursor:
                stats = build_cross_tool_bridges(
                    cursor,
                    organization_id,
                    write=self.stdout.write,
                )

        self.stdout.write(self.style.SUCCESS(
            f"Rebridge complete: {stats['table_bridges']} table edges, "
            f"{stats['column_bridges']} column edges."
        ))
