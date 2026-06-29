"""
Management command: diagnose_usage_identity

Read-only diagnostic for the Power BI usage "Champions"/leaderboard, which
currently shows opaque AAD GUIDs instead of names/emails. It answers one
question without requiring any extra Azure permissions:

    Can we still recover user names/emails from the Power BI usage dataset
    itself (via DAX), the way the legacy app did — or has Microsoft's modern
    usage model truly stripped that PII out?

Two independent phases, both strictly read-only:

  DB phase   (--db)   Scans the loaded PowerBIReportUsage table (production
                      when DEBUG=False) across ALL workspaces/reports and
                      classifies every viewer identity as email / GUID /
                      opaque, and whether a display name is present. Shows the
                      scale of the problem per report.

  Live phase (--live) Uses the existing Fabric service-principal creds (Power
                      BI scope only — NO Graph, NO new consent) to open each
                      workspace's usage dataset and reveal the REAL column
                      names on its 'Users' (and views) table via
                      `EVALUATE TOPN(...)`. If a UPN / name / email column is
                      still present, the fix is a 2-line DAX change; if not,
                      DAX alone cannot resolve names and we need Graph or a
                      manual mapping.

Usage:
    python manage.py diagnose_usage_identity            # both phases
    python manage.py diagnose_usage_identity --db       # DB scan only
    python manage.py diagnose_usage_identity --live     # live probe only
    python manage.py diagnose_usage_identity --source-id 3
"""
from __future__ import annotations

import os
import re
import sys

from django.core.management.base import BaseCommand

# AAD object id, e.g. F1727136-CF1B-4905-B81C-03C2444EFE32
GUID_RE = re.compile(
    r'^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-'
    r'[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$'
)

# 'Users' columns that would let us resolve a real identity via DAX. Matched
# case-insensitively against whatever the modern model actually exposes.
NAME_LIKE_COLS = {
    'userprincipalname', 'upn', 'email', 'useremail', 'mail',
    'displayname', 'fullname', 'name', 'username',
    'givenname', 'firstname', 'familyname', 'lastname', 'surname',
}


def _classify(email: str) -> str:
    """Bucket a stored user_email value by what kind of identity it is."""
    e = (email or '').strip()
    if not e:
        return 'empty'
    if '@' in e:
        return 'email'
    if GUID_RE.match(e):
        return 'guid'
    return 'opaque'  # bare UserKey or other non-resolvable token


class Command(BaseCommand):
    help = ('Read-only diagnostic: can Power BI usage names be recovered via '
            'DAX without extra permissions? Scans the loaded DB and probes the '
            'live usage dataset schema.')

    def add_arguments(self, parser):
        parser.add_argument('--db', action='store_true',
                            help='Run only the loaded-database scan')
        parser.add_argument('--live', action='store_true',
                            help='Run only the live Power BI schema probe')
        parser.add_argument('--source-id', type=int, default=None,
                            help='Probe only this IntegrationSource id (live phase)')

    # ──────────────────────────────────────────────────────────────────────
    def handle(self, *args, **opts):
        # Make the `etl` package importable (mirrors test_sources).
        etl_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), '../../../../'))
        if etl_root not in sys.path:
            sys.path.insert(0, etl_root)

        from django.conf import settings
        db_host = settings.DATABASES['default'].get('HOST', '?')
        target = 'PRODUCTION' if not settings.DEBUG else 'local'
        self.stdout.write(self.style.WARNING(
            f'\nDB target: {target}  (DEBUG={settings.DEBUG}, HOST={db_host})'))

        run_db = opts['db'] or not opts['live']
        run_live = opts['live'] or not opts['db']

        if run_db:
            self._phase_db()
        if run_live:
            self._phase_live(opts['source_id'])

    # ── DB phase ───────────────────────────────────────────────────────────
    def _phase_db(self):
        from catalog.models import PowerBIReportUsage
        from django.db.models import Sum

        self.stdout.write('\n' + '=' * 72)
        self.stdout.write(' DB PHASE — viewer identities currently loaded (all reports)')
        self.stdout.write('=' * 72)

        # One row per (workspace, report, viewer); aggregate views.
        rows = list(
            PowerBIReportUsage.objects
            .values('workspace_name', 'report_name', 'user_email', 'user_display_name')
            .annotate(views=Sum('view_count'))
        )
        if not rows:
            self.stdout.write('  (no PowerBIReportUsage rows loaded)')
            return

        # Per-report rollup + global identity tallies.
        per_report: dict[tuple, dict] = {}
        overall = {'email': 0, 'guid': 0, 'opaque': 0, 'empty': 0}
        named_identities = 0
        seen_identity: set[str] = set()

        for r in rows:
            key = (r['workspace_name'] or '—', r['report_name'] or '—')
            rep = per_report.setdefault(
                key, {'viewers': set(), 'kinds': {'email': 0, 'guid': 0,
                      'opaque': 0, 'empty': 0}, 'named': 0, 'views': 0})
            email = r['user_email'] or ''
            kind = _classify(email)
            rep['viewers'].add(email)
            rep['kinds'][kind] += 1
            rep['views'] += r['views'] or 0
            if (r['user_display_name'] or '').strip():
                rep['named'] += 1
            # global (dedupe identity across reports)
            if email not in seen_identity:
                seen_identity.add(email)
                overall[kind] += 1
                if (r['user_display_name'] or '').strip():
                    named_identities += 1

        # Per-report table (sorted by views desc).
        self.stdout.write(
            f'\n  {"workspace / report":52}{"viewers":>8}{"email":>7}'
            f'{"guid":>6}{"opaq":>6}{"named":>7}')
        self.stdout.write('  ' + '-' * 86)
        for (ws, rep_name), d in sorted(
                per_report.items(), key=lambda kv: -kv[1]['views']):
            label = f'{ws} / {rep_name}'
            if len(label) > 50:
                label = label[:49] + '…'
            self.stdout.write(
                f'  {label:52}{len(d["viewers"]):>8}{d["kinds"]["email"]:>7}'
                f'{d["kinds"]["guid"]:>6}{d["kinds"]["opaque"]:>6}{d["named"]:>7}')

        total_ids = len(seen_identity)
        self.stdout.write('\n  Distinct viewer identities across all reports: '
                          f'{total_ids}')
        for k in ('email', 'guid', 'opaque', 'empty'):
            n = overall[k]
            if n:
                pct = 100 * n / total_ids if total_ids else 0
                self.stdout.write(f'    {k:8}: {n:>5}  ({pct:4.1f}%)')
        self.stdout.write(f'    with a non-empty display name: {named_identities} '
                          f'({100*named_identities/total_ids if total_ids else 0:4.1f}%)')

        if overall['guid'] or overall['opaque']:
            self.stdout.write(self.style.WARNING(
                '\n  ⇒ Some/all viewers are stored as GUIDs/opaque keys with no '
                'name — this is exactly what the leaderboard renders.'))
        else:
            self.stdout.write(self.style.SUCCESS(
                '\n  ⇒ All loaded viewers already have email/name; the GUID issue '
                'may be limited to specific workspaces (check the live phase).'))

    # ── Live phase ───────────────────────────────────────────────────────────
    def _phase_live(self, source_id):
        import requests
        from catalog.models import IntegrationSource
        from etl.sources.registry import get_source
        from etl.sources.fabric.extract_usage import (
            _find_usage_datasets, _run_dax, USAGE_DS_NAMES,
        )

        self.stdout.write('\n' + '=' * 72)
        self.stdout.write(' LIVE PHASE — real usage-dataset schema (Power BI scope only)')
        self.stdout.write('=' * 72)

        qs = IntegrationSource.objects.filter(
            is_active=True, source_type='powerbi_fabric')
        if source_id:
            qs = qs.filter(pk=source_id)
        if not qs.exists():
            self.stdout.write('  (no active powerbi_fabric IntegrationSource found)')
            return

        for source in qs:
            self.stdout.write(f'\n  Source #{source.pk}  {source.name}  '
                              f'(org={source.organization_id})')
            try:
                src = get_source(source)
                token = src._get_token()
            except Exception as e:
                self.stderr.write(f'    [auth-fail] {e}')
                continue
            headers = {'Authorization': f'Bearer {token}'}
            ws_ids = src.workspace_ids or []
            if not ws_ids:
                self.stdout.write('    (no workspace_ids configured)')
                continue

            for ws_id in ws_ids:
                self._probe_workspace(requests, _find_usage_datasets, _run_dax,
                                      USAGE_DS_NAMES, headers, ws_id)

    def _probe_workspace(self, requests, _find_usage_datasets, _run_dax,
                         USAGE_DS_NAMES, headers, ws_id):
        self.stdout.write(f'\n    ── workspace {ws_id}')
        datasets = _find_usage_datasets(headers, ws_id)
        if not datasets:
            self.stdout.write(f'      no usage dataset ({" / ".join(USAGE_DS_NAMES)})')
            return

        # ── Why-empty instrument ────────────────────────────────────────────
        # A workspace can hold BOTH the modern 'Usage Metrics Report' and the
        # legacy 'Report Usage Metrics Model'. For each, show its fact table,
        # total row count, and date range:
        #   rows=0          → empty shell (auto-created, never populated)
        #   rows>0, old max → stale / retention (no recent views landed)
        #   rows>0, recent  → live data (this is the one we want)
        # The extractor keeps the FIRST dataset that returns rows, so this is
        # exactly what decides whether usage shows up for the workspace.
        probed = None
        for usage_ds in datasets:
            ds_id = usage_ds['id']
            flavour, fact = self._fact_flavour(requests, _run_dax, headers,
                                               ws_id, ds_id)
            n, dmin, dmax = self._fact_stats(requests, _run_dax, headers,
                                             ws_id, ds_id, fact)
            self.stdout.write(
                f'      • "{usage_ds.get("name")}" [{flavour}] '
                f'rows={"?" if n is None else n} '
                f'dates={dmin or "—"}..{dmax or "—"}  ({ds_id})')
            if probed is None and n:
                probed = (usage_ds, ds_id, fact, flavour)

        if probed is None:
            self.stdout.write(self.style.WARNING(
                '      ⇒ no usage dataset here has ANY rows — nothing to load '
                '(this is the empty case the dashboard shows as 0).'))
            return

        usage_ds, ds_id, fact, flavour = probed
        self.stdout.write(
            f'      ⇒ extractor uses "{usage_ds.get("name")}" ({flavour})')

        # The crux: dump the REAL 'Users' columns. TOPN(1) returns one row whose
        # keys are the fully-qualified column names — no INFO.* / Premium needed.
        users_cols = self._table_columns(requests, _run_dax, headers, ws_id, ds_id,
                                         "'Users'")
        if users_cols is None:
            self.stdout.write('      Users table   : not queryable')
            return
        self.stdout.write(f'      Users columns : {", ".join(users_cols) or "(empty)"}')

        # Dump real VALUES — a column named opaquely (UserId/UniqueUser) might
        # still CONTAIN an email/UPN, which would be a pure-DAX fix.
        try:
            for r in _run_dax(headers, ws_id, ds_id, "EVALUATE TOPN(3, 'Users')"):
                vals = '  '.join(f'{k.split("[",1)[-1].rstrip("]")}={v!r}'
                                 for k, v in r.items())
                self.stdout.write(f'        row: {vals}')
        except requests.HTTPError:
            pass

        # Which of those columns could resolve a real identity?
        name_cols = [c for c in users_cols
                     if c.split('[', 1)[-1].rstrip(']').lower() in NAME_LIKE_COLS]
        if name_cols:
            self.stdout.write(self.style.SUCCESS(
                f'      ✓ name/email columns present: {", ".join(name_cols)}'))
            self._sample_users(requests, _run_dax, headers, ws_id, ds_id, name_cols)
            self.stdout.write(self.style.SUCCESS(
                '      VERDICT (DAX): names ARE recoverable from the usage '
                'dataset — extend the modern DAX, no extra permissions.'))
        else:
            self.stdout.write(self.style.WARNING(
                '      ✗ no UPN/name/email column on Users — DAX alone CANNOT '
                'resolve names here.'))

        # Sample the real viewer GUIDs so we can measure how many a no-Graph
        # REST endpoint would actually resolve.
        sample_guids = self._sample_guids(requests, _run_dax, headers, ws_id, ds_id)
        # Fallback that needs NO Microsoft Graph: the Power BI REST user-listing
        # endpoints return displayName + emailAddress + graphId (= UserGuid),
        # using the same Power BI token we already hold.
        self._probe_user_apis(requests, headers, ws_id, sample_guids)

    def _sample_guids(self, requests, _run_dax, headers, ws_id, ds_id) -> set:
        from etl.sources.fabric.extract_usage import _normalize_guid
        try:
            rows = _run_dax(headers, ws_id, ds_id,
                            "EVALUATE TOPN(50, SELECTCOLUMNS('Users', "
                            "\"g\", 'Users'[UserGuid]))")
        except requests.HTTPError:
            return set()
        return {_normalize_guid(r.get('[g]')) for r in rows if r.get('[g]')} - {''}

    def _probe_user_apis(self, requests, headers, ws_id, sample_guids):
        """Test the no-Graph REST fallbacks and measure GUID→name coverage."""
        from etl.sources.fabric.extract_usage import PBI_BASE, _normalize_guid
        endpoints = [
            ('groups/{}/users      ', f'{PBI_BASE}/groups/{ws_id}/users'),
            ('admin/groups/{}/users', f'{PBI_BASE}/admin/groups/{ws_id}/users'),
        ]
        self.stdout.write('      identity via Power BI REST (no Graph, same token):')
        for label, url in endpoints:
            try:
                r = requests.get(url, headers=headers, timeout=30)
            except Exception as e:
                self.stdout.write(f'        {label}: error {e}')
                continue
            if r.status_code != 200:
                body = (r.text or '')[:110].replace('\n', ' ')
                self.stdout.write(f'        {label}: HTTP {r.status_code}  {body}')
                continue
            users = r.json().get('value', [])
            graphids = {_normalize_guid(u.get('graphId'))
                        for u in users if u.get('graphId')} - {''}
            n_email = sum(1 for u in users if u.get('emailAddress'))
            n_name = sum(1 for u in users if u.get('displayName'))
            matched = len(sample_guids & graphids) if sample_guids else 0
            self.stdout.write(
                f'        {label}: HTTP 200, {len(users)} users '
                f'(name={n_name}, email={n_email}, graphId={len(graphids)}); '
                f'resolves {matched}/{len(sample_guids)} sampled viewer GUIDs')
            for u in users[:2]:
                self.stdout.write(
                    f'          e.g. type={u.get("principalType")} '
                    f'name={u.get("displayName")!r} '
                    f'email={u.get("emailAddress")!r} graphId={u.get("graphId")}')
            if matched:
                self.stdout.write(self.style.SUCCESS(
                    f'        ✓ {label.strip()} resolves real viewer GUIDs with '
                    f'the EXISTING token — no Graph, no new consent.'))

    def _table_columns(self, requests, _run_dax, headers, ws_id, ds_id, table):
        """Return the fully-qualified column names of `table`, or None."""
        try:
            rows = _run_dax(headers, ws_id, ds_id, f'EVALUATE TOPN(1, {table})')
        except requests.HTTPError:
            return None
        return list(rows[0].keys()) if rows else []

    def _sample_users(self, requests, _run_dax, headers, ws_id, ds_id, name_cols):
        """Print a couple of sample identities to prove they resolve (internal
        diagnostic — names are expected, not leaked externally)."""
        cols = ', '.join(f'"{c.split("[",1)[-1].rstrip("]")}", {c}'
                         for c in name_cols)
        try:
            rows = _run_dax(headers, ws_id, ds_id,
                            f"EVALUATE TOPN(3, SELECTCOLUMNS('Users', {cols}))")
        except requests.HTTPError:
            return
        for r in rows[:3]:
            vals = '  '.join(f'{k.strip("[]")}={v!r}' for k, v in r.items())
            self.stdout.write(f'        e.g. {vals}')

    def _fact_flavour(self, requests, _run_dax, headers, ws_id, ds_id):
        """Return (flavour, fact_table_expr) by probing which fact table the
        usage dataset exposes. TOPN(1) on an EMPTY table still succeeds (returns
        no rows), so this detects the schema even when the table holds no data."""
        for cand in ("'Report views'", "'Views'"):
            try:
                _run_dax(headers, ws_id, ds_id, f'EVALUATE TOPN(1, {cand})')
                return ('modern' if cand == "'Report views'" else 'legacy'), cand
            except requests.HTTPError:
                continue
        return 'unknown', None

    def _fact_stats(self, requests, _run_dax, headers, ws_id, ds_id, fact):
        """Total row count + min/max [Date] of the fact table — the signal that
        separates 'empty shell' (rows=0) from 'stale' (old max date) from
        'live' (recent max date). Returns (count|None, min_date|None, max|None);
        None when the fact table can't be queried."""
        if not fact:
            return None, None, None
        col = f'{fact}[Date]'
        dax = (f'EVALUATE ROW("n", COUNTROWS({fact}), '
               f'"mn", MIN({col}), "mx", MAX({col}))')
        try:
            rows = _run_dax(headers, ws_id, ds_id, dax)
        except requests.HTTPError:
            return None, None, None
        if not rows:
            return 0, None, None
        r = rows[0]
        n = r.get('[n]')
        to_d = lambda v: (str(v)[:10] if v else None)
        return (int(n) if n is not None else 0), to_d(r.get('[mn]')), to_d(r.get('[mx]'))
