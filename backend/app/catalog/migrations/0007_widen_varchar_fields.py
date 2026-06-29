"""
Widen VARCHAR fields that can overflow for dbt data.

dbt unique_ids (stored in item_id, lineage_tag, dataset_id) follow the pattern
  {resource_type}.{project_name}.{model_path}
and for tests with long names / accepted_values, can easily exceed 255 chars.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0006_chatbot_bigquery_dbt_flags'),
    ]

    operations = [
        # ── catalog_item ─────────────────────────────────────────────────────
        migrations.AlterField(
            model_name='item',
            name='item_id',
            field=models.CharField(max_length=2000, primary_key=True, serialize=False, unique=True),
        ),
        migrations.AlterField(
            model_name='item',
            name='lineage_tag',
            field=models.CharField(blank=True, max_length=2000, null=True),
        ),
        migrations.AlterField(
            model_name='item',
            name='item_name',
            field=models.CharField(blank=True, max_length=1000, null=True),
        ),
        migrations.AlterField(
            model_name='item',
            name='workspace_id',
            field=models.CharField(blank=True, max_length=500, null=True),
        ),
        migrations.AlterField(
            model_name='item',
            name='workspace_name',
            field=models.CharField(blank=True, max_length=500, null=True),
        ),
        migrations.AlterField(
            model_name='item',
            name='dataset_id',
            field=models.CharField(blank=True, max_length=2000, null=True),
        ),
        migrations.AlterField(
            model_name='item',
            name='dataset_name',
            field=models.CharField(blank=True, max_length=500, null=True),
        ),
        migrations.AlterField(
            model_name='item',
            name='table_name',
            field=models.CharField(blank=True, max_length=500, null=True),
        ),
        migrations.AlterField(
            model_name='item',
            name='datatype',
            field=models.CharField(blank=True, max_length=500, null=True),
        ),
        migrations.AlterField(
            model_name='item',
            name='column_type',
            field=models.CharField(blank=True, max_length=500, null=True),
        ),
        migrations.AlterField(
            model_name='item',
            name='database_name',
            field=models.CharField(
                blank=True, max_length=500, null=True,
                help_text='Database part of the 3-part FQN (database.schema.table)',
            ),
        ),
    ]
