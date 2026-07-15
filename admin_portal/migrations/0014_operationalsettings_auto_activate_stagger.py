from django.db import migrations, models

import admin_portal.models


class Migration(migrations.Migration):

    dependencies = [
        ("admin_portal", "0013_operationalsettings_dsar_auto_send_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="operationalsettings",
            name="auto_activate_stagger_enabled",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "When enabled (with automatic activation on), each new account is still "
                    "auto-approved, but only after a different, human-looking delay taken from the "
                    "approval-delay schedule below. The account stays pending during the wait, so "
                    "approvals look manual while remaining fully automatic."
                ),
            ),
        ),
        migrations.AddField(
            model_name="operationalsettings",
            name="auto_activate_delay_schedule",
            field=models.JSONField(
                blank=True,
                default=admin_portal.models.default_stagger_schedule,
                help_text=(
                    "Repeating sequence of per-account approval delays in minutes. Each new account "
                    "takes the next value; the sequence recycles once exhausted."
                ),
            ),
        ),
        migrations.AddField(
            model_name="operationalsettings",
            name="auto_activate_stagger_cursor",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Internal pointer into the delay schedule for the next new account.",
            ),
        ),
    ]
