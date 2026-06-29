"""Collapse the global 'Admin' auth Group into OrganizationMembership.is_admin.

Org-admin is inherently per-organization, so it now lives on the membership
(multi-tenant correct) rather than a global Group that can't distinguish org A
from org B. This migration migrates existing admins and removes the group so it
can't be reassigned. See catalog/access.py (is_org_admin) for the new model.
"""
from django.db import migrations


def collapse_admin_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    User = apps.get_model("catalog", "CustomUser")
    OrganizationMembership = apps.get_model("catalog", "OrganizationMembership")

    if not Group.objects.filter(name="Admin").exists():
        return

    # Every membership held by an Admin-group user becomes an admin membership —
    # matching the old behavior where the global group granted admin everywhere.
    admin_user_ids = list(
        User.objects.filter(groups__name="Admin").values_list("id", flat=True)
    )
    if admin_user_ids:
        OrganizationMembership.objects.filter(user_id__in=admin_user_ids).update(
            is_admin=True
        )

    # Delete the group (clears the M2M rows too) so admin is purely the flag.
    Group.objects.filter(name="Admin").delete()


def restore_admin_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    User = apps.get_model("catalog", "CustomUser")
    OrganizationMembership = apps.get_model("catalog", "OrganizationMembership")

    admin_group, _ = Group.objects.get_or_create(name="Admin")
    admin_user_ids = list(
        OrganizationMembership.objects.filter(is_admin=True)
        .values_list("user_id", flat=True)
        .distinct()
    )
    for uid in admin_user_ids:
        try:
            User.objects.get(id=uid).groups.add(admin_group)
        except User.DoesNotExist:
            pass


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0045_metricsmap"),
    ]

    operations = [
        migrations.RunPython(collapse_admin_group, restore_admin_group),
    ]
