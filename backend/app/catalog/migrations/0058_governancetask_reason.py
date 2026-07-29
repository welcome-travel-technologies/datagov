"""Give GovernanceTask a `reason` column (schema only).

Before this, a task's identity was its ItemGroup and the only trigger was a
status flip, so the Task Manager could not express "this measure is still
unverified" or "this measure has no category" — there is no status *transition*
behind either, and a group could only ever hold one open task anyway.

`reason` is the task kind. It decides who the task routes to (owner vs steward)
and, together with the group, forms the uniqueness key for OPEN tasks — which is
what lets the sweep in ``governance_tasks.generate_tasks`` be re-run at will.

SCHEMA ONLY, on purpose. `reason` is indexed, and Django emits index creation
into ``schema_editor.deferred_sql``, which runs at the very END of the
migration. Postgres refuses to CREATE INDEX on a table that still has pending
trigger events from row changes in the same transaction — so putting the
backfill here would abort the migration on any database with rows to touch.
(That is not hypothetical: it aborts after collapsing ~800 duplicate tasks.)
The backfill is therefore 0059 and the extra indexes/constraint are 0060.
"""

from django.db import migrations, models

_STATUS_CHOICES = [
    ('UNVERIFIED', 'Unverified'),
    ('VERIFIED', 'Verified'),
    ('DELETED', 'To Be Deleted'),
    ('ATTENTION', 'Attention'),
]


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0057_dataperson_uniqueness'),
    ]

    operations = [
        migrations.AddField(
            model_name='governancetask',
            name='closed_reason',
            field=models.CharField(
                blank=True, null=True, max_length=32,
                choices=[('manual', 'Marked done'), ('resolved', 'Auto-resolved')],
            ),
        ),
        migrations.AddField(
            model_name='governancetask',
            name='reason',
            field=models.CharField(
                db_index=True, default='ATTENTION', max_length=32,
                choices=[
                    ('UNVERIFIED', 'Verify'), ('ATTENTION', 'Attention'),
                    ('DELETED', 'To Be Deleted'), ('NO_CATEGORY', 'Category'),
                ],
            ),
        ),
        migrations.AlterField(
            model_name='governancetask',
            name='trigger_status',
            field=models.CharField(
                blank=True, null=True, max_length=20, choices=_STATUS_CHOICES,
            ),
        ),
        # Label-only change: 'DELETED' stays the stored value everywhere.
        migrations.AlterField(
            model_name='item',
            name='status',
            field=models.CharField(
                db_index=True, default='UNVERIFIED', max_length=20, choices=_STATUS_CHOICES,
            ),
        ),
        migrations.AlterField(
            model_name='itemgroup',
            name='status',
            field=models.CharField(
                default='UNVERIFIED', max_length=20, choices=_STATUS_CHOICES,
            ),
        ),
        migrations.AlterField(
            model_name='statuschangelog',
            name='new_status',
            field=models.CharField(max_length=20, choices=_STATUS_CHOICES),
        ),
        migrations.AlterField(
            model_name='statuschangelog',
            name='old_status',
            field=models.CharField(
                blank=True, null=True, max_length=20, choices=_STATUS_CHOICES,
            ),
        ),
    ]
