import logging

from ..models import AdminAuditLog
from .notifier import notify_developer_action

logger = logging.getLogger(__name__)


def get_client_ip(request):
    """Extract client IP from request."""
    x_forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded:
        return x_forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def record(actor, action: str, *, target_type: str = "", target_id: str = "",
           request=None, **details):
    ip = get_client_ip(request) if request else None
    AdminAuditLog.objects.create(
        actor=actor if (actor and hasattr(actor, 'is_authenticated') and actor.is_authenticated) else None,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id else "",
        details=details,
        ip_address=ip,
    )


def record_write(actor, action: str, *, summary: str, target_type: str = "", target_id: str = "",
                 request=None, **details):
    record(
        actor,
        action,
        target_type=target_type,
        target_id=target_id,
        request=request,
        **details,
    )
    if getattr(actor, "is_developer", False) or getattr(actor, "is_admin", False):
        notify_developer_action(actor, action, summary)


# ---------------------------------------------------------------------------
# Automated transitions (spec §25 item 15)
# ---------------------------------------------------------------------------

#: The actions the system records against itself. Kept as a closed vocabulary
#: because the audit view filters on it, and because an action name invented at
#: the call site is a line nobody can search for afterwards.
SYSTEM_ACTIONS = {
    "v2.decision.recorded": "A durable decision was recorded against an application.",
    "v2.decision.safety_hold": "An application was held because evidence did not arrive in time.",
    "v2.dossier.received": "A CRM evidence dossier was accepted and attached to an application.",
    "v2.dossier.refused": "A CRM evidence dossier was refused and not attached.",
    "v2.outbox.dead_letter": "An outbound event was abandoned and needs a person.",
    "v2.erasure.queued": "An erasure instruction was queued to the CRM.",
    "review.sent_to_human": "An application was parked for a person because the clean-case gate was not satisfied.",
    "review.auto_approved": "An account was activated without an operator.",
    "review.auto_deactivated": "An account was deactivated without an operator.",
}


def record_system(action: str, *, target_type: str = "", target_id: str = "",
                  actor=None, **details) -> None:
    """Record something the system did, with no request behind it.

    Separate from :func:`record_write` because that notifies developers on
    every write: these fire from schedulers and webhook handlers, and a
    notification per automated transition would bury the ones a person chose
    to make. Failing to write the audit row must never take down the
    transition it was describing, so this swallows its own errors — an audit
    trail that can roll back a decision is worse than a gap in the trail.
    """
    if action not in SYSTEM_ACTIONS:
        raise ValueError(
            f"{action!r} is not a known system action; add it to SYSTEM_ACTIONS "
            "so it can be found and filtered later")
    try:
        AdminAuditLog.objects.create(
            # An operator may be attributable even on an automated path (a
            # manual override still goes through the canonical decision
            # recorder); None means nobody chose this.
            actor=actor if (actor and getattr(actor, "pk", None)) else None,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id else "",
            details=details,
            ip_address=None,
        )
    except Exception:
        logger.exception("could not record system audit entry %s for %s",
                         action, target_id)
