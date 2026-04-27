"""Support mailbox ingestion and reply helpers."""
from __future__ import annotations

import email
import imaplib
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime, parseaddr

from django.db import IntegrityError
from django.utils import timezone

from ..models import ExternalBreederProfile, ExternalConsultantProfile, ExternalUser, SupportInquiry
from .notifier import send_custom_email
from .runtime_config import get_mailbox_runtime_config


def fetch_support_inbox(limit: int = 25) -> dict[str, int]:
    mailbox = get_mailbox_runtime_config()
    if not mailbox.configured:
        raise RuntimeError("Mailbox settings are incomplete. Configure IMAP host, username, and password first.")

    client_cls = imaplib.IMAP4_SSL if mailbox.use_ssl else imaplib.IMAP4
    added = 0
    updated = 0
    with client_cls(mailbox.host, mailbox.port) as client:
        try:
            client.login(mailbox.username, mailbox.password)
        except imaplib.IMAP4.error as exc:
            raise RuntimeError(_friendly_mailbox_error(str(exc))) from exc
        client.select(mailbox.folder)
        status, data = client.search(None, "ALL")
        if status != "OK":
            raise RuntimeError("Could not search the mailbox.")
        ids = [item for item in data[0].split() if item][-limit:]
        for mail_id in reversed(ids):
            status, payload = client.fetch(mail_id, "(RFC822)")
            if status != "OK":
                continue
            raw = payload[0][1]
            message = email.message_from_bytes(raw)
            parsed = _parse_message(message)
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
    return {"added": added, "updated": updated}


def send_support_reply(inquiry: SupportInquiry, body: str) -> dict[str, str | bool]:
    result = send_custom_email(
        subject=f"Re: {inquiry.subject or 'Aqua AI support enquiry'}",
        body=body,
        recipients=[inquiry.from_email],
    )
    if result["ok"]:
        history = list(inquiry.response_history or [])
        history.append({
            "sent_at": timezone.now().isoformat(),
            "body": body,
        })
        inquiry.response_history = history
        inquiry.response_draft = body
        inquiry.responded_at = timezone.now()
        inquiry.status = "replied"
        inquiry.save(update_fields=["response_history", "response_draft", "responded_at", "status"])
    return result


def _parse_message(message):
    from_name, from_email = parseaddr(message.get("From", ""))
    subject = str(make_header(decode_header(message.get("Subject", ""))))
    message_id = (message.get("Message-ID") or "").strip().strip("<>").strip()
    received_at = parsedate_to_datetime(message.get("Date")) if message.get("Date") else timezone.now()
    if received_at and timezone.is_naive(received_at):
        received_at = timezone.make_aware(received_at, timezone.get_current_timezone())
    return {
        "message_id": message_id,
        "from_name": from_name,
        "from_email": from_email,
        "subject": subject,
        "body_text": _extract_body(message),
        "received_at": received_at or timezone.now(),
    }


def _extract_body(message) -> str:
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


def _inquiry_defaults(parsed):
    matched_entity_type, matched_entity_id = _match_sender(parsed["from_email"])
    return {
        "from_email": parsed["from_email"],
        "from_name": parsed["from_name"],
        "subject": parsed["subject"][:255],
        "body_text": parsed["body_text"],
        "received_at": parsed["received_at"],
        "matched_entity_type": matched_entity_type,
        "matched_entity_id": matched_entity_id,
    }


def _match_sender(sender_email: str) -> tuple[str, str]:
    if not sender_email:
        return "", ""
    try:
        user = ExternalUser.objects.get(email__iexact=sender_email)
    except ExternalUser.DoesNotExist:
        return "", ""
    try:
        consultant = ExternalConsultantProfile.objects.get(user_id=user.id)
        return "consultant", str(consultant.id)
    except ExternalConsultantProfile.DoesNotExist:
        pass
    try:
        breeder = ExternalBreederProfile.objects.get(user_id=user.id)
        return "breeder", str(breeder.id)
    except ExternalBreederProfile.DoesNotExist:
        return "user", str(user.id)


def _friendly_mailbox_error(error: str) -> str:
    lowered = (error or "").lower()
    if "authentication failed" in lowered or "login failed" in lowered or "invalid credentials" in lowered:
        return (
            "The inbox login was rejected. Re-check the mailbox credentials, and if this is Microsoft 365 also confirm "
            "that IMAP access is allowed and that MFA, device approval, or an app password is not blocking the sign-in."
        )
    return error
