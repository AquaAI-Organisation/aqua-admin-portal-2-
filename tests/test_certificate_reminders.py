"""The portal's certificate renewal emails: coverage, cadence and copy.

This is the reminder path that actually runs in production — the in-process
scheduler calls it every six hours — so it is the one a breeder really hears
from. Two things are checked that reading the code does not settle:

  * every breeder gets scanned. The scan used to slice 500 certificate ROWS
    after ordering by seller, so a breeder whose certificates sorted past the
    cut-off was never looked at, and because the ordering is stable it was the
    same breeders every time.
  * the copy names what a lapsed certificate actually costs. The licence gates
    dispatch, so an email implying the whole account stops is both wrong and
    the wrong thing to worry someone with.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import uuid
from datetime import date, timedelta

PORTAL = str(pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, PORTAL)

os.environ["DJANGO_SETTINGS_MODULE"] = "aqua_admin.settings"
os.environ["DATABASE_URL"] = "sqlite:///" + tempfile.mkstemp(suffix=".sqlite3")[1]
os.environ["INBOX_AUTOREFRESH"] = "false"

import django  # noqa: E402

django.setup()

from django.test import override_settings  # noqa: E402
from django.utils import timezone  # noqa: E402

PASS, FAIL = [], []
SENT: list[dict] = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{extra}]" if extra else ""))


def section(title):
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def build_mirror_schema():
    from django.apps import apps
    from django.db import connection

    mirrors = [m for m in apps.get_app_config("admin_portal").get_models()
               if not m._meta.managed]
    for m in mirrors:
        m._meta.managed = True
    with connection.schema_editor() as se:
        for m in mirrors:
            try:
                se.create_model(m)
            except Exception:
                pass


@override_settings(ADMIN_EMAILS=[], SECURE_SSL_REDIRECT=False)
def main():
    from admin_portal.models import (
        ExternalBreederProfile, ExternalBreederVerification, ExternalUser,
    )
    from admin_portal.services import certificates as certs

    build_mirror_schema()

    # Capture outbound mail rather than sending it.
    def fake_send(**kwargs):
        SENT.append(kwargs)
        return {"ok": True, "error": ""}

    certs.send_custom_email = fake_send
    certs.pick_alias_for_mailbox = lambda mailbox: "providers@aquaai.uk"

    today = timezone.now().date()

    def make_breeder(label, *, expiry=None, awarded=None, certs_count=1):
        user = ExternalUser.objects.create(
            id=uuid.uuid4(), email=f"{label}@aqua.test", is_active=True)
        profile = ExternalBreederProfile.objects.create(
            id=uuid.uuid4(), user_id=user.id, company_name=label,
            is_active=True, metadata={})
        rows = [
            ExternalBreederVerification.objects.create(
                seller_id=user.id, licence_number=f"{label}-{n}",
                issuing_authority="DEFRA", status="approved",
                expiry_date=expiry, awarded_date=awarded,
                created_at=timezone.now() - timedelta(days=n),
            )
            for n in range(certs_count)
        ]
        return user, profile, rows

    section("1. A CERTIFICATE NEARING EXPIRY IS CHASED")
    SENT.clear()
    make_breeder("expiring", expiry=today + timedelta(days=20))
    counts = certs.run_certificate_checks()
    check("a certificate 20 days from expiry is chased", counts["sent"] == 1, str(counts))
    check("exactly one email went out", len(SENT) == 1, str(len(SENT)))

    section("2. THE COPY NAMES DELIVERY, NOT THE WHOLE ACCOUNT")
    body = (SENT[0]["body"] if SENT else "").lower()
    subject = (SENT[0]["subject"] if SENT else "").lower()
    check("the body explains delivery is what stops", "delivery" in body, subject)
    check("and that collection still works", "collection" in body)
    check("and that listing still works", "list" in body)
    check("it does not claim the account stops being verified",
          "fully verified" not in body)
    check("the subject says delivery is at stake", "delivery" in subject, subject)

    section("3. EACH BREEDER IS CHASED ONCE PER STAGE")
    SENT.clear()
    counts = certs.run_certificate_checks()
    check("a second run sends nothing for the same stage",
          counts["sent"] == 0 and not SENT, str(counts))

    section("4. STAGES ESCALATE AS EXPIRY APPROACHES")
    SENT.clear()
    ExternalBreederVerification.objects.filter(licence_number="expiring-0").update(
        expiry_date=today + timedelta(days=5))
    counts = certs.run_certificate_checks()
    check("moving inside 7 days chases again", counts["sent"] == 1, str(counts))
    check("and the detail states the new date",
          "in 5 days" in (SENT[0]["body"] if SENT else ""),
          (SENT[0]["body"][:0] if SENT else ""))

    SENT.clear()
    ExternalBreederVerification.objects.filter(licence_number="expiring-0").update(
        expiry_date=today - timedelta(days=1))
    counts = certs.run_certificate_checks()
    check("and expiry itself chases once more", counts["sent"] == 1, str(counts))

    section("5. A CERTIFICATE WELL INSIDE ITS LIFE IS LEFT ALONE")
    SENT.clear()
    ExternalBreederVerification.objects.all().delete()
    ExternalBreederProfile.objects.all().update(metadata={})
    make_breeder("healthy", expiry=today + timedelta(days=200))
    counts = certs.run_certificate_checks()
    check("nothing is sent 200 days out", counts["sent"] == 0, str(counts))

    section("6. EVERY BREEDER IS SCANNED, NOT THE FIRST N CERTIFICATE ROWS")
    # The old slice took 500 certificate rows ordered by seller, so breeders
    # holding several certificates could exhaust it and starve the rest.
    SENT.clear()
    ExternalBreederVerification.objects.all().delete()
    ExternalBreederProfile.objects.all().delete()
    ExternalUser.objects.all().delete()
    for n in range(6):
        make_breeder(f"multi{n}", expiry=today + timedelta(days=20), certs_count=3)
    counts = certs.run_certificate_checks(limit=5)
    check("the limit counts breeders, not certificate rows",
          counts["scanned"] == 5, f"scanned {counts['scanned']} (18 rows exist)")
    check("and each scanned breeder got one email", len(SENT) == 5, str(len(SENT)))

    SENT.clear()
    counts = certs.run_certificate_checks(limit=50)
    check("a later run reaches the breeder the limit cut off",
          counts["sent"] == 1, f"sent {counts['sent']}")

    section("7. ONE EMAIL PER BREEDER, NOT ONE PER CERTIFICATE")
    SENT.clear()
    ExternalBreederVerification.objects.all().delete()
    ExternalBreederProfile.objects.all().update(metadata={})
    make_breeder("stacked", expiry=today + timedelta(days=20), certs_count=4)
    counts = certs.run_certificate_checks()
    check("four certificates produce one email", len(SENT) == 1, str(len(SENT)))
    check("and one breeder scanned", counts["scanned"] == 1, str(counts))

    section("8. A BREEDER WITH NO EMAIL IS SKIPPED QUIETLY")
    SENT.clear()
    ExternalBreederVerification.objects.all().delete()
    ExternalBreederProfile.objects.all().delete()
    ExternalUser.objects.all().delete()
    blank = ExternalUser.objects.create(id=uuid.uuid4(), email="", is_active=True)
    ExternalBreederProfile.objects.create(
        id=uuid.uuid4(), user_id=blank.id, company_name="No Email", metadata={})
    ExternalBreederVerification.objects.create(
        seller_id=blank.id, licence_number="noemail-0", issuing_authority="DEFRA",
        status="approved", expiry_date=today + timedelta(days=5))
    counts = certs.run_certificate_checks()
    check("no email is attempted", not SENT, str(len(SENT)))
    check("and it does not raise", counts["scanned"] == 1, str(counts))

    section("9. REJECTED CERTIFICATES ARE NOT CHASED FOR RENEWAL")
    SENT.clear()
    ExternalBreederVerification.objects.all().delete()
    ExternalBreederProfile.objects.all().delete()
    ExternalUser.objects.all().delete()
    user, profile, _ = make_breeder("rejected", expiry=today + timedelta(days=5))
    ExternalBreederVerification.objects.all().update(status="rejected")
    ExternalBreederProfile.objects.all().update(metadata={})
    counts = certs.run_certificate_checks()
    check("a rejected certificate is not treated as one to renew",
          counts["scanned"] == 0 and not SENT, str(counts))

    section("10. A BREEDER WHO NEVER UPLOADED IS CHASED, AFTER A GRACE PERIOD")
    SENT.clear()
    ExternalBreederVerification.objects.all().delete()
    ExternalBreederProfile.objects.all().delete()
    ExternalUser.objects.all().delete()

    def bare_breeder(label, *, age_days):
        user = ExternalUser.objects.create(
            id=uuid.uuid4(), email=f"{label}@aqua.test", is_active=True)
        return user, ExternalBreederProfile.objects.create(
            id=uuid.uuid4(), user_id=user.id, company_name=label, is_active=True,
            metadata={}, created_at=timezone.now() - timedelta(days=age_days))

    _, brand_new = bare_breeder("brandnew", age_days=1)
    _, settled = bare_breeder("settled", age_days=30)
    counts = certs.run_certificate_checks()
    recipients = sorted(r for s in SENT for r in s["recipients"])
    check("a breeder past the grace period is chased",
          "settled@aqua.test" in recipients, str(recipients))
    check("a breeder who signed up yesterday is left alone",
          "brandnew@aqua.test" not in recipients, str(recipients))
    check("the counts report it separately",
          counts.get("no_certificate_sent") == 1, str(counts))

    body = next((s["body"] for s in SENT if "settled" in s["recipients"][0]), "").lower()
    subject = next((s["subject"] for s in SENT if "settled" in s["recipients"][0]), "").lower()
    check("the email says delivery is what needs the certificate",
          "delivery" in body and "delivery" in subject)
    check("and that collection still works without one", "collection" in body)
    check("and that they can still list", "list your stock" in body)

    section("11. THE MISSING-CERTIFICATE CHASE IS MONTHLY, NOT EVERY SWEEP")
    SENT.clear()
    for _ in range(3):
        certs.run_certificate_checks()
    check("repeated sweeps send nothing more", not SENT, str(len(SENT)))

    SENT.clear()
    stale = (timezone.now().date() - timedelta(
        days=certs.NO_CERTIFICATE_REMINDER_INTERVAL_DAYS + 1)).isoformat()
    meta = dict(settled.metadata or {})
    meta["no_cert_reminder_sent_on"] = stale
    ExternalBreederProfile.objects.filter(pk=settled.pk).update(metadata=meta)
    certs.run_certificate_checks()
    check("but it returns after the interval",
          any("settled" in s["recipients"][0] for s in SENT), str(len(SENT)))

    section("12. UPLOADING A CERTIFICATE STOPS THE CHASE")
    SENT.clear()
    ExternalBreederProfile.objects.filter(pk=settled.pk).update(metadata={})
    ExternalBreederVerification.objects.create(
        seller_id=settled.user_id, licence_number="settled-now",
        issuing_authority="DEFRA", status="pending",
        expiry_date=timezone.now().date() + timedelta(days=400))
    counts = certs.run_certificate_checks()
    recipients = sorted(r for s in SENT for r in s["recipients"])
    check("a breeder with a certificate on file is no longer chased for missing one",
          "settled@aqua.test" not in recipients, str(recipients))
    check("and is not counted as missing",
          counts.get("no_certificate_sent") == 0, str(counts))

    section("13. AN INACTIVE BREEDER IS NOT CHASED")
    SENT.clear()
    ExternalBreederVerification.objects.all().delete()
    ExternalBreederProfile.objects.all().delete()
    ExternalUser.objects.all().delete()
    gone = ExternalUser.objects.create(
        id=uuid.uuid4(), email="gone@aqua.test", is_active=True)
    ExternalBreederProfile.objects.create(
        id=uuid.uuid4(), user_id=gone.id, company_name="Closed", is_active=False,
        metadata={}, created_at=timezone.now() - timedelta(days=60))
    counts = certs.run_certificate_checks()
    check("a deactivated breeder profile is skipped",
          not SENT and counts.get("no_certificate_scanned") == 0, str(counts))

    section("14. THE COPY ONLY THREATENS DELIVERY WHERE DELIVERY IS AT RISK")
    # A certificate with no printed expiry never lapses: effective_status only
    # downgrades to expired when expiry_date is set and past. Telling its holder
    # they cannot dispatch would be false, so the undated stages must not.
    ExternalBreederVerification.objects.all().delete()
    ExternalBreederProfile.objects.all().delete()
    ExternalUser.objects.all().delete()

    def render(label, **cert_kw):
        ExternalBreederVerification.objects.all().delete()
        ExternalBreederProfile.objects.all().delete()
        ExternalUser.objects.all().delete()
        u, _profile, _rows = make_breeder(label, certs_count=0)
        ExternalBreederVerification.objects.create(
            seller_id=u.id, licence_number=f"{label}-x", issuing_authority="DEFRA",
            status="approved", **cert_kw)
        SENT.clear()
        certs.run_certificate_checks()
        return (SENT[0] if SENT else {"subject": "", "body": ""})

    at_risk = render("soon", expiry_date=today + timedelta(days=21))
    check("an expiring certificate warns about losing delivery",
          "not be able to dispatch" in at_risk["body"])

    already = render("gone", expiry_date=today - timedelta(days=3))
    check("an expired certificate says delivery is already off",
          "cannot dispatch orders" in already["body"], already["subject"])
    check("and its subject says so too", "delivery is paused" in already["subject"],
          already["subject"])
    check("and it promises restoration on approval",
          "restored as soon as" in already["body"])

    for label, kw in [
        ("undated1", dict(expiry_date=None, awarded_date=today - timedelta(days=400))),
        ("undated2", dict(expiry_date=None, awarded_date=today - timedelta(days=760))),
    ]:
        mail = render(label, **kw)
        check(f"an undated certificate ({label}) does not claim dispatch stops",
              "dispatch" not in mail["body"], mail["subject"])
        check(f"and tells them delivery is unaffected ({label})",
              "delivery are unaffected" in mail["body"])

    section("15. EVERY STAGE HAS COPY AND A SUBJECT OF ITS OWN")
    families = sorted(certs._STAGE_CONSEQUENCE)
    check("consequence copy exists for every stage family",
          all(certs._STAGE_CONSEQUENCE.get(f) for f in families), str(families))
    check("a subject exists for every stage family",
          all(certs._STAGE_SUBJECT.get(f) for f in families), str(families))
    check("the year-suffixed recurring keys resolve to their family",
          certs._stage_family(f"expired_{today.year}") == "expired"
          and certs._stage_family(f"age_2yr_{today.year}") == "age_2yr"
          and certs._stage_family(f"reverify_{today.year}") == "reverify"
          and certs._stage_family("expiry_7d") == "expiry_7d")
    check("an unrecognised key falls back rather than raising",
          certs._stage_family("something_new") == "reverify")

    print(f"\n{'=' * 74}\nRESULT: {len(PASS)} passed, {len(FAIL)} failed\n{'=' * 74}")
    for name in FAIL:
        print("  FAILED:", name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    from django.test.runner import DiscoverRunner
    from django.test.utils import setup_test_environment, teardown_test_environment

    setup_test_environment()
    runner = DiscoverRunner(verbosity=0, interactive=False)
    config = runner.setup_databases()
    try:
        code = main()
    finally:
        runner.teardown_databases(config)
        teardown_test_environment()
    sys.exit(code)
