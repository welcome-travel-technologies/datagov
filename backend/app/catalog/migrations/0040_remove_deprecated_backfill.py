from django.db import migrations


def remove_backfilled_deprecated_log(apps, schema_editor):
    """Delete the spurious DEPRECATED rows written by 0038.

    0038 created one DEPRECATED status-change row per group with a soft-deleted
    item, assuming deletion implied deprecation. That conflates ``Item.deleted``
    (a separate soft-delete flag, mostly ETL-set) with ``ItemGroup`` governance
    status, so the rows are wrong (production had ~10.3k of them against only a
    hundred genuinely-deprecated groups).

    They carry a distinct signature — system-written, so no prior status and no
    acting user — that real, forward-going status changes never have
    (``log_status_change`` always records the previous status), so this delete
    cannot touch genuine history. Irreversible by design: we don't want the bad
    rows back, so the reverse is a no-op.
    """
    StatusChangeLog = apps.get_model('catalog', 'StatusChangeLog')
    StatusChangeLog.objects.filter(
        new_status='DEPRECATED', old_status__isnull=True, changed_by__isnull=True,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0039_itemgroup_deleted_at'),
    ]

    operations = [
        migrations.RunPython(remove_backfilled_deprecated_log, migrations.RunPython.noop),
    ]
