from django import forms
from django.contrib.auth.forms import AuthenticationForm

from .models import AdminInvite, ROLE_CHOICES


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
        help_text="Guest = read-only. Developer = read + write (updates notify super-admins).",
    )

    class Meta:
        model = AdminInvite
        fields = ["email", "full_name"]
        widgets = {
            "email": forms.EmailInput(attrs={"placeholder": "new.admin@example.com"}),
            "full_name": forms.TextInput(attrs={"placeholder": "Full name (optional)"}),
        }


class ChangeRoleForm(forms.Form):
    ASSIGNABLE_ROLES = [c for c in ROLE_CHOICES if c[0] != "super_admin"]
    role = forms.ChoiceField(
        choices=ASSIGNABLE_ROLES,
        help_text="Guest = read-only. Developer = read + write with notifications.",
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
    new_decision = forms.ChoiceField(choices=DECISION_CHOICES, widget=forms.RadioSelect)
    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Reason for overriding the AI decision..."}),
        required=True,
        min_length=10,
    )


class AcceptInviteForm(forms.Form):
    full_name = forms.CharField(max_length=200, required=False)
    password1 = forms.CharField(label="Password", widget=forms.PasswordInput, min_length=10)
    password2 = forms.CharField(label="Confirm password", widget=forms.PasswordInput, min_length=10)

    def clean(self):
        data = super().clean()
        if data.get("password1") != data.get("password2"):
            raise forms.ValidationError("Passwords do not match.")
        return data
