"""All-in-one automation loop for the privacy/DSAR pipeline.

Designed to run as a single always-on worker dyno so the whole flow is hands-off:
fetch the mailbox (which auto-analyses, auto-creates DSARs and sends verification
links), then check for requesters who have logged in at aquaai.uk and deliver
their data.

  python manage.py run_automation                # loop forever (default 120s)
  python manage.py run_automation --interval 60  # custom cadence
  python manage.py run_automation --once         # single pass (for a scheduler)
"""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand

from admin_portal.services.dsar import run_due_login_checks
from admin_portal.services.mailbox import fetch_support_inbox


class Command(BaseCommand):
    help = "Continuously poll the mailbox and confirm/deliver DSAR requests."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=120, help="Seconds between cycles (default 120).")
        parser.add_argument("--limit", type=int, default=25, help="Max messages to fetch per cycle.")
        parser.add_argument("--once", action="store_true", help="Run a single cycle and exit.")

    def _cycle(self, limit: int):
        try:
            result = fetch_support_inbox(limit=limit)
            if result.get("added") or result.get("dsar_created"):
                self.stdout.write(
                    f"Inbox: {result.get('added', 0)} new, "
                    f"{result.get('dsar_created', 0)} DSAR request(s) created."
                )
        except Exception as exc:
            self.stderr.write(f"Inbox fetch failed: {exc}")
        try:
            checked, confirmed = run_due_login_checks()
            if confirmed:
                self.stdout.write(f"Confirmed {confirmed} login(s) of {checked} pending DSAR request(s).")
        except Exception as exc:
            self.stderr.write(f"DSAR login check failed: {exc}")

    def handle(self, *args, **options):
        limit = int(options.get("limit") or 25)
        if options.get("once"):
            self._cycle(limit)
            return
        interval = max(30, int(options.get("interval") or 120))
        self.stdout.write(f"Automation running every {interval}s. Press Ctrl+C to stop.")
        while True:
            self._cycle(limit)
            time.sleep(interval)
