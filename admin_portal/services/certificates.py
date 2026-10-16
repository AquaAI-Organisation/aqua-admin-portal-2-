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
from datetime import date, timedelta

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

#: A breeder who has never uploaded anything is given this long to finish
#: setting up before being chased, so a signup is not nagged on day one.
NO_CERTIFICATE_GRACE_DAYS = 7
#: ...and is then reminded at most once a month, since there is no expiry date
#: to pace it and the reminder has no natural end until they upload.
NO_CERTIFICATE_REMINDER_INTERVAL_DAYS = 30


def run_certificate_checks(limit: int = 500) -> dict:
    """Scan breeder certificates and send any due renewal / re-verification reminders."""
    counts = {"sent": 0, "scanned": 0}
    today = timezone.now().date()

    handled_sellers: set = set()
    verifications = (
        ExternalBreederVerification.objects
        .exclude(status__iexact="rejected")
        .order_by("seller_id", "-created_at")
        .iterator()
    )
    # ``limit`` counts SELLERS, not certificate rows. Slicing the rows meant a
    # breeder whose certificates happened to sort past the cut-off was never
    # scanned at all, and because the ordering is stable it was always the same
    # breeders being skipped.
    for verification in verifications:
        # Only the most recent (non-rejected) certificate per seller.
        if verification.seller_id in handled_sellers:
            continue
        if len(handled_sellers) >= limit:
            break
        handled_sellers.add(verification.seller_id)
        counts["scanned"] += 1
        try:
            if _remind_breeder_certificate(verification, today):
                counts["sent"] += 1
        except Exception:
            logger.exception("Certificate reminder failed for verification %s", verification.id)

    # Breeders who have never uploaded anything have no certificate row, so the
    # scan above cannot see them at all. They are the ones who most need
    # telling: nothing in their account explains why delivery is unavailable.
    counts.update(_remind_breeders_without_certificates(today, limit=limit))
    return counts


def _remind_breeders_without_certificates(today, *, limit: int) -> dict:
    """Chase active breeders with no certificate on record at all."""
    counts = {"no_certificate_scanned": 0, "no_certificate_sent": 0}
    sellers_with_certificates = set(
        ExternalBreederVerification.objects
        .exclude(status__iexact="rejected")
        .values_list("seller_id", flat=True)
    )
    cutoff = timezone.now() - timedelta(days=NO_CERTIFICATE_GRACE_DAYS)
    candidates = (
        ExternalBreederProfile.objects
        .filter(is_active=True)
        .exclude(user_id__in=sellers_with_certificates)
        .order_by("created_at", "id")
    )
    for profile in candidates.iterator():
        if counts["no_certificate_scanned"] >= limit:
            break
        # A profile with no created_at cannot be aged, so it is left alone
        # rather than chased on a guess.
        if not profile.created_at or profile.created_at > cutoff:
            continue
        counts["no_certificate_scanned"] += 1
        try:
            if _remind_missing_certificate(profile, today):
                counts["no_certificate_sent"] += 1
        except Exception:
            logger.exception("Missing-certificate reminder failed for profile %s", profile.id)
    return counts


def _remind_missing_certificate(profile, today) -> bool:
    user = ExternalUser.objects.filter(id=profile.user_id).first()
    if not user or not (getattr(user, "email", "") or "").strip():
        return False

    metadata = dict(profile.metadata or {})
    last_sent = metadata.get("no_cert_reminder_sent_on")
    if last_sent:
        try:
            previous = date.fromisoformat(str(last_sent))
        except ValueError:
            previous = None
        if previous and (today - previous).days < NO_CERTIFICATE_REMINDER_INTERVAL_DAYS:
            return False

    name = (getattr(user, "name", "") or getattr(user, "first_name", "") or "there").strip()
    result = send_custom_email(
        subject="Upload your breeder certificate to offer delivery",
        body=(
            f"Hi {name},\n\n"
            "We do not have a breeder certificate on file for your Aqua AI account.\n\n"
            "You can list your stock and sell for collection without one. A valid "
            "certificate is what lets you offer delivery — until we have one you "
            "will not be able to dispatch orders.\n\n"
            "To add it, upload your certificate in your account settings. We will "
            "review it and enable delivery once it is approved.\n\n"
            "If you only ever sell for collection in person, you can ignore this.\n\n"
            "Thank you,\nThe Aqua AI Team"
        ),
        recipients=[user.email.strip()],
        from_email=pick_alias_for_mailbox("providers"),
    )

    metadata["no_cert_reminder_sent_on"] = today.isoformat()
    profile.metadata = metadata
    profile.save(update_fields=["metadata"])
    logger.info("Missing-certificate reminder sent to %s (ok=%s)",
                user.email, bool(result.get("ok")))
    return True


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
        subject=_STAGE_SUBJECT[_stage_family(stage_key)],
        body=_renewal_body(user, detail, stage_key),
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


#: What each stage actually costs the breeder. Only a certificate with a
#: printed expiry date ever lapses, so only those stages may warn about losing
#: delivery; an undated certificate stays current indefinitely and warning its
#: holder that they cannot dispatch would simply be untrue.
_DELIVERY_AT_RISK = (
    "Please upload a current or renewed certificate in your account settings. A valid "
    "certificate is what lets you offer delivery \u2014 once it expires you can still "
    "list your stock and sell for collection, but you will not be able to dispatch orders."
)
_DELIVERY_ALREADY_OFF = (
    "Please upload a renewed certificate in your account settings. Until we have one you "
    "cannot dispatch orders \u2014 your listings stay up and you can still sell for "
    "collection in person, and delivery is restored as soon as the new certificate is "
    "approved."
)
_RECORDS_ONLY = (
    "Please upload your most recent certificate in your account settings so your "
    "verification stays up to date. Your listings, collection sales and delivery are "
    "unaffected in the meantime."
)

_STAGE_CONSEQUENCE = {
    "expiry_30d": _DELIVERY_AT_RISK,
    "expiry_7d": _DELIVERY_AT_RISK,
    "expired": _DELIVERY_ALREADY_OFF,
    "age_1yr": _RECORDS_ONLY,
    "age_2yr": _RECORDS_ONLY,
    "reverify": _RECORDS_ONLY,
}

_STAGE_SUBJECT = {
    "expiry_30d": "Your Aqua AI breeder certificate needs renewing to keep delivery on",
    "expiry_7d": "Your Aqua AI breeder certificate expires soon \u2014 renew it to keep delivery on",
    "expired": "Your Aqua AI breeder certificate has expired \u2014 delivery is paused",
    "age_1yr": "Keep your Aqua AI breeder certificate up to date",
    "age_2yr": "Your Aqua AI breeder certificate is due for renewal",
    "reverify": "Please re-confirm your Aqua AI breeder certificate",
}


def _stage_family(stage_key: str) -> str:
    """The stage without its year suffix, since recurring stages carry one."""
    for family in _STAGE_CONSEQUENCE:
        if stage_key == family or stage_key.startswith(family + "_"):
            return family
    return "reverify"


def _renewal_body(user, detail: str, stage_key: str = "reverify") -> str:
    name = (getattr(user, "name", "") or getattr(user, "first_name", "") or "there").strip()
    consequence = _STAGE_CONSEQUENCE[_stage_family(stage_key)]
    return (
        f"Hi {name},\n\n"
        f"This is a friendly reminder from Aqua AI: your verification certificate {detail}.\n\n"
        f"{consequence}\n\n"
        "If you have already renewed it, please ignore this message.\n\n"
        "Thank you,\nThe Aqua AI Team"
    )
