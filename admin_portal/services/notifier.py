"""Email and Slack notifications to the platform super admins."""
from __future__ import annotations

import logging
from typing import Iterable

from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.utils import timezone

from .google_oauth import gmail_configured, send_gmail_message
from .runtime_config import (
    get_email_runtime_config,
    get_gmail_runtime_config,
)
from . import slack as slack_service

logger = logging.getLogger(__name__)


def _admin_emails() -> list[str]:
    return list(getattr(settings, "SUPERADMIN_EMAILS", []))


def _slack_token_ok() -> bool:
    return slack_service.slack_configured()


def _email_ok() -> bool:
    return email_config_status()["configured"]


def email_config_status() -> dict[str, str | bool]:
    gmail = get_gmail_runtime_config()
    if gmail.configured:
        return {
            "configured": True,
            "detail": (
                "Gmail OAuth configured for "
                f"{gmail.sender} with aliases {gmail.support_alias}, {gmail.privacy_alias}, "
                f"and {gmail.providers_alias}."
            ),
        }
    config = get_email_runtime_config()
    host = config.host
    user = config.username
    password = config.password
    missing = []
    if not host:
        missing.append("EMAIL_HOST")
    if not user:
        missing.append("EMAIL_HOST_USER")
    if not password or "REPLACE" in password.upper():
        missing.append("EMAIL_HOST_PASSWORD")
    if missing:
        return {
            "configured": False,
            "detail": f"SMTP settings are incomplete: missing {', '.join(missing)}.",
        }
    return {
        "configured": True,
        "detail": f"SMTP configured via {host} as {user}.",
    }


def _portal_url(path: str) -> str:
    base = (getattr(settings, "LEGACY_ADMIN_REDIRECT_URL", "") or "").rstrip("/")
    if not base:
        return path
    return f"{base}{path}"


def notify_flag(review, flag) -> dict:
    subject = f"[Aqua Admin] {flag.severity.upper()} flag on {review.subject_type} {review.subject_display_name or review.subject_user_email}"
    body = (
        f"AI raised a {flag.severity} flag on a {review.subject_type} signup.\n\n"
        f"Subject: {review.subject_display_name or review.subject_user_email}\n"
        f"Decision: {review.decision} (confidence {review.confidence:.2f})\n"
        f"Reason: {flag.reason}\n\n"
        f"Recommended solution: {flag.recommended_solution}\n"
        f"Applied solution: {flag.applied_solution or '(none yet)'}\n\n"
        f"Open review: {_portal_url(f'/admin-portal/reviews/{review.id}/')}\n"
    )
    delivered_email = _send_email(subject, body, _admin_emails())
    delivered_slack = _send_slack_to_super_admins(
        f"*{flag.severity.upper()}* signup flag on `{review.subject_type}` "
        f"_{review.subject_display_name or review.subject_user_email}_\n"
        f"> {flag.reason}\n"
        f"_Recommended:_ {flag.recommended_solution}"
    )
    return {"email": delivered_email, "slack": delivered_slack, "recipients": _admin_emails()}


def notify_issue(issue) -> dict:
    subject = f"[Aqua Admin] {issue.severity.upper()} issue on {issue.subject_display_name or issue.subject_user_email}"
    body = (
        f"AI triaged a {issue.source_label.lower()} affecting a platform account.\n\n"
        f"Source: {issue.source_label}\n"
        f"Subject: {issue.subject_display_name or issue.subject_user_email}\n"
        f"Severity: {issue.severity}\n"
        f"Summary: {issue.summary}\n\n"
        f"Rationale: {issue.rationale}\n\n"
        f"Recommended actions: {issue.recommended_actions}\n"
        f"Applied actions: {issue.applied_actions}\n\n"
        f"Open issue: {_portal_url(f'/admin-portal/issues/{issue.id}/')}\n"
    )
    delivered_email = _send_email(subject, body, _admin_emails())
    delivered_slack = _send_slack_to_super_admins(
        f"*{issue.severity.upper()}* {issue.source_label.lower()} on "
        f"_{issue.subject_display_name or issue.subject_user_email}_\n"
        f"> {issue.summary}\n"
        f"_Recommended:_ {issue.recommended_actions}"
    )
    return {"email": delivered_email, "slack": delivered_slack, "recipients": _admin_emails()}


def notify_daily_report(report) -> dict:
    subject = f"[Aqua Admin] Daily AI review report - {report.report_date}"
    body = (
        f"AI auto-review summary for {report.report_date}\n\n"
        f"Approved : {report.approved_count}\n"
        f"Rejected : {report.rejected_count}\n"
        f"Flagged  : {report.flagged_count}\n"
        f"Pending  : {report.pending_count}\n"
        f"Issues triaged: {report.issue_count}\n"
        f"Critical issues: {report.critical_issue_count}\n"
        f"Manual overrides: {report.manual_override_count}\n\n"
        f"Breeders reviewed: {report.breeder_count}\n"
        f"Consultants reviewed: {report.consultant_count}\n\n"
        f"{report.summary}\n\n"
        f"Open full report: {_portal_url(f'/admin-portal/reports/{report.id}/')}\n"
    )
    delivered_email = _send_email(subject, body, _admin_emails())
    delivered_slack = _send_slack_to_super_admins(
        f":bar_chart: Daily AI review for *{report.report_date}*: "
        f"{report.approved_count} approved, {report.rejected_count} rejected, "
        f"{report.flagged_count} flagged, {report.pending_count} pending, "
        f"{report.issue_count} issues triaged."
    )
    return {"email": delivered_email, "slack": delivered_slack}


def notify_invite(invite, accept_url: str) -> dict:
    subject = "[Aqua Admin] You have been invited to the control plane"
    body = (
        f"{invite.created_by.email} has invited you to join the Aqua AI Admin control plane.\n\n"
        f"Role: {invite.role}\n"
        f"Accept the invite (expires {invite.expires_at:%Y-%m-%d %H:%M UTC}):\n  {accept_url}\n\n"
        f"If you were not expecting this, ignore this email.\n"
    )
    email_result = _send_email_result(subject, body, [invite.email])
    delivery_status = "email_sent" if email_result["ok"] else "link_available"
    if email_result["error"]:
        delivery_status = "email_failed"
    invite.delivery_status = delivery_status
    invite.delivery_error = email_result["error"]
    invite.last_delivery_attempt_at = timezone.now()
    invite.save(update_fields=["delivery_status", "delivery_error", "last_delivery_attempt_at"])
    return {
        "email": email_result["ok"],
        "delivery_status": delivery_status,
        "error": email_result["error"],
        "accept_url": accept_url,
    }


def notify_manual_override(review, admin_user, new_decision, reason) -> dict:
    subject = f"[Aqua Admin] Manual override on {review.subject_type} {review.subject_display_name or review.subject_user_email}"
    body = (
        f"A manual override was applied to a {review.subject_type} review.\n\n"
        f"Subject: {review.subject_display_name or review.subject_user_email}\n"
        f"Original decision: {review.original_decision}\n"
        f"New decision: {new_decision}\n"
        f"Overridden by: {admin_user.email}\n"
        f"Reason: {reason}\n\n"
        f"Open review: {_portal_url(f'/admin-portal/reviews/{review.id}/')}\n"
    )
    delivered_email = _send_email(subject, body, _admin_emails())
    delivered_slack = _send_slack_to_super_admins(
        f":warning: Manual override on `{review.subject_type}` "
        f"_{review.subject_display_name or review.subject_user_email}_\n"
        f"> {review.original_decision} -> {new_decision} by {admin_user.email}\n"
        f"_Reason:_ {reason}"
    )
    return {"email": delivered_email, "slack": delivered_slack}


def notify_developer_action(developer_user, action: str, details: str) -> dict:
    subject = f"[Aqua Admin] Developer action by {developer_user.email}: {action}"
    body = (
        f"A developer-role admin has performed a write action.\n\n"
        f"Developer: {developer_user.email} ({developer_user.full_name})\n"
        f"Action: {action}\n"
        f"Details: {details}\n\n"
        f"Review this in the audit log: {_portal_url('/admin-portal/audit/')}\n"
    )
    delivered_email = _send_email(subject, body, _admin_emails())
    delivered_slack = _send_slack_to_super_admins(
        f":pencil2: Developer `{developer_user.email}` performed: *{action}*\n"
        f"> {details}"
    )
    return {"email": delivered_email, "slack": delivered_slack}


def notify_password_change(user) -> dict:
    subject = f"[Aqua Admin] Password changed: {user.email}"
    body = f"{user.email} has changed their control-plane password.\n"
    delivered_email = _send_email(subject, body, _admin_emails())
    return {"email": delivered_email}


def _send_email(subject: str, body: str, recipients: Iterable[str]) -> bool:
    return _send_email_result(subject, body, recipients)["ok"]


def _send_email_result(
    subject: str,
    body: str,
    recipients: Iterable[str],
    *,
    from_email: str | None = None,
    attachments: list[dict] | None = None,
) -> dict[str, str | bool]:
    recipients = [r for r in recipients if r]
    if not recipients:
        return {"ok": False, "error": "No recipients were provided."}
    if not _email_ok():
        logger.warning("SMTP not configured; skipping email %r -> %s", subject, recipients)
        return {"ok": False, "error": str(email_config_status()["detail"])}
    try:
        if gmail_configured():
            send_gmail_message(
                subject=subject,
                body=body,
                recipients=recipients,
                from_email=from_email,
                attachments=attachments,
            )
            return {"ok": True, "error": ""}
        config = get_email_runtime_config()
        connection = get_connection(
            host=config.host,
            port=config.port,
            username=config.username,
            password=config.password,
            use_tls=config.use_tls,
            fail_silently=False,
        )
        email = EmailMessage(
            subject,
            body,
            from_email or config.default_from_email,
            recipients,
            connection=connection,
        )
        for item in attachments or []:
            email.attach_file(item["path"], mimetype=item.get("mime_type"))
        email.send(fail_silently=False)
        return {"ok": True, "error": ""}
    except Exception as exc:
        logger.exception("Email send failed")
        return {"ok": False, "error": _friendly_email_error(str(exc))}


def _send_slack_to_super_admins(text: str) -> bool:
    # Delegates to the typed Slack service (DM each super-admin + fallback channel).
    return slack_service.send_to_super_admins(text)


def send_custom_email(
    *,
    subject: str,
    body: str,
    recipients: Iterable[str],
    from_email: str | None = None,
    attachments: list[dict] | None = None,
) -> dict[str, str | bool]:
    return _send_email_result(
        subject,
        body,
        recipients,
        from_email=from_email,
        attachments=attachments,
    )


def _friendly_email_error(error: str) -> str:
    lowered = (error or "").lower()
    if "smtpauthentication is disabled for the tenant" in lowered or "smtp auth disabled" in lowered:
        return (
            "Microsoft 365 blocked SMTP login because authenticated SMTP is disabled for this mailbox or tenant. "
            "Enable SMTP AUTH for support@aquaai.uk in Microsoft 365 / Exchange Online, then resend the invite."
        )
    if "invalid_grant" in lowered or "token has been expired or revoked" in lowered:
        return (
            "Google rejected the saved Gmail refresh token. Reconnect the Google Workspace OAuth credentials "
            "and save a fresh refresh token in Settings."
        )
    if "unauthorized_client" in lowered or "invalid_client" in lowered:
        return (
            "Google rejected the Gmail OAuth client credentials. Re-check the client ID, client secret, "
            "and that the OAuth app has Gmail API access enabled."
        )
    if "authentication unsuccessful" in lowered:
        return (
            "The mail server rejected the sign-in attempt. Re-check the mailbox credentials, and if this is Microsoft 365 "
            "also confirm that SMTP AUTH is allowed and that MFA, device approval, or an app password is not blocking the login."
        )
    if "connection" in lowered or "timed out" in lowered:
        return "The SMTP server could not be reached. Re-check the host, port, encryption mode, and firewall/network rules."
    return error
