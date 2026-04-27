"""Runtime health checks for dashboard visibility."""
from __future__ import annotations

import time

from django.conf import settings
from django.db import connection

from ..models import AdminInvite
from .error_classifier import classify_openai_error
from .intelligence_adapter import get_intelligence_readiness
from .notifier import email_config_status
from .runtime_config import get_mailbox_runtime_config, get_slack_runtime_config

_OPENAI_CACHE: dict[str, object] = {
    "checked_at": 0.0,
    "result": None,
}


def _placeholder(value: str) -> bool:
    return not value or "REPLACE" in value.upper()


def _status(ok: bool, label: str, detail: str, *, state: str | None = None) -> dict[str, str | bool]:
    return {"ok": ok, "label": label, "detail": detail, "state": state or ("ok" if ok else "bad")}


def get_health_snapshot() -> dict[str, dict[str, str | bool]]:
    db_status = _check_database()
    openai_status = _check_openai()
    slack_status = _check_slack()
    email_status = _check_email()
    mailbox_status = _check_mailbox()
    invite_status = _check_invites()
    intelligence_status = _check_intelligence()
    legacy_status = _check_legacy_redirect()
    return {
        "database": db_status,
        "openai": openai_status,
        "slack": slack_status,
        "email": email_status,
        "mailbox": mailbox_status,
        "invites": invite_status,
        "intelligence": intelligence_status,
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

    now = time.monotonic()
    cached = _OPENAI_CACHE.get("result")
    if cached and (now - float(_OPENAI_CACHE.get("checked_at", 0.0))) < 300:
        return cached

    try:
        from openai import OpenAI
    except ImportError:
        result = _status(False, "OpenAI", "openai package is not installed in this deployment.")
        _OPENAI_CACHE.update({"checked_at": now, "result": result})
        return result

    try:
        client = OpenAI(api_key=key)
        client.models.retrieve(model)
        result = _status(True, "OpenAI", f"Authenticated successfully and can access model {model}.")
    except Exception as exc:
        info = classify_openai_error(str(exc))
        state = "warn" if info["category"] == "transport_error" else "bad"
        result = _status(
            False,
            "OpenAI",
            f"{info['label']}: {info['summary']}",
            state=state,
        )

    _OPENAI_CACHE.update({"checked_at": now, "result": result})
    return result


def _check_slack():
    slack = get_slack_runtime_config()
    token = slack.token
    dm_emails = getattr(settings, "SUPERADMIN_EMAILS", [])
    fallback = slack.channel
    if _placeholder(token):
        return _status(False, "Slack", "SLACK_BOT_TOKEN is missing or still using a placeholder.")
    if not fallback:
        return _status(True, "Slack", f"Configured for direct messages to {len(dm_emails)} super admins.")
    return _status(True, "Slack", f"Configured for direct messages with channel fallback to {fallback}.")


def _check_email():
    status = email_config_status()
    return _status(bool(status["configured"]), "Email", str(status["detail"]), state="ok" if status["configured"] else "bad")


def _check_mailbox():
    mailbox = get_mailbox_runtime_config()
    if not mailbox.configured:
        return _status(False, "Mailbox", "IMAP inbox settings are incomplete.", state="bad")
    return _status(True, "Mailbox", f"Mailbox configured via {mailbox.host} as {mailbox.username}.", state="ok")


def _check_invites():
    last_invite = AdminInvite.objects.order_by("-created_at").first()
    if not last_invite:
        return _status(True, "Invites", "No admin invites have been sent yet.", state="warn")
    if last_invite.delivery_status == "email_sent":
        return _status(
            True,
            "Invites",
            f"Last invite to {last_invite.email} was emailed successfully at {last_invite.last_delivery_attempt_at:%Y-%m-%d %H:%M UTC}.",
        )
    if last_invite.delivery_status == "email_failed":
        return _status(
            False,
            "Invites",
            f"Last invite to {last_invite.email} failed email delivery. Fallback link is available. {last_invite.delivery_error}",
            state="warn",
        )
    return _status(
        True,
        "Invites",
        f"Last invite to {last_invite.email} is available through the fallback link even though email was not confirmed.",
        state="warn",
    )


def _check_intelligence():
    status = get_intelligence_readiness()
    return _status(bool(status["ok"]), "Intelligence", str(status["detail"]), state=str(status["state"]))


def _check_legacy_redirect():
    url = getattr(settings, "LEGACY_ADMIN_REDIRECT_URL", "")
    path = getattr(settings, "LEGACY_ADMIN_INTERNAL_PATH", "")
    if not url:
        return _status(False, "Legacy admin", "LEGACY_ADMIN_REDIRECT_URL is not configured.")
    if not path:
        return _status(False, "Legacy admin", "LEGACY_ADMIN_INTERNAL_PATH is not configured.")
    return _status(True, "Legacy admin", f"/admin/ should redirect to {url}; fallback path is {path}.")
