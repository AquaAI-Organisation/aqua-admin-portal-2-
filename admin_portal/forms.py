from django import forms
from django.contrib.auth.forms import AuthenticationForm

from .models import AdminInvite, OperationalSettings, ROLE_CHOICES


class EmailLoginForm(AuthenticationForm):
    username = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "email", "placeholder": "admin@example.com"}),
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password", "placeholder": "Password"}),
    )


class AdminInviteForm(forms.ModelForm):
    role = forms.ChoiceField(
        choices=[c for c in ROLE_CHOICES if c[0] != "super_admin"],
        initial="guest",
        help_text="Guest = read-only. Admin = moderation and operational control. Developer = standard write access with notifications.",
    )

    class Meta:
        model = AdminInvite
        fields = ["email", "full_name", "role"]
        widgets = {
            "email": forms.EmailInput(attrs={"placeholder": "new.admin@example.com"}),
            "full_name": forms.TextInput(attrs={"placeholder": "Full name (optional)"}),
        }


class ChangeRoleForm(forms.Form):
    ASSIGNABLE_ROLES = [c for c in ROLE_CHOICES if c[0] != "super_admin"]
    role = forms.ChoiceField(
        choices=ASSIGNABLE_ROLES,
        help_text="Guest = read-only. Admin = moderation and operational control. Developer = standard write access with notifications.",
    )


class ChangePasswordForm(forms.Form):
    current_password = forms.CharField(
        widget=forms.PasswordInput(attrs={"placeholder": "Current password"}),
        required=True,
    )
    new_password1 = forms.CharField(
        label="New password",
        widget=forms.PasswordInput(attrs={"placeholder": "New password (min 10 chars)"}),
        min_length=10,
    )
    new_password2 = forms.CharField(
        label="Confirm new password",
        widget=forms.PasswordInput(attrs={"placeholder": "Confirm new password"}),
        min_length=10,
    )

    def clean(self):
        data = super().clean()
        if data.get("new_password1") != data.get("new_password2"):
            raise forms.ValidationError("Passwords do not match.")
        return data


class FlagResolveForm(forms.Form):
    resolution_notes = forms.CharField(widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Describe how this was resolved..."}), required=True)


class ManualOverrideForm(forms.Form):
    DECISION_CHOICES = [
        ("approved", "Approve"),
        ("rejected", "Reject"),
    ]
    REASON_CHOICES = [
        ("compliance_verified", "Compliance or documents manually verified"),
        ("false_positive", "AI false positive"),
        ("business_confirmed", "Business legitimacy confirmed"),
        ("policy_violation", "Policy violation confirmed"),
        ("duplicate_or_test", "Duplicate, spam, or test account"),
        ("insufficient_context", "AI lacked enough context"),
        ("other", "Other reason"),
    ]
    new_decision = forms.ChoiceField(choices=DECISION_CHOICES, widget=forms.RadioSelect)
    reason_code = forms.ChoiceField(choices=REASON_CHOICES)
    other_reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Add the custom reason for this override..."}),
        required=False,
        min_length=10,
    )

    def clean(self):
        data = super().clean()
        code = data.get("reason_code")
        other_reason = (data.get("other_reason") or "").strip()
        if code == "other":
            if len(other_reason) < 10:
                raise forms.ValidationError("Please add a fuller explanation when choosing Other reason.")
            data["resolved_reason"] = other_reason
            return data
        label = dict(self.REASON_CHOICES).get(code, "")
        data["resolved_reason"] = label
        return data


class AcceptInviteForm(forms.Form):
    full_name = forms.CharField(max_length=200, required=False)
    password1 = forms.CharField(label="Password", widget=forms.PasswordInput, min_length=10)
    password2 = forms.CharField(label="Confirm password", widget=forms.PasswordInput, min_length=10)

    def clean(self):
        data = super().clean()
        if data.get("password1") != data.get("password2"):
            raise forms.ValidationError("Passwords do not match.")
        return data


class OperationalSettingsForm(forms.ModelForm):
    smtp_password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=True, attrs={"placeholder": "SMTP password or app password"}),
    )
    slack_bot_token = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=True, attrs={"placeholder": "xoxb-..."}),
    )
    imap_password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=True, attrs={"placeholder": "Mailbox password"}),
    )

    class Meta:
        model = OperationalSettings
        fields = [
            "smtp_host",
            "smtp_port",
            "smtp_use_tls",
            "smtp_username",
            "smtp_password",
            "default_from_email",
            "slack_bot_token",
            "slack_channel",
            "imap_host",
            "imap_port",
            "imap_use_ssl",
            "imap_username",
            "imap_password",
            "imap_folder",
        ]


class SupportReplyForm(forms.Form):
    body = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 6, "placeholder": "Write or edit the reply that should be sent to the enquiry."}),
        required=True,
        min_length=5,
    )
