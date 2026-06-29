from django.db import migrations


def backfill_deprecated_log(apps, schema_editor):
    """Seed StatusChangeLog with the DEPRECATED transitions that predate the
    log, reconstructing the timestamp from the items' deletion stamps.

    One DEPRECATED row per ItemGroup that owns a soft-deleted item, timestamped
    at the most recent ``Item.deleted_at`` among its items (falling back to the
    group's ``updated_at`` for items deleted before deleted_at was stamped).
    Idempotent: groups that already have a DEPRECATED log row are skipped, so
    re-running never duplicates.
    """
    Item = apps.get_model('catalog', 'Item')
    ItemGroup = apps.get_model('catalog', 'ItemGroup')
    StatusChangeLog = apps.get_model('catalog', 'StatusChangeLog')
    from django.db.models import Max

    already = set(
        StatusChangeLog.objects.filter(new_status='DEPRECATED')
        .values_list('item_group_id', flat=True)
    )

    # One record per group: the latest deletion stamp among its deleted items.
    grouped = (
        Item.objects.filter(deleted=True, item_group__isnull=False)
        .values('item_group_id')
        .annotate(when=Max('deleted_at'))
    )

    for row in grouped:
        gid = row['item_group_id']
        if gid in already:
            continue
        grp = ItemGroup.objects.filter(pk=gid).first()
        if grp is None:
            continue
        when = row['when'] or grp.updated_at
        log = StatusChangeLog.objects.create(
            organization_id=grp.organization_id,
            item_group_id=gid,
            group_key=grp.group_key,
            old_status=None,          # historical previous status is unknown
            new_status='DEPRECATED',
            changed_by=None,          # system backfill, no acting user
        )
        # changed_at is auto_now_add; override it via a direct UPDATE so the
        # row reflects when the item was actually deleted, not migration time.
        if when is not None:
            StatusChangeLog.objects.filter(pk=log.pk).update(changed_at=when)


def remove_backfilled_log(apps, schema_editor):
    """Reverse: drop the system-backfilled DEPRECATED rows (no acting user and
    no prior status — the signature this migration writes)."""
    StatusChangeLog = apps.get_model('catalog', 'StatusChangeLog')
    StatusChangeLog.objects.filter(
        new_status='DEPRECATED', old_status__isnull=True, changed_by__isnull=True,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0037_statuschangelog'),
    ]

    operations = [
        migrations.RunPython(backfill_deprecated_log, remove_backfilled_log),
    ]
