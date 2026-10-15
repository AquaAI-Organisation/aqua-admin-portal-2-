"""Record evidence-overdue safety holds (spec §12.1).

Run at least every 15 minutes. The 12-hour deadline is only meaningful if
something checks it; left unscheduled, a case waits for evidence forever and
the 24-hour applicant promise passes unnoticed.
"""
from django.core.management.base import BaseCommand

from admin_portal.integrations.evidence_sla import sweep


class Command(BaseCommand):
    help = "Hold provider cases whose CRM evidence has passed the 12-hour deadline."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200)
        parser.add_argument("--quiet", action="store_true",
                            help="Record holds without sending alerts.")

    def handle(self, *args, **options):
        counts = sweep(limit=options["limit"], notify=not options["quiet"])
        self.stdout.write(self.style.SUCCESS(
            "checked={checked} held={held} past_target={past_target} "
            "skipped={skipped}".format(**counts)))
        if counts["held"]:
            self.stderr.write(self.style.WARNING(
                f"{counts['held']} case(s) held for human review — each needs a "
                "decision before its 24-hour promise expires."))
