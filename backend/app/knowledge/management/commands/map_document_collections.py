"""Apply a reviewed, explicit provenance mapping without moving any evidence."""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from catalog.models import IntegrationSource
from knowledge.models import KnowledgeSource


class Command(BaseCommand):
    help = 'Validate/apply explicit collection group and connection IDs. ACLs, jobs and document IDs are preserved.'

    def add_arguments(self, parser):
        parser.add_argument('--mapping-file', required=True)
        parser.add_argument('--apply', action='store_true')

    @transaction.atomic
    def handle(self, *args, **options):
        try:
            payload = json.loads(Path(options['mapping_file']).read_text(encoding='utf-8'))
            org_id, rows = payload['organization_id'], payload['collections']
            if not isinstance(rows, list) or not rows or len(rows) > 10000:
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            raise CommandError('Use a bounded mapping containing organization_id and collections.')
        seen, changes = set(), []
        for row in rows:
            source_id = row.get('id')
            if source_id in seen:
                raise CommandError('Duplicate collection ID.')
            seen.add(source_id)
            source = KnowledgeSource.objects.select_for_update().filter(pk=source_id, organization_id=org_id).first()
            if source is None:
                raise CommandError('Every collection must exist in the selected organization.')
            group, integration_id = row.get('import_group', ''), row.get('integration_id')
            if not isinstance(group, str) or len(group) > 200:
                raise CommandError('Invalid import group.')
            if integration_id and not IntegrationSource.objects.filter(pk=integration_id, organization_id=org_id).exists():
                raise CommandError('Connection is outside the selected organization.')
            if source.integration_id and source.integration_id != integration_id:
                raise CommandError('This mapping would move an established connection relationship.')
            if source.pipeline_enabled and source.integration_id != integration_id:
                raise CommandError('Cannot move an active indexing output.')
            changes.append((source, group, integration_id))
        if options['apply']:
            for source, group, integration_id in changes:
                source.import_group, source.integration_id = group, integration_id
                source.save(update_fields=['import_group', 'integration'])
        self.stdout.write(json.dumps({'validated': len(changes), 'applied': bool(options['apply']),
                                      'preserved': ['document_ids', 'version_ids', 'access', 'schedules', 'run_history']}))
