"""Admin portal views for auth, reviews, issues, reporting, and governance."""
from __future__ import annotations

import json
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .forms import (
    AcceptInviteForm,
    AdminInviteForm,
    ChangePasswordForm,
    ChangeRoleForm,
    EmailLoginForm,
    FlagResolveForm,
    ManualOverrideForm,
)
from .models import (
    AdminAuditLog,
    AdminInvite,
    AdminUser,
    AIAccountReview,
    AIFlag,
    AIFlaggedIssue,
    DailyReport,
    ExternalBreederProfile,
    ExternalConsultantProfile,
    ExternalUser,
)
from .permissions import admin_required, super_admin_required, write_access_required
from .services import audit
from .services.health import get_health_snapshot
from .services.notifier import notify_invite, notify_password_change
from .services.issue_runner import process_pending_issues
from .services.reporting import build_report_for
from .services.review_runner import manual_override, process_pending, run_review


def login_view(request):
    if request.user.is_authenticated:
        return redirect("admin_portal:dashboard")
    form = EmailLoginForm(request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = authenticate(
            request,
            username=form.cleaned_data["username"].lower(),
            password=form.cleaned_data["password"],
        )
        if user and user.is_active:
            login(request, user)
            audit.record(user, "login", request=request)
            return redirect(request.GET.get("next") or "admin_portal:dashboard")
        messages.error(request, "Invalid email or password.")
    return render(request, "admin_portal/login.html", {"form": form})


@login_required(login_url="admin_portal:login")
def logout_view(request):
    audit.record(request.user, "logout", request=request)
    logout(request)
    return redirect("admin_portal:login")


@admin_required
def dashboard(request):
    today = timezone.now().date()
    last_7 = today - timedelta(days=6)
    reviews = AIAccountReview.objects.all()
    issues = AIFlaggedIssue.objects.all()

    review_counts = reviews.aggregate(
        total=Count("id"),
        approved=Count("id", filter=Q(decision="approved")),
        rejected=Count("id", filter=Q(decision="rejected")),
        flagged=Count("id", filter=Q(decision="flagged")),
        pending=Count("id", filter=Q(decision="pending")),
        overrides=Count("id", filter=Q(manually_overridden=True)),
    )
    issue_counts = issues.aggregate(
        total=Count("id"),
        open=Count("id", filter=Q(resolved=False)),
        critical=Count("id", filter=Q(severity="critical", resolved=False)),
        resolved=Count("id", filter=Q(resolved=True)),
    )
    today_counts = reviews.filter(created_at__date=today).aggregate(
        total=Count("id"),
        approved=Count("id", filter=Q(decision="approved")),
        rejected=Count("id", filter=Q(decision="rejected")),
        flagged=Count("id", filter=Q(decision="flagged")),
        pending=Count("id", filter=Q(decision="pending")),
    )

    week_data = []
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        day_reviews = reviews.filter(created_at__date=day)
        day_issues = issues.filter(created_at__date=day)
        day_counts = day_reviews.aggregate(
            approved=Count("id", filter=Q(decision="approved")),
            rejected=Count("id", filter=Q(decision="rejected")),
            flagged=Count("id", filter=Q(decision="flagged")),
        )
        week_data.append(
            {
                "date": day.strftime("%a"),
                "approved": day_counts["approved"] or 0,
                "rejected": day_counts["rejected"] or 0,
                "flagged": day_counts["flagged"] or 0,
                "issues": day_issues.count(),
            }
        )

    recent_reviews = reviews[:8]
    recent_issues = issues[:8]
    last_reports = DailyReport.objects.filter(report_date__gte=last_7)
    health_snapshot = get_health_snapshot()

    context = {
        "counts": review_counts,
        "issue_counts": issue_counts,
        "today_counts": today_counts,
        "week_data": json.dumps(week_data),
        "recent_reviews": recent_reviews,
        "recent_issues": recent_issues,
        "last_reports": last_reports,
        "health_snapshot": health_snapshot,
        "breeder_count": reviews.filter(subject_type="breeder").count(),
        "consultant_count": reviews.filter(subject_type="consultant").count(),
    }
    return render(request, "admin_portal/dashboard.html", context)


@admin_required
def review_list(request):
    qs = AIAccountReview.objects.all()
    decision = request.GET.get("decision", "").strip()
    subject = request.GET.get("subject", "").strip()
    q = (request.GET.get("q") or "").strip()
    if decision in {"approved", "rejected", "flagged", "pending", "error"}:
        qs = qs.filter(decision=decision)
    if subject in {"breeder", "consultant"}:
        qs = qs.filter(subject_type=subject)
    if q:
        qs = qs.filter(
            Q(subject_user_email__icontains=q) | Q(subject_display_name__icontains=q)
        )
    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    summary = qs.aggregate(
        total=Count("id"),
        pending=Count("id", filter=Q(decision="pending")),
        approved=Count("id", filter=Q(decision="approved")),
        rejected=Count("id", filter=Q(decision="rejected")),
        flagged=Count("id", filter=Q(decision="flagged")),
    )
    return render(
        request,
        "admin_portal/review_list.html",
        {"page": page, "decision": decision, "subject": subject, "q": q, "summary": summary},
    )


@admin_required
def review_detail(request, review_id):
    review = get_object_or_404(AIAccountReview, pk=review_id)
    profile = _load_external_profile(review)
    user = None
    if profile:
        try:
            user = ExternalUser.objects.get(pk=profile.user_id)
        except ExternalUser.DoesNotExist:
            user = None
    flags = review.flags.all().order_by("-created_at")
    return render(
        request,
        "admin_portal/review_detail.html",
        {"review": review, "profile": profile, "external_user": user, "flags": flags},
    )


@super_admin_required
def review_rerun(request, review_id):
    review = get_object_or_404(AIAccountReview, pk=review_id)
    profile = _load_external_profile(review)
    if not profile:
        messages.error(request, "External profile no longer exists; cannot re-run.")
        return redirect("admin_portal:review_detail", review_id=review.id)
    try:
        user = ExternalUser.objects.get(pk=profile.user_id)
    except ExternalUser.DoesNotExist:
        messages.error(request, "External user no longer exists.")
        return redirect("admin_portal:review_detail", review_id=review.id)
    if request.method == "POST":
        run_review(review.subject_type, profile, user)
        audit.record_write(
            request.user,
            "review.rerun",
            target_type="review",
            target_id=review.id,
            request=request,
            summary=f"Re-ran AI review for {review.subject_display_name or review.subject_user_email}.",
        )
        messages.success(request, "Review re-ran with the latest profile state.")
    return redirect("admin_portal:review_detail", review_id=review.id)


@super_admin_required
def review_override(request, review_id):
    review = get_object_or_404(AIAccountReview, pk=review_id)
    if request.method != "POST":
        return redirect("admin_portal:review_detail", review_id=review.id)
    form = ManualOverrideForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Please provide a valid decision and reason.")
        return redirect("admin_portal:review_detail", review_id=review.id)

    new_decision = form.cleaned_data["new_decision"]
    reason = form.cleaned_data["reason"]
    manual_override(review, new_decision, reason, request.user)
    audit.record_write(
        request.user,
        "review.manual_override",
        target_type="review",
        target_id=review.id,
        request=request,
        summary=f"Overrode review to {new_decision} for {review.subject_display_name or review.subject_user_email}.",
        original_decision=review.original_decision,
        new_decision=new_decision,
        reason=reason,
    )
    messages.success(request, f"Review manually overridden to '{new_decision}'.")
    return redirect("admin_portal:review_detail", review_id=review.id)


def _load_external_profile(review: AIAccountReview):
    model = ExternalConsultantProfile if review.subject_type == "consultant" else ExternalBreederProfile
    try:
        return model.objects.get(pk=review.subject_id)
    except model.DoesNotExist:
        return None


@admin_required
def issue_list(request):
    qs = AIFlaggedIssue.objects.all()
    severity = request.GET.get("severity", "").strip()
    source = request.GET.get("source", "").strip()
    show_resolved = request.GET.get("resolved") == "1"
    q = (request.GET.get("q") or "").strip()
    if severity in {"info", "warning", "critical"}:
        qs = qs.filter(severity=severity)
    if source in {"incident", "consultant_warning"}:
        qs = qs.filter(source_type=source)
    if not show_resolved:
        qs = qs.filter(resolved=False)
    if q:
        qs = qs.filter(
            Q(subject_user_email__icontains=q)
            | Q(subject_display_name__icontains=q)
            | Q(title__icontains=q)
            | Q(summary__icontains=q)
        )
    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    summary = qs.aggregate(
        total=Count("id"),
        critical=Count("id", filter=Q(severity="critical")),
        warning=Count("id", filter=Q(severity="warning")),
        info=Count("id", filter=Q(severity="info")),
    )
    return render(
        request,
        "admin_portal/issue_list.html",
        {
            "page": page,
            "severity": severity,
            "source": source,
            "show_resolved": show_resolved,
            "q": q,
            "summary": summary,
        },
    )


@admin_required
def issue_detail(request, issue_id):
    issue = get_object_or_404(AIFlaggedIssue, pk=issue_id)
    return render(
        request,
        "admin_portal/issue_detail.html",
        {"issue": issue, "form": FlagResolveForm()},
    )


@write_access_required
def issue_resolve(request, issue_id):
    issue = get_object_or_404(AIFlaggedIssue, pk=issue_id)
    if request.method != "POST":
        return redirect("admin_portal:issue_detail", issue_id=issue.id)
    form = FlagResolveForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Resolution notes are required.")
        return redirect("admin_portal:issue_detail", issue_id=issue.id)
    issue.resolved = True
    issue.status = "resolved"
    issue.resolved_by = request.user
    issue.resolved_at = timezone.now()
    issue.resolution_notes = form.cleaned_data["resolution_notes"]
    issue.save(update_fields=["resolved", "status", "resolved_by", "resolved_at", "resolution_notes"])
    audit.record_write(
        request.user,
        "issue.resolve",
        target_type="issue",
        target_id=issue.id,
        request=request,
        summary=f"Resolved issue {issue.title or issue.source_label} for {issue.subject_display_name or issue.subject_user_email}.",
        severity=issue.severity,
    )
    messages.success(request, "Issue marked as resolved.")
    return redirect("admin_portal:issue_detail", issue_id=issue.id)


@admin_required
def flag_list(request):
    qs = AIFlag.objects.select_related("review").all()
    severity = request.GET.get("severity", "").strip()
    show_resolved = request.GET.get("resolved") == "1"
    if severity in {"info", "warning", "critical"}:
        qs = qs.filter(severity=severity)
    if not show_resolved:
        qs = qs.filter(resolved=False)
    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "admin_portal/flag_list.html",
        {"page": page, "severity": severity, "show_resolved": show_resolved},
    )


@admin_required
def flag_detail(request, flag_id):
    flag = get_object_or_404(AIFlag.objects.select_related("review"), pk=flag_id)
    return render(request, "admin_portal/flag_detail.html", {"flag": flag, "form": FlagResolveForm()})


@write_access_required
def flag_resolve(request, flag_id):
    flag = get_object_or_404(AIFlag, pk=flag_id)
    if request.method != "POST":
        return redirect("admin_portal:flag_detail", flag_id=flag.id)
    form = FlagResolveForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Resolution notes are required.")
        return redirect("admin_portal:flag_detail", flag_id=flag.id)
    flag.resolved = True
    flag.resolved_by = request.user
    flag.resolved_at = timezone.now()
    flag.resolution_notes = form.cleaned_data["resolution_notes"]
    flag.save(update_fields=["resolved", "resolved_by", "resolved_at", "resolution_notes"])
    audit.record_write(
        request.user,
        "review_flag.resolve",
        target_type="flag",
        target_id=flag.id,
        request=request,
        summary=f"Resolved review flag {flag.id} on {flag.review.subject_display_name or flag.review.subject_user_email}.",
    )
    messages.success(request, "Review flag resolved.")
    return redirect("admin_portal:flag_detail", flag_id=flag.id)


@admin_required
def daily_report_list(request):
    page = Paginator(DailyReport.objects.all(), 30).get_page(request.GET.get("page"))
    return render(request, "admin_portal/daily_report_list.html", {"page": page})


@admin_required
def daily_report_detail(request, report_id):
    report = get_object_or_404(DailyReport, pk=report_id)
    review_ids = (report.details or {}).get("review_ids", [])
    issue_ids = (report.details or {}).get("issue_ids", [])
    reviews = AIAccountReview.objects.filter(id__in=review_ids).order_by("-created_at")
    issues = AIFlaggedIssue.objects.filter(id__in=issue_ids).order_by("-created_at")
    return render(
        request,
        "admin_portal/daily_report_detail.html",
        {"report": report, "reviews": reviews, "issues": issues},
    )


@super_admin_required
def daily_report_run_now(request):
    if request.method == "POST":
        report = build_report_for()
        audit.record_write(
            request.user,
            "report.run_now",
            target_type="daily_report",
            target_id=report.id,
            request=request,
            summary=f"Generated report for {report.report_date}.",
        )
        messages.success(request, f"Report generated for {report.report_date}.")
        return redirect("admin_portal:daily_report_detail", report_id=report.id)
    return redirect("admin_portal:daily_report_list")


@super_admin_required
def audit_log(request):
    qs = AdminAuditLog.objects.select_related("actor").all()
    action_filter = request.GET.get("action", "").strip()
    actor_filter = request.GET.get("actor", "").strip()
    if action_filter:
        qs = qs.filter(action__icontains=action_filter)
    if actor_filter:
        qs = qs.filter(actor__email__icontains=actor_filter)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(
        request,
        "admin_portal/audit_log.html",
        {"page": page, "action_filter": action_filter, "actor_filter": actor_filter},
    )


@super_admin_required
def admin_user_list(request):
    users = AdminUser.objects.order_by("email")
    invites = AdminInvite.objects.filter(accepted_at__isnull=True, revoked=False)
    return render(
        request,
        "admin_portal/admin_user_list.html",
        {"users": users, "invites": invites, "invite_form": AdminInviteForm()},
    )


@super_admin_required
def admin_user_invite(request):
    if request.method != "POST":
        return redirect("admin_portal:admin_user_list")
    form = AdminInviteForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Invalid invite form.")
        return redirect("admin_portal:admin_user_list")
    email = form.cleaned_data["email"].lower().strip()
    role = form.cleaned_data.get("role", "guest")
    if AdminUser.objects.filter(email=email).exists():
        messages.warning(request, f"{email} is already an admin.")
        return redirect("admin_portal:admin_user_list")
    invite = form.save(commit=False)
    invite.email = email
    invite.role = role
    invite.created_by = request.user
    invite.token = secrets.token_urlsafe(32)
    invite.expires_at = timezone.now() + timedelta(days=7)
    invite.save()
    accept_url = request.build_absolute_uri(reverse("admin_portal:invite_accept", args=[invite.token]))
    notify_invite(invite, accept_url)
    audit.record_write(
        request.user,
        "invite.create",
        target_type="invite",
        target_id=invite.id,
        request=request,
        summary=f"Invited {email} with role {role}.",
        email=email,
        role=role,
    )
    messages.success(request, f"Invite sent to {email} as {role}.")
    return redirect("admin_portal:admin_user_list")


@super_admin_required
def admin_user_revoke(request, user_id):
    target = get_object_or_404(AdminUser, pk=user_id)
    if target.is_super_admin:
        messages.error(request, "You cannot revoke a platform super-admin.")
        return redirect("admin_portal:admin_user_list")
    if target.pk == request.user.pk:
        messages.error(request, "You cannot revoke your own account.")
        return redirect("admin_portal:admin_user_list")
    if request.method == "POST":
        target.is_active = False
        target.save(update_fields=["is_active"])
        audit.record_write(
            request.user,
            "admin.revoke",
            target_type="admin_user",
            target_id=target.id,
            request=request,
            summary=f"Deactivated admin user {target.email}.",
        )
        messages.success(request, f"{target.email} deactivated.")
    return redirect("admin_portal:admin_user_list")


@super_admin_required
def invite_cancel(request, invite_id):
    invite = get_object_or_404(AdminInvite, pk=invite_id, accepted_at__isnull=True)
    if request.method == "POST":
        invite.revoked = True
        invite.revoked_at = timezone.now()
        invite.save(update_fields=["revoked", "revoked_at"])
        audit.record_write(
            request.user,
            "invite.cancel",
            target_type="invite",
            target_id=invite.id,
            request=request,
            summary=f"Cancelled invite for {invite.email}.",
        )
        messages.success(request, "Invite cancelled.")
    return redirect("admin_portal:admin_user_list")


def invite_accept(request, token):
    invite = get_object_or_404(AdminInvite, token=token)
    if not invite.is_pending:
        return render(request, "admin_portal/invite_invalid.html", {"invite": invite})
    form = AcceptInviteForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if AdminUser.objects.filter(email=invite.email).exists():
            messages.error(request, "An admin already exists for this email.")
            return redirect("admin_portal:login")
        role = invite.role if invite.role in {"guest", "developer"} else "guest"
        user = AdminUser.objects.create_user(
            email=invite.email,
            password=form.cleaned_data["password1"],
            full_name=form.cleaned_data.get("full_name") or invite.full_name,
            is_staff=True,
            is_platform_super_admin=False,
            invited_by=invite.created_by,
            role=role,
        )
        invite.accepted_at = timezone.now()
        invite.save(update_fields=["accepted_at"])
        login(request, user)
        audit.record(
            user,
            "invite.accept",
            target_type="invite",
            target_id=invite.id,
            request=request,
            role=role,
        )
        messages.success(request, f"Welcome - your account is active with {user.role_display} access.")
        return redirect("admin_portal:dashboard")
    return render(request, "admin_portal/invite_accept.html", {"invite": invite, "form": form})


@super_admin_required
def process_now(request):
    if request.method == "POST":
        try:
            review_counts = process_pending(limit_per_type=25)
            issue_counts = process_pending_issues(limit_per_type=25)
        except Exception as exc:
            messages.error(request, f"AI scan failed: {exc}")
        else:
            audit.record_write(
                request.user,
                "ai.process_now",
                request=request,
                summary=(
                    f"Processed {review_counts['breeder']} breeders, {review_counts['consultant']} consultants, "
                    f"{issue_counts['incident']} incidents, and {issue_counts['consultant_warning']} consultant warnings."
                ),
                **review_counts,
                **issue_counts,
            )
            messages.success(
                request,
                (
                    f"Processed {review_counts['breeder']} breeders, {review_counts['consultant']} consultants, "
                    f"{issue_counts['incident']} incidents, and {issue_counts['consultant_warning']} consultant warnings."
                ),
            )
    return redirect("admin_portal:dashboard")


@admin_required
def api_review_stats(request):
    today = timezone.now().date()
    data = []
    for i in range(29, -1, -1):
        day = today - timedelta(days=i)
        day_reviews = AIAccountReview.objects.filter(created_at__date=day)
        day_issues = AIFlaggedIssue.objects.filter(created_at__date=day)
        day_counts = day_reviews.aggregate(
            approved=Count("id", filter=Q(decision="approved")),
            rejected=Count("id", filter=Q(decision="rejected")),
            flagged=Count("id", filter=Q(decision="flagged")),
        )
        data.append(
            {
                "date": day.isoformat(),
                "label": day.strftime("%d %b"),
                "approved": day_counts["approved"] or 0,
                "rejected": day_counts["rejected"] or 0,
                "flagged": day_counts["flagged"] or 0,
                "issues": day_issues.count(),
            }
        )
    return JsonResponse({"stats": data})


@login_required(login_url="admin_portal:login")
def change_password(request):
    form = ChangePasswordForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if not request.user.check_password(form.cleaned_data["current_password"]):
            messages.error(request, "Current password is incorrect.")
            return render(request, "admin_portal/change_password.html", {"form": form})
        request.user.set_password(form.cleaned_data["new_password1"])
        request.user.password_changed_at = timezone.now()
        request.user.must_change_password = False
        request.user.save(update_fields=["password", "password_changed_at", "must_change_password"])
        update_session_auth_hash(request, request.user)
        audit.record(request.user, "password.change", request=request)
        notify_password_change(request.user)
        messages.success(request, "Password changed successfully.")
        return redirect("admin_portal:dashboard")
    return render(request, "admin_portal/change_password.html", {"form": form})


@super_admin_required
def change_user_role(request, user_id):
    target = get_object_or_404(AdminUser, pk=user_id)
    if target.is_super_admin:
        messages.error(request, "Cannot change the role of a platform super-admin.")
        return redirect("admin_portal:admin_user_list")
    if request.method == "POST":
        form = ChangeRoleForm(request.POST)
        if form.is_valid():
            old_role = target.role
            new_role = form.cleaned_data["role"]
            target.role = new_role
            target.save(update_fields=["role"])
            audit.record_write(
                request.user,
                "admin.role_change",
                target_type="admin_user",
                target_id=target.id,
                request=request,
                summary=f"Changed {target.email} from {old_role} to {new_role}.",
                email=target.email,
                old_role=old_role,
                new_role=new_role,
            )
            messages.success(request, f"{target.email} role changed from {old_role} to {new_role}.")
    return redirect("admin_portal:admin_user_list")
