from datetime import date

from django.core.management.base import BaseCommand

from admin_portal.services.reporting import build_report_for


class Command(BaseCommand):
    help = "Build (or rebuild) the AI review summary for a date and email/Slack the super-admins."

    def add_arguments(self, parser):
        parser.add_argument("--date", type=str, default=None,
                            help="ISO date (YYYY-MM-DD). Defaults to today.")

    def handle(self, *args, **options):
        target = date.fromisoformat(options["date"]) if options["date"] else None
        report = build_report_for(target)
        self.stdout.write(self.style.SUCCESS(
            f"Report {report.report_date}: "
            f"{report.approved_count} approved / {report.rejected_count} rejected / "
            f"{report.flagged_count} flagged / {report.pending_count} pending."
        ))
