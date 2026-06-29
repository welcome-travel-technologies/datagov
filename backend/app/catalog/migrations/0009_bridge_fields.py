"""
Add fields to support the dbt ↔ PowerBI bridge by BigQuery FQN.

- Item.bq_project / bq_schema / bq_source_name: PowerBI tables persist the
  BigQuery FQN extracted from their M-query partition source.
- Item.schema_name / alias: dbt items persist the schema and alias separately
  (today they live concatenated as 'schema.alias' in table_name) so the
  matcher can join against PowerBI's bq_* triple without re-parsing.
- NetworkEdge.bridge_reason: records which pass produced a cross-tool bridge
  edge ('bq_fqn' | 'name_full' | 'name_tail'). NULL for in-domain edges.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0008_text_fields_for_dbt_data'),
    ]

    operations = [
        migrations.AddField(
            model_name='item',
            name='schema_name',
            field=models.TextField(
                blank=True, null=True,
                help_text='Schema part of the 3-part FQN (database.schema.table)',
            ),
        ),
        migrations.AddField(
            model_name='item',
            name='alias',
            field=models.TextField(
                blank=True, null=True,
                help_text='dbt alias / materialized table name (without schema prefix)',
            ),
        ),
        migrations.AddField(
            model_name='item',
            name='bq_project',
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name='item',
            name='bq_schema',
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name='item',
            name='bq_source_name',
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name='networkedge',
            name='bridge_reason',
            field=models.CharField(blank=True, max_length=16, null=True),
        ),
    ]
