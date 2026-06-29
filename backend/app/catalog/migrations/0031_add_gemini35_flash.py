from django.db import migrations


def add_gemini35_flash(apps, schema_editor):
    ChatbotModel = apps.get_model('catalog', 'ChatbotModel')
    ChatbotModel.objects.update_or_create(
        identifier='google-gla:gemini-3.5-flash',
        defaults={
            'display_name': 'Gemini 3.5 Flash',
            'sort_order': 15,
            'is_active': True,
        },
    )


def remove_gemini35_flash(apps, schema_editor):
    ChatbotModel = apps.get_model('catalog', 'ChatbotModel')
    ChatbotModel.objects.filter(identifier='google-gla:gemini-3.5-flash').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0030_dataperson_is_other'),
    ]

    operations = [
        migrations.RunPython(add_gemini35_flash, remove_gemini35_flash),
    ]
