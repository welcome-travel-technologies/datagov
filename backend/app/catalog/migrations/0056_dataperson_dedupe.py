"""Merge duplicate DataPerson rows.

The Owner / Steward dropdowns were listing the same person twice. Root cause:
``DataPerson`` had no uniqueness rule at all, and the member-save upsert matched
on ``user`` alone — so a login-less row for someone (added via the Django admin
or a bulk import before they had an account) was never found when they later got
a login, and a second row with the identical name appeared beside it.

Deliberately split from the AddConstraint migration that follows: Postgres
refuses to CREATE INDEX on a table that still has pending FK trigger events from
row changes in the same transaction, and this migration changes a lot of rows.
Each Django migration gets its own transaction, so ending here flushes them.
"""

from django.db import migrations

from catalog.people_dedupe import dedupe_data_persons


def forwards(apps, schema_editor):
    DataPerson = apps.get_model('catalog', 'DataPerson')
    ItemGroup = apps.get_model('catalog', 'ItemGroup')
    GovernanceTask = apps.get_model('catalog', 'GovernanceTask')
    summary = dedupe_data_persons(
        DataPerson, ItemGroup, GovernanceTask, apply=True, log=print,
    )
    print(f'[0056] DataPerson dedupe: {summary}')


def backwards(apps, schema_editor):
    """Irreversible in substance — merged rows can't be un-merged. A no-op so
    the constraints in 0057 can still be rolled back."""


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0055_mcpapikey'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
