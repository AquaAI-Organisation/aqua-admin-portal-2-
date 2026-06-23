"""Detect platform (aquaai.uk) logins by reading the shared django_session table.

DSAR identity verification relies on the requester logging in to their real
account at aquaai.uk. Django creates a fresh session (with a new session key)
on every successful login, storing the authenticated user id inside the signed
session blob.

AD-2: when ``PLATFORM_SECRET_KEY`` is configured we VERIFY each session blob's
signature with the platform's signing key before trusting the user id, so a
tampered or forged ``django_session`` row cannot be used to spoof a DSAR
identity. If the key is not provisioned yet we fall back to an unverified decode
(legacy behaviour) and log a warning.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import zlib

from django.conf import settings
from django.contrib.sessions.serializers import JSONSerializer
from django.core import signing
from django.utils import timezone

from ..models import PlatformSession

logger = logging.getLogger(__name__)

# Django signs db-backed session blobs with SECRET_KEY under this salt
# (``"django.contrib.sessions." + SessionStore.__qualname__``).
_SESSION_SALT = "django.contrib.sessions.SessionStore"


def _platform_secret_key() -> str:
    return (getattr(settings, "PLATFORM_SECRET_KEY", "") or "").strip()


def _verified_session_user_id(session_data: str) -> str | None:
    """Return ``_auth_user_id`` only if the blob's signature verifies against the
    platform key. Returns None on a missing key or any signature/format failure."""
    key = _platform_secret_key()
    if not key or not session_data:
        return None
    try:
        obj = signing.loads(
            session_data, key=key, salt=_SESSION_SALT, serializer=JSONSerializer
        )
    except (signing.BadSignature, ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    uid = obj.get("_auth_user_id")
    return str(uid) if uid is not None else None


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
    """Session keys of the subject's currently-valid platform sessions.

    With ``PLATFORM_SECRET_KEY`` set, only sessions whose signature verifies are
    counted (AD-2). Without it, falls back to the legacy unverified decode and
    warns once."""
    if not user_id:
        return set()
    target = str(user_id)
    keys: set[str] = set()
    now = timezone.now()

    if _platform_secret_key():
        decode = _verified_session_user_id
    else:
        decode = _decode_session_user_id
        logger.warning(
            "PLATFORM_SECRET_KEY not set — DSAR identity uses an UNVERIFIED session "
            "decode. Provision the admin-service secret 'PLATFORM_SECRET_KEY' "
            "(= the platform's SECRET_KEY) to enforce signature verification (AD-2)."
        )

    rows = PlatformSession.objects.filter(expire_date__gt=now).only("session_key", "session_data")
    for row in rows.iterator():
        if decode(row.session_data) == target:
            keys.add(row.session_key)
    return keys


def has_new_login_since_baseline(user_id, baseline_keys) -> bool:
    """True if the subject has a live session whose key was not in the baseline,
    i.e. they logged in afresh after the DSAR request was raised."""
    current = active_session_keys_for_user(user_id)
    return bool(current - set(baseline_keys or []))


def has_jwt_login_since(user_id, since) -> bool:
    """True if the subject obtained a new JWT after ``since`` — i.e. they logged
    in via the mobile app / API after the DSAR request was raised.

    The platform uses SimpleJWT, which writes one OutstandingToken row per issued
    refresh token (per login). Mobile/API logins never touch ``django_session``,
    so this is the authoritative signal for confirming a DSAR requester's identity.
    The row is created server-side by the platform on token issuance, so its
    ``user_id`` column is trusted at the same level as the shared database."""
    if not user_id or since is None:
        return False
    from ..models import ExternalOutstandingToken

    return ExternalOutstandingToken.objects.filter(
        user_id=user_id, created_at__gt=since
    ).exists()


def has_new_platform_login(user_id, baseline_keys, since) -> bool:
    """Confirm a fresh aquaai.uk login by EITHER mechanism: a new web session
    (``django_session``) or a new JWT issued after ``since`` (mobile app / API)."""
    return has_new_login_since_baseline(user_id, baseline_keys) or has_jwt_login_since(user_id, since)
