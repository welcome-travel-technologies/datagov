from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0004_widen_item_type_datatype_column_type'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='item',
            index=models.Index(fields=['service', 'item_type', 'deleted'], name='cat_item_svc_type_del_idx'),
        ),
        migrations.AddIndex(
            model_name='item',
            index=models.Index(fields=['organization', 'service', 'item_type', 'deleted'], name='cat_item_org_svc_type_del_idx'),
        ),
        migrations.AddIndex(
            model_name='item',
            index=models.Index(fields=['organization', 'deleted', 'service'], name='cat_item_org_del_svc_idx'),
        ),
        migrations.AddIndex(
            model_name='item',
            index=models.Index(fields=['deleted', 'item_type'], name='cat_item_del_type_idx'),
        ),
    ]