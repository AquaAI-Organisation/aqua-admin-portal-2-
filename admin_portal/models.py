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
from .services.error_classifier import classify_openai_error


ROLE_CHOICES = [
    ("super_admin", "Super Admin"),
    ("admin", "Admin"),
    ("developer", "Developer"),
    ("guest", "Guest"),
]
INVITE_DELIVERY_CHOICES = [
    ("pending", "Pending"),
    ("email_sent", "Email sent"),
    ("email_failed", "Email failed"),
    ("link_available", "Link available"),
]
INVITE_ROLE_CHOICES = [
    ("admin", "Admin"),
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
ISSUE_SOURCE_CHOICES = [
    ("incident", "Incident"),
    ("consultant_warning", "Consultant Warning"),
    ("message_risk", "Message Risk"),
    ("breeder_inquiry_risk", "Breeder Inquiry Risk"),
    ("booking_risk", "Booking Risk"),
    ("payment_risk", "Payment Risk"),
    ("trust_drop", "Trust Drop"),
    ("support_inquiry", "Support Inquiry"),
]
ISSUE_STATUS_CHOICES = [
    ("open", "Open"),
    ("resolved", "Resolved"),
    ("error", "Error"),
]
INQUIRY_STATUS_CHOICES = [
    ("new", "New"),
    ("triaged", "Triaged"),
    ("actioned", "Actioned"),
    ("replied", "Replied"),
    ("archived", "Archived"),
    ("error", "Error"),
]


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
            "admin = moderation and operational control. "
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
        """Admins, developers, and super admins can make changes."""
        return self.role in ("admin", "developer", "super_admin") or self.is_super_admin

    @property
    def is_guest(self):
        return self.role == "guest" and not self.is_super_admin

    @property
    def is_admin(self):
        return self.role == "admin" and not self.is_super_admin

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
    role = models.CharField(max_length=20, choices=INVITE_ROLE_CHOICES, default="guest")
    created_by = models.ForeignKey(AdminUser, on_delete=models.CASCADE, related_name="invites_sent")
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked = models.BooleanField(default=False)
    revoked_at = models.DateTimeField(null=True, blank=True)
    delivery_status = models.CharField(max_length=20, choices=INVITE_DELIVERY_CHOICES, default="pending")
    delivery_error = models.TextField(blank=True)
    last_delivery_attempt_at = models.DateTimeField(null=True, blank=True)

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


class OperationalSettings(models.Model):
    smtp_host = models.CharField(max_length=255, blank=True)
    smtp_port = models.PositiveIntegerField(default=587)
    smtp_use_tls = models.BooleanField(default=True)
    smtp_username = models.CharField(max_length=255, blank=True)
    smtp_password = models.CharField(max_length=255, blank=True)
    default_from_email = models.CharField(max_length=255, blank=True)

    slack_bot_token = models.CharField(max_length=255, blank=True)
    slack_channel = models.CharField(max_length=255, blank=True)

    imap_host = models.CharField(max_length=255, blank=True)
    imap_port = models.PositiveIntegerField(default=993)
    imap_use_ssl = models.BooleanField(default=True)
    imap_username = models.CharField(max_length=255, blank=True)
    imap_password = models.CharField(max_length=255, blank=True)
    imap_folder = models.CharField(max_length=128, default="INBOX", blank=True)

    updated_by = models.ForeignKey(
        AdminUser, null=True, blank=True, on_delete=models.SET_NULL, related_name="operational_settings_updates"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Operational settings"
        verbose_name_plural = "Operational settings"

    def __str__(self):
        return "Operational settings"

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def masked_smtp_username(self):
        if not self.smtp_username:
            return ""
        if "@" in self.smtp_username:
            name, domain = self.smtp_username.split("@", 1)
            prefix = name[:2]
            return f"{prefix}***@{domain}"
        return f"{self.smtp_username[:2]}***"


class SupportInquiry(models.Model):
    message_id = models.CharField(max_length=255, unique=True)
    from_email = models.EmailField()
    from_name = models.CharField(max_length=255, blank=True)
    subject = models.CharField(max_length=255, blank=True)
    body_text = models.TextField(blank=True)
    received_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=20, choices=INQUIRY_STATUS_CHOICES, default="new")
    matched_entity_type = models.CharField(max_length=32, blank=True)
    matched_entity_id = models.CharField(max_length=64, blank=True)
    ai_summary = models.TextField(blank=True)
    ai_rationale = models.TextField(blank=True)
    ai_recommended_actions = models.JSONField(default=list, blank=True)
    ai_raw = models.JSONField(default=dict, blank=True)
    ai_model = models.CharField(max_length=80, blank=True)
    ai_error = models.TextField(blank=True)
    response_draft = models.TextField(blank=True)
    response_history = models.JSONField(default=list, blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-received_at", "-created_at"]
        indexes = [
            models.Index(fields=["status", "-received_at"], name="admin_porta_inquiry_status_idx"),
        ]

    def __str__(self):
        return self.subject or self.from_email


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
    badges_count = models.IntegerField(null=True, blank=True)
    successful_transactions = models.IntegerField(null=True, blank=True)
    average_rating = models.FloatField(null=True, blank=True)
    consultations_completed = models.IntegerField(null=True, blank=True)
    avg_response_time_hours = models.FloatField(null=True, blank=True)
    stock_items_sold = models.IntegerField(null=True, blank=True)
    health_reports_submitted = models.IntegerField(null=True, blank=True)
    lineage_documented_count = models.IntegerField(null=True, blank=True)
    last_activity_at = models.DateTimeField(null=True, blank=True)
    last_login_at = models.DateTimeField(null=True, blank=True)
    overall_score = models.IntegerField(null=True, blank=True)
    responsibility_score = models.IntegerField(null=True, blank=True)
    community_score = models.IntegerField(null=True, blank=True)
    transaction_score = models.IntegerField(null=True, blank=True)
    consistency_score = models.IntegerField(null=True, blank=True)
    data_stewardship_score = models.IntegerField(null=True, blank=True)
    habitat_stability_score = models.IntegerField(null=True, blank=True)
    trading_reliability_score = models.IntegerField(null=True, blank=True)
    date_joined = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(null=True, blank=True)

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
    rating = models.FloatField(null=True, blank=True)
    reviews_count = models.IntegerField(null=True, blank=True)
    verification_level = models.CharField(max_length=20, blank=True, default="none")
    credentials = models.JSONField(default=list, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    total_bookings = models.IntegerField(null=True, blank=True)
    completed_bookings = models.IntegerField(null=True, blank=True)
    cancelled_bookings = models.IntegerField(null=True, blank=True)
    no_show_count = models.IntegerField(null=True, blank=True)
    completion_rate = models.FloatField(null=True, blank=True)
    cancellation_rate = models.FloatField(null=True, blank=True)
    complaint_count = models.IntegerField(null=True, blank=True)
    average_response_time_hours = models.FloatField(null=True, blank=True)
    fast_responses_count = models.IntegerField(null=True, blank=True)
    total_inquiries = models.IntegerField(null=True, blank=True)
    repeated_clients_count = models.IntegerField(null=True, blank=True)
    overall_score = models.IntegerField(null=True, blank=True)
    professionalism_score = models.IntegerField(null=True, blank=True)
    reliability_score = models.IntegerField(null=True, blank=True)
    responsiveness_score = models.IntegerField(null=True, blank=True)
    expertise_score = models.IntegerField(null=True, blank=True)
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
    rating = models.FloatField(null=True, blank=True)
    reviews_count = models.IntegerField(null=True, blank=True)
    total_inquiries = models.IntegerField(null=True, blank=True)
    total_responded = models.IntegerField(null=True, blank=True)
    average_response_hours = models.FloatField(null=True, blank=True)
    has_certified_lineage = models.BooleanField(default=False)
    lineage_documentation_count = models.IntegerField(default=0)
    breeding_records_complete = models.BooleanField(default=False)
    healthy_stock_rate = models.FloatField(null=True, blank=True)
    stock_mortality_rate = models.FloatField(null=True, blank=True)
    disease_reported_rate = models.FloatField(null=True, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    total_sales = models.IntegerField(null=True, blank=True)
    successful_sales = models.IntegerField(null=True, blank=True)
    returned_stock_count = models.IntegerField(null=True, blank=True)
    species_count = models.IntegerField(null=True, blank=True)
    total_stock_sold = models.IntegerField(null=True, blank=True)
    local_sales_count = models.IntegerField(null=True, blank=True)
    repeat_local_customers = models.IntegerField(null=True, blank=True)
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


class ExternalIncidentLog(models.Model):
    id = models.UUIDField(primary_key=True)
    user = models.ForeignKey(
        ExternalUser, on_delete=models.DO_NOTHING, db_column="user_id", related_name="+"
    )
    incident_code = models.CharField(max_length=50)
    severity_level = models.CharField(max_length=5)
    penalty_points = models.IntegerField(default=0)
    description = models.TextField()
    evidence = models.JSONField(default=dict, blank=True, null=True)
    related_entity_type = models.CharField(max_length=50, blank=True)
    related_entity_id = models.CharField(max_length=255, blank=True, null=True)
    occurred_at = models.DateTimeField(null=True, blank=True)
    is_cleared = models.BooleanField(default=False)
    cleared_at = models.DateTimeField(null=True, blank=True)
    decay_percentage = models.IntegerField(default=0)
    created_at = models.DateTimeField(null=True, blank=True)
    created_by = models.CharField(max_length=100, blank=True)

    class Meta:
        managed = False
        db_table = "badges_incidentlog"

    def __str__(self):
        return f"{self.incident_code} ({self.severity_level})"


class ExternalTrustScoreSnapshot(models.Model):
    id = models.UUIDField(primary_key=True)
    user = models.ForeignKey(
        ExternalUser, on_delete=models.DO_NOTHING, db_column="user_id", related_name="+"
    )
    trust_score = models.IntegerField(default=0)
    regulatory_tier = models.CharField(max_length=32, blank=True)
    total_badge_points = models.IntegerField(default=0)
    total_incident_penalties = models.IntegerField(default=0)
    contributing_factors = models.JSONField(default=dict, blank=True)
    calculated_at = models.DateTimeField(null=True, blank=True)
    calculation_version = models.CharField(max_length=64, blank=True)

    class Meta:
        managed = False
        db_table = "badges_trustscoresnapshot"

    def __str__(self):
        return f"{self.user_id} @ {self.calculated_at}"


class ExternalConsultantWarning(models.Model):
    id = models.UUIDField(primary_key=True)
    consultant = models.ForeignKey(
        ExternalConsultantProfile, on_delete=models.DO_NOTHING, db_column="consultant_id", related_name="+"
    )
    title = models.CharField(max_length=255)
    message = models.TextField()
    severity = models.CharField(max_length=32, blank=True)
    status = models.CharField(max_length=32, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "consultant_consultantwarning"

    def __str__(self):
        return self.title


class ExternalBreederReview(models.Model):
    id = models.UUIDField(primary_key=True)
    breeder = models.ForeignKey(
        ExternalBreederProfile, on_delete=models.DO_NOTHING, db_column="breeder_id", related_name="+"
    )
    reviewer_id = models.UUIDField(null=True, blank=True)
    rating = models.IntegerField(default=0)
    comment = models.TextField(blank=True)
    stock_health_rating = models.IntegerField(null=True, blank=True)
    communication_rating = models.IntegerField(null=True, blank=True)
    accuracy_rating = models.IntegerField(null=True, blank=True)
    is_verified_purchase = models.BooleanField(default=False)
    created_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "breeders_breederreview"


class ExternalBreederInquiry(models.Model):
    id = models.UUIDField(primary_key=True)
    breeder = models.ForeignKey(
        ExternalBreederProfile, on_delete=models.DO_NOTHING, db_column="breeder_id", related_name="+"
    )
    user_id = models.UUIDField(null=True, blank=True)
    message = models.TextField()
    response = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=32, blank=True)
    priority = models.CharField(max_length=32, blank=True)
    source = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(null=True, blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        managed = False
        db_table = "breeders_breederinquiry"


class ExternalConsultantBooking(models.Model):
    id = models.UUIDField(primary_key=True)
    consultant = models.ForeignKey(
        ExternalConsultantProfile, on_delete=models.DO_NOTHING, db_column="consultant_id", related_name="+"
    )
    requester_id = models.UUIDField(null=True, blank=True)
    scheduled_start = models.DateTimeField(null=True, blank=True)
    scheduled_end = models.DateTimeField(null=True, blank=True)
    full_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    booking_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    payment_status = models.CharField(max_length=32, blank=True)
    status = models.CharField(max_length=32, blank=True)
    consultant_status = models.CharField(max_length=32, blank=True)
    notes = models.TextField(blank=True, null=True)
    rating = models.IntegerField(null=True, blank=True)
    review = models.TextField(blank=True, null=True)
    response_time_hours = models.FloatField(null=True, blank=True)
    was_fast_response = models.BooleanField(default=False)
    was_successful = models.BooleanField(default=False)
    created_at = models.DateTimeField(null=True, blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "consultant_consultantbooking"


class ExternalPaymentFailureLog(models.Model):
    id = models.UUIDField(primary_key=True)
    user_id = models.UUIDField(null=True, blank=True)
    payment_intent_id = models.UUIDField(null=True, blank=True)
    failure_reason = models.TextField(blank=True)
    failure_code = models.CharField(max_length=128, blank=True, null=True)
    stripe_error = models.JSONField(default=dict, blank=True, null=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=16, blank=True)
    endpoint = models.CharField(max_length=255, blank=True, null=True)
    created_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "payments_paymentfailurelog"


class ExternalRefund(models.Model):
    id = models.UUIDField(primary_key=True)
    payment_intent_id = models.UUIDField(null=True, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=16, blank=True)
    status = models.CharField(max_length=32, blank=True)
    reason = models.CharField(max_length=64, blank=True)
    failure_reason = models.TextField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "payments_refund"


class ExternalConversation(models.Model):
    id = models.UUIDField(primary_key=True)
    participant_1_id = models.UUIDField()
    participant_2_id = models.UUIDField()
    created_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(null=True, blank=True)
    last_message_at = models.DateTimeField(null=True, blank=True)
    last_message_preview = models.TextField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "messaging_conversation"


class ExternalMessage(models.Model):
    id = models.UUIDField(primary_key=True)
    conversation = models.ForeignKey(
        ExternalConversation, on_delete=models.DO_NOTHING, db_column="conversation_id", related_name="+"
    )
    sender_id = models.UUIDField()
    content = models.TextField()
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(null=True, blank=True)
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True, null=True)

    class Meta:
        managed = False
        db_table = "messaging_message"


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
            models.Index(fields=["subject_type", "subject_id"], name="admin_porta_subject_type_idx"),
            models.Index(fields=["decision", "-created_at"], name="admin_porta_decision_idx"),
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

    @property
    def error_info(self):
        return classify_openai_error(self.error)

    @property
    def error_category(self):
        return self.error_info["category"]

    @property
    def error_label(self):
        return self.error_info["label"]

    @property
    def error_summary(self):
        return self.error_info["summary"]

    @property
    def decision_basis(self):
        return (self.evidence or {}).get("decision_basis", {})


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


class AIFlaggedIssue(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_type = models.CharField(max_length=32, choices=ISSUE_SOURCE_CHOICES)
    source_id = models.CharField(max_length=64)
    subject_type = models.CharField(max_length=16, choices=SUBJECT_CHOICES, blank=True)
    subject_user_id = models.UUIDField(null=True, blank=True)
    subject_user_email = models.EmailField(blank=True)
    subject_display_name = models.CharField(max_length=255, blank=True)
    title = models.CharField(max_length=255, blank=True)
    severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default="warning")
    status = models.CharField(max_length=20, choices=ISSUE_STATUS_CHOICES, default="open")
    summary = models.TextField(blank=True)
    rationale = models.TextField(blank=True)
    evidence = models.JSONField(default=dict, blank=True)
    recommended_actions = models.JSONField(default=list, blank=True)
    applied_actions = models.JSONField(default=list, blank=True)
    source_payload = models.JSONField(default=dict, blank=True)
    openai_raw = models.JSONField(default=dict, blank=True)
    ai_model = models.CharField(max_length=80, blank=True)
    error = models.TextField(blank=True)
    notified_emails = models.JSONField(default=list, blank=True)
    notified_slack = models.BooleanField(default=False)
    resolved = models.BooleanField(default=False)
    resolved_by = models.ForeignKey(
        AdminUser, null=True, blank=True, on_delete=models.SET_NULL, related_name="resolved_issues"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    triaged_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["source_type", "source_id"], name="admin_porta_source__idx"),
            models.Index(fields=["status", "-created_at"], name="admin_porta_status__idx"),
            models.Index(fields=["severity", "-created_at"], name="admin_porta_severity_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["source_type", "source_id"], name="one_triage_record_per_external_issue"
            )
        ]

    def __str__(self):
        return f"{self.source_type}:{self.source_id} -> {self.severity}/{self.status}"

    @property
    def badge_class(self):
        return {
            "critical": "danger",
            "warning": "warn",
            "info": "info",
        }.get(self.severity, "muted")

    @property
    def source_label(self):
        return dict(ISSUE_SOURCE_CHOICES).get(self.source_type, self.source_type)

    @property
    def error_info(self):
        return classify_openai_error(self.error)

    @property
    def error_label(self):
        return self.error_info["label"]

    @property
    def error_summary(self):
        return self.error_info["summary"]


class DailyReport(models.Model):
    report_date = models.DateField(unique=True)
    approved_count = models.PositiveIntegerField(default=0)
    rejected_count = models.PositiveIntegerField(default=0)
    flagged_count = models.PositiveIntegerField(default=0)
    pending_count = models.PositiveIntegerField(default=0)
    breeder_count = models.PositiveIntegerField(default=0)
    consultant_count = models.PositiveIntegerField(default=0)
    manual_override_count = models.PositiveIntegerField(default=0)
    issue_count = models.PositiveIntegerField(default=0)
    critical_issue_count = models.PositiveIntegerField(default=0)
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
        indexes = [models.Index(fields=["-created_at"], name="admin_porta_created_idx")]

    def __str__(self):
        return f"{self.action} by {self.actor_id or 'system'}"
