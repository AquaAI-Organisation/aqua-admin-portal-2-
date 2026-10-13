# Admin portal reporting/analytics regression suites

Two self-contained end-to-end suites. Neither needs network access, and neither
touches the production database. Run them before any release that touches
`admin_portal/services/bi.py`, the Reports section, or the App Analytics
section.

```bash
python3 tests/test_insurer_bi_e2e.py        # 75 checks
python3 tests/test_portal_analytics_e2e.py  # 93 checks
```

## `test_insurer_bi_e2e.py` — the insurer exportable MI/BI

Builds a database from the **real unmanaged mirror models**, seeds
representative data across a 12-month window, then runs all four dashboards
(`providers`, `stores`, `marketplace`, `consultants`) and both exporters.

The important part: `bi.py`'s helpers (`_sum`, `_count`, `_monthly_series`,
`_status_rows`) each swallow their own exception and return `0`/`[]` **without**
setting `Dashboard.note`. A broken metric therefore reads as a legitimate zero
and the export still succeeds — silent rot. So the suite attaches a log handler
to `admin_portal.services.bi` and **fails on any logged error**, as well as
asserting that every KPI is non-zero, every chart has a non-zero point and
every table has rows. Checking `note` alone is not enough.

It also captures the PostgreSQL the engine emits (compiled against a
never-connected postgres backend) and writes it to `bi_pg_queries.json` in a
temp dir, so the statements can be replayed against the real database when
diagnosing a production reporting problem.

## `test_portal_analytics_e2e.py` — App Analytics + Reports, through the real URLs

Real URLs, real decorators, real session auth, real templates; only the backend
bridge is stubbed. Covers all three entity types, search by name/email/ID, the
per-account drill-down, all five periods, bad input, backend outage, access
control, the four insurer dashboards, and the CSV + PDF export endpoints.

Note it disables `SECURE_SSL_REDIRECT` and the secure-cookie flags for the test
client. The portal runs with `DEBUG=False`, which turns those on; over plain
http every URL would answer `301` before the view ran and every assertion would
pass vacuously.

## Known environment requirement

Three portal sections are inert without `AQUAAI_BACKEND_API_URL` and
`AQUAAI_BACKEND_API_TOKEN` (Feature D, Content Moderation, App Analytics). They
render a "Backend bridge not configured" notice rather than failing. See
`.env.example`.
