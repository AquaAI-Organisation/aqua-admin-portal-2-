"""Auto-confirm DSAR identity by detecting aquaai.uk logins.

Run periodically (e.g. every few minutes) so a requester's login is recognised
within the 48-hour window without an admin having to open each case:

    python manage.py confirm_dsar_logins
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from admin_portal.models import DSARRequest
from admin_portal.services.dsar import check_dsar_login


class Command(BaseCommand):
    help = "Detect aquaai.uk logins and mark matching DSAR requests login-confirmed."

    def handle(self, *args, **options):
        pending = DSARRequest.objects.filter(
            login_confirmed_at__isnull=True,
            subject_user_id__isnull=False,
            verification_expires_at__gte=timezone.now(),
        ).exclude(status__in=["fulfilled", "rejected", "withdrawn"])

        confirmed = 0
        checked = 0
        for dsar_request in pending.iterator():
            checked += 1
            try:
                if check_dsar_login(dsar_request):
                    confirmed += 1
            except Exception as exc:  # pragma: no cover - defensive
                self.stderr.write(f"DSAR {dsar_request.id}: check failed: {exc}")
        self.stdout.write(f"Checked {checked} pending DSAR request(s); confirmed {confirmed} login(s).")
