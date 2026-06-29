from django.db import migrations


OLD_PREFIX = 'google-gla:'
NEW_PREFIX = 'google:'


def _reprefix(apps, old, new):
    """Swap the provider prefix on every ChatbotModel.identifier that uses it.

    Renaming the row in place keeps each Organization's FK selection valid, so
    an org that explicitly picked a Gemini model keeps it — just with a working
    provider prefix.
    """
    ChatbotModel = apps.get_model('catalog', 'ChatbotModel')
    existing = set(ChatbotModel.objects.values_list('identifier', flat=True))
    for model in ChatbotModel.objects.filter(identifier__startswith=old):
        renamed = new + model.identifier[len(old):]
        # Skip if the target identifier already exists (unique constraint).
        if renamed in existing:
            continue
        model.identifier = renamed
        model.save(update_fields=['identifier'])
        existing.add(renamed)


def fix_prefix(apps, schema_editor):
    # pydantic-ai dropped the deprecated `google-gla` provider alias; the
    # canonical name is now `google` (GoogleProvider / google-genai SDK, which
    # still reads GEMINI_API_KEY as a fallback). Selecting a Gemini model under
    # the old prefix raised "Unknown provider: google-gla".
    _reprefix(apps, OLD_PREFIX, NEW_PREFIX)


def revert_prefix(apps, schema_editor):
    _reprefix(apps, NEW_PREFIX, OLD_PREFIX)


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0042_alter_governancetask_trigger_status_and_more'),
    ]

    operations = [
        migrations.RunPython(fix_prefix, revert_prefix),
    ]
