from django.db import migrations


OLD_IDENTIFIER = 'google:gemini-3.5-flash'
NEW_IDENTIFIER = 'google:gemini-3.6-flash'


def _rename(apps, old_identifier, new_identifier, new_display_name):
    """Move the Gemini Flash ChatbotModel row from ``old`` to ``new``.

    Renamed IN PLACE (not add-new + delete-old) so every Organization that
    explicitly selected Gemini Flash keeps its FK selection — the same
    reasoning as 0043's provider-prefix swap. Falls back to creating the row
    when the old identifier is absent (e.g. a database seeded after 0031 was
    squashed away), and no-ops when the target already exists so the unique
    constraint on ``identifier`` can never be violated.
    """
    ChatbotModel = apps.get_model('catalog', 'ChatbotModel')

    if ChatbotModel.objects.filter(identifier=new_identifier).exists():
        return

    row = ChatbotModel.objects.filter(identifier=old_identifier).first()
    if row is None:
        ChatbotModel.objects.create(
            identifier=new_identifier,
            display_name=new_display_name,
            sort_order=15,
            is_active=True,
        )
        return

    row.identifier = new_identifier
    row.display_name = new_display_name
    row.save(update_fields=['identifier', 'display_name'])


def to_36(apps, schema_editor):
    _rename(apps, OLD_IDENTIFIER, NEW_IDENTIFIER, 'Gemini 3.6 Flash')


def back_to_35(apps, schema_editor):
    _rename(apps, NEW_IDENTIFIER, OLD_IDENTIFIER, 'Gemini 3.5 Flash')


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0064_governancetask_condition_cleared_at'),
    ]

    operations = [
        migrations.RunPython(to_36, back_to_35),
    ]
