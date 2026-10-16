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
    lede = "We don't yet have a breeder certificate on file for your AQUA AI account."
    result = send_custom_email(
        subject=_STAGE_COPY["missing"]["subject"],
        reply_to=_reply_to(),
        html_body=_render_reminder_html(
            user=user, stage_family="missing", lede=lede,
            subject=_STAGE_COPY["missing"]["subject"]),
        # No detail table: there is no certificate to describe.
        body=_plain_body(name, "missing", lede, rows=None),
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
    """Return the stage key for the reminder due now, or None.

    Recurring stages carry the year in their key so they re-send at most once a
    year. The wording lives with the stage copy; this only decides which stage
    the certificate is in.
    """
    if verification.expiry_date:
        days_left = (verification.expiry_date - today).days
        if days_left < 0:
            return f"expired_{today.year}"
        if days_left <= EXPIRY_REMINDER_DAYS[1]:
            return "expiry_7d"
        if days_left <= EXPIRY_REMINDER_DAYS[0]:
            return "expiry_30d"
        return None

    issue = verification.awarded_date or (
        verification.created_at.date() if verification.created_at else None
    )
    if issue:
        age = (today - issue).days
        if age >= RENEWAL_DUE_DAYS:
            return f"age_2yr_{today.year}"
        if age >= FIRST_AGE_REMINDER_DAYS:
            return "age_1yr"
        return None

    # No expiry and no issue date on record -> annual re-verification.
    return f"reverify_{today.year}"


def _remind_breeder_certificate(verification, today) -> bool:
    profile = ExternalBreederProfile.objects.filter(user_id=verification.seller_id).first()
    user = ExternalUser.objects.filter(id=verification.seller_id).first()
    if not profile or not user or not (getattr(user, "email", "") or "").strip():
        return False

    stage_key = _due_stage(verification, today)
    if not stage_key:
        return False

    metadata = dict(profile.metadata or {})
    reminders = dict(metadata.get("cert_reminders") or {})
    cert_key = str(verification.id)
    sent_stages = list(reminders.get(cert_key) or [])
    if stage_key in sent_stages:
        return False  # already reminded for this stage of this certificate

    family = _stage_family(stage_key)
    rows = _certificate_rows(verification)
    lede_html, lede_plain = _lede(family, verification, today)
    days_left = ((verification.expiry_date - today).days
                 if verification.expiry_date else 0)
    subject = _STAGE_COPY[family]["subject"].format(days=days_left)
    name = (getattr(user, "name", "") or getattr(user, "first_name", "") or "there").strip()
    result = send_custom_email(
        subject=subject,
        body=_plain_body(name, family, lede_plain, rows),
        recipients=[user.email.strip()],
        from_email=pick_alias_for_mailbox("providers"),
        reply_to=_reply_to(),
        html_body=_render_reminder_html(
            user=user, stage_family=family, lede=lede_html, certificate_rows=rows,
            subject=subject),
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


#: Approved wording, 16 Oct 2026. The design is unchanged; only the words are
#: set here, one entry per stage, so a copy change never means touching the
#: template. ``consequence`` is the callout box, ``upload_tail`` is appended to
#: the standing upload sentence, and stages 5-7 have no tail because nothing is
#: at stake for them.
_STAGE_COPY = {
    "missing": {
        "subject": "Upload your certificate to offer delivery to your customers",
        "pill_label": "Certificate needed",
        "pill_bg": "#FEF3C7", "pill_fg": "#92400E",
        "callout_bg": "#FFFBEB", "callout_border": "#F59E0B",
        "consequence": (
            "You can still list your stock and let customers collect in person. "
            "A valid certificate also lets you offer delivery."
        ),
        "upload_tail": "We'll review it and switch delivery on for you once it's approved.",
        "ignore_note": "If you only sell for collection in person, there's nothing you need to do.",
        # Only this one carries the licensing signpost: it is the only stage
        # where the breeder may not know a licence is needed at all.
        "licence_note": True,
    },
    "expiry_30d": {
        "subject": "Your certificate is due for renewal",
        "pill_label": "Renewal due",
        "pill_bg": "#FEF3C7", "pill_fg": "#92400E",
        "callout_bg": "#FFFBEB", "callout_border": "#F59E0B",
        "consequence": (
            "Your certificate is what lets you offer delivery to your customers through "
            "AQUA AI. If it expires, you can still list your stock and sell for collection, "
            "but delivery will be paused until a renewed certificate is approved."
        ),
        "upload_tail": "We'll review it and keep delivery running for your business.",
    },
    "expiry_7d": {
        "subject": "Your certificate expires in {days} days",
        "pill_label": "Expires soon",
        "pill_bg": "#FEF3C7", "pill_fg": "#92400E",
        "callout_bg": "#FFFBEB", "callout_border": "#F59E0B",
        "consequence": (
            "To keep offering delivery to your customers without a break, please upload "
            "your renewed certificate before then. After it expires you can still list "
            "your stock and sell for collection, but delivery will be paused until the "
            "new certificate is approved."
        ),
        "upload_tail": "We'll review it as quickly as we can.",
    },
    "expired": {
        "subject": "Your certificate has expired, so delivery is paused",
        "pill_label": "Delivery paused",
        "pill_bg": "#FEE2E2", "pill_fg": "#991B1B",
        "callout_bg": "#FEF2F2", "callout_border": "#EF4444",
        "consequence": (
            "Your listings stay live, and customers can still buy from you for "
            "collection in person."
        ),
        "upload_tail": "We'll review it and switch delivery back on as soon as it's approved.",
    },
    "age_1yr": {
        "subject": "Time to plan your certificate renewal",
        "pill_label": "Renewal reminder",
        "pill_bg": "#DBEAFE", "pill_fg": "#1E40AF",
        "callout_bg": "#EFF6FF", "callout_border": "#3B82F6",
        "consequence": (
            "Nothing changes in the meantime: your listings, collection and delivery "
            "all continue as normal."
        ),
    },
    "age_2yr": {
        "subject": "Your certificate is due for renewal",
        "pill_label": "Renewal due",
        "pill_bg": "#FEF3C7", "pill_fg": "#92400E",
        "callout_bg": "#FFFBEB", "callout_border": "#F59E0B",
        "consequence": (
            "Please upload your current certificate so your verification stays up to "
            "date. Your listings, collection and delivery continue as normal in the "
            "meantime."
        ),
    },
    "reverify": {
        "subject": "Please confirm your certificate is still current",
        "pill_label": "Confirmation needed",
        "pill_bg": "#DBEAFE", "pill_fg": "#1E40AF",
        "callout_bg": "#EFF6FF", "callout_border": "#3B82F6",
        "consequence": (
            "Please upload your most recent certificate. Your listings, collection and "
            "delivery continue as normal in the meantime."
        ),
    },
}

#: Everyone gets the same standing lines.
UPLOAD_LINE = "Upload your certificate in the Aqua Providers app, under your account settings."
HELP_LINE = "If you need any help, just reply to this email or contact support@aquaai.uk."
IGNORE_NOTE = "If you've already uploaded your renewed certificate, please ignore this email."
SUPPORT_EMAIL = "support@aquaai.uk"
FAQ_URL = "https://aquaai.uk/faqs"


def _certificate_rows(verification) -> list[tuple[str, str]]:
    """The certificate's own details, so the breeder knows which one we mean.

    A breeder may hold certificates for several sites, so naming the number
    saves them opening the app to work out which reminder this is. A row is
    omitted when we hold no value for it, except the expiry: saying there is
    none is the point of the two-year cycle, so that one is always shown.
    """
    rows: list[tuple[str, str]] = []
    if getattr(verification, "licence_number", ""):
        rows.append(("Certificate number", str(verification.licence_number)))
    if getattr(verification, "issuing_authority", ""):
        rows.append(("Issuing authority", str(verification.issuing_authority)))
    if getattr(verification, "awarded_date", None):
        rows.append(("Issued", verification.awarded_date.strftime("%d %b %Y")))
    if getattr(verification, "expiry_date", None):
        rows.append(("Expires", verification.expiry_date.strftime("%d %b %Y")))
    else:
        rows.append(("Expires", "No expiry date shown"))
    return rows


def _stage_family(stage_key: str) -> str:
    """The stage without its year suffix, since recurring stages carry one."""
    for family in _STAGE_COPY:
        if stage_key == family or stage_key.startswith(family + "_"):
            return family
    return "reverify"


def _lede(stage_family: str, verification, today) -> tuple[str, str]:
    """The opening line, as (html, plain).

    It carries the dates, so it is built per stage rather than pulled from a
    table: the date is the whole point of the sentence, and burying it in a
    shared "your certificate {detail}" line is what made the expired email read
    like the others.
    """
    from django.utils.html import format_html

    expiry = getattr(verification, "expiry_date", None)
    issued = getattr(verification, "awarded_date", None)
    when = expiry.strftime("%d %b %Y") if expiry else ""
    days = (expiry - today).days if expiry else 0
    issued_str = issued.strftime("%d %b %Y") if issued else ""

    if stage_family == "expiry_30d":
        plain = (f"This is a friendly reminder that your breeder certificate expires on "
                 f"{when} (in {days} days).")
        return format_html(
            "This is a friendly reminder that your breeder certificate expires on "
            "<strong style=\"color:#0F172A;\">{}</strong> (in {} days).", when, days), plain
    if stage_family == "expiry_7d":
        plain = f"Your breeder certificate expires on {when}, in {days} days."
        return format_html(
            "Your breeder certificate expires on "
            "<strong style=\"color:#0F172A;\">{}</strong>, in {} days.", when, days), plain
    if stage_family == "expired":
        plain = (f"Your breeder certificate expired on {when}, so delivery from your "
                 f"AQUA AI store is paused for now.")
        return format_html(
            "Your breeder certificate expired on "
            "<strong style=\"color:#0F172A;\">{}</strong>, so delivery from your AQUA AI "
            "store is paused for now.", when), plain
    if stage_family == "age_1yr":
        due = (issued + timedelta(days=RENEWAL_DUE_DAYS)).strftime("%d %b %Y") if issued else ""
        plain = (f"Your breeder certificate was issued on {issued_str}. As it doesn't show "
                 f"an expiry date, we ask for a renewed certificate every two years, so "
                 f"please plan to upload a new one by {due}.")
        return format_html(
            "Your breeder certificate was issued on "
            "<strong style=\"color:#0F172A;\">{}</strong>. As it doesn't show an expiry "
            "date, we ask for a renewed certificate every two years, so please plan to "
            "upload a new one by <strong style=\"color:#0F172A;\">{}</strong>.",
            issued_str, due), plain
    if stage_family == "age_2yr":
        plain = (f"Your breeder certificate was issued on {issued_str}, more than two "
                 f"years ago, so it's now due for renewal.")
        return format_html(
            "Your breeder certificate was issued on "
            "<strong style=\"color:#0F172A;\">{}</strong>, more than two years ago, so "
            "it's now due for renewal.", issued_str), plain
    # reverify: no dates at all, which is itself the reason for the email.
    plain = ("The breeder certificate on your AQUA AI account doesn't show an issue or "
             "expiry date, so we ask you to confirm once a year that it's still current.")
    return plain, plain


def _render_reminder_html(*, user, stage_family: str, lede, certificate_rows=None,
                          subject: str = "") -> str:
    """Render the branded reminder, matching the provider approval email."""
    from django.conf import settings
    from django.template.loader import render_to_string

    copy = _STAGE_COPY[stage_family]
    name = (getattr(user, "name", "") or getattr(user, "first_name", "") or "there").strip()
    return render_to_string("emails/breeders/breeder_certificate_reminder.html", {
        "name": name,
        "lede": lede,
        "consequence": copy["consequence"],
        "upload_tail": copy.get("upload_tail", ""),
        "ignore_note": copy.get("ignore_note", IGNORE_NOTE),
        "licence_note": copy.get("licence_note", False),
        "certificate_rows": certificate_rows or [],
        # The formatted subject, not the template: the 7-day stage carries a
        # {days} placeholder and an unformatted title leaks it into the tab.
        "email_title": subject or copy["subject"].format(days=0),
        "pill_label": copy["pill_label"],
        "pill_bg": copy["pill_bg"], "pill_fg": copy["pill_fg"],
        "callout_bg": copy["callout_bg"], "callout_border": copy["callout_border"],
        # Absolute https URL to a PNG or JPG, or blank: the template renders no
        # <img> at all rather than a broken-image icon.
        "logo_url": (getattr(settings, "EMAIL_LOGO_URL", "") or "").strip(),
        "privacy_url": getattr(settings, "PRIVACY_POLICY_URL", "https://aquaai.uk/privacy"),
        "terms_url": getattr(settings, "TERMS_URL", "https://aquaai.uk/terms"),
        "support_email": SUPPORT_EMAIL,
        "faq_url": FAQ_URL,
    })


def _reply_to() -> str:
    from django.conf import settings

    return (getattr(settings, "PROVIDER_EMAIL_REPLY_TO", "") or SUPPORT_EMAIL).strip()


def _plain_body(name: str, stage_family: str, lede_plain: str, rows) -> str:
    """The text alternative, carrying the same words as the HTML."""
    copy = _STAGE_COPY[stage_family]
    parts = [f"Hi {name},", "", lede_plain, ""]
    if rows:
        parts += [f"{label}: {value}" for label, value in rows] + [""]
    parts += [copy["consequence"], ""]
    if copy.get("licence_note"):
        parts += [
            "Selling animals as pets as a business usually needs a licence. To find out "
            f"how to get one, see our guide at {FAQ_URL}.",
            "",
        ]
    upload = UPLOAD_LINE
    if copy.get("upload_tail"):
        upload += " " + copy["upload_tail"]
    parts += [upload, "", HELP_LINE, "", copy.get("ignore_note", IGNORE_NOTE), "",
              "Thank you,", "The AQUA AI team"]
    return "\n".join(parts)
