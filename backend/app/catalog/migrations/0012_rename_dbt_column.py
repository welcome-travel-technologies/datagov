"""Wipe catalog data so it can be re-populated under the new item_type naming.

Both the dbt-side rename (COLUMN -> DBT_COLUMN) and the PowerBI-side rename
(TABLE/COLUMN/MEASURE/REPORT/PAGE/VISUAL/FIELD/WORKSPACE -> PB_* equivalents)
ship together. Rather than rewriting every Item row + graph node/edge in
place, we truncate the catalog tables and let the user re-run the ETL.

Run order after migrate:
  1. python manage.py load_data        (PowerBI / Fabric)
  2. python manage.py load_dbt_data    (dbt)
  3. python manage.py run_workflow_final  (rebuild bridges + summary)
"""
from django.db import migrations


def forwards(apps, schema_editor):
    Item = apps.get_model('catalog', 'Item')
    NetworkNode = apps.get_model('catalog', 'NetworkNode')
    NetworkEdge = apps.get_model('catalog', 'NetworkEdge')
    Summary = apps.get_model('catalog', 'Summary')

    NetworkEdge.objects.all().delete()
    NetworkNode.objects.all().delete()
    Item.objects.all().delete()
    Summary.objects.all().delete()


def backwards(apps, schema_editor):
    # Cannot reverse a data wipe.
    pass


class Migration(migrations.Migration):
    atomic = True

    dependencies = [
        ('catalog', '0011_workflow_raw_export'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
