from django.urls import path

from . import views

app_name = "admin_portal"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),

    # AI reviews
    path("reviews/", views.review_list, name="review_list"),
    path("reviews/<uuid:review_id>/", views.review_detail, name="review_detail"),
    path("reviews/<uuid:review_id>/re-run/", views.review_rerun, name="review_rerun"),
    path("reviews/<uuid:review_id>/override/", views.review_override, name="review_override"),
    path("reviews/process-now/", views.process_now, name="process_now"),

    # Flags
    path("flags/", views.flag_list, name="flag_list"),
    path("flags/<int:flag_id>/", views.flag_detail, name="flag_detail"),
    path("flags/<int:flag_id>/resolve/", views.flag_resolve, name="flag_resolve"),

    # Daily reports
    path("reports/", views.daily_report_list, name="daily_report_list"),
    path("reports/<int:report_id>/", views.daily_report_detail, name="daily_report_detail"),
    path("reports/run-now/", views.daily_report_run_now, name="daily_report_run_now"),

    # Audit log
    path("audit/", views.audit_log, name="audit_log"),

    # Admin user management (super admins only)
    path("team/", views.admin_user_list, name="admin_user_list"),
    path("team/invite/", views.admin_user_invite, name="admin_user_invite"),
    path("team/<int:user_id>/revoke/", views.admin_user_revoke, name="admin_user_revoke"),
    path("team/invites/<int:invite_id>/cancel/", views.invite_cancel, name="invite_cancel"),
    path("invite/accept/<str:token>/", views.invite_accept, name="invite_accept"),

    # Password
    path("change-password/", views.change_password, name="change_password"),

    # Role management (super admins only)
    path("team/<int:user_id>/role/", views.change_user_role, name="change_user_role"),

    # API endpoints for dashboard charts
    path("api/review-stats/", views.api_review_stats, name="api_review_stats"),
]
