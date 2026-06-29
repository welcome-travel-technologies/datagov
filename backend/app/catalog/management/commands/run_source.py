from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Run an IntegrationSource by ID, capturing logs into SourceRunLog'

    def add_arguments(self, parser):
        parser.add_argument('source_id', type=int, help='ID of the IntegrationSource to run')
        parser.add_argument('--triggered-by', type=str, default='manual', help='Who triggered this run')

    def handle(self, *args, **options):
        from catalog.integration_tasks import run_source_task

        source_id = options['source_id']
        triggered_by = options['triggered_by']

        self.stdout.write(f"Executing run_source_task for source_id={source_id}...")
        status = run_source_task(source_id, triggered_by=triggered_by)

        if status == 'success':
            self.stdout.write(self.style.SUCCESS(f"✅ Source run completed successfully."))
        else:
            self.stderr.write(f"❌ Source run failed with status: {status}")


