from django.apps import AppConfig


class AdminPortalConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "admin_portal"
    verbose_name = "Aqua Admin Portal"

    def ready(self):
        # Start the in-process inbox/DSAR automation loop (no external worker
        # or cron needed). No-ops during migrations and one-off commands.
        try:
            from .services.scheduler import maybe_start_background_scheduler

            maybe_start_background_scheduler()
        except Exception:  # pragma: no cover - never block app startup
            import logging

            logging.getLogger(__name__).exception("Could not start inbox auto-refresh scheduler")
