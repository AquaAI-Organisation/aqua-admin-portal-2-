"""Google Workspace Gmail OAuth helpers."""
from __future__ import annotations

import base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from pathlib import Path

from django.conf import settings
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .runtime_config import GmailRuntimeConfig, get_gmail_runtime_config

_CACHE_KEY: tuple[str, str, str] | None = None
_CACHE_SERVICE = None


def gmail_configured() -> bool:
    return get_gmail_runtime_config().configured


def clear_gmail_service_cache() -> None:
    global _CACHE_KEY, _CACHE_SERVICE
    _CACHE_KEY = None
    _CACHE_SERVICE = None


def get_gmail_service():
    global _CACHE_KEY, _CACHE_SERVICE
    runtime = get_gmail_runtime_config()
    if not runtime.configured:
        raise RuntimeError("Gmail OAuth credentials are not configured.")
    cache_key = (runtime.client_id, runtime.client_secret, runtime.refresh_token)
    if _CACHE_SERVICE is not None and _CACHE_KEY == cache_key:
        return _CACHE_SERVICE

    creds = Credentials(
        token=None,
        refresh_token=runtime.refresh_token,
        client_id=runtime.client_id,
        client_secret=runtime.client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=list(getattr(settings, "GMAIL_SCOPES", [])),
    )
    _CACHE_SERVICE = build("gmail", "v1", credentials=creds, cache_discovery=False)
    _CACHE_KEY = cache_key
    return _CACHE_SERVICE


def send_gmail_message(
    *,
    subject: str,
    body: str,
    recipients: list[str],
    from_email: str | None = None,
    attachments: list[dict] | None = None,
) -> None:
    service = get_gmail_service()
    runtime = get_gmail_runtime_config()
    from_addr = from_email or runtime.sender

    if attachments:
        msg = MIMEMultipart()
        msg.attach(MIMEText(body, "plain", "utf-8"))
        for item in attachments:
            path = Path(item["path"])
            part = MIMEApplication(path.read_bytes(), Name=item.get("filename") or path.name)
            part["Content-Disposition"] = f'attachment; filename="{item.get("filename") or path.name}"'
            msg.attach(part)
    else:
        msg = MIMEText(body, "plain", "utf-8")

    msg["To"] = ", ".join([r for r in recipients if r])
    msg["From"] = from_addr
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


def pick_alias_for_mailbox(mailbox_kind: str, runtime: GmailRuntimeConfig | None = None) -> str:
    runtime = runtime or get_gmail_runtime_config()
    if mailbox_kind == "privacy":
        return runtime.privacy_alias or runtime.sender
    if mailbox_kind == "providers":
        return runtime.providers_alias or runtime.sender
    return runtime.support_alias or runtime.sender
