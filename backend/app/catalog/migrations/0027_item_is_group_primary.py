"""
Add Item.is_group_primary — the user-chosen primary instance of a measure
group_id.

The Data Dictionary previously picked the "representative" instance of a
measure group purely with a client-side heuristic. This field lets a user
pin a specific (workspace, dataset) instance as the group's primary, which
then drives the Dictionary's default workspace / dataset / DAX and the
Dashboard's per-workspace measure attribution. At most one instance per
group should be True; the heuristic still applies when none is set.

User-managed (like status / ownership): NOT overwritten by the ETL upsert.
New column defaults to False, so existing rows fall back to the heuristic
until a user pins one.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0026_item_group_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='item',
            name='is_group_primary',
            field=models.BooleanField(default=False),
        ),
    ]
