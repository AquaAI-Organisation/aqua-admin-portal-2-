"""Building and queuing Admin -> CRM events.

One place that turns a recorded decision into the envelope the CRM expects, so
the row an operator sees and the event the CRM receives cannot drift apart.

Queuing is always transactional: callers are expected to be inside the same
transaction as the state change the event describes (spec §17.3).
"""
from __future__ import annotations

import logging

from django.utils import timezone

from ..models import AIAccountReview
from . import contract as c
from .decisions import decision_payload
from .outbox import enqueue

logger = logging.getLogger(__name__)


def _envelope(*, event_type: str, correlation_id: str, data: dict,
              environment: str, occurred_at=None) -> dict:
    return {
        "schema_version": c.SCHEMA_VERSION,
        "event_id": c.new_ulid(),
        "event_type": event_type,
        "occurred_at": c.rfc3339_ms(occurred_at or timezone.now()),
        "correlation_id": correlation_id,
        "producer": {"system": "aqua-admin-portal", "environment": environment},
        "data": data,
    }


def provider_id_for(review: AIAccountReview) -> str:
    """The ``aqua_provider_id`` (user UUID) for this review.

    Preferred from the value captured when the submitted event was built, since
    the profile may later be erased. Falls back to a live lookup for reviews
    created before that field existed.
    """
    if review.aqua_provider_id:
        return str(review.aqua_provider_id)
    from ..models import ExternalBreederProfile, ExternalConsultantProfile
    model = (ExternalBreederProfile if review.subject_type == "breeder"
             else ExternalConsultantProfile)
    profile = model.objects.filter(pk=review.subject_id).first()
    return str(profile.user_id) if profile is not None else ""


def enqueue_decision_event(review: AIAccountReview, *, environment: str | None = None):
    """Queue ``admin.provider.application.decision.recorded`` (spec §13.1).

    Emitted after every durable decision creation or replacement, including a
    ``held / evidence_overdue`` safety hold and any later human resolution.
    Returns the outbox row, or None when no CRM environment is configured.
    """
    environment = environment or c.key_ring_environment()
    if not environment:
        return None
    if not review.decision_id:
        raise ValueError(
            "a decision event requires a recorded decision; call record_decision first")

    provider_id = provider_id_for(review)
    if not provider_id:
        # Without a provider id the CRM cannot attach the decision. Better to
        # leave it unqueued and let reconciliation surface it than to send an
        # event the receiver will refuse.
        logger.error("no aqua_provider_id for review %s; decision event not queued",
                     review.subject_id)
        return None

    event = _envelope(
        event_type="admin.provider.application.decision.recorded",
        correlation_id=str(review.subject_id),
        environment=environment,
        occurred_at=review.decided_at,
        data={
            "application_id": str(review.subject_id),
            "aqua_provider_id": provider_id,
            "decision": decision_payload(review),
        },
    )
    row = enqueue(event)
    logger.info("queued decision event %s for %s (v%s %s)",
                row.event_id, review.subject_id, review.decision_version, review.decision)
    return row
