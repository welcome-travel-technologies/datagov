from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0013_chatbot_model'),
    ]

    operations = [
        migrations.AddField(
            model_name='customuser',
            name='default_workspaces',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='integrationsource',
            name='default_workspace_id',
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
    ]
