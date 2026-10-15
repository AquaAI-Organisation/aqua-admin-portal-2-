"""The V2.3 wire contract, mirrored from the CRM's own implementation.

This is a deliberate, line-by-line port of
``aqua-provider-crm/shared/aquaV2Integration.ts`` (read at commit b367071). The
two sides must agree byte-for-byte or every signature fails, so each rule below
cites the TypeScript it mirrors. Do not "improve" anything here without changing
the CRM in the same release.

The single most dangerous detail: the shared secret is validated as 64 hex
characters but is used by Node as a UTF-8 **string** key --

    crypto.createHmac("sha256", secret)        // aquaV2Integration.ts:345

so the HMAC key is the 64 ASCII bytes of the hex text, NOT the 32 bytes it
decodes to. ``bytes.fromhex(secret)`` would be wrong and would fail every
signature.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# -- constants (aquaV2Integration.ts:4-18) ----------------------------------
SCHEMA_VERSION = "2.2"
ADMIN_EVENTS_PATH = "/api/integrations/v2/admin-events"      # Admin -> CRM
CRM_EVENTS_PATH = "/api/integrations/v2/crm-events"          # CRM -> Admin
MAX_BODY_BYTES = 256 * 1024
TIMESTAMP_TOLERANCE_MS = 5 * 60 * 1000

ADMIN_EVENT_TYPES = (
    "admin.provider.application.submitted",
    "admin.provider.application.decision.recorded",
    "admin.provider.evidence.requested",
    "admin.provider.account.status_changed",
    "admin.provider.erased",
)
CRM_EVENT_TYPES = ("crm.provider.evidence_dossier.ready",)

#: Exactly what the CRM's route demands (aquaV2IntegrationRoutes.ts:43); it
#: lowercases and trims before comparing, and anything else is 400.
CONTENT_TYPE = "application/json; charset=utf-8"

# -- patterns (aquaV2Integration.ts:24-27, 269-270) -------------------------
ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$", re.I)
UUID_V7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
RFC3339_MS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
TIMESTAMP_MS_RE = re.compile(r"^\d{13}$")
KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SECRET_RE = re.compile(r"^[a-f0-9]{64}$", re.I)


def is_event_id(value: str) -> bool:
    """event_id must be a ULID or a UUIDv7 (aquaV2Integration.ts:29-31)."""
    return bool(value) and (bool(ULID_RE.match(value)) or bool(UUID_V7_RE.match(value)))


def is_profile_id(value: str) -> bool:
    """application_id / aqua_provider_id / correlation_id (ts:33)."""
    return bool(value) and bool(UUID_RE.match(value))


def is_rfc3339_ms(value: str) -> bool:
    return bool(value) and bool(RFC3339_MS_RE.match(value))


def rfc3339_ms(moment: datetime) -> str:
    """Render a datetime the way the contract requires: UTC, exactly 3 decimals."""
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


# -- ULID (aquaV2Integration.ts:315-342) ------------------------------------
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid(now_ms: int | None = None, random_bytes: bytes | None = None) -> str:
    """Sortable ULID, same alphabet and layout as the CRM's generator."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000) if now_ms is None else now_ms
    if not (0 <= now_ms < 2 ** 48):
        raise ValueError("ULID timestamp is outside the supported range")
    random_bytes = secrets.token_bytes(10) if random_bytes is None else random_bytes
    if len(random_bytes) != 10:
        raise ValueError("ULID randomness must contain exactly 10 bytes")

    timestamp = now_ms
    time_part = ""
    for _ in range(10):
        time_part = _CROCKFORD[timestamp % 32] + time_part
        timestamp //= 32

    entropy_part = ""
    pending_bits = 0
    pending_value = 0
    for byte in random_bytes:
        pending_value = (pending_value << 8) | byte
        pending_bits += 8
        while pending_bits >= 5:
            pending_bits -= 5
            entropy_part += _CROCKFORD[(pending_value >> pending_bits) & 31]
    return time_part + entropy_part


# -- signing (aquaV2Integration.ts:311-355) ---------------------------------
def sha256_hex(raw_body: bytes) -> str:
    return hashlib.sha256(raw_body).hexdigest()


def sign(secret: str, timestamp_ms: str, raw_body: bytes) -> str:
    """HMAC-SHA256 over ``timestamp_ms + "." + raw_body``, hex digest.

    Mirrors aquaV2Integration.ts:344-350. The secret is keyed as UTF-8 text --
    see the module docstring.
    """
    mac = hmac.new(secret.encode("utf-8"), digestmod=hashlib.sha256)
    mac.update(f"{timestamp_ms}.".encode("utf-8"))
    mac.update(raw_body)
    return mac.hexdigest()


def signature_header(secret: str, timestamp_ms: str, raw_body: bytes) -> str:
    """The ``x-aqua-signature`` value, including the ``v1=`` prefix (ts:424)."""
    return f"v1={sign(secret, timestamp_ms, raw_body)}"


def signatures_equal(expected: str, actual: str | None) -> bool:
    """Constant-time compare of the whole header value (ts:352-355)."""
    if not actual or len(actual) != len(expected):
        return False
    return hmac.compare_digest(expected.encode("utf-8"), actual.encode("utf-8"))


# -- key rings (aquaV2Integration.ts:268-305) -------------------------------
@dataclass(frozen=True)
class SigningKey:
    key_id: str
    secret: str
    expires_at: str | None = None


@dataclass(frozen=True)
class KeyRing:
    current: SigningKey
    previous: SigningKey | None = None


def _parse_key(raw: object) -> SigningKey | None:
    if not isinstance(raw, dict):
        return None
    allowed = {"key_id", "secret", "expires_at"}
    if set(raw) - allowed:                       # keySchema is .strict()
        return None
    key_id, secret = raw.get("key_id"), raw.get("secret")
    expires_at = raw.get("expires_at")
    if not isinstance(key_id, str) or not KEY_ID_RE.match(key_id):
        return None
    if not isinstance(secret, str) or not SECRET_RE.match(secret):
        return None
    if expires_at is not None and (not isinstance(expires_at, str) or not is_rfc3339_ms(expires_at)):
        return None
    return SigningKey(key_id=key_id, secret=secret, expires_at=expires_at)


def parse_key_ring(value: str | None, *, now: datetime | None = None) -> KeyRing | None:
    """Parse ``{"current": {...}, "previous": {...}}``; None when unusable.

    Enforces the CRM's superRefine rules (ts:277-292): the two key IDs must
    differ, a previous key requires an expiry, and that expiry may not sit more
    than 30 days ahead.
    """
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict) or set(parsed) - {"current", "previous"}:
        return None

    current = _parse_key(parsed.get("current"))
    if current is None:
        return None
    previous = None
    if parsed.get("previous") is not None:
        previous = _parse_key(parsed.get("previous"))
        if previous is None:
            return None
        if previous.key_id == current.key_id:
            return None
        if not previous.expires_at:
            return None
        moment = now or datetime.now(timezone.utc)
        try:
            expires = datetime.strptime(previous.expires_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                tzinfo=timezone.utc)
        except ValueError:
            return None
        if expires - moment > timedelta(days=30):
            return None
    return KeyRing(current=current, previous=previous)


def key_ring_from_env(name: str, *, now: datetime | None = None) -> KeyRing | None:
    from django.conf import settings
    raw = getattr(settings, name, None) or os.environ.get(name)
    return parse_key_ring(raw, now=now)


ENVIRONMENTS = ("sandbox", "staging", "production")


def key_ring_environment() -> str:
    """This deployment's declared environment, or "" when unset.

    Goes into ``producer.environment`` on everything we send, and is what an
    inbound event's environment is checked against. Unset means the check is
    skipped, which is the right behaviour for a local test run but must never
    be the case in a deployed environment.
    """
    from django.conf import settings
    value = (getattr(settings, "AQUA_V2_ENVIRONMENT", "")
             or os.environ.get("AQUA_V2_ENVIRONMENT", "") or "").strip().lower()
    return value if value in ENVIRONMENTS else ""


# -- verification (aquaV2Integration.ts:361-404) ----------------------------
@dataclass(frozen=True)
class Verification:
    valid: bool
    key_id: str = ""
    key_kind: str = ""
    status: int = 0
    error: str = ""


def verify(*, key_ring: KeyRing | None, timestamp_ms: str | None, key_id: str | None,
           signature: str | None, raw_body: bytes, now: datetime | None = None) -> Verification:
    """Verify an inbound webhook. Status codes match the CRM's exactly."""
    if key_ring is None:
        return Verification(False, status=503, error="webhook_not_configured")
    if not timestamp_ms or not TIMESTAMP_MS_RE.match(timestamp_ms):
        return Verification(False, status=400, error="invalid_timestamp_unit")

    moment = now or datetime.now(timezone.utc)
    now_ms = int(moment.timestamp() * 1000)
    if abs(now_ms - int(timestamp_ms)) > TIMESTAMP_TOLERANCE_MS:
        return Verification(False, status=401, error="timestamp_outside_window")

    if not key_id:
        return Verification(False, status=401, error="missing_key_id")

    key, kind = None, ""
    if key_ring.current.key_id == key_id:
        key, kind = key_ring.current, "current"
    elif key_ring.previous is not None and key_ring.previous.key_id == key_id:
        key, kind = key_ring.previous, "previous"
    if key is None:
        return Verification(False, status=401, error="unknown_key_id")

    if kind == "previous":
        if not key.expires_at:
            return Verification(False, status=401, error="expired_key_id")
        expires = datetime.strptime(key.expires_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
        if expires <= moment:
            return Verification(False, status=401, error="expired_key_id")

    if not signatures_equal(signature_header(key.secret, timestamp_ms, raw_body), signature):
        return Verification(False, status=401, error="invalid_signature")

    return Verification(True, key_id=key.key_id, key_kind=kind)


def signed_headers(*, key_ring: KeyRing, event_id: str, event_type: str,
                   raw_body: bytes, timestamp_ms: str | None = None) -> dict[str, str]:
    """Headers for an outbound Admin -> CRM event (ts:406-426)."""
    timestamp_ms = timestamp_ms or str(int(datetime.now(timezone.utc).timestamp() * 1000))
    if not TIMESTAMP_MS_RE.match(timestamp_ms):
        raise ValueError("timestampMs must contain 13 decimal digits")
    return {
        "content-type": CONTENT_TYPE,
        "x-aqua-event": event_type,
        "x-aqua-schema-version": SCHEMA_VERSION,
        "x-aqua-event-id": event_id,
        "x-aqua-timestamp-ms": timestamp_ms,
        "x-aqua-key-id": key_ring.current.key_id,
        "x-aqua-signature": signature_header(key_ring.current.secret, timestamp_ms, raw_body),
    }


# -- retry policy (aquaV2Integration.ts:432-452) ----------------------------
RETRY_DELAYS_MS = (
    1 * 60 * 1000,
    5 * 60 * 1000,
    15 * 60 * 1000,
    60 * 60 * 1000,
    4 * 60 * 60 * 1000,
)
DEAD_LETTER_AFTER = timedelta(hours=24)


def is_retryable_status(status: int) -> bool:
    return status == 429 or status in (500, 502, 503, 504)


def next_retry_at(*, first_attempt_at: datetime, attempt_count: int,
                  now: datetime | None = None) -> datetime | None:
    """Next attempt time, or None once the 24-hour dead-letter point is reached."""
    moment = now or datetime.now(timezone.utc)
    if moment - first_attempt_at >= DEAD_LETTER_AFTER:
        return None
    delay_ms = RETRY_DELAYS_MS[attempt_count] if attempt_count < len(RETRY_DELAYS_MS) else 6 * 60 * 60 * 1000
    nxt = moment + timedelta(milliseconds=delay_ms)
    deadline = first_attempt_at + DEAD_LETTER_AFTER
    return deadline if nxt > deadline else nxt
