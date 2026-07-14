"""Control-plane data model.

Two kinds of tables live here:

1. `admin_portal_*` — local, managed tables (AdminUser, AdminInvite, AIAccountReview,
   AIFlag, DailyReport, AdminAuditLog). Owned by this project.
2. Unmanaged mirrors of the main backend's tables (`user_auth_user`,
   `consultant_consultantprofile`, `breeders_breederprofile`). The `managed = False`
   flag guarantees migrations here will never touch the main backend's schema.
"""
from datetime import timedelta
import random
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
MAILBOX_KIND_CHOICES = [
    ("general", "General Support"),
    ("privacy", "Privacy"),
    ("providers", "Providers"),
]
INQUIRY_STATUS_CHOICES = [
    ("new", "New"),
    ("triaged", "Triaged"),
    ("actioned", "Actioned"),
    ("replied", "Replied"),
    ("archived", "Archived"),
    ("error", "Error"),
]
DSAR_REQUEST_TYPE_CHOICES = [
    ("access", "Access"),
    ("portability", "Portability"),
    ("rectification", "Rectification"),
    ("erasure", "Erasure"),
    ("restriction", "Restriction"),
    ("objection", "Objection"),
]
DSAR_STATUS_CHOICES = [
    ("received", "Received"),
    ("verifying", "Verifying"),
    ("verified", "Verified"),
    ("in_progress", "In progress"),
    ("awaiting_dpo_approval", "Awaiting DPO approval"),
    ("fulfilled", "Fulfilled"),
    ("rejected", "Rejected"),
    ("extended", "Extended"),
    ("withdrawn", "Withdrawn"),
    ("unmatched", "Unmatched"),
]
DSAR_CHANNEL_CHOICES = [
    ("web_form", "Web form"),
    ("email", "Email"),
    ("in_app", "In app"),
]
DSAR_DELIVERABLE_CHOICES = [
    ("access_export_pdf", "Access export PDF"),
    ("access_export_json", "Access export JSON"),
    ("access_export_html", "Access export HTML"),
    ("deletion_report_json", "Deletion report JSON"),
    ("deletion_report_html", "Deletion report HTML"),
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


def default_stagger_schedule():
    """A long, varied, deterministic sequence of per-account approval delays (in
    minutes) that new accounts cycle through when staggered auto-activation is on.

    It starts with a hand-picked set of human-looking values and then fills out to
    a long list of varied delays, so approvals do not fall on an obvious repeating
    pattern before the sequence recycles. Deterministic (fixed seed) so the schedule
    is stable across processes and restarts."""
    seed_values = [35, 22, 17, 8, 10, 50, 45, 12, 28, 6, 41, 19, 33, 9, 25]
    rng = random.Random(20260611)
    values = list(seed_values)
    while len(values) < 720:
        values.append(rng.randint(5, 55))
    return values


class OperationalSettings(models.Model):
    auto_activate_new_accounts = models.BooleanField(
        default=False,
        help_text=(
            "When enabled, new breeder and consultant accounts are automatically approved "
            "without AI review as soon as the review processor picks them up."
        ),
    )
    auto_activate_stagger_enabled = models.BooleanField(
        default=False,
        help_text=(
            "When enabled (with automatic activation on), each new account is still "
            "auto-approved, but only after a different, human-looking delay taken from the "
            "approval-delay schedule below. The account stays pending during the wait, so "
            "approvals look manual while remaining fully automatic."
        ),
    )
    auto_activate_delay_schedule = models.JSONField(
        default=default_stagger_schedule,
        blank=True,
        help_text=(
            "Repeating sequence of per-account approval delays in minutes. Each new account "
            "takes the next value; the sequence recycles once exhausted."
        ),
    )
    auto_activate_stagger_cursor = models.PositiveIntegerField(
        default=0,
        help_text="Internal pointer into the delay schedule for the next new account.",
    )
    dsar_auto_send = models.BooleanField(
        default=True,
        help_text=(
            "When enabled, an access/portability data request is compiled and emailed to the "
            "requester automatically as soon as their aquaai.uk login is confirmed. Turn off to "
            "require an admin to press Approve and send."
        ),
    )
    gmail_client_id = models.CharField(max_length=255, blank=True)
    gmail_client_secret = models.CharField(max_length=255, blank=True)
    gmail_refresh_token = models.CharField(max_length=255, blank=True)
    gmail_sender = models.CharField(max_length=255, blank=True, default="support@aquaai.uk")
    support_alias_email = models.CharField(max_length=255, blank=True, default="support@aquaai.uk")
    privacy_alias_email = models.CharField(max_length=255, blank=True, default="privacy@aquaai.uk")
    providers_alias_email = models.CharField(max_length=255, blank=True, default="providers@aquaai.uk")

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

    def take_next_stagger_delay(self):
        """Return ``(index, delay_minutes)`` for the next new account and advance
        the cursor (persisted), recycling when the schedule is exhausted."""
        schedule = [int(x) for x in (self.auto_activate_delay_schedule or []) if isinstance(x, (int, float))]
        if not schedule:
            schedule = default_stagger_schedule()
            self.auto_activate_delay_schedule = schedule
        idx = (self.auto_activate_stagger_cursor or 0) % len(schedule)
        delay = max(0, int(schedule[idx]))
        self.auto_activate_stagger_cursor = (idx + 1) % len(schedule)
        self.save(update_fields=["auto_activate_delay_schedule", "auto_activate_stagger_cursor", "updated_at"])
        return idx, delay

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
    gmail_thread_id = models.CharField(max_length=255, blank=True)
    from_email = models.EmailField()
    from_name = models.CharField(max_length=255, blank=True)
    to_email = models.CharField(max_length=255, blank=True)
    mailbox_kind = models.CharField(max_length=20, choices=MAILBOX_KIND_CHOICES, default="general")
    subject = models.CharField(max_length=255, blank=True)
    body_text = models.TextField(blank=True)
    gmail_label_ids = models.JSONField(default=list, blank=True)
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
            models.Index(fields=["mailbox_kind", "-received_at"], name="admp_inquiry_mailbox_idx"),
        ]

    def __str__(self):
        return self.subject or self.from_email

    @property
    def mailbox_label(self):
        return dict(MAILBOX_KIND_CHOICES).get(self.mailbox_kind, self.mailbox_kind)

    @property
    def latest_dsar_request(self):
        return self.dsar_requests.order_by("-created_at").first()


class DSARRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inquiry = models.ForeignKey(
        SupportInquiry, null=True, blank=True, on_delete=models.SET_NULL, related_name="dsar_requests"
    )
    request_type = models.CharField(max_length=20, choices=DSAR_REQUEST_TYPE_CHOICES, default="access")
    status = models.CharField(max_length=32, choices=DSAR_STATUS_CHOICES, default="received")
    subject_user_id = models.UUIDField(null=True, blank=True)
    submitted_email = models.EmailField()
    submitted_name = models.CharField(max_length=255, blank=True)
    detail = models.TextField(blank=True)
    channel = models.CharField(max_length=16, choices=DSAR_CHANNEL_CHOICES, default="email")
    received_at = models.DateTimeField(default=timezone.now)
    verified_at = models.DateTimeField(null=True, blank=True)
    dpo_actioned_at = models.DateTimeField(null=True, blank=True)
    fulfilled_at = models.DateTimeField(null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    extended = models.BooleanField(default=False)
    extension_reason = models.TextField(blank=True)
    dpo_actor = models.ForeignKey(
        AdminUser, null=True, blank=True, on_delete=models.SET_NULL, related_name="dsar_actions"
    )
    verification_token_hash = models.CharField(max_length=128, blank=True)
    verification_sent_at = models.DateTimeField(null=True, blank=True)
    verification_expires_at = models.DateTimeField(null=True, blank=True)
    verification_email = models.EmailField(blank=True)
    verification_attempts = models.PositiveIntegerField(default=0)
    # Set once the requester proves identity by logging in on the main aquaai.uk
    # platform (a new session is observed). Data may only be sent after this.
    login_confirmed_at = models.DateTimeField(null=True, blank=True)
    login_confirmed_email = models.EmailField(blank=True)
    # Snapshot of the subject's active platform session keys at request time, so a
    # later *new* session can be recognised as a fresh login after the request.
    login_baseline_keys = models.JSONField(default=list, blank=True)
    export_summary = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["received_at", "-created_at"]
        indexes = [
            models.Index(fields=["status", "due_at"], name="admp_dsar_status_due_idx"),
            models.Index(fields=["submitted_email"], name="admin_porta_dsar_email_idx"),
        ]

    def save(self, *args, **kwargs):
        if not self.due_at and self.received_at:
            self.due_at = self.received_at + timedelta(days=30)
        super().save(*args, **kwargs)

    @property
    def days_remaining(self) -> int:
        if not self.due_at:
            return 0
        delta = self.due_at - timezone.now()
        return int(delta.total_seconds() // 86400)

    @property
    def is_overdue(self) -> bool:
        return bool(self.due_at and timezone.now() > self.due_at and self.status not in {"fulfilled", "rejected", "withdrawn"})

    @property
    def request_type_label(self):
        return dict(DSAR_REQUEST_TYPE_CHOICES).get(self.request_type, self.request_type)

    @property
    def login_confirmed(self) -> bool:
        return self.login_confirmed_at is not None


class DSAREvent(models.Model):
    request = models.ForeignKey(DSARRequest, on_delete=models.CASCADE, related_name="events")
    action = models.CharField(max_length=64)
    actor = models.ForeignKey(
        AdminUser, null=True, blank=True, on_delete=models.SET_NULL, related_name="dsar_events"
    )
    details = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["request", "created_at"], name="admin_porta_dsar_event_idx"),
        ]


class DSARDeliverable(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(DSARRequest, on_delete=models.CASCADE, related_name="deliverables")
    artefact_type = models.CharField(max_length=32, choices=DSAR_DELIVERABLE_CHOICES)
    storage_ref = models.TextField()
    file_name = models.CharField(max_length=255, blank=True)
    mime_type = models.CharField(max_length=128, blank=True)
    generated_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(null=True, blank=True)
    retrieved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-generated_at"]


# ---------------------------------------------------------------------------
# Unmanaged mirrors of the main backend's tables
# ---------------------------------------------------------------------------

class PlatformSession(models.Model):
    """Read-only mirror of the main platform's django_session table. Used to
    detect when a DSAR subject has logged in at aquaai.uk."""
    session_key = models.CharField(max_length=40, primary_key=True)
    session_data = models.TextField()
    expire_date = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "django_session"


class ExternalOutstandingToken(models.Model):
    """Read-only mirror of the main platform's SimpleJWT outstanding-token table.

    The mobile app / API authenticates with JWT, not Django session cookies, so a
    user logging in does NOT create a ``django_session`` row — it issues a refresh
    token, recorded here with the user id and issue time. A row created after a
    DSAR request was raised is therefore proof the subject logged in to confirm
    their identity."""
    id = models.BigAutoField(primary_key=True)
    user_id = models.UUIDField(null=True, blank=True)
    jti = models.CharField(max_length=255)
    created_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "token_blacklist_outstandingtoken"


class ExternalUser(models.Model):
    id = models.UUIDField(primary_key=True)
    username = models.CharField(max_length=150)
    email = models.EmailField()
    password = models.CharField(max_length=255, blank=True)
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


class ExternalMarketplaceSellerProfile(models.Model):
    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        ExternalUser, on_delete=models.DO_NOTHING, db_column="user_id", related_name="+"
    )
    rating = models.FloatField(null=True, blank=True)
    reviews_count = models.IntegerField(null=True, blank=True)
    stripe_connect_account_id = models.CharField(max_length=255, blank=True, null=True)
    stripe_connect_status = models.CharField(max_length=32, blank=True)
    payouts_enabled = models.BooleanField(default=False)
    delivery_sales_enabled = models.BooleanField(default=False)
    delivery_suspended = models.BooleanField(default=False)
    updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "marketplace_sellerprofile"


class ExternalBreederShippingProfile(models.Model):
    id = models.BigAutoField(primary_key=True)
    seller = models.ForeignKey(
        ExternalUser, on_delete=models.DO_NOTHING, db_column="seller_id", related_name="+"
    )
    supports_collection = models.BooleanField(default=True)
    supports_delivery = models.BooleanField(default=False)
    collection_radius_km = models.IntegerField(default=0)
    local_zone_km = models.IntegerField(default=0)
    regional_zone_km = models.IntegerField(default=0)
    appointment_only = models.BooleanField(default=False)
    holiday_mode_enabled = models.BooleanField(default=False)
    holiday_message = models.TextField(blank=True)
    collection_address = models.TextField(blank=True)
    opening_hours = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "marketplace_breedershippingprofile"


class ExternalBreederVerification(models.Model):
    id = models.BigAutoField(primary_key=True)
    seller = models.ForeignKey(
        ExternalUser, on_delete=models.DO_NOTHING, db_column="seller_id", related_name="+"
    )
    document = models.TextField(blank=True)
    licence_number = models.CharField(max_length=100, blank=True)
    issuing_authority = models.CharField(max_length=255, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    # Issue / "received" date of the certificate. Most certificates carry this even
    # when they have no printed expiry, so it drives the 2-year renewal cycle.
    awarded_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, blank=True)
    rejection_reason = models.TextField(blank=True)
    document_metadata = models.JSONField(default=dict, blank=True, null=True)
    reviewed_by_id = models.UUIDField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "marketplace_breederverification"


class ExternalBreederReservation(models.Model):
    id = models.BigAutoField(primary_key=True)
    reservation_code = models.CharField(max_length=40)
    buyer = models.ForeignKey(
        ExternalUser, on_delete=models.DO_NOTHING, db_column="buyer_id", related_name="+"
    )
    seller = models.ForeignKey(
        ExternalUser, on_delete=models.DO_NOTHING, db_column="seller_id", related_name="+"
    )
    delivery_method = models.CharField(max_length=20, blank=True)
    status = models.CharField(max_length=30, blank=True)
    payment_status = models.CharField(max_length=20, blank=True)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    shipping_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    platform_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    no_show_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    tracking_number = models.CharField(max_length=120, blank=True)
    courier_name = models.CharField(max_length=120, blank=True)
    delivery_zone = models.CharField(max_length=20, blank=True)
    checkout_group_code = models.CharField(max_length=32, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "marketplace_breederreservation"


class ExternalReservationDispute(models.Model):
    id = models.BigAutoField(primary_key=True)
    reservation = models.ForeignKey(
        ExternalBreederReservation, on_delete=models.DO_NOTHING, db_column="reservation_id", related_name="+"
    )
    opened_by = models.ForeignKey(
        ExternalUser, on_delete=models.DO_NOTHING, db_column="opened_by_id", related_name="+"
    )
    reason = models.CharField(max_length=30, blank=True)
    description = models.TextField(blank=True)
    breeder_response = models.TextField(blank=True)
    resolution = models.CharField(max_length=50, blank=True)
    resolution_summary = models.TextField(blank=True)
    status = models.CharField(max_length=30, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    breeder_responded_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "marketplace_reservationdispute"


class ExternalFeatureDAuditLog(models.Model):
    id = models.UUIDField(primary_key=True)
    entity_id = models.CharField(max_length=255)
    entity_type = models.CharField(max_length=32, blank=True)
    badge_type = models.CharField(max_length=64, blank=True)
    action = models.CharField(max_length=64, blank=True)
    action_reason = models.TextField(blank=True)
    previous_state = models.JSONField(default=dict, blank=True, null=True)
    new_state = models.JSONField(default=dict, blank=True, null=True)
    evidence_data = models.JSONField(default=dict, blank=True)
    triggered_by = models.CharField(max_length=255, blank=True)
    triggered_by_type = models.CharField(max_length=32, blank=True)
    timestamp = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "badges_badgeauditlog"


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

    @property
    def risk_bucket(self):
        return (self.decision_basis or {}).get("risk_bucket", "")

    @property
    def risk_badge_class(self):
        return {
            "low": "ok",
            "medium": "info",
            "high": "warn",
            "critical": "danger",
        }.get(self.risk_bucket, "muted")


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
