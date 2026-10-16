"""Transactional outbox for Admin -> CRM events (spec §17.3, §18).

Nothing is ever sent inline from a web request or a review cycle. The business
change and the outbox row commit together; a separate dispatcher drains the
table. That is what makes the protocol at-least-once on the wire and
exactly-once in effect, and it is why a CRM outage can never roll back a
decision an operator has already been shown.
"""
from __future__ import annotations

import json
import logging

import requests
from django.db import transaction
from django.utils import timezone

from ..models import V2OutboxEvent
from . import contract as c

logger = logging.getLogger(__name__)

#: Matches the CRM's own dispatcher budget; keeps one run bounded.
DEFAULT_BATCH = 50
REQUEST_TIMEOUT_SECONDS = 20


def serialise(event: dict) -> str:
    """The exact text we will sign and send.

    Compact and deterministic: the signature covers these bytes, so whatever is
    stored here must be transmitted unchanged. ``ensure_ascii=False`` keeps
    non-ASCII business names as UTF-8 rather than escapes — the CRM signs the
    raw bytes either way, but the readable form is what an operator sees in the
    ledger.
    """
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def enqueue(event: dict) -> V2OutboxEvent:
    """Write one outbox row. Call inside the caller's transaction.

    Idempotent on ``event_id``: re-enqueuing the same event returns the
    existing row rather than creating a second delivery.
    """
    raw_body = serialise(event)
    existing = V2OutboxEvent.objects.filter(event_id=event["event_id"]).first()
    if existing is not None:
        return existing
    return V2OutboxEvent.objects.create(
        event_id=event["event_id"],
        event_type=event["event_type"],
        schema_version=event["schema_version"],
        correlation_id=event["correlation_id"],
        raw_body=raw_body,
    )


def crm_endpoint() -> str:
    from django.conf import settings
    import os
    value = (getattr(settings, "AQUA_CRM_V2_EVENTS_URL", "")
             or os.environ.get("AQUA_CRM_V2_EVENTS_URL", "") or "").strip()
    return value


def _mark_attempt(row: V2OutboxEvent, *, status_code: int | None, error: str,
                  delivered: bool, retryable: bool) -> None:
    now = timezone.now()
    row.attempt_count += 1
    row.last_attempt_at = now
    row.last_status_code = status_code
    row.last_error = error[:2000]
    if row.first_attempt_at is None:
        row.first_attempt_at = now

    if delivered:
        row.status = "delivered"
        row.delivered_at = now
        row.next_attempt_at = now
    elif not retryable:
        # 400/401/409/413/422 are sender or contract defects. Retrying the same
        # bytes cannot fix them, so they go straight to the dead-letter queue
        # for a human (spec §18).
        row.status = "dead_letter"
    else:
        nxt = c.next_retry_at(first_attempt_at=row.first_attempt_at,
                              attempt_count=row.attempt_count, now=now)
        if nxt is None:
            row.status = "dead_letter"
        else:
            row.status = "pending"
            row.next_attempt_at = nxt
    row.save(update_fields=[
        "attempt_count", "last_attempt_at", "last_status_code", "last_error",
        "first_attempt_at", "status", "delivered_at", "next_attempt_at",
    ])
    if row.status == "dead_letter":
        logger.error("v2 outbox dead-letter: %s %s (status=%s error=%s)",
                     row.event_type, row.event_id, status_code, error)
        # A dead letter means the CRM was never told something we decided. The
        # log line alone is not enough: this needs to reach the same place an
        # operator reviews everything else the system did.
        from ..services.audit import record_system

        record_system(
            "v2.outbox.dead_letter",
            target_type="review", target_id=row.correlation_id,
            event_id=row.event_id, event_type=row.event_type,
            attempts=row.attempt_count, last_status_code=status_code,
            last_error=error[:500],
        )


def deliver(row: V2OutboxEvent, *, session=None) -> bool:
    """Send one row. Returns True when the CRM accepted it."""
    endpoint = crm_endpoint()
    key_ring = c.key_ring_from_env("AQUA_ADMIN_TO_CRM_WEBHOOK_KEYS")
    if not endpoint or key_ring is None:
        _mark_attempt(row, status_code=None, error="delivery_not_configured",
                      delivered=False, retryable=True)
        return False
    if not endpoint.endswith(c.ADMIN_EVENTS_PATH):
        # Symmetrical to the CRM's own guard on our endpoint path: refuse to
        # send somewhere that cannot be the admin-events receiver.
        _mark_attempt(row, status_code=None, error="unexpected_crm_endpoint_path",
                      delivered=False, retryable=False)
        return False

    raw_body = row.raw_body.encode("utf-8")
    headers = c.signed_headers(key_ring=key_ring, event_id=row.event_id,
                               event_type=row.event_type, raw_body=raw_body)
    try:
        http = session or requests
        response = http.post(endpoint, data=raw_body, headers=headers,
                             timeout=REQUEST_TIMEOUT_SECONDS)
    except Exception as exc:
        _mark_attempt(row, status_code=None, error=f"network_error: {exc}",
                      delivered=False, retryable=True)
        return False

    delivered = response.status_code == 200
    _mark_attempt(row, status_code=response.status_code,
                  error="" if delivered else f"http_{response.status_code}",
                  delivered=delivered,
                  retryable=c.is_retryable_status(response.status_code))
    return delivered


def dispatch_due(*, limit: int = DEFAULT_BATCH, session=None) -> dict:
    """Drain due rows. Safe to run every minute from a scheduler."""
    now = timezone.now()
    counts = {"attempted": 0, "delivered": 0, "pending": 0, "dead_letter": 0}
    due = (V2OutboxEvent.objects
           .filter(status="pending", next_attempt_at__lte=now)
           .order_by("next_attempt_at")[:limit])
    for row in list(due):
        with transaction.atomic():
            locked = (V2OutboxEvent.objects
                      .select_for_update(skip_locked=True)
                      .filter(pk=row.pk, status="pending")
                      .first())
            if locked is None:
                continue
            counts["attempted"] += 1
            if deliver(locked, session=session):
                counts["delivered"] += 1
            elif locked.status == "dead_letter":
                counts["dead_letter"] += 1
            else:
                counts["pending"] += 1
    return counts
