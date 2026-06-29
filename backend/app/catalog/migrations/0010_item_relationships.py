"""
Add semantic-model relationship fields to Item.

- is_related: True for TABLE / COLUMN items that participate in any
  relationship declared in their semantic model's relationships.tmdl.
- relationships_json: list of relationship descriptors from the perspective
  of this item ({role, this_table, this_column, other_table, other_column,
  cardinality, other_cardinality, cross_filter, is_active}).
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0009_bridge_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='item',
            name='is_related',
            field=models.BooleanField(
                default=False,
                help_text='True if this TABLE/COLUMN participates in a relationship in its semantic model.',
            ),
        ),
        migrations.AddField(
            model_name='item',
            name='relationships_json',
            field=models.JSONField(
                blank=True, default=list,
                help_text='List of relationship descriptors from the perspective of this item.',
            ),
        ),
    ]
