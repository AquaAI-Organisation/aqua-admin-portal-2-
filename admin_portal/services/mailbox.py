"""Inbox ingestion and reply helpers."""
from __future__ import annotations

import base64
import email
import imaplib
import logging
from datetime import datetime, timedelta, timezone as dt_timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime, parseaddr

from django.db import IntegrityError
from django.utils import timezone

from ..models import ExternalBreederProfile, ExternalConsultantProfile, ExternalUser, SupportInquiry
from .google_oauth import get_gmail_service, gmail_configured, pick_alias_for_mailbox
from .notifier import send_custom_email
from .runtime_config import get_gmail_runtime_config, get_mailbox_runtime_config

logger = logging.getLogger(__name__)


def fetch_support_inbox(limit: int = 25) -> dict[str, int]:
    if gmail_configured():
        return _fetch_gmail_inbox(limit=limit)
    return _fetch_imap_inbox(limit=limit)


def _run_privacy_automation(inquiry: SupportInquiry, created: bool) -> bool:
    """Hands-off triage + DSAR intake for a privacy-lane message.

    Shared by both the Gmail and IMAP ingestion paths so an inbound privacy
    request is analysed and turned into a DSAR the moment it lands — no operator
    "refresh"/"analyse" clicks required. Returns True if a new DSAR was created.
    Scoped to the privacy lane to control OpenAI token spend; other lanes are
    still analysed on demand. Falls back to heuristics if OpenAI is unavailable.
    """
    if inquiry.mailbox_kind != "privacy":
        return False
    if created:
        try:
            from .inquiry_intelligence import persist_inquiry_analysis

            persist_inquiry_analysis(inquiry)
        except Exception:
            logger.exception("Auto analysis failed for inquiry %s", inquiry.pk)
    try:
        from .dsar import ensure_dsar_request_from_inquiry

        dsar_request, created_request = ensure_dsar_request_from_inquiry(inquiry)
        return bool(dsar_request and created_request)
    except Exception:
        logger.exception("Auto DSAR intake failed for inquiry %s", inquiry.pk)
        return False


def send_support_reply(inquiry: SupportInquiry, body: str) -> dict[str, str | bool]:
    result = send_custom_email(
        subject=f"Re: {inquiry.subject or 'Aqua AI support enquiry'}",
        body=body,
        recipients=[inquiry.from_email],
        from_email=pick_alias_for_mailbox(inquiry.mailbox_kind),
    )
    if result["ok"]:
        history = list(inquiry.response_history or [])
        history.append(
            {
                "sent_at": timezone.now().isoformat(),
                "body": body,
                "from_alias": pick_alias_for_mailbox(inquiry.mailbox_kind),
            }
        )
        inquiry.response_history = history
        inquiry.response_draft = body
        inquiry.responded_at = timezone.now()
        inquiry.status = "replied"
        inquiry.save(update_fields=["response_history", "response_draft", "responded_at", "status"])
    return result


def _fetch_gmail_inbox(limit: int = 25) -> dict[str, int]:
    service = get_gmail_service()
    response = service.users().messages().list(
        userId="me",
        labelIds=["INBOX"],
        maxResults=max(1, min(limit, 100)),
    ).execute()
    messages = response.get("messages", [])
    added = 0
    updated = 0
    dsar_created = 0
    for entry in messages:
        try:
            payload = service.users().messages().get(
                userId="me",
                id=entry["id"],
                format="full",
            ).execute()
            parsed = _parse_gmail_message(payload)
            if not parsed["message_id"]:
                continue
            defaults = _inquiry_defaults(parsed)
            try:
                inquiry, created = SupportInquiry.objects.update_or_create(
                    message_id=parsed["message_id"],
                    defaults=defaults,
                )
            except IntegrityError:
                continue
            if created:
                added += 1
            else:
                updated += 1
            if _run_privacy_automation(inquiry, created):
                dsar_created += 1
        except Exception:
            # A single malformed or ambiguous message must not abort the sync;
            # log and move on so the rest of the inbox still ingests.
            logger.exception("Skipped a Gmail message during inbox sync (id=%s)", entry.get("id"))
            continue
    return {"added": added, "updated": updated, "dsar_created": dsar_created}


def _fetch_imap_inbox(limit: int = 25) -> dict[str, int]:
    mailbox = get_mailbox_runtime_config()
    if not mailbox.configured:
        raise RuntimeError("Inbox settings are incomplete. Configure Gmail OAuth or IMAP host, username, and password first.")

    client_cls = imaplib.IMAP4_SSL if mailbox.use_ssl else imaplib.IMAP4
    added = 0
    updated = 0
    dsar_created = 0
    with client_cls(mailbox.host, mailbox.port) as client:
        try:
            client.login(mailbox.username, mailbox.password)
        except imaplib.IMAP4.error as exc:
            raise RuntimeError(_friendly_mailbox_error(str(exc))) from exc
        client.select(mailbox.folder)
        status, data = client.search(None, "ALL")
        if status != "OK":
            raise RuntimeError("Could not search the inbox.")
        ids = [item for item in data[0].split() if item][-limit:]
        for mail_id in reversed(ids):
            status, payload = client.fetch(mail_id, "(RFC822)")
            if status != "OK":
                continue
            raw = payload[0][1]
            message = email.message_from_bytes(raw)
            parsed = _parse_imap_message(message)
            if not parsed["message_id"]:
                continue
            defaults = _inquiry_defaults(parsed)
            try:
                inquiry, created = SupportInquiry.objects.update_or_create(
                    message_id=parsed["message_id"],
                    defaults=defaults,
                )
            except IntegrityError:
                continue
            if created:
                added += 1
            else:
                updated += 1
            # Same hands-off triage + DSAR intake as the Gmail path, so privacy
            # requests are analysed and turned into DSARs automatically whether
            # the mailbox is connected via Gmail OAuth or IMAP (e.g. M365).
            if _run_privacy_automation(inquiry, created):
                dsar_created += 1
    return {"added": added, "updated": updated, "dsar_created": dsar_created}


def _parse_gmail_message(message: dict) -> dict:
    payload = message.get("payload", {}) or {}
    headers = _header_map(payload.get("headers", []))
    to_email = _first_nonempty(headers, "delivered-to", "x-original-to", "to")
    received_at = _gmail_received_at(message, headers)
    mailbox_kind = _resolve_mailbox_kind(to_email, headers)
    from_name, from_email = parseaddr(headers.get("from", ""))
    body_text = _extract_gmail_body(payload)
    return {
        "message_id": (headers.get("message-id", "") or "").strip().strip("<>").strip(),
        "gmail_thread_id": message.get("threadId", ""),
        "gmail_label_ids": list(message.get("labelIds", []) or []),
        "from_name": from_name,
        "from_email": from_email,
        "to_email": to_email,
        "mailbox_kind": mailbox_kind,
        "subject": headers.get("subject", ""),
        "body_text": body_text,
        "received_at": received_at,
    }


def _parse_imap_message(message):
    from_name, from_email = parseaddr(message.get("From", ""))
    subject = str(make_header(decode_header(message.get("Subject", ""))))
    message_id = (message.get("Message-ID") or "").strip().strip("<>").strip()
    received_at = parsedate_to_datetime(message.get("Date")) if message.get("Date") else timezone.now()
    if received_at and timezone.is_naive(received_at):
        received_at = timezone.make_aware(received_at, timezone.get_current_timezone())
    to_email = message.get("Delivered-To") or message.get("X-Original-To") or message.get("To", "")
    mailbox_kind = _resolve_mailbox_kind(to_email, {key.lower(): message.get(key, "") for key in message.keys()})
    return {
        "message_id": message_id,
        "gmail_thread_id": "",
        "gmail_label_ids": [],
        "from_name": from_name,
        "from_email": from_email,
        "to_email": to_email,
        "mailbox_kind": mailbox_kind,
        "subject": subject,
        "body_text": _extract_imap_body(message),
        "received_at": received_at or timezone.now(),
    }


def _extract_imap_body(message) -> str:
    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if content_type == "text/plain" and "attachment" not in disposition:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
    payload = message.get_payload(decode=True) or b""
    charset = message.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def _extract_gmail_body(payload: dict) -> str:
    mime_type = payload.get("mimeType", "")
    body = payload.get("body", {}) or {}
    data = body.get("data")
    parts = payload.get("parts") or []
    if mime_type == "text/plain" and data:
        return _decode_gmail_data(data)
    if mime_type == "text/html" and data:
        return _decode_gmail_data(data)
    for part in parts:
        text = _extract_gmail_body(part)
        if text:
            return text
    if data:
        return _decode_gmail_data(data)
    return ""


def _decode_gmail_data(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8", errors="replace")


def _header_map(headers: list[dict]) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for item in headers:
        name = str(item.get("name", "")).strip().lower()
        value = str(item.get("value", "")).strip()
        if name:
            mapped[name] = value
    return mapped


def _first_nonempty(headers: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = headers.get(key, "")
        if value:
            return value
    return ""


def _gmail_received_at(message: dict, headers: dict[str, str]):
    if headers.get("date"):
        parsed = parsedate_to_datetime(headers["date"])
        if parsed and timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
        if parsed:
            return parsed
    internal_date = message.get("internalDate")
    if internal_date:
        return datetime.fromtimestamp(int(internal_date) / 1000, tz=dt_timezone.utc)
    return timezone.now()


def _resolve_mailbox_kind(to_email: str, headers: dict[str, str]) -> str:
    runtime = get_gmail_runtime_config()
    haystack = " ".join(
        [
            str(to_email or ""),
            headers.get("to", ""),
            headers.get("delivered-to", ""),
            headers.get("x-original-to", ""),
        ]
    ).lower()
    if runtime.privacy_alias and runtime.privacy_alias.lower() in haystack:
        return "privacy"
    if runtime.providers_alias and runtime.providers_alias.lower() in haystack:
        return "providers"
    return "general"


def _inquiry_defaults(parsed):
    matched_entity_type, matched_entity_id = _match_sender(parsed["from_email"])
    return {
        "gmail_thread_id": parsed.get("gmail_thread_id", ""),
        "from_email": parsed["from_email"],
        "from_name": parsed["from_name"],
        "to_email": parsed.get("to_email", "")[:255],
        "mailbox_kind": parsed.get("mailbox_kind", "general"),
        "subject": parsed["subject"][:255],
        "body_text": parsed["body_text"],
        "gmail_label_ids": parsed.get("gmail_label_ids", []),
        "received_at": parsed["received_at"],
        "matched_entity_type": matched_entity_type,
        "matched_entity_id": matched_entity_id,
    }


def _match_sender(sender_email: str) -> tuple[str, str]:
    if not sender_email:
        return "", ""
    # The shared Supabase table can contain duplicate or case-variant rows for
    # the same email, so use filter().first() instead of get() to stay resilient
    # — one ambiguous sender must never abort the whole inbox sync.
    user = ExternalUser.objects.filter(email__iexact=sender_email).order_by("id").first()
    if not user:
        return "", ""
    consultant = ExternalConsultantProfile.objects.filter(user_id=user.id).order_by("id").first()
    if consultant:
        return "consultant", str(consultant.id)
    breeder = ExternalBreederProfile.objects.filter(user_id=user.id).order_by("id").first()
    if breeder:
        return "breeder", str(breeder.id)
    return "user", str(user.id)


def _friendly_mailbox_error(error: str) -> str:
    lowered = (error or "").lower()
    if "invalid_grant" in lowered or "token has been expired or revoked" in lowered:
        return (
            "Google rejected the saved Gmail refresh token. Generate a fresh OAuth refresh token and save it in Settings."
        )
    if "authentication failed" in lowered or "login failed" in lowered or "invalid credentials" in lowered:
        return (
            "The inbox login was rejected. Re-check the mailbox credentials, and if this is Microsoft 365 also confirm "
            "that IMAP access is allowed and that MFA, device approval, or an app password is not blocking the sign-in."
        )
    return error
