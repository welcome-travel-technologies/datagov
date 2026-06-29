# Generated for UserActivityLog (login + page view tracking).

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0017_powerbireportusage'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='UserActivityLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('email', models.CharField(blank=True, default='', max_length=255)),
                ('event', models.CharField(choices=[('login', 'Login'), ('logout', 'Logout'), ('login_failed', 'Login Failed'), ('pageview', 'Page View')], max_length=20)),
                ('path', models.CharField(blank=True, default='', max_length=500)),
                ('method', models.CharField(blank=True, default='', max_length=10)),
                ('status_code', models.PositiveSmallIntegerField(blank=True, null=True)),
                ('ip', models.GenericIPAddressField(blank=True, null=True)),
                ('user_agent', models.CharField(blank=True, default='', max_length=500)),
                ('timestamp', models.DateTimeField(auto_now_add=True)),
                ('user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='activity_logs', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-timestamp'],
            },
        ),
        migrations.AddIndex(
            model_name='useractivitylog',
            index=models.Index(fields=['-timestamp'], name='catalog_use_timesta_2ee4f6_idx'),
        ),
        migrations.AddIndex(
            model_name='useractivitylog',
            index=models.Index(fields=['user', '-timestamp'], name='catalog_use_user_id_c98c7e_idx'),
        ),
        migrations.AddIndex(
            model_name='useractivitylog',
            index=models.Index(fields=['event', '-timestamp'], name='catalog_use_event_23826b_idx'),
        ),
    ]
