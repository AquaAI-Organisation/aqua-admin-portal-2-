"""Daily reconciliation of the Admin <-> CRM integration (spec §12, §25).

The protocol is at-least-once on the wire and exactly-once in effect, which is
only true while something checks. Every guarantee in the contract has a way of
failing silently:

  * an outbox row can sit pending forever if the dispatcher stops running;
  * a dead-lettered event means a decision the CRM never heard about;
  * a case can wait for evidence that is never coming;
  * a dossier can arrive for a profile this portal does not know;
  * a held case can sit in a queue nobody is working.

None of those raise an exception at the time. This job looks for each one and
reports it, so the failure is noticed the next day rather than the next audit.
It only reads and reports — it never repairs, because an automatic repair of a
delivery or a decision is exactly the kind of silent action the contract is
written to prevent.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.db.models import Count
from django.utils import timezone

from ..models import AIAccountReview, V2InboxEvent, V2OutboxEvent
from .evidence_sla import (
    APPLICANT_PROMISE, AWAITING_STATES, DOSSIER_DEADLINE, DOSSIER_TARGET,
    queue_counts,
)

logger = logging.getLogger(__name__)

#: An outbox row still pending this long after its first attempt means the
#: dispatcher is not running, independently of any individual retry schedule.
STUCK_AFTER = timedelta(hours=2)


@dataclass
class Finding:
    code: str
    severity: str            # info | warning | critical
    count: int
    detail: str
    examples: list = field(default_factory=list)


@dataclass
class Report:
    generated_at: object
    findings: list = field(default_factory=list)
    totals: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(f.severity in ("warning", "critical") for f in self.findings)

    @property
    def worst(self) -> str:
        order = {"info": 0, "warning": 1, "critical": 2}
        return max((f.severity for f in self.findings), key=lambda s: order[s], default="info")


def _examples(queryset, label_attr: str, limit: int = 5) -> list:
    out = []
    for row in queryset[:limit]:
        value = getattr(row, label_attr, None)
        out.append(str(value) if value is not None else str(row.pk))
    return out


def build_report(*, now=None) -> Report:
    """Compare what the ledgers say against what the contract requires."""
    now = now or timezone.now()
    report = Report(generated_at=now)

    # --- delivery ---------------------------------------------------------
    dead = V2OutboxEvent.objects.filter(status="dead_letter")
    dead_count = dead.count()
    if dead_count:
        report.findings.append(Finding(
            "outbox_dead_letter", "critical", dead_count,
            "Events the CRM never received and that will not be retried. Each one "
            "is a decision or submission the CRM does not know about.",
            _examples(dead, "event_id")))

    stuck = V2OutboxEvent.objects.filter(
        status="pending", first_attempt_at__isnull=False,
        first_attempt_at__lte=now - STUCK_AFTER)
    stuck_count = stuck.count()
    if stuck_count:
        report.findings.append(Finding(
            "outbox_stuck", "warning", stuck_count,
            f"Pending for more than {int(STUCK_AFTER.total_seconds() // 3600)}h after a "
            "first attempt. Usually means the dispatcher is not running on its schedule.",
            _examples(stuck, "event_id")))

    never = V2OutboxEvent.objects.filter(status="pending", first_attempt_at__isnull=True,
                                         created_at__lte=now - STUCK_AFTER)
    never_count = never.count()
    if never_count:
        report.findings.append(Finding(
            "outbox_never_attempted", "critical", never_count,
            "Queued but never attempted once. The dispatcher has not run since "
            "these were created.",
            _examples(never, "event_id")))

    # --- receipt ----------------------------------------------------------
    failed_inbox = V2InboxEvent.objects.filter(processing_state="failed")
    failed_count = failed_inbox.count()
    if failed_count:
        report.findings.append(Finding(
            "inbox_failed", "warning", failed_count,
            "Dossiers the CRM delivered that this portal could not apply — most "
            "often for a profile it does not know, which means the two sides "
            "disagree about which applications exist.",
            _examples(failed_inbox, "correlation_id")))

    duplicate_ids = (V2InboxEvent.objects
                     .values("correlation_id")
                     .annotate(n=Count("id"))
                     .filter(n__gt=25))
    noisy = list(duplicate_ids[:5])
    if noisy:
        report.findings.append(Finding(
            "inbox_high_volume", "info", len(noisy),
            "Unusually many events for one application; worth a look for a "
            "redelivery loop.",
            [row["correlation_id"] for row in noisy]))

    # --- evidence timing --------------------------------------------------
    overdue = AIAccountReview.objects.filter(
        review_state__in=AWAITING_STATES, submitted_event_at__isnull=False,
        submitted_event_at__lte=now - DOSSIER_DEADLINE, dossier_id="")
    overdue_count = overdue.count()
    if overdue_count:
        report.findings.append(Finding(
            "evidence_overdue_unheld", "critical", overdue_count,
            "Past the 12-hour evidence deadline but no safety hold was recorded. "
            "The sweep is not running.",
            _examples(overdue, "subject_display_name")))

    promise_at_risk = AIAccountReview.objects.filter(
        decision="held", review_state="human_review",
        submitted_event_at__isnull=False,
        submitted_event_at__lte=now - APPLICANT_PROMISE)
    at_risk_count = promise_at_risk.count()
    if at_risk_count:
        report.findings.append(Finding(
            "applicant_promise_breached", "critical", at_risk_count,
            "Held cases that have passed the 24-hour applicant promise without a "
            "human decision.",
            _examples(promise_at_risk, "subject_display_name")))

    # --- decisions the CRM may not know about -----------------------------
    decided = AIAccountReview.objects.exclude(decision_id="").exclude(decision_id=None)
    undelivered = []
    for review in decided.order_by("-decided_at")[:500]:
        delivered = V2OutboxEvent.objects.filter(
            correlation_id=str(review.subject_id),
            event_type="admin.provider.application.decision.recorded",
            status="delivered").exists()
        queued = V2OutboxEvent.objects.filter(
            correlation_id=str(review.subject_id),
            event_type="admin.provider.application.decision.recorded").exists()
        if not queued and not delivered:
            undelivered.append(review)
    if undelivered:
        report.findings.append(Finding(
            "decision_not_queued", "warning", len(undelivered),
            "Decisions recorded with no corresponding decision event. The CRM's "
            "view of these providers is stale.",
            [r.subject_display_name or str(r.subject_id) for r in undelivered[:5]]))

    report.totals = {
        "reviews": AIAccountReview.objects.count(),
        "awaiting_evidence": AIAccountReview.objects.filter(
            review_state__in=AWAITING_STATES, dossier_id="").count(),
        "past_target": AIAccountReview.objects.filter(
            review_state__in=AWAITING_STATES, dossier_id="",
            submitted_event_at__lte=now - DOSSIER_TARGET).count(),
        "with_dossier": AIAccountReview.objects.exclude(dossier_id="").count(),
        "outbox_pending": V2OutboxEvent.objects.filter(status="pending").count(),
        "outbox_delivered": V2OutboxEvent.objects.filter(status="delivered").count(),
        "outbox_dead_letter": dead_count,
        "inbox_recorded": V2InboxEvent.objects.count(),
        "inbox_failed": failed_count,
        "queues": queue_counts(),
    }
    return report


def render(report: Report) -> str:
    """Plain-text report for an operator."""
    lines = [
        f"Aqua AI — CRM integration reconciliation, {report.generated_at:%d %b %Y %H:%M} UTC",
        "=" * 72,
    ]
    for key, value in report.totals.items():
        lines.append(f"  {key:<22} {value}")
    lines.append("")
    if not report.findings:
        lines.append("No divergence found.")
        return "\n".join(lines)
    for finding in sorted(report.findings,
                          key=lambda f: {"critical": 0, "warning": 1, "info": 2}[f.severity]):
        lines.append(f"[{finding.severity.upper()}] {finding.code} — {finding.count}")
        lines.append(f"    {finding.detail}")
        if finding.examples:
            lines.append(f"    e.g. {', '.join(finding.examples)}")
        lines.append("")
    return "\n".join(lines)


def notify(report: Report) -> bool:
    """Alert only when something diverged. A clean day stays quiet."""
    if report.ok:
        return False
    text = render(report)
    sent = False
    try:
        from ..services import slack as slack_service
        headline = ", ".join(f"{f.code}×{f.count}" for f in report.findings
                             if f.severity in ("warning", "critical"))
        sent = slack_service.send_to_super_admins(
            f":triangular_flag_on_post: *CRM integration reconciliation*\n{headline}")
    except Exception:
        logger.exception("slack reconciliation alert failed")
    try:
        from django.conf import settings
        from ..services.notifier import send_custom_email
        recipients = list(getattr(settings, "ADMIN_EMAILS", []) or [])
        if recipients:
            send_custom_email(
                subject=f"[AquaAI] CRM integration reconciliation — {report.worst}",
                body=text, recipients=recipients)
            sent = True
    except Exception:
        logger.exception("email reconciliation alert failed")
    return sent
