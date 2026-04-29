from ..models import AdminAuditLog
from .notifier import notify_developer_action


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
