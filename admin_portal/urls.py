from django.urls import path

from . import views

app_name = "admin_portal"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("background-video/", views.background_video, name="background_video"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),

    # Raw pending intake from source-of-truth backend tables
    path("intake/", views.intake_list, name="intake_list"),
    path("entities/", views.entity_directory, name="entity_directory"),
    path("entities/<str:entity_type>/<str:entity_id>/status/", views.entity_status_update, name="entity_status_update"),
    path("inbox/", views.support_inbox_list, name="support_inbox_list"),
    path("inbox/refresh/", views.support_inbox_refresh, name="support_inbox_refresh"),
    path("inbox/<int:inquiry_id>/", views.support_inbox_detail, name="support_inbox_detail"),
    path("inbox/<int:inquiry_id>/analyse/", views.support_inbox_analyse, name="support_inbox_analyse"),
    path("inbox/<int:inquiry_id>/apply-action/", views.support_inbox_apply_action, name="support_inbox_apply_action"),
    path("inbox/<int:inquiry_id>/reply/", views.support_inbox_send_reply, name="support_inbox_send_reply"),
    path("settings/", views.operational_settings_view, name="operational_settings"),

    # AI reviews
    path("reviews/", views.review_list, name="review_list"),
    path("reviews/<uuid:review_id>/", views.review_detail, name="review_detail"),
    path("reviews/<uuid:review_id>/re-run/", views.review_rerun, name="review_rerun"),
    path("reviews/<uuid:review_id>/override/", views.review_override, name="review_override"),
    path("reviews/process-now/", views.process_now, name="process_now"),

    # Flagged issues (post-signup monitoring)
    path("issues/", views.issue_list, name="issue_list"),
    path("issues/<uuid:issue_id>/", views.issue_detail, name="issue_detail"),
    path("issues/<uuid:issue_id>/resolve/", views.issue_resolve, name="issue_resolve"),

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
    path("team/<int:user_id>/activate/", views.admin_user_activate, name="admin_user_activate"),
    path("team/<int:user_id>/remove/", views.admin_user_remove, name="admin_user_remove"),
    path("team/invites/<int:invite_id>/cancel/", views.invite_cancel, name="invite_cancel"),
    path("team/invites/<int:invite_id>/resend/", views.invite_resend, name="invite_resend"),
    path("team/invites/<int:invite_id>/link/", views.invite_link, name="invite_link"),
    path("invite/accept/<str:token>/", views.invite_accept, name="invite_accept"),

    # Password
    path("change-password/", views.change_password, name="change_password"),

    # Role management (super admins only)
    path("team/<int:user_id>/role/", views.change_user_role, name="change_user_role"),

    # API endpoints for dashboard charts
    path("api/review-stats/", views.api_review_stats, name="api_review_stats"),
]
