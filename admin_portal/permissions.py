from functools import wraps

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden


def super_admin_required(view_func):
    """Allow only Steven@humara.io / Ben@humara.io (platform super-admins)."""

    @login_required(login_url="admin_portal:login")
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        user = request.user
        if not getattr(user, "is_super_admin", False):
            return HttpResponseForbidden(
                "Only Steven@humara.io and Ben@humara.io can perform this action."
            )
        return view_func(request, *args, **kwargs)

    return _wrapped


def admin_required(view_func):
    """Any active admin in this control plane (guest, developer, super_admin)."""

    @login_required(login_url="admin_portal:login")
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_active:
            return HttpResponseForbidden("Your admin account is inactive.")
        # Force password change if flagged
        if getattr(request.user, "must_change_password", False):
            from django.shortcuts import redirect
            from django.urls import reverse
            if request.path != reverse("admin_portal:change_password"):
                return redirect("admin_portal:change_password")
        return view_func(request, *args, **kwargs)

    return _wrapped


def write_access_required(view_func):
    """Only developers and super-admins can perform write operations.
    Guests get a 403. Developer writes trigger super-admin notifications."""

    @login_required(login_url="admin_portal:login")
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        user = request.user
        if not user.is_active:
            return HttpResponseForbidden("Your admin account is inactive.")
        if getattr(user, "is_guest", False):
            return HttpResponseForbidden(
                "Your account has read-only (Guest) access. "
                "Contact Steven or Ben to upgrade your role."
            )
        return view_func(request, *args, **kwargs)

    return _wrapped
