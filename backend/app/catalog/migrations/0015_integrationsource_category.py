from django.db import migrations, models


# Mirror of IntegrationSource.DEFAULT_CATEGORY_BY_TYPE — kept literal here so
# the migration is hermetic and survives future renames of the model attribute.
_DEFAULTS = {
    'powerbi_fabric': 'visualization',
    'dbt':            'transformation',
    'postgresql':     'transformation',
    'mysql':          'transformation',
    'snowflake':      'transformation',
    'csv_upload':     'transformation',
}


def backfill_category(apps, schema_editor):
    IntegrationSource = apps.get_model('catalog', 'IntegrationSource')
    for source_type, category in _DEFAULTS.items():
        IntegrationSource.objects.filter(source_type=source_type).update(category=category)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0014_customuser_default_workspaces'),
    ]

    operations = [
        migrations.AddField(
            model_name='integrationsource',
            name='category',
            field=models.CharField(
                choices=[
                    ('transformation', 'Transformation'),
                    ('visualization', 'Visualization'),
                ],
                default='visualization',
                help_text='Pipeline layer. Transformation sources run before visualization sources.',
                max_length=20,
            ),
        ),
        migrations.RunPython(backfill_category, noop_reverse),
    ]
