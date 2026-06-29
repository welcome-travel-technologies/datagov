"""Consolidate the old fine-grained " Access" groups into the three new
user-friendly groups (Company / Analytics / Admin).

For every user, their legacy groups are folded into the new groups per
``catalog.access.LEGACY_GROUP_MAP``; afterwards every legacy " Access"
group is deleted so the UI only ever shows the three new ones.

This is a one-way data migration: reverse is a no-op (the original
fine-grained assignments cannot be reconstructed once consolidated).
"""
from django.db import migrations

from catalog.access import ACCESS_GROUPS, LEGACY_GROUP_MAP


def consolidate(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    CustomUser = apps.get_model('catalog', 'CustomUser')

    new_groups = {name: Group.objects.get_or_create(name=name)[0] for name in ACCESS_GROUPS}

    for user in CustomUser.objects.all().prefetch_related('groups'):
        legacy_names = [g.name for g in user.groups.all() if g.name in LEGACY_GROUP_MAP]
        if not legacy_names:
            continue
        targets = set()
        for ln in legacy_names:
            targets.update(LEGACY_GROUP_MAP[ln])
        user.groups.add(*[new_groups[t] for t in targets])

    # Remove every obsolete legacy group. The new groups have no " Access"
    # suffix, so this can never delete them.
    Group.objects.filter(name__in=list(LEGACY_GROUP_MAP)).delete()
    Group.objects.filter(name__endswith=" Access").delete()


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0027_item_is_group_primary'),
    ]

    operations = [
        migrations.RunPython(consolidate, noop_reverse),
    ]
