"""Daily Admin <-> CRM reconciliation (spec §12 definition of done).

Reports divergence; never repairs. Exits non-zero when something needs a
person, so a scheduler can surface it.
"""
from django.core.management.base import BaseCommand

from admin_portal.integrations.reconciliation import build_report, notify, render


class Command(BaseCommand):
    help = "Reconcile the Admin and CRM integration ledgers and report divergence."

    def add_arguments(self, parser):
        parser.add_argument("--quiet", action="store_true",
                            help="Print the report without alerting.")

    def handle(self, *args, **options):
        report = build_report()
        self.stdout.write(render(report))
        if not options["quiet"]:
            notify(report)
        if not report.ok:
            raise SystemExit(1)
