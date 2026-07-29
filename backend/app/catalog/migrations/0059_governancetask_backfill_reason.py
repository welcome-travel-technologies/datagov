"""Backfill GovernanceTask.reason and collapse legacy duplicate open tasks.

Data only, in its own transaction. It sits between the column (0058) and the
indexes/constraint (0060) because Postgres refuses to CREATE INDEX on a table
holding pending trigger events from row changes in the same transaction, and
Django defers index creation to the end of whichever migration declares it.
Combining any two of these three steps aborts the migration on a database that
actually has rows to change.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    GovernanceTask = apps.get_model('catalog', 'GovernanceTask')
    using = schema_editor.connection.alias
    tasks = GovernanceTask.objects.using(using)

    # Every pre-existing task came from a status flip, so its status IS its reason.
    for status in ('ATTENTION', 'DELETED'):
        tasks.filter(trigger_status=status).update(reason=status)

    # Before 0058, the only way a task became done was the human-facing Done
    # endpoint. Preserve that durable dismissal explicitly. Without this,
    # 0064 cannot recognize an active manual episode and the first sweep would
    # recreate work that a person had already dismissed.
    manual = tasks.filter(
        state='done',
        closed_reason__isnull=True,
    ).update(closed_reason='manual')
    if manual:
        print(f'[0059] marked {manual} legacy done task(s) as manual')

    # The old rule was one open task per GROUP, so duplicates on (group, reason)
    # shouldn't exist — but legacy rows with a blank/unknown status all land on
    # the default reason and collide. Keep the newest of each colliding set open
    # and retire the rest, so 0060's constraint can be added.
    seen = set()
    stale = []
    rows = (tasks.filter(state='open')
            .order_by('-created_at', '-id')
            .values_list('id', 'item_group_id', 'reason'))
    for task_id, group_id, reason in rows.iterator():
        if group_id is None:
            continue
        key = (group_id, reason)
        if key in seen:
            stale.append(task_id)
        else:
            seen.add(key)
    if stale:
        for i in range(0, len(stale), 2000):
            tasks.filter(id__in=stale[i:i + 2000]).update(
                state='done', closed_reason='resolved',
            )
        print(f'[0059] collapsed {len(stale)} duplicate open task(s)')


def unbackfill(apps, schema_editor):
    """No-op: `reason` disappears with the column, and re-opening the collapsed
    duplicates would recreate the very collision this removed."""


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0058_governancetask_reason'),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
