"""
Management command: test_sources

Tests API connectivity for every active IntegrationSource in the database.
Uses the source registry — each source type is a self-contained class.

Usage:
    python manage.py test_sources
    python manage.py test_sources --source-id 3   # test a specific source only
"""
import sys
import os
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Test API access for all (or one) active IntegrationSource(s)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--source-id', type=int, default=None,
            help='Only test this specific IntegrationSource ID',
        )

    def handle(self, *args, **options):
        # Make the etl package importable
        etl_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), '../../../../')
        )
        if etl_root not in sys.path:
            sys.path.insert(0, etl_root)

        from catalog.models import IntegrationSource
        from etl.sources.registry import get_source

        qs = IntegrationSource.objects.filter(is_active=True)
        if options['source_id']:
            qs = qs.filter(pk=options['source_id'])

        if not qs.exists():
            self.stderr.write('No active IntegrationSource(s) found.')
            return

        for source in qs:
            self.stdout.write(f'\n{"-"*60}')
            self.stdout.write(
                f'[?]  Source #{source.pk}  |  {source.name}  |  type: {source.source_type}'
            )
            self.stdout.write('-'*60)

            try:
                src = get_source(source)
            except ValueError as e:
                self.stderr.write(f'  [WARN] {e}')
                continue

            result = src.test()
            for line in result.get('lines', []):
                self.stdout.write(f'  {line}')

            if result['status'] == 'ok':
                self.stdout.write(self.style.SUCCESS('  --> PASS'))
            else:
                self.stderr.write('  --> FAIL')

        self.stdout.write(f'\n{"-"*60}\nDone.\n')
