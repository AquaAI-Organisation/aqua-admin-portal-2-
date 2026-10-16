"""Recording a provider decision in V2.3 terms (spec §13).

Every durable decision — automatic, manual or a safety hold — goes through
:func:`record_decision`. Having one path is the point: ``decision_version`` has
to be monotonic per profile, ``reason_code`` has to belong to its status, and
the actor-type rules exist to stop a rules engine recording a rejection. Those
are invariants, not conventions, and the CRM enforces them on receipt — an
invalid combination is a 422 that no retry can fix, so it is cheaper to refuse
to build one here.

This module records the decision. It does not decide anything, does not emit
the outbox event, and does not apply account state; those are separate steps
with their own review.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from ..models import (
    V2_ACTOR_TYPE_CHOICES,
    V2_DECISION_PATH_CHOICES,
    V2_REASON_CODES_BY_STATUS,
    AIAccountReview,
    canonical_decision,
)
from . import contract as c

logger = logging.getLogger(__name__)

#: §13.3 — the only statuses a decision may carry. ``pending`` and ``error``
#: are internal review states, never decisions.
DECISION_STATUSES = ("approved", "rejected", "held")

ACTOR_TYPES = tuple(value for value, _label in V2_ACTOR_TYPE_CHOICES)
DECISION_PATHS = tuple(value for value, _label in V2_DECISION_PATH_CHOICES)

#: §12.3 — the clean-case score floor. An automatic approval below this, or
#: with any unresolved flag, is not a clean case.
CLEAN_CASE_MIN_SCORE = 0.75

def _state_after(status: str, decision_path: str) -> str:
    """Where a decision leaves the review in the §21.1 state machine.

    Status decides this, not the path. ``held`` is not an outcome: §12.3 says a
    held case MUST reach a named person and MUST NOT auto-promote, so it stays
    in ``human_review`` whichever path recorded it — an operator recording
    "needs more evidence" leaves the case just as open as a system safety hold
    does. Only an approval or a rejection is terminal.
    """
    if status == "held":
        return "human_review"
    if status == "approved" and decision_path == "automatic_clean_case":
        return "automatic_approved"
    return "decided"


class DecisionRefused(ValueError):
    """The requested decision violates the contract and was not recorded."""


def _validate(*, status: str, reason_code: str, actor_type: str, decision_path: str,
              score: float | None, flags: list, dossier_id: str) -> None:
    if status not in DECISION_STATUSES:
        raise DecisionRefused(
            f"status must be one of {', '.join(DECISION_STATUSES)}; got {status!r}")

    permitted = V2_REASON_CODES_BY_STATUS[status]
    if reason_code not in permitted:
        raise DecisionRefused(
            f"reason_code {reason_code!r} is not permitted for {status!r}; "
            f"allowed: {', '.join(permitted)}")

    if actor_type not in ACTOR_TYPES:
        raise DecisionRefused(f"decision_actor_type must be one of {', '.join(ACTOR_TYPES)}")
    if decision_path not in DECISION_PATHS:
        raise DecisionRefused(f"decision_path must be one of {', '.join(DECISION_PATHS)}")

    # §13.3: "Rejection requires operator."
    if status == "rejected" and actor_type != "operator":
        raise DecisionRefused("only an operator may record a rejection")

    # §13.3: "system_timeout is permitted only for a safety hold."
    if actor_type == "system_timeout" and not (
            status == "held" and decision_path == "system_safety_hold"):
        raise DecisionRefused("system_timeout is only permitted for a safety hold")

    # §13.3 + §12.3: an automatic approval must be a clean case, and must carry
    # the evidence that made it one.
    if actor_type == "approved_rules_engine":
        if status != "approved":
            raise DecisionRefused("approved_rules_engine may only record an approval")
        if decision_path != "automatic_clean_case":
            raise DecisionRefused(
                "an approved_rules_engine decision must carry decision_path "
                "'automatic_clean_case'")
        if score is None or score < CLEAN_CASE_MIN_SCORE:
            raise DecisionRefused(
                f"a clean-case approval requires a score of at least {CLEAN_CASE_MIN_SCORE}; "
                f"got {score!r}")
        if flags:
            raise DecisionRefused(
                f"a clean-case approval requires zero unresolved flags; got {len(flags)}")
        if not dossier_id:
            raise DecisionRefused("a clean-case approval requires a terminal dossier reference")

    # A safety hold is the one decision allowed to exist without a dossier
    # (§13.3, "a timeout hold may have no dossier reference").
    if decision_path == "automatic_clean_case" and actor_type != "approved_rules_engine":
        raise DecisionRefused(
            "decision_path 'automatic_clean_case' belongs to approved_rules_engine only")


def record_decision(
    review: AIAccountReview,
    *,
    status: str,
    reason_code: str,
    reason_text: str,
    actor_type: str,
    decision_path: str,
    actor=None,
    score: float | None = None,
    flags: list | None = None,
    use_dossier: bool = True,
) -> AIAccountReview:
    """Record one durable decision against ``review``.

    Increments ``decision_version`` under a row lock so two concurrent
    operators cannot mint the same version, mints a ``decision_id``, and moves
    the review to its §21.1 state. Raises :class:`DecisionRefused` without
    writing anything when the combination is not permitted.
    """
    status = canonical_decision(status)
    flags = list(flags or [])
    reason_text = (reason_text or "").strip()
    if not reason_text:
        raise DecisionRefused("reason_text is required and may not be blank")
    if len(reason_text) > 1000:
        raise DecisionRefused("reason_text may not exceed 1000 characters")

    with transaction.atomic():
        locked = AIAccountReview.objects.select_for_update().get(pk=review.pk)
        dossier_id = locked.dossier_id if use_dossier else ""
        _validate(status=status, reason_code=reason_code, actor_type=actor_type,
                  decision_path=decision_path, score=score, flags=flags,
                  dossier_id=dossier_id)

        locked.decision = status
        locked.decision_version = (locked.decision_version or 0) + 1
        locked.decision_id = f"dec_{c.new_ulid()}"
        locked.reason_code = reason_code
        locked.decision_actor_type = actor_type
        locked.decision_path = decision_path
        locked.rationale = reason_text
        locked.decided_at = timezone.now()
        locked.review_state = _state_after(status, decision_path)
        if score is not None:
            locked.confidence = float(score)
        if actor is not None:
            locked.overridden_by = actor
        locked.save(update_fields=[
            "decision", "decision_version", "decision_id", "reason_code",
            "decision_actor_type", "decision_path", "rationale", "decided_at",
            "review_state", "confidence", "overridden_by",
        ])

    logger.info("v2 decision recorded: %s %s v%s (%s / %s)",
                locked.subject_id, locked.decision, locked.decision_version,
                locked.reason_code, locked.decision_actor_type)
    # Every durable decision leaves a trail, whoever or whatever made it. This
    # is the one place all of them pass through, so auditing here is what makes
    # "who decided this, and on what" answerable for automatic decisions and
    # safety holds as well as operator ones.
    from ..services.audit import record_system

    record_system(
        "v2.decision.safety_hold" if decision_path == "system_safety_hold"
        else "v2.decision.recorded",
        target_type="review", target_id=locked.subject_id, actor=actor,
        decision_id=locked.decision_id,
        decision_version=locked.decision_version,
        status=locked.decision,
        reason_code=locked.reason_code,
        reason_text=reason_text[:500],
        actor_type=actor_type,
        decision_path=decision_path,
        review_state=locked.review_state,
        score=float(locked.confidence or 0.0),
        evidence_dossier_id=locked.dossier_id or "",
        evidence_dossier_revision=locked.dossier_revision,
    )
    review.refresh_from_db()
    return locked


def decision_payload(review: AIAccountReview) -> dict:
    """The ``decision`` object for an outbound event (spec §13.2).

    Built here so the recorded row and the event the CRM receives can never
    describe different decisions.
    """
    payload = {
        "decision_id": review.decision_id,
        "decision_version": review.decision_version,
        "status": canonical_decision(review.decision),
        "reason_code": review.reason_code,
        "reason_text": (review.rationale or "")[:1000],
        "decided_at": c.rfc3339_ms(review.decided_at or timezone.now()),
        "decision_actor_type": review.decision_actor_type,
        "review": {
            "score": round(float(review.confidence or 0.0), 4),
            "flags": list(review.unresolved_flag_reasons()),
            "decision_path": review.decision_path,
        },
    }
    # §13.3: required for ordinary decisions; a timeout hold may omit it.
    if review.dossier_id:
        payload["evidence_dossier_id"] = review.dossier_id
        payload["evidence_dossier_revision"] = review.dossier_revision
    return payload
