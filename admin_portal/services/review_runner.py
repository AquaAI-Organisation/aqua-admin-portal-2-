"""Glue between the unmanaged mirror models, the GPT-4 pipeline, and our own
AIAccountReview / AIFlag tables. Also applies recommended actions back onto
the main backend's tables when safe to do so.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Iterable

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from ..models import (
    AIAccountReview, AIFlag, OperationalSettings,
    ExternalBreederProfile, ExternalConsultantProfile, ExternalUser,
)
from .notifier import notify_flag
from .openai_review import (
    AIReviewOutcome,
    build_breeder_dossier, build_consultant_dossier,
    call_gpt4,
)

logger = logging.getLogger(__name__)


def _global_auto_activate_enabled() -> bool:
    try:
        return bool(OperationalSettings.get_solo().auto_activate_new_accounts)
    except Exception:
        logger.exception("Could not read operational auto-activation setting")
        return False


def _auto_activate_outcome(subject_type: str, profile, user, stagger: dict | None = None) -> AIReviewOutcome:
    display = (profile.company_name or user.name or f"{user.first_name} {user.last_name}").strip()
    mode = "staggered_auto_activate" if stagger else "global_auto_activate"
    evidence = {
        "operational_mode": mode,
        "auto_activated_by_setting": True,
        "subject_type": subject_type,
        "subject_display_name": display[:255],
        "decision_basis": {
            "risk_bucket": "unreviewed",
            "hard_blocks": [],
            "operational_policy": mode,
        },
    }
    if stagger:
        evidence["staggered"] = stagger
        delay = stagger.get("assigned_delay_minutes")
        rationale = (
            f"Automatically approved after a staggered {delay}-minute delay "
            "(automatic activation is enabled, in staggered mode)."
        )
    else:
        rationale = (
            "Automatically approved because the global automatic account activation toggle "
            "is enabled in operational settings."
        )
    return AIReviewOutcome(
        decision="approved",
        confidence=1.0,
        rationale=rationale,
        evidence=evidence,
        recommended_actions=[{"action": "approve_account", "mode": mode}],
        flags=[],
        raw={"source": "operational_settings", "mode": mode},
        model="operational:auto-activate",
        error="",
    )


def _max_flag_severity(flags: list[dict] | None) -> str:
    levels = {"info": 0, "warning": 1, "critical": 2}
    highest = "info"
    for flag in flags or []:
        severity = str((flag or {}).get("severity", "info")).lower()
        if levels.get(severity, 0) > levels.get(highest, 0):
            highest = severity
    return highest


def _risk_bucket(outcome: AIReviewOutcome) -> str:
    if outcome.decision == "rejected":
        return "critical"
    highest = _max_flag_severity(outcome.flags)
    if highest == "critical":
        return "critical"
    if outcome.confidence < 0.35 or highest == "warning":
        return "high"
    if outcome.confidence < 0.60:
        return "medium"
    return "low"


def _operationalise_outcome(outcome: AIReviewOutcome) -> AIReviewOutcome:
    """Bias toward admitting plausible accounts while preserving risk alerts.

    Only truly clear-risk outcomes should remain rejected. Non-critical flagged
    outcomes are auto-approved and kept visible via AIFlag severity plus a
    stored risk bucket in evidence.
    """
    risk_bucket = _risk_bucket(outcome)
    decision_basis = dict((outcome.evidence or {}).get("decision_basis", {}))
    decision_basis["risk_bucket"] = risk_bucket
    auto_admit_floor = 0.09
    decision_basis["operational_policy"] = "auto_admit_non_critical"
    decision_basis["auto_admit_floor"] = auto_admit_floor

    evidence = dict(outcome.evidence or {})
    evidence["decision_basis"] = decision_basis
    evidence["risk_bucket"] = risk_bucket
    outcome.evidence = evidence

    hard_blocks = list(decision_basis.get("hard_blocks", []))
    should_auto_admit = (
        outcome.decision in {"flagged", "rejected"}
        and risk_bucket != "critical"
        and not hard_blocks
        and outcome.confidence >= auto_admit_floor
    )

    if should_auto_admit:
        outcome.decision = "approved"
        rationale = (outcome.rationale or "").strip()
        prefix = "Auto-approved with follow-up risk monitoring."
        outcome.rationale = f"{prefix} {rationale}".strip()
        actions = list(outcome.recommended_actions or [])
        actions.insert(0, {"action": "approve_account", "mode": "auto_with_risk_review"})
        outcome.recommended_actions = actions
    return outcome


# ---------------------------------------------------------------------------
# Discover profiles that the AI hasn't reviewed yet
# ---------------------------------------------------------------------------

def _already_reviewed_ids(subject_type: str) -> set:
    return set(
        AIAccountReview.objects
        .filter(subject_type=subject_type)
        .exclude(decision__in=["error", "pending"])
        .values_list("subject_id", flat=True)
    )


def discover_pending_breeders(limit: int = 50):
    seen = _already_reviewed_ids("breeder")
    qs = (ExternalBreederProfile.objects
          .filter(is_verified=False)
          .order_by("-created_at"))
    for profile in qs[: limit * 4]:
        if profile.id in seen:
            continue
        metadata = profile.metadata or {}
        application_status = str(metadata.get("application_status", "")).strip().lower()
        if profile.is_verified:
            continue
        if profile.is_active and application_status not in {"", "pending", "under_review"}:
            continue
        try:
            user = ExternalUser.objects.get(pk=profile.user_id)
        except ExternalUser.DoesNotExist:
            continue
        yield profile, user


def discover_pending_consultants(limit: int = 50):
    seen = _already_reviewed_ids("consultant")
    qs = (ExternalConsultantProfile.objects
          .exclude(admin_status="approved")
          .order_by("-created_at"))
    for profile in qs[: limit * 4]:
        if profile.id in seen:
            continue
        status = str(profile.admin_status or "").strip().lower()
        if status and status not in {"pending", "under_review", "needs_info", "needs_review"}:
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
    config = OperationalSettings.get_solo()
    if config.auto_activate_new_accounts and config.auto_activate_stagger_enabled:
        # Staggered mode fully owns the account's lifecycle: hold it pending until
        # its assigned, human-looking delay elapses, then approve.
        return _process_staggered_auto_activate(subject_type, profile, user, config)

    if config.auto_activate_new_accounts:
        outcome = _auto_activate_outcome(subject_type, profile, user)
    else:
        if subject_type == "breeder":
            dossier = build_breeder_dossier(profile, user)
        else:
            dossier = build_consultant_dossier(profile, user)
        outcome = call_gpt4(dossier)
        outcome = _operationalise_outcome(outcome)

    return _persist_review(subject_type, profile, user, outcome)


def _persist_review(subject_type: str, profile, user, outcome: AIReviewOutcome) -> AIAccountReview:
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


def _process_staggered_auto_activate(subject_type: str, profile, user, config) -> AIAccountReview:
    """Hold a new account pending until its assigned staggered delay elapses, then
    auto-approve it — so approvals look manual but are fully automatic.

    Called once per processing cycle for each still-pending account:
    - first sighting  -> assign the next delay from the schedule, store the due time,
      and leave the account pending (nothing applied to the source tables yet);
    - subsequent runs -> approve once now >= the due time, otherwise keep waiting.
    """
    now = timezone.now()
    existing = (
        AIAccountReview.objects
        .filter(subject_type=subject_type, subject_id=profile.id)
        .first()
    )
    # Already decided (e.g. approved, or a manual override) — leave it untouched.
    if existing is not None and existing.decision not in ("pending", "error"):
        return existing

    scheduled_at = None
    if existing is not None:
        raw = (existing.evidence or {}).get("scheduled_approval_at")
        scheduled_at = parse_datetime(raw) if raw else None
        if scheduled_at is not None and timezone.is_naive(scheduled_at):
            scheduled_at = timezone.make_aware(scheduled_at, timezone.get_current_timezone())

    if scheduled_at is None:
        # First time we see this account: assign the next staggered delay and hold.
        idx, delay = config.take_next_stagger_delay()
        scheduled_at = now + timedelta(minutes=delay)
        return _persist_scheduled_pending(subject_type, profile, user, idx, delay, scheduled_at)

    if now >= scheduled_at:
        # The wait has elapsed — approve now, exactly as an admin action would.
        stagger = {
            "assigned_delay_minutes": (existing.evidence or {}).get("assigned_delay_minutes"),
            "scheduled_approval_at": scheduled_at.isoformat(),
            "stagger_index": (existing.evidence or {}).get("stagger_index"),
        }
        outcome = _auto_activate_outcome(subject_type, profile, user, stagger=stagger)
        return _persist_review(subject_type, profile, user, outcome)

    # Still within the delay window — keep the account pending.
    return existing


def _persist_scheduled_pending(subject_type: str, profile, user, idx: int, delay: int, scheduled_at) -> AIAccountReview:
    display = (profile.company_name or user.name or f"{user.first_name} {user.last_name}").strip()
    review, _ = AIAccountReview.objects.update_or_create(
        subject_type=subject_type,
        subject_id=profile.id,
        defaults=dict(
            subject_user_email=user.email,
            subject_display_name=display[:255],
            decision="pending",
            confidence=1.0,
            rationale=(
                f"Queued for automatic approval after a staggered {delay}-minute delay "
                f"(position {idx} in the approval schedule)."
            ),
            evidence={
                "operational_mode": "staggered_auto_activate",
                "scheduled_approval_at": scheduled_at.isoformat(),
                "assigned_delay_minutes": delay,
                "stagger_index": idx,
                "decision_basis": {
                    "risk_bucket": "unreviewed",
                    "operational_policy": "staggered_auto_activate",
                },
            },
            recommended_actions=[],
            applied_actions=[],
            openai_raw={"source": "operational_settings", "mode": "staggered_auto_activate"},
            ai_model="operational:auto-activate-staggered",
            error="",
            decided_at=None,
            manually_overridden=False,
            overridden_by=None,
            override_reason="",
            original_decision="",
        ),
    )
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
    # Clear stale AI failures once a super admin has made the authoritative
    # decision so approved/rejected reviews do not keep surfacing old auth errors.
    review.error = ""
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
    user.is_active = True
    user.is_verified = True
    user.verified_at = now
    user.save(update_fields=["is_active", "is_verified", "verified_at"])
    profile.is_verified = True
    profile.verified_at = now
    if subject_type == "consultant":
        profile.is_active = True
        profile.admin_status = "approved"
        metadata = dict(profile.metadata or {})
        metadata.update({
            "application_status": "approved",
            "approval_date": now.isoformat(),
            "requires_admin_approval": False,
        })
        profile.metadata = metadata
        profile.save(update_fields=["is_active", "is_verified", "verified_at", "admin_status", "metadata"])
    else:
        profile.is_active = True
        metadata = dict(profile.metadata or {})
        metadata.update({
            "application_status": "approved",
            "approval_date": now.isoformat(),
            "requires_admin_approval": False,
        })
        profile.metadata = metadata
        profile.save(update_fields=["is_active", "is_verified", "verified_at", "metadata"])


def _deactivate(subject_type, profile, user, *, reason: str):
    profile.is_active = False
    if subject_type == "consultant":
        profile.admin_status = "rejected"
        profile.admin_notes = (profile.admin_notes or "") + f"\n[AI] {reason}"
        metadata = dict(profile.metadata or {})
        metadata.update({
            "application_status": "rejected",
            "rejection_reason": reason,
            "requires_admin_approval": False,
        })
        profile.metadata = metadata
        profile.save(update_fields=["is_active", "admin_status", "admin_notes", "metadata"])
    else:
        metadata = dict(profile.metadata or {})
        metadata.update({
            "application_status": "rejected",
            "rejection_reason": reason,
            "requires_admin_approval": False,
        })
        profile.metadata = metadata
        profile.save(update_fields=["is_active", "metadata"])


# ---------------------------------------------------------------------------

def process_pending(limit_per_type: int = 25, *, max_runtime_seconds: float | None = None) -> dict:
    counts = {"breeder": 0, "consultant": 0, "truncated": False}
    started = time.monotonic()

    def _deadline_hit() -> bool:
        return bool(max_runtime_seconds and (time.monotonic() - started) >= max_runtime_seconds)

    for profile, user in discover_pending_breeders(limit=limit_per_type):
        if _deadline_hit():
            counts["truncated"] = True
            break
        run_review("breeder", profile, user)
        counts["breeder"] += 1
    if not counts["truncated"]:
        for profile, user in discover_pending_consultants(limit=limit_per_type):
            if _deadline_hit():
                counts["truncated"] = True
                break
            run_review("consultant", profile, user)
            counts["consultant"] += 1
    return counts
