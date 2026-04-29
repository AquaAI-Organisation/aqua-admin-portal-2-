"""Hybrid intelligence adapter for richer signup and issue signals.

The control plane remains the source-of-truth UI and audit surface. This
adapter assembles higher-signal summaries from the shared backend tables so the
OpenAI scorer can work with better evidence and so periodic issue scans can
cover more than just incidents and consultant warnings.

Important: this is a shared-database hybrid integration, not a direct runtime
import of the external Aqua Intelligence repo. The admin app mirrors the most
useful signals locally so operations remain stable even when the external
service codebase evolves independently.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db.models import Avg, Count, Q

from ..models import (
    ExternalBreederInquiry,
    ExternalBreederProfile,
    ExternalBreederReview,
    ExternalConsultantProfile,
    ExternalConsultantBooking,
    ExternalConversation,
    ExternalIncidentLog,
    ExternalMessage,
    ExternalPaymentFailureLog,
    ExternalRefund,
    ExternalTrustScoreSnapshot,
    ExternalUser,
)


INTELLIGENCE_KEYWORDS = {
    "off_platform_contact": ["whatsapp", "telegram", "text me", "email me", "dm me", "call me"],
    "payment_bypass": ["bank transfer", "paypal friends", "cashapp", "zelle", "crypto", "wire"],
    "urgency_pressure": ["urgent", "immediately", "right now", "asap"],
}
SUSPICIOUS_IDENTITY_TOKENS = ("test", "fake", "spam", "asdf", "qwerty", "demo")
INTERNAL_ROLES = {"superadmin", "admin", "staff", "developer"}


@dataclass
class IssueCandidate:
    source_type: str
    source_id: str
    title: str
    subject_type: str
    user: ExternalUser
    profile: Any
    payload: dict[str, Any]


def get_intelligence_mode() -> str:
    return getattr(settings, "INTELLIGENCE_MODE", "hybrid")


def get_intelligence_readiness() -> dict[str, str | bool]:
    mode = get_intelligence_mode()
    if mode != "hybrid":
        return {"ok": True, "state": "warn", "detail": f"INTELLIGENCE_MODE is set to {mode}; hybrid signals are reduced."}
    return {
        "ok": True,
        "state": "ok",
        "detail": "Hybrid intelligence is active using shared-database trust, review, dispute, messaging, and payment signals.",
    }


def build_signup_intelligence(subject_type: str, profile, user: ExternalUser) -> dict[str, Any]:
    trust_snapshot = (
        ExternalTrustScoreSnapshot.objects
        .filter(user_id=user.id)
        .order_by("-calculated_at")
        .first()
    )
    incidents = list(
        ExternalIncidentLog.objects
        .filter(user_id=user.id, is_cleared=False)
        .order_by("-occurred_at", "-created_at")[:5]
    )
    message_signal = _message_signal_for_user(user.id, limit=30)
    payment_signal = _payment_signal_for_user(user.id)
    network_signal = _network_signal_for_user(user.id)
    identity_signal = _identity_signal(profile, user)
    role_signal = _role_fit_signal(subject_type, profile)

    if subject_type == "breeder":
        role_metrics = _breeder_metrics(profile)
        review_signal = _review_authenticity_signal(profile)
        dispute_signal = _breeder_inquiry_signal(profile)
    else:
        role_metrics = _consultant_metrics(profile)
        review_signal = {}
        dispute_signal = _consultant_booking_signal(profile)

    hard_blocks = []
    hard_blocks.extend(identity_signal["hard_blocks"])
    hard_blocks.extend(message_signal["hard_blocks"])
    hard_blocks.extend(payment_signal["hard_blocks"])
    if incidents and any((incident.severity_level or "").lower() in {"critical", "high"} for incident in incidents):
        hard_blocks.append("Active critical incident history is already attached to this account.")

    # New users often have little or no historical activity; that alone should
    # not block approval. Treat thin evidence as a profile-completeness concern,
    # not a "you are new" concern.
    thin_evidence = (
        identity_signal["missing_required_count"] >= 3
        or role_signal["missing_required_count"] >= 2
        or (
            not getattr(profile, "company_name", "")
            and not getattr(profile, "bio", "")
        )
    )

    return {
        "mode": get_intelligence_mode(),
        "identity": identity_signal,
        "role_fit": role_signal,
        "trust": {
            "current_trust_score": user.current_trust_score,
            "current_regulatory_tier": user.current_regulatory_tier,
            "is_at_risk": user.is_at_risk,
            "snapshot": {
                "trust_score": getattr(trust_snapshot, "trust_score", None),
                "regulatory_tier": getattr(trust_snapshot, "regulatory_tier", ""),
                "total_badge_points": getattr(trust_snapshot, "total_badge_points", 0),
                "total_incident_penalties": getattr(trust_snapshot, "total_incident_penalties", 0),
                "contributing_factors": getattr(trust_snapshot, "contributing_factors", {}) or {},
            },
            "open_incidents": [
                {
                    "incident_code": incident.incident_code,
                    "severity_level": incident.severity_level,
                    "penalty_points": incident.penalty_points,
                    "description": incident.description,
                }
                for incident in incidents
            ],
        },
        "review_authenticity": review_signal,
        "dispute_intelligence": dispute_signal,
        "behavioural_intelligence": {
            "messages": message_signal,
            "payments": payment_signal,
            "network": network_signal,
        },
        "role_metrics": role_metrics,
        "hard_blocks": hard_blocks,
        "thin_evidence": thin_evidence,
    }


def discover_behavioral_issue_candidates(limit_per_type: int = 25) -> list[IssueCandidate]:
    candidates: list[IssueCandidate] = []
    candidates.extend(_discover_message_risks(limit_per_type))
    candidates.extend(_discover_breeder_inquiry_risks(limit_per_type))
    candidates.extend(_discover_booking_risks(limit_per_type))
    candidates.extend(_discover_payment_risks(limit_per_type))
    candidates.extend(_discover_trust_drop_risks(limit_per_type))
    return candidates


def _identity_signal(profile, user: ExternalUser) -> dict[str, Any]:
    required = [
        ("email", bool(user.email)),
        ("name", bool(user.name or user.first_name or user.last_name)),
        ("company_name", bool(getattr(profile, "company_name", ""))),
        ("bio", bool(getattr(profile, "bio", ""))),
    ]
    text = " ".join(
        [
            user.email or "",
            user.username or "",
            user.name or "",
            getattr(profile, "company_name", "") or "",
            getattr(profile, "bio", "") or "",
        ]
    ).lower()
    suspicious = [token for token in SUSPICIOUS_IDENTITY_TOKENS if token in text]
    hard_blocks = []
    if suspicious:
        hard_blocks.append("Profile contains obvious test, demo, or spam identity patterns.")
    return {
        "required_present": [name for name, present in required if present],
        "missing_required": [name for name, present in required if not present],
        "missing_required_count": sum(1 for _, present in required if not present),
        "suspicious_tokens": suspicious,
        "hard_blocks": hard_blocks,
    }


def _role_fit_signal(subject_type: str, profile) -> dict[str, Any]:
    if subject_type == "breeder":
        required = [
            ("service_area", bool(getattr(profile, "service_area", ""))),
            ("specializations", bool(getattr(profile, "specializations", []))),
        ]
    else:
        required = [
            ("services_list", bool(getattr(profile, "services_list", []))),
            ("credentials", bool(getattr(profile, "credentials", []))),
        ]
    return {
        "required_present": [name for name, present in required if present],
        "missing_required": [name for name, present in required if not present],
        "missing_required_count": sum(1 for _, present in required if not present),
    }


def _review_authenticity_signal(profile: ExternalBreederProfile) -> dict[str, Any]:
    qs = ExternalBreederReview.objects.filter(breeder_id=profile.id)
    metrics = qs.aggregate(
        sample_size=Count("id"),
        avg_rating=Avg("rating"),
        verified_count=Count("id", filter=Q(is_verified_purchase=True)),
    )
    suspicious_review_count = qs.filter(
        Q(comment__icontains="whatsapp")
        | Q(comment__icontains="telegram")
        | Q(comment__icontains="bank transfer")
    ).count()
    return {
        "sample_size": metrics["sample_size"] or 0,
        "avg_rating": _as_float(metrics["avg_rating"]),
        "verified_purchase_count": metrics["verified_count"] or 0,
        "suspicious_review_count": suspicious_review_count,
    }


def _breeder_inquiry_signal(profile: ExternalBreederProfile) -> dict[str, Any]:
    qs = ExternalBreederInquiry.objects.filter(breeder_id=profile.id)
    keyword_counts = Counter()
    for inquiry in qs[:50]:
        for label, count in _keyword_hits(inquiry.message or "").items():
            keyword_counts[label] += count
    return {
        "sample_size": qs.count(),
        "open_inquiries": qs.filter(status__in=["open", "pending"]).count(),
        "suspicious_keyword_hits": dict(keyword_counts),
    }


def _consultant_booking_signal(profile: ExternalConsultantProfile) -> dict[str, Any]:
    qs = ExternalConsultantBooking.objects.filter(consultant_id=profile.id)
    return {
        "sample_size": qs.count(),
        "failed_bookings": qs.filter(
            Q(was_successful=False)
            | Q(status__icontains="cancel")
            | Q(consultant_status__icontains="no_show")
        ).count(),
        "low_rating_bookings": qs.filter(rating__lte=2).count(),
        "refund_like_bookings": qs.filter(payment_status__icontains="refund").count(),
    }


def _breeder_metrics(profile: ExternalBreederProfile) -> dict[str, Any]:
    return {
        "healthy_stock_rate": _as_float(profile.healthy_stock_rate),
        "stock_mortality_rate": _as_float(profile.stock_mortality_rate),
        "disease_reported_rate": _as_float(profile.disease_reported_rate),
        "total_sales": profile.total_sales,
        "successful_sales": profile.successful_sales,
    }


def _consultant_metrics(profile: ExternalConsultantProfile) -> dict[str, Any]:
    return {
        "completion_rate": _as_float(profile.completion_rate),
        "cancellation_rate": _as_float(profile.cancellation_rate),
        "complaint_count": profile.complaint_count,
        "average_response_time_hours": _as_float(profile.average_response_time_hours),
    }


def _message_signal_for_user(user_id, *, limit: int = 20) -> dict[str, Any]:
    conversation_ids = list(
        ExternalConversation.objects
        .filter(Q(participant_1_id=user_id) | Q(participant_2_id=user_id))
        .values_list("id", flat=True)[:100]
    )
    messages = list(
        ExternalMessage.objects
        .filter(conversation_id__in=conversation_ids, is_deleted=False)
        .order_by("-created_at")[:limit]
    )
    keyword_counts = Counter()
    excerpts = []
    for message in messages:
        hits = _keyword_hits(message.content or "")
        for label, count in hits.items():
            keyword_counts[label] += count
        if hits:
            excerpts.append((message.content or "")[:160])
    hard_blocks = []
    if keyword_counts["payment_bypass"] >= 2:
        hard_blocks.append("Message history contains repeated attempts to move payment off-platform.")
    return {
        "sample_size": len(messages),
        "suspicious_keyword_hits": dict(keyword_counts),
        "sample_excerpts": excerpts[:3],
        "hard_blocks": hard_blocks,
    }


def _payment_signal_for_user(user_id) -> dict[str, Any]:
    failures = ExternalPaymentFailureLog.objects.filter(user_id=user_id)
    failure_count = failures.count()
    hard_blocks = []
    if failure_count >= 5:
        hard_blocks.append("User has repeated payment failures that may indicate abuse or fraud risk.")
    return {
        "failure_count": failure_count,
        "recent_failure_reasons": list(failures.order_by("-created_at").values_list("failure_reason", flat=True)[:3]),
        "hard_blocks": hard_blocks,
    }


def _network_signal_for_user(user_id) -> dict[str, Any]:
    conversations = ExternalConversation.objects.filter(Q(participant_1_id=user_id) | Q(participant_2_id=user_id))
    counterpart_ids = set()
    for conversation in conversations[:100]:
        counterpart_ids.add(str(conversation.participant_1_id if conversation.participant_2_id == user_id else conversation.participant_2_id))
    return {
        "conversation_count": conversations.count(),
        "counterpart_count": len(counterpart_ids),
    }


def _discover_message_risks(limit_per_type: int) -> list[IssueCandidate]:
    candidates = []
    qs = ExternalMessage.objects.filter(is_deleted=False).order_by("-created_at")[: limit_per_type * 8]
    for message in qs:
        hits = _keyword_hits(message.content or "")
        if not hits:
            continue
        user = _load_user(message.sender_id)
        if not user:
            continue
        subject_type, profile = _load_profile(user)
        if not profile:
            continue
        candidates.append(
            IssueCandidate(
                source_type="message_risk",
                source_id=str(message.id),
                title="Suspicious marketplace communication",
                subject_type=subject_type,
                user=user,
                profile=profile,
                payload={
                    "message_id": str(message.id),
                    "conversation_id": str(message.conversation_id),
                    "created_at": message.created_at.isoformat() if message.created_at else None,
                    "content_excerpt": (message.content or "")[:250],
                    "keyword_hits": hits,
                },
            )
        )
        if len(candidates) >= limit_per_type:
            break
    return candidates


def _discover_breeder_inquiry_risks(limit_per_type: int) -> list[IssueCandidate]:
    candidates = []
    qs = ExternalBreederInquiry.objects.order_by("-created_at")[: limit_per_type * 8]
    for inquiry in qs:
        hits = _keyword_hits(inquiry.message or "")
        if not hits:
            continue
        try:
            profile = ExternalBreederProfile.objects.get(pk=inquiry.breeder_id)
            user = ExternalUser.objects.get(pk=profile.user_id)
        except (ExternalBreederProfile.DoesNotExist, ExternalUser.DoesNotExist):
            continue
        candidates.append(
            IssueCandidate(
                source_type="breeder_inquiry_risk",
                source_id=str(inquiry.id),
                title="Risky breeder inquiry content",
                subject_type="breeder",
                user=user,
                profile=profile,
                payload={
                    "inquiry_id": str(inquiry.id),
                    "created_at": inquiry.created_at.isoformat() if inquiry.created_at else None,
                    "priority": inquiry.priority,
                    "status": inquiry.status,
                    "message_excerpt": (inquiry.message or "")[:250],
                    "keyword_hits": hits,
                },
            )
        )
        if len(candidates) >= limit_per_type:
            break
    return candidates


def _discover_booking_risks(limit_per_type: int) -> list[IssueCandidate]:
    candidates = []
    qs = ExternalConsultantBooking.objects.order_by("-created_at")[: limit_per_type * 10]
    for booking in qs:
        risky = (
            not booking.was_successful
            or (booking.rating is not None and booking.rating <= 2)
            or "cancel" in (booking.status or "").lower()
            or "no_show" in (booking.consultant_status or "").lower()
        )
        if not risky:
            continue
        try:
            profile = ExternalConsultantProfile.objects.get(pk=booking.consultant_id)
            user = ExternalUser.objects.get(pk=profile.user_id)
        except (ExternalConsultantProfile.DoesNotExist, ExternalUser.DoesNotExist):
            continue
        candidates.append(
            IssueCandidate(
                source_type="booking_risk",
                source_id=str(booking.id),
                title="Risky consultant booking pattern",
                subject_type="consultant",
                user=user,
                profile=profile,
                payload={
                    "booking_id": str(booking.id),
                    "created_at": booking.created_at.isoformat() if booking.created_at else None,
                    "status": booking.status,
                    "consultant_status": booking.consultant_status,
                    "payment_status": booking.payment_status,
                    "rating": booking.rating,
                    "review_excerpt": (booking.review or "")[:250],
                    "was_successful": booking.was_successful,
                    "response_time_hours": _as_float(booking.response_time_hours),
                },
            )
        )
        if len(candidates) >= limit_per_type:
            break
    return candidates


def _discover_payment_risks(limit_per_type: int) -> list[IssueCandidate]:
    candidates = []
    failures = ExternalPaymentFailureLog.objects.order_by("-created_at")[: limit_per_type * 8]
    for failure in failures:
        if failure.user_id is None:
            continue
        user = _load_user(failure.user_id)
        if not user:
            continue
        subject_type, profile = _load_profile(user)
        if not profile:
            continue
        recent_failures = ExternalPaymentFailureLog.objects.filter(user_id=user.id).count()
        if recent_failures < 3 and "fraud" not in (failure.failure_reason or "").lower():
            continue
        candidates.append(
            IssueCandidate(
                source_type="payment_risk",
                source_id=f"failure:{failure.id}",
                title="Repeated payment failure pattern",
                subject_type=subject_type,
                user=user,
                profile=profile,
                payload={
                    "failure_id": str(failure.id),
                    "created_at": failure.created_at.isoformat() if failure.created_at else None,
                    "failure_reason": failure.failure_reason,
                    "failure_code": failure.failure_code,
                    "recent_failure_count": recent_failures,
                    "amount": _as_float(failure.amount),
                    "currency": failure.currency,
                },
            )
        )
        if len(candidates) >= limit_per_type:
            break
    return candidates


def _discover_trust_drop_risks(limit_per_type: int) -> list[IssueCandidate]:
    candidates = []
    snapshots = ExternalTrustScoreSnapshot.objects.order_by("-calculated_at")[: limit_per_type * 10]
    for snapshot in snapshots:
        if snapshot.trust_score is None:
            continue
        if snapshot.trust_score > 35 and snapshot.total_incident_penalties < 15:
            continue
        user = _load_user(snapshot.user_id)
        if not user:
            continue
        subject_type, profile = _load_profile(user)
        if not profile:
            continue
        candidates.append(
            IssueCandidate(
                source_type="trust_drop",
                source_id=str(snapshot.id),
                title="Low trust snapshot detected",
                subject_type=subject_type,
                user=user,
                profile=profile,
                payload={
                    "snapshot_id": str(snapshot.id),
                    "calculated_at": snapshot.calculated_at.isoformat() if snapshot.calculated_at else None,
                    "trust_score": snapshot.trust_score,
                    "regulatory_tier": snapshot.regulatory_tier,
                    "total_badge_points": snapshot.total_badge_points,
                    "total_incident_penalties": snapshot.total_incident_penalties,
                    "contributing_factors": snapshot.contributing_factors or {},
                },
            )
        )
        if len(candidates) >= limit_per_type:
            break
    return candidates


def _keyword_hits(text: str) -> dict[str, int]:
    lower = (text or "").lower()
    hits = {}
    for label, keywords in INTELLIGENCE_KEYWORDS.items():
        count = sum(1 for keyword in keywords if keyword in lower)
        if count:
            hits[label] = count
    return hits


def _load_user(user_id) -> ExternalUser | None:
    try:
        user = ExternalUser.objects.get(pk=user_id)
    except ExternalUser.DoesNotExist:
        return None
    if (user.role or "").strip().lower() in INTERNAL_ROLES:
        return None
    return user


def _load_profile(user: ExternalUser) -> tuple[str, Any]:
    try:
        return "consultant", ExternalConsultantProfile.objects.get(user_id=user.id)
    except ExternalConsultantProfile.DoesNotExist:
        pass
    try:
        return "breeder", ExternalBreederProfile.objects.get(user_id=user.id)
    except ExternalBreederProfile.DoesNotExist:
        return user.role or "", None


def _as_float(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
