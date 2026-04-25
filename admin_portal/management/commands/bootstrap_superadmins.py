"""Idempotently create AdminUser rows for the platform super-admins."""
import os
import secrets

from django.conf import settings
from django.core.management.base import BaseCommand

from admin_portal.models import AdminUser


class Command(BaseCommand):
    help = "Create / refresh the platform super-admin accounts (Steven + Ben)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--password", type=str, default=None,
            help="Password to set on every newly-created super-admin account.",
        )
        parser.add_argument(
            "--reset-password", action="store_true",
            help="Also reset the password on existing super-admins to --password.",
        )

    def handle(self, *args, **options):
        emails = [e.strip().lower() for e in getattr(settings, "SUPERADMIN_EMAILS", []) if e.strip()]
        if not emails:
            self.stderr.write("SUPERADMIN_EMAILS is empty; nothing to do.")
            return

        for email in emails:
            local = email.split("@", 1)[0].upper()
            env_pw = os.getenv(f"SUPERADMIN_PASSWORD_{local}")
            password = options["password"] or env_pw

            user, created = AdminUser.objects.get_or_create(
                email=email,
                defaults={
                    "is_staff": True,
                    "is_active": True,
                    "is_superuser": True,
                    "is_platform_super_admin": True,
                },
            )
            changed = []
            if not user.is_platform_super_admin:
                user.is_platform_super_admin = True; changed.append("is_platform_super_admin")
            if not user.is_superuser:
                user.is_superuser = True; changed.append("is_superuser")
            if not user.is_staff:
                user.is_staff = True; changed.append("is_staff")
            if not user.is_active:
                user.is_active = True; changed.append("is_active")

            if created or options["reset_password"]:
                final_pw = password or secrets.token_urlsafe(16)
                user.set_password(final_pw)
                changed.append("password")
                if not password:
                    self.stdout.write(self.style.WARNING(
                        f"Generated random password for {email}: {final_pw}\n"
                        "  Store it somewhere safe and rotate it after first login."
                    ))

            if changed:
                user.save()

            verb = "Created" if created else "Updated"
            self.stdout.write(self.style.SUCCESS(f"{verb} super-admin {email} ({', '.join(changed) or 'no changes'})."))
