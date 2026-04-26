"""Runtime health checks for dashboard visibility."""
from __future__ import annotations

from django.conf import settings
from django.db import connection


def _placeholder(value: str) -> bool:
    return not value or "REPLACE" in value.upper()


def _status(ok: bool, label: str, detail: str) -> dict[str, str | bool]:
    return {"ok": ok, "label": label, "detail": detail}


def get_health_snapshot() -> dict[str, dict[str, str | bool]]:
    db_status = _check_database()
    openai_status = _check_openai()
    slack_status = _check_slack()
    email_status = _check_email()
    legacy_status = _check_legacy_redirect()
    return {
        "database": db_status,
        "openai": openai_status,
        "slack": slack_status,
        "email": email_status,
        "legacy_redirect": legacy_status,
    }


def _check_database():
    try:
        connection.ensure_connection()
        return _status(True, "Database", "Connected to the shared backend database.")
    except Exception as exc:
        return _status(False, "Database", f"Connection failed: {exc}")


def _check_openai():
    key = getattr(settings, "OPENAI_API_KEY", "")
    model = getattr(settings, "OPENAI_MODEL", "gpt-4o")
    if _placeholder(key):
        return _status(False, "OpenAI", "OPENAI_API_KEY is missing or still using a placeholder.")
    return _status(True, "OpenAI", f"Configured for model {model}.")


def _check_slack():
    token = getattr(settings, "SLACK_BOT_TOKEN", "")
    dm_emails = getattr(settings, "SUPERADMIN_EMAILS", [])
    fallback = getattr(settings, "SLACK_CHANNEL", "")
    if _placeholder(token):
        return _status(False, "Slack", "SLACK_BOT_TOKEN is missing or still using a placeholder.")
    if not fallback:
        return _status(True, "Slack", f"Configured for direct messages to {len(dm_emails)} super admins.")
    return _status(True, "Slack", f"Configured for direct messages with channel fallback to {fallback}.")


def _check_email():
    host = getattr(settings, "EMAIL_HOST", "")
    user = getattr(settings, "EMAIL_HOST_USER", "")
    password = getattr(settings, "EMAIL_HOST_PASSWORD", "")
    if not host or not user or _placeholder(password):
        return _status(False, "Email", "SMTP settings are incomplete.")
    return _status(True, "Email", f"SMTP configured via {host} as {user}.")


def _check_legacy_redirect():
    url = getattr(settings, "LEGACY_ADMIN_REDIRECT_URL", "")
    path = getattr(settings, "LEGACY_ADMIN_INTERNAL_PATH", "")
    if not url:
        return _status(False, "Legacy admin", "LEGACY_ADMIN_REDIRECT_URL is not configured.")
    if not path:
        return _status(False, "Legacy admin", "LEGACY_ADMIN_INTERNAL_PATH is not configured.")
    return _status(True, "Legacy admin", f"/admin/ should redirect to {url}; fallback path is {path}.")
