from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand
from django.db import transaction

from catalog.access import COMPANY
from catalog.models import (
    CustomUser,
    DataPerson,
    Organization,
    OrganizationMembership,
)

# (display_name, email, role)
# role is one of: 'owner', 'steward', 'viewer', 'other'.
# 'viewer' and 'other' both map to is_other=True (kept as separate labels for
# legacy rows that pre-date the explicit 'other' role).
MEMBERS = [
    ("Kostas Papadakis",        "kpapadakis@welcomepickups.com",      "owner"),
    ("Savvas Georgiou",         "savvas@welcomepickups.com",          "owner"),
    ("Richard Campagna",        "rcampagna@welcomepickups.com",       "owner"),
    ("Maria Tapini",            "maria@welcomepickups.com",           "owner"),
    ("George Sykokis",          "gsykokis@welcomepickups.com",        "owner"),
    ("Fernando Garcia-Poveda",  "fgarciapoveda@welcomepickups.com",   "owner"),
    ("Odysseas Bletsas",        "obletsas@welcomepickups.com",        "other"),
    ("Giannos Annousakis",      "gannousakis@welcomepickups.com",     "owner"),
    ("Aris Apostolopoulos",     "aris@welcomepickups.com",            "owner"),
    ("Thanos Topaloudis",       "ttopaloudis@welcomepickups.com",     "owner"),
    ("Vassilis Mentzelopoulos", "vmentzelopoulos@welcomepickups.com", "owner"),
    ("Ioanna Vlasopoulou",      "ivlasopoulou@welcomepickups.com",    "owner"),
    ("Aimilia Aspri",           "aaspri@welcomepickups.com",          "steward"),
    ("Alexandros Ntouvlis",     "antouvlis@welcomepickups.com",       "other"),
    ("Artemis Kasomoulis",      "akasomoulis@welcomepickups.com",     "owner"),
    ("Kalliopi Kasou",          "kkasou@welcomepickups.com",          "other"),
    ("Martina Grigorelli",      "mgrigorelli@welcomepickups.com",     "other"),
    ("Vasilis Georgopoulos",    "vgeorgopoulos@welcomepickups.com",   "other"),
    ("Alexis Andriopoulos",     "alandriopoulos@welcomepickups.com",  "other"),
    ("Klelia Pouliou",          "kpouliou@welcomepickups.com",        "other"),
    ("Nikos Manessis",          "nmanessis@welcomepickups.com",       "other"),
    ("Sotiris Koufokostas",     "skoufokostas@welcomepickups.com",    "other"),
    ("Antigoni Dantsi",         "adantsi@welcomepickups.com",         "other"),
]

ROLE_FLAGS = {
    "owner":   {"is_owner": True,  "is_steward": False, "is_other": False},
    "steward": {"is_owner": False, "is_steward": True,  "is_other": False},
    "viewer":  {"is_owner": False, "is_steward": False, "is_other": True},
    "other":   {"is_owner": False, "is_steward": False, "is_other": True},
}


class Command(BaseCommand):
    help = (
        "Bulk-create Welcome Pickups members (CustomUser + OrganizationMembership + "
        "DataPerson). Password is set to the local part of the email on first create. "
        "If a user already exists the password is left alone, but their DataPerson "
        "role flags and Company group membership are re-applied so re-running this "
        "command syncs the MEMBERS list. Run with --dry-run to preview."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Print what would happen without writing to the database.",
        )
        parser.add_argument(
            "--org", type=str, default=None,
            help="Organization name. Defaults to the only/first Organization in the DB.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        org_name = options["org"]

        if org_name:
            try:
                org = Organization.objects.get(name=org_name)
            except Organization.DoesNotExist:
                self.stderr.write(f"Organization '{org_name}' not found.")
                return
        else:
            orgs = list(Organization.objects.all())
            if not orgs:
                self.stderr.write("No Organization rows exist. Aborting.")
                return
            if len(orgs) > 1:
                names = ", ".join(o.name for o in orgs)
                self.stderr.write(
                    f"Multiple organizations exist ({names}). Re-run with --org <name>."
                )
                return
            org = orgs[0]

        self.stdout.write(f"Target organization: {org.name} (id={org.id})")
        self.stdout.write(f"Members to process: {len(MEMBERS)}")
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no changes will be written."))

        created = updated = 0

        for name, email, role in MEMBERS:
            local_part = email.split("@")[0]
            flags = ROLE_FLAGS[role]

            existing = CustomUser.objects.filter(email=email).first()

            if dry_run:
                if existing:
                    updated += 1
                    self.stdout.write(
                        f"  PLAN  update {email} (role={role}, name={name!r}) — id={existing.id}"
                    )
                else:
                    created += 1
                    self.stdout.write(
                        f"  PLAN  create user {email} (username={local_part}, "
                        f"password={local_part}, role={role}, name={name!r})"
                    )
                continue

            with transaction.atomic():
                if existing:
                    user = existing
                else:
                    user = CustomUser(email=email, username=local_part)
                    user.set_password(local_part)
                    user.save()

                company_group, _ = Group.objects.get_or_create(name=COMPANY)
                user.groups.add(company_group)

                OrganizationMembership.objects.get_or_create(
                    user=user, organization=org,
                )

                DataPerson.objects.update_or_create(
                    user=user,
                    defaults={
                        "name": name,
                        "organization": org,
                        **flags,
                    },
                )

            if existing:
                updated += 1
                self.stdout.write(self.style.SUCCESS(f"  UPDATE {email} ({role})"))
            else:
                created += 1
                self.stdout.write(self.style.SUCCESS(f"  CREATE {email} ({role})"))

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Done. created={created}, updated={updated}, total={len(MEMBERS)}"
        ))
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — no rows were written."))
