"""
PowerBI assistant provider.

Uniform contract (see ``assistant/__init__.py``):
  scope_options(org)                     -> workspaces available to scope on
  build_context(org, *, client, scope_ids) -> front-loaded measure+report catalog
  build_tools(org, *, client)            -> [schema-bundle, run-DAX]

The agent answers PowerBI questions WITHOUT searching: the measure and
report catalog is dumped into the system prompt, ``get_pb_measure_schema``
gives one-shot depth on a specific measure (ids, DAX, tables, columns,
relationships), and ``powerbi_run_dax_query`` runs the DAX the agent writes.
"""
from __future__ import annotations

from .cache import cached_context, scope_key


def scope_options(org) -> list[dict]:
    """Workspaces selectable for the PowerBI context, across the org's
    active PowerBI sources. Returns ``[{"id","name"}]``."""
    from ...models import IntegrationSource
    from ...services.workspaces import get_workspaces_for_source

    out: list[dict] = []
    seen: set = set()
    sources = IntegrationSource.objects.filter(
        organization=org, source_type='powerbi_fabric', is_active=True,
    )
    for src in sources:
        try:
            workspaces = get_workspaces_for_source(src)
        except Exception:
            continue
        for w in workspaces:
            wid = w.get('id')
            if not wid or wid in seen:
                continue
            seen.add(wid)
            out.append({'id': wid, 'name': w.get('name') or wid})
    return out


def build_context(org, *, client=None, scope_ids=None) -> str:
    """Front-loaded PowerBI catalog: every measure (name + description) and
    report for the selected workspaces. Cached per org + scope."""
    org_id = getattr(org, 'id', 'x')
    key = f'asst_ctx_pb_{org_id}_{scope_key(scope_ids)}'
    return cached_context(key, lambda: _build(scope_ids))


def _build(scope_ids) -> str:
    from ...models import Item

    m_qs = Item.objects.filter(
        deleted=False, item_type='PB_MEASURE', service='powerbi',
    )
    if scope_ids:
        m_qs = m_qs.filter(workspace_id__in=list(scope_ids))
    m_qs = m_qs.order_by('-connected_reports', '-connected_visuals', 'item_name')

    # Group-first: the same measure name recurs across datasets as several Items
    # that all share ONE ItemGroup (one owner / status / governance). Collapse to
    # one row PER GROUP and surface the group-level owner + status inline, so
    # ownership questions ("who owns X", "list KPIs with an owner") are answerable
    # straight from this listing with NO tool call. Ungrouped legacy rows fall
    # back to name-dedup. Owner comes via the ItemGroup join; the status mirror
    # column is identical across a group's items.
    seen: set = set()
    measures: list[dict] = []
    for m in m_qs.values('item_name', 'description', 'item_group_id', 'status',
                         'item_group__ownership_person__name'):
        name = m['item_name']
        gkey = m['item_group_id'] or f'name::{name}'
        if not name or gkey in seen:
            continue
        seen.add(gkey)
        measures.append({
            'item_name': name,
            'description': m['description'],
            'owner': m['item_group__ownership_person__name'],
            'status': m['status'],
        })

    r_qs = Item.objects.filter(
        deleted=False, item_type='PB_REPORT', service='powerbi',
    )
    if scope_ids:
        r_qs = r_qs.filter(workspace_id__in=list(scope_ids))
    reports = list(
        r_qs.order_by('item_name').values('item_name', 'description', 'workspace_name')
    )

    # Tables: a compact name inventory (deduped). The agent used to see only
    # measures + reports, so for table-level questions it had to hunt for the
    # table name — listing them here lets it resolve in one call.
    t_qs = Item.objects.filter(
        deleted=False, item_type='PB_TABLE', service='powerbi',
    )
    if scope_ids:
        t_qs = t_qs.filter(workspace_id__in=list(scope_ids))
    seen_t: set = set()
    tables: list[str] = []
    for t in t_qs.order_by('-connected_measures', 'item_name').values_list('item_name', flat=True):
        if t and t not in seen_t:
            seen_t.add(t)
            tables.append(t)

    if not measures and not reports and not tables:
        return ''

    lines = [
        '\n\n## PowerBI catalog (authoritative — the full measure & report '
        'list is here; do NOT search the catalog)\n'
    ]
    lines.append(
        f'### Measures ({len(measures)}) — one row per measure GROUP; '
        '`owner:` and status are group-level governance (answer "who owns X" / '
        '"which KPIs have an owner / need attention" straight from here, no tool '
        'call). Each name is ONE governed measure even if it spans datasets.')
    # Descriptions are ~68% of the whole context; a one-line gloss is enough to
    # recognise a measure here — full detail comes from get_pb_item_details. Cap
    # each so the front-loaded catalog stays small (faster round-trips, so the
    # tool-call budget's graceful finalize fires before the hard timeout).
    _DESC_CAP = 100
    _NOTABLE_STATUS = {'VERIFIED', 'ATTENTION', 'DELETED'}
    for m in measures:
        desc = (m['description'] or '').strip().replace('\n', ' ')
        if len(desc) > _DESC_CAP:
            desc = desc[:_DESC_CAP - 1].rstrip() + '…'
        line = f'- **{m["item_name"]}**' + (f' — {desc}' if desc else '')
        meta = []
        if m['owner']:
            meta.append(f'owner: {m["owner"]}')
        if m['status'] in _NOTABLE_STATUS:
            meta.append(m['status'].lower())
        if meta:
            line += '  ·  ' + ' · '.join(meta)
        lines.append(line)
    lines.append('')
    lines.append(f'### Reports ({len(reports)})')
    for r in reports:
        desc = (r['description'] or '').strip().replace('\n', ' ')
        ws = (r['workspace_name'] or '').strip()
        head = f'- **{r["item_name"]}**'
        if ws:
            head += f' ({ws})'
        lines.append(head + (f' — {desc}' if desc else ''))
    if tables:
        lines.append('')
        lines.append(
            f'### Tables ({len(tables)}) — for table-level questions ("which '
            "measures use table X / where is table X used\"), call "
            'get_pb_item_details(name) with the exact table name below; it '
            "returns the table's columns plus the measures and reports that use it."
        )
        lines.append(', '.join(f'**{t}**' for t in tables))
    return '\n'.join(lines) + '\n'


def build_tools(org, *, client=None) -> list:
    """The PowerBI item profiler ``get_pb_item_details`` and the usage-analytics
    rollup ``get_pb_usage_analytics`` (both catalog-only, always) plus the live
    ``powerbi_run_dax_query`` tool when a client is present. The profiler returns
    ONE item's full DAX bundle / columns / ownership / where-used; the analytics
    tool returns the cross-cutting measure↔report usage map (which measures feed
    a report, which reports use a measure, top/unused rankings).
    """
    from ..analytics import get_pb_usage_analytics
    from ..lineage import get_pb_item_details
    tools = [get_pb_item_details, get_pb_usage_analytics]
    if client is not None:
        from ...powerbi_tools import make_powerbi_tools
        for tool in make_powerbi_tools(client):
            if tool.__name__ == 'powerbi_run_dax_query':
                tools.append(tool)
    return tools
