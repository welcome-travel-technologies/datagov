"""Central page-access map — the single source of truth.

The app has exactly three assignable access groups (plus an implicit
"everyone" tier for pages that are always visible). Every page in the UI
belongs to one tier. To re-map a page in the future, change the group on
its line in ``PAGE_ACCESS`` below — nothing else needs to change.

Mechanics: templates and views still gate on ``perms.can_view_<key>``
flags (e.g. ``perms.can_view_dictionary``). ``get_user_permissions`` in
``frontend_views.py`` is what turns a user's group membership into those
flags, using ``GROUP_PERM_KEYS`` derived here. So this file is the only
place the page → group relationship lives.

The three groups are Django ``auth.Group`` rows named exactly as the
constants below (``Company`` / ``Analytics`` / ``Admin``) — that is also
what shows in the Org Settings member chips and the add-member wizard.
"""

COMPANY = "Company"
ANALYTICS = "Analytics"
ADMIN = "Admin"
EVERYONE = "everyone"  # Dashboard + User Settings: always visible, not assignable

# Historical list of the three group names. KEPT AS-IS because migration 0028
# (consolidate_access_groups) and the backfill command import it at runtime —
# changing it would rewrite the past. New code that means "groups a user can be
# assigned in the UI" must use ASSIGNABLE_GROUPS below instead.
ACCESS_GROUPS = [COMPANY, ANALYTICS, ADMIN]

# Org-admin is NOT a global auth Group anymore — it lives on
# OrganizationMembership.is_admin (org-scoped, multi-tenant correct). The only
# self-service page-access groups are the feature tiers; "Admin" pages
# (Org Settings, Integrations) are unlocked solely by is_org_admin().
ASSIGNABLE_GROUPS = [COMPANY, ANALYTICS]

# (page label, perms.can_view_<key> flag, access group).
# ``key=None`` means the page is ungated for any authenticated user.
# NOTE: a single perm key can back several pages (e.g. "dbt" gates all three
# dbt pages). They are listed once per page here for clarity, but as long as
# every page sharing a key stays in the same group the mapping is exact.
PAGE_ACCESS = [
    ("Data Dictionary",               "dictionary",   COMPANY),
    ("Task Manager",                  "tasks",        COMPANY),
    ("Data Champions",                "champions",    COMPANY),
    ("AI Assistant",                  "chat",         COMPANY),
    ("PowerBI Catalog",               "powerbi",      COMPANY),
    ("PowerBI Report Health & Usage", "reports",      COMPANY),
    ("Lineage Graph",                 "lineage",      ANALYTICS),
    ("PowerBI Cleanup Opportunities", "unused",       ANALYTICS),
    ("PowerBI Top Assets & Impact",   "insights",     ANALYTICS),
    ("dbt Catalog",                   "dbt",          ANALYTICS),
    ("dbt Cleanup Opportunities",     "dbt",          ANALYTICS),
    ("dbt Top Assets & Impact",       "dbt",          ANALYTICS),
    ("Org Settings",                  "org_settings", ADMIN),
    ("Integrations",                  "integrations", ADMIN),
    ("Dashboard",                     None,           EVERYONE),
    ("User Settings",                 None,           EVERYONE),
]

# group name -> set of perm keys it unlocks (derived from PAGE_ACCESS).
# Only the assignable feature tiers grant page perms via group membership; the
# ADMIN tier's pages are gated by is_org_admin(), never by a group.
GROUP_PERM_KEYS: dict[str, set[str]] = {}
for _label, _key, _group in PAGE_ACCESS:
    if _key and _group in (COMPANY, ANALYTICS):
        GROUP_PERM_KEYS.setdefault(_group, set()).add(_key)

# Every page-view perm key in the system, derived from PAGE_ACCESS directly
# (independent of the group mapping) so org admins are granted ALL pages —
# including the ADMIN-tier ones that no group unlocks.
ALL_PERM_KEYS = {_key for _label, _key, _group in PAGE_ACCESS if _key}

# How the old fine-grained " Access" groups fold into the new three.
# Consumed once by the 0028 data migration, then the old groups are deleted.
LEGACY_GROUP_MAP = {
    "All Access":          [COMPANY, ANALYTICS, ADMIN],
    "Dictionary Access":   [COMPANY],
    "PowerBI Access":      [COMPANY],
    "Chat Access":         [COMPANY],
    "Reports Access":      [COMPANY],
    "Lineage Access":      [ANALYTICS],
    "Unused Access":       [ANALYTICS],
    "Insights Access":     [ANALYTICS],
    "dbt Access":          [ANALYTICS],
    "Org Settings Access": [ADMIN],
    "Integrations Access": [ADMIN],
}


def perm_keys_for_groups(group_names) -> set[str]:
    """All ``can_view_<key>`` flags unlocked by the given group names."""
    keys: set[str] = set()
    for name in group_names:
        keys |= GROUP_PERM_KEYS.get(name, set())
    return keys


# ---------------------------------------------------------------------------
# Org-scoped access resolution — the single source of truth.
#
# Permissions in this app are per-organization, so the org role lives on
# OrganizationMembership, not on a global auth.Group. These two helpers are the
# ONE place that answers "which org is this user acting in" and "is this user an
# admin there". Every layer (page views, the SPA API, DRF permission classes)
# routes through them so page-visibility and write-authorization can never drift
# apart again. Models are imported lazily to keep this module import-cycle free.
# ---------------------------------------------------------------------------

def resolve_org(user):
    """The Organization this user acts in: their membership's org, or — for a
    superuser with no membership — the first org. None if unauthenticated or
    orgless.

    NOTE: like the helpers it replaces, this picks the user's first membership.
    A dedicated "active org" concept is the right follow-up when a single user
    needs to act across multiple orgs.
    """
    if not getattr(user, "is_authenticated", False):
        return None
    from .models import OrganizationMembership, Organization

    mem = (
        OrganizationMembership.objects.filter(user=user)
        .select_related("organization")
        .first()
    )
    if mem:
        return mem.organization
    if getattr(user, "is_superuser", False):
        return Organization.objects.first()
    return None


def is_org_admin(user, org=None) -> bool:
    """True if the user is an admin of ``org`` (or of ANY org when ``org`` is
    None). Superusers are always admins. This is THE admin predicate."""
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    from .models import OrganizationMembership

    qs = OrganizationMembership.objects.filter(user=user, is_admin=True)
    if org is not None:
        qs = qs.filter(organization=org)
    return qs.exists()
