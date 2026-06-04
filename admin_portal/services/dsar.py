"""Privacy inbox and DSAR workflow helpers."""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from ..models import (
    DSARDeliverable,
    DSAREvent,
    DSARRequest,
    ExternalBreederInquiry,
    ExternalBreederProfile,
    ExternalBreederReview,
    ExternalConsultantBooking,
    ExternalConsultantProfile,
    ExternalConversation,
    ExternalIncidentLog,
    ExternalMessage,
    ExternalPaymentFailureLog,
    ExternalRefund,
    ExternalTrustScoreSnapshot,
    ExternalUser,
    SupportInquiry,
)
from .google_oauth import pick_alias_for_mailbox
from .json_utils import sanitize_json
from .notifier import send_custom_email

DSAR_KEYWORDS = {
    "access": ["dsar", "data subject access", "subject access", "access request", "my data", "personal data"],
    "portability": ["portability", "portable copy", "machine readable"],
    "rectification": ["rectification", "correct my data", "fix my data", "update my data"],
    "erasure": ["erase my data", "delete my data", "remove my data", "erasure", "right to be forgotten"],
    "restriction": ["restrict processing", "restriction of processing"],
    "objection": ["object to processing", "objection to processing"],
}


def ensure_dsar_request_from_inquiry(inquiry: SupportInquiry) -> tuple[DSARRequest | None, bool]:
    if inquiry.mailbox_kind != "privacy":
        return None, False
    request_type = detect_dsar_request_type(inquiry.subject or "", inquiry.body_text or "")
    if not request_type:
        return None, False
    existing = inquiry.dsar_requests.order_by("-created_at").first()
    if existing:
        return existing, False

    matched_user = ExternalUser.objects.filter(email__iexact=inquiry.from_email).first()
    now = timezone.now()
    dsar_request = DSARRequest.objects.create(
        inquiry=inquiry,
        request_type=request_type,
        status="verifying" if matched_user else "unmatched",
        subject_user_id=getattr(matched_user, "id", None),
        submitted_email=inquiry.from_email,
        submitted_name=inquiry.from_name,
        detail=inquiry.body_text[:4000],
        channel="email",
        received_at=inquiry.received_at or now,
        verification_email=matched_user.email if matched_user else "",
    )
    record_dsar_event(
        dsar_request,
        "request_received",
        details={
            "mailbox_kind": inquiry.mailbox_kind,
            "message_id": inquiry.message_id,
            "request_type": request_type,
            "matched_user": str(matched_user.id) if matched_user else "",
        },
    )
    if matched_user:
        raw_token = issue_verification_token(dsar_request)
        verify_url = build_verification_url(raw_token)
        result = send_custom_email(
            subject="Verify your Aqua AI privacy request",
            body=_verification_email_body(dsar_request, verify_url),
            recipients=[matched_user.email],
            from_email=pick_alias_for_mailbox("privacy"),
        )
        record_dsar_event(
            dsar_request,
            "verification_sent",
            details={
                "verification_email": matched_user.email,
                "delivery_ok": result["ok"],
                "delivery_error": result["error"],
            },
        )
    else:
        record_dsar_event(
            dsar_request,
            "unmatched_request",
            details={
                "note": "No matching platform account was confirmed from the request email.",
            },
        )
    return dsar_request, True


def detect_dsar_request_type(subject: str, body: str) -> str:
    text = f"{subject}\n{body}".lower()
    for request_type in ("erasure", "portability", "rectification", "restriction", "objection", "access"):
        if any(token in text for token in DSAR_KEYWORDS[request_type]):
            return request_type
    return "access" if "privacy" in text or "data" in text else ""


def issue_verification_token(dsar_request: DSARRequest) -> str:
    raw_token = secrets.token_urlsafe(24)
    dsar_request.verification_token_hash = _hash_token(raw_token)
    dsar_request.verification_sent_at = timezone.now()
    dsar_request.verification_expires_at = timezone.now() + timedelta(hours=48)
    dsar_request.status = "verifying"
    dsar_request.save(
        update_fields=[
            "verification_token_hash",
            "verification_sent_at",
            "verification_expires_at",
            "status",
            "updated_at",
        ]
    )
    return raw_token


# Maximum credential attempts allowed against a single verification link before
# it is locked. Protects against someone brute-forcing an account password
# through the public verification page.
MAX_VERIFICATION_ATTEMPTS = 6


def attempts_remaining(dsar_request: DSARRequest | None) -> int:
    if not dsar_request:
        return 0
    return max(0, MAX_VERIFICATION_ATTEMPTS - (dsar_request.verification_attempts or 0))


def peek_dsar_token(raw_token: str) -> tuple[DSARRequest | None, str]:
    """Validate a verification link without changing state. Drives the GET form."""
    token_hash = _hash_token(raw_token)
    dsar_request = (
        DSARRequest.objects.filter(verification_token_hash=token_hash)
        .order_by("-created_at")
        .first()
    )
    if not dsar_request:
        return None, "invalid"
    if dsar_request.status == "fulfilled":
        return dsar_request, "already_verified"
    if dsar_request.verification_expires_at and dsar_request.verification_expires_at < timezone.now():
        return dsar_request, "expired"
    if (dsar_request.verification_attempts or 0) >= MAX_VERIFICATION_ATTEMPTS:
        return dsar_request, "locked"
    if not dsar_request.subject_user_id:
        return dsar_request, "unmatched"
    return dsar_request, "ok"


@transaction.atomic
def verify_dsar_credentials(
    raw_token: str, identifier: str, password: str, ip: str = ""
) -> tuple[DSARRequest | None, str]:
    """Cross-match the requester's account email/username + password against the
    platform database before releasing their data. On success the export is
    compiled and (for access/portability requests) emailed automatically."""
    token_hash = _hash_token(raw_token)
    dsar_request = (
        DSARRequest.objects.select_for_update()
        .filter(verification_token_hash=token_hash)
        .order_by("-created_at")
        .first()
    )
    if not dsar_request:
        return None, "invalid"
    if dsar_request.status == "fulfilled":
        return dsar_request, "already_verified"
    if dsar_request.verification_expires_at and dsar_request.verification_expires_at < timezone.now():
        return dsar_request, "expired"
    if (dsar_request.verification_attempts or 0) >= MAX_VERIFICATION_ATTEMPTS:
        return dsar_request, "locked"
    if not dsar_request.subject_user_id:
        return dsar_request, "unmatched"

    subject_user = ExternalUser.objects.filter(id=dsar_request.subject_user_id).first()
    identifier = (identifier or "").strip()
    identifier_ok = bool(subject_user) and identifier.lower() in {
        (subject_user.email or "").lower(),
        (subject_user.username or "").lower(),
    }
    # check_password safely verifies the raw input against the stored
    # pbkdf2_sha256 hash; a plaintext or unrecognised stored value simply fails.
    password_ok = bool(subject_user) and bool(password) and check_password(
        password, subject_user.password or ""
    )

    if not (identifier_ok and password_ok):
        dsar_request.verification_attempts = (dsar_request.verification_attempts or 0) + 1
        dsar_request.save(update_fields=["verification_attempts", "updated_at"])
        record_dsar_event(
            dsar_request,
            "verification_failed",
            details={
                # Never store the submitted password; only the failure reason.
                "reason": "identifier_mismatch" if not identifier_ok else "password_mismatch",
                "attempts": dsar_request.verification_attempts,
                "ip": ip or "",
            },
        )
        if dsar_request.verification_attempts >= MAX_VERIFICATION_ATTEMPTS:
            return dsar_request, "locked"
        return dsar_request, "invalid_credentials"

    dsar_request.status = "verified"
    dsar_request.verified_at = timezone.now()
    dsar_request.save(update_fields=["status", "verified_at", "updated_at"])
    record_dsar_event(dsar_request, "verified", details={"method": "credentials", "ip": ip or ""})

    prepare_dsar_request(dsar_request, actor=None)
    dsar_request.refresh_from_db()

    if dsar_request.status == "awaiting_dpo_approval" and dsar_request.deliverables.exists():
        result = approve_and_send_dsar(dsar_request, actor=None)
        return dsar_request, "fulfilled" if result["ok"] else "send_failed"
    # Erasure / rectification / restriction / objection cannot be auto-fulfilled
    # with a data package — they are queued for manual DPO handling.
    return dsar_request, "verified_manual"


def prepare_dsar_request(dsar_request: DSARRequest, actor=None) -> DSARRequest:
    if not dsar_request.subject_user_id:
        raise RuntimeError("This data request is not linked to a platform user yet.")
    if dsar_request.request_type not in {"access", "portability"}:
        dsar_request.status = "in_progress"
        dsar_request.save(update_fields=["status", "updated_at"])
        record_dsar_event(dsar_request, "queued_for_manual_handling", actor=actor, details={"request_type": dsar_request.request_type})
        return dsar_request

    subject_user = ExternalUser.objects.get(id=dsar_request.subject_user_id)
    export_bundle = build_subject_export(subject_user)
    runtime_dir = Path(settings.BASE_DIR) / "runtime_exports" / "dsar" / str(dsar_request.id)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    json_path = runtime_dir / "aquaai-dsar-export.json"
    html_path = runtime_dir / "aquaai-dsar-export.html"

    json_path.write_text(json.dumps(export_bundle, indent=2, default=str), encoding="utf-8")
    html_path.write_text(_render_export_html(export_bundle), encoding="utf-8")

    expires_at = timezone.now() + timedelta(days=7)
    _upsert_deliverable(dsar_request, "access_export_json", json_path, "application/json", expires_at)
    _upsert_deliverable(dsar_request, "access_export_html", html_path, "text/html", expires_at)

    dsar_request.export_summary = sanitize_json(export_bundle.get("summary", {}))
    dsar_request.status = "awaiting_dpo_approval"
    dsar_request.save(update_fields=["export_summary", "status", "updated_at"])
    record_dsar_event(
        dsar_request,
        "export_prepared",
        actor=actor,
        details={"deliverables": [str(json_path), str(html_path)]},
    )
    return dsar_request


def approve_and_send_dsar(dsar_request: DSARRequest, actor) -> dict[str, str | bool]:
    if not dsar_request.deliverables.exists():
        prepare_dsar_request(dsar_request, actor=actor)
        dsar_request.refresh_from_db()

    attachments = [
        {
            "path": deliverable.storage_ref,
            "filename": deliverable.file_name or Path(deliverable.storage_ref).name,
            "mime_type": deliverable.mime_type,
        }
        for deliverable in dsar_request.deliverables.all()
    ]
    recipient = dsar_request.verification_email or dsar_request.submitted_email
    result = send_custom_email(
        subject="Your Aqua AI data request package",
        body=_fulfilment_email_body(dsar_request),
        recipients=[recipient],
        from_email=pick_alias_for_mailbox("privacy"),
        attachments=attachments,
    )
    record_dsar_event(
        dsar_request,
        "fulfilment_sent" if result["ok"] else "fulfilment_failed",
        actor=actor,
        details={"recipient": recipient, "error": result["error"]},
    )
    if result["ok"]:
        dsar_request.status = "fulfilled"
        dsar_request.fulfilled_at = timezone.now()
        dsar_request.dpo_actioned_at = timezone.now()
        dsar_request.dpo_actor = actor
        dsar_request.save(update_fields=["status", "fulfilled_at", "dpo_actioned_at", "dpo_actor", "updated_at"])
    return result


def reject_dsar_request(dsar_request: DSARRequest, actor, reason: str) -> None:
    dsar_request.status = "rejected"
    dsar_request.dpo_actioned_at = timezone.now()
    dsar_request.dpo_actor = actor
    dsar_request.save(update_fields=["status", "dpo_actioned_at", "dpo_actor", "updated_at"])
    record_dsar_event(dsar_request, "rejected", actor=actor, details={"reason": reason})


def extend_dsar_request(dsar_request: DSARRequest, actor, reason: str, days: int = 30) -> None:
    dsar_request.extended = True
    dsar_request.extension_reason = reason
    dsar_request.due_at = (dsar_request.due_at or timezone.now()) + timedelta(days=days)
    dsar_request.status = "extended"
    dsar_request.dpo_actioned_at = timezone.now()
    dsar_request.dpo_actor = actor
    dsar_request.save(
        update_fields=[
            "extended",
            "extension_reason",
            "due_at",
            "status",
            "dpo_actioned_at",
            "dpo_actor",
            "updated_at",
        ]
    )
    record_dsar_event(dsar_request, "extended", actor=actor, details={"reason": reason, "days": days})


def build_subject_export(subject_user: ExternalUser) -> dict:
    breeder_profile = ExternalBreederProfile.objects.filter(user_id=subject_user.id).first()
    consultant_profile = ExternalConsultantProfile.objects.filter(user_id=subject_user.id).first()
    incidents = ExternalIncidentLog.objects.filter(user_id=subject_user.id).order_by("-created_at")
    trust_snapshots = ExternalTrustScoreSnapshot.objects.filter(user_id=subject_user.id).order_by("-calculated_at")
    payment_failures = ExternalPaymentFailureLog.objects.filter(user_id=subject_user.id).order_by("-created_at")
    refunds = ExternalRefund.objects.filter(payment_intent_id__in=list(payment_failures.values_list("payment_intent_id", flat=True))).order_by("-created_at")
    conversations = ExternalConversation.objects.filter(participant_1_id=subject_user.id) | ExternalConversation.objects.filter(participant_2_id=subject_user.id)
    conversations = conversations.order_by("-updated_at")
    messages = ExternalMessage.objects.filter(conversation_id__in=list(conversations.values_list("id", flat=True))).order_by("-created_at")
    support_enquiries = SupportInquiry.objects.filter(from_email__iexact=subject_user.email).order_by("-received_at")

    breeder_reviews = ExternalBreederReview.objects.none()
    breeder_inquiries = ExternalBreederInquiry.objects.none()
    consultant_bookings = ExternalConsultantBooking.objects.none()
    if breeder_profile:
        breeder_reviews = ExternalBreederReview.objects.filter(breeder_id=breeder_profile.id).order_by("-created_at")
        breeder_inquiries = ExternalBreederInquiry.objects.filter(
            breeder_id=breeder_profile.id
        ).order_by("-created_at")
    if consultant_profile:
        consultant_bookings = ExternalConsultantBooking.objects.filter(
            consultant_id=consultant_profile.id
        ).order_by("-created_at")
    requester_bookings = ExternalConsultantBooking.objects.filter(requester_id=subject_user.id).order_by("-created_at")

    bundle = {
        "generated_at": timezone.now().isoformat(),
        "subject": _serialize_instance(subject_user),
        "profiles": {
            "breeder_profile": _serialize_instance(breeder_profile) if breeder_profile else None,
            "consultant_profile": _serialize_instance(consultant_profile) if consultant_profile else None,
        },
        "regulatory_and_trust": {
            "incidents": [_serialize_instance(item) for item in incidents],
            "trust_snapshots": [_serialize_instance(item) for item in trust_snapshots],
        },
        "commercial_and_support": {
            "payment_failures": [_serialize_instance(item) for item in payment_failures],
            "refunds": [_serialize_instance(item) for item in refunds],
            "support_inbox_messages": [_serialize_support_inquiry(item) for item in support_enquiries],
        },
        "provider_activity": {
            "breeder_reviews": [_serialize_instance(item) for item in breeder_reviews],
            "breeder_inquiries": [_serialize_breeder_inquiry(item, subject_user.id, breeder_profile.id if breeder_profile else None) for item in breeder_inquiries],
            "consultant_bookings": [_serialize_booking(item, subject_user.id, consultant_profile.id if consultant_profile else None) for item in consultant_bookings],
            "requested_bookings": [_serialize_booking(item, subject_user.id, consultant_profile.id if consultant_profile else None) for item in requester_bookings],
        },
        "messaging": {
            "conversations": [_serialize_conversation(item, subject_user.id) for item in conversations],
            "messages": [_serialize_message(item, subject_user.id) for item in messages],
        },
        "notes": [
            "This export was assembled from the shared Aqua AI application database available to the control plane.",
            "Third-party or counterparty identifiers have been redacted where disclosure would expose other users' personal data.",
            "Any external processor data not held in the shared application database may require a separate downstream fulfilment step.",
        ],
    }
    bundle["summary"] = {
        "incident_count": len(bundle["regulatory_and_trust"]["incidents"]),
        "trust_snapshot_count": len(bundle["regulatory_and_trust"]["trust_snapshots"]),
        "support_message_count": len(bundle["commercial_and_support"]["support_inbox_messages"]),
        "conversation_count": len(bundle["messaging"]["conversations"]),
        "message_count": len(bundle["messaging"]["messages"]),
        "booking_count": len(bundle["provider_activity"]["consultant_bookings"]) + len(bundle["provider_activity"]["requested_bookings"]),
    }
    return sanitize_json(bundle)


def record_dsar_event(dsar_request: DSARRequest, action: str, actor=None, details: dict | None = None) -> DSAREvent:
    return DSAREvent.objects.create(
        request=dsar_request,
        action=action,
        actor=actor,
        details=sanitize_json(details or {}),
    )


def build_verification_url(raw_token: str) -> str:
    path = reverse("admin_portal:dsar_verify", args=[raw_token])
    base = (getattr(settings, "LEGACY_ADMIN_REDIRECT_URL", "") or "").rstrip("/")
    if base:
        return f"{base}{path}"
    return path


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _upsert_deliverable(
    dsar_request: DSARRequest,
    artefact_type: str,
    path: Path,
    mime_type: str,
    expires_at,
) -> None:
    DSARDeliverable.objects.update_or_create(
        request=dsar_request,
        artefact_type=artefact_type,
        defaults={
            "storage_ref": str(path),
            "file_name": path.name,
            "mime_type": mime_type,
            "generated_at": timezone.now(),
            "expires_at": expires_at,
        },
    )


def _serialize_instance(instance):
    if instance is None:
        return None
    data = {}
    for field in instance._meta.fields:
        name = field.attname if field.is_relation else field.name
        data[field.name] = getattr(instance, name)
    return sanitize_json(data)


def _serialize_conversation(conversation: ExternalConversation, subject_user_id):
    payload = _serialize_instance(conversation)
    payload["participant_1_id"] = str(subject_user_id) if conversation.participant_1_id == subject_user_id else "redacted"
    payload["participant_2_id"] = str(subject_user_id) if conversation.participant_2_id == subject_user_id else "redacted"
    return payload


def _serialize_message(message: ExternalMessage, subject_user_id):
    payload = _serialize_instance(message)
    payload["sender_id"] = str(subject_user_id) if message.sender_id == subject_user_id else "redacted"
    payload["sender_context"] = "subject" if message.sender_id == subject_user_id else "counterparty_redacted"
    return payload


def _serialize_breeder_inquiry(inquiry: ExternalBreederInquiry, subject_user_id, breeder_profile_id):
    payload = _serialize_instance(inquiry)
    if inquiry.user_id != subject_user_id:
        payload["user_id"] = "redacted"
    if breeder_profile_id and inquiry.breeder_id != breeder_profile_id:
        payload["breeder_id"] = "redacted"
    return payload


def _serialize_booking(booking: ExternalConsultantBooking, subject_user_id, consultant_profile_id):
    payload = _serialize_instance(booking)
    if booking.requester_id != subject_user_id:
        payload["requester_id"] = "redacted"
    if consultant_profile_id and booking.consultant_id != consultant_profile_id:
        payload["consultant_id"] = "redacted"
    return payload


def _serialize_support_inquiry(inquiry: SupportInquiry):
    payload = {
        "id": inquiry.id,
        "mailbox_kind": inquiry.mailbox_kind,
        "from_email": inquiry.from_email,
        "subject": inquiry.subject,
        "received_at": inquiry.received_at,
        "status": inquiry.status,
        "response_history": inquiry.response_history,
    }
    return sanitize_json(payload)


def _verification_email_body(dsar_request: DSARRequest, verify_url: str) -> str:
    return (
        "We received a privacy or data request for your Aqua AI account.\n\n"
        "To protect your personal data, open the secure link below and confirm the email or "
        "username and password of your Aqua AI account. We verify these against your account "
        "before any data is released:\n"
        f"{verify_url}\n\n"
        "This link expires in 48 hours. If you did not submit this request, you can ignore this message."
    )


def _fulfilment_email_body(dsar_request: DSARRequest) -> str:
    return (
        "Your Aqua AI privacy request has been fulfilled.\n\n"
        "We have attached the current export package available from the shared platform database.\n"
        "Please keep these files secure and contact privacy@aquaai.uk if you need anything clarified."
    )


def _render_export_html(bundle: dict) -> str:
    pretty = json.dumps(bundle, indent=2)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Aqua AI DSAR Export</title>"
        "<style>body{font-family:Arial,sans-serif;background:#f6f8fb;color:#10243c;padding:32px;}"
        "pre{white-space:pre-wrap;background:#fff;border:1px solid #dce4f0;border-radius:12px;padding:20px;}"
        "h1{margin-top:0;} p{max-width:780px;line-height:1.6;}</style></head><body>"
        "<h1>Aqua AI Data Request Export</h1>"
        "<p>This package was generated from the Aqua AI shared application database. "
        "Counterparty identifiers are redacted where needed to protect other users' personal data.</p>"
        f"<pre>{pretty}</pre></body></html>"
    )
