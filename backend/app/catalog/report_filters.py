"""Draft / test report classification.

Some PowerBI reports are personal scratch work — sandboxes, test dashboards,
QA/demo/WIP drafts — that users create but that are not governed production
reports. They pollute the usage stats, dashboard counts and report listings,
so we exclude them everywhere reports are counted or listed. The single source
of truth for "does this name look like a draft?" lives here.

The heuristic is intentionally conservative. The distinctive stems
(``playgro``, ``sandbox``, ``scratch``, ``workshop``, ``learning space``)
match anywhere, but the short/ambiguous stems (``test``, ``qa``, ``demo``,
``draft`` …) only match as whole words via the ``(^|[^a-z0-9]) … ([^a-z0-9]|$)``
boundary, so "Testament", "Contest", "Protest", "Qatar", "Demography" and
"Draftsman" do NOT match.

Used from two kinds of call site:
  * Python-side, on a name string — :func:`is_draft_report_name`.
  * ORM-side, on a queryset — :func:`exclude_draft_reports` (reports-only
    querysets: PB_REPORT items or the usage table) and
    :func:`draft_report_q` (mixed querysets, e.g. the generic Item list).

The pattern carries no ``(?i)`` inline flag: Python compiles it with
``re.IGNORECASE`` and the ORM uses ``__iregex`` (Postgres ``~*``), which is
already case-insensitive.
"""
import re

from django.db.models import Q

# Keep this the ONE definition of the heuristic. Both the compiled Python
# regex and every ORM ``__iregex`` lookup below reference it.
DRAFT_REPORT_REGEX = (
    r'(playgro|sandbox|scratch|workshop|learning[ _]?space|'
    r'(^|[^a-z0-9])(test|testing|draft|qa|demo|wip|poc|sample|practice|dummy)'
    r'([^a-z0-9]|$))'
)

_DRAFT_RE = re.compile(DRAFT_REPORT_REGEX, re.IGNORECASE)


def is_draft_report_name(name) -> bool:
    """True if ``name`` looks like a personal draft/test/sandbox report."""
    return bool(name and _DRAFT_RE.search(name))


def exclude_draft_reports(qs, name_field: str = 'item_name'):
    """Drop draft-named rows from a queryset **already narrowed to reports**
    (PB_REPORT items, or the PowerBIReportUsage table via
    ``name_field='report_name'``)."""
    return qs.exclude(**{f'{name_field}__iregex': DRAFT_REPORT_REGEX})


def draft_report_q(name_field: str = 'item_name',
                   type_field: str = 'item_type') -> Q:
    """Q matching draft **reports** in a mixed-type queryset. Pass to
    ``qs.exclude(draft_report_q())`` on the generic Item list so only PB_REPORT
    rows are name-tested (Postgres short-circuits the regex for other types)."""
    return Q(**{type_field: 'PB_REPORT'}) & Q(**{f'{name_field}__iregex': DRAFT_REPORT_REGEX})


# NetworkNode.group values for the PowerBI report hierarchy (see NetworkNode
# docstring): a report and its exclusively-owned page/visual children.
_GRAPH_REPORT_GROUP = 'PB_REPORT'
_GRAPH_STRUCT_CHILD_GROUPS = frozenset({'PB_PAGE', 'PB_VISUAL'})


def strip_draft_reports_from_graph(nodes, links=None):
    """Remove draft-named report nodes — and, when ``links`` are supplied, the
    page/visual subtree exclusive to them — from an assembled lineage payload,
    dropping any link that references a removed node.

    Operates purely on the ``{id,label,group}`` node / ``{source,target}`` link
    lists the graph endpoints already build, so the many graph-traversal paths
    stay oblivious to the heuristic. Edges run upstream→downstream
    (…measure→visual→page→report), so a page/visual feeding a removed node is
    exclusive to it and gets pruned too (iterated to a fixpoint). Returns
    ``(nodes, links)``; ``links`` is ``[]`` when none were passed.
    """
    links = links or []
    remove = {n['id'] for n in nodes
              if n.get('group') == _GRAPH_REPORT_GROUP
              and is_draft_report_name(n.get('label'))}
    if not remove:
        return nodes, links
    group_by_id = {n['id']: n.get('group') for n in nodes}
    changed = True
    while changed:
        changed = False
        for link in links:
            src, tgt = link.get('source'), link.get('target')
            if (tgt in remove and src not in remove
                    and group_by_id.get(src) in _GRAPH_STRUCT_CHILD_GROUPS):
                remove.add(src)
                changed = True
    nodes = [n for n in nodes if n['id'] not in remove]
    links = [l for l in links
             if l.get('source') not in remove and l.get('target') not in remove]
    return nodes, links
