from django.contrib import admin
from django.urls import include, path
from django.shortcuts import redirect

urlpatterns = [
    path("", lambda r: redirect("admin_portal:dashboard"), name="root"),
    path("admin-portal/", include("admin_portal.urls", namespace="admin_portal")),
    # Signed CRM -> Admin webhook receiver. Mounted at the project root because
    # the contract fixes the absolute path, and it authenticates by signature
    # rather than by session, so it sits outside the portal's login prefix.
    path("", include("admin_portal.integrations.urls", namespace="v2_integrations")),
    path("django-admin/", admin.site.urls),
]
