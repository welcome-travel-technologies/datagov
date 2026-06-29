"""
Management command: backfill_usage_identity

One-off repair for historical PowerBIReportUsage rows whose ``user_email`` is an
opaque AAD GUID (or bare UserKey) with no display name — the symptom that made
the Champions leaderboard show GUIDs instead of names.

Normal ETL runs only refresh the most recent ~3 months (windowed replace), so
older months keep whatever was loaded at the time. This command resolves those
stale identities WITHOUT any Microsoft Graph permission, using the same Power BI
token the ETL already holds:

  * GUID → email  : the modern usage 'Users' table exposes both 'Users'[UserGuid]
                    and the email in 'Users'[UserId].
  * email → name  : the workspace user list (GET /groups/{id}/users) carries
                    displayName + emailAddress.

It then relabels matching rows (GUID → email) and fills in display names. It
also fills display names for rows that already have an email but no name.

Safe by default: prints what WOULD change. Pass --apply to write.

Usage:
    python manage.py backfill_usage_identity              # dry run
    python manage.py backfill_usage_identity --apply      # write changes
    python manage.py backfill_usage_identity --source-id 3 --apply
"""
from __future__ import annotations

import os
import sys

from django.core.management.base import BaseCommand


def _norm_guid(g):
    return (g or '').strip().strip('{}').upper()


class Command(BaseCommand):
    help = ('Resolve historical GUID viewer identities in PowerBIReportUsage to '
            'real emails/names via the existing Power BI token (no Graph).')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Persist changes (default: dry run)')
        parser.add_argument('--source-id', type=int, default=None,
                            help='Only use this IntegrationSource id for lookups')

    def handle(self, *args, **opts):
        etl_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), '../../../../'))
        if etl_root not in sys.path:
            sys.path.insert(0, etl_root)

        from django.conf import settings
        from catalog.models import IntegrationSource, PowerBIReportUsage

        apply = opts['apply']
        target = 'PRODUCTION' if not settings.DEBUG else 'local'
        self.stdout.write(self.style.WARNING(
            f'\nDB target: {target}  (DEBUG={settings.DEBUG})   '
            f'mode: {"APPLY" if apply else "DRY-RUN"}'))

        guid_to_email, email_to_name = self._build_maps(opts['source_id'])
        self.stdout.write(
            f'\nLookup maps: {len(guid_to_email)} GUID→email, '
            f'{len(email_to_name)} email→name')
        if not guid_to_email and not email_to_name:
            self.stderr.write('No lookup data resolved — aborting.')
            return

        # Distinct current identities in the usage table.
        current = list(
            PowerBIReportUsage.objects
            .values_list('user_email', flat=True).distinct())

        relabel_plan = []   # (old_email_guid, new_email, name)
        name_only_plan = []  # (email, name) for rows that already have the email
        for cur in current:
            if not cur:
                continue
            if '@' in cur:
                # already an email — just see if a name is missing anywhere.
                name = email_to_name.get(cur.strip().lower())
                if name:
                    name_only_plan.append((cur, name))
                continue
            email = guid_to_email.get(_norm_guid(cur))
            if email:
                relabel_plan.append((cur, email, email_to_name.get(email.lower(), '')))

        self.stdout.write(
            f'\nWould relabel {len(relabel_plan)} GUID identities → email, '
            f'and top up names for {len(name_only_plan)} email identities.')
        for old, new, name in relabel_plan[:15]:
            self.stdout.write(f'  {old}  →  {new}  ({name or "—"})')
        if len(relabel_plan) > 15:
            self.stdout.write(f'  … +{len(relabel_plan) - 15} more')

        if not apply:
            self.stdout.write(self.style.WARNING(
                '\nDry run — no changes written. Re-run with --apply.'))
            return

        # Apply: relabel GUID rows, then top up missing names on email rows.
        n_rows = 0
        for old, new, name in relabel_plan:
            updated = (PowerBIReportUsage.objects
                       .filter(user_email=old)
                       .update(user_email=new, user_display_name=name))
            n_rows += updated
        n_named = 0
        for email, name in name_only_plan:
            n_named += (PowerBIReportUsage.objects
                        .filter(user_email=email)
                        .exclude(user_display_name=name)
                        .update(user_display_name=name))
        self.stdout.write(self.style.SUCCESS(
            f'\n✅ Relabelled {n_rows} rows (GUID→email); '
            f'set/updated display name on {n_named} rows.'))

    # ──────────────────────────────────────────────────────────────────────
    def _build_maps(self, source_id):
        """Return (guid_to_email, email_to_name) built from every workspace's
        usage dataset + workspace user list, using the existing Power BI token."""
        from catalog.models import IntegrationSource
        from etl.sources.registry import get_source
        from etl.sources.fabric.extract_usage import (
            _find_usage_datasets, _run_dax, _normalize_guid,
            _fetch_workspace_display_names,
        )
        import requests

        DAX_GUID_EMAIL = (
            "EVALUATE SELECTCOLUMNS('Users', "
            "\"UserGuid\", 'Users'[UserGuid], "
            "\"UserId\", 'Users'[UserId])"
        )

        guid_to_email: dict = {}
        email_to_name: dict = {}

        qs = IntegrationSource.objects.filter(
            is_active=True, source_type='powerbi_fabric')
        if source_id:
            qs = qs.filter(pk=source_id)

        for source in qs:
            try:
                src = get_source(source)
                headers = {'Authorization': f'Bearer {src._get_token()}'}
            except Exception as e:
                self.stderr.write(f'  [auth-fail] source #{source.pk}: {e}')
                continue
            for ws_id in (src.workspace_ids or []):
                # A workspace can carry several usage datasets (modern + legacy);
                # the GUID→email mapping lives on the modern one's 'Users'[UserId].
                for ds in _find_usage_datasets(headers, ws_id):
                    try:
                        for u in _run_dax(headers, ws_id, ds['id'], DAX_GUID_EMAIL):
                            g = _normalize_guid(u.get('[UserGuid]'))
                            email = (u.get('[UserId]') or '').strip()
                            if g and email and '@' in email:
                                guid_to_email[g] = email
                    except requests.HTTPError:
                        pass  # legacy dataset has no [UserId]; its rows had emails
                email_to_name.update(
                    _fetch_workspace_display_names(headers, ws_id))

        return guid_to_email, email_to_name
