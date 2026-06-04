"""Auto-confirm DSAR identity by detecting aquaai.uk logins.

Two ways to run it:

  # One-shot — ideal for Heroku Scheduler (e.g. every 10 minutes)
  python manage.py confirm_dsar_logins

  # Continuous — for an always-on worker dyno (checks every --interval seconds)
  python manage.py confirm_dsar_logins --loop --interval 300
"""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand

from admin_portal.services.dsar import run_due_login_checks


class Command(BaseCommand):
    help = "Detect aquaai.uk logins and mark matching DSAR requests login-confirmed."

    def add_arguments(self, parser):
        parser.add_argument(
            "--loop",
            action="store_true",
            help="Run continuously instead of once (for a worker dyno).",
        )
        parser.add_argument(
            "--interval",
            type=int,
            default=300,
            help="Seconds between checks when --loop is set (default 300).",
        )

    def handle(self, *args, **options):
        if not options.get("loop"):
            checked, confirmed = run_due_login_checks()
            self.stdout.write(f"Checked {checked} pending DSAR request(s); confirmed {confirmed} login(s).")
            return

        interval = max(30, int(options.get("interval") or 300))
        self.stdout.write(f"Watching for DSAR logins every {interval}s. Press Ctrl+C to stop.")
        while True:
            try:
                checked, confirmed = run_due_login_checks()
                if confirmed:
                    self.stdout.write(f"Confirmed {confirmed} login(s) out of {checked} pending.")
            except Exception as exc:  # pragma: no cover - keep the loop alive
                self.stderr.write(f"DSAR login watcher error: {exc}")
            time.sleep(interval)
