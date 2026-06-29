from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0019_chatsession_flow_state'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='chatsession',
            name='flow_state',
        ),
        migrations.AddField(
            model_name='chatsession',
            name='langgraph_thread_id',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
    ]
