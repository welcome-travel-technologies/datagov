from django.db import migrations, models


class Migration(migrations.Migration):
    """Add semantic column-derivation type to NetworkEdge.

    ``kind``/``level`` are classified from endpoint types; ``lineage_type`` is a
    distinct semantic dimension (how a column was derived) that can only come from
    SQL/DAX analysis, so it is stored rather than computed.
    """

    dependencies = [
        ('catalog', '0047_metricsmap_canvas'),
    ]

    operations = [
        migrations.AddField(
            model_name='networkedge',
            name='lineage_type',
            field=models.CharField(blank=True, max_length=16, null=True),
        ),
    ]
