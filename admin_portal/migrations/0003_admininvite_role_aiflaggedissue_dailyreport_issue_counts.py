import uuid

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("admin_portal", "0002_externalbreederprofile_externalconsultantprofile_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="admininvite",
            name="role",
            field=models.CharField(
                choices=[("developer", "Developer"), ("guest", "Guest")],
                default="guest",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="dailyreport",
            name="critical_issue_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="dailyreport",
            name="issue_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.CreateModel(
            name="AIFlaggedIssue",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("source_type", models.CharField(choices=[("incident", "Incident"), ("consultant_warning", "Consultant Warning")], max_length=32)),
                ("source_id", models.CharField(max_length=64)),
                ("subject_type", models.CharField(blank=True, choices=[("consultant", "Consultant"), ("breeder", "Breeder")], max_length=16)),
                ("subject_user_id", models.UUIDField(blank=True, null=True)),
                ("subject_user_email", models.EmailField(blank=True, max_length=254)),
                ("subject_display_name", models.CharField(blank=True, max_length=255)),
                ("title", models.CharField(blank=True, max_length=255)),
                ("severity", models.CharField(choices=[("info", "Info"), ("warning", "Warning"), ("critical", "Critical")], default="warning", max_length=20)),
                ("status", models.CharField(choices=[("open", "Open"), ("resolved", "Resolved"), ("error", "Error")], default="open", max_length=20)),
                ("summary", models.TextField(blank=True)),
                ("rationale", models.TextField(blank=True)),
                ("evidence", models.JSONField(blank=True, default=dict)),
                ("recommended_actions", models.JSONField(blank=True, default=list)),
                ("applied_actions", models.JSONField(blank=True, default=list)),
                ("source_payload", models.JSONField(blank=True, default=dict)),
                ("openai_raw", models.JSONField(blank=True, default=dict)),
                ("ai_model", models.CharField(blank=True, max_length=80)),
                ("error", models.TextField(blank=True)),
                ("notified_emails", models.JSONField(blank=True, default=list)),
                ("notified_slack", models.BooleanField(default=False)),
                ("resolved", models.BooleanField(default=False)),
                ("resolution_notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("triaged_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("resolved_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="resolved_issues", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="aiflaggedissue",
            index=models.Index(fields=["source_type", "source_id"], name="admin_porta_source__idx"),
        ),
        migrations.AddIndex(
            model_name="aiflaggedissue",
            index=models.Index(fields=["status", "-created_at"], name="admin_porta_status__idx"),
        ),
        migrations.AddIndex(
            model_name="aiflaggedissue",
            index=models.Index(fields=["severity", "-created_at"], name="admin_porta_severity_idx"),
        ),
        migrations.AddConstraint(
            model_name="aiflaggedissue",
            constraint=models.UniqueConstraint(fields=("source_type", "source_id"), name="one_triage_record_per_external_issue"),
        ),
    ]
