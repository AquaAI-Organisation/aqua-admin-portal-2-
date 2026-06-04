"""Fetch the Aqua AI mailbox so the support/DSAR pipeline runs hands-off.

Each fetch auto-runs AI triage on new messages, and for privacy-lane messages
auto-creates DSAR requests and emails the requester a verification link.

  # One-shot — ideal for Heroku Scheduler (e.g. every 10 minutes)
  python manage.py poll_inbox

  # Continuous — for an always-on worker dyno
  python manage.py poll_inbox --loop --interval 300
"""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand

from admin_portal.services.mailbox import fetch_support_inbox


class Command(BaseCommand):
    help = "Fetch the support/privacy mailbox and run intake automation."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=25, help="Max messages to fetch per run.")
        parser.add_argument("--loop", action="store_true", help="Run continuously (worker dyno).")
        parser.add_argument("--interval", type=int, default=300, help="Seconds between runs when --loop is set.")

    def _run_once(self, limit: int):
        try:
            result = fetch_support_inbox(limit=limit)
            self.stdout.write(
                f"Inbox: {result.get('added', 0)} added, {result.get('updated', 0)} updated, "
                f"{result.get('dsar_created', 0)} DSAR request(s) created."
            )
        except Exception as exc:
            self.stderr.write(f"Inbox fetch failed: {exc}")

    def handle(self, *args, **options):
        limit = int(options.get("limit") or 25)
        if not options.get("loop"):
            self._run_once(limit)
            return
        interval = max(30, int(options.get("interval") or 300))
        self.stdout.write(f"Polling inbox every {interval}s. Press Ctrl+C to stop.")
        while True:
            self._run_once(limit)
            time.sleep(interval)
