"""Placeholder migration to match previously-applied schema changes.

This migration exists because an earlier version of the project created 0002
on the database. The actual schema changes are now folded into 0001_initial.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("admin_portal", "0001_initial"),
    ]

    operations = [
        # No-op: schema already matches from 0001_initial rebuild
    ]
