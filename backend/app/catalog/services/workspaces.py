"""
Per-source workspace resolution.

A workspace is a top-level grouping in PowerBI / Fabric. The chatbot and the
lineage UI need to scope queries to a single workspace by default; only when
the user explicitly asks to compare across workspaces should both be in scope.

Resolution rule (per IntegrationSource):
  1. If the source has exactly one workspace in the catalog → use it.
  2. Else if the user has a saved default for that source → use it.
  3. Else if the source itself has an org-level default → use it.
  4. Else → return None (caller decides: ask, or show a dropdown).

The user's per-source default always wins over the org default; the org default
is only consulted when the user has not picked one themselves.
"""
from typing import Optional

from ..models import IntegrationSource, Item


def get_workspaces_for_source(source: IntegrationSource):
    """Return a sorted list of {'id', 'name'} dicts for the workspaces in
    ``source``.

    Reads from the dedicated ``PB_WORKSPACE`` item rows when present (one row
    per workspace — naturally deduped, cheap query). Falls back to a distinct
    sweep across all items when the source hasn't materialized PB_WORKSPACE
    items yet (e.g. brand-new source whose ETL hasn't completed).
    """
    base = Item.objects.filter(
        integration_source=source, deleted=False,
    ).exclude(workspace_id__isnull=True).exclude(workspace_id='')

    rows = list(
        base.filter(item_type='PB_WORKSPACE')
            .values('workspace_id', 'workspace_name')
    )
    if not rows:
        # ``.order_by()`` clears Item.Meta.ordering; without it Django injects
        # ``item_name`` into the SELECT, which causes DISTINCT to dedupe across
        # (item_name, workspace_id, workspace_name) instead of just the
        # workspace columns — flooding the dropdown with thousands of dupes.
        rows = list(
            base.order_by().values('workspace_id', 'workspace_name').distinct()
        )

    seen = {}
    for r in rows:
        wid = r['workspace_id']
        if wid not in seen:
            seen[wid] = r['workspace_name'] or wid
    return sorted(
        ({'id': wid, 'name': name} for wid, name in seen.items()),
        key=lambda w: (w['name'] or '').lower(),
    )


def resolve_default_workspace(user, source: IntegrationSource) -> Optional[str]:
    """Return the workspace_id to use for ``user`` against ``source``.

    Returns None when there are zero workspaces, or more than one with no user
    default — caller is responsible for asking the user / showing a picker.
    """
    workspaces = get_workspaces_for_source(source)
    if not workspaces:
        return None
    if len(workspaces) == 1:
        return workspaces[0]['id']
    saved = (getattr(user, 'default_workspaces', None) or {}).get(str(source.id))
    if saved and any(w['id'] == saved for w in workspaces):
        return saved
    org_default = source.default_workspace_id
    if org_default and any(w['id'] == org_default for w in workspaces):
        return org_default
    return None


def get_user_default_workspace_name(user, org) -> str:
    """Pick a single workspace name to pre-select on catalog list pages.

    The catalog pages (dictionary, opportunities, top assets, report health)
    share one ``#filterWorkspace`` dropdown that filters by ``workspace_name``.
    So we resolve a single name here, scanning the user's saved defaults across
    every active PowerBI source. Returns '' when nothing should be pre-selected.
    """
    if not user or not user.is_authenticated or org is None:
        return ''
    for entry in resolve_default_workspaces_for_org(user, org):
        if entry.get('workspace_name'):
            return entry['workspace_name']
    return ''


def resolve_default_workspaces_for_org(user, org) -> list:
    """Return a list of {'source_id', 'source_name', 'workspace_id', 'workspace_name',
    'workspace_count'} for every active source belonging to ``org`` that has
    workspaces. Used by the chatbot to know which workspace to scope to and
    when to ask.
    """
    out = []
    sources = IntegrationSource.objects.filter(
        organization=org,
        is_active=True,
        source_type='powerbi_fabric',
    )
    user_defaults = (getattr(user, 'default_workspaces', None) or {})
    for src in sources:
        workspaces = get_workspaces_for_source(src)
        if not workspaces:
            continue
        # Inline resolution to avoid an extra get_workspaces_for_source query
        # (which resolve_default_workspace would otherwise issue).
        ids = {w['id']: w['name'] for w in workspaces}
        if len(workspaces) == 1:
            wid = workspaces[0]['id']
        else:
            saved = user_defaults.get(str(src.id))
            if saved and saved in ids:
                wid = saved
            elif src.default_workspace_id and src.default_workspace_id in ids:
                wid = src.default_workspace_id
            else:
                wid = None
        out.append({
            'source_id': src.id,
            'source_name': src.name,
            'workspace_id': wid,
            'workspace_name': ids.get(wid) if wid else None,
            'workspace_count': len(workspaces),
            'workspaces': workspaces,
        })
    return out
