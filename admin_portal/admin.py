from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import (
    AdminAuditLog,
    AdminInvite,
    AdminUser,
    AIAccountReview,
    AIFlag,
    DailyReport,
)


@admin.register(AdminUser)
class AdminUserAdmin(DjangoUserAdmin):
    ordering = ("email",)
    list_display = ("email", "full_name", "is_platform_super_admin", "is_active", "last_login")
    search_fields = ("email", "full_name")
    fieldsets = (
        (None, {"fields": ("email", "password", "full_name")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "is_platform_super_admin", "groups", "user_permissions")}),
        ("Meta", {"fields": ("invited_by", "last_login", "created_at")}),
    )
    readonly_fields = ("created_at",)
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("email", "full_name", "password1", "password2", "is_platform_super_admin")}),
    )


admin.site.register(AdminInvite)
admin.site.register(AIAccountReview)
admin.site.register(AIFlag)
admin.site.register(DailyReport)
admin.site.register(AdminAuditLog)
