"""Make duplicate DataPerson rows impossible.

Runs after 0056's merge, in its own transaction, so the indexes are built on a
table with no pending trigger events and no remaining duplicates.

Two rules:
  * one DataPerson per login — the member-save upsert keys on ``user``, and a
    second row for the same user made that upsert ambiguous;
  * one person per name per org, case-insensitively — the actual duplicate the
    dropdowns were showing.
"""

from django.db import migrations, models
from django.db.models.functions import Lower


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0056_dataperson_dedupe'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='dataperson',
            constraint=models.UniqueConstraint(
                condition=models.Q(('user__isnull', False)),
                fields=('user',),
                name='uniq_dataperson_user',
            ),
        ),
        migrations.AddConstraint(
            model_name='dataperson',
            constraint=models.UniqueConstraint(
                Lower('name'), 'organization',
                name='uniq_dataperson_name_org',
            ),
        ),
    ]
