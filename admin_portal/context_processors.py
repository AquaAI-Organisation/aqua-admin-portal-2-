from django.conf import settings


def branding(request):
    user = getattr(request, "user", None)
    is_authenticated = bool(
        user and hasattr(user, "is_authenticated") and user.is_authenticated
    )

    open_flag_count = 0
    if is_authenticated:
        try:
            from .models import AIFlag
            open_flag_count = AIFlag.objects.filter(resolved=False).count()
        except Exception:
            open_flag_count = 0

    api_key = getattr(settings, "OPENAI_API_KEY", "") or ""
    ai_key_set = bool(api_key) and "REPLACE" not in api_key.upper()

    return {
        "APP_NAME": "Aqua AI Admin",
        "APP_TAGLINE": "AI-driven approval control plane",
        "SUPERADMIN_EMAILS": getattr(settings, "SUPERADMIN_EMAILS", []),
        "IS_SUPER_ADMIN": bool(getattr(user, "is_super_admin", False)) if is_authenticated else False,
        "CAN_WRITE": bool(getattr(user, "can_write", False)) if is_authenticated else False,
        "USER_ROLE": getattr(user, "role_display", "") if is_authenticated else "",
        "AI_KEY_SET": ai_key_set,
        "OPEN_FLAG_COUNT": open_flag_count,
    }
