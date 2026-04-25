"""Glue between the unmanaged mirror models, the GPT-4 pipeline, and our own
AIAccountReview / AIFlag tables. Also applies recommended actions back onto
the main backend's tables when safe to do so.
"""
from __future__ import annotations

import logging
from typing import Iterable

from django.utils import timezone

from ..models import (
    AIAccountReview, AIFlag,
    ExternalBreederProfile, ExternalConsultantProfile, ExternalUser,
)
from .notifier import notify_flag
from .openai_review import (
    AIReviewOutcome,
    build_breeder_dossier, build_consultant_dossier,
    call_gpt4,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Discover profiles that the AI hasn't reviewed yet
# ---------------------------------------------------------------------------

def _already_reviewed_ids(subject_type: str) -> set:
    return set(
        AIAccountReview.objects
        .filter(subject_type=subject_type)
        .values_list("subject_id", flat=True)
    )


def discover_pending_breeders(limit: int = 50):
    seen = _already_reviewed_ids("breeder")
    qs = (ExternalBreederProfile.objects
          .filter(is_active=True, is_verified=False)
          .order_by("-created_at"))
    for profile in qs[: limit * 4]:
        if profile.id in seen:
            continue
        try:
            user = ExternalUser.objects.get(pk=profile.user_id)
        except ExternalUser.DoesNotExist:
            continue
        yield profile, user


def discover_pending_consultants(limit: int = 50):
    seen = _already_reviewed_ids("consultant")
    qs = (ExternalConsultantProfile.objects
          .filter(is_active=True)
          .exclude(admin_status="approved")
          .order_by("-created_at"))
    for profile in qs[: limit * 4]:
        if profile.id in seen:
            continue
        try:
            user = ExternalUser.objects.get(pk=profile.user_id)
        except ExternalUser.DoesNotExist:
            continue
        yield profile, user


# ---------------------------------------------------------------------------
# Run AI on one profile
# ---------------------------------------------------------------------------

def run_review(subject_type: str, profile, user) -> AIAccountReview:
    if subject_type == "breeder":
        dossier = build_breeder_dossier(profile, user)
    else:
        dossier = build_consultant_dossier(profile, user)

    outcome = call_gpt4(dossier)

    display = (profile.company_name or user.name or f"{user.first_name} {user.last_name}").strip()
    review, _ = AIAccountReview.objects.update_or_create(
        subject_type=subject_type,
        subject_id=profile.id,
        defaults=dict(
            subject_user_email=user.email,
            subject_display_name=display[:255],
            decision=outcome.decision,
            confidence=outcome.confidence,
            rationale=outcome.rationale,
            evidence=outcome.evidence,
            recommended_actions=outcome.recommended_actions,
            openai_raw=outcome.raw,
            ai_model=outcome.model,
            error=outcome.error,
            decided_at=timezone.now() if outcome.decision != "pending" else None,
            manually_overridden=False,
            overridden_by=None,
            override_reason="",
            original_decision="",
        ),
    )

    # Persist flags + notify super-admins
    for f in outcome.flags:
        flag = AIFlag.objects.create(
            review=review,
            severity=str(f.get("severity", "warning")).lower(),
            reason=str(f.get("reason", ""))[:4000],
            recommended_solution=str(f.get("recommended_solution", ""))[:4000],
        )
        delivery = notify_flag(review, flag)
        flag.notified_emails = delivery.get("recipients", [])
        flag.notified_slack = delivery.get("slack", False)
        flag.save(update_fields=["notified_emails", "notified_slack"])

    # Apply safe automatic actions back onto the source-of-truth tables
    applied = _apply_actions(subject_type, profile, user, review)
    if applied:
        review.applied_actions = applied
        review.save(update_fields=["applied_actions"])

    return review


# ---------------------------------------------------------------------------
# Manual override — super admin can force approve/reject
# ---------------------------------------------------------------------------

def manual_override(review: AIAccountReview, new_decision: str, reason: str, admin_user) -> AIAccountReview:
    """Allow a super-admin to override an AI decision."""
    from .notifier import notify_manual_override

    review.original_decision = review.decision
    review.decision = new_decision
    review.manually_overridden = True
    review.overridden_by = admin_user
    review.override_reason = reason
    review.decided_at = timezone.now()
    review.save()

    # Apply the override to the external profile
    try:
        if review.subject_type == "breeder":
            profile = ExternalBreederProfile.objects.get(pk=review.subject_id)
        else:
            profile = ExternalConsultantProfile.objects.get(pk=review.subject_id)
        user = ExternalUser.objects.get(pk=profile.user_id)

        if new_decision == "approved":
            _approve(review.subject_type, profile, user)
            review.applied_actions = list(review.applied_actions or []) + [
                {"action": "manual_approve", "by": admin_user.email}
            ]
        elif new_decision == "rejected":
            _deactivate(review.subject_type, profile, user, reason=f"Manual reject: {reason}")
            review.applied_actions = list(review.applied_actions or []) + [
                {"action": "manual_reject", "by": admin_user.email}
            ]
        review.save(update_fields=["applied_actions"])
    except Exception:
        logger.exception("Failed applying manual override actions")

    notify_manual_override(review, admin_user, new_decision, reason)
    return review


# ---------------------------------------------------------------------------
# Apply remediation actions back to the main backend's tables
# ---------------------------------------------------------------------------

SAFE_VERIFICATION_LEVELS = {"none", "basic", "standard", "premium"}


def _apply_actions(subject_type: str, profile, user, review) -> list[dict]:
    applied = []
    for action in review.recommended_actions or []:
        name = (action or {}).get("action", "")
        try:
            if review.decision == "approved" and name in ("", None):
                _approve(subject_type, profile, user)
                applied.append({"action": "approve_account"})
            elif name == "approve_account" and review.decision == "approved":
                _approve(subject_type, profile, user)
                applied.append(action)
            elif name == "reject_account" and review.decision == "rejected":
                _deactivate(subject_type, profile, user, reason="AI auto-reject")
                applied.append(action)
            elif name == "deactivate_pending_docs":
                _deactivate(subject_type, profile, user, reason="Awaiting documents")
                applied.append(action)
            elif name == "set_verification_level":
                lvl = str(action.get("value", "")).lower()
                if lvl in SAFE_VERIFICATION_LEVELS:
                    profile.verification_level = lvl
                    profile.save(update_fields=["verification_level"])
                    applied.append(action)
        except Exception:
            logger.exception("Failed applying action %s", action)

    # Top-level: if approved and we didn't already approve via an action
    if review.decision == "approved" and not any(a.get("action") in ("approve_account",) for a in applied):
        try:
            _approve(subject_type, profile, user)
            applied.append({"action": "approve_account", "auto": True})
        except Exception:
            logger.exception("Auto-approve failed")
    if review.decision == "rejected" and not any(a.get("action") == "reject_account" for a in applied):
        try:
            _deactivate(subject_type, profile, user, reason="AI auto-reject")
            applied.append({"action": "reject_account", "auto": True})
        except Exception:
            logger.exception("Auto-reject failed")
    return applied


def _approve(subject_type, profile, user):
    now = timezone.now()
    user.is_verified = True
    user.verified_at = now
    user.save(update_fields=["is_verified", "verified_at"])
    profile.is_verified = True
    profile.verified_at = now
    if subject_type == "consultant":
        profile.admin_status = "approved"
        profile.save(update_fields=["is_verified", "verified_at", "admin_status"])
    else:
        profile.save(update_fields=["is_verified", "verified_at"])


def _deactivate(subject_type, profile, user, *, reason: str):
    profile.is_active = False
    if subject_type == "consultant":
        profile.admin_status = "rejected"
        profile.admin_notes = (profile.admin_notes or "") + f"\n[AI] {reason}"
        profile.save(update_fields=["is_active", "admin_status", "admin_notes"])
    else:
        profile.save(update_fields=["is_active"])


# ---------------------------------------------------------------------------

def process_pending(limit_per_type: int = 25) -> dict:
    counts = {"breeder": 0, "consultant": 0}
    for profile, user in discover_pending_breeders(limit=limit_per_type):
        run_review("breeder", profile, user)
        counts["breeder"] += 1
    for profile, user in discover_pending_consultants(limit=limit_per_type):
        run_review("consultant", profile, user)
        counts["consultant"] += 1
    return counts
