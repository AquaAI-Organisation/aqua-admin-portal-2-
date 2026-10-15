"""The Admin Portal's CRM-dossier receiver.

``POST /api/integrations/v2/crm-events`` — the other half of the signed
contract. Deliberately plain Django: the signature is computed over the exact
bytes received, so nothing may parse and re-serialise the body before
verification (spec §6.2). A framework that consumes ``request.body`` into a
parsed structure first would break that.

Order of checks mirrors the CRM's own receiver
(``server/aquaV2IntegrationRoutes.ts``:41-107) so that a malformed request gets
the same status code from both sides and the shared staging fixtures agree.
"""
from __future__ import annotations

import json
import logging

from django.db import DatabaseError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from ..models import AIAccountReview, V2InboxEvent
from . import contract as c
from .schema import Invalid, validate_dossier_event

logger = logging.getLogger(__name__)

PRODUCER_SYSTEM = "aqua-provider-crm"


def _fail(status: int, error: str, **extra) -> JsonResponse:
    return JsonResponse({"ok": False, "error": error, **extra}, status=status)


def _header(request, name: str) -> str | None:
    value = request.headers.get(name)
    return value if isinstance(value, str) else None


@csrf_exempt
@require_POST
def crm_events(request):
    # 1. Content type, exactly as the CRM demands it (routes.ts:42-45).
    content_type = (_header(request, "content-type") or "").strip().lower()
    if content_type != c.CONTENT_TYPE:
        return _fail(400, "invalid_content_type")

    # 2. Raw bytes. Django has already read the body; we never re-encode it.
    raw_body: bytes = request.body
    if len(raw_body) > c.MAX_BODY_BYTES:
        return _fail(413, "payload_too_large")

    # 3. Signature, before anything parses the payload.
    verification = c.verify(
        key_ring=c.key_ring_from_env("AQUA_CRM_TO_ADMIN_WEBHOOK_KEYS"),
        timestamp_ms=_header(request, "x-aqua-timestamp-ms"),
        key_id=_header(request, "x-aqua-key-id"),
        signature=_header(request, "x-aqua-signature"),
        raw_body=raw_body,
    )
    if not verification.valid:
        # A signature failure must raise an alert, not just a status code
        # (spec §20.1 item 6).
        logger.warning("v2 inbox rejected: %s (key_id=%s)",
                       verification.error, _header(request, "x-aqua-key-id"))
        return _fail(verification.status, verification.error)

    # 4. Parse, then validate.
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return _fail(422, "invalid_json")
    try:
        event = validate_dossier_event(payload)
    except Invalid as exc:
        logger.warning("v2 inbox invalid payload: %s", exc)
        return _fail(422, "invalid_payload", detail=str(exc))

    # 5. Headers must agree with the envelope (routes.ts:71-78).
    for header_name, expected in (
        ("x-aqua-event", event["event_type"]),
        ("x-aqua-schema-version", event["schema_version"]),
        ("x-aqua-event-id", event["event_id"]),
    ):
        if _header(request, header_name) != expected:
            return _fail(400, "header_body_mismatch")

    # 6. Environments must match (spec §5). The CRM does not enforce this on
    # its own inbound path, so a mismatch here is far more likely to be a
    # misconfigured staging host than an attack — it is still refused, because
    # staging evidence must never land on a production case.
    expected_environment = c.key_ring_environment()
    if expected_environment and event["producer"]["environment"] != expected_environment:
        logger.warning("v2 inbox environment mismatch: sender=%s receiver=%s",
                       event["producer"]["environment"], expected_environment)
        return _fail(422, "environment_mismatch")

    try:
        result = _ingest(event, raw_body, verification.key_id)
    except DatabaseError:
        logger.exception("v2 inbox persistence failed")
        return _fail(503, "delivery_persistence_unavailable")

    if result["status"] in ("event_id_payload_mismatch",):
        return _fail(409, result["status"],
                     event_id=event["event_id"], correlation_id=event["correlation_id"])
    if result["status"] == "unknown_application":
        return _fail(422, "unknown_application",
                     event_id=event["event_id"], correlation_id=event["correlation_id"])

    return JsonResponse({
        "ok": True,
        "status": result["status"],
        "event_id": event["event_id"],
        "correlation_id": event["correlation_id"],
        "received_at": c.rfc3339_ms(timezone.now()),
    }, status=200)


def _ingest(event: dict, raw_body: bytes, signature_key_id: str) -> dict:
    """Record the event, then apply it. Idempotent per spec §17.1.

    The ledger row is written first and in the same transaction as the business
    effect, so "recorded" can never be true while the dossier was lost.
    """
    body_hash = c.sha256_hex(raw_body)
    event_id = event["event_id"]

    with transaction.atomic():
        existing = (V2InboxEvent.objects
                    .select_for_update()
                    .filter(producer_system=PRODUCER_SYSTEM, event_id=event_id)
                    .first())
        if existing is not None:
            if existing.body_sha256 != body_hash:
                # Same event ID, different body: a contract violation worth an
                # integration alert, never a silent overwrite.
                logger.error("v2 inbox event_id reuse with changed body: %s", event_id)
                return {"status": "event_id_payload_mismatch"}
            return {"status": "already_recorded"}

        data = event["data"]
        dossier = data["dossier"]
        review = (AIAccountReview.objects
                  .select_for_update()
                  .filter(subject_id=data["application_id"])
                  .first())

        record = V2InboxEvent.objects.create(
            producer_system=PRODUCER_SYSTEM,
            event_id=event_id,
            event_type=event["event_type"],
            schema_version=event["schema_version"],
            correlation_id=event["correlation_id"],
            body_sha256=body_hash,
            raw_payload=raw_body.decode("utf-8"),
            signature_key_id=signature_key_id,
        )

        if review is None:
            # Spec §17.1: a dossier for an unknown or erased profile is a 422
            # that the CRM must not retry. The ledger still keeps the evidence
            # that it arrived.
            record.processing_state = "failed"
            record.response_code = 422
            record.error = "unknown_application"
            record.save(update_fields=["processing_state", "response_code", "error"])
            return {"status": "unknown_application"}

        # A later revision supersedes an earlier one; an earlier revision that
        # arrives late must never overwrite newer evidence.
        incoming_revision = int(dossier["revision"])
        if review.dossier_revision is not None and incoming_revision < review.dossier_revision:
            record.processing_state = "processed"
            record.error = "superseded_revision"
            record.save(update_fields=["processing_state", "error"])
            return {"status": "already_superseded"}

        review.dossier_id = dossier["dossier_id"]
        review.dossier_revision = incoming_revision
        review.dossier_status = dossier["status"]
        review.dossier_payload = dossier
        review.dossier_received_at = timezone.now()
        if review.review_state in ("submitted_to_crm", "awaiting_crm_evidence"):
            review.review_state = "evidence_received"
        review.save(update_fields=[
            "dossier_id", "dossier_revision", "dossier_status", "dossier_payload",
            "dossier_received_at", "review_state",
        ])

        record.processing_state = "processed"
        record.save(update_fields=["processing_state"])
        return {"status": "recorded"}
