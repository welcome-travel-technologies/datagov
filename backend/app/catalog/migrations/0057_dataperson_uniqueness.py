"""Make duplicate DataPerson rows impossible.

Runs after 0056's merge, in its own transaction, so the indexes are built on a
table with no pending trigger events and no remaining duplicates.

Two rules:
  * one DataPerson per login per organization — the member-save upsert keys on
    that pair, while the same login can belong to several organizations;
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
                fields=('user', 'organization'),
                name='uniq_dataperson_user_org',
                nulls_distinct=False,
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
