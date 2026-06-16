# Aqua AI — Admin Control Plane: User Guide

A practical, end-to-end guide to using the Aqua AI admin portal (the "control
plane"). It explains how to log in, what every section does, the actions
available to each role, and the day-to-day operational workflows.

> **Audience:** Aqua AI operators, moderators, and DPO/privacy staff.
> **Scope:** the Django admin portal in `aqua-admin-portal-2-` (app `admin_portal`).
> All routes below are under the portal's mount point (shown as `/…`).

---

## 1. What the portal is

The admin control plane is the single operational door for running Aqua AI. It
**reads the shared platform database** (the same Postgres the mobile app and
backend use) through read-only "mirror" models, and **owns only its own
`admin_portal_*` tables** for governance data (admin users, reviews, issues,
DSARs, audit log, settings).

It does five jobs:

1. **Account review** — AI-assisted triage of new breeder/consultant signups.
2. **Post-signup monitoring** — flagged issues, incidents, and risk triage.
3. **Support inbox** — triage of email across General, Providers, and Privacy lanes.
4. **Privacy / DSAR** — GDPR/CCPA data-subject requests end to end (see the
   companion **DSAR Admin Guide**).
5. **Marketplace operations (Feature D)** — verifications, disputes, Stripe
   Connect monitoring, delivery toggles.

Plus reporting, team management, settings, and a full audit trail.

---

## 2. Roles & access

Every admin is an `AdminUser` with one of four roles. Access to each action is
enforced by view-level decorators.

| Role | Read | Operational writes (moderation) | Team / settings / audit | Review overrides |
|---|---|---|---|---|
| **Guest** | ✅ | ❌ | ❌ | ❌ |
| **Developer** | ✅ | ✅ (super-admins are notified) | ❌ | ❌ |
| **Admin** | ✅ | ✅ | ❌ | ❌ |
| **Super Admin** | ✅ | ✅ | ✅ | ✅ |

**Platform super-admins** are fixed by the `SUPERADMIN_EMAILS` environment
variable (currently `steven@humara.io`, `ben@humara.io`). Only they can manage
the team, change settings, view the audit log, override AI reviews, and approve
DSAR releases.

Permission tiers map to these decorators in the code:

- `@admin_required` — any active admin (incl. guests) — read access.
- `@write_access_required` — blocks guests; dev/admin writes notify super-admins.
- `@operational_admin_required` — admin or super-admin — moderation actions.
- `@super_admin_required` — platform super-admins only.

---

## 3. Logging in

1. Go to **`/login/`**.
2. Sign in with your **email + password** (`EmailLoginForm`).
3. **First login / after a reset:** if your account has `must_change_password`
   set, you're redirected to **`/change-password/`** before you can continue
   (new password ≥ 10 characters).
4. **Sign out** any time via **`/logout/`** (or the dock foot).

> **Invited?** Open the invite link you were sent (`/invite/accept/<token>/`),
> which creates your admin user and lets you set a password.
>
> **Google sign-in?** The Google OAuth screens under **Settings** are for
> connecting the **Gmail mailbox** used by the Support Inbox — they are *not*
> the admin login.

---

## 4. The interface & navigation

The portal uses a vertical icon **dock** on the left (hover for tooltips). What
you see depends on your role.

**Everyone:**
- **Dashboard** (`/`)
- **Pending Intake** (`/intake/`)
- **Account Directory** (`/entities/`)
- **Pending Reviews** (`/reviews/`)
- **Flagged Issues** (`/issues/`) — badge shows the open count
- **Feature D Ops** (`/feature-d/`)
- **Daily Reports** (`/reports/`)
- Dock foot: **Change password**, **Sign out**, your avatar

**Admin & Super-Admin also see:**
- **Inbox** (`/inbox/`)
- **Data Requests** (`/data-requests/`) — badge shows the open count

**Super-Admin only:**
- **Team Access** (`/team/`)
- **Audit Log** (`/audit/`)
- **Feature D Audit** (`/feature-d/audit/`)
- **Settings** (`/settings/`)

A light/dark theme toggle sits in the top bar.

---

## 5. Modules in detail

### 5.1 Dashboard — `/`
The landing overview. Shows review counts (approved / rejected / flagged /
pending / overrides), issue counts (open / critical / resolved), today vs the
7-day trend, the 8 most recent reviews and issues, recent daily reports, a
health snapshot (database, OpenAI, Slack, email, legacy-admin routing), and
pending breeder/consultant counts. Read-only.

### 5.2 Pending Intake — `/intake/`
The raw feed of **new signups awaiting a decision** (unverified breeders +
pending consultants), each annotated with its AI review state (`not_scanned`,
`pending`, `approved`, `rejected`, `flagged`, `error`).

- **Search** by company, email, username, or full name; **filter** by role and
  AI state.
- **Decide:** open an entity and choose an action — `approve`, `reject`, `flag`,
  or `verify` — via `/intake/<entity_type>/<entity_id>/<action>/`.

### 5.3 Account Directory — `/entities/`
Browse **all** breeders and consultants with status summaries (verified/active,
AI review state). Search and filter by role, verification, and activity.

- **Update status** (activate / deactivate) via
  `/entities/<entity_type>/<entity_id>/status/`.

### 5.4 AI Account Reviews — `/reviews/`
The central queue of AI-generated approve/reject/flag recommendations
(`AIAccountReview`). Filter by decision and subject type; search by email/name.

- **View detail** (`/reviews/<id>/`): profile, external user, risk analysis, flags.
- **Re-run** (`/reviews/<id>/re-run/`) — re-analyse with the latest data
  *(operational admin)*.
- **Override** (`/reviews/<id>/override/`) — force approve/reject with a reason
  *(super-admin)*.
- **Process now** (`/reviews/process-now/`) — trigger an immediate review batch
  *(super-admin)*.

### 5.5 Flagged Issues — `/issues/`
Post-signup risk triage (`AIFlaggedIssue`) sourced from incidents, consultant
warnings, message/booking/payment risk, trust drops, and support inquiries.
Filter by severity (critical / warning / info) and source; toggle resolved.

- **View detail** (`/issues/<id>/`): summary, rationale, evidence, recommendations.
- **Resolve** (`/issues/<id>/resolve/`) with notes *(operational admin)*.

### 5.6 AI Flags — `/flags/`
Individual warnings attached to a review (`AIFlag`) — e.g. "compliance concern".
Filter by severity; toggle resolved.

- **View detail** (`/flags/<id>/`): reason, recommended solution, applied solution.
- **Resolve** (`/flags/<id>/resolve/`) with resolution notes *(operational admin)*.

### 5.7 Support Inbox — `/inbox/`
Email triage across three lanes — **General Support, Providers, Privacy** —
pulled from the connected Google Workspace mailbox (`SupportInquiry`). Filter by
mailbox and status (new / triaged / actioned / replied / archived / error);
search sender, subject, body.

- **Refresh** (`/inbox/refresh/`) — fetch new mail, run AI triage, and
  **auto-create DSARs** from the privacy lane *(operational admin)*.
- **View detail** (`/inbox/<id>/`): full message, AI analysis, draft reply,
  and any linked DSAR.
- **Analyse** (`/inbox/<id>/analyse/`) — run OpenAI triage *(operational admin)*.
- **Apply action** (`/inbox/<id>/apply-action/`) — mark triaged/actioned, etc.
  *(operational admin)*.
- **Send reply** (`/inbox/<id>/reply/`) — respond via Gmail *(operational admin)*.

### 5.8 Data Requests (DSAR) — `/data-requests/`
The GDPR/CCPA privacy queue. **See the dedicated [DSAR Admin Guide](./DSAR_ADMIN_GUIDE.md)**
for the full workflow. In brief: requests arrive (often auto-created from the
Privacy inbox lane), the subject proves identity by logging in at aquaai.uk,
an admin **Prepares** the export, and a super-admin/DPO **Approves & sends** it.

### 5.9 Daily Reports — `/reports/`
Historical daily summaries (`DailyReport`): counts of approved/rejected/flagged/
pending, breeder/consultant splits, overrides, critical issues, plus delivery
status (email/Slack).

- **View detail** (`/reports/<id>/`): full report with linked reviews and issues.
- **Run now** (`/reports/run-now/`) — generate today's report *(super-admin)*.

### 5.10 Feature D — Marketplace Ops — `/feature-d/`
Moderation of marketplace transactions and sellers. Shows active/completed
reservations, the verification queue (expiring within 30 days), open disputes,
the Stripe Connect watchlist (restricted / pending / no-payouts / delivery
suspended), holiday-mode sellers, and recent activity.

- **Approve / reject verification** — `/feature-d/verifications/<id>/<decision>/`.
- **Resolve dispute** — `/feature-d/disputes/<id>/resolve/` (calls the backend API).
- **Toggle delivery** — `/feature-d/breeders/<seller_id>/delivery-toggle/`
  (suspend/enable a seller).
- **Feature D audit** — `/feature-d/audit/` *(super-admin)*.

> Feature D actions call the platform backend using `AQUAAI_BACKEND_API_URL`
> with the `AQUAAI_BACKEND_API_TOKEN` bearer token.

### 5.11 Settings — `/settings/` *(super-admin)*
Operational configuration (`OperationalSettings`): Gmail OAuth (client id/secret,
connected refresh-token status, sender), email aliases (support / privacy /
providers), Slack bot token + channel, and operational flags (e.g. DSAR
auto-send, auto-activate).

- **Google:** Connect (`/settings/google/connect/`) → callback → Disconnect.
- **Slack:** Test (`/settings/slack/test/`) / Disconnect.

### 5.12 Team Access — `/team/` *(super-admin)*
Manage control-plane admins (`AdminUser`, `AdminInvite`).

- **Invite** (`/team/invite/`) — emails an invite link.
- **Revoke / Activate / Remove** — `/team/<id>/revoke|activate|remove/`.
- **Change role** — `/team/<id>/role/` (guest ↔ admin ↔ developer).
- **Invites:** cancel / resend / copy link — `/team/invites/<id>/cancel|resend|link/`.

### 5.13 Audit Log — `/audit/` *(super-admin)*
The complete trail of admin reads and writes (`AdminAuditLog`): login/logout,
review re-runs/overrides, issue/flag resolutions, invites, DSAR actions, etc.
Filter by action and actor; each entry has a timestamp, IP, and summary.
Read-only.

---

## 6. Common workflows

**Approve a new provider signup**
1. **Pending Intake** → open the signup → review the AI assessment.
2. If clean, **approve** (or **verify**). If concerning, **flag** or **reject**.
3. Borderline AI call? Open it under **Reviews**, **Re-run**, or ask a
   super-admin to **Override**.

**Handle a flagged risk**
1. **Flagged Issues** → filter **critical** → open the issue.
2. Read the rationale/evidence → take the platform action → **Resolve** with notes.

**Answer a support email**
1. **Inbox** → **Refresh** → open the message.
2. **Analyse** for a triage + draft → edit → **Send reply**.
3. Privacy-lane mail auto-creates a DSAR — handle it under **Data Requests**.

**Add a teammate** *(super-admin)*
1. **Team Access** → **Invite** (set role) → they accept and set a password.
2. Adjust later with **Change role**; remove access with **Revoke/Remove**.

---

## 7. Setup & operations (for the deploying engineer)

**Run locally**
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in real values
python manage.py migrate         # only touches admin_portal_* tables
python manage.py bootstrap_superadmins --password "ChangeMeNow123!"
python manage.py runserver 0.0.0.0:8001
```

**Key environment variables**

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Shared platform Postgres |
| `SUPERADMIN_EMAILS` | Comma-separated platform super-admins |
| `PLATFORM_LOGIN_URL` | aquaai.uk login URL used for DSAR identity proof |
| `GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN/SENDER` | Support inbox (Gmail API) |
| `SUPPORT/PRIVACY/PROVIDERS_ALIAS_EMAIL` | Inbox lane routing |
| `SLACK_BOT_TOKEN`, `SLACK_CHANNEL` | Alert delivery |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | AI review/triage (Edge-function fallback exists) |
| `AQUAAI_BACKEND_API_URL`, `AQUAAI_BACKEND_API_TOKEN` | Feature D backend calls |

**Background jobs** (cron / worker — every few minutes):
```bash
python manage.py process_pending_reviews --limit 25
python manage.py poll_inbox            # fetch + triage mail, auto-create DSARs
python manage.py confirm_dsar_logins   # detect DSAR identity logins
python manage.py generate_daily_report
```
By default an in-process scheduler runs the refresh loop; for high volume set
`INBOX_AUTOREFRESH=false` and run `python manage.py run_automation` as a worker.

---

## 8. Key security & secret rotation

All sensitive credentials the platform uses — Supabase service/JWT keys, the
OpenAI key, the Django `SECRET_KEY`, the database URL, Stripe keys, etc. — are
**not** committed to git or hand-edited on each server. They live in a **central
secret store** and are **pulled into the running process at startup**, so a key
is defined in exactly one place and used everywhere from there.

### 8.1 How keys are stored & fetched (AWS Secrets Manager)

The backend (the EC2 application server) is wired to **AWS Secrets Manager**:

- All keys are stored as a **single JSON secret** named **`aqua_backend`** in
  region **`eu-west-2`** — a flat object, e.g.
  ```json
  {
    "SECRET_KEY": "…",
    "DATABASE_URL": "postgres://…",
    "SUPABASE_SERVICE_KEY": "…",
    "SUPABASE_JWT_SECRET": "…",
    "OPENAI_API_KEY": "sk-…",
    "STRIPE_SECRET_KEY": "…"
  }
  ```
- At boot, **before any setting is read**, `aquaai/settings.py` calls
  `load_aws_secrets()` (`core/secrets/aws_secrets.py`). It authenticates to AWS,
  calls `get_secret_value(SecretId="aqua_backend")`, parses the JSON, and
  **injects every key into the process environment** (`os.environ`). The rest of
  the app then reads them as normal environment variables.
- **Fail-safe:** if the fetch or JSON parse fails, the loader raises and the app
  **does not start** — it never boots with missing or partial secrets.

> **The two bootstrap credentials.** Secrets Manager itself is reached with
> `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (region `AWS_REGION`, secret name
> overridable via `AWS_SECRET_NAME`). These two **cannot** live inside the secret
> they unlock, so they remain on the host. **Best practice: attach an IAM
> instance role to the EC2 box** and drop the static keys entirely — boto3 then
> authenticates via the role automatically.

This is exactly the "one place, updated everywhere" model: change a value in
`aqua_backend` and every place that key is used picks it up on the next restart —
no editing files on each server.

### 8.2 Rotating / updating a key

1. **Update the value in AWS Secrets Manager** (one place only).
   - **Console:** Secrets Manager → **`aqua_backend`** (region `eu-west-2`) →
     *Retrieve secret value* → **Edit** → change the key → **Save**.
   - **CLI** — the secret is one JSON blob, so fetch it, edit the one key, and
     write the **whole** object back (don't submit a single key alone, or you'll
     overwrite the rest):
     ```bash
     aws secretsmanager get-secret-value --secret-id aqua_backend \
       --region eu-west-2 --query SecretString --output text > aqua_backend.json
     # edit the one value in aqua_backend.json, then:
     aws secretsmanager put-secret-value --secret-id aqua_backend \
       --region eu-west-2 --secret-string file://aqua_backend.json
     rm aqua_backend.json   # don't leave plaintext secrets on disk
     ```
2. **Restart / redeploy the service.** Secrets are loaded **once at process
   start**, so a rotation only takes effect after the app restarts (e.g. restart
   gunicorn on EC2, or trigger a redeploy). Nothing else to change.
3. **Verify** the app comes back up healthy (the backend won't boot if the new
   value is wrong — that's the fail-safe doing its job).

> **Rotating the AWS bootstrap keys themselves** is an **IAM** task (create a new
> access key, update the host/role, deactivate the old one) — not a Secrets
> Manager edit. If you use an IAM instance role (recommended) there are no static
> keys to rotate.

### 8.3 Security practices

- **Least privilege:** the app's IAM principal needs only
  `secretsmanager:GetSecretValue` on the `aqua_backend` secret ARN — nothing else.
- **Audit:** every fetch is logged by **AWS CloudTrail** (`GetSecretValue`), so
  access is traceable.
- **Never commit secrets:** no real keys in git or in a committed `.env`; in
  production the secret store is the only source. Host env vars / the secret
  override any `.env` file.
- **Public vs secret:** Supabase **anon/publishable** keys are *public by design*
  and may stay in the client app; only **service-role / JWT / private** keys
  belong in Secrets Manager.

### 8.4 Per-service status (important)

| Service | Host | Secret source today |
|---|---|---|
| **Backend API** | EC2 | ✅ **AWS Secrets Manager** (`aqua_backend`, `eu-west-2`) via `load_aws_secrets()` |
| **Admin portal** (this app) | Heroku | Boots via the central `secrets_loader` (Supabase-hosted manager); set its keys as Heroku Config Vars, or align it to AWS — see below |
| **Intelligence service** | Render | Central `secrets_loader` |

To put the **admin portal** on the same AWS Secrets Manager path as the backend,
mirror the backend's bootstrap: add a `load_aws_secrets()` call at the **top of
`aqua_admin/settings.py`** (before settings are read), give the host the AWS
bootstrap credential (or an IAM role with `GetSecretValue` on the portal's
secret), and store the portal's keys (`SECRET_KEY`, `DATABASE_URL`,
`SLACK_BOT_TOKEN`, `EMAIL_HOST_PASSWORD`, the Supabase service key, etc.) in a
secret (e.g. `aqua_admin`). After that, rotation for the portal follows §8.2
identically.

---

## 9. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Redirected to change-password on login | `must_change_password` set — set a new ≥10-char password. |
| Can't see Inbox / Data Requests | You're a **Guest** — needs Admin or Super-Admin. |
| Can't see Team / Settings / Audit | Super-admin only (`SUPERADMIN_EMAILS`). |
| Inbox empty / "not configured" | Connect Gmail under **Settings → Google**; check aliases. |
| "Approve & send" disabled on a DSAR | Subject hasn't logged in to confirm identity yet — see DSAR guide. |
| Feature D actions failing | Check `AQUAAI_BACKEND_API_URL`/token; see the health snapshot on the dashboard. |
| Backend won't boot after a key change | The secret fetch failed or a value is wrong — fail-safe is working. Check the `aqua_backend` secret JSON is valid and the host's AWS credential/role can `GetSecretValue` (§8). |
| Rotated a key but nothing changed | Secrets load **once at startup** — restart/redeploy the service so it re-fetches (§8.2). |

---

*Companion docs: [DSAR Admin Guide](./DSAR_ADMIN_GUIDE.md) ·
[DSAR login verification](./DSAR_LOGIN_VERIFICATION.md).*
