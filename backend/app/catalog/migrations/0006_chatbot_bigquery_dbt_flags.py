from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0005_catalog_query_indexes'),
    ]

    operations = [
        migrations.AddField(
            model_name='organization',
            name='bigquery_tools_enabled',
            field=models.BooleanField(
                default=False,
                help_text='Allow the AI Assistant to execute read-only live queries against BigQuery.',
            ),
        ),
        migrations.AddField(
            model_name='organization',
            name='dbt_tools_enabled',
            field=models.BooleanField(
                default=False,
                help_text='Allow the AI Assistant to use dbt catalog, SQL, and dbt lineage tools.',
            ),
        ),
    ]