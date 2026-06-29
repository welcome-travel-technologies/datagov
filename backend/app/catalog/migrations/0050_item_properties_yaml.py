# Generated for schema.yml properties in lineage detail panel.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0049_item_compiled_expression'),
    ]

    operations = [
        migrations.AddField(
            model_name='item',
            name='properties_yaml',
            field=models.TextField(blank=True, null=True),
        ),
    ]
