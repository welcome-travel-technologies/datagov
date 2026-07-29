"""
Management command: dedupe_data_persons

Repairs the two ways a person's governance identity goes wrong:

  * DUPLICATES — the same human as two ``DataPerson`` rows, which is why an
    Owner / Steward dropdown could list a name twice. Merged (losslessly) by
    :func:`catalog.people_dedupe.dedupe_data_persons`; this command is only the
    CLI wrapper, so the migration and the operator run identical logic.
  * THE IDENTITY GAP — rows and logins that never got joined. Printed first
    because it is the usual *cause* of a duplicate: a login-less row for someone
    who later got an account is exactly what the old upsert failed to find.

Safe by default: prints the clusters it would merge and writes nothing.
Pass --apply to perform the merge.

Usage:
    python manage.py dedupe_data_persons              # dry run, every org
    python manage.py dedupe_data_persons --apply      # perform the merge
    python manage.py dedupe_data_persons --org 1      # scope the gap report
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from catalog.access import unlinked_people_report
from catalog.models import DataPerson, GovernanceTask, ItemGroup, Organization
from catalog.people_dedupe import dedupe_data_persons

# Long lists are noise in a terminal; the counts above them are the signal.
PREVIEW = 20


class Command(BaseCommand):
    help = ('Report (and with --apply, merge) duplicate DataPerson rows, plus the '
            'identity gap between DataPersons and logins.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Perform the merge (default: dry run)')
        parser.add_argument('--org', type=int, default=None,
                            help='Organization id for the identity-gap report '
                                 '(default: every organization)')

    def handle(self, *args, **opts):
        apply = opts['apply']

        self.stdout.write(self.style.WARNING(
            f'mode: {"APPLY" if apply else "DRY-RUN"}'))

        if opts['org'] is not None:
            orgs = list(Organization.objects.filter(pk=opts['org']))
            if not orgs:
                # CommandError, not a message: a typo'd --org must not exit 0 and
                # look to a caller like a clean run that found nothing.
                raise CommandError(f"Organization id={opts['org']} not found.")
        else:
            orgs = list(Organization.objects.order_by('id'))
            if not orgs:
                self.stdout.write(self.style.WARNING(
                    'No Organization rows exist — skipping the identity-gap report.'))

        self._report_identity_gap(orgs, scoped=opts['org'] is not None)

        self.stdout.write('\nDuplicate DataPerson rows')
        self.stdout.write('=' * 60)
        # The scan is catalog-wide even with --org: a cluster is linked by shared
        # login as well as by name-within-org, so two rows can straddle orgs and
        # merging only one side would leave the other half of the pair behind.
        if opts['org'] is not None:
            self.stdout.write(
                '  (--org scopes the report above only; the merge scan is catalog-wide)')

        # All-or-nothing: a merge repoints owners, stewards and task assignees
        # BEFORE deleting the rows it merged away, so a failure part-way through
        # (a uniqueness conflict on a survivor, a lost connection) would strand
        # references against rows that were never merged. A dry run writes
        # nothing, so the transaction costs it nothing either.
        with transaction.atomic():
            summary = dedupe_data_persons(
                DataPerson, ItemGroup, GovernanceTask, apply=apply, log=self.stdout.write,
            )
        if not summary['clusters']:
            self.stdout.write('  No duplicates found.')

        counts = ', '.join(f'{k}={v}' for k, v in summary.items())
        verb = 'Merged' if apply else 'Would merge'
        self.stdout.write('')
        self.stdout.write((self.style.SUCCESS if apply else self.style.NOTICE)(
            f'{verb}: {counts}'))
        if summary['conflicts']:
            # Renames are the deliberate non-merge, so say it plainly rather than
            # let 'conflicts' read as a failure.
            self.stdout.write(self.style.WARNING(
                f'  {summary["conflicts"]} cluster(s) had the same name on different '
                f'logins — renamed to stay distinct, never merged.'))
        if not apply:
            self.stdout.write(self.style.WARNING(
                '\n*** DRY RUN — NOTHING WAS WRITTEN. Re-run with --apply to merge. ***'))

    # ──────────────────────────────────────────────────────────────────────
    def _report_identity_gap(self, orgs, scoped):
        """Who holds tasks nobody can see, and who has no governance identity."""
        self.stdout.write('\nIdentity gap')
        self.stdout.write('=' * 60)

        for org in orgs:
            report = unlinked_people_report(org)
            people = report['unlinked_people']
            members = report['members_without_person']
            self.stdout.write(f'\n{org.name} (id={org.id})')

            self.stdout.write(
                f'  {len(people)} DataPerson(s) with no linked login — assignable, '
                f'but their tasks appear in nobody\'s "My tasks"')
            for p in people[:PREVIEW]:
                roles = ','.join(
                    r for r, on in (('owner', p['is_owner']), ('steward', p['is_steward'])) if on
                ) or '—'
                self.stdout.write(f'    #{p["id"]} {p["name"]} [{roles}]')
            if len(people) > PREVIEW:
                self.stdout.write(f'    … +{len(people) - PREVIEW} more')

            self.stdout.write(
                f'  {len(members)} org member(s) with no DataPerson — they can sign in '
                f'but own nothing')
            for m in members[:PREVIEW]:
                self.stdout.write(f'    user #{m["user_id"]} {m["email"]}')
            if len(members) > PREVIEW:
                self.stdout.write(f'    … +{len(members) - PREVIEW} more')

            if people or members:
                self.stdout.write(
                    '  Fix: edit the member in Org Settings — it adopts a login-less '
                    'namesake instead of creating a twin.')

        # unlinked_people_report is org-scoped, so orgless rows are invisible to
        # every run of it — yet they still show in dropdowns and hold tasks.
        if not scoped:
            orphans = DataPerson.objects.filter(organization__isnull=True).count()
            if orphans:
                self.stdout.write(self.style.WARNING(
                    f'\n{orphans} DataPerson(s) belong to no organization — invisible '
                    f'to every per-org report above. Set their organization.'))
