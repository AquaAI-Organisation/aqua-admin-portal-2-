"""All-in-one automation loop for the signup-review and privacy/DSAR pipelines.

Designed to run as a single always-on worker dyno so the whole flow is hands-off:
fetch the mailbox (which auto-analyses, auto-creates DSARs and sends verification
links), check for requesters who have logged in at aquaai.uk and deliver their
data, and review/triage new breeder & consultant signups (auto-approving them
when the operational toggle is on).

  python manage.py run_automation                # loop forever (default 120s)
  python manage.py run_automation --interval 60  # custom cadence
  python manage.py run_automation --once         # single pass (for a scheduler)
"""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand

from admin_portal.services.dsar import run_due_login_checks
from admin_portal.services.issue_runner import process_pending_issues
from admin_portal.services.mailbox import fetch_support_inbox
from admin_portal.services.review_runner import process_pending


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
        try:
            review_counts = process_pending(limit_per_type=limit)
            if review_counts.get("breeder") or review_counts.get("consultant"):
                self.stdout.write(
                    f"Reviewed {review_counts['breeder']} breeder(s) and "
                    f"{review_counts['consultant']} consultant(s)."
                )
        except Exception as exc:
            self.stderr.write(f"Account review pass failed: {exc}")
        try:
            issue_counts = process_pending_issues(limit_per_type=limit)
            if issue_counts.get("incident") or issue_counts.get("consultant_warning"):
                self.stdout.write(
                    f"Triaged {issue_counts.get('incident', 0)} incident(s) and "
                    f"{issue_counts.get('consultant_warning', 0)} consultant warning(s)."
                )
        except Exception as exc:
            self.stderr.write(f"Issue triage pass failed: {exc}")

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
