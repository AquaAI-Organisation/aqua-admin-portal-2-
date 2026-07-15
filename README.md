# Aqua AI Admin Control Plane

This Django project is the hardened admin control plane for Aqua AI. It runs as a separate service on the same Postgres database as the main backend and is designed to become the primary door for admin operations while leaving the legacy Django admin available at a hidden internal path during transition.

## What it does

- Automates breeder and consultant signup review with OpenAI
- Auto-applies safe actions only: approve, reject, verify, deactivate, and safe verification-level updates
- Triages post-signup incidents and consultant warnings into a dedicated Flagged Issues queue
- Sends email and Slack alerts to `steven@humara.io` and `ben@humara.io`
- Pulls the Aqua AI mailbox through Google Workspace OAuth and separates messages into General Support, Providers, and Privacy inbox lanes
- Converts privacy mailbox DSAR requests into verified data-request cases with export preparation and DPO approval
- Produces daily drill-down reports for review decisions and issue triage
- Restricts admin access so only Steven and Ben can invite, revoke, or change other control-plane users

## Main areas in the UI

- `Dashboard`
- `Inbox`
- `Pending Reviews`
- `Flagged Issues`
- `Daily Reports`
- `Data Requests`
- `Team Access`

## Shared-database approach

This project owns only the `admin_portal_*` tables. It reads the main backend through unmanaged mirror models, including:

- `user_auth_user`
- `breeders_breederprofile`
- `consultant_consultantprofile`
- `badges_incidentlog`
- `consultant_consultantwarning`

## Environment variables

Copy `.env.example` to `.env` and fill in real values.

Required:

- `DATABASE_URL`
- `SUPERADMIN_EMAILS`
- `SLACK_BOT_TOKEN`
- `SLACK_CHANNEL`
- `GMAIL_CLIENT_ID`
- `GMAIL_CLIENT_SECRET`
- `GMAIL_REFRESH_TOKEN`
- `GMAIL_SENDER`
- `SUPPORT_ALIAS_EMAIL`
- `PRIVACY_ALIAS_EMAIL`
- `PROVIDERS_ALIAS_EMAIL`
- `LEGACY_ADMIN_REDIRECT_URL`
- `LEGACY_ADMIN_INTERNAL_PATH`

## Local setup

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py bootstrap_superadmins --password "ChangeMeNow123!"
python manage.py runserver 0.0.0.0:8001
```

Open `http://localhost:8001/admin-portal/`.

## Scheduled jobs

Run these on a scheduler:

- `python manage.py process_pending_reviews --limit 25`
- `python manage.py generate_daily_report`
- `python manage.py poll_inbox` — fetches the mailbox, auto-runs AI triage on new messages, and for privacy-lane emails auto-creates DSAR requests and sends the requester a verification link. Run every ~5–10 minutes.
- `python manage.py confirm_dsar_logins` — detects when a DSAR requester has logged in at aquaai.uk, marks the request login-confirmed, and (if auto-delivery is on) emails their data PDF automatically. Run every ~5–10 minutes.

Together, `poll_inbox` + `confirm_dsar_logins` make the privacy/DSAR pipeline fully hands-off: intake → AI analysis → identity verification email → login confirmation → PDF delivery.

The review command now processes both new account signups and new incident/warning triage. Inbox refresh can also create DSAR requests from the privacy mailbox automatically.

### Running the automation

**Default — built into the web app (works on any host):** the web process runs an
in-process scheduler that refreshes the mailbox, processes DSARs, and reviews new
breeder/consultant signups (auto-approving them when the operational toggle is on)
every `INBOX_AUTOREFRESH_INTERVAL` seconds (default 120). Nothing extra to run —
just deploy the web app. It is safe with multiple web workers/instances (a
PostgreSQL advisory lock ensures only one runs each cycle). This works identically
on Heroku, a VPS, Docker, Render, etc. Disable with `INBOX_AUTOREFRESH=false`.

**Optional — dedicated worker/scheduler (for higher scale):** instead of the
in-process scheduler you can run the loop in its own process:

- **Heroku worker dyno:** the `Procfile` defines a `worker` running `run_automation`. Enable with `heroku ps:scale worker=1` (and set `INBOX_AUTOREFRESH=false` on the web dyno).
- **Heroku Scheduler:** add `python manage.py poll_inbox` and `python manage.py confirm_dsar_logins` every 10 minutes.
- **Any other host (systemd, Docker, Render, etc.):** run `python manage.py run_automation` as a long-lived process (see "Deploying off Heroku").

Inbox refresh and login confirmation also still happen when an admin opens the
inbox / a data request, and via the **Refresh inbox** and **Re-check login**
buttons.

### Deploying off Heroku

The app is a standard Django/gunicorn app, so it runs anywhere. The automation
needs no Heroku-specific pieces:

- **In-process (recommended):** keep `INBOX_AUTOREFRESH=true` (the default). As
  long as the web app is running under gunicorn/uvicorn, the mailbox
  auto-refreshes and new signups are reviewed/auto-approved — no cron, worker, or
  scheduler required.
- **Dedicated process (optional):** run `python manage.py run_automation`
  supervised by whatever your host uses (a systemd service, a second Docker/
  Compose service, a Render/Railway background worker, a Kubernetes Deployment),
  and set `INBOX_AUTOREFRESH=false` on the web app.

Run the web app with e.g. `gunicorn aqua_admin.wsgi --bind 0.0.0.0:$PORT --workers 3`.
Avoid gunicorn `--preload` if you rely on the in-process scheduler (start it
post-fork so each worker hosts it).

## Main backend redirect

The main backend repo should redirect `/admin/` to this control plane and keep legacy Django admin at a hidden internal path. In the paired backend repo included locally, this is now wired through:

- `CONTROL_PLANE_ADMIN_URL`
- `CONTROL_PLANE_INTERNAL_ADMIN_PATH`

## Heroku notes

- `Procfile` release now runs `migrate` and `collectstatic`
- The dashboard exposes runtime health for database, OpenAI, Slack, email, and legacy-admin redirect configuration
- Keep secrets in environment variables only

## Security note

Any keys or passwords previously pasted into prompts or local files should be rotated before production rollout.
