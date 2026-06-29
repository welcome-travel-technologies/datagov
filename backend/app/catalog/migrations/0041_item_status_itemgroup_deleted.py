"""Re-introduce a per-item ``status`` column and add ``ItemGroup.deleted``.

Background: migration 0029 moved governance onto ``ItemGroup`` and hard-dropped
the old per-item ``status`` column. We now re-add ``Item.status`` as a
denormalized MIRROR of ``ItemGroup.status`` (the group stays the single source
of truth; the API cascade keeps the column in sync). This lets item-level views
(PowerBI Cleanup) and the BigQuery export read/filter status without joining,
and makes a group status change visibly propagate to its items.

``ItemGroup.deleted`` is a group-level soft-delete flag; the API cascades it
down to ``Item.deleted`` / ``Item.deleted_at`` and forces DEPRECATED status.

The data step backfills ``Item.status`` from each item's group so existing
rows start consistent.
"""
from django.db import migrations, models


def backfill_item_status(apps, schema_editor):
    ItemGroup = apps.get_model('catalog', 'ItemGroup')
    Item = apps.get_model('catalog', 'Item')
    for ig in ItemGroup.objects.all().values('id', 'status').iterator(chunk_size=2000):
        if ig['status'] and ig['status'] != 'UNVERIFIED':
            Item.objects.filter(item_group_id=ig['id']).update(status=ig['status'])


def noop_reverse(apps, schema_editor):
    # Nothing to undo: the columns are dropped by the schema reverse below.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0040_remove_deprecated_backfill'),
    ]

    operations = [
        migrations.AddField(
            model_name='item',
            name='status',
            field=models.CharField(
                choices=[
                    ('UNVERIFIED', 'Unverified'),
                    ('VERIFIED', 'Verified'),
                    ('DEPRECATED', 'Deprecated'),
                    ('ATTENTION', 'Attention'),
                ],
                default='UNVERIFIED', db_index=True, max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='itemgroup',
            name='deleted',
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(backfill_item_status, noop_reverse),
    ]
