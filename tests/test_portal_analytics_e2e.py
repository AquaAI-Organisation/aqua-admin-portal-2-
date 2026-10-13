"""End-to-end test of the admin portal's App Analytics section and the
insurer Reports section, through the real URLs, real views and real templates.

The backend bridge is stubbed so no network is needed, but everything else is
the production code path: real decorators, real session auth, real template
rendering. It checks the pages work when the bridge IS configured, and that
they degrade to a readable notice (rather than a 500) when it is NOT -- which
is how they will behave until AQUAAI_BACKEND_API_URL / _TOKEN are deployed.
"""
from __future__ import annotations

import os
import sys

import pathlib as _pathlib
import tempfile as _tempfile

PORTAL = str(_pathlib.Path(__file__).resolve().parent.parent)
SCRATCH = _tempfile.mkdtemp(prefix="aquaai-bi-test-")
sys.path.insert(0, PORTAL)

os.environ["DJANGO_SETTINGS_MODULE"] = "aqua_admin.settings"
os.environ["DATABASE_URL"] = f"sqlite:///{SCRATCH}/portal_test.sqlite3"
os.environ["INBOX_AUTOREFRESH"] = "false"

import django  # noqa: E402

django.setup()

from django.test import override_settings  # noqa: E402
from django.test.utils import setup_test_environment, teardown_test_environment  # noqa: E402

PASS, FAIL = [], []

# The portal runs with DEBUG=False, which turns on SECURE_SSL_REDIRECT and the
# secure-cookie flags. Over the test client's plain-http requests that makes
# every URL answer 301 before the view ever runs, which would make every
# assertion below vacuous. Disable just those three for the test.
PROD_SECURITY_OFF = override_settings(
    SECURE_SSL_REDIRECT=False,
    SESSION_COOKIE_SECURE=False,
    CSRF_COOKIE_SECURE=False,
)


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{extra}]" if extra else ""))


def section(t):
    print(f"\n{'=' * 78}\n{t}\n{'=' * 78}")


# Stub payloads shaped to the ACTUAL contract of the backend's admin analytics
# API (user_analytics/admin_views.py: AdminPlatformOverviewView._users /
# ._consultants / ._breeders and AdminEntityAnalyticsView.get), which is what
# the portal templates read.
PERIOD = {"key": "3m", "label": "Last 3 months",
          "start_date": "2026-07-13", "end_date": "2026-10-13"}

OVERVIEW_BY_TYPE = {
    "users": {
        "type": "users", "time_period": PERIOD,
        "accounts": {"total": 177, "new_in_period": 12, "active": 59},
        "purchasing": {"buyers": 21, "settled_spend": 10056.35,
                       "average_order_value": 150.09, "settled_orders": 67},
        "consultations": {"bookings": 18, "requesters": 9, "average_price": 36.11},
    },
    "consultants": {
        "type": "consultants", "time_period": PERIOD,
        "consultants": {"total": 19, "with_bookings_in_period": 9},
        "bookings": {"total": 18, "completed": 0, "cancelled": 2,
                     "completion_rate": 0.0, "gross_value": 650.00,
                     "platform_fees": 65.00, "average_price": 36.11,
                     "monthly": [{"month": "2026-09", "value": 300.0}]},
        "most_booked": [{"consultant_id": "c-1", "bookings": 5, "gross_value": 222.22}],
    },
    "breeders": {
        "type": "breeders", "time_period": PERIOD,
        "breeders": {"total": 17, "selling_in_period": 6},
        "sales": {"settled_orders": 67, "cancelled": 5, "gross_value": 10056.35,
                  "average_order_value": 150.09},
        "fulfilment": {"delivery": 40, "collection": 27},
        "top_sellers": [{"breeder_id": "b-1", "orders": 12, "revenue": 333.33}],
    },
}
SEARCH = {"data": {"results": [
    {"id": "f091f448-d6aa-45b0-af95-42d3e5952dc7", "name": "Rohit Sharma",
     "email": "rohitomegalol@gmail.com", "role": "user"},
    {"id": "aaa-bbb", "name": "Ben Ibeonochie", "email": "ben@humara.io", "role": "user"},
]}}
ENTITY = {"data": {
    "type": "users", "time_period": PERIOD,
    "account": {"id": "f091f448-d6aa-45b0-af95-42d3e5952dc7",
                "email": "rohitomegalol@gmail.com", "name": "Rohit Sharma",
                "role": "user", "is_active": True, "date_joined": "2026-01-04",
                "trust_score": None, "regulatory_tier": "tier_1"},
    # Keys here are the real ones returned by
    # user_analytics.services.metrics.UserAnalyticsService.get_full_metrics.
    "user_analytics": {
        "time_period": PERIOD,
        "purchases": {"orders": 3, "items_purchased": 5, "total_spend": 61.98,
                      "settled_spend": 61.98, "average_order_value": 20.66},
        "consultants": {"bookings": 2, "total_paid": 40.00, "booking_fees_paid": 4.00},
        "marketplace": {"listings": {"total": 2, "active": 1, "sold": 1}},
        "community": {"showcases_published": 1, "showcases_total": 1,
                      "photos_shared": 4, "likes_given": 7, "likes_received": 3},
        "planner": {"runs": 7},
        "recommendations": {"generated": 9, "accepted": 3},
        "moderation": {"reports_made": 0, "reports_against": 0, "blocked_users": 1},
    },
}}

# A distinctive value that must appear on each overview page if the backend's
# numbers actually reached the template.
MARKERS = {
    "users": ["177", "10056.35", "150.09"],
    # leaderboard id + value, and the KPI block, must all reach the page
    "consultants": ["650", "c-1", "222.22"],
    "breeders": ["b-1", "333.33", "collection", "10056.35"],
}


@PROD_SECURITY_OFF
def main():
    from django.test import Client
    from django.contrib.auth import get_user_model
    from admin_portal.services import analytics_backend as ab

    AdminUser = get_user_model()

    section("0. SETUP")
    # AdminUser authenticates by email (USERNAME_FIELD = "email").
    admin = AdminUser.objects.create_user(
        email="ben@humara.io", password="pw-test-12345")
    admin.is_active = True
    admin.must_change_password = False
    admin.role = "super_admin"
    admin.is_staff = True
    admin.is_superuser = True
    admin.save()
    c = Client()
    check("super-admin account created and logged in",
          c.login(email="ben@humara.io", password="pw-test-12345"))

    # The insurer BI engine reads UNMANAGED mirror models, so their tables are
    # not created by migrations. Without them bi.py swallows every query and
    # the Reports pages render all-zero but still return 200 -- which would
    # make section 9's checks pass for the wrong reason. Build them.
    from django.apps import apps
    from django.db import connection
    mirrors = [mm for mm in apps.get_app_config("admin_portal").get_models()
               if not mm._meta.managed]
    for mm in mirrors:
        mm._meta.managed = True
    made, skipped = 0, []
    with connection.schema_editor() as se:
        for mm in mirrors:
            try:
                se.create_model(mm)
                made += 1
            except Exception as exc:
                # PlatformSession mirrors django_session, which Django's own
                # migrations have already created -- that is expected.
                if "already exists" not in str(exc):
                    skipped.append(f"{mm.__name__}: {type(exc).__name__}: {exc}")
    check("insurer BI mirror tables available for this test", not skipped,
          f"created {made}/{len(mirrors)}; unexpected failures={skipped}")

    # A BI log trap, so a silently-swallowed metric fails section 9 loudly.
    import logging as _logging

    class _Trap(_logging.Handler):
        def __init__(self):
            super().__init__(level=_logging.ERROR)
            self.records = []

        def emit(self, record):
            self.records.append(record)

    bi_trap = _Trap()
    _logging.getLogger("admin_portal.services.bi").addHandler(bi_trap)

    # ---------------------------------------------------------------
    section("1. APP ANALYTICS — bridge NOT configured (today's prod state)")
    ab.is_configured = lambda: False
    for url in ["/admin-portal/analytics/",
                "/admin-portal/analytics/consultants/",
                "/admin-portal/analytics/breeders/",
                "/admin-portal/analytics/users/f091f448-d6aa-45b0-af95-42d3e5952dc7/"]:
        r = c.get(url)
        check(f"{url} renders instead of erroring", r.status_code == 200, str(r.status_code))
        if r.status_code == 200:
            body = r.content.decode()
            check(f"{url} tells the admin the bridge is unconfigured",
                  "not configured" in body.lower() or "configure" in body.lower())

    # ---------------------------------------------------------------
    section("2. APP ANALYTICS — bridge configured, real data shapes")
    ab.is_configured = lambda: True
    calls = []

    def fake_overview(entity_type, period, start, end):
        calls.append(("overview", entity_type, period, start, end))
        return {"data": OVERVIEW_BY_TYPE[entity_type]}

    def fake_search(entity_type, query):
        calls.append(("search", entity_type, query))
        return SEARCH

    def fake_entity(entity_type, entity_id, period, start, end):
        calls.append(("entity", entity_type, entity_id, period, start, end))
        return ENTITY

    ab.fetch_overview, ab.search_entities, ab.fetch_entity = (
        fake_overview, fake_search, fake_entity)

    for et in ab.ENTITY_TYPES:
        calls.clear()
        r = c.get(f"/admin-portal/analytics/{et}/")
        check(f"'{et}' overview page returns 200", r.status_code == 200, str(r.status_code))
        body = r.content.decode()
        check(f"'{et}' overview asked the backend for the right entity type",
              any(x[0] == "overview" and x[1] == et for x in calls), str(calls))
        check(f"'{et}' page shows the entity label", ab.TYPE_LABELS[et] in body)
        missing = [mk for mk in MARKERS[et] if mk not in body]
        check(f"'{et}' page renders the backend's actual numbers", not missing,
              f"missing from page: {missing}")
        check(f"'{et}' page has no unrendered template tags",
              "{{" not in body and "{%" not in body)

    section("3. SEARCH BY NAME / EMAIL / ID")
    for q in ["ben@humara.io", "Rohit", "f091f448-d6aa-45b0-af95-42d3e5952dc7"]:
        calls.clear()
        r = c.get("/admin-portal/analytics/users/", {"q": q})
        check(f"search '{q[:28]}' returns 200", r.status_code == 200, str(r.status_code))
        check(f"search '{q[:28]}' reached the backend",
              any(x[0] == "search" and x[2] == q for x in calls), str(calls))
        body = r.content.decode()
        check(f"search '{q[:28]}' lists the matching accounts",
              "Rohit Sharma" in body and "rohitomegalol@gmail.com" in body)
        check(f"search '{q[:28]}' links through to the per-account page",
              "/analytics/users/f091f448-d6aa-45b0-af95-42d3e5952dc7/" in body)

    section("4. PER-ACCOUNT DRILL-DOWN")
    calls.clear()
    r = c.get("/admin-portal/analytics/users/f091f448-d6aa-45b0-af95-42d3e5952dc7/")
    check("entity page returns 200", r.status_code == 200, str(r.status_code))
    body = r.content.decode()
    check("entity page asked the backend for that exact account",
          any(x[0] == "entity" and x[2] == "f091f448-d6aa-45b0-af95-42d3e5952dc7"
              for x in calls), str(calls))
    check("entity page shows the account's name", "Rohit Sharma" in body)
    check("entity page shows the account's email", "rohitomegalol@gmail.com" in body)
    for label, mk in [("purchase spend", "61.98"), ("bookings", "Bookings"),
                      ("listings", "Listings"), ("showcases", "Showcases")]:
        check(f"entity page shows {label}", mk in body)
    check("entity page has no unrendered template tags",
          "{{" not in body and "{%" not in body)

    section("5. PERIOD SELECTION (1m / 3m / 6m / 12m / all)")
    for key, _label in ab.PERIODS:
        calls.clear()
        r = c.get("/admin-portal/analytics/users/", {"period": key})
        ok = r.status_code == 200 and any(x[0] == "overview" and x[2] == key for x in calls)
        check(f"period '{key}' is honoured and passed to the backend", ok,
              f"status={r.status_code} calls={calls}")

    section("6. BAD INPUT MUST NOT 500")
    for url in ["/admin-portal/analytics/not-a-type/",
                "/admin-portal/analytics/users/?period=bogus",
                "/admin-portal/analytics/users/?period=3m&start=not-a-date&end=xx",
                "/admin-portal/analytics/not-a-type/some-id/"]:
        r = c.get(url)
        check(f"{url} -> {r.status_code} (no server error)", r.status_code == 200,
              str(r.status_code))

    section("7. BACKEND FAILURE IS SHOWN, NOT CRASHED")
    def boom(*a, **k):
        raise ab.AnalyticsBackendError("backend returned 503")
    ab.fetch_overview = boom
    r = c.get("/admin-portal/analytics/users/")
    check("a backend outage still renders the page", r.status_code == 200, str(r.status_code))
    check("and surfaces the error to the admin", "503" in r.content.decode())
    ab.fetch_overview = fake_overview

    section("8. ACCESS CONTROL")
    anon = Client()
    for url in ["/admin-portal/analytics/", "/admin-portal/analytics/users/abc/",
                "/admin-portal/reports/", "/admin-portal/reports/providers/",
                "/admin-portal/reports/export/"]:
        r = anon.get(url)
        # Must be a redirect to the login page (not merely any redirect) or 403.
        denied = r.status_code == 403 or (
            r.status_code == 302 and "login" in r.get("Location", ""))
        check(f"anonymous is sent to login for {url}", denied,
              f"{r.status_code} -> {r.get('Location', '')}")

    section("9. INSURER REPORTS SECTION (bi.py) THROUGH THE REAL URLS")
    bi_trap.records.clear()
    r = c.get("/admin-portal/reports/")
    check("reports home returns 200", r.status_code == 200, str(r.status_code))
    body = r.content.decode()
    check("reports home has no unrendered template tags",
          "{{" not in body and "{%" not in body)
    check("reports home shows no degradation note",
          "could not be computed" not in body)

    from admin_portal.services import bi
    for key in bi.ORDER:
        r = c.get(f"/admin-portal/reports/{key}/")
        check(f"reports/{key}/ returns 200", r.status_code == 200, str(r.status_code))
        b = r.content.decode()
        check(f"reports/{key}/ shows no degradation note",
              "could not be computed" not in b)
        check(f"reports/{key}/ has no unrendered template tags",
              "{{" not in b and "{%" not in b)

    r = c.get("/admin-portal/reports/no-such-dashboard/")
    check("an unknown dashboard key does not 500", r.status_code in (200, 302, 404),
          str(r.status_code))

    check("the Reports pages swallowed NO metric failures", not bi_trap.records,
          "; ".join(sorted({x.getMessage() for x in bi_trap.records})))

    section("10. INSURER EXPORT ENDPOINT (CSV + PDF) THROUGH THE REAL URL")
    bi_trap.records.clear()
    for fmt, ctype, sig in [("csv", "text/csv", b"Aqua AI"), ("pdf", "application/pdf", b"%PDF-")]:
        r = c.post("/admin-portal/reports/export/", {
            "format": fmt, "dash_providers": "1", "dash_stores": "1",
            "dash_marketplace": "1", "dash_consultants": "1"})
        check(f"export {fmt.upper()} returns 200", r.status_code == 200, str(r.status_code))
        if r.status_code == 200:
            check(f"export {fmt.upper()} has content type {ctype}",
                  ctype in r["Content-Type"], r["Content-Type"])
            content = r.content
            check(f"export {fmt.upper()} body is a real {fmt.upper()}",
                  content[:200].find(sig) >= 0 or sig in content[:2048], str(content[:40]))
            check(f"export {fmt.upper()} is a download", "attachment" in
                  r.get("Content-Disposition", ""), r.get("Content-Disposition", "(none)"))

    r = c.post("/admin-portal/reports/export/", {"format": "csv"})
    check("export with no dashboards ticked still works (defaults to all)",
          r.status_code == 200, str(r.status_code))

    r = c.get("/admin-portal/reports/export/")
    check("GET on the export endpoint does not 500", r.status_code in (200, 302, 405),
          str(r.status_code))

    check("the export path swallowed NO metric failures", not bi_trap.records,
          "; ".join(sorted({x.getMessage() for x in bi_trap.records})))

    section("11. DAILY REPORTS ARE REACHABLE AGAIN (URL shadowing fix)")
    for url, want in [("/admin-portal/daily-reports/", "daily_report_list"),
                      ("/admin-portal/daily-reports/run-now/", "daily_report_run_now")]:
        from django.urls import resolve
        check(f"{url} routes to {want}", resolve(url).func.__name__ == want,
              resolve(url).func.__name__)
    r = c.get("/admin-portal/daily-reports/")
    check("daily reports list renders", r.status_code in (200, 302), str(r.status_code))

    print(f"\n{'=' * 78}\nRESULT: {len(PASS)} passed, {len(FAIL)} failed\n{'=' * 78}")
    for f in FAIL:
        print("  FAILED:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    from django.test.runner import DiscoverRunner

    setup_test_environment()
    runner = DiscoverRunner(verbosity=0, interactive=False)
    cfg = runner.setup_databases()
    try:
        code = main()
    finally:
        runner.teardown_databases(cfg)
        teardown_test_environment()
    sys.exit(code)
