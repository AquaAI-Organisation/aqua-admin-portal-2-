"""Building and queuing Admin -> CRM events.

One place that turns a recorded decision into the envelope the CRM expects, so
the row an operator sees and the event the CRM receives cannot drift apart.

Queuing is always transactional: callers are expected to be inside the same
transaction as the state change the event describes (spec §17.3).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

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


def reviews_for_user(user_id) -> list[AIAccountReview]:
    """Every provider review belonging to one platform user.

    A user may hold both a breeder and a consultant profile, and each is a
    separate application to the CRM, so an account-level action has to reach
    all of them.
    """
    from ..models import ExternalBreederProfile, ExternalConsultantProfile

    if not user_id:
        return []
    pairs = []
    for subject_type, model in (("breeder", ExternalBreederProfile),
                                ("consultant", ExternalConsultantProfile)):
        for profile_id in model.objects.filter(user_id=user_id).values_list("pk", flat=True):
            pairs.append((subject_type, profile_id))
    if not pairs:
        return []
    reviews = []
    for subject_type, profile_id in pairs:
        review = AIAccountReview.objects.filter(
            subject_type=subject_type, subject_id=profile_id).first()
        if review is not None:
            reviews.append(review)
    return reviews


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


# ---------------------------------------------------------------------------
# Evidence requests (spec §14)
# ---------------------------------------------------------------------------

#: §14.2. ``licence_changed`` additionally requires a credential snapshot.
EVIDENCE_REASON_CODES = (
    "assessment_rerun", "appeal_received", "licence_changed",
    "stale_evidence", "operator_requested",
)

#: Named checks the CRM understands. The wire type is a free string, but using
#: an agreed vocabulary is what makes a targeted re-check mean anything.
CHECK_COMPANIES_HOUSE = "companies_house"
CHECK_WEBSITE = "website_and_domain"
CHECK_BREEDER_LICENCE = "breeder_licence"
CHECK_SOCIAL = "social_presence"
CHECK_ALL = (CHECK_COMPANIES_HOUSE, CHECK_WEBSITE, CHECK_SOCIAL)

#: The CRM's ``credential_snapshot.breeder_licence`` takes exactly these three,
#: with ``expires_at`` as a bare ``YYYY-MM-DD`` date. All three or nothing: a
#: half-filled object is refused on receipt, and a 422 is not retryable.
LICENCE_SNAPSHOT_FIELDS = ("licence_number", "issuing_authority", "expires_at")
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validated_licence_snapshot(snapshot: dict | None) -> dict | None:
    """Check a credential snapshot against the CRM's schema before sending.

    Built from the same resolver the submitted event uses, so the licence the
    CRM is asked to re-verify is the licence we actually hold.
    """
    licence = (snapshot or {}).get("breeder_licence")
    if not licence:
        return None
    missing = [f for f in LICENCE_SNAPSHOT_FIELDS if not str(licence.get(f) or "").strip()]
    if missing:
        raise ValueError(
            "the breeder licence snapshot is missing " + ", ".join(missing)
            + "; the CRM requires all of " + ", ".join(LICENCE_SNAPSHOT_FIELDS))
    expires_at = str(licence["expires_at"]).strip()
    if not _DATE_ONLY.match(expires_at):
        raise ValueError(
            f"expires_at must be a YYYY-MM-DD date, not {expires_at!r}")
    return {"breeder_licence": {
        "licence_number": str(licence["licence_number"])[:255],
        "issuing_authority": str(licence["issuing_authority"])[:255],
        "expires_at": expires_at,
    }}


def _live_licence_snapshot(review: AIAccountReview) -> dict | None:
    """The licence currently on record for this review's provider."""
    from ..models import ExternalBreederProfile, ExternalUser
    from .payload import breeder_licence_for

    if review.subject_type != "breeder":
        return None
    profile = ExternalBreederProfile.objects.filter(pk=review.subject_id).first()
    if profile is None:
        return None
    user = ExternalUser.objects.filter(pk=profile.user_id).first()
    if user is None:
        return None
    licence = breeder_licence_for(user)
    return {"breeder_licence": licence} if licence else None


def enqueue_evidence_request(review: AIAccountReview, *, reason_code: str,
                             requested_checks=None, credential_snapshot: dict | None = None,
                             environment: str | None = None):
    """Queue ``admin.provider.evidence.requested`` (spec §14).

    ``requested_checks`` must name at least one check — the CRM's schema
    requires it, and an empty request would ask the CRM to redo everything
    without saying so.
    """
    environment = environment or c.key_ring_environment()
    if not environment:
        return None
    if reason_code not in EVIDENCE_REASON_CODES:
        raise ValueError(
            f"reason_code must be one of {', '.join(EVIDENCE_REASON_CODES)}; got {reason_code!r}")

    checks = [str(check)[:160] for check in (requested_checks or CHECK_ALL) if str(check).strip()]
    if not checks:
        raise ValueError("requested_checks must name at least one check")

    # §14.2 and §13.5: a licence_changed request MUST carry the current
    # credential snapshot, otherwise the CRM has nothing to verify against.
    # Resolved from the live record when the caller does not supply one, so the
    # snapshot comes from the same place the submitted event's did.
    if credential_snapshot is None and reason_code == "licence_changed":
        credential_snapshot = _live_licence_snapshot(review)
    snapshot = _validated_licence_snapshot(credential_snapshot)
    if reason_code == "licence_changed" and snapshot is None:
        raise ValueError(
            "a licence_changed evidence request requires the current breeder "
            "licence credential snapshot")

    provider_id = provider_id_for(review)
    if not provider_id:
        logger.error("no aqua_provider_id for review %s; evidence request not queued",
                     review.subject_id)
        return None

    request: dict = {
        "request_id": f"evr_{c.new_ulid()}",
        "reason_code": reason_code,
        "requested_checks": checks,
        "requested_at": c.rfc3339_ms(timezone.now()),
    }
    if review.dossier_revision:
        request["requested_dossier_revision_after"] = int(review.dossier_revision)
    request["credential_snapshot"] = snapshot

    event = _envelope(
        event_type="admin.provider.evidence.requested",
        correlation_id=str(review.subject_id), environment=environment,
        data={
            "application_id": str(review.subject_id),
            "aqua_provider_id": provider_id,
            "request": request,
        },
    )
    row = enqueue(event)
    logger.info("queued evidence request %s for %s (%s)",
                row.event_id, review.subject_id, reason_code)
    return row


# ---------------------------------------------------------------------------
# Account status (spec §15)
# ---------------------------------------------------------------------------

#: §15.1 splits the permitted reasons by status. The CRM's schema accepts the
#: flat union, so validating per status here is deliberately the stricter side
#: of the contract: it stops "reactivated / policy_breach" ever being sent.
ACCOUNT_STATUS_REASONS = {
    "suspended": ("conduct_review", "policy_breach", "provider_request"),
    "reactivated": ("review_resolved", "manual_reactivation"),
}


def enqueue_account_status(review: AIAccountReview, *, status: str, reason_code: str,
                           actor_type: str = "operator", environment: str | None = None):
    """Queue ``admin.provider.account.status_changed`` (spec §15).

    ``status_version`` is incremented under a row lock, so two operators acting
    at once cannot mint the same version and leave the CRM unable to order
    them. A lower version arriving late is ignored by the receiver rather than
    reinstating a suspended provider.
    """
    from django.db import transaction

    environment = environment or c.key_ring_environment()
    if not environment:
        return None
    if status not in ACCOUNT_STATUS_REASONS:
        raise ValueError(f"status must be 'suspended' or 'reactivated'; got {status!r}")
    permitted = ACCOUNT_STATUS_REASONS[status]
    if reason_code not in permitted:
        raise ValueError(
            f"reason_code {reason_code!r} is not permitted for {status!r}; "
            f"allowed: {', '.join(permitted)}")
    if actor_type not in ("operator", "approved_rules_engine", "system_timeout"):
        raise ValueError(f"actor_type {actor_type!r} is not a contract value")

    provider_id = provider_id_for(review)
    if not provider_id:
        logger.error("no aqua_provider_id for review %s; account status not queued",
                     review.subject_id)
        return None

    with transaction.atomic():
        locked = AIAccountReview.objects.select_for_update().get(pk=review.pk)
        locked.account_status_version = (locked.account_status_version or 0) + 1
        locked.save(update_fields=["account_status_version"])
        version = locked.account_status_version

        event = _envelope(
            event_type="admin.provider.account.status_changed",
            correlation_id=str(locked.subject_id), environment=environment,
            data={
                "application_id": str(locked.subject_id),
                "aqua_provider_id": provider_id,
                "account_status": {
                    "status_version": version,
                    "status": status,
                    "reason_code": reason_code,
                    "changed_at": c.rfc3339_ms(timezone.now()),
                    "actor_type": actor_type,
                },
            },
        )
        row = enqueue(event)
    logger.info("queued account status %s for %s (v%s %s)",
                row.event_id, review.subject_id, version, status)
    review.refresh_from_db()
    return row


# ---------------------------------------------------------------------------
# Erasure (spec §16)
# ---------------------------------------------------------------------------

def enqueue_erasure(review: AIAccountReview, *, request_id: str, legal_basis: str,
                    requested_at=None, environment: str | None = None):
    """Queue ``admin.provider.erased`` (spec §16).

    Sent only after an erasure has been approved by its own process. The CRM
    then stops all evidence and outreach for the profile and deletes or
    anonymises within 30 days, keeping only a salted suppression hash so a
    rediscovered applicant is never contacted again.
    """
    environment = environment or c.key_ring_environment()
    if not environment:
        return None
    request_id = (request_id or "").strip()
    legal_basis = (legal_basis or "").strip()
    if not request_id:
        raise ValueError("request_id is required for an erasure event")
    if not legal_basis:
        raise ValueError("legal_basis is required for an erasure event")

    provider_id = provider_id_for(review)
    if not provider_id:
        logger.error("no aqua_provider_id for review %s; erasure not queued", review.subject_id)
        return None

    # Erasure is not reversible on the CRM side, and a DSAR queue that is worked
    # twice must not produce two of them.
    if review.erasure_requested_at:
        logger.info("erasure already queued for %s at %s; not queuing again",
                    review.subject_id, review.erasure_requested_at.isoformat())
        return None

    event = _envelope(
        event_type="admin.provider.erased",
        correlation_id=str(review.subject_id), environment=environment,
        data={
            "application_id": str(review.subject_id),
            "aqua_provider_id": provider_id,
            "erasure": {
                "request_id": request_id[:160],
                "requested_at": c.rfc3339_ms(requested_at or timezone.now()),
                "legal_basis": legal_basis[:160],
            },
        },
    )
    row = enqueue(event)
    AIAccountReview.objects.filter(pk=review.pk).update(erasure_requested_at=timezone.now())
    review.refresh_from_db()
    logger.info("queued erasure %s for %s", row.event_id, review.subject_id)
    return row


# ---------------------------------------------------------------------------
# Stale evidence (spec §12.2)
# ---------------------------------------------------------------------------

STALE_AFTER = timedelta(days=30)


def _checked_at(value) -> datetime | None:
    """Parse a dossier ``checked_at``, which the CRM pins to ``.mmmZ``."""
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=dt_timezone.utc)
    except (ValueError, TypeError):
        return None


def dossier_is_stale(review: AIAccountReview, *, now=None) -> bool:
    """Whether a decisive source in the held dossier is older than 30 days.

    §12.2 names Companies House and breeder licence verification as decisive.
    Each carries its own ``checked_at`` inside the dossier, which is what makes
    a dossier stale rather than merely old: the CRM may have assembled it today
    from a check it made months ago.

    A decisive source that is present but cannot be dated counts as stale. The
    question is whether we can show the evidence is current, and an unparseable
    timestamp is not a demonstration of freshness — treating it as fresh would
    let the one case we cannot vouch for through unchallenged. A source absent
    from the payload altogether is not judged here; that is a completeness
    question for the dossier status, not a staleness one.
    """
    if not review.dossier_id or not isinstance(review.dossier_payload, dict):
        return False
    now = now or timezone.now()
    cutoff = now - STALE_AFTER

    decisive = []
    house = review.dossier_payload.get("companies_house")
    if isinstance(house, dict):
        decisive.append(house)
    for licence in review.dossier_payload.get("licences") or []:
        if isinstance(licence, dict):
            decisive.append(licence)

    for source in decisive:
        checked = _checked_at(source.get("checked_at"))
        if checked is None or checked < cutoff:
            return True
    return False


def request_fresh_evidence_if_stale(review: AIAccountReview, *, now=None):
    """Ask the CRM to re-check decisive evidence that has gone stale (§12.2).

    This sends the request. It does NOT block the decision: §12.2 also requires
    that a stale dossier cannot be approved or rejected until fresh evidence
    arrives, and that gate belongs with the approval gates (§12.3), which are
    not built yet. Calling this on its own refreshes the evidence without
    holding the case, so it must not be read as §12.2 being enforced.
    """
    if not dossier_is_stale(review, now=now):
        return None
    return enqueue_evidence_request(
        review, reason_code="stale_evidence",
        requested_checks=[CHECK_COMPANIES_HOUSE, CHECK_BREEDER_LICENCE, CHECK_WEBSITE])
