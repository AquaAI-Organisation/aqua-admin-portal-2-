# Integration and Lockdown Guide

## 1. OpenAI and mailbox placement

Recommended production setup:

- keep the real OpenAI key in Supabase Edge Function secrets
- let the admin portal call the Edge Function for review / triage
- keep the Aqua AI mailbox on Google Workspace OAuth

Google Workspace OAuth variables for the control plane:

```env
GMAIL_CLIENT_ID=...
GMAIL_CLIENT_SECRET=...
GMAIL_REFRESH_TOKEN=...
GMAIL_SENDER=support@aquaai.uk
SUPPORT_ALIAS_EMAIL=support@aquaai.uk
PRIVACY_ALIAS_EMAIL=privacy@aquaai.uk
PROVIDERS_ALIAS_EMAIL=providers@aquaai.uk
```

Optional direct OpenAI fallback:

```env
OPENAI_API_KEY=sk-proj-your-real-key
OPENAI_MODEL=gpt-4o
```

The dashboard will show whether the OpenAI and Gmail paths are configured and authenticated.

## 2. What the control plane monitors

Signup review sources:

- `user_auth_user`
- `breeders_breederprofile`
- `consultant_consultantprofile`

Post-signup issue sources:

- `badges_incidentlog`
- `consultant_consultantwarning`

Safe automatic actions:

- approve account
- reject account
- verify account
- deactivate account pending review
- set safe verification levels
- set consultant warning status when the source warning model is available

## 3. Make the control plane the main entry point

In the main backend repo, `/admin/` should redirect to the control plane and the old Django admin should stay at a hidden internal path.

Environment variables used by the paired main backend change:

```env
CONTROL_PLANE_ADMIN_URL=https://admin-control.aquaai.uk/admin-portal/
CONTROL_PLANE_INTERNAL_ADMIN_PATH=django-internal-admin-8x7k/
```

The included local backend repo has already been updated to do this in `aquaai/urls.py`.

## 4. Control-plane environment variables

Keep these in the control-plane deployment environment:

```env
DATABASE_URL=postgres://...
SLACK_BOT_TOKEN=xoxb-...
SLACK_CHANNEL=#aqua-admin-alerts
GMAIL_CLIENT_ID=...
GMAIL_CLIENT_SECRET=...
GMAIL_REFRESH_TOKEN=...
GMAIL_SENDER=support@aquaai.uk
SUPPORT_ALIAS_EMAIL=support@aquaai.uk
PRIVACY_ALIAS_EMAIL=privacy@aquaai.uk
PROVIDERS_ALIAS_EMAIL=providers@aquaai.uk
SUPERADMIN_EMAILS=steven@humara.io,ben@humara.io
LEGACY_ADMIN_REDIRECT_URL=https://admin-control.aquaai.uk
LEGACY_ADMIN_INTERNAL_PATH=/django-internal-admin-8x7k/
```

Optional:

```env
OPENAI_API_KEY=sk-proj-your-real-key
OPENAI_MODEL=gpt-4o
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_HOST_USER=admin@humara.io
EMAIL_HOST_PASSWORD=your-app-password
DEFAULT_FROM_EMAIL=Aqua Admin <admin@humara.io>
```

## 5. Heroku deployment notes

- `Procfile` release now runs migrations and static collection
- After deploy, run `bootstrap_superadmins`
- Add a scheduler for:
  - `python manage.py process_pending_reviews --limit 25`
  - `python manage.py generate_daily_report`

## 6. Verification checklist

- Confirm Steven and Ben can sign in
- Confirm guest users cannot mutate data
- Confirm developer writes trigger email and Slack notifications
- Confirm `/admin/` redirects to the control plane
- Confirm the hidden internal admin path still loads the legacy Django admin
- Confirm the dashboard health cards are green for database, OpenAI, Slack, email, and legacy-admin routing
- Confirm Inbox splits messages correctly into General Support, Providers, and Privacy lanes
- Confirm privacy inbox emails create DSAR requests and send verification links
