"""Run the governance task sweep from the shell.

Same reconcile as the Task Manager's "Generate tasks" button
(``catalog.governance_tasks.generate_tasks``), for cron and one-off runs:

    python manage.py generate_governance_tasks --dry-run
    python manage.py generate_governance_tasks --org 1 \
        --reasons UNVERIFIED,NO_CATEGORY --confirm-broad
    python manage.py generate_governance_tasks --confirm-broad --notify

Slack stays quiet unless --notify is passed. The sweep is idempotent and meant
to be re-run freely, so an unasked-for digest on every run would train people to
ignore the channel; the UI's button opts in, a shell run has a human watching
the output already.
"""
from django.core.management.base import BaseCommand, CommandError

from catalog.governance_tasks import (
    DEFAULT_KIND_SCOPE, KIND_SCOPES, REASON_ORDER, generate_tasks, reason_label,
)
from catalog.models import Organization

# The counts generate_tasks returns per reason, in table order.
COUNT_COLUMNS = ('target', 'created', 'reassigned', 'closed', 'unassigned')

_LABEL_WIDTH = 16
_COL_WIDTH = 12


class Command(BaseCommand):
    help = (
        'Reconcile governance tasks against the catalog: create what is missing, '
        're-resolve assignees on open tasks, and close tasks whose gap is fixed. '
        'Defaults to every organization and every reason. Use --dry-run to preview.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--org', type=int, default=None,
            help='Organization id to sweep. Defaults to every organization.',
        )
        parser.add_argument(
            '--reasons', type=str, default=None,
            help='Comma-separated reasons to sweep, e.g. UNVERIFIED,NO_CATEGORY. '
                 f"Valid: {', '.join(REASON_ORDER)}. Defaults to all.",
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Print the same counts without writing anything.',
        )
        parser.add_argument(
            '--require-assignee', action='store_true',
            help='Skip groups missing the role required by their reason instead '
                 'of creating an unassigned task for them.',
        )
        parser.add_argument(
            '--notify', action='store_true',
            help='Send the Slack digest (one message per organization). Off by default.',
        )
        parser.add_argument(
            '--kind-scope', type=str, default=DEFAULT_KIND_SCOPE,
            choices=sorted(KIND_SCOPES),
            help='Which asset kinds to sweep. Default "all" so Category, '
                 'Attention, and To Be Deleted cover every asset kind; '
                 'Unverified remains measure-only. The default can be very '
                 'high volume — preview it with --dry-run first.',
        )
        parser.add_argument(
            '--confirm-broad', action='store_true',
            help='Required to apply a singleton/all sweep after reviewing its '
                 '--dry-run output. Has no effect on measure-only runs.',
        )

    def handle(self, *args, **options):
        reasons = self._parse_reasons(options['reasons'])
        dry_run = options['dry_run']
        notify = options['notify']

        if options['org'] is not None:
            org = Organization.objects.filter(id=options['org']).first()
            if org is None:
                raise CommandError(f"No organization with id {options['org']}")
            orgs = [org]
        else:
            orgs = list(Organization.objects.order_by('id'))
            if not orgs:
                raise CommandError('No Organization rows exist — nothing to sweep.')

        kind_scope = options['kind_scope']
        broad_scope = kind_scope in ('singleton', 'all')
        if broad_scope and not dry_run and not options['confirm_broad']:
            raise CommandError(
                'Refusing to apply a broad governance sweep without '
                '--confirm-broad. Run the same command with --dry-run, review '
                'the counts, then repeat with --confirm-broad.'
            )
        self.stdout.write(
            f"Reasons: {', '.join(reasons or REASON_ORDER)} | "
            f"scope: {KIND_SCOPES[kind_scope]['label']} | "
            f"organizations: {len(orgs)} | "
            f"require_assignee={options['require_assignee']}"
        )
        if kind_scope != DEFAULT_KIND_SCOPE:
            self.stdout.write(self.style.WARNING(
                f"Non-default scope: {KIND_SCOPES[kind_scope]['hint']}"
            ))
        if notify and dry_run:
            self.stdout.write(self.style.WARNING(
                'A dry run never notifies — ignoring --notify.'
            ))

        grand = {c: 0 for c in COUNT_COLUMNS}
        # One exact-tenant sweep per organization: tasks land on the right
        # organization and --notify sends one digest each. Legacy groups with no
        # organization are deliberately excluded because no tenant can safely
        # claim them.
        for org in orgs:
            result = generate_tasks(
                org,
                reasons=reasons,
                dry_run=dry_run,
                notify=notify,
                require_assignee=options['require_assignee'],
                kind_scope=kind_scope,
            )
            self.stdout.write('')
            self.stdout.write(f'{org.name} (id={org.id})')
            self._print_table(result['reasons'], result['totals'], dry_run)
            for column in COUNT_COLUMNS:
                grand[column] += result['totals'][column]

        if len(orgs) > 1:
            self.stdout.write('')
            self.stdout.write(self._row('ALL ORGS', grand))

        if grand['unassigned'] and not options['require_assignee']:
            self.stdout.write(self.style.WARNING(
                f"{grand['unassigned']} target(s) are missing the role required "
                'by their reason (Owner or Steward), so their tasks are '
                'unassigned. Fill in that role and re-run to route them.'
            ))

        if dry_run:
            banner = '=' * 68
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(banner))
            self.stdout.write(self.style.WARNING(
                'DRY RUN — NOTHING WAS WRITTEN. No tasks were created, reassigned '
                'or closed.'
            ))
            self.stdout.write(self.style.WARNING(
                'Re-run without --dry-run to apply the numbers above'
                + (' and add --confirm-broad.' if broad_scope else '.')
            ))
            self.stdout.write(self.style.WARNING(banner))
        else:
            self.stdout.write('')
            self.stdout.write(self.style.SUCCESS(
                f"Done. created={grand['created']}, reassigned={grand['reassigned']}, "
                f"closed={grand['closed']}"
            ))

    def _parse_reasons(self, raw):
        """``None`` (every reason) or the validated subset from --reasons."""
        # `is None` rather than falsy: `--reasons ""` is what an unset shell
        # variable expands to, and silently reading that as "sweep everything"
        # would mint tasks for every reason the caller thought they'd excluded.
        if raw is None:
            return None
        wanted = [r.strip().upper() for r in raw.split(',') if r.strip()]
        if not wanted:
            raise CommandError('--reasons was empty. Omit it to sweep every reason.')
        unknown = [r for r in wanted if r not in REASON_ORDER]
        if unknown:
            raise CommandError(
                f"Unknown reason(s): {', '.join(unknown)}. "
                f"Valid reasons: {', '.join(REASON_ORDER)}"
            )
        return wanted

    def _row(self, label, counts):
        return (f'{label:<{_LABEL_WIDTH}}'
                + ''.join(f'{counts[c]:>{_COL_WIDTH}}' for c in COUNT_COLUMNS))

    def _print_table(self, per_reason, totals, dry_run):
        header = (f"{'Reason':<{_LABEL_WIDTH}}"
                  + ''.join(f'{c.capitalize():>{_COL_WIDTH}}' for c in COUNT_COLUMNS))
        rule = '-' * len(header)
        self.stdout.write(header)
        self.stdout.write(rule)
        # REASON_ORDER, not the dict's keys, so the table reads the same way the
        # Task Manager groups its board — and skips whatever --reasons excluded.
        for reason in REASON_ORDER:
            counts = per_reason.get(reason)
            if counts is None:
                continue
            self.stdout.write(self._row(reason_label(reason), counts))
        self.stdout.write(rule)
        total_row = self._row('TOTAL', totals)
        self.stdout.write(self.style.WARNING(total_row) if dry_run
                          else self.style.SUCCESS(total_row))
