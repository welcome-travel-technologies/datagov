# Generated for compiled SQL in lineage detail panel.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0048_networkedge_lineage_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='item',
            name='compiled_expression',
            field=models.TextField(blank=True, null=True),
        ),
    ]
