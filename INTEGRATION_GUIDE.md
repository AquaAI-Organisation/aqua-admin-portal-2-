# Integration and Lockdown Guide

## 1. OpenAI key placement

Set the key in the root `.env` file for this project:

```env
OPENAI_API_KEY=sk-proj-your-real-key
OPENAI_MODEL=gpt-4o
```

The dashboard will show whether the key is missing or still using a placeholder.

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
OPENAI_API_KEY=sk-proj-your-real-key
OPENAI_MODEL=gpt-4o
SLACK_BOT_TOKEN=xoxb-...
SLACK_CHANNEL=#aqua-admin-alerts
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_HOST_USER=admin@humara.io
EMAIL_HOST_PASSWORD=your-app-password
DEFAULT_FROM_EMAIL=Aqua Admin <admin@humara.io>
SUPERADMIN_EMAILS=steven@humara.io,ben@humara.io
LEGACY_ADMIN_REDIRECT_URL=https://admin-control.aquaai.uk
LEGACY_ADMIN_INTERNAL_PATH=/django-internal-admin-8x7k/
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
