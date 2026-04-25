# Integration & Lockdown Guide

## 1. Where to place your OpenAI Key

Open (or create) the `.env` file in the root of this project (same folder as `manage.py`):

```bash
cp .env.example .env
```

Then edit `.env` and replace the placeholder:

```
OPENAI_API_KEY=sk-proj-YOUR-ACTUAL-KEY-HERE
```

That's it. The AI engine reads this on every review scan.


## 2. What Tables the AI Monitors

The AI monitors these tables from your **main backend** database (`backend_aqua_ai-1-s3_optimized`):

| Mirror Model in Control Plane        | Actual Table in Main DB              | What it checks                                    |
|---------------------------------------|--------------------------------------|---------------------------------------------------|
| `ExternalUser`                        | `user_auth_user`                     | email, name, phone, is_verified, trust_score, is_at_risk, verification_documents |
| `ExternalBreederProfile`              | `breeders_breederprofile`            | company, bio, website, lineage, mortality/disease rates, verification_level |
| `ExternalConsultantProfile`           | `consultant_consultantprofile`       | company, bio, credentials, specializations, admin_status |

**Discovery logic:**
- **Breeders:** Finds rows where `is_active=True` AND `is_verified=False` (new signups not yet verified)
- **Consultants:** Finds rows where `is_active=True` AND `admin_status != 'approved'` (pending approval)

When the AI approves an account, it sets `is_verified=True` and `verified_at=now()` on both the user and profile.
When it rejects, it sets `is_active=False` (and `admin_status='rejected'` for consultants).


## 3. How to Lock Down the Old Django Admin

This control plane is a **separate Django project** that connects to the **same database**. 
It does NOT replace the old admin — it runs alongside it. Here's how to restrict the old one:

### Option A: Password-protect the old admin URL (recommended for now)

In your **main backend** (`backend_aqua_ai-1-s3_optimized`), edit the main `urls.py`:

```python
# In your main backend's urls.py (NOT this project)
from django.contrib import admin
from django.urls import path, include
from django.http import HttpResponseRedirect

# Redirect /admin/ to the new control plane
def admin_redirect(request):
    return HttpResponseRedirect("https://admin-control.aquaai.uk/admin-portal/")

urlpatterns = [
    # Redirect the old admin to the new control plane
    path("admin/", admin_redirect),
    
    # Keep the old admin at a hidden URL only the developer knows
    path("django-internal-admin-8x7k/", admin.site.urls),
    
    # ... rest of your URLs
]
```

This way:
- Anyone going to `http://api.aquaai.uk/admin/` gets redirected to your new control plane
- The developer can still access Django admin at a secret URL if needed during transition
- After full migration, remove the secret URL entirely

### Option B: Restrict old admin by IP (Nginx)

If using Nginx in front of the main backend, add to your server block:

```nginx
location /admin/ {
    # Redirect to new control plane
    return 301 https://admin-control.aquaai.uk/admin-portal/;
}
```

### Option C: Disable old admin entirely (after full migration)

In your main backend's `settings.py`, remove `django.contrib.admin` from `INSTALLED_APPS` 
and remove the admin URL pattern. Only do this once you're 100% on the new control plane.


## 4. Deployment Architecture

```
┌─────────────────────────────────────────────┐
│                SHARED POSTGRES               │
│  (same DATABASE_URL for both projects)       │
│                                              │
│  Tables owned by main backend:               │
│  - user_auth_user                            │
│  - breeders_breederprofile                   │
│  - consultant_consultantprofile              │
│  - ... all other main app tables             │
│                                              │
│  Tables owned by control plane:              │
│  - admin_portal_adminuser                    │
│  - admin_portal_aiaccountreview              │
│  - admin_portal_aiflag                       │
│  - admin_portal_dailyreport                  │
│  - admin_portal_adminauditlog                │
│  - admin_portal_admininvite                  │
└──────────────┬──────────────┬────────────────┘
               │              │
    ┌──────────┴──┐    ┌──────┴──────────┐
    │ Main Backend│    │ Control Plane   │
    │ api.aquaai  │    │ admin.aquaai    │
    │ .uk         │    │ .uk             │
    │ Port: 8000  │    │ Port: 8001      │
    │             │    │                 │
    │ Existing    │    │ NEW: AI-driven  │
    │ Django app  │    │ review + UI     │
    └─────────────┘    └─────────────────┘
```

Both point at the same Postgres. The control plane only creates its own `admin_portal_*` tables.


## 5. Role System

| Role         | Can View | Can Edit | Notifications | Who assigns |
|-------------|----------|----------|---------------|-------------|
| Guest       | ✓        | ✗        | —             | Steven/Ben  |
| Developer   | ✓        | ✓        | Every write action notifies Steven & Ben via email+Slack | Steven/Ben |
| Super Admin | ✓        | ✓        | Full control  | Hardcoded: steven@humara.io, ben@humara.io only |


## 6. Quick Start Commands

```bash
# 1. Setup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

# 2. Edit .env with your real values:
#    DATABASE_URL=postgres://user:pass@host:5432/aquaai?sslmode=require
#    OPENAI_API_KEY=sk-proj-your-key-here
#    SLACK_BOT_TOKEN=xoxb-your-token
#    EMAIL_HOST_USER=admin@humara.io
#    EMAIL_HOST_PASSWORD=your-app-password

# 3. Run migrations (only creates admin_portal_* tables, never touches main backend tables)
python manage.py migrate

# 4. Create super-admin accounts
python manage.py bootstrap_superadmins --password 'YourSecurePassword123!'

# 5. Start the server
python manage.py runserver 0.0.0.0:8001

# 6. Set up cron jobs:
# Every 5 minutes:  python manage.py process_pending_reviews
# Daily at 23:55:   python manage.py generate_daily_report
```
