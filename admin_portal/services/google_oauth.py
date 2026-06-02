"""Google Workspace Gmail OAuth helpers."""
from __future__ import annotations

import base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from pathlib import Path
from urllib.parse import urlencode

import requests
from django.conf import settings
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .runtime_config import GmailRuntimeConfig, get_gmail_runtime_config

_CACHE_KEY: tuple[str, str, str] | None = None
_CACHE_SERVICE = None

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"


def gmail_configured() -> bool:
    return get_gmail_runtime_config().configured


def _scopes() -> list[str]:
    return list(getattr(settings, "GMAIL_SCOPES", []))


def build_authorization_url(*, client_id: str, redirect_uri: str, state: str, login_hint: str = "") -> str:
    """Build the Google consent screen URL for the installed-app / web OAuth flow."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(_scopes()),
        "access_type": "offline",
        "include_granted_scopes": "true",
        # prompt=consent forces Google to always return a refresh_token, even on
        # re-authorisation, so reconnecting always yields a usable long-lived token.
        "prompt": "consent",
        "state": state,
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{GOOGLE_AUTH_ENDPOINT}?{urlencode(params)}"


def exchange_code_for_tokens(*, client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    """Trade the one-time authorization code for access + refresh tokens."""
    response = requests.post(
        GOOGLE_TOKEN_ENDPOINT,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=20,
    )
    payload = {}
    try:
        payload = response.json()
    except ValueError:
        pass
    if response.status_code != 200 or "access_token" not in payload:
        detail = payload.get("error_description") or payload.get("error") or response.text[:300]
        raise RuntimeError(f"Google rejected the OAuth code exchange: {detail}")
    return payload


def _service_from_token(*, access_token: str, refresh_token: str, client_id: str, client_secret: str):
    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri=GOOGLE_TOKEN_ENDPOINT,
        scopes=_scopes(),
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def fetch_connected_profile(*, access_token: str, refresh_token: str, client_id: str, client_secret: str) -> dict:
    """Return the connected mailbox profile (primary email address + send-as aliases)."""
    service = _service_from_token(
        access_token=access_token,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
    )
    profile = service.users().getProfile(userId="me").execute()
    primary_email = profile.get("emailAddress", "")
    aliases: list[str] = []
    try:
        send_as = service.users().settings().sendAs().list(userId="me").execute()
        aliases = [
            entry.get("sendAsEmail", "")
            for entry in send_as.get("sendAs", [])
            if entry.get("sendAsEmail")
        ]
    except Exception:
        # gmail.settings.basic may be unavailable; alias auto-mapping is best-effort.
        aliases = []
    return {"primary_email": primary_email, "aliases": aliases}


def map_aliases(primary_email: str, aliases: list[str]) -> dict[str, str]:
    """Map discovered send-as addresses onto the support / privacy / providers lanes."""
    pool = list(dict.fromkeys([primary_email, *aliases]))  # de-dupe, keep order

    def _match(*keywords: str) -> str:
        for address in pool:
            lowered = address.lower()
            if any(keyword in lowered for keyword in keywords):
                return address
        return ""

    return {
        "sender": primary_email or _match("support") or (pool[0] if pool else ""),
        "support_alias": _match("support") or primary_email,
        "privacy_alias": _match("privacy"),
        "providers_alias": _match("provider"),
    }


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
