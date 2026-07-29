"""Link ``DataPerson`` rows to the ``CustomUser`` logins they belong to.

    python manage.py link_data_persons                        # dry run, auto-match
    python manage.py link_data_persons --apply
    python manage.py link_data_persons --csv people.csv --apply
    python manage.py link_data_persons --org 2 --csv people.csv

``DataPerson.user`` is the ONLY join between governance identity (who owns a
measure) and authentication identity (who signed in) — see
``access.resolve_data_person``. Most rows don't have it set, which is why the
Task Manager falls back to "pick your own name": without the link the app knows
who you are but not which assets are yours. This command closes that gap in
bulk, for the rows where the answer is not a guess.

Why the matching bar is exact-and-unique
----------------------------------------
A wrong link is worse than no link: it hands one person another person's task
list, and every "My Tasks" board, Slack ping and Done click after that is
attributed to the wrong human. Fuzzy matching (first-name-only, email local
part, edit distance) gets that wrong on exactly the cases it's meant to help
with — two Georges, a married name, a transliterated Greek surname. So the auto
matcher links only when a DataPerson name and a member's ``get_full_name()``
agree exactly (case- and whitespace-insensitively) AND nothing else on either
side shares that name. Everything else is *reported*, not resolved: the human
reading the AMBIGUOUS / UNMATCHED sections can settle those with ``--csv``.
"""
import csv
import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from catalog.models import DataPerson, Organization, OrganizationMembership


def _norm(name):
    """Match key: case-folded, inner whitespace collapsed.

    Deliberately narrow — 'jane  doe' and 'Jane Doe' are the same person, but
    that is the entire tolerance. Anything looser stops being a match and
    becomes a guess (see the module docstring).
    """
    return ' '.join((name or '').split()).lower()


class Command(BaseCommand):
    help = (
        "Link DataPerson rows to CustomUser logins (DataPerson.user), which is what "
        "gives a person their own task feed. Default is a DRY RUN — pass --apply to "
        "write. Without --csv, links only unambiguous exact full-name matches and "
        "reports everything else; with --csv <path> (columns email,name) links the "
        "pairs you list. Never re-points a DataPerson that already belongs to a "
        "different login."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--org', type=int, default=None,
            help='Organization id. Defaults to the only/first Organization in the DB.',
        )
        parser.add_argument(
            '--apply', action='store_true',
            help='Write the links. Without it nothing is saved.',
        )
        parser.add_argument(
            '--csv', dest='csv_path', default=None,
            help='CSV with columns email,name giving explicit pairs. Disables auto-matching.',
        )

    def handle(self, *args, **options):
        apply = options['apply']
        csv_path = options['csv_path']
        org = self._resolve_org(options['org'])

        # Org members only: a login outside this org can never act here, so
        # linking it would create a dead reference that still trips the
        # one-DataPerson-per-login constraint.
        users = [
            m.user for m in OrganizationMembership.objects
            .filter(organization=org).select_related('user').order_by('user__email')
        ]
        people = list(DataPerson.objects.filter(organization=org).order_by('name'))

        self.stdout.write(f"Target organization: {org.name} (id={org.id})")
        self.stdout.write(f"People: {len(people)}   Members: {len(users)}")
        self.stdout.write(
            f"Mode: CSV pairs ({csv_path})" if csv_path
            else "Mode: auto-match on exact, unique full name"
        )
        if not apply:
            self.stdout.write(self.style.WARNING("DRY RUN — no changes will be written."))

        if csv_path:
            plan, ambiguous, unmatched = self._from_csv(csv_path, org, people, users)
        else:
            plan, ambiguous, unmatched = self._auto_match(people, users)

        plan, already, conflicts = self._reject_conflicts(plan)
        # Conflicts are printed inside UNMATCHED rather than as a fourth section:
        # from the reader's point of view they are simply rows that did not get
        # linked, and the CONFLICT marker says why. The summary counts them apart.
        unmatched.extend(conflicts)

        if apply and plan:
            with transaction.atomic():
                for person, user in plan:
                    person.user = user
                    person.save(update_fields=['user'])

        verb = 'LINK' if apply else 'PLAN'
        self._section('LINKED', len(plan), [
            (f"  {verb}  #{p.id} {p.name!r}", f"-> {u.email}") for p, u in plan
        ], style=self.style.SUCCESS if apply else None)
        self._section('AMBIGUOUS', len(ambiguous), ambiguous)
        self._section('UNMATCHED', len(unmatched), unmatched)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Done. linked={len(plan)}, already_linked={already}, "
            f"ambiguous={len(ambiguous)}, unmatched={len(unmatched)} "
            f"(of which conflicts={len(conflicts)}), people={len(people)}"
        ))
        if not apply:
            self.stdout.write(self.style.WARNING("Dry run — no rows were written. Re-run with --apply."))

    # --- resolution ---------------------------------------------------------

    def _resolve_org(self, org_id):
        if org_id is not None:
            org = Organization.objects.filter(id=org_id).first()
            if org is None:
                raise CommandError(f"Organization id={org_id} not found.")
            return org
        orgs = list(Organization.objects.all())
        if not orgs:
            raise CommandError("No Organization rows exist. Aborting.")
        if len(orgs) > 1:
            listed = ", ".join(f"{o.name} (id={o.id})" for o in orgs)
            raise CommandError(f"Multiple organizations exist ({listed}). Re-run with --org <id>.")
        return orgs[0]

    def _auto_match(self, people, users):
        """Exact, unique full-name matches only. Returns (plan, ambiguous, unmatched)."""
        by_name = {}
        for user in users:
            key = _norm(user.get_full_name())
            if not key:
                # A login with no first/last name has nothing to match on. Not a
                # failure — it just can't be resolved this way; --csv can.
                continue
            by_name.setdefault(key, []).append(user)

        plan, ambiguous, unmatched = [], [], []
        for person in people:
            if person.user_id is not None:
                continue  # already has its login; nothing to decide
            candidates = by_name.get(_norm(person.name), [])
            subject = f"  #{person.id} {person.name!r}"
            if len(candidates) == 1:
                plan.append((person, candidates[0]))
            elif candidates:
                emails = ", ".join(u.email for u in candidates)
                ambiguous.append((subject, f"— {len(candidates)} logins share this name: {emails}"))
            else:
                unmatched.append((subject, "— no member whose full name matches exactly"))
        return plan, ambiguous, unmatched

    def _from_csv(self, path, org, people, users):
        """Explicit email,name pairs. Same safety rules — the CSV says *which*
        pair to link, it doesn't authorise inventing rows that aren't there."""
        if not os.path.isfile(path):
            raise CommandError(f"CSV file not found: {path}")

        by_email = {(u.email or '').strip().lower(): u for u in users}
        by_name = {}
        for person in people:
            by_name.setdefault(_norm(person.name), []).append(person)

        plan, ambiguous, unmatched = [], [], []
        # utf-8-sig: exports from Excel/Sheets carry a BOM that would otherwise
        # end up glued to the first header name.
        with open(path, newline='', encoding='utf-8-sig') as fh:
            reader = csv.DictReader(fh)
            headers = {(h or '').strip().lower() for h in (reader.fieldnames or [])}
            if not {'email', 'name'} <= headers:
                raise CommandError(
                    f"CSV must have columns 'email' and 'name' (got: {sorted(headers)})."
                )
            for lineno, row in enumerate(reader, start=2):
                # A row with more cells than headers hands DictReader a list
                # under a None key — ignore those instead of crashing the run.
                clean = {
                    (k or '').strip().lower(): v.strip()
                    for k, v in row.items() if isinstance(v, str)
                }
                email, name = clean.get('email', '').lower(), clean.get('name', '')
                subject = f"  row {lineno} {name!r} <{email}>"
                if not email or not name:
                    unmatched.append((subject, "— needs both an email and a name"))
                    continue
                user = by_email.get(email)
                if user is None:
                    unmatched.append((subject, f"— no member of {org.name} with that email"))
                    continue
                candidates = by_name.get(_norm(name), [])
                if len(candidates) == 1:
                    plan.append((candidates[0], user))
                elif candidates:
                    ids = ", ".join(f"#{p.id}" for p in candidates)
                    ambiguous.append((subject, f"— {len(candidates)} DataPersons named that: {ids}"))
                else:
                    unmatched.append((subject, "— no DataPerson with that name in this org"))
        return plan, ambiguous, unmatched

    def _reject_conflicts(self, plan):
        """Drop anything that would take a link away from someone.

        Four ways that happens, all of them a signal the data needs a human
        rather than a bigger hammer: the DataPerson already belongs to another
        login, the login already has another DataPerson (the
        ``uniq_dataperson_user`` constraint would reject it anyway), or the same
        login / the same DataPerson was claimed twice in one run.

        The two "twice in one run" cases matter because neither hits a database
        constraint: two rows pointing one DataPerson at different logins just
        save in order, so the last line of the CSV silently wins and the run
        still reports both as linked — a wrong link that looks like a right one.
        """
        taken = {
            dp.user_id: dp for dp in
            DataPerson.objects.filter(user__isnull=False).select_related('user')
        }
        kept, conflicts, already = [], [], 0
        claimed_users, claimed_people = {}, {}
        for person, user in plan:
            subject = f"  CONFLICT #{person.id} {person.name!r}"
            planned_user = claimed_people.get(person.id)
            if person.user_id == user.id or (planned_user and planned_user.id == user.id):
                # Same link asked for twice (or already in the DB) — the first
                # entry covers it, so this one needs no write of its own.
                already += 1
            elif person.user_id is not None:
                conflicts.append((subject, f"— already linked to another login (wanted {user.email})"))
            elif planned_user is not None:
                conflicts.append((
                    subject,
                    f"— already claimed by {planned_user.email} earlier in this run "
                    f"(wanted {user.email})",
                ))
            elif user.id in claimed_users:
                conflicts.append((subject, f"— {user.email} was already claimed by #{claimed_users[user.id].id}"))
            elif user.id in taken:
                other = taken[user.id]
                conflicts.append((subject, f"— {user.email} already belongs to #{other.id} {other.name!r}"))
            else:
                claimed_users[user.id] = person
                claimed_people[person.id] = user
                kept.append((person, user))
        return kept, already, conflicts

    def _section(self, title, count, rows, style=None):
        self.stdout.write("")
        self.stdout.write(f"{title} ({count})")
        for subject, detail in rows:
            line = f"{subject} {detail}"
            self.stdout.write(style(line) if style else line)
