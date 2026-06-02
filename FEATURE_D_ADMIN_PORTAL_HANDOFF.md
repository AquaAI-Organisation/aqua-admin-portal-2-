# Feature D Admin Portal Handoff

## Purpose

This admin repo does not own the Feature D commerce data. It acts as a moderation and operations surface over the backend APIs.

This pass adds:

- Feature D dashboard page
- Feature D audit page
- backend bridge service for moderation actions
- navigation links in the admin shell

## New Environment Variables

Add these to the admin portal environment:

- `AQUAAI_BACKEND_API_URL`
  - example: `https://backend.example.com`
- `AQUAAI_BACKEND_API_TOKEN`
  - bearer token for a backend account allowed to call the Feature D admin APIs

These are consumed in:

- [aqua_admin/settings.py](</C:/Users/bibe/Downloads/aqua-admin-portal%20(2)/aqua_admin/settings.py>)
- [admin_portal/services/feature_d_backend.py](</C:/Users/bibe/Downloads/aqua-admin-portal%20(2)/admin_portal/services/feature_d_backend.py>)

## New Admin Pages

- `/feature-d/`
- `/feature-d/audit/`

Implemented in:

- [admin_portal/views.py](</C:/Users/bibe/Downloads/aqua-admin-portal%20(2)/admin_portal/views.py>)
- [admin_portal/urls.py](</C:/Users/bibe/Downloads/aqua-admin-portal%20(2)/admin_portal/urls.py>)
- [admin_portal/templates/admin_portal/feature_d_dashboard.html](</C:/Users/bibe/Downloads/aqua-admin-portal%20(2)/admin_portal/templates/admin_portal/feature_d_dashboard.html>)
- [admin_portal/templates/admin_portal/feature_d_audit.html](</C:/Users/bibe/Downloads/aqua-admin-portal%20(2)/admin_portal/templates/admin_portal/feature_d_audit.html>)

## What The Admin Portal Calls

The bridge service calls these backend endpoints:

- `GET /api/v1/marketplace/admin/reservations/dashboard/`
- `POST /api/v1/marketplace/admin/verifications/{verification_id}/review/`
- `POST /api/v1/marketplace/admin/disputes/{dispute_id}/resolve/`
- `POST /api/v1/marketplace/admin/breeders/{seller_id}/delivery-toggle/`

## What The Dashboard Shows

- active reservations
- completed reservations
- open dispute count
- licence verification queue
- reservation dispute queue
- Stripe Connect account monitor
- delivery enabled / locked state

## Verification Completed

Command:

```powershell
python manage.py check
```

Result:

- system check passed

## Integration Notes

- this repo does not migrate or own the Feature D tables
- it reads shared mirror models for visibility and uses the backend API bridge for actions
- if the backend token is missing or invalid, the Feature D pages will render an explicit configuration error
