from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0020_chatsession_langgraph_thread_id'),
    ]

    operations = [
        migrations.CreateModel(
            name='PbLiveQueryThread',
            fields=[
                ('thread_id', models.CharField(max_length=64, primary_key=True, serialize=False)),
                ('stage', models.CharField(default='plan', max_length=32)),
                ('state', models.JSONField(blank=True, default=dict)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'indexes': [models.Index(fields=['updated_at'], name='catalog_pbl_updated_idx')],
            },
        ),
    ]
