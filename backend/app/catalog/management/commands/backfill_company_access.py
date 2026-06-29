from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand

from catalog.access import ACCESS_GROUPS, COMPANY
from catalog.models import CustomUser


class Command(BaseCommand):
    help = (
        "Grant 'Company' group to every CustomUser that currently has none of the "
        "three access groups (Company / Analytics / Admin). Intended as a one-off "
        "fix for users created via bulk_add_members before Company became the "
        "default. Idempotent — users that already have any access group are skipped."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Print what would happen without writing to the database.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        company, _ = Group.objects.get_or_create(name=COMPANY)

        users_without_access = (
            CustomUser.objects
            .exclude(groups__name__in=ACCESS_GROUPS)
            .order_by("email")
            .distinct()
        )

        total = users_without_access.count()
        self.stdout.write(f"Users with no access group: {total}")
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no changes will be written."))

        granted = 0
        for user in users_without_access:
            if dry_run:
                self.stdout.write(f"  PLAN  grant Company -> {user.email}")
            else:
                user.groups.add(company)
                self.stdout.write(self.style.SUCCESS(f"  GRANT {user.email}"))
            granted += 1

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Done. granted={granted}, total={total}"))
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — no rows were written."))
