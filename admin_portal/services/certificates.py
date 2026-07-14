"""Provider certificate lifecycle reminders (breeders).

Emails providers (via the providers alias) to keep verification certificates current:

- Certificates WITH an expiry date -> remind 30 and 7 days before, and once when expired.
- Certificates WITHOUT an expiry date (most of them) but WITH an issue/"received"
  (awarded) date -> treat as a 2-year renewal cycle from the issue date: a first
  reminder once it is over 1 year old, and a renewal-due reminder at 2 years.
- Certificates with neither date -> an annual re-verification nudge.

De-duplicated via the breeder profile metadata so nobody is spammed. One-off stages
send once; recurring stages (expired / overdue / annual re-verify) send at most once
per calendar year until the provider uploads a renewed certificate (a new certificate
row starts a fresh set of stages).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from ..models import (
    ExternalBreederProfile,
    ExternalBreederVerification,
    ExternalUser,
)
from .google_oauth import pick_alias_for_mailbox
from .notifier import send_custom_email

logger = logging.getLogger(__name__)

FIRST_AGE_REMINDER_DAYS = 365          # over a year old -> first renewal reminder
RENEWAL_DUE_DAYS = 2 * 365             # 2 years since issue -> renewal due
EXPIRY_REMINDER_DAYS = (30, 7)         # days before an expiry date to remind


def run_certificate_checks(limit: int = 500) -> dict:
    """Scan breeder certificates and send any due renewal / re-verification reminders."""
    counts = {"sent": 0, "scanned": 0}
    today = timezone.now().date()

    handled_sellers: set = set()
    verifications = (
        ExternalBreederVerification.objects
        .exclude(status__iexact="rejected")
        .order_by("seller_id", "-created_at")
    )
    for verification in verifications[:limit]:
        # Only the most recent (non-rejected) certificate per seller.
        if verification.seller_id in handled_sellers:
            continue
        handled_sellers.add(verification.seller_id)
        counts["scanned"] += 1
        try:
            if _remind_breeder_certificate(verification, today):
                counts["sent"] += 1
        except Exception:
            logger.exception("Certificate reminder failed for verification %s", verification.id)
    return counts


def _due_stage(verification, today):
    """Return (stage_key, human_detail) for the reminder due now, or None.

    Recurring stages carry the year in their key so they re-send at most once a year.
    """
    if verification.expiry_date:
        days_left = (verification.expiry_date - today).days
        when = verification.expiry_date.strftime("%d %b %Y")
        if days_left < 0:
            return (f"expired_{today.year}", f"expired on {when}")
        if days_left <= EXPIRY_REMINDER_DAYS[1]:
            return ("expiry_7d", f"expires on {when} (in {days_left} days)")
        if days_left <= EXPIRY_REMINDER_DAYS[0]:
            return ("expiry_30d", f"expires on {when} (in {days_left} days)")
        return None

    issue = verification.awarded_date or (
        verification.created_at.date() if verification.created_at else None
    )
    if issue:
        age = (today - issue).days
        issued_str = issue.strftime("%d %b %Y")
        if age >= RENEWAL_DUE_DAYS:
            return (f"age_2yr_{today.year}", f"was issued on {issued_str}, over 2 years ago, and is now due for renewal")
        if age >= FIRST_AGE_REMINDER_DAYS:
            due = (issue + timedelta(days=RENEWAL_DUE_DAYS)).strftime("%d %b %Y")
            return ("age_1yr", f"was issued on {issued_str}; please plan to renew it by {due} (2 years after issue)")
        return None

    # No expiry and no issue date on record -> annual re-verification.
    return (f"reverify_{today.year}", "should be re-confirmed to keep your account verified")


def _remind_breeder_certificate(verification, today) -> bool:
    profile = ExternalBreederProfile.objects.filter(user_id=verification.seller_id).first()
    user = ExternalUser.objects.filter(id=verification.seller_id).first()
    if not profile or not user or not (getattr(user, "email", "") or "").strip():
        return False

    stage = _due_stage(verification, today)
    if not stage:
        return False
    stage_key, detail = stage

    metadata = dict(profile.metadata or {})
    reminders = dict(metadata.get("cert_reminders") or {})
    cert_key = str(verification.id)
    sent_stages = list(reminders.get(cert_key) or [])
    if stage_key in sent_stages:
        return False  # already reminded for this stage of this certificate

    result = send_custom_email(
        subject="Your Aqua AI certificate may need renewing",
        body=_renewal_body(user, detail),
        recipients=[user.email.strip()],
        from_email=pick_alias_for_mailbox("providers"),
    )

    sent_stages.append(stage_key)
    reminders[cert_key] = sent_stages
    metadata["cert_reminders"] = reminders
    profile.metadata = metadata
    profile.save(update_fields=["metadata"])
    logger.info(
        "Certificate reminder '%s' sent to %s (ok=%s)",
        stage_key, user.email, bool(result.get("ok")),
    )
    return True


def _renewal_body(user, detail: str) -> str:
    name = (getattr(user, "name", "") or getattr(user, "first_name", "") or "there").strip()
    return (
        f"Hi {name},\n\n"
        f"This is a friendly reminder from Aqua AI: your verification certificate {detail}.\n\n"
        "To keep your account fully verified and continue exploring all provider features on "
        "Aqua AI, please upload a current or renewed certificate in your account settings.\n\n"
        "If you have already renewed it, please ignore this message.\n\n"
        "Thank you,\nThe Aqua AI Team"
    )
