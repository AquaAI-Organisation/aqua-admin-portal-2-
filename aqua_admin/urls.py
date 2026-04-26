from django.contrib import admin
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import include, path


def healthz(_request):
    return HttpResponse("ok", content_type="text/plain")


urlpatterns = [
    path("", lambda r: redirect("admin_portal:dashboard"), name="root"),
    path("healthz/", healthz, name="healthz"),
    path("admin-portal/", include("admin_portal.urls", namespace="admin_portal")),
    path("django-admin/", admin.site.urls),
]
