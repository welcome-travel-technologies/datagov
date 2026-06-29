"""API-only helpers shared by the DRF views and the SPA auth endpoints.

The classic server-rendered pages were removed — the React app
(welcome-data-catalog-react/frontend) is the only frontend now, and Django
serves just ``/api/`` + ``/admin/``. The three functions kept here are the ones
the API still imports:

  * ``get_user_permissions``      -> /api/me/ perms (spa_auth) + RBAC flags
  * ``get_access_groups_qs``      -> the assignable page-access groups (spa_auth)
  * ``compute_pb_cleanup_counts`` -> /api/pb-cleanup-counts/ (views)
"""
from collections import defaultdict

from django.db.models import Q

from catalog.access import (
    GROUP_PERM_KEYS, ALL_PERM_KEYS, ASSIGNABLE_GROUPS, is_org_admin,
)
from .models import Item


def get_access_groups_qs():
    """The self-service page-access groups as a queryset, ordered for the UI.

    Org-admin is no longer a group (it's OrganizationMembership.is_admin), so
    this is just the feature tiers. Returns a real queryset (not a list) so
    callers can still chain ``.filter(...)``. Order follows ASSIGNABLE_GROUPS.
    """
    from django.contrib.auth.models import Group
    from django.db.models import Case, When, IntegerField

    ordering = Case(
        *[When(name=name, then=pos) for pos, name in enumerate(ASSIGNABLE_GROUPS)],
        output_field=IntegerField(),
    )
    return Group.objects.filter(name__in=ASSIGNABLE_GROUPS).order_by(ordering)


def get_user_permissions(user):
    if not user.is_authenticated:
        # Default read-only / view permissions for unauthenticated users
        perms = defaultdict(bool)
        return perms

    if user.is_superuser:
        return defaultdict(lambda: True)

    perms = defaultdict(bool)

    # Page access is driven entirely by the central map in catalog/access.py:
    # each feature group (Company / Analytics) unlocks a fixed set of
    # can_view_<key> flags. To re-map a page, edit PAGE_ACCESS there.
    for group in user.groups.all():
        for key in GROUP_PERM_KEYS.get(group.name, ()):
            perms[f'can_view_{key}'] = True

    # Org admins get every page-view permission — including the ADMIN-tier pages
    # (Org Settings, Integrations) that no group unlocks. is_org_admin() is the
    # single admin predicate shared with the API gates. Editing in the dictionary
    # is open to any authenticated org member (no per-member read/write split).
    if is_org_admin(user):
        for key in ALL_PERM_KEYS:
            perms[f'can_view_{key}'] = True
        perms['is_admin'] = True
    else:
        perms['is_admin'] = False

    return perms


def compute_pb_cleanup_counts(pb_qs, workspace_name=None, dataset_name=None):
    """Cleanup-category counts for the PowerBI hygiene view.

    Honours the same Workspace/Dataset filters the table applies so the KPI
    cards and tab badges track the filtered view instead of always showing the
    global totals. ``pb_qs`` must already be org-scoped and deleted-filtered by
    the caller.
    """
    if workspace_name:
        pb_qs = pb_qs.filter(workspace_name=workspace_name)
    if dataset_name:
        pb_qs = pb_qs.filter(dataset_name=dataset_name)

    missing_docs_qs = pb_qs.filter(item_type__in=['PB_MEASURE', 'PB_COLUMN']).filter(
        (Q(description__isnull=True) | Q(description='')) &
        (Q(item_group__custom_description__isnull=True) | Q(item_group__custom_description=''))
    )

    return {
        'unused_measures': pb_qs.filter(item_type='PB_MEASURE', is_unused=True).count(),
        'unused_columns': pb_qs.filter(item_type='PB_COLUMN', is_unused=True).count(),
        'missing_descriptions': missing_docs_qs.count(),
        'attention': pb_qs.filter(item_group__status='ATTENTION').count(),
        'deprecated': pb_qs.filter(item_group__status='DELETED').count(),
    }
