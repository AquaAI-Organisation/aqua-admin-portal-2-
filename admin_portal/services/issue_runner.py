"""Discovery and triage of external incidents and consultant warnings."""
from __future__ import annotations

import logging

from django.utils import timezone

from ..models import (
    AIFlaggedIssue,
    ExternalBreederProfile,
    ExternalConsultantProfile,
    ExternalConsultantWarning,
    ExternalIncidentLog,
    ExternalUser,
)
from .issue_review import (
    build_incident_dossier,
    build_warning_dossier,
    call_issue_gpt,
)
from .notifier import notify_issue
from .review_runner import _deactivate

logger = logging.getLogger(__name__)

WARNING_OPEN_STATUSES = {"open", "pending", "active", "unresolved", "new", ""}


def _already_triaged_ids(source_type: str) -> set[str]:
    return set(
        AIFlaggedIssue.objects
        .filter(source_type=source_type)
        .exclude(status="error")
        .values_list("source_id", flat=True)
    )


def _load_profiles_for_user(user):
    consultant = None
    breeder = None
    try:
        consultant = ExternalConsultantProfile.objects.get(user_id=user.id)
    except ExternalConsultantProfile.DoesNotExist:
        consultant = None
    try:
        breeder = ExternalBreederProfile.objects.get(user_id=user.id)
    except ExternalBreederProfile.DoesNotExist:
        breeder = None
    return consultant, breeder


def discover_pending_incidents(limit: int = 50):
    seen = _already_triaged_ids("incident")
    qs = ExternalIncidentLog.objects.filter(is_cleared=False).order_by("-occurred_at", "-created_at")
    for incident in qs[: limit * 4]:
        source_id = str(incident.id)
        if source_id in seen:
            continue
        try:
            user = ExternalUser.objects.get(pk=incident.user_id)
        except ExternalUser.DoesNotExist:
            continue
        consultant, breeder = _load_profiles_for_user(user)
        if consultant:
            yield incident, user, "consultant", consultant
        elif breeder:
            yield incident, user, "breeder", breeder
        else:
            yield incident, user, user.role or "", None


def discover_pending_consultant_warnings(limit: int = 50):
    seen = _already_triaged_ids("consultant_warning")
    qs = ExternalConsultantWarning.objects.order_by("-created_at")
    for warning in qs[: limit * 4]:
        source_id = str(warning.id)
        if source_id in seen:
            continue
        if warning.resolved_at:
            continue
        status = (warning.status or "").strip().lower()
        if status not in WARNING_OPEN_STATUSES:
            continue
        consultant = warning.consultant
        try:
            user = ExternalUser.objects.get(pk=consultant.user_id)
        except ExternalUser.DoesNotExist:
            continue
        yield warning, user, consultant


def run_incident_triage(incident, user, subject_type: str, profile=None) -> AIFlaggedIssue:
    dossier = build_incident_dossier(incident, user, profile)
    outcome = call_issue_gpt(dossier)
    issue = _save_issue(
        source_type="incident",
        source_id=str(incident.id),
        subject_type=subject_type,
        user=user,
        title=f"Incident {incident.incident_code}",
        source_payload=dossier,
        outcome=outcome,
    )
    _deliver_and_apply(issue, profile=profile, user=user, warning=None)
    return issue


def run_warning_triage(warning, user, consultant_profile) -> AIFlaggedIssue:
    dossier = build_warning_dossier(warning, user, consultant_profile)
    outcome = call_issue_gpt(dossier)
    title = warning.title or "Consultant warning"
    issue = _save_issue(
        source_type="consultant_warning",
        source_id=str(warning.id),
        subject_type="consultant",
        user=user,
        title=title,
        source_payload=dossier,
        outcome=outcome,
    )
    _deliver_and_apply(issue, profile=consultant_profile, user=user, warning=warning)
    return issue


def _save_issue(*, source_type: str, source_id: str, subject_type: str, user, title: str, source_payload: dict, outcome) -> AIFlaggedIssue:
    display_name = user.name or f"{user.first_name} {user.last_name}".strip() or user.email
    issue, _ = AIFlaggedIssue.objects.update_or_create(
        source_type=source_type,
        source_id=source_id,
        defaults=dict(
            subject_type=subject_type if subject_type in {"breeder", "consultant"} else "",
            subject_user_id=user.id,
            subject_user_email=user.email,
            subject_display_name=display_name[:255],
            title=title[:255],
            severity=outcome.severity,
            status="error" if outcome.error else "open",
            summary=outcome.summary,
            rationale=outcome.rationale,
            evidence=outcome.evidence,
            recommended_actions=outcome.recommended_actions,
            applied_actions=[],
            source_payload=source_payload,
            openai_raw=outcome.raw,
            ai_model=outcome.model,
            error=outcome.error,
            triaged_at=timezone.now(),
            resolved=False,
            resolved_by=None,
            resolved_at=None,
            resolution_notes="",
        ),
    )
    return issue


def _deliver_and_apply(issue: AIFlaggedIssue, *, profile=None, user=None, warning=None):
    delivery = notify_issue(issue)
    issue.notified_emails = delivery.get("recipients", [])
    issue.notified_slack = delivery.get("slack", False)
    issue.applied_actions = _apply_issue_actions(issue, profile=profile, user=user, warning=warning)
    issue.save(update_fields=["notified_emails", "notified_slack", "applied_actions"])


def _apply_issue_actions(issue: AIFlaggedIssue, *, profile=None, user=None, warning=None) -> list[dict]:
    applied: list[dict] = []
    for action in issue.recommended_actions or []:
        name = str((action or {}).get("action", "")).strip()
        if not name:
            continue
        try:
            if name == "notify_super_admins":
                applied.append({"action": name, "status": "sent"})
            elif name == "request_manual_review":
                applied.append({"action": name, "status": "queued"})
            elif name == "deactivate_account_pending_review" and profile and user and issue.subject_type in {"breeder", "consultant"}:
                _deactivate(issue.subject_type, profile, user, reason=str(action.get("reason", "AI issue triage")))
                applied.append({"action": name, "status": "applied"})
            elif name == "set_warning_status" and warning:
                value = str(action.get("value", "")).strip()
                if value:
                    warning.status = value
                    update_fields = ["status"]
                    if value.lower() in {"resolved", "closed"}:
                        warning.resolved_at = timezone.now()
                        update_fields.append("resolved_at")
                    warning.save(update_fields=update_fields)
                    applied.append({"action": name, "value": value, "status": "applied"})
        except Exception:
            logger.exception("Failed applying issue action %s", action)
    return applied


def process_pending_issues(limit_per_type: int = 25) -> dict[str, int]:
    counts = {"incident": 0, "consultant_warning": 0}
    for incident, user, subject_type, profile in discover_pending_incidents(limit=limit_per_type):
        run_incident_triage(incident, user, subject_type, profile)
        counts["incident"] += 1
    for warning, user, consultant in discover_pending_consultant_warnings(limit=limit_per_type):
        run_warning_triage(warning, user, consultant)
        counts["consultant_warning"] += 1
    return counts
