"""Drain the Admin -> CRM outbox.

Run at least every minute from the deployment scheduler. Deliberately a
command rather than an in-process timer, so a web dyno restart can neither
lose nor duplicate a delivery.
"""
from django.core.management.base import BaseCommand

from admin_portal.integrations.outbox import DEFAULT_BATCH, dispatch_due


class Command(BaseCommand):
    help = "Deliver due Admin -> CRM integration events (V2.3 outbox)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=DEFAULT_BATCH,
                            help=f"Max events per run (default {DEFAULT_BATCH}).")

    def handle(self, *args, **options):
        counts = dispatch_due(limit=options["limit"])
        self.stdout.write(self.style.SUCCESS(
            "attempted={attempted} delivered={delivered} "
            "pending={pending} dead_letter={dead_letter}".format(**counts)
        ))
        if counts["dead_letter"]:
            self.stderr.write(self.style.ERROR(
                f"{counts['dead_letter']} event(s) went to dead letter — human action required."
            ))
