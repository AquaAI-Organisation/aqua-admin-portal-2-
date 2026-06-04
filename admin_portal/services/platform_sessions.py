"""Detect platform (aquaai.uk) logins by reading the shared django_session table.

DSAR identity verification relies on the requester logging in to their real
account at aquaai.uk. Django creates a fresh session (with a new session key)
on every successful login, storing the authenticated user id inside the signed
session blob. We decode that blob (no secret required — we only read the user
id, we do not need to verify the signature because the row lives in the trusted
shared database) to tell whether a given user has a live session.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import zlib

from django.utils import timezone

from ..models import PlatformSession

logger = logging.getLogger(__name__)


def _decode_session_user_id(session_data: str) -> str | None:
    """Pull `_auth_user_id` out of a Django session blob without the SECRET_KEY.

    Format: ``[.]<urlsafe_b64(zlib(json))>:<timestamp>:<signature>``. The leading
    dot marks zlib compression. We read only the payload segment.
    """
    if not session_data:
        return None
    try:
        payload = session_data.split(":", 1)[0]
        compressed = payload.startswith(".")
        if compressed:
            payload = payload[1:]
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        if compressed:
            raw = zlib.decompress(raw)
        obj = json.loads(raw)
        uid = obj.get("_auth_user_id")
        return str(uid) if uid is not None else None
    except (ValueError, binascii.Error, zlib.error, TypeError):
        return None


def active_session_keys_for_user(user_id) -> set[str]:
    """Session keys of the subject's currently-valid platform sessions."""
    if not user_id:
        return set()
    target = str(user_id)
    keys: set[str] = set()
    now = timezone.now()
    rows = PlatformSession.objects.filter(expire_date__gt=now).only("session_key", "session_data")
    for row in rows.iterator():
        if _decode_session_user_id(row.session_data) == target:
            keys.add(row.session_key)
    return keys


def has_new_login_since_baseline(user_id, baseline_keys) -> bool:
    """True if the subject has a live session whose key was not in the baseline,
    i.e. they logged in afresh after the DSAR request was raised."""
    current = active_session_keys_for_user(user_id)
    return bool(current - set(baseline_keys or []))
