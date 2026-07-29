"""Enforce trimmed, non-empty names including organization=NULL rows.

0062 committed all row cleanup first so these expression indexes are not built
while PostgreSQL still has pending trigger events.
"""

import django.db.models.functions.text
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0062_integrity_cleanup'),
    ]

    operations = [
        # Fresh installs get this org-scoped identity index from the corrected
        # 0057 migration. Existing databases may already have recorded 0057
        # while still carrying its former global user-only index, so reconcile
        # the physical database idempotently here without changing state.
        migrations.RunSQL(
            sql=[
                'DROP INDEX IF EXISTS "uniq_dataperson_user";',
                (
                    'CREATE UNIQUE INDEX IF NOT EXISTS '
                    '"uniq_dataperson_user_org" '
                    'ON "catalog_dataperson" '
                    '("user_id", "organization_id") NULLS NOT DISTINCT '
                    'WHERE "user_id" IS NOT NULL;'
                ),
            ],
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RemoveConstraint(
            model_name='dataperson',
            name='uniq_dataperson_name_org',
        ),
        migrations.RemoveConstraint(
            model_name='definition',
            name='uniq_definition_name_org',
        ),
        migrations.AddConstraint(
            model_name='dataperson',
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower(
                    django.db.models.functions.text.Trim('name')),
                models.F('organization'),
                name='uniq_dataperson_name_org',
                nulls_distinct=False,
            ),
        ),
        migrations.AddConstraint(
            model_name='dataperson',
            constraint=models.CheckConstraint(
                condition=~models.Q(('name__regex', r'^\s*$')),
                name='dataperson_name_not_blank',
            ),
        ),
        migrations.AddConstraint(
            model_name='definition',
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower(
                    django.db.models.functions.text.Trim('name')),
                models.F('organization'),
                name='uniq_definition_name_org',
                nulls_distinct=False,
            ),
        ),
        migrations.AddConstraint(
            model_name='definition',
            constraint=models.CheckConstraint(
                condition=~models.Q(('name__regex', r'^\s*$')),
                name='definition_name_not_blank',
            ),
        ),
        migrations.AddConstraint(
            model_name='category',
            constraint=models.CheckConstraint(
                condition=~models.Q(
                    ('name__iregex', r'^\s*to be deleted\s*$'),
                ),
                name='category_name_not_reserved',
            ),
        ),
        migrations.AddConstraint(
            model_name='itemgroup',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(('definition__isnull', True))
                    | models.Q(('kind', 'measure_name'))
                ),
                name='itemgroup_definition_measure_only',
            ),
        ),
    ]
