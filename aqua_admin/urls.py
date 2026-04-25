from django.contrib import admin
from django.urls import include, path
from django.shortcuts import redirect

urlpatterns = [
    path("", lambda r: redirect("admin_portal:dashboard"), name="root"),
    path("admin-portal/", include("admin_portal.urls", namespace="admin_portal")),
    path("django-admin/", admin.site.urls),
]
