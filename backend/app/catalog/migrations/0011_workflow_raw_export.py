from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0010_item_relationships'),
    ]

    operations = [
        migrations.CreateModel(
            name='WorkflowRawExport',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('is_active', models.BooleanField(default=False)),
                ('gcs_bucket_name', models.CharField(blank=True, max_length=255, null=True)),
                ('gcs_service_account_json', models.TextField(blank=True, help_text='Full GCP service account JSON with Cloud Storage access', null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('organization', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='workflow_raw_export', to='catalog.organization')),
            ],
        ),
    ]
