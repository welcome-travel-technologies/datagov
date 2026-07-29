from django.db import migrations, models
from django.db.models import Max


def backfill_condition_episodes(apps, schema_editor):
    """Keep only the latest still-active manual dismissal unresolved."""
    GovernanceTask = apps.get_model('catalog', 'GovernanceTask')
    Item = apps.get_model('catalog', 'Item')
    StatusChangeLog = apps.get_model('catalog', 'StatusChangeLog')
    using = schema_editor.connection.alias

    # 0059 originally shipped without labeling old Done rows. Some databases
    # may already have that version recorded. Its synthetic duplicate closures
    # never set completed_at, while the historical human Done endpoint always
    # did. Classify the synthetic rows first so they can never masquerade as a
    # durable manual dismissal, then label the remaining human rows.
    legacy_resolved_backfilled = (
        GovernanceTask.objects.using(using)
        .filter(
            state='done',
            closed_reason__isnull=True,
            completed_at__isnull=True,
        )
        .update(closed_reason='resolved')
    )
    if legacy_resolved_backfilled:
        print(
            '[0064] marked '
            f'{legacy_resolved_backfilled} legacy duplicate task(s) resolved'
        )

    legacy_manual_backfilled = (
        GovernanceTask.objects.using(using)
        .filter(
            state='done',
            closed_reason__isnull=True,
            completed_at__isnull=False,
        )
        .update(closed_reason='manual')
    )
    if legacy_manual_backfilled:
        print(
            '[0064] marked '
            f'{legacy_manual_backfilled} legacy done task(s) as manual'
        )

    # 0062 preserves empty groups so their curation is not lost, but without a
    # member they are not currently actionable. Resolve that historical
    # episode so a later ingestion that repopulates the group can open fresh
    # work. Precompute membership once instead of querying per task.
    populated_group_ids = set(
        Item.objects.using(using)
        .filter(item_group_id__isnull=False)
        .values_list('item_group_id', flat=True)
        .distinct()
        .iterator(chunk_size=1000)
    )
    active_group_ids = set(
        Item.objects.using(using)
        .filter(item_group_id__isnull=False, deleted=False)
        .values_list('item_group_id', flat=True)
        .distinct()
        .iterator(chunk_size=1000)
    )

    # An open row for the same episode key is newer than every historical Done
    # row and proves the condition reappeared after that dismissal. The older
    # manual row is therefore historical even if today's condition is active.
    open_keys = set(
        GovernanceTask.objects.using(using)
        .filter(state='open', item_group__isnull=False)
        .values_list('organization_id', 'item_group_id', 'reason')
        .iterator(chunk_size=1000)
    )

    # A Done row dismisses only the status episode in which it was completed.
    # If the group entered that same status again later, the old dismissal
    # cannot suppress the new episode. Aggregate once in PostgreSQL rather than
    # issuing a history lookup per task. The exact organization is part of the
    # key so corrupt cross-tenant history can never influence a task.
    latest_status_entry = {
        (organization_id, item_group_id, new_status): changed_at
        for (
            organization_id,
            item_group_id,
            new_status,
            changed_at,
        ) in (
            StatusChangeLog.objects.using(using)
            .filter(
                item_group__isnull=False,
                new_status__in=('UNVERIFIED', 'ATTENTION', 'DELETED'),
            )
            .values('organization_id', 'item_group_id', 'new_status')
            .annotate(latest_changed_at=Max('changed_at'))
            .values_list(
                'organization_id',
                'item_group_id',
                'new_status',
                'latest_changed_at',
            )
            .iterator(chunk_size=1000)
        )
    }

    seen = set()
    tasks = (
        GovernanceTask.objects.using(using).filter(
            state='done',
            closed_reason='manual',
        )
        .select_related('item_group')
        .order_by(
            'organization_id',
            'item_group_id',
            'reason',
            '-completed_at',
            '-id',
        )
    )
    changed = []
    for task in tasks.iterator(chunk_size=1000):
        group = task.item_group
        active = False
        if (
            group is not None
            and task.item_group_id in populated_group_ids
        ):
            if task.reason == 'UNVERIFIED':
                active = (
                    task.item_group_id in active_group_ids
                    and group.kind == 'measure_name'
                    and group.status == 'UNVERIFIED'
                    and not group.deleted
                )
            elif task.reason == 'ATTENTION':
                active = group.status == 'ATTENTION'
            elif task.reason == 'DELETED':
                active = group.status == 'DELETED'
            elif task.reason == 'NO_CATEGORY':
                active = (
                    task.item_group_id in active_group_ids
                    and group.category_id is None
                    and not group.deleted
                    and group.status != 'DELETED'
                )

        key = (task.organization_id, task.item_group_id, task.reason)
        dismissed_at = (
            task.completed_at or task.updated_at or task.created_at
        )
        entered_at = latest_status_entry.get(key)
        entered_after_dismissal = (
            entered_at is not None
            and entered_at > dismissed_at
        )
        if (
            active
            and not entered_after_dismissal
            and key not in open_keys
            and key not in seen
        ):
            seen.add(key)
            continue
        task.condition_cleared_at = dismissed_at
        changed.append(task)
        if len(changed) >= 1000:
            GovernanceTask.objects.using(using).bulk_update(
                changed, ['condition_cleared_at'], batch_size=1000,
            )
            changed = []
    if changed:
        GovernanceTask.objects.using(using).bulk_update(
            changed, ['condition_cleared_at'], batch_size=1000,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0063_name_integrity_constraints'),
    ]

    operations = [
        migrations.AddField(
            model_name='governancetask',
            name='condition_cleared_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(
            backfill_condition_episodes,
            migrations.RunPython.noop,
        ),
    ]
