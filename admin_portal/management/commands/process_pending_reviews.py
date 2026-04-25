from django.core.management.base import BaseCommand

from admin_portal.services.review_runner import process_pending


class Command(BaseCommand):
    help = "Run GPT-4 review on every breeder/consultant signup the AI hasn't seen yet."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=25,
                            help="Max profiles per subject_type per run (default 25).")

    def handle(self, *args, **options):
        counts = process_pending(limit_per_type=options["limit"])
        self.stdout.write(self.style.SUCCESS(
            f"Reviewed {counts['breeder']} breeders and {counts['consultant']} consultants."
        ))
