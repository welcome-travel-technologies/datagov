"""
Convert variable-length string fields in catalog_item to TEXT (unbounded).

BigQuery STRUCT types stored in 'datatype'/'column_type' can be arbitrarily long
(e.g. deeply nested STRUCT<field1 ARRAY<STRUCT<...>>, ...>).
Table/schema names from dbt can also exceed any fixed limit.

Using TEXT instead of VARCHAR(N) removes ALL length constraints and future-proofs
the schema. In PostgreSQL, ALTER COLUMN TYPE from VARCHAR(N) to TEXT is safe
and does not rewrite the table.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0007_widen_varchar_fields'),
    ]

    operations = [
        migrations.AlterField(
            model_name='item',
            name='workspace_id',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='item',
            name='workspace_name',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='item',
            name='dataset_id',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='item',
            name='dataset_name',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='item',
            name='table_name',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='item',
            name='datatype',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='item',
            name='column_type',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='item',
            name='formatstring',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='item',
            name='database_name',
            field=models.TextField(
                blank=True, null=True,
                help_text='Database part of the 3-part FQN (database.schema.table)',
            ),
        ),
    ]
