from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('catalog', '0065_gemini36_flash')]

    operations = [
        migrations.AddField(
            model_name='customuser',
            name='google_subject',
            field=models.CharField(blank=True, editable=False, max_length=255, null=True, unique=True),
        ),
    ]
