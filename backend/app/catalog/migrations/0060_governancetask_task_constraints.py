"""Indexes + the (group, reason) uniqueness rule for open tasks.

Separated from 0059 so the backfill's row changes are committed first —
Postgres refuses to CREATE INDEX on a table with pending trigger events.

The partial unique constraint is the dedupe rule made real: at most one OPEN
task per (item_group, reason). ``generate_tasks`` relies on it directly via
``bulk_create(ignore_conflicts=True)``, which is what makes the sweep safe to
re-run instead of racing a read-then-write check.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0059_governancetask_backfill_reason'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='governancetask',
            index=models.Index(fields=['assignee', 'state'], name='cat_task_assignee_state_idx'),
        ),
        migrations.AddIndex(
            model_name='governancetask',
            index=models.Index(fields=['reason', 'state'], name='cat_task_reason_state_idx'),
        ),
        migrations.AddConstraint(
            model_name='governancetask',
            constraint=models.UniqueConstraint(
                condition=models.Q(('state', 'open')),
                fields=('item_group', 'reason'),
                name='uniq_open_task_per_group_reason',
            ),
        ),
    ]
