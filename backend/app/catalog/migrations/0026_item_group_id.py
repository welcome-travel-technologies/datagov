"""
Add Item.group_id for measure grouping.

The same PB_MEASURE name can appear in many datasets/workspaces. group_id
("{organization_id or 0}::{lower(trim(item_name))}") lets the Data Dictionary
collapse all instances of a measure into a single, governance-consistent row.

Only PB_MEASURE rows get a group_id; everything else stays NULL. The backfill
sets it for existing measures in one UPDATE so the feature works before the
next ETL run; subsequent loads keep it fresh (see load_data).

The backfill is raw SQL on purpose: COALESCE / CAST AS TEXT / `||` / LOWER /
TRIM all behave identically on PostgreSQL and SQLite, and it produces the exact
same key as the ETL CASE expression in load_data.py (NULL org -> '0').
"""
from django.db import migrations, models


_BACKFILL_SQL = """
    UPDATE catalog_item
    SET group_id = COALESCE(CAST(organization_id AS TEXT), '0')
                   || '::' || LOWER(TRIM(item_name))
    WHERE item_type = 'PB_MEASURE'
      AND item_name IS NOT NULL
      AND TRIM(item_name) <> '';
"""

_CLEAR_SQL = "UPDATE catalog_item SET group_id = NULL WHERE group_id IS NOT NULL;"


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0025_remove_organizationmembership_access_level'),
    ]

    operations = [
        migrations.AddField(
            model_name='item',
            name='group_id',
            field=models.CharField(blank=True, max_length=1100, null=True),
        ),
        migrations.AddIndex(
            model_name='item',
            index=models.Index(fields=['group_id'], name='cat_item_group_idx'),
        ),
        migrations.RunSQL(_BACKFILL_SQL, reverse_sql=_CLEAR_SQL),
    ]
