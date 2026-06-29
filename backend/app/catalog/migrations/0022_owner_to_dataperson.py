from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion

from catalog.models import _validate_slack_handle


def backfill_steward_flag(apps, schema_editor):
    """Any DataPerson currently referenced by Item.steward becomes is_steward=True.

    Existing rows default to is_owner=True (set by the AddField default), since
    historically the table was used as the owner directory. This call layers
    is_steward on top so people who are also stewards show up in that dropdown.
    """
    DataPerson = apps.get_model('catalog', 'DataPerson')
    Item = apps.get_model('catalog', 'Item')
    steward_ids = set(
        Item.objects.exclude(steward__isnull=True).values_list('steward_id', flat=True)
    )
    if steward_ids:
        DataPerson.objects.filter(pk__in=steward_ids).update(is_steward=True)


def noop_reverse(apps, schema_editor):
    # Reverse leaves the booleans in place — they are harmless if the model
    # is renamed back. Reversing the rename is handled by Django via the
    # paired RenameModel below.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0021_pblivequerythread'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # 1. Rename the model. Django keeps the underlying table name in sync
        #    (catalog_owner → catalog_dataperson) and rewrites FK metadata on
        #    Item.ownership_person and Item.steward automatically.
        migrations.RenameModel(
            old_name='Owner',
            new_name='DataPerson',
        ),

        # 2. Update related_name on the Department FK so reverse access goes
        #    via department.data_persons (matches the new model name).
        migrations.AlterField(
            model_name='dataperson',
            name='department',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='data_persons',
                to='catalog.department',
            ),
        ),
        migrations.AlterField(
            model_name='dataperson',
            name='organization',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='data_persons',
                to='catalog.organization',
            ),
        ),

        # 3. New optional fields.
        migrations.AddField(
            model_name='dataperson',
            name='user',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='data_person_profiles',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name='dataperson',
            name='is_owner',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='dataperson',
            name='is_steward',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='dataperson',
            name='slack_handle',
            field=models.CharField(
                blank=True, max_length=80, null=True,
                validators=[_validate_slack_handle],
                help_text="Slack handle, e.g. '@jane'. Optional.",
            ),
        ),

        # 4. Backfill is_steward for people who are already assigned as
        #    stewards on existing Items.
        migrations.RunPython(backfill_steward_flag, noop_reverse),
    ]
