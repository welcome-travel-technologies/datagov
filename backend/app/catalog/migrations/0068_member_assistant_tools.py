from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('catalog', '0067_document_explorer_chat')]
    operations = [
        migrations.AddField(model_name='organizationmembership', name='assistant_tool_access', field=models.JSONField(default=dict, blank=True)),
        migrations.AddField(model_name='organizationmembership', name='assistant_tool_preferences', field=models.JSONField(default=dict, blank=True)),
    ]
