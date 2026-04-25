"""Initial migration for the admin_portal control plane.

This migration ONLY creates the local `admin_portal_*` tables. The
ExternalUser, ExternalConsultantProfile, ExternalBreederProfile mirror models
have `managed = False`, so Django will not emit DDL for them — the schema is
owned by the main backend project.
"""
import uuid

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.CreateModel(
            name="AdminUser",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("password", models.CharField(max_length=128, verbose_name="password")),
                ("last_login", models.DateTimeField(blank=True, null=True, verbose_name="last login")),
                ("is_superuser", models.BooleanField(default=False, help_text="Designates that this user has all permissions without explicitly assigning them.", verbose_name="superuser status")),
                ("email", models.EmailField(max_length=254, unique=True)),
                ("full_name", models.CharField(blank=True, max_length=200)),
                ("is_active", models.BooleanField(default=True)),
                ("is_staff", models.BooleanField(default=True)),
                ("is_platform_super_admin", models.BooleanField(default=False, help_text="Total control over the control plane. Only steven@humara.io and ben@humara.io should ever have this flag set.")),
                ("role", models.CharField(max_length=20, choices=[("super_admin", "Super Admin"), ("developer", "Developer"), ("guest", "Guest")], default="guest")),
                ("password_changed_at", models.DateTimeField(blank=True, null=True)),
                ("must_change_password", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("groups", models.ManyToManyField(blank=True, related_name="user_set", related_query_name="user", to="auth.group", verbose_name="groups")),
                ("invited_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="invited_admins", to=settings.AUTH_USER_MODEL)),
                ("user_permissions", models.ManyToManyField(blank=True, related_name="user_set", related_query_name="user", to="auth.permission", verbose_name="user permissions")),
            ],
            options={"verbose_name": "Admin user", "verbose_name_plural": "Admin users", "ordering": ["email"]},
        ),
        migrations.CreateModel(
            name="AdminInvite",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("email", models.EmailField(max_length=254)),
                ("token", models.CharField(max_length=64, unique=True)),
                ("full_name", models.CharField(blank=True, max_length=200)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("expires_at", models.DateTimeField()),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                ("revoked", models.BooleanField(default=False)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="invites_sent", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="AIAccountReview",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("subject_type", models.CharField(choices=[("consultant", "Consultant"), ("breeder", "Breeder")], max_length=16)),
                ("subject_id", models.UUIDField()),
                ("subject_user_email", models.EmailField(blank=True, max_length=254)),
                ("subject_display_name", models.CharField(blank=True, max_length=255)),
                ("decision", models.CharField(choices=[("pending", "Pending"), ("approved", "Approved"), ("rejected", "Rejected"), ("flagged", "Flagged"), ("error", "Error")], default="pending", max_length=20)),
                ("confidence", models.FloatField(default=0.0)),
                ("rationale", models.TextField(blank=True)),
                ("evidence", models.JSONField(blank=True, default=dict)),
                ("recommended_actions", models.JSONField(blank=True, default=list)),
                ("applied_actions", models.JSONField(blank=True, default=list)),
                ("openai_raw", models.JSONField(blank=True, default=dict)),
                ("ai_model", models.CharField(blank=True, max_length=80)),
                ("error", models.TextField(blank=True)),
                ("manually_overridden", models.BooleanField(default=False)),
                ("overridden_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="manual_overrides", to=settings.AUTH_USER_MODEL)),
                ("override_reason", models.TextField(blank=True)),
                ("original_decision", models.CharField(blank=True, max_length=20)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("decided_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="aiaccountreview",
            index=models.Index(fields=["subject_type", "subject_id"], name="admin_porta_subject_type_idx"),
        ),
        migrations.AddIndex(
            model_name="aiaccountreview",
            index=models.Index(fields=["decision", "-created_at"], name="admin_porta_decision_idx"),
        ),
        migrations.AddConstraint(
            model_name="aiaccountreview",
            constraint=models.UniqueConstraint(fields=("subject_type", "subject_id"), name="one_review_per_external_profile"),
        ),
        migrations.CreateModel(
            name="AIFlag",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("severity", models.CharField(choices=[("info", "Info"), ("warning", "Warning"), ("critical", "Critical")], default="warning", max_length=20)),
                ("reason", models.TextField()),
                ("recommended_solution", models.TextField(blank=True)),
                ("applied_solution", models.TextField(blank=True)),
                ("notified_emails", models.JSONField(blank=True, default=list)),
                ("notified_slack", models.BooleanField(default=False)),
                ("resolved", models.BooleanField(default=False)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("resolution_notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("review", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="flags", to="admin_portal.aiaccountreview")),
                ("resolved_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="resolved_flags", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="DailyReport",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("report_date", models.DateField(unique=True)),
                ("approved_count", models.PositiveIntegerField(default=0)),
                ("rejected_count", models.PositiveIntegerField(default=0)),
                ("flagged_count", models.PositiveIntegerField(default=0)),
                ("pending_count", models.PositiveIntegerField(default=0)),
                ("breeder_count", models.PositiveIntegerField(default=0)),
                ("consultant_count", models.PositiveIntegerField(default=0)),
                ("manual_override_count", models.PositiveIntegerField(default=0)),
                ("summary", models.TextField(blank=True)),
                ("details", models.JSONField(blank=True, default=dict)),
                ("delivered_email", models.BooleanField(default=False)),
                ("delivered_slack", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={"ordering": ["-report_date"]},
        ),
        migrations.CreateModel(
            name="AdminAuditLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action", models.CharField(max_length=64)),
                ("target_type", models.CharField(blank=True, max_length=32)),
                ("target_id", models.CharField(blank=True, max_length=64)),
                ("details", models.JSONField(blank=True, default=dict)),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="audit_entries", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="adminauditlog",
            index=models.Index(fields=["-created_at"], name="admin_porta_created_idx"),
        ),
    ]
