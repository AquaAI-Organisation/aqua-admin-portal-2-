"""Daily analytics rollup."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from django.db.models import Count, Q
from django.utils import timezone

from ..models import AIAccountReview, DailyReport
from .notifier import notify_daily_report


def _day_window(d: date):
    start = timezone.make_aware(datetime.combine(d, time.min))
    end = timezone.make_aware(datetime.combine(d, time.max))
    return start, end


def build_report_for(d: date | None = None) -> DailyReport:
    d = d or timezone.now().date()
    start, end = _day_window(d)

    qs = AIAccountReview.objects.filter(created_at__range=(start, end))
    by_decision = qs.aggregate(
        approved=Count("id", filter=Q(decision="approved")),
        rejected=Count("id", filter=Q(decision="rejected")),
        flagged=Count("id", filter=Q(decision="flagged")),
        pending=Count("id", filter=Q(decision="pending")),
    )
    by_subject = qs.aggregate(
        breeder=Count("id", filter=Q(subject_type="breeder")),
        consultant=Count("id", filter=Q(subject_type="consultant")),
    )
    override_count = qs.filter(manually_overridden=True).count()

    summary_lines = []
    if by_decision["approved"]:
        summary_lines.append(f"{by_decision['approved']} accounts auto-approved.")
    if by_decision["rejected"]:
        summary_lines.append(f"{by_decision['rejected']} accounts auto-rejected.")
    if by_decision["flagged"]:
        summary_lines.append(f"{by_decision['flagged']} flagged for human review.")
    if by_decision["pending"]:
        summary_lines.append(f"{by_decision['pending']} still pending.")
    if override_count:
        summary_lines.append(f"{override_count} manually overridden by admins.")
    summary = " ".join(summary_lines) or "No new reviews today."

    details = {
        "decisions": list(qs.values("decision").annotate(n=Count("id"))),
        "subjects": list(qs.values("subject_type").annotate(n=Count("id"))),
        "review_ids": list(map(str, qs.values_list("id", flat=True))),
        "overrides": list(
            qs.filter(manually_overridden=True).values(
                "id", "subject_display_name", "original_decision", "decision",
                "override_reason"
            )
        ),
    }

    report, _ = DailyReport.objects.update_or_create(
        report_date=d,
        defaults=dict(
            approved_count=by_decision["approved"] or 0,
            rejected_count=by_decision["rejected"] or 0,
            flagged_count=by_decision["flagged"] or 0,
            pending_count=by_decision["pending"] or 0,
            breeder_count=by_subject["breeder"] or 0,
            consultant_count=by_subject["consultant"] or 0,
            manual_override_count=override_count,
            summary=summary,
            details=details,
        ),
    )

    delivery = notify_daily_report(report)
    report.delivered_email = delivery.get("email", False)
    report.delivered_slack = delivery.get("slack", False)
    report.save(update_fields=["delivered_email", "delivered_slack"])
    return report
