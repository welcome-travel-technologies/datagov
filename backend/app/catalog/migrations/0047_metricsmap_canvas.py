from django.db import migrations, models


class Migration(migrations.Migration):
    """Add the canvas-editor fields to MetricsMap.

    `kind` discriminates the YAML metric scratchpad from the visual canvas
    editor; `graph` holds the whole diagram document (nodes/edges/groups/
    viewport/meta) for canvas maps. Existing rows default to 'scratchpad'.
    """

    dependencies = [
        ('catalog', '0046_collapse_admin_group_into_membership'),
    ]

    operations = [
        migrations.AddField(
            model_name='metricsmap',
            name='kind',
            field=models.CharField(
                choices=[('scratchpad', 'Scratchpad'), ('canvas', 'Canvas')],
                default='scratchpad',
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name='metricsmap',
            name='graph',
            field=models.JSONField(blank=True, null=True),
        ),
    ]
