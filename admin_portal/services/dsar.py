"""Privacy inbox and DSAR workflow helpers."""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Q
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
from .platform_sessions import (
    active_session_keys_for_user,
    has_jwt_login_since,
    has_new_login_since_baseline,
)
from .runtime_config import get_operational_settings
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
        issue_verification_token(dsar_request)
        # Snapshot the sessions the subject already has, so a later *new* session
        # is recognised as a fresh login made in response to this request.
        dsar_request.login_baseline_keys = sorted(active_session_keys_for_user(matched_user.id))
        dsar_request.save(update_fields=["login_baseline_keys", "updated_at"])
        result = send_custom_email(
            subject="Verify your Aqua AI privacy request",
            body=_verification_email_body(dsar_request, settings.PLATFORM_LOGIN_URL),
            recipients=[matched_user.email],
            from_email=pick_alias_for_mailbox("privacy"),
        )
        record_dsar_event(
            dsar_request,
            "verification_sent",
            details={
                "verification_email": matched_user.email,
                "login_url": settings.PLATFORM_LOGIN_URL,
                "baseline_sessions": len(dsar_request.login_baseline_keys),
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


def _candidate_user_ids_for_request(dsar_request: DSARRequest) -> list:
    """Platform user ids whose login can confirm this request: the linked subject
    plus every account that shares the requester's verified email. Handles the
    common case of one person having duplicate accounts under a single email."""
    ids: list = []
    seen = set()
    if dsar_request.subject_user_id and dsar_request.subject_user_id not in seen:
        ids.append(dsar_request.subject_user_id)
        seen.add(dsar_request.subject_user_id)
    email = (dsar_request.verification_email or dsar_request.submitted_email or "").strip()
    if email:
        for user_id in (
            ExternalUser.objects.filter(email__iexact=email).values_list("id", flat=True)
        ):
            if user_id not in seen:
                ids.append(user_id)
                seen.add(user_id)
    return ids


def _subject_accounts_for_request(dsar_request: DSARRequest) -> list:
    """All platform accounts whose data belongs to this DSAR subject: the linked
    account plus every account sharing the verified email (same natural person).
    The linked subject is returned first as the primary account."""
    user_ids = _candidate_user_ids_for_request(dsar_request)
    users = list(ExternalUser.objects.filter(id__in=user_ids))
    users.sort(key=lambda u: 0 if u.id == dsar_request.subject_user_id else 1)
    return users


@transaction.atomic
def check_dsar_login(dsar_request: DSARRequest) -> bool:
    """Confirm identity by detecting a fresh aquaai.uk login for the subject.

    Returns True if the request is (now or already) login-confirmed. Looks for a
    platform session that did not exist when the request was raised, within the
    48-hour verification window. Never sends data — only flips the gate."""
    if dsar_request.login_confirmed_at:
        return True
    if not dsar_request.subject_user_id:
        return False
    if dsar_request.status in ("fulfilled", "rejected", "withdrawn"):
        return False
    # Enforce the window: only confirm while the verification link is valid.
    if dsar_request.verification_expires_at and dsar_request.verification_expires_at < timezone.now():
        return False

    # Identity is proven by a fresh login made AFTER we asked them to: either a
    # new web session (django_session) or a new JWT issued after the verification
    # email went out. The platform is JWT-based, so the JWT signal is the one that
    # fires for mobile-app logins — a django_session row is never created for them.
    #
    # A login on ANY account that shares the requester's verified email counts:
    # the same person frequently has duplicate accounts under one email, and the
    # DSAR is linked to only one of them. Requiring the login to land on that exact
    # account would leave genuine confirmations stuck.
    since = dsar_request.verification_sent_at or dsar_request.received_at
    candidate_user_ids = _candidate_user_ids_for_request(dsar_request)
    login_via = ""
    confirmed_user_id = None
    for user_id in candidate_user_ids:
        if has_jwt_login_since(user_id, since):
            login_via = "aquaai_jwt"
            confirmed_user_id = user_id
            break
    if not confirmed_user_id and has_new_login_since_baseline(
        dsar_request.subject_user_id, dsar_request.login_baseline_keys
    ):
        login_via = "aquaai_session"
        confirmed_user_id = dsar_request.subject_user_id
    if not confirmed_user_id:
        return False

    now = timezone.now()
    subject_user = ExternalUser.objects.filter(id=dsar_request.subject_user_id).first()
    dsar_request.login_confirmed_at = now
    dsar_request.login_confirmed_email = (
        getattr(subject_user, "email", "") or dsar_request.verification_email or ""
    )[:254]
    dsar_request.verified_at = dsar_request.verified_at or now
    if dsar_request.status in ("received", "verifying", "unmatched"):
        dsar_request.status = "verified"
    dsar_request.save(
        update_fields=["login_confirmed_at", "login_confirmed_email", "verified_at", "status", "updated_at"]
    )
    record_dsar_event(
        dsar_request,
        "login_confirmed",
        details={
            "via": login_via,
            "subject_user_id": str(dsar_request.subject_user_id),
            "confirmed_via_user_id": str(confirmed_user_id),
        },
    )

    # Full automation: once identity is confirmed, compile and email the data
    # package automatically for access/portability requests (toggleable).
    if (
        get_operational_settings().dsar_auto_send
        and dsar_request.request_type in {"access", "portability"}
    ):
        try:
            result = approve_and_send_dsar(dsar_request, actor=None)
            record_dsar_event(
                dsar_request,
                "auto_send" if result["ok"] else "auto_send_failed",
                details={"error": result.get("error", "")},
            )
        except Exception as exc:  # pragma: no cover - defensive; admin can resend
            record_dsar_event(dsar_request, "auto_send_failed", details={"error": str(exc)})
    return True


def run_due_login_checks() -> tuple[int, int]:
    """Check every pending DSAR for a fresh aquaai.uk login. Returns
    (checked, confirmed). Shared by the management command and worker loop."""
    pending = DSARRequest.objects.filter(
        login_confirmed_at__isnull=True,
        subject_user_id__isnull=False,
        verification_expires_at__gte=timezone.now(),
    ).exclude(status__in=["fulfilled", "rejected", "withdrawn"])
    checked = 0
    confirmed = 0
    for dsar_request in pending.iterator():
        checked += 1
        try:
            if check_dsar_login(dsar_request):
                confirmed += 1
        except Exception:  # pragma: no cover - defensive, keep the loop alive
            pass
    return checked, confirmed


def prepare_dsar_request(dsar_request: DSARRequest, actor=None) -> DSARRequest:
    if not dsar_request.subject_user_id:
        raise RuntimeError("This data request is not linked to a platform user yet.")
    if dsar_request.request_type not in {"access", "portability"}:
        dsar_request.status = "in_progress"
        dsar_request.save(update_fields=["status", "updated_at"])
        record_dsar_event(dsar_request, "queued_for_manual_handling", actor=actor, details={"request_type": dsar_request.request_type})
        return dsar_request

    # A person may hold several accounts under one verified email (e.g. a standard
    # user account and a separate provider account). Their data is spread across
    # all of them, so the DSAR export must span every account — exporting only the
    # linked one would return an incomplete response.
    subject_users = _subject_accounts_for_request(dsar_request)
    primary_user = next(
        (u for u in subject_users if u.id == dsar_request.subject_user_id),
        subject_users[0] if subject_users else None,
    )
    if primary_user is None:
        primary_user = ExternalUser.objects.get(id=dsar_request.subject_user_id)
    related_users = [u for u in subject_users if u.id != primary_user.id]
    export_bundle = build_subject_export(primary_user, related_users=related_users)
    runtime_dir = Path(settings.BASE_DIR) / "runtime_exports" / "dsar" / str(dsar_request.id)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = runtime_dir / "aquaai-data-export.pdf"
    json_path = runtime_dir / "aquaai-data-export.json"

    pdf_path.write_bytes(_render_export_pdf(export_bundle))
    # JSON kept for the data-portability right (machine-readable copy).
    json_path.write_text(json.dumps(export_bundle, indent=2, default=str), encoding="utf-8")

    expires_at = timezone.now() + timedelta(days=7)
    _upsert_deliverable(dsar_request, "access_export_pdf", pdf_path, "application/pdf", expires_at)
    _upsert_deliverable(dsar_request, "access_export_json", json_path, "application/json", expires_at)

    dsar_request.export_summary = sanitize_json(export_bundle.get("summary", {}))
    dsar_request.status = "awaiting_dpo_approval"
    dsar_request.save(update_fields=["export_summary", "status", "updated_at"])
    record_dsar_event(
        dsar_request,
        "export_prepared",
        actor=actor,
        details={
            "deliverables": [str(pdf_path), str(json_path)],
            "merged_account_ids": [str(u.id) for u in subject_users],
            "merged_account_count": len(subject_users),
        },
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


def build_subject_export(subject_user: ExternalUser, related_users: list | None = None) -> dict:
    """Assemble the personal-data export for a subject.

    ``related_users`` are additional accounts belonging to the same person (e.g.
    duplicate accounts under one verified email). All of their data is merged into
    a single package so the DSAR response is complete; ``subject_user`` is treated
    as the primary account. Counterparty identifiers are redacted against the full
    set of the subject's own accounts, so the subject's own ids are never redacted."""
    users = [subject_user] + [u for u in (related_users or []) if u and u.id != subject_user.id]
    user_ids = {u.id for u in users}
    emails = {u.email for u in users if u.email}

    breeder_profiles = list(ExternalBreederProfile.objects.filter(user_id__in=user_ids).order_by("-created_at"))
    consultant_profiles = list(ExternalConsultantProfile.objects.filter(user_id__in=user_ids).order_by("-created_at"))
    breeder_profile_ids = {p.id for p in breeder_profiles}
    consultant_profile_ids = {p.id for p in consultant_profiles}

    incidents = ExternalIncidentLog.objects.filter(user_id__in=user_ids).order_by("-created_at")
    trust_snapshots = ExternalTrustScoreSnapshot.objects.filter(user_id__in=user_ids).order_by("-calculated_at")
    payment_failures = ExternalPaymentFailureLog.objects.filter(user_id__in=user_ids).order_by("-created_at")
    refunds = ExternalRefund.objects.filter(payment_intent_id__in=list(payment_failures.values_list("payment_intent_id", flat=True))).order_by("-created_at")
    conversations = (
        ExternalConversation.objects.filter(participant_1_id__in=user_ids)
        | ExternalConversation.objects.filter(participant_2_id__in=user_ids)
    ).order_by("-updated_at").distinct()
    messages = ExternalMessage.objects.filter(conversation_id__in=list(conversations.values_list("id", flat=True))).order_by("-created_at")
    support_enquiries = SupportInquiry.objects.none()
    if emails:
        email_q = Q()
        for email in emails:
            email_q |= Q(from_email__iexact=email)
        support_enquiries = SupportInquiry.objects.filter(email_q).order_by("-received_at")

    breeder_reviews = ExternalBreederReview.objects.none()
    breeder_inquiries = ExternalBreederInquiry.objects.none()
    consultant_bookings = ExternalConsultantBooking.objects.none()
    if breeder_profile_ids:
        breeder_reviews = ExternalBreederReview.objects.filter(breeder_id__in=breeder_profile_ids).order_by("-created_at")
        breeder_inquiries = ExternalBreederInquiry.objects.filter(breeder_id__in=breeder_profile_ids).order_by("-created_at")
    if consultant_profile_ids:
        consultant_bookings = ExternalConsultantBooking.objects.filter(consultant_id__in=consultant_profile_ids).order_by("-created_at")
    requester_bookings = ExternalConsultantBooking.objects.filter(requester_id__in=user_ids).order_by("-created_at")

    bundle = {
        "generated_at": timezone.now().isoformat(),
        "subject": _serialize_instance(subject_user),
        "profiles": {
            "breeder_profiles": [_serialize_instance(item) for item in breeder_profiles],
            "consultant_profiles": [_serialize_instance(item) for item in consultant_profiles],
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
            "breeder_inquiries": [_serialize_breeder_inquiry(item, user_ids, breeder_profile_ids) for item in breeder_inquiries],
            "consultant_bookings": [_serialize_booking(item, user_ids, consultant_profile_ids) for item in consultant_bookings],
            "requested_bookings": [_serialize_booking(item, user_ids, consultant_profile_ids) for item in requester_bookings],
        },
        "messaging": {
            "conversations": [_serialize_conversation(item, user_ids) for item in conversations],
            "messages": [_serialize_message(item, user_ids) for item in messages],
        },
        "notes": [
            "This export was assembled from the shared Aqua AI application database available to the control plane.",
            "Third-party or counterparty identifiers have been redacted where disclosure would expose other users' personal data.",
            "Any external processor data not held in the shared application database may require a separate downstream fulfilment step.",
        ],
    }
    # When the person holds more than one account under their email, list them all
    # and merge their data so the response is complete.
    if len(users) > 1:
        bundle["additional_accounts"] = [_serialize_instance(u) for u in users if u.id != subject_user.id]
        bundle["notes"].insert(
            0,
            (
                f"This export combines {len(users)} accounts registered under your email "
                f"({', '.join(sorted(emails)) or 'your email'}); all data held across them is included."
            ),
        )
    bundle["summary"] = {
        "account_count": len(users),
        "breeder_profile_count": len(breeder_profiles),
        "consultant_profile_count": len(consultant_profiles),
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


def build_verification_url(raw_token: str = "") -> str:
    """The aquaai.uk login link placed in the verification email."""
    return getattr(settings, "PLATFORM_LOGIN_URL", "") or "https://aquaai.uk/login"


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


def _serialize_conversation(conversation: ExternalConversation, subject_ids):
    payload = _serialize_instance(conversation)
    payload["participant_1_id"] = str(conversation.participant_1_id) if conversation.participant_1_id in subject_ids else "redacted"
    payload["participant_2_id"] = str(conversation.participant_2_id) if conversation.participant_2_id in subject_ids else "redacted"
    return payload


def _serialize_message(message: ExternalMessage, subject_ids):
    payload = _serialize_instance(message)
    is_subject = message.sender_id in subject_ids
    payload["sender_id"] = str(message.sender_id) if is_subject else "redacted"
    payload["sender_context"] = "subject" if is_subject else "counterparty_redacted"
    return payload


def _serialize_breeder_inquiry(inquiry: ExternalBreederInquiry, subject_ids, breeder_profile_ids):
    payload = _serialize_instance(inquiry)
    if inquiry.user_id not in subject_ids:
        payload["user_id"] = "redacted"
    if breeder_profile_ids and inquiry.breeder_id not in breeder_profile_ids:
        payload["breeder_id"] = "redacted"
    return payload


def _serialize_booking(booking: ExternalConsultantBooking, subject_ids, consultant_profile_ids):
    payload = _serialize_instance(booking)
    if booking.requester_id not in subject_ids:
        payload["requester_id"] = "redacted"
    if consultant_profile_ids and booking.consultant_id not in consultant_profile_ids:
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
        "To protect your personal data, please confirm it is you by logging in to your "
        "Aqua AI account at the link below within the next 48 hours. Once we see that you have "
        "signed in successfully, our privacy team will verify and release your data:\n"
        f"{verify_url}\n\n"
        "If you did not submit this request, you can ignore this message and no data will be shared."
    )


def _fulfilment_email_body(dsar_request: DSARRequest) -> str:
    return (
        "Your Aqua AI privacy request has been fulfilled.\n\n"
        "Attached is your personal data export as a PDF, along with a machine-readable JSON copy.\n"
        "Please keep these files secure and contact privacy@aquaai.uk if you need anything clarified."
    )


# Friendly section titles for the PDF. Internal storage/table names are never
# used — only these human-readable labels and humanised field names.
_SECTION_TITLES = {
    "subject": "Your account",
    "additional_accounts": "Other accounts registered to you",
    "profiles": "Your profiles",
    "regulatory_and_trust": "Trust & regulatory record",
    "commercial_and_support": "Payments & support history",
    "provider_activity": "Marketplace & provider activity",
    "messaging": "Messages & conversations",
}
_FIELD_LABEL_OVERRIDES = {
    "id": "Reference",
    "current_trust_score": "Trust score",
    "current_regulatory_tier": "Regulatory tier",
}
# Fields that are internal plumbing and add no value for the data subject.
_FIELD_HIDE = {"password", "_state"}


def _humanize(key: str) -> str:
    return _FIELD_LABEL_OVERRIDES.get(key, key.replace("_", " ").strip().capitalize())


def _render_export_pdf(bundle: dict) -> bytes:
    """Render the export bundle as a clean, human-readable PDF.

    Nested records and lists are laid out as indented sub-sections and tables
    (no raw JSON). Uses friendly section titles and humanised field labels only —
    no database or table names are ever written into the document.
    """
    from io import BytesIO

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        HRFlowable,
        Indenter,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
        title="Aqua AI Data Export",
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=18, spaceAfter=4, textColor=colors.HexColor("#0c2233"))
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=13, spaceBefore=14, spaceAfter=6, textColor=colors.HexColor("#0c2233"))
    body = ParagraphStyle("body", parent=styles["Normal"], fontSize=9.5, leading=14, alignment=TA_LEFT)
    label = ParagraphStyle("label", parent=body, fontName="Helvetica-Bold", spaceBefore=4)
    meta = ParagraphStyle("meta", parent=body, textColor=colors.HexColor("#5a6b7a"))

    INDENT = 12

    def esc(value) -> str:
        text = "" if value is None else str(value)
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def is_scalar(v) -> bool:
        return not isinstance(v, (dict, list))

    flow: list = []
    flow.append(Paragraph("Aqua AI — Personal Data Export", h1))
    flow.append(Paragraph(
        "This document contains the personal data Aqua AI holds about you. Where information about "
        "other people would otherwise be revealed, it has been redacted to protect their privacy.",
        meta,
    ))
    flow.append(Paragraph(f"Generated: {esc(bundle.get('generated_at', ''))}", meta))
    flow.append(Spacer(1, 6))
    flow.append(HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#cdd8e3")))

    def scalar_table(pairs):
        rows = [[Paragraph(esc(_humanize(k)), body), Paragraph(esc(v), body)] for k, v in pairs]
        table = Table(rows, colWidths=[52 * mm, 108 * mm])
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#e6ecf2")),
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f4f7fa")),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        flow.append(table)
        flow.append(Spacer(1, 5))

    def add_record(record: dict):
        # Scalar fields grouped into one table; complex fields recursed below.
        scalars = [(k, v) for k, v in record.items() if k not in _FIELD_HIDE and is_scalar(v)]
        complexes = [(k, v) for k, v in record.items() if k not in _FIELD_HIDE and not is_scalar(v)]
        if scalars:
            scalar_table(scalars)
        for key, val in complexes:
            flow.append(Paragraph(_humanize(key), label))
            flow.append(Indenter(left=INDENT))
            add_value(val)
            flow.append(Indenter(left=-INDENT))

    def add_value(value):
        if isinstance(value, dict):
            if value:
                add_record(value)
            else:
                flow.append(Paragraph("None on record.", meta))
        elif isinstance(value, list):
            if not value:
                flow.append(Paragraph("None on record.", meta))
                flow.append(Spacer(1, 3))
                return
            for idx, item in enumerate(value, 1):
                if isinstance(item, dict):
                    flow.append(Paragraph(f"Entry {idx}", label))
                    flow.append(Indenter(left=INDENT))
                    add_record(item)
                    flow.append(Indenter(left=-INDENT))
                else:
                    flow.append(Paragraph(f"• {esc(item)}", body))
        else:
            flow.append(Paragraph(esc(value), body))

    summary = bundle.get("summary") or {}
    if summary:
        flow.append(Paragraph("Summary", h2))
        add_record(summary)

    for section_key, title in _SECTION_TITLES.items():
        if section_key not in bundle:
            continue
        flow.append(Paragraph(title, h2))
        add_value(bundle[section_key])

    notes = bundle.get("notes") or []
    if notes:
        flow.append(Paragraph("Notes", h2))
        for note in notes:
            flow.append(Paragraph(f"• {esc(note)}", body))

    doc.build(flow)
    return buf.getvalue()
