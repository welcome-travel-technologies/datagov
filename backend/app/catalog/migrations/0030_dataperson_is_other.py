"""
Add DataPerson.is_other — an extra person-level data role.

Owner / Steward double as assignable governance slots on catalog
item-groups. "Other" is intentionally NOT a governance slot: it only
classifies people (selectable in the add-member wizard and admin,
filterable via the API ?role=other / ?is_other=true), so people who are
neither owners nor stewards can still be tagged.

New column defaults to False, so existing rows are unaffected.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0029_itemgroup'),
    ]

    operations = [
        migrations.AddField(
            model_name='dataperson',
            name='is_other',
            field=models.BooleanField(default=False),
        ),
    ]
