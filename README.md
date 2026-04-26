# Aqua AI Admin Control Plane

This Django project is the hardened admin control plane for Aqua AI. It runs as a separate service on the same Postgres database as the main backend and is designed to become the primary door for admin operations while leaving the legacy Django admin available at a hidden internal path during transition.

## What it does

- Automates breeder and consultant signup review with OpenAI
- Auto-applies safe actions only: approve, reject, verify, deactivate, and safe verification-level updates
- Triages post-signup incidents and consultant warnings into a dedicated Flagged Issues queue
- Sends email and Slack alerts to `steven@humara.io` and `ben@humara.io`
- Produces daily drill-down reports for review decisions and issue triage
- Restricts admin access so only Steven and Ben can invite, revoke, or change other control-plane users

## Main areas in the UI

- `Dashboard`
- `Pending Reviews`
- `Flagged Issues`
- `Daily Reports`
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
- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `SUPERADMIN_EMAILS`
- `SLACK_BOT_TOKEN`
- `SLACK_CHANNEL`
- `EMAIL_HOST`
- `EMAIL_PORT`
- `EMAIL_USE_TLS`
- `EMAIL_HOST_USER`
- `EMAIL_HOST_PASSWORD`
- `DEFAULT_FROM_EMAIL`
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

The review command now processes both new account signups and new incident/warning triage.

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
