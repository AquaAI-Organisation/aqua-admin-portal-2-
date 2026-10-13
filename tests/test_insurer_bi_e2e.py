"""Verify the insurer exportable MI/BI engine (admin_portal/services/bi.py).

Production Postgres is not reachable from this container, so verification is
split in two:

  PART 1 (this script) runs the REAL engine against a database built from the
  REAL unmanaged mirror models, with seeded data. It proves the ORM logic,
  the KPI maths, the chart/table shapes and both exporters work.

  Crucially it also installs a log handler on ``admin_portal.services.bi``.
  The engine's helpers (_sum, _count, _monthly_series, _status_rows) each
  swallow their own exception and return 0/[] WITHOUT setting d.note, so a
  broken metric reads as a legitimate zero. Any logged exception is therefore
  treated as a hard failure -- that is the silent-rot failure mode we are
  hunting.

  PART 2 captures the exact PostgreSQL the engine emits (compiled against a
  postgres backend that is never connected) and writes it to JSON, so it can
  be executed against the real production database over the Supabase HTTPS
  API in a separate step.
"""
from __future__ import annotations

import datetime as dt
import decimal
import json
import logging
import os
import sys
import uuid

import pathlib as _pathlib
import tempfile as _tempfile

PORTAL = str(_pathlib.Path(__file__).resolve().parent.parent)
SCRATCH = _tempfile.mkdtemp(prefix="aquaai-bi-test-")
sys.path.insert(0, PORTAL)

SQLITE = f"{SCRATCH}/bi_verify.sqlite3"
if os.path.exists(SQLITE):
    os.remove(SQLITE)

# Keep the real .env (and therefore the real DATABASE_URL, used only to shape
# the never-connected 'pg' alias) but run the engine on local sqlite.
os.environ["DJANGO_SETTINGS_MODULE"] = "aqua_admin.settings"
os.environ["INBOX_AUTOREFRESH"] = "false"

import dj_database_url  # noqa: E402
from dotenv import dotenv_values  # noqa: E402

PROD_URL = dotenv_values(f"{PORTAL}/.env").get("DATABASE_URL", "")

os.environ["DATABASE_URL"] = f"sqlite:///{SQLITE}"

import django  # noqa: E402

django.setup()

from django.apps import apps  # noqa: E402
from django.conf import settings  # noqa: E402

# A second alias used ONLY to compile PostgreSQL. Never connected.
if PROD_URL:
    settings.DATABASES["pg"] = dj_database_url.parse(PROD_URL)

from django.db import connection, connections  # noqa: E402
from django.db.models.sql.query import Query  # noqa: E402

PASS, FAIL = [], []
CAPTURED: list[tuple[str, tuple]] = []
CAPTURE_ERRORS: list[str] = []
capturing = False


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{extra}]" if extra else ""))


def section(t):
    print(f"\n{'=' * 78}\n{t}\n{'=' * 78}")


# --------------------------------------------------------------------------
# Capture the PostgreSQL the engine would emit, without connecting.
# --------------------------------------------------------------------------
def install_pg_capture():
    if "pg" not in settings.DATABASES:
        return
    pg = connections["pg"]
    pg.__dict__["pg_version"] = 150004          # prefill: stops a version probe
    _orig = Query.get_compiler

    def get_compiler(self, using=None, connection=None, elide_empty=True):
        comp = _orig(self, using=using, connection=connection, elide_empty=elide_empty)
        if capturing:
            try:
                sql, params = _orig(self, using="pg", elide_empty=elide_empty).as_sql()
                CAPTURED.append((sql, params))
            except Exception as exc:
                CAPTURE_ERRORS.append(f"{type(exc).__name__}: {exc}")
        return comp

    Query.get_compiler = get_compiler


def lit(v):
    """Render a query parameter as a PostgreSQL literal."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float, decimal.Decimal)):
        return str(v)
    if isinstance(v, (dt.datetime, dt.date, dt.time, uuid.UUID)):
        return "'" + str(v) + "'"
    return "'" + str(v).replace("'", "''") + "'"


def inline(sql, params):
    out, i = [], 0
    for ch in sql:
        if ch == "%":
            i += 1
            out.append(ch)
            continue
        out.append(ch)
    # Simple positional substitution of %s placeholders.
    parts = sql.split("%s")
    if len(parts) - 1 != len(params):
        return None
    rendered = parts[0]
    for p, tail in zip(params, parts[1:]):
        rendered += lit(p) + tail
    return rendered.replace("%%", "%")


# --------------------------------------------------------------------------
# Build the schema from the mirror models
# --------------------------------------------------------------------------
def build_schema():
    mirrors = [m for m in apps.get_app_config("admin_portal").get_models()
               if not m._meta.managed]
    for m in mirrors:
        m._meta.managed = True
    with connection.schema_editor() as se:
        for m in mirrors:
            try:
                se.create_model(m)
            except Exception as exc:
                print(f"    (schema) {m.__name__}: {type(exc).__name__}: {exc}")
    return mirrors


# --------------------------------------------------------------------------
# Seed representative data across the whole 12-month window
# --------------------------------------------------------------------------
def seed():
    from django.utils import timezone
    from admin_portal import models as m

    now = timezone.now()

    def ago(days):
        return now - dt.timedelta(days=days)

    users = []
    for i in range(24):
        u = m.ExternalUser.objects.create(
            id=uuid.uuid4(),
            username=f"user{i}",
            email=(f"erased{i}@deleted.invalid" if i % 12 == 0 else f"user{i}@aqua.test"),
            password="x", first_name="F", last_name="L", name=f"User {i}",
            role=("breeder" if i % 3 == 0 else "consultant" if i % 3 == 1 else "user"),
            is_verified=(i % 2 == 0),
            is_at_risk=(i % 7 == 0),
            is_active=True,
            verification_documents={},
            current_regulatory_tier=["tier_1", "tier_2", "tier_3", None][i % 4],
            date_joined=ago(i * 14), created_at=ago(i * 14), updated_at=ago(i * 14),
        )
        users.append(u)

    breeders, consultants = [], []
    for i, u in enumerate(users[:8]):
        breeders.append(m.ExternalBreederProfile.objects.create(
            id=uuid.uuid4(), user=u, company_name=f"Breeder {i}", bio="",
            specializations=[], metadata={},
            created_at=ago(i * 20), updated_at=ago(i * 20)))
    for i, u in enumerate(users[8:16]):
        consultants.append(m.ExternalConsultantProfile.objects.create(
            id=uuid.uuid4(), user=u, company_name=f"Consultancy {i}", bio="",
            admin_status=["approved", "pending", "rejected", "approved"][i % 4],
            credentials=[], specializations=[], services_list=[], metadata={},
            created_at=ago(i * 20)))

    for i, u in enumerate(users[:10]):
        m.ExternalMarketplaceSellerProfile.objects.create(
            user=u,
            stripe_connect_status=["active", "pending", "restricted", "active"][i % 4],
            payouts_enabled=(i % 2 == 0),
            delivery_sales_enabled=(i % 3 == 0),
            delivery_suspended=(i % 5 == 0),
            updated_at=ago(i))

    today = now.date()
    for i, u in enumerate(users[:12]):
        m.ExternalBreederVerification.objects.create(
            seller=u, document="doc.pdf", licence_number=f"LIC{i}",
            issuing_authority="DEFRA",
            # a spread of expired / expiring / healthy / none
            expiry_date=[today - dt.timedelta(days=30),
                         today + dt.timedelta(days=20),
                         today + dt.timedelta(days=400),
                         None][i % 4],
            awarded_date=today - dt.timedelta(days=400),
            status=["approved", "approved", "pending", "rejected"][i % 4],
            rejection_reason="", document_metadata={},
            created_at=ago(i * 25), updated_at=ago(i * 25))

    reservations = []
    for i in range(30):
        r = m.ExternalBreederReservation.objects.create(
            reservation_code=f"RES{i:04d}",
            buyer=users[i % len(users)], seller=users[(i + 5) % len(users)],
            delivery_method=["delivery", "collection"][i % 2],
            status=["completed", "pending", "cancelled", "completed", "dispatched"][i % 5],
            payment_status=["paid", "paid", "failed", "refunded"][i % 4],
            subtotal=decimal.Decimal("40.00") + i,
            shipping_cost=decimal.Decimal("4.99"),
            platform_fee=decimal.Decimal("2.50"),
            total_amount=decimal.Decimal("47.49") + i,
            no_show_fee=decimal.Decimal("0.00"),
            tracking_number="", courier_name="", delivery_zone="UK",
            checkout_group_code=f"GRP{i}", metadata={},
            created_at=ago(i * 12), paid_at=ago(i * 12))
        reservations.append(r)

    for i in range(9):
        m.ExternalReservationDispute.objects.create(
            reservation=reservations[i], opened_by=users[i % len(users)],
            reason=["not_as_described", "dead_on_arrival", "not_delivered"][i % 3],
            description="d", breeder_response="",
            resolution=["refund_full", "refund_partial", "rejected"][i % 3],
            resolution_summary="", status="resolved",
            opened_at=ago(i * 30))

    for i in range(11):
        m.ExternalRefund.objects.create(
            id=uuid.uuid4(), payment_intent_id=uuid.uuid4(),
            amount=decimal.Decimal("15.00") + i, currency="GBP",
            status="succeeded",
            reason=["requested_by_customer", "dispute", "duplicate"][i % 3],
            created_at=ago(i * 28), processed_at=ago(i * 28))

    for i in range(7):
        m.ExternalPaymentFailureLog.objects.create(
            id=uuid.uuid4(), user_id=users[i].id, payment_intent_id=uuid.uuid4(),
            failure_reason="card_declined", failure_code="card_declined",
            stripe_error={}, amount=decimal.Decimal("22.00") + i, currency="GBP",
            endpoint="/pay", created_at=ago(i * 40))

    for i in range(26):
        m.ExternalConsultantBooking.objects.create(
            id=uuid.uuid4(), consultant=consultants[i % len(consultants)],
            requester_id=users[i % len(users)].id,
            scheduled_start=ago(i * 13), scheduled_end=ago(i * 13),
            full_price=decimal.Decimal("60.00") + i,
            booking_fee=decimal.Decimal("2.00"),
            payment_status=["paid", "pending", "refunded"][i % 3],
            # BOTH cancellation spellings on purpose: production writes the
            # backend's STATUS_CANCELED ("canceled", one L) while older rows
            # and other writers use "cancelled". The Cancelled KPI must count
            # both -- it silently read 0 until this was fixed.
            status=["completed", "canceled", "confirmed", "cancelled"][i % 4],
            consultant_status="accepted", notes="",
            rating=(None if i % 4 == 0 else (i % 5) + 1),
            review="", response_time_hours=float(i % 9),
            was_fast_response=(i % 2 == 0), was_successful=(i % 3 != 0),
            created_at=ago(i * 13), responded_at=ago(i * 13),
            completed_at=ago(i * 13))

    for i in range(6):
        m.ExternalConsultantWarning.objects.create(
            id=uuid.uuid4(), consultant=consultants[i % len(consultants)],
            title=f"Warning {i}", message="m",
            severity=["low", "medium", "high"][i % 3],
            status="open", metadata={}, created_at=ago(i * 50))

    return {
        "users": m.ExternalUser.objects.count(),
        "breeders": m.ExternalBreederProfile.objects.count(),
        "consultants": m.ExternalConsultantProfile.objects.count(),
        "sellers": m.ExternalMarketplaceSellerProfile.objects.count(),
        "verifications": m.ExternalBreederVerification.objects.count(),
        "reservations": m.ExternalBreederReservation.objects.count(),
        "disputes": m.ExternalReservationDispute.objects.count(),
        "refunds": m.ExternalRefund.objects.count(),
        "payment_failures": m.ExternalPaymentFailureLog.objects.count(),
        "bookings": m.ExternalConsultantBooking.objects.count(),
        "warnings": m.ExternalConsultantWarning.objects.count(),
    }


class Trap(logging.Handler):
    """Any ERROR logged by the BI engine means a metric silently degraded."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def main():
    global capturing

    section("0. SETUP")
    install_pg_capture()
    mirrors = build_schema()
    check("schema built from the real unmanaged mirror models", True,
          f"{len(mirrors)} mirror models")
    counts = seed()
    check("representative data seeded across the 12-month window",
          all(v > 0 for v in counts.values()),
          ", ".join(f"{k}={v}" for k, v in counts.items()))

    from admin_portal.services import bi

    trap = Trap()
    logging.getLogger("admin_portal.services.bi").addHandler(trap)

    start, end = bi.default_range()
    print(f"\n  window: {start:%Y-%m-%d} -> {end:%Y-%m-%d}")

    capturing = True
    dashboards = []
    for key in bi.ORDER:
        section(f"DASHBOARD '{key}'")
        before = len(trap.records)
        try:
            d = bi.build_dashboard(key, start, end)
        except Exception as exc:
            check(f"'{key}' builds", False, f"{type(exc).__name__}: {exc}")
            continue
        check(f"'{key}' builds and returns a Dashboard", d is not None)
        if d is None:
            continue
        dashboards.append(d)

        check(f"'{key}' set NO degradation note", not d.note, d.note or "note empty")

        # The real test: nothing was swallowed inside the helpers.
        swallowed = trap.records[before:]
        check(f"'{key}' logged NO swallowed metric failures", not swallowed,
              "; ".join(r.getMessage() for r in swallowed))

        check(f"'{key}' produced KPIs", len(d.kpis) > 0, f"{len(d.kpis)} KPIs")
        check(f"'{key}' produced charts", len(d.series) > 0, f"{len(d.series)} series")
        check(f"'{key}' produced tables", len(d.tables) > 0, f"{len(d.tables)} tables")

        nonzero = [k for k in d.kpis if (k.raw or 0) != 0]
        check(f"'{key}' every KPI carries real non-zero data",
              len(nonzero) == len(d.kpis), f"{len(nonzero)}/{len(d.kpis)} non-zero; "
              f"zero: {[k.label for k in d.kpis if (k.raw or 0) == 0]}")

        bad = [s.title for s in d.series if len(s.labels) != len(s.values)]
        check(f"'{key}' chart labels and values align", not bad, str(bad))

        empty_series = [s.title for s in d.series if not any(s.values)]
        check(f"'{key}' every chart has at least one non-zero point",
              not empty_series, str(empty_series))

        badt = {t.title for t in d.tables for r in t.rows if len(r) != len(t.columns)}
        check(f"'{key}' table rows match their headers", not badt, str(badt))

        empty_tables = [t.title for t in d.tables if not t.rows]
        check(f"'{key}' every table has rows", not empty_tables, str(empty_tables))

        if key == "consultants":
            # Regression: the Cancelled KPI must count BOTH spellings.
            from admin_portal import models as am
            expected = am.ExternalConsultantBooking.objects.filter(
                status__in=["canceled", "cancelled"]).count()
            got = next((k.raw for k in d.kpis if k.label == "Cancelled"), None)
            check("'consultants' Cancelled KPI counts BOTH 'canceled' and "
                  "'cancelled' spellings", got == expected and expected > 0,
                  f"KPI={got} actual cancelled rows={expected}")

        print("    --- as the insurer would read it ---")
        for k in d.kpis:
            print(f"      {k.label:<30} {k.value:<22} raw={k.raw}")
        for s in d.series:
            print(f"      chart: {s.title:<36} pts={len(s.values)} total={sum(s.values)}")
        for t in d.tables:
            print(f"      table: {t.title:<36} {t.rows}")

    capturing = False

    section("EXPORT: CSV")
    csv_text = ""
    try:
        csv_text = bi.to_csv(dashboards, start, end)
        check("to_csv() returns content", bool(csv_text), f"{len(csv_text)} chars")
        check("CSV carries the report header", "Aqua AI — BI Report" in csv_text)
        check("CSV states the reporting period", "Period" in csv_text)
        for d in dashboards:
            check(f"CSV contains the '{d.key}' section", f"# {d.title}" in csv_text)
        check("CSV contains no degradation note", "could not be computed" not in csv_text)
        lines = [ln for ln in csv_text.splitlines() if ln.strip()]
        check("CSV has substantive content", len(lines) > 60, f"{len(lines)} non-empty lines")
        open(f"{SCRATCH}/insurer_bi_report.csv", "w").write(csv_text)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        check("to_csv() works", False, f"{type(exc).__name__}: {exc}")

    section("EXPORT: PDF")
    try:
        pdf = bi.to_pdf(dashboards, start, end)
        check("to_pdf() returns bytes", isinstance(pdf, bytes), f"{len(pdf)} bytes")
        check("output is a real PDF", pdf[:5] == b"%PDF-", str(pdf[:8]))
        check("PDF is not an empty shell", len(pdf) > 8000, f"{len(pdf)} bytes")
        check("PDF is not truncated", b"%%EOF" in pdf[-3072:])
        open(f"{SCRATCH}/insurer_bi_report.pdf", "wb").write(pdf)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        check("to_pdf() works", False, f"{type(exc).__name__}: {exc}")

    section("EDGE WINDOWS (a report run over an odd date range must not break)")
    for label, s, e in [
        ("single day", end - dt.timedelta(days=1), end),
        ("8 years", end - dt.timedelta(days=365 * 8), end),
        ("future-only (no data)", end + dt.timedelta(days=1), end + dt.timedelta(days=30)),
    ]:
        before = len(trap.records)
        try:
            ds = bi.build_many(bi.ORDER, s, e)
            notes = [f"{x.key}: {x.note}" for x in ds if x.note]
            check(f"all four dashboards build over a {label} window", len(ds) == 4, f"{len(ds)}/4")
            check(f"no degradation note over a {label} window", not notes, "; ".join(notes))
            check(f"nothing swallowed over a {label} window",
                  len(trap.records) == before,
                  "; ".join(r.getMessage() for r in trap.records[before:]))
            bi.to_csv(ds, s, e)
            bi.to_pdf(ds, s, e)
            check(f"both exporters work over a {label} window", True)
        except Exception as exc:
            check(f"{label} window", False, f"{type(exc).__name__}: {exc}")

    section("TOTAL swallowed-failure count")
    check("the engine logged ZERO swallowed metric failures in the whole run",
          not trap.records, f"{len(trap.records)} errors")
    for r in trap.records:
        print("      LOGGED:", r.getMessage())

    # ---------------------------------------------------------------
    section("POSTGRES SQL CAPTURE (for execution against production)")
    check("the compiler produced no errors", not CAPTURE_ERRORS,
          "; ".join(CAPTURE_ERRORS[:3]))
    seen, probes = set(), []
    for sql, params in CAPTURED:
        rendered = inline(sql, params)
        if rendered and rendered not in seen:
            seen.add(rendered)
            probes.append(rendered)
    check("captured the PostgreSQL the engine emits", len(probes) > 20,
          f"{len(probes)} distinct statements from {len(CAPTURED)} queries")
    json.dump(probes, open(f"{SCRATCH}/bi_pg_queries.json", "w"), indent=1)
    print(f"    wrote {SCRATCH}/bi_pg_queries.json")

    print(f"\n{'=' * 78}\nRESULT: {len(PASS)} passed, {len(FAIL)} failed\n{'=' * 78}")
    for f in FAIL:
        print("  FAILED:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
