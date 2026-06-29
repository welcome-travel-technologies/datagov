"""
Aggregate "analyst" tool for the chatbot — a cross-cutting rollup the
per-item profiler can't give.

The item profiler (``get_pb_item_details`` / ``get_dbt_item_details``) goes
DEEP on ONE item. This one goes WIDE across the whole catalog:

  • ``get_pb_usage_analytics`` — the PowerBI measure ↔ report usage map.
    "which measures are used in report X", "which reports use measure Y",
    "top metrics by report coverage", "what's unused". Built straight from
    each measure's precomputed ``connected_reports_json`` (its downstream
    report list) — no lineage-graph walk.

Returns ONE rich Markdown block meant to be dropped into the model's context
and asked follow-up questions against. Pure read-only catalog ops.
"""
from collections import Counter

from ..models import Item

# Shared scope filters.
_PB_MEASURE = dict(deleted=False, item_type='PB_MEASURE', service='powerbi')
_PB_REPORT = dict(deleted=False, item_type='PB_REPORT', service='powerbi')

# Caps so a big catalog can't blow up the model's context window.
_LIST_CAP = 12       # report names shown inline per measure (and vice-versa)
_ALL_CAP = 200       # rows in the full alphabetical measure index


def _norm(s) -> str:
    return (s or '').strip()


def _group_key(m) -> str:
    """Measure-group collapse key: curated group id, else a name-based fallback."""
    return m['item_group_id'] or f"name::{(m['item_name'] or '').lower()}"


def _report_names(crj) -> list[str]:
    """Distinct report names from an Item.connected_reports_json value."""
    out: list[str] = []
    seen: set[str] = set()
    for e in (crj or []):
        if isinstance(e, dict):
            name = _norm(e.get('name'))
            if name and name.lower() not in seen:
                seen.add(name.lower())
                out.append(name)
    return out


# ---------------------------------------------------------------------------
# PowerBI usage analytics
# ---------------------------------------------------------------------------

def get_pb_usage_analytics(report_name: str = '', measure_name: str = '',
                           workspace: str = '', top: int = 25) -> str:
    """Aggregate PowerBI measure↔report usage analytics — the "which measures
    feed which reports" map plus top / most-shared / unused rankings.

    THREE modes, chosen automatically from the arguments:
      • ``report_name`` set  → every MEASURE used in that report (with its
        dataset and visual count), the report's pages/visuals and owner.
        Answers "which measures are used in report X?".
      • ``measure_name`` set → every REPORT that uses that measure, how widely
        it is shared, its datasets, owner and status. Answers "where is measure
        Y used / which reports show it?".
      • neither (OVERVIEW) → catalog-wide usage analytics: totals, the TOP
        measures by report coverage (and the reports they feed), the TOP
        reports by number of distinct measures, the usage distribution, the
        UNUSED measures, and a full alphabetical measure index. Answers "top
        metrics", "most-used KPIs", "which reports are measure-heavy", "what's
        unused".

    ``workspace`` — optional workspace-name substring applied in every mode.
    ``top`` — rows per ranking (default 25, capped at 100).

    Source: each measure's precomputed ``connected_reports_json`` (its
    downstream report list) — no lineage walk. For the FULL profile of ONE
    measure or report (DAX, columns, relationships, lineage) call
    ``get_pb_item_details`` instead; this tool is for the cross-cutting rollups.
    """
    top = max(1, min(int(top or 25), 100))
    ws = _norm(workspace)

    def measures_qs():
        qs = Item.objects.filter(**_PB_MEASURE)
        return qs.filter(workspace_name__icontains=ws) if ws else qs

    if _norm(report_name):
        return _report_mode(_norm(report_name), ws, measures_qs)
    if _norm(measure_name):
        return _measure_mode(_norm(measure_name), ws, measures_qs)
    return _overview_mode(ws, measures_qs, top)


def _report_mode(report_name, ws, measures_qs) -> str:
    rqs = Item.objects.filter(**_PB_REPORT, item_name__icontains=report_name)
    if ws:
        rqs = rqs.filter(workspace_name__icontains=ws)
    reports = list(rqs.values(
        'item_id', 'item_name', 'workspace_name', 'connected_report_pages',
        'connected_visuals', 'description', 'item_group__ownership_person__name',
    )[:25])
    if not reports:
        return (f"No PowerBI report matches '{report_name}'"
                + (f' in workspace ~{ws!r}' if ws else '') + '.')

    target_lc = report_name.lower()
    exact = [r for r in reports if (r['item_name'] or '').lower() == target_lc]
    if len(reports) > 1 and not exact:
        rows = '\n'.join(
            f"- **{r['item_name']}** ({r['workspace_name'] or '?'})" for r in reports[:15]
        )
        return (f"'{report_name}' matches {len(reports)} reports — present this list "
                f"and ask the user which one, then re-run with the chosen name:\n{rows}")
    report = exact[0] if exact else reports[0]

    rid, rname_lc = report['item_id'], (report['item_name'] or '').lower()
    used = []  # (measure_name, dataset_name, connected_visuals)
    seen: set = set()
    for m in measures_qs().values(
        'item_name', 'dataset_name', 'connected_visuals',
        'connected_reports_json', 'item_group_id',
    ):
        crj = m['connected_reports_json'] or []
        hit = any(
            isinstance(e, dict) and (
                str(e.get('id')) == str(rid)
                or _norm(e.get('name')).lower() == rname_lc
            )
            for e in crj
        )
        if not hit:
            continue
        gkey = _group_key(m)
        if gkey in seen:
            continue
        seen.add(gkey)
        used.append((m['item_name'], m['dataset_name'] or '?', m['connected_visuals'] or 0))
    used.sort(key=lambda t: (-t[2], (t[0] or '').lower()))

    lines = [f"# Report usage: **{report['item_name']}**\n"]
    meta = [f"workspace: {report['workspace_name'] or '?'}",
            f"{report['connected_report_pages'] or 0} pages",
            f"{report['connected_visuals'] or 0} visuals"]
    owner = report['item_group__ownership_person__name']
    if owner:
        meta.append(f"owner: {owner}")
    lines.append('- ' + '  ·  '.join(meta))
    desc = _norm(report['description'])
    if desc:
        lines.append(f"- **Description:** {desc[:400]}")

    lines.append(f"\n## Measures used in this report ({len(used)})")
    if not used:
        lines.append("_No measures are recorded as feeding this report "
                     "(it may use only columns / fields, or usage is not yet computed)._")
    else:
        lines.append("| Measure | Dataset | Visuals (total) |")
        lines.append("| --- | --- | --- |")
        for name, ds, vis in used:
            lines.append(f"| {name} | {ds} | {vis} |")
        lines.append("\n_Visuals is each measure's total across ALL reports. For one "
                     "measure's full definition call `get_pb_item_details(name)`._")
    return '\n'.join(lines)


def _measure_mode(measure_name, ws, measures_qs) -> str:
    distinct = sorted({
        n for n in measures_qs()
        .filter(item_name__icontains=measure_name)
        .values_list('item_name', flat=True) if n
    })
    if not distinct:
        return (f"No PowerBI measure matches '{measure_name}'"
                + (f' in workspace ~{ws!r}' if ws else '') + '.')
    target_lc = measure_name.lower()
    exact = [n for n in distinct if n.lower() == target_lc]
    if len(distinct) > 1 and not exact:
        rows = '\n'.join(f"- **{n}**" for n in distinct[:20])
        more = '' if len(distinct) <= 20 else f"\n…and {len(distinct) - 20} more."
        return (f"'{measure_name}' matches {len(distinct)} measures — present this list "
                f"and ask the user which one:\n{rows}{more}")
    target = exact[0] if exact else distinct[0]

    insts = list(measures_qs().filter(item_name=target).values(
        'dataset_name', 'workspace_name', 'status', 'connected_visuals',
        'connected_reports_json', 'is_unused', 'item_group__ownership_person__name',
    ))
    reports: dict = {}            # report name (lower) -> display name
    datasets: set = set()
    owner = None
    statuses: set = set()
    visuals = 0
    any_unused = True
    for m in insts:
        for rn in _report_names(m['connected_reports_json']):
            reports.setdefault(rn.lower(), rn)
        if m['dataset_name']:
            datasets.add(m['dataset_name'])
        owner = owner or m['item_group__ownership_person__name']
        if m['status']:
            statuses.add(m['status'])
        visuals = max(visuals, m['connected_visuals'] or 0)
        any_unused = any_unused and bool(m['is_unused'])

    report_list = sorted(reports.values(), key=str.lower)
    lines = [f"# Measure usage: **{target}**\n"]
    meta = [f"used in {len(report_list)} report(s)", f"{visuals} visuals (max)"]
    if owner:
        meta.append(f"owner: {owner}")
    if statuses:
        meta.append('status: ' + '/'.join(sorted(s.lower() for s in statuses)))
    if any_unused:
        meta.append('⚠️ flagged UNUSED')
    lines.append('- ' + '  ·  '.join(meta))
    if datasets:
        lines.append(f"- **Datasets ({len(datasets)}):** " + ', '.join(sorted(datasets)))

    lines.append(f"\n## Reports using this measure ({len(report_list)})")
    if not report_list:
        lines.append("_This measure does not feed any report (unused)._")
    else:
        for rn in report_list:
            lines.append(f"- {rn}")
    lines.append("\n_For this measure's DAX, tables and relationships call "
                 "`get_pb_item_details(name)`._")
    return '\n'.join(lines)


def _overview_mode(ws, measures_qs, top) -> str:
    measures = list(measures_qs().values(
        'item_name', 'dataset_name', 'connected_visuals',
        'connected_reports_json', 'item_group_id',
    ))
    if not measures:
        return ('No PowerBI measures in the catalog'
                + (f' for workspace ~{ws!r}' if ws else '') + '.')

    # Collapse to ONE row per measure GROUP (a name that spans datasets counts
    # once); union the report sets across the group's instances.
    groups: dict = {}
    for m in measures:
        gkey = _group_key(m)
        g = groups.setdefault(gkey, {
            'name': m['item_name'], 'reports': set(), 'visuals': 0, 'datasets': set(),
        })
        for rn in _report_names(m['connected_reports_json']):
            g['reports'].add(rn)
        g['visuals'] = max(g['visuals'], m['connected_visuals'] or 0)
        if m['dataset_name']:
            g['datasets'].add(m['dataset_name'])

    ordered = sorted(groups.values(), key=lambda g: (-len(g['reports']), -g['visuals'],
                                                     (g['name'] or '').lower()))
    n_measures = len(groups)
    unused = [g for g in ordered if not g['reports']]

    # Report → distinct-measure count (invert the union sets).
    report_measures: Counter = Counter()
    for g in groups.values():
        for rn in g['reports']:
            report_measures[rn] += 1

    rqs = Item.objects.filter(**_PB_REPORT)
    if ws:
        rqs = rqs.filter(workspace_name__icontains=ws)
    n_reports = rqs.count()

    def _pb_count(item_type):
        qs = Item.objects.filter(deleted=False, item_type=item_type, service='powerbi')
        return (qs.filter(workspace_name__icontains=ws) if ws else qs).count()

    scope = f" — workspace ~**{ws}**" if ws else ''
    lines = [f"# PowerBI usage analytics{scope}\n"]
    lines.append(
        f"- **Totals:** {n_measures:,} measures (grouped) · {n_reports:,} reports · "
        f"{_pb_count('PB_TABLE'):,} tables · {_pb_count('PB_COLUMN'):,} columns")
    used_n = n_measures - len(unused)
    lines.append(
        f"- **Coverage:** {used_n:,} measures feed at least one report; "
        f"{len(unused):,} are unused (0 reports).")

    # Usage distribution.
    buckets = Counter()
    for g in ordered:
        c = len(g['reports'])
        key = '0' if c == 0 else '1' if c == 1 else '2–4' if c <= 4 else '5–9' if c <= 9 else '10+'
        buckets[key] += 1
    dist = ', '.join(f"{buckets[k]} in {k}" for k in ('0', '1', '2–4', '5–9', '10+') if buckets[k])
    if dist:
        lines.append(f"- **Reports-per-measure spread:** {dist}.")

    # Top measures by report coverage.
    lines.append(f"\n## Top {min(top, n_measures)} measures by report coverage")
    lines.append("| # | Measure | Reports | Visuals | Used in |")
    lines.append("| --- | --- | --- | --- | --- |")
    for i, g in enumerate(ordered[:top], 1):
        names = sorted(g['reports'], key=str.lower)
        shown = ', '.join(names[:_LIST_CAP])
        if len(names) > _LIST_CAP:
            shown += f" …(+{len(names) - _LIST_CAP})"
        lines.append(f"| {i} | {g['name']} | {len(g['reports'])} | {g['visuals']} | {shown or '—'} |")

    # Top reports by distinct measure count.
    if report_measures:
        lines.append(f"\n## Top {min(top, len(report_measures))} reports by distinct measures used")
        lines.append("| # | Report | Distinct measures |")
        lines.append("| --- | --- | --- |")
        for i, (rn, c) in enumerate(report_measures.most_common(top), 1):
            lines.append(f"| {i} | {rn} | {c} |")

    # Unused measures.
    if unused:
        names = sorted((g['name'] for g in unused), key=str.lower)
        shown = ', '.join(names[:_ALL_CAP])
        if len(names) > _ALL_CAP:
            shown += f" …(+{len(names) - _ALL_CAP} more)"
        lines.append(f"\n## Unused measures ({len(unused)}) — feed no report")
        lines.append(shown)

    # Full alphabetical index (the "all metrics" ask).
    alpha = sorted(ordered, key=lambda g: (g['name'] or '').lower())
    lines.append(f"\n## All measures ({n_measures}) — alphabetical, with report count")
    rows = [f"{g['name']} ({len(g['reports'])})" for g in alpha[:_ALL_CAP]]
    suffix = '' if n_measures <= _ALL_CAP else f"  …(+{n_measures - _ALL_CAP} more)"
    lines.append(', '.join(rows) + suffix)
    return '\n'.join(lines)
