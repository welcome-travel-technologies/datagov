from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter
from django.db import transaction
from django.utils import timezone

from .jobs import JobConflict, enqueue, reconcile_expired
from .models import KnowledgeSource


def next_occurrence(cron, zone, now=None):
    if not cron:
        return None
    return croniter(cron, (now or timezone.now()).astimezone(ZoneInfo(zone))).get_next(datetime)


def tick():
    from .permissions import enabled
    if not enabled():
        return 0
    reconcile_expired()
    now, count = timezone.now(), 0
    for source_id in KnowledgeSource.objects.filter(is_active=True, next_run_at__lte=now).exclude(cron='').values_list('id', flat=True):
        with transaction.atomic():
            source = KnowledgeSource.objects.select_for_update().get(id=source_id)
            if not source.is_active or not source.next_run_at or source.next_run_at > now:
                continue
            key = f'schedule:{source.next_run_at.isoformat()}'
            try:
                enqueue(source, key)
                count += 1
            except JobConflict:
                # Existing work covers this occurrence; never launch an overlap.
                pass
            source.next_run_at = next_occurrence(source.cron, source.timezone, now)
            source.save(update_fields=['next_run_at'])
    return count


def ensure_schedule(**kwargs):
    from django_q.models import Schedule
    Schedule.objects.update_or_create(name='document_explorer_tick', defaults={
        'func': 'knowledge.scheduling.tick', 'schedule_type': Schedule.MINUTES,
        'minutes': 1, 'repeats': -1,
    })
