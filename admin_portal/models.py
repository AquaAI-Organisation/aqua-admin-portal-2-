"""Control-plane data model.

Two kinds of tables live here:

1. `admin_portal_*` — local, managed tables (AdminUser, AdminInvite, AIAccountReview,
   AIFlag, DailyReport, AdminAuditLog). Owned by this project.
2. Unmanaged mirrors of the main backend's tables (`user_auth_user`,
   `consultant_consultantprofile`, `breeders_breederprofile`). The `managed = False`
   flag guarantees migrations here will never touch the main backend's schema.
"""
import uuid
from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.utils import timezone

from .managers import AdminUserManager


ROLE_CHOICES = [
    ("super_admin", "Super Admin"),
    ("developer", "Developer"),
    ("guest", "Guest"),
]

SUBJECT_CHOICES = [("consultant", "Consultant"), ("breeder", "Breeder")]
DECISION_CHOICES = [
    ("pending", "Pending"),
    ("approved", "Approved"),
    ("rejected", "Rejected"),
    ("flagged", "Flagged"),
    ("error", "Error"),
]
SEVERITY_CHOICES = [("info", "Info"), ("warning", "Warning"), ("critical", "Critical")]


# ---------------------------------------------------------------------------
# Local admin identity & governance
# ---------------------------------------------------------------------------

class AdminUser(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=200, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=True)
    is_platform_super_admin = models.BooleanField(
        default=False,
        help_text=(
            "Total control over the control plane. Only steven@humara.io and "
            "ben@humara.io should ever have this flag set."
        ),
    )
    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default="guest",
        help_text=(
            "guest = read-only access. "
            "developer = read + write (updates notify super-admins). "
            "super_admin = full control (set automatically for Steven/Ben)."
        ),
    )
    invited_by = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="invited_admins",
    )
    password_changed_at = models.DateTimeField(null=True, blank=True)
    must_change_password = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = AdminUserManager()

    class Meta:
        verbose_name = "Admin user"
        verbose_name_plural = "Admin users"
        ordering = ["email"]

    def __str__(self):
        return self.email

    @property
    def is_super_admin(self):
        allow = {e.lower() for e in getattr(settings, "SUPERADMIN_EMAILS", [])}
        return bool(self.is_platform_super_admin and self.email.lower() in allow)

    @property
    def can_write(self):
        """Developers and super admins can make changes."""
        return self.role in ("developer", "super_admin") or self.is_super_admin

    @property
    def is_guest(self):
        return self.role == "guest" and not self.is_super_admin

    @property
    def is_developer(self):
        return self.role == "developer" and not self.is_super_admin

    @property
    def role_display(self):
        if self.is_super_admin:
            return "Super Admin"
        return dict(ROLE_CHOICES).get(self.role, self.role)


class AdminInvite(models.Model):
    email = models.EmailField()
    token = models.CharField(max_length=64, unique=True)
    full_name = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(AdminUser, on_delete=models.CASCADE, related_name="invites_sent")
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked = models.BooleanField(default=False)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Invite for {self.email}"

    @property
    def is_pending(self):
        return (
            self.accepted_at is None
            and not self.revoked
            and self.expires_at > timezone.now()
        )


# ---------------------------------------------------------------------------
# Unmanaged mirrors of the main backend's tables
# ---------------------------------------------------------------------------

class ExternalUser(models.Model):
    id = models.UUIDField(primary_key=True)
    username = models.CharField(max_length=150)
    email = models.EmailField()
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    name = models.CharField(max_length=255, blank=True)
    phone = models.CharField(max_length=15, blank=True, null=True)
    role = models.CharField(max_length=20)
    is_verified = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    profile_picture = models.CharField(max_length=255, blank=True, null=True)
    verification_documents = models.JSONField(default=list, blank=True)
    current_trust_score = models.FloatField(null=True, blank=True)
    current_regulatory_tier = models.CharField(max_length=32, blank=True, null=True)
    is_at_risk = models.BooleanField(default=False)
    date_joined = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "user_auth_user"

    def __str__(self):
        return self.email or self.username


class ExternalConsultantProfile(models.Model):
    id = models.UUIDField(primary_key=True)
    user = models.ForeignKey(
        ExternalUser, on_delete=models.DO_NOTHING, db_column="user_id", related_name="+"
    )
    company_name = models.CharField(max_length=255, blank=True, null=True)
    bio = models.TextField(blank=True)
    admin_status = models.CharField(max_length=20, default="pending")
    admin_notes = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    is_verified = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    website = models.CharField(max_length=255, blank=True, null=True)
    business_phone = models.CharField(max_length=20, blank=True, null=True)
    business_address = models.TextField(blank=True, null=True)
    verification_level = models.CharField(max_length=20, blank=True, default="none")
    credentials = models.JSONField(default=list, blank=True)
    specializations = models.JSONField(default=list, blank=True)
    services_list = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "consultant_consultantprofile"

    def __str__(self):
        return self.company_name or str(self.user)


class ExternalBreederProfile(models.Model):
    id = models.UUIDField(primary_key=True)
    user = models.ForeignKey(
        ExternalUser, on_delete=models.DO_NOTHING, db_column="user_id", related_name="+"
    )
    company_name = models.CharField(max_length=255, blank=True, null=True)
    bio = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    is_verified = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    verification_level = models.CharField(max_length=20, blank=True, default="none")
    website = models.CharField(max_length=255, blank=True, null=True)
    business_phone = models.CharField(max_length=20, blank=True, null=True)
    business_address = models.TextField(blank=True, null=True)
    has_certified_lineage = models.BooleanField(default=False)
    lineage_documentation_count = models.IntegerField(default=0)
    breeding_records_complete = models.BooleanField(default=False)
    healthy_stock_rate = models.FloatField(null=True, blank=True)
    stock_mortality_rate = models.FloatField(null=True, blank=True)
    disease_reported_rate = models.FloatField(null=True, blank=True)
    local_trust_score = models.FloatField(null=True, blank=True)
    specializations = models.JSONField(default=list, blank=True)
    service_area = models.CharField(max_length=255, blank=True, null=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "breeders_breederprofile"

    def __str__(self):
        return self.company_name or str(self.user)


# ---------------------------------------------------------------------------
# AI decision record, flags, analytics, audit
# ---------------------------------------------------------------------------

class AIAccountReview(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subject_type = models.CharField(max_length=16, choices=SUBJECT_CHOICES)
    subject_id = models.UUIDField()
    subject_user_email = models.EmailField(blank=True)
    subject_display_name = models.CharField(max_length=255, blank=True)
    decision = models.CharField(max_length=20, choices=DECISION_CHOICES, default="pending")
    confidence = models.FloatField(default=0.0)
    rationale = models.TextField(blank=True)
    evidence = models.JSONField(default=dict, blank=True)
    recommended_actions = models.JSONField(default=list, blank=True)
    applied_actions = models.JSONField(default=list, blank=True)
    openai_raw = models.JSONField(default=dict, blank=True)
    ai_model = models.CharField(max_length=80, blank=True)
    error = models.TextField(blank=True)
    # Manual override fields
    manually_overridden = models.BooleanField(default=False)
    overridden_by = models.ForeignKey(
        AdminUser, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="manual_overrides",
    )
    override_reason = models.TextField(blank=True)
    original_decision = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["subject_type", "subject_id"]),
            models.Index(fields=["decision", "-created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["subject_type", "subject_id"], name="one_review_per_external_profile"
            )
        ]

    def __str__(self):
        return f"{self.subject_type}:{self.subject_id} → {self.decision}"

    @property
    def badge_class(self):
        return {
            "approved": "ok",
            "rejected": "danger",
            "flagged": "warn",
            "pending": "muted",
            "error": "danger",
        }.get(self.decision, "muted")

    @property
    def confidence_percent(self):
        return int(self.confidence * 100)

    @property
    def confidence_level(self):
        if self.confidence >= 0.8:
            return "high"
        if self.confidence >= 0.5:
            return "medium"
        return "low"


class AIFlag(models.Model):
    review = models.ForeignKey(AIAccountReview, on_delete=models.CASCADE, related_name="flags")
    severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default="warning")
    reason = models.TextField()
    recommended_solution = models.TextField(blank=True)
    applied_solution = models.TextField(blank=True)
    notified_emails = models.JSONField(default=list, blank=True)
    notified_slack = models.BooleanField(default=False)
    resolved = models.BooleanField(default=False)
    resolved_by = models.ForeignKey(
        AdminUser, null=True, blank=True, on_delete=models.SET_NULL, related_name="resolved_flags"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.severity.upper()} on {self.review_id}"


class DailyReport(models.Model):
    report_date = models.DateField(unique=True)
    approved_count = models.PositiveIntegerField(default=0)
    rejected_count = models.PositiveIntegerField(default=0)
    flagged_count = models.PositiveIntegerField(default=0)
    pending_count = models.PositiveIntegerField(default=0)
    breeder_count = models.PositiveIntegerField(default=0)
    consultant_count = models.PositiveIntegerField(default=0)
    manual_override_count = models.PositiveIntegerField(default=0)
    summary = models.TextField(blank=True)
    details = models.JSONField(default=dict, blank=True)
    delivered_email = models.BooleanField(default=False)
    delivered_slack = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-report_date"]

    def __str__(self):
        return f"Daily report {self.report_date}"

    @property
    def total_reviewed(self):
        return self.approved_count + self.rejected_count + self.flagged_count


class AdminAuditLog(models.Model):
    actor = models.ForeignKey(
        AdminUser, null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_entries"
    )
    action = models.CharField(max_length=64)
    target_type = models.CharField(max_length=32, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    details = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["-created_at"])]

    def __str__(self):
        return f"{self.action} by {self.actor_id or 'system'}"
