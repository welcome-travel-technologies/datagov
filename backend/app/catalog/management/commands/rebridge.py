"""
Re-run only the cross-tool bridging step (dbt ↔ PowerBI).

Today rebuilding bridges requires a full ETL re-run; this command isolates
the step so an operator can refresh edges after fixing a dbt manifest or a
PowerBI source binding without re-extracting anything.
"""
from django.core.management.base import BaseCommand
from django.db import connection, transaction

from catalog.services.bridge_builder import build_cross_tool_bridges


class Command(BaseCommand):
    help = 'Rebuild dbt ↔ PowerBI bridge edges using the FQN-first matcher.'

    def add_arguments(self, parser):
        parser.add_argument('--organization-id', type=int, default=None,
                            help='Organization PK to scope the rebridge')

    def handle(self, *args, **kwargs):
        organization_id = kwargs.get('organization_id')
        org_id_literal = 'NULL'
        if organization_id is not None:
            org_id_literal = str(int(organization_id))

        with transaction.atomic(), connection.cursor() as cursor:
            stats = build_cross_tool_bridges(
                cursor,
                org_id_literal,
                write=self.stdout.write,
            )

        self.stdout.write(self.style.SUCCESS(
            f"Rebridge complete: {stats['table_bridges']} table edges, "
            f"{stats['column_bridges']} column edges."
        ))
