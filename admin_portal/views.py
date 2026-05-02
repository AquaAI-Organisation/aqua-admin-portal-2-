"""Admin portal views for auth, reviews, issues, reporting, and governance."""
from __future__ import annotations

import json
import secrets
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import FileResponse, Http404, JsonResponse
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
    OperationalSettingsForm,
    SupportReplyForm,
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
    OperationalSettings,
    SupportInquiry,
)
from .permissions import admin_required, operational_admin_required, super_admin_required
from .services import audit
from .services.health import get_health_snapshot
from .services.inquiry_intelligence import apply_inquiry_action, persist_inquiry_analysis
from .services.mailbox import fetch_support_inbox, send_support_reply
from .services.notifier import notify_invite, notify_password_change
from .services.issue_runner import process_pending_issues
from .services.reporting import build_report_for
from .services.review_runner import manual_override, process_pending, run_review


def background_video(request):
    video_path = Path(settings.BASE_DIR) / "admin_portal" / "static" / "admin_portal" / "media" / "underwater-reef.mp4"
    if not video_path.exists():
        raise Http404("Background video not found.")
    response = FileResponse(video_path.open("rb"), content_type="video/mp4")
    response["Cache-Control"] = "public, max-age=86400"
    return response


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
        "pending_breeder_count": ExternalBreederProfile.objects.filter(is_verified=False).count(),
        "pending_consultant_count": ExternalConsultantProfile.objects.filter(admin_status="pending").count(),
    }
    return render(request, "admin_portal/dashboard.html", context)


@admin_required
def intake_list(request):
    role = request.GET.get("role", "").strip()
    ai_state = request.GET.get("ai_state", "").strip()
    q = (request.GET.get("q") or "").strip()

    breeder_qs = (
        ExternalBreederProfile.objects
        .filter(is_verified=False)
        .select_related("user")
        .order_by("-created_at")
    )
    consultant_qs = (
        ExternalConsultantProfile.objects
        .filter(admin_status="pending")
        .select_related("user")
        .order_by("-created_at")
    )

    if q:
        breeder_qs = breeder_qs.filter(
            Q(company_name__icontains=q)
            | Q(user__email__icontains=q)
            | Q(user__username__icontains=q)
            | Q(user__name__icontains=q)
        )
        consultant_qs = consultant_qs.filter(
            Q(company_name__icontains=q)
            | Q(user__email__icontains=q)
            | Q(user__username__icontains=q)
            | Q(user__name__icontains=q)
        )

    breeders = list(breeder_qs if role in {"", "breeder"} else [])
    consultants = list(consultant_qs if role in {"", "consultant"} else [])

    breeder_reviews = {
        review.subject_id: review
        for review in AIAccountReview.objects.filter(
            subject_type="breeder",
            subject_id__in=[profile.id for profile in breeders],
        )
    }
    consultant_reviews = {
        review.subject_id: review
        for review in AIAccountReview.objects.filter(
            subject_type="consultant",
            subject_id__in=[profile.id for profile in consultants],
        )
    }

    rows = []
    for profile in breeders:
        review = breeder_reviews.get(profile.id)
        review_state = review.decision if review else "not_scanned"
        rows.append(
            {
                "role": "breeder",
                "created_at": profile.created_at,
                "company_name": profile.company_name or "",
                "display_name": profile.company_name or profile.user.name or profile.user.username or profile.user.email,
                "user_email": profile.user.email,
                "username": profile.user.username,
                "entity_id": str(profile.id),
                "is_active": bool(profile.is_active),
                "source_status": "Pending verification",
                "source_detail": f"Verified: {'Yes' if profile.is_verified else 'No'} | Active: {'Yes' if profile.is_active else 'No'}",
                "review": review,
                "ai_state": review_state,
                "ai_label": "Not scanned" if review_state == "not_scanned" else review_state.replace("_", " "),
                "ai_badge_class": {
                    "not_scanned": "muted",
                    "pending": "pending",
                    "approved": "ok",
                    "rejected": "danger",
                    "flagged": "warn",
                    "error": "danger",
                }.get(review_state, "muted"),
                "failure_reason": review.error_summary if review and review.error else "",
            }
        )

    for profile in consultants:
        review = consultant_reviews.get(profile.id)
        review_state = review.decision if review else "not_scanned"
        rows.append(
            {
                "role": "consultant",
                "created_at": profile.created_at,
                "company_name": profile.company_name or "",
                "display_name": profile.company_name or profile.user.name or profile.user.username or profile.user.email,
                "user_email": profile.user.email,
                "username": profile.user.username,
                "entity_id": str(profile.id),
                "is_active": bool(profile.is_active),
                "source_status": "Pending admin approval",
                "source_detail": f"Admin status: {profile.admin_status or 'pending'} | Active: {'Yes' if profile.is_active else 'No'}",
                "review": review,
                "ai_state": review_state,
                "ai_label": "Not scanned" if review_state == "not_scanned" else review_state.replace("_", " "),
                "ai_badge_class": {
                    "not_scanned": "muted",
                    "pending": "pending",
                    "approved": "ok",
                    "rejected": "danger",
                    "flagged": "warn",
                    "error": "danger",
                }.get(review_state, "muted"),
                "failure_reason": review.error_summary if review and review.error else "",
            }
        )

    if ai_state in {"not_scanned", "pending", "approved", "rejected", "flagged", "error"}:
        rows = [row for row in rows if row["ai_state"] == ai_state]

    oldest_fallback = timezone.now() - timedelta(days=36500)
    rows.sort(key=lambda row: row["created_at"] or oldest_fallback, reverse=True)
    page = Paginator(rows, 25).get_page(request.GET.get("page"))
    summary = {
        "total": len(rows),
        "breeders": sum(1 for row in rows if row["role"] == "breeder"),
        "consultants": sum(1 for row in rows if row["role"] == "consultant"),
        "not_scanned": sum(1 for row in rows if row["ai_state"] == "not_scanned"),
        "error": sum(1 for row in rows if row["ai_state"] == "error"),
    }
    return render(
        request,
        "admin_portal/intake_list.html",
        {
            "page": page,
            "role": role,
            "ai_state": ai_state,
            "q": q,
            "summary": summary,
        },
    )


def _ensure_review(subject_type: str, profile, user) -> AIAccountReview:
    review = AIAccountReview.objects.filter(subject_type=subject_type, subject_id=profile.id).first()
    if review:
        return review
    display = (profile.company_name or user.name or f"{user.first_name} {user.last_name}").strip()
    return AIAccountReview.objects.create(
        subject_type=subject_type,
        subject_id=profile.id,
        subject_user_email=user.email,
        subject_display_name=display[:255],
        decision="pending",
        confidence=0.0,
        rationale="",
        evidence={},
        recommended_actions=[],
        applied_actions=[],
        openai_raw={},
        ai_model="",
        error="",
    )


@operational_admin_required
def intake_decide(request, entity_type, entity_id, action):
    if request.method != "POST":
        return redirect("admin_portal:intake_list")
    next_url = request.POST.get("next") or reverse("admin_portal:intake_list")
    action = (action or "").strip().lower()
    if entity_type not in {"breeder", "consultant"}:
        messages.error(request, "Invalid intake entity type.")
        return redirect(next_url)
    if action not in {"approve", "reject", "suspend", "reactivate"}:
        messages.error(request, "Invalid intake action.")
        return redirect(next_url)

    profile_model = ExternalBreederProfile if entity_type == "breeder" else ExternalConsultantProfile
    profile = get_object_or_404(profile_model.objects.select_related("user"), pk=entity_id)
    user = profile.user

    try:
        if action == "approve":
            review = _ensure_review(entity_type, profile, user)
            manual_override(review, "approved", "Approved from Pending Intake.", request.user)
            summary = f"{profile.company_name or user.email} approved from Pending Intake."
        elif action == "reject":
            review = _ensure_review(entity_type, profile, user)
            manual_override(review, "rejected", "Rejected from Pending Intake.", request.user)
            summary = f"{profile.company_name or user.email} rejected from Pending Intake."
        else:
            activate = action == "reactivate"
            summary = _set_entity_active_state(entity_type, entity_id, activate=activate, actor=request.user)
    except Exception as exc:
        messages.error(request, f"Could not update intake account: {exc}")
        return redirect(next_url)

    audit.record_write(
        request.user,
        f"intake.{action}",
        target_type=entity_type,
        target_id=entity_id,
        request=request,
        summary=summary,
        entity_type=entity_type,
        intake_action=action,
    )
    messages.success(request, summary)
    return redirect(next_url)


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


@operational_admin_required
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
    reason = form.cleaned_data["resolved_reason"]
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
def entity_directory(request):
    entity_type = (request.GET.get("entity_type") or "breeder").strip()
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()

    rows = []
    if entity_type == "consultant":
        qs = ExternalConsultantProfile.objects.select_related("user").order_by("-created_at")
        if q:
            qs = qs.filter(
                Q(company_name__icontains=q)
                | Q(user__email__icontains=q)
                | Q(user__username__icontains=q)
                | Q(user__name__icontains=q)
                | Q(user__first_name__icontains=q)
                | Q(user__last_name__icontains=q)
            )
        if status == "active":
            qs = qs.filter(is_active=True, user__is_active=True)
        elif status == "inactive":
            qs = qs.filter(Q(is_active=False) | Q(user__is_active=False))
        for profile in qs:
            rows.append(
                {
                    "entity_type": "consultant",
                    "entity_id": str(profile.id),
                    "display_name": profile.company_name or profile.user.name or profile.user.username or profile.user.email,
                    "email": profile.user.email,
                    "name": profile.user.name or f"{profile.user.first_name} {profile.user.last_name}".strip() or "-",
                    "role_label": "Consultant",
                    "is_active": bool(profile.is_active and profile.user.is_active),
                    "status_detail": f"Admin status: {profile.admin_status or '-'} | Verified: {'Yes' if profile.is_verified else 'No'}",
                    "created_at": profile.created_at,
                    "target": profile,
                }
            )
    elif entity_type == "user":
        qs = ExternalUser.objects.exclude(role__in=["breeder", "consultant"]).order_by("-created_at", "-date_joined")
        if q:
            qs = qs.filter(
                Q(email__icontains=q)
                | Q(username__icontains=q)
                | Q(name__icontains=q)
                | Q(first_name__icontains=q)
                | Q(last_name__icontains=q)
            )
        if status == "active":
            qs = qs.filter(is_active=True)
        elif status == "inactive":
            qs = qs.filter(is_active=False)
        for user in qs:
            rows.append(
                {
                    "entity_type": "user",
                    "entity_id": str(user.id),
                    "display_name": user.name or user.username or user.email,
                    "email": user.email,
                    "name": user.name or f"{user.first_name} {user.last_name}".strip() or "-",
                    "role_label": "User",
                    "is_active": bool(user.is_active),
                    "status_detail": f"Role: {user.role or '-'} | Verified: {'Yes' if user.is_verified else 'No'}",
                    "created_at": user.created_at or user.date_joined,
                    "target": user,
                }
            )
    else:
        entity_type = "breeder"
        qs = ExternalBreederProfile.objects.select_related("user").order_by("-created_at")
        if q:
            qs = qs.filter(
                Q(company_name__icontains=q)
                | Q(user__email__icontains=q)
                | Q(user__username__icontains=q)
                | Q(user__name__icontains=q)
                | Q(user__first_name__icontains=q)
                | Q(user__last_name__icontains=q)
            )
        if status == "active":
            qs = qs.filter(is_active=True, user__is_active=True)
        elif status == "inactive":
            qs = qs.filter(Q(is_active=False) | Q(user__is_active=False))
        for profile in qs:
            rows.append(
                {
                    "entity_type": "breeder",
                    "entity_id": str(profile.id),
                    "display_name": profile.company_name or profile.user.name or profile.user.username or profile.user.email,
                    "email": profile.user.email,
                    "name": profile.user.name or f"{profile.user.first_name} {profile.user.last_name}".strip() or "-",
                    "role_label": "Breeder",
                    "is_active": bool(profile.is_active and profile.user.is_active),
                    "status_detail": f"Verified: {'Yes' if profile.is_verified else 'No'} | Verification level: {profile.verification_level or '-'}",
                    "created_at": profile.created_at,
                    "target": profile,
                }
            )

    oldest_fallback = timezone.now() - timedelta(days=36500)
    rows.sort(key=lambda row: row["created_at"] or oldest_fallback, reverse=True)
    page = Paginator(rows, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "admin_portal/entity_directory.html",
        {
            "page": page,
            "entity_type": entity_type,
            "q": q,
            "status": status,
        },
    )


@operational_admin_required
def entity_status_update(request, entity_type, entity_id):
    if request.method != "POST":
        return redirect("admin_portal:entity_directory")
    action = (request.POST.get("action") or "").strip()
    next_url = request.POST.get("next") or reverse("admin_portal:entity_directory")
    activate = action == "reactivate"
    if action not in {"suspend", "reactivate"}:
        messages.error(request, "Invalid account action.")
        return redirect(next_url)
    try:
        summary = _set_entity_active_state(entity_type, entity_id, activate=activate, actor=request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        audit.record_write(
            request.user,
            "entity.reactivate" if activate else "entity.suspend",
            target_type=entity_type,
            target_id=entity_id,
            request=request,
            summary=summary,
            entity_type=entity_type,
            requested_action=action,
        )
        messages.success(request, summary)
    return redirect(next_url)


@super_admin_required
def operational_settings_view(request):
    config = OperationalSettings.get_solo()
    form = OperationalSettingsForm(request.POST or None, instance=config)
    if request.method == "POST" and form.is_valid():
        settings_obj = form.save(commit=False)
        settings_obj.updated_by = request.user
        settings_obj.save()
        audit.record_write(
            request.user,
            "settings.update",
            target_type="operational_settings",
            target_id=settings_obj.id,
            request=request,
            summary="Updated operational email, Slack, or mailbox settings.",
        )
        messages.success(request, "Operational settings updated.")
        return redirect("admin_portal:operational_settings")
    return render(
        request,
        "admin_portal/operational_settings.html",
        {"form": form, "settings_obj": config},
    )


@operational_admin_required
def support_inbox_list(request):
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()
    qs = SupportInquiry.objects.all()
    if q:
        qs = qs.filter(
            Q(from_email__icontains=q)
            | Q(from_name__icontains=q)
            | Q(subject__icontains=q)
            | Q(body_text__icontains=q)
        )
    if status in {"new", "triaged", "actioned", "replied", "archived", "error"}:
        qs = qs.filter(status=status)
    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "admin_portal/support_inbox_list.html",
        {"page": page, "q": q, "status": status},
    )


@operational_admin_required
def support_inbox_refresh(request):
    if request.method == "POST":
        try:
            result = fetch_support_inbox(limit=25)
        except Exception as exc:
            messages.error(request, f"Inbox refresh failed: {exc}")
        else:
            audit.record_write(
                request.user,
                "inbox.refresh",
                target_type="support_inbox",
                target_id="support",
                request=request,
                summary=f"Fetched support inbox messages: {result['added']} added, {result['updated']} updated.",
                **result,
            )
            messages.success(request, f"Inbox refreshed: {result['added']} added, {result['updated']} updated.")
    return redirect("admin_portal:support_inbox_list")


@operational_admin_required
def support_inbox_detail(request, inquiry_id):
    inquiry = get_object_or_404(SupportInquiry, pk=inquiry_id)
    reply_form = SupportReplyForm(initial={"body": inquiry.response_draft})
    return render(
        request,
        "admin_portal/support_inbox_detail.html",
        {"inquiry": inquiry, "reply_form": reply_form},
    )


@operational_admin_required
def support_inbox_analyse(request, inquiry_id):
    inquiry = get_object_or_404(SupportInquiry, pk=inquiry_id)
    if request.method == "POST":
        persist_inquiry_analysis(inquiry)
        audit.record_write(
            request.user,
            "inbox.analyse",
            target_type="support_inquiry",
            target_id=inquiry.id,
            request=request,
            summary=f"Analysed support enquiry from {inquiry.from_email}.",
        )
        if inquiry.ai_error:
            messages.warning(request, f"Enquiry analysed with fallback logic: {inquiry.ai_error}")
        else:
            messages.success(request, "Enquiry analysed and suggested actions were updated.")
    return redirect("admin_portal:support_inbox_detail", inquiry_id=inquiry.id)


@operational_admin_required
def support_inbox_apply_action(request, inquiry_id):
    inquiry = get_object_or_404(SupportInquiry, pk=inquiry_id)
    if request.method == "POST":
        action_index = int(request.POST.get("action_index", "-1"))
        actions = list(inquiry.ai_recommended_actions or [])
        if not (0 <= action_index < len(actions)):
            messages.error(request, "That enquiry action is no longer available.")
            return redirect("admin_portal:support_inbox_detail", inquiry_id=inquiry.id)
        summary = apply_inquiry_action(inquiry, actions[action_index], actor=request.user, state_handler=_set_entity_active_state)
        audit.record_write(
            request.user,
            "inbox.apply_action",
            target_type="support_inquiry",
            target_id=inquiry.id,
            request=request,
            summary=summary,
            action_index=action_index,
            recommended_action=actions[action_index],
        )
        messages.success(request, summary)
    return redirect("admin_portal:support_inbox_detail", inquiry_id=inquiry.id)


@operational_admin_required
def support_inbox_send_reply(request, inquiry_id):
    inquiry = get_object_or_404(SupportInquiry, pk=inquiry_id)
    form = SupportReplyForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        result = send_support_reply(inquiry, form.cleaned_data["body"])
        if result["ok"]:
            audit.record_write(
                request.user,
                "inbox.reply",
                target_type="support_inquiry",
                target_id=inquiry.id,
                request=request,
                summary=f"Sent reply to support enquiry from {inquiry.from_email}.",
            )
            messages.success(request, "Reply sent.")
        else:
            messages.error(request, f"Reply failed: {result['error']}")
    return redirect("admin_portal:support_inbox_detail", inquiry_id=inquiry.id)


@admin_required
def issue_list(request):
    qs = AIFlaggedIssue.objects.all()
    severity = request.GET.get("severity", "").strip()
    source = request.GET.get("source", "").strip()
    show_resolved = request.GET.get("resolved") == "1"
    q = (request.GET.get("q") or "").strip()
    if severity in {"info", "warning", "critical"}:
        qs = qs.filter(severity=severity)
    if source in {"incident", "consultant_warning", "message_risk", "breeder_inquiry_risk", "booking_risk", "payment_risk", "trust_drop", "support_inquiry"}:
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


@operational_admin_required
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


@operational_admin_required
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
    users = AdminUser.objects.filter(is_staff=True).order_by("email")
    invites = AdminInvite.objects.filter(accepted_at__isnull=True, revoked=False)
    for invite in invites:
        invite.accept_url = _invite_accept_url(request, invite)
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
    accept_url = _invite_accept_url(request, invite)
    delivery = notify_invite(invite, accept_url)
    audit.record_write(
        request.user,
        "invite.create",
        target_type="invite",
        target_id=invite.id,
        request=request,
        summary=f"Invited {email} with role {role}.",
        email=email,
        role=role,
        delivery_status=delivery.get("delivery_status", "pending"),
    )
    audit.record(
        request.user,
        "invite.email_sent" if delivery.get("email") else "invite.email_failed",
        target_type="invite",
        target_id=invite.id,
        request=request,
        email=email,
        delivery_status=delivery.get("delivery_status", "pending"),
        error=delivery.get("error", ""),
    )
    if delivery.get("email"):
        messages.success(request, f"Invite sent to {email} as {role}.")
    else:
        messages.warning(request, f"Invite created for {email}, but email delivery was not confirmed. Use the fallback link in Pending invites.")
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
        if not target.is_active:
            messages.info(request, f"{target.email} is already inactive.")
            return redirect("admin_portal:admin_user_list")
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
def admin_user_activate(request, user_id):
    target = get_object_or_404(AdminUser, pk=user_id)
    if target.is_super_admin:
        messages.error(request, "You cannot manually change the activation state of a platform super-admin here.")
        return redirect("admin_portal:admin_user_list")
    if request.method == "POST":
        if target.is_active:
            messages.info(request, f"{target.email} is already active.")
            return redirect("admin_portal:admin_user_list")
        target.is_active = True
        target.save(update_fields=["is_active"])
        audit.record_write(
            request.user,
            "admin.reactivate",
            target_type="admin_user",
            target_id=target.id,
            request=request,
            summary=f"Re-activated admin user {target.email}.",
        )
        messages.success(request, f"{target.email} re-activated.")
    return redirect("admin_portal:admin_user_list")


@super_admin_required
def admin_user_remove(request, user_id):
    target = get_object_or_404(AdminUser, pk=user_id)
    if target.is_super_admin:
        messages.error(request, "You cannot remove a platform super-admin.")
        return redirect("admin_portal:admin_user_list")
    if target.pk == request.user.pk:
        messages.error(request, "You cannot remove your own account.")
        return redirect("admin_portal:admin_user_list")
    if request.method == "POST":
        if target.is_active:
            messages.error(request, "Revoke this account before removing it from the list.")
            return redirect("admin_portal:admin_user_list")
        target_email = target.email
        target_id = target.id
        target.is_staff = False
        target.save(update_fields=["is_staff"])
        audit.record_write(
            request.user,
            "admin.remove",
            target_type="admin_user",
            target_id=target_id,
            request=request,
            summary=f"Removed admin user {target_email} from the control-plane list.",
            email=target_email,
        )
        messages.success(request, f"{target_email} removed from the admin list.")
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


@super_admin_required
def invite_resend(request, invite_id):
    invite = get_object_or_404(AdminInvite, pk=invite_id, accepted_at__isnull=True, revoked=False)
    if request.method == "POST":
        accept_url = _invite_accept_url(request, invite)
        delivery = notify_invite(invite, accept_url)
        audit.record_write(
            request.user,
            "invite.resend",
            target_type="invite",
            target_id=invite.id,
            request=request,
            summary=f"Re-sent invite for {invite.email}.",
            email=invite.email,
            delivery_status=delivery.get("delivery_status", "pending"),
        )
        audit.record(
            request.user,
            "invite.email_sent" if delivery.get("email") else "invite.email_failed",
            target_type="invite",
            target_id=invite.id,
            request=request,
            email=invite.email,
            delivery_status=delivery.get("delivery_status", "pending"),
            error=delivery.get("error", ""),
        )
        if delivery.get("email"):
            messages.success(request, f"Invite re-sent to {invite.email}.")
        else:
            messages.warning(request, f"Invite email still failed for {invite.email}. The fallback link remains available.")
    return redirect("admin_portal:admin_user_list")


@super_admin_required
def invite_link(request, invite_id):
    invite = get_object_or_404(AdminInvite, pk=invite_id, accepted_at__isnull=True, revoked=False)
    accept_url = _invite_accept_url(request, invite)
    audit.record(
        request.user,
        "invite.copied",
        target_type="invite",
        target_id=invite.id,
        request=request,
        email=invite.email,
    )
    return JsonResponse({"accept_url": accept_url})


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


@operational_admin_required
def process_now(request):
    if request.method == "POST":
        try:
            review_counts = process_pending(limit_per_type=8, max_runtime_seconds=8)
            issue_counts = process_pending_issues(limit_per_type=4, max_runtime_seconds=8)
        except Exception as exc:
            messages.error(request, f"AI scan failed: {exc}")
        else:
            review_truncated = bool(review_counts.get("truncated"))
            issue_truncated = bool(issue_counts.get("truncated"))
            truncated = review_truncated or issue_truncated
            summary = (
                f"Processed {review_counts['breeder']} breeders, {review_counts['consultant']} consultants, "
                f"and issue sources: {_issue_count_summary(issue_counts)}."
            )
            if truncated:
                summary += " The scan was time-bounded to keep the web request stable; run it again for the next batch."
            audit_details = {
                **{k: v for k, v in review_counts.items() if k != "truncated"},
                **{k: v for k, v in issue_counts.items() if k != "truncated"},
                "review_truncated": review_truncated,
                "issue_truncated": issue_truncated,
                "truncated": truncated,
            }
            audit.record_write(
                request.user,
                "ai.process_now",
                request=request,
                summary=summary,
                **audit_details,
            )
            if truncated:
                messages.warning(request, summary)
            else:
                messages.success(request, summary)
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


def _invite_accept_url(request, invite: AdminInvite) -> str:
    return request.build_absolute_uri(reverse("admin_portal:invite_accept", args=[invite.token]))


def _issue_count_summary(counts: dict[str, int]) -> str:
    labels = {
        "incident": "incidents",
        "consultant_warning": "consultant warnings",
        "message_risk": "message risks",
        "breeder_inquiry_risk": "breeder inquiry risks",
        "booking_risk": "booking risks",
        "payment_risk": "payment risks",
        "trust_drop": "trust drops",
    }
    parts = [f"{counts.get(key, 0)} {label}" for key, label in labels.items()]
    return ", ".join(parts)


def _set_entity_active_state(entity_type: str, entity_id: str, *, activate: bool, actor) -> str:
    note = f"[Control plane:{timezone.now():%Y-%m-%d %H:%M UTC}] {'Reactivated' if activate else 'Suspended'} by {actor.email}."
    if entity_type == "breeder":
        profile = get_object_or_404(ExternalBreederProfile, pk=entity_id)
        user = get_object_or_404(ExternalUser, pk=profile.user_id)
        profile.is_active = activate
        metadata = dict(profile.metadata or {})
        metadata["account_status"] = "active" if activate else "suspended"
        metadata["status_note"] = note
        profile.metadata = metadata
        profile.save(update_fields=["is_active", "metadata"])
        user.is_active = activate
        user.save(update_fields=["is_active"])
        return f"{profile.company_name or user.email} was {'re-activated' if activate else 'suspended'}."
    if entity_type == "consultant":
        profile = get_object_or_404(ExternalConsultantProfile, pk=entity_id)
        user = get_object_or_404(ExternalUser, pk=profile.user_id)
        profile.is_active = activate
        profile.admin_status = "approved" if activate else "suspended"
        profile.admin_notes = ((profile.admin_notes or "").strip() + "\n" + note).strip()
        metadata = dict(profile.metadata or {})
        metadata["account_status"] = "active" if activate else "suspended"
        profile.metadata = metadata
        profile.save(update_fields=["is_active", "admin_status", "admin_notes", "metadata"])
        user.is_active = activate
        user.save(update_fields=["is_active"])
        return f"{profile.company_name or user.email} was {'re-activated' if activate else 'suspended'}."
    if entity_type == "user":
        user = get_object_or_404(ExternalUser, pk=entity_id)
        user.is_active = activate
        user.save(update_fields=["is_active"])
        return f"{user.email} was {'re-activated' if activate else 'suspended'}."
    raise ValueError("Unknown entity type.")
