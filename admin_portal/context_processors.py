from django.conf import settings
from .models import AIFlaggedIssue


def branding(request):
    user = getattr(request, "user", None)
    is_authenticated = user and hasattr(user, "is_authenticated") and user.is_authenticated
    open_issue_count = 0
    if is_authenticated:
        open_issue_count = AIFlaggedIssue.objects.filter(resolved=False).count()
    return {
        "APP_NAME": "Aqua AI Admin",
        "APP_TAGLINE": "AI-driven approval control plane",
        "SUPERADMIN_EMAILS": getattr(settings, "SUPERADMIN_EMAILS", []),
        "IS_SUPER_ADMIN": bool(getattr(user, "is_super_admin", False)) if is_authenticated else False,
        "IS_ADMIN": bool(getattr(user, "is_admin", False)) if is_authenticated else False,
        "CAN_WRITE": bool(getattr(user, "can_write", False)) if is_authenticated else False,
        "USER_ROLE": getattr(user, "role_display", "") if is_authenticated else "",
        "OPEN_ISSUE_COUNT": open_issue_count,
        "LEGACY_ADMIN_REDIRECT_URL": getattr(settings, "LEGACY_ADMIN_REDIRECT_URL", ""),
        "LEGACY_ADMIN_INTERNAL_PATH": getattr(settings, "LEGACY_ADMIN_INTERNAL_PATH", ""),
    }
