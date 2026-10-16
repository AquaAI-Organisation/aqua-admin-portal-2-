"""Evidence timing, the safety hold, and the named human queues (spec §12.1, §21.1).

Three clocks, all measured from the moment the submitted-profile outbox row was
created — not from profile creation, and not from when the CRM happened to
start work:

    2 hours   CRM dossier target    — informational; nothing changes state
    12 hours  CRM dossier deadline  — a safety hold is recorded
    24 hours  applicant promise     — a person must have decided by here

The 12-hour action is deliberately *not* a decision about the applicant. §12.1
calls it "an operational safeguard, not an approval or rejection": it records
``held / evidence_overdue``, puts the case in a named queue and tells the CRM.
A person still has to decide, and §12.3 forbids the case auto-promoting later —
which holds structurally here, because ``_already_reviewed_ids`` excludes any
review whose decision is not ``pending`` or ``error``, so a held case is never
rediscovered by the review runner.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from ..models import AIAccountReview, V2InboxEvent, V2OutboxEvent
from . import contract as c
from .decisions import record_decision
from .events import enqueue_decision_event

logger = logging.getLogger(__name__)

DOSSIER_TARGET = timedelta(hours=2)
DOSSIER_DEADLINE = timedelta(hours=12)
APPLICANT_PROMISE = timedelta(hours=24)

#: The states a case can be in while still waiting on CRM evidence.
AWAITING_STATES = ("submitted_to_crm", "awaiting_crm_evidence")


@dataclass(frozen=True)
class SlaState:
    """Where one case sits against the three clocks."""
    submitted_at: object | None
    target_at: object | None
    deadline_at: object | None
    promise_at: object | None
    has_dossier: bool
    state: str              # not_submitted | on_target | past_target | overdue | satisfied
    minutes_remaining: int | None

    @property
    def breached_deadline(self) -> bool:
        return self.state == "overdue"


def sla_state(review: AIAccountReview, *, now=None) -> SlaState:
    """Evaluate the evidence clocks for one review. Never raises."""
    now = now or timezone.now()
    submitted = review.submitted_event_at
    has_dossier = bool(review.dossier_id)

    if submitted is None:
        return SlaState(None, None, None, None, has_dossier, "not_submitted", None)

    target_at = submitted + DOSSIER_TARGET
    deadline_at = submitted + DOSSIER_DEADLINE
    promise_at = submitted + APPLICANT_PROMISE

    if has_dossier:
        state = "satisfied"
    elif now >= deadline_at:
        state = "overdue"
    elif now >= target_at:
        state = "past_target"
    else:
        state = "on_target"

    remaining = int((deadline_at - now).total_seconds() // 60) if state != "satisfied" else None
    return SlaState(submitted, target_at, deadline_at, promise_at, has_dossier,
                    state, remaining)


def due_for_safety_hold(*, now=None):
    """Reviews past the 12-hour deadline with no terminal dossier."""
    now = now or timezone.now()
    cutoff = now - DOSSIER_DEADLINE
    return (AIAccountReview.objects
            .filter(review_state__in=AWAITING_STATES,
                    submitted_event_at__isnull=False,
                    submitted_event_at__lte=cutoff,
                    dossier_id="")
            .order_by("submitted_event_at"))


def past_target(*, now=None):
    """Reviews past the 2-hour target but not yet overdue. Informational."""
    now = now or timezone.now()
    return (AIAccountReview.objects
            .filter(review_state__in=AWAITING_STATES,
                    submitted_event_at__isnull=False,
                    submitted_event_at__lte=now - DOSSIER_TARGET,
                    submitted_event_at__gt=now - DOSSIER_DEADLINE,
                    dossier_id="")
            .order_by("submitted_event_at"))


def record_safety_hold(review: AIAccountReview, *, now=None) -> bool:
    """Place one overdue case in ``held / evidence_overdue`` and tell the CRM.

    Returns True when a hold was recorded. Idempotent: a case that already
    carries the hold, or that has since received a dossier, is left alone.
    """
    now = now or timezone.now()
    with transaction.atomic():
        locked = AIAccountReview.objects.select_for_update().get(pk=review.pk)
        if locked.dossier_id:
            return False                      # evidence arrived in the meantime
        if locked.review_state not in AWAITING_STATES:
            return False                      # already held or decided
        if locked.submitted_event_at is None or now - locked.submitted_event_at < DOSSIER_DEADLINE:
            return False

        record_decision(
            locked,
            status="held",
            reason_code="evidence_overdue",
            reason_text=(
                "No terminal CRM evidence dossier arrived within 12 hours of the "
                "submitted-profile event. Held for human review before the "
                "24-hour applicant promise expires."
            ),
            actor_type="system_timeout",
            decision_path="system_safety_hold",
            # §13.3: a timeout hold may carry no dossier reference.
            use_dossier=False,
        )
        locked.refresh_from_db()
        # §12.1: "synchronise that hold to CRM" — queued in the same
        # transaction as the hold itself.
        enqueue_decision_event(locked)
    return True


def sweep(*, now=None, limit: int = 200, notify: bool = True) -> dict:
    """Record safety holds for every overdue case. Safe to run every minute."""
    now = now or timezone.now()
    counts = {"checked": 0, "held": 0, "past_target": 0, "skipped": 0}
    held_rows = []

    for review in list(due_for_safety_hold(now=now)[:limit]):
        counts["checked"] += 1
        try:
            if record_safety_hold(review, now=now):
                counts["held"] += 1
                held_rows.append(review)
            else:
                counts["skipped"] += 1
        except Exception:
            logger.exception("could not record the evidence-overdue hold for %s",
                             review.subject_id)
            counts["skipped"] += 1

    counts["past_target"] = past_target(now=now).count()
    if held_rows and notify:
        _notify_overdue(held_rows)
    return counts


def _notify_overdue(reviews) -> None:
    """Tell a person. A hold nobody is told about is not a queue."""
    try:
        from ..services import slack as slack_service
        from ..services.notifier import send_custom_email
    except Exception:                                    # pragma: no cover
        return

    lines = []
    for review in reviews:
        state = sla_state(review)
        promise = state.promise_at.strftime("%d %b %H:%M") if state.promise_at else "unknown"
        lines.append(
            f"• {review.subject_type} {review.subject_display_name or review.subject_user_email} "
            f"— decide before {promise}")
    body = (
        f"{len(reviews)} provider application(s) passed the 12-hour CRM evidence "
        f"deadline with no terminal dossier and are now held for human review.\n\n"
        + "\n".join(lines)
        + "\n\nThese are safety holds, not rejections. Each one still needs a person "
          "to decide before the 24-hour applicant promise expires.\n"
    )
    try:
        slack_service.send_to_super_admins(
            f":hourglass: *{len(reviews)} provider case(s) held — CRM evidence overdue*\n"
            + "\n".join(lines))
    except Exception:
        logger.exception("slack alert failed for the evidence-overdue sweep")
    try:
        from django.conf import settings
        recipients = list(getattr(settings, "ADMIN_EMAILS", []) or [])
        if recipients:
            send_custom_email(
                subject=f"[AquaAI] {len(reviews)} provider case(s) held — CRM evidence overdue",
                body=body, recipients=recipients)
    except Exception:
        logger.exception("email alert failed for the evidence-overdue sweep")


# ---------------------------------------------------------------------------
# Named human queues (spec §23.3)
# ---------------------------------------------------------------------------

#: A held case sits in ``human_review`` until a person records a decision,
#: which moves it to ``decided``. So the queue is derived from state rather
#: than duplicated into another table that could drift out of step.
#: §12.3: an application the clean-case gate did not clear is undecided and
#: waiting for a person. It carries no decision, so it is invisible to the
#: queues that key on one — and an application nobody can see is the failure
#: mode this queue exists to prevent.
QUEUE_AWAITING_DECISION = "awaiting_decision"


QUEUE_EVIDENCE_OVERDUE = "evidence_overdue"
QUEUE_IDENTITY_REVIEW = "identity_review"
QUEUE_INTEGRATION_EXCEPTIONS = "integration_exceptions"

QUEUE_LABELS = {
    QUEUE_AWAITING_DECISION: "Awaiting a decision",
    QUEUE_EVIDENCE_OVERDUE: "Evidence overdue",
    QUEUE_IDENTITY_REVIEW: "Identity review",
    QUEUE_INTEGRATION_EXCEPTIONS: "Integration exceptions",
}

_IDENTITY_REASONS = ("identity_review_required", "evidence_conflict",
                     "manual_review_required", "awaiting_additional_evidence")


def queue(name: str):
    """The open items in one named queue."""
    if name == QUEUE_AWAITING_DECISION:
        return (AIAccountReview.objects
                .filter(review_state="human_review", decision__in=("pending", "error"))
                .order_by("submitted_event_at", "created_at"))
    if name == QUEUE_EVIDENCE_OVERDUE:
        return (AIAccountReview.objects
                .filter(decision="held", reason_code="evidence_overdue",
                        review_state="human_review")
                .order_by("submitted_event_at", "decided_at"))
    if name == QUEUE_IDENTITY_REVIEW:
        return (AIAccountReview.objects
                .filter(decision="held", reason_code__in=_IDENTITY_REASONS,
                        review_state="human_review")
                .order_by("decided_at"))
    if name == QUEUE_INTEGRATION_EXCEPTIONS:
        return (V2OutboxEvent.objects
                .filter(status="dead_letter")
                .order_by("-last_attempt_at"))
    raise ValueError(f"unknown queue {name!r}")


def queue_counts() -> dict:
    """Open item counts for every named queue, plus the failed inbox."""
    return {
        QUEUE_AWAITING_DECISION: queue(QUEUE_AWAITING_DECISION).count(),
        QUEUE_EVIDENCE_OVERDUE: queue(QUEUE_EVIDENCE_OVERDUE).count(),
        QUEUE_IDENTITY_REVIEW: queue(QUEUE_IDENTITY_REVIEW).count(),
        QUEUE_INTEGRATION_EXCEPTIONS: (
            queue(QUEUE_INTEGRATION_EXCEPTIONS).count()
            + V2InboxEvent.objects.filter(processing_state="failed").count()
        ),
    }
