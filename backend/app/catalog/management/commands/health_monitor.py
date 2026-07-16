"""
Run every health check and alert on trouble. The django-q schedule
(registered in apps.py) runs the same logic every 15 minutes; this command
exists for ad-hoc runs and testing:

    python manage.py health_monitor              # check + alert (de-duped)
    python manage.py health_monitor --no-notify  # check only, print results
    python manage.py health_monitor --force      # alert even if already alerted
"""
from django.core.management.base import BaseCommand

from catalog.health import monitor_and_alert


class Command(BaseCommand):
    help = 'Run health checks (DB, disk) and send Slack alerts on failure.'

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true',
                            help='Send an alert regardless of previous state (for testing).')
        parser.add_argument('--no-notify', action='store_true',
                            help="Run checks and print results but don't send alerts.")

    def handle(self, *args, **options):
        result = monitor_and_alert(force=options['force'], notify=not options['no_notify'])
        for c in result['checks']:
            line = f"[{c['status'].upper():4}] {c['name']} {c['detail']}"
            writer = self.stdout.write if c['status'] == 'ok' else self.stderr.write
            writer(line)
        self.stdout.write(self.style.NOTICE(f"Overall: {result['status'].upper()}"))
        if result['status'] == 'down':
            raise SystemExit(1)
