# Aqua AI — Admin Control Plane

A separate Django project that replaces the ad-hoc `api.aquaai.uk/admin/` workflow with an **AI-driven approval control plane** for Breeder and Consultant signups on the main Aqua AI backend.

This project **does not replace** the existing Django admin on the main API — the developer can keep using that. It adds a second, more controlled UI where:

1. New breeder / consultant profiles are auto-reviewed by GPT‑4 and approved or rejected on verifiable grounds.
2. When the AI flags an issue, **Steven@humara.io** and **Ben@humara.io** are notified via **email + Slack** (both emails are registered on Slack) along with the recommended solutions which the AI also applies.
3. A daily analytics report summarises approvals / rejections with full drill-down into the AI's reasoning for every decision.
4. Only **Steven@humara.io** and **Ben@humara.io** are super admins. They are the only users who can invite or remove other admin accounts.
5. Super admins can **manually override** any AI decision with a documented reason — the override is applied to the external account and all stakeholders are notified.

The legacy `/admin/` continues to work unchanged.

## Key Features

### AI-Powered Automated Review
- GPT-4 analyses each new breeder/consultant signup across 5 dimensions: identity clarity, business legitimacy, documentation, role fit, and risk signals
- Automatic approval (confidence ≥ 0.80), rejection (confidence ≤ 0.30), or flagging for human review
- Every decision includes detailed rationale, evidence bullets, and dimension scores

### Intelligent Notifications
- Email + Slack alerts for every flagged issue, sent to super-admins
- Daily summary reports delivered via both channels
- Override notifications when a super-admin manually changes a decision

### Manual Override System
- Super-admins can override any AI decision (approve/reject) with documented reasoning
- Overrides are applied immediately to the external account
- Full audit trail of who overrode what and why

### Team Access Control
- Only Steven and Ben can invite/revoke admin accounts
- Invite-based onboarding with email verification
- Complete audit log of all actions

### Analytics Dashboard
- Real-time metrics: total, approved, rejected, flagged, pending, overrides
- 7-day trend chart
- Daily drill-down reports with per-account reasoning

## How it talks to the main backend

Both projects share the same Postgres database (pointed at by `DATABASE_URL`). The control plane exposes its own auth / UI tables (prefixed `admin_portal_*`) and reaches into the main app's tables via **unmanaged mirror models** — so migrations from this project will never touch `user_auth_*`, `breeders_*` or `consultant_*`.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # then fill in the values
python manage.py migrate
python manage.py bootstrap_superadmins
python manage.py runserver 0.0.0.0:8001
```

Then browse to `http://localhost:8001/admin-portal/` and log in with the password you set for `steven@humara.io` / `ben@humara.io` during bootstrap.

## Scheduled jobs

Run these on a cron / celery-beat / systemd timer:

| Job | Frequency | Command |
|---|---|---|
| Scan main DB for new profiles and run AI review | every 5 min | `python manage.py process_pending_reviews` |
| Build the end-of-day analytics summary | daily 23:55 UTC | `python manage.py generate_daily_report` |

## Environment variables

See `.env.example` for the full list. The ones you must set:

- `DATABASE_URL` — same Postgres used by the main API.
- `OPENAI_API_KEY` — your GPT‑4 key. Leave the placeholder in `.env.example` alone until you have the real key.
- `SLACK_BOT_TOKEN` and `SLACK_CHANNEL` — for AI issue alerts.
- `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` — for email alerts.
- `SUPERADMIN_EMAILS` — comma-separated list; defaults to `steven@humara.io,ben@humara.io`.

## Migration / go-live plan from current admin

1. Keep the legacy admin at `http://api.aquaai.uk/admin/` available during rollout.
2. Deploy this project as a separate service (example URL: `https://admin-control.aquaai.uk/admin-portal/`).
3. Point `DATABASE_URL` to the same production Postgres used by the main backend.
4. Run:
   - `python manage.py migrate`
   - `python manage.py bootstrap_superadmins --password '<temporary-strong-password>'`
5. Set production secrets:
   - `OPENAI_API_KEY=<your key>`
   - `SLACK_BOT_TOKEN=<bot token>` and `SLACK_CHANNEL=<channel>`
   - SMTP settings (`EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, etc.)
6. Add schedules:
   - `python manage.py process_pending_reviews` every 5 minutes
   - `python manage.py generate_daily_report` daily at `23:55 UTC`
7. Validate with one breeder + one consultant test signup before enforcing full automation.
8. After validation, instruct ops/developers to use only the new control-plane UI for approvals.
