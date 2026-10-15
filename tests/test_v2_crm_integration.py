"""V2.3 CRM <-> Admin integration, end to end.

The fixtures in ``crm_dossiers.json`` were produced by the CRM's **own**
``createAquaV2SignedHeaders`` and validated by its **own**
``aquaV2CrmDossierEventSchema`` (aqua-provider-crm @ b367071), so what is
exercised here is genuine interop rather than this repository agreeing with
itself.

Where a test needs a fresh delivery timestamp it re-signs the CRM's unmodified
body with our signer; the CRM's original signature is separately proven to
verify under our verifier with the clock pinned, which is what shows the two
implementations agree.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta, timezone

PORTAL = str(pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, PORTAL)

import os  # noqa: E402

os.environ["DJANGO_SETTINGS_MODULE"] = "aqua_admin.settings"
os.environ["DATABASE_URL"] = "sqlite:///" + tempfile.mkstemp(suffix=".sqlite3")[1]
os.environ["INBOX_AUTOREFRESH"] = "false"

import django  # noqa: E402

django.setup()

from django.test import override_settings  # noqa: E402
from django.utils import timezone as djtz  # noqa: E402

from admin_portal.integrations import contract as c  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "crm_dossiers.json"
CRM_SECRET = "c" * 64
CRM_KEY_ID = "crm-2026-10"
ADMIN_SECRET = "b" * 64
CRM_RING = json.dumps({"current": {"key_id": CRM_KEY_ID, "secret": CRM_SECRET}})
ADMIN_RING = json.dumps({"current": {"key_id": "admin-2026-10", "secret": ADMIN_SECRET}})
APPLICATION_ID = "3ebe41bf-5c83-4ad4-bf29-cc51f6f282c3"
PROVIDER_ID = "f091f448-d6aa-45b0-af95-42d3e5952dc7"
CRM_EVENTS_URL = "/api/integrations/v2/crm-events"

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{extra}]" if extra else ""))


def section(title):
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def load_cases():
    data = json.loads(FIXTURES.read_text())
    return {case["name"]: case for case in data["cases"]}


def post(client, body: bytes, headers: dict, content_type: str | None = None):
    """POST with explicit headers, as the CRM's fetch() would."""
    return client.post(
        CRM_EVENTS_URL,
        data=body,
        content_type=content_type if content_type is not None else c.CONTENT_TYPE,
        headers={k: v for k, v in headers.items() if k != "content-type"},
    )


def resign(body: bytes, event: dict, *, secret=CRM_SECRET, key_id=CRM_KEY_ID,
           timestamp_ms: str | None = None, overrides: dict | None = None) -> dict:
    """Headers for an unmodified CRM body, signed now so the window is fresh."""
    timestamp_ms = timestamp_ms or str(int(djtz.now().timestamp() * 1000))
    headers = {
        "x-aqua-event": event["event_type"],
        "x-aqua-schema-version": event["schema_version"],
        "x-aqua-event-id": event["event_id"],
        "x-aqua-timestamp-ms": timestamp_ms,
        "x-aqua-key-id": key_id,
        "x-aqua-signature": c.signature_header(secret, timestamp_ms, body),
    }
    headers.update(overrides or {})
    return headers


@override_settings(
    AQUA_CRM_TO_ADMIN_WEBHOOK_KEYS=CRM_RING,
    AQUA_ADMIN_TO_CRM_WEBHOOK_KEYS=ADMIN_RING,
    AQUA_V2_ENVIRONMENT="staging",
    SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False,
)
def main():
    from django.test import Client
    from admin_portal.models import AIAccountReview, V2InboxEvent, V2OutboxEvent

    cases = load_cases()
    rev1, rev2 = cases["rev1"], cases["rev2"]
    body1 = rev1["body"].encode("utf-8")
    body2 = rev2["body"].encode("utf-8")
    event1 = json.loads(rev1["body"])
    event2 = json.loads(rev2["body"])
    client = Client()

    section("1. THE CRM'S OWN SIGNATURE VERIFIES UNDER OUR VERIFIER")
    pinned = datetime.fromtimestamp(
        int(rev1["headers"]["x-aqua-timestamp-ms"]) / 1000, tz=timezone.utc)
    verification = c.verify(
        key_ring=c.parse_key_ring(CRM_RING),
        timestamp_ms=rev1["headers"]["x-aqua-timestamp-ms"],
        key_id=rev1["headers"]["x-aqua-key-id"],
        signature=rev1["headers"]["x-aqua-signature"],
        raw_body=body1, now=pinned)
    check("a CRM-generated signature is accepted", verification.valid,
          verification.error or "valid")
    check("and it resolves to the current key", verification.key_kind == "current")

    tampered = c.verify(
        key_ring=c.parse_key_ring(CRM_RING),
        timestamp_ms=rev1["headers"]["x-aqua-timestamp-ms"],
        key_id=rev1["headers"]["x-aqua-key-id"],
        signature=rev1["headers"]["x-aqua-signature"],
        raw_body=body1 + b" ", now=pinned)
    check("one extra byte in the body invalidates it",
          not tampered.valid and tampered.error == "invalid_signature", tampered.error)

    section("2. UNKNOWN APPLICATION IS REFUSED, NOT RETRIED")
    response = post(client, body1, resign(body1, event1))
    check("dossier for an unknown profile returns 422", response.status_code == 422,
          str(response.status_code))
    check("with the documented error code",
          response.json().get("error") == "unknown_application", response.content[:120])
    check("and the arrival is still recorded in the ledger",
          V2InboxEvent.objects.filter(event_id=event1["event_id"]).exists())

    section("3. HAPPY PATH")
    V2InboxEvent.objects.all().delete()
    review = AIAccountReview.objects.create(
        subject_type="consultant", subject_id=APPLICATION_ID,
        subject_user_email="info@aquaticelements.co.uk",
        subject_display_name="Aquatic Elements Ltd", decision="pending",
        review_state="awaiting_crm_evidence",
    )
    response = post(client, body1, resign(body1, event1))
    check("a valid dossier returns 200", response.status_code == 200, str(response.status_code))
    payload = response.json()
    check("status is 'recorded'", payload.get("status") == "recorded", str(payload))
    check("the correlation id is echoed", payload.get("correlation_id") == APPLICATION_ID)
    review.refresh_from_db()
    check("the dossier is stored on the review", review.dossier_id == "dos_01JQA1KBGZQYTC4X2MXAERWKFM")
    check("revision recorded", review.dossier_revision == 1, str(review.dossier_revision))
    check("status recorded", review.dossier_status == "complete", review.dossier_status)
    check("the review advanced to evidence_received",
          review.review_state == "evidence_received", review.review_state)
    check("the full dossier is retained for the reviewer",
          review.dossier_payload.get("match_confidence") == "high")

    section("4. IDEMPOTENCY (spec §17.1)")
    response = post(client, body1, resign(body1, event1))
    check("an identical replay returns 200", response.status_code == 200, str(response.status_code))
    check("and reports already_recorded",
          response.json().get("status") == "already_recorded", str(response.json()))
    check("without creating a second ledger row",
          V2InboxEvent.objects.filter(event_id=event1["event_id"]).count() == 1)

    mutated = dict(event1)
    mutated["occurred_at"] = "2026-10-15T15:11:43.600Z"
    mutated_body = json.dumps(mutated).encode("utf-8")
    response = post(client, mutated_body, resign(mutated_body, event1))
    check("the same event id with a changed body returns 409",
          response.status_code == 409, str(response.status_code))
    check("with error event_id_payload_mismatch",
          response.json().get("error") == "event_id_payload_mismatch", str(response.json()))

    section("5. REVISION ORDERING")
    response = post(client, body2, resign(body2, event2))
    check("a higher revision is accepted", response.status_code == 200, str(response.status_code))
    review.refresh_from_db()
    check("the review now holds revision 2", review.dossier_revision == 2, str(review.dossier_revision))
    check("and its status", review.dossier_status == "partial", review.dossier_status)

    stale = dict(event1)
    stale["event_id"] = c.new_ulid()
    stale_body = json.dumps(stale).encode("utf-8")
    response = post(client, stale_body, resign(stale_body, stale))
    check("a late, lower revision is not applied", response.status_code == 200,
          str(response.status_code))
    check("and reports already_superseded",
          response.json().get("status") == "already_superseded", str(response.json()))
    review.refresh_from_db()
    check("newer evidence is preserved", review.dossier_revision == 2, str(review.dossier_revision))

    section("6. TRANSPORT REJECTIONS")
    fresh = dict(event1)
    fresh["event_id"] = c.new_ulid()
    fresh_body = json.dumps(fresh).encode("utf-8")

    r = post(client, fresh_body, resign(fresh_body, fresh), content_type="application/json")
    check("wrong content-type -> 400 invalid_content_type",
          r.status_code == 400 and r.json()["error"] == "invalid_content_type", str(r.status_code))

    r = post(client, fresh_body, resign(fresh_body, fresh, timestamp_ms="1760544000"))
    check("ten-digit timestamp -> 400 invalid_timestamp_unit",
          r.status_code == 400 and r.json()["error"] == "invalid_timestamp_unit", str(r.json()))

    old = str(int((djtz.now() - timedelta(minutes=6)).timestamp() * 1000))
    r = post(client, fresh_body, resign(fresh_body, fresh, timestamp_ms=old))
    check("a timestamp outside the 5-minute window -> 401",
          r.status_code == 401 and r.json()["error"] == "timestamp_outside_window", str(r.json()))

    h = resign(fresh_body, fresh)
    h["x-aqua-signature"] = "v1=" + "0" * 64
    r = post(client, fresh_body, h)
    check("a bad signature -> 401 invalid_signature",
          r.status_code == 401 and r.json()["error"] == "invalid_signature", str(r.json()))

    r = post(client, fresh_body, resign(fresh_body, fresh, key_id="no-such-key"))
    check("an unknown key id -> 401 unknown_key_id",
          r.status_code == 401 and r.json()["error"] == "unknown_key_id", str(r.json()))

    h = resign(fresh_body, fresh)
    h["x-aqua-event-id"] = c.new_ulid()
    r = post(client, fresh_body, h)
    check("header/body event-id mismatch -> 400",
          r.status_code == 400 and r.json()["error"] == "header_body_mismatch", str(r.json()))

    oversize = b'{"padding":"' + b"x" * (c.MAX_BODY_BYTES + 10) + b'"}'
    r = post(client, oversize, resign(oversize, fresh))
    check("a body over 256 KB -> 413 payload_too_large",
          r.status_code == 413 and r.json()["error"] == "payload_too_large", str(r.status_code))

    wrong_env = dict(fresh)
    wrong_env["producer"] = {"system": "aqua-provider-crm", "environment": "production"}
    wrong_env_body = json.dumps(wrong_env).encode("utf-8")
    r = post(client, wrong_env_body, resign(wrong_env_body, wrong_env))
    check("a production dossier on a staging receiver -> 422 environment_mismatch",
          r.status_code == 422 and r.json()["error"] == "environment_mismatch", str(r.json()))

    extra = dict(fresh)
    extra["unexpected"] = True
    extra_body = json.dumps(extra).encode("utf-8")
    r = post(client, extra_body, resign(extra_body, extra))
    check("an unknown top-level field -> 422 invalid_payload",
          r.status_code == 422 and r.json()["error"] == "invalid_payload", str(r.json()))

    bad_enum = json.loads(rev1["body"])
    bad_enum["event_id"] = c.new_ulid()
    bad_enum["data"]["dossier"]["match_confidence"] = "very_high"
    bad_enum_body = json.dumps(bad_enum).encode("utf-8")
    r = post(client, bad_enum_body, resign(bad_enum_body, bad_enum))
    check("an out-of-contract enum value -> 422",
          r.status_code == 422 and r.json()["error"] == "invalid_payload", str(r.json()))

    r = client.get(CRM_EVENTS_URL)
    check("GET is refused", r.status_code == 405, str(r.status_code))

    section("7. KEY ROTATION")
    expires = c.rfc3339_ms(djtz.now() + timedelta(days=5))
    rotated = json.dumps({
        "current": {"key_id": "crm-2026-11", "secret": "d" * 64},
        "previous": {"key_id": CRM_KEY_ID, "secret": CRM_SECRET, "expires_at": expires},
    })
    with override_settings(AQUA_CRM_TO_ADMIN_WEBHOOK_KEYS=rotated):
        rot = dict(event1)
        rot["event_id"] = c.new_ulid()
        rot_body = json.dumps(rot).encode("utf-8")
        r = post(client, rot_body, resign(rot_body, rot))
        check("the previous key still verifies during overlap", r.status_code == 200,
              str(r.status_code))
        rot2 = dict(event1)
        rot2["event_id"] = c.new_ulid()
        rot2_body = json.dumps(rot2).encode("utf-8")
        r = post(client, rot2_body, resign(rot2_body, rot2, secret="d" * 64, key_id="crm-2026-11"))
        check("the current key verifies", r.status_code == 200, str(r.status_code))

    expired = json.dumps({
        "current": {"key_id": "crm-2026-11", "secret": "d" * 64},
        "previous": {"key_id": CRM_KEY_ID, "secret": CRM_SECRET,
                     "expires_at": c.rfc3339_ms(djtz.now() - timedelta(days=1))},
    })
    with override_settings(AQUA_CRM_TO_ADMIN_WEBHOOK_KEYS=expired):
        exp = dict(event1)
        exp["event_id"] = c.new_ulid()
        exp_body = json.dumps(exp).encode("utf-8")
        r = post(client, exp_body, resign(exp_body, exp))
        check("an expired previous key -> 401 expired_key_id",
              r.status_code == 401 and r.json()["error"] == "expired_key_id", str(r.json()))

    check("a previous key with no expiry is refused by the parser",
          c.parse_key_ring(json.dumps({
              "current": {"key_id": "a1", "secret": "a" * 64},
              "previous": {"key_id": "a0", "secret": "b" * 64}})) is None)
    check("an overlap beyond 30 days is refused",
          c.parse_key_ring(json.dumps({
              "current": {"key_id": "a1", "secret": "a" * 64},
              "previous": {"key_id": "a0", "secret": "b" * 64,
                           "expires_at": c.rfc3339_ms(djtz.now() + timedelta(days=31))}})) is None)
    check("a non-hex secret is refused",
          c.parse_key_ring(json.dumps({"current": {"key_id": "a1", "secret": "z" * 64}})) is None)
    check("no key ring configured -> 503",
          c.verify(key_ring=None, timestamp_ms="1760544000000", key_id="k",
                   signature="v1=x", raw_body=b"{}").status == 503)

    section("8. OUTBOX (spec §17.3, §18)")
    from admin_portal.integrations import outbox

    event = {
        "schema_version": "2.2", "event_id": c.new_ulid(),
        "event_type": "admin.provider.application.submitted",
        "occurred_at": c.rfc3339_ms(djtz.now()), "correlation_id": APPLICATION_ID,
        "producer": {"system": "aqua-admin-portal", "environment": "staging"},
        "data": {"application": {}, "company": {}, "contact": {}, "declared_credentials": {}},
    }
    row = outbox.enqueue(event)
    check("enqueue writes one row", V2OutboxEvent.objects.count() == 1)
    check("re-enqueuing the same event id does not duplicate it",
          outbox.enqueue(event).pk == row.pk and V2OutboxEvent.objects.count() == 1)
    check("the stored body is exactly what will be signed",
          row.raw_body == outbox.serialise(event))

    class Resp:
        def __init__(self, status): self.status_code = status

    class Session:
        def __init__(self, status): self.status, self.calls = status, []

        def post(self, url, data=None, headers=None, timeout=None):
            self.calls.append({"url": url, "data": data, "headers": headers})
            return Resp(self.status)

    with override_settings(AQUA_CRM_V2_EVENTS_URL="https://crm.aquaai.uk" + c.ADMIN_EVENTS_PATH):
        session = Session(200)
        counts = outbox.dispatch_due(session=session)
        check("a 200 marks the event delivered", counts["delivered"] == 1, str(counts))
        row.refresh_from_db()
        check("status is delivered", row.status == "delivered", row.status)
        sent = session.calls[0]
        check("it was sent to the admin-events path",
              sent["url"].endswith(c.ADMIN_EVENTS_PATH), sent["url"])
        check("the exact stored bytes were transmitted",
              sent["data"] == row.raw_body.encode("utf-8"))
        check("all seven contract headers are present",
              {"content-type", "x-aqua-event", "x-aqua-schema-version", "x-aqua-event-id",
               "x-aqua-timestamp-ms", "x-aqua-key-id", "x-aqua-signature"} == set(sent["headers"]),
              str(sorted(sent["headers"])))
        check("the signature verifies against what was sent",
              c.verify(key_ring=c.parse_key_ring(ADMIN_RING),
                       timestamp_ms=sent["headers"]["x-aqua-timestamp-ms"],
                       key_id=sent["headers"]["x-aqua-key-id"],
                       signature=sent["headers"]["x-aqua-signature"],
                       raw_body=sent["data"]).valid)

        retry_event = dict(event, event_id=c.new_ulid())
        retry_row = outbox.enqueue(retry_event)
        outbox.dispatch_due(session=Session(503))
        retry_row.refresh_from_db()
        check("a 503 keeps the event pending for retry", retry_row.status == "pending",
              retry_row.status)
        check("and schedules the next attempt in the future",
              retry_row.next_attempt_at > djtz.now())

        fatal_event = dict(event, event_id=c.new_ulid())
        fatal_row = outbox.enqueue(fatal_event)
        outbox.dispatch_due(session=Session(422))
        fatal_row.refresh_from_db()
        check("a 422 goes straight to dead letter, not a retry loop",
              fatal_row.status == "dead_letter", fatal_row.status)

    section("9. RETRY SCHEDULE MATCHES THE CONTRACT")
    start = djtz.now()
    expected = [1, 5, 15, 60, 240, 360]
    actual = []
    for attempt in range(6):
        nxt = c.next_retry_at(first_attempt_at=start, attempt_count=attempt, now=start)
        actual.append(round((nxt - start).total_seconds() / 60))
    check("delays are 1m, 5m, 15m, 1h, 4h then 6h", actual == expected, str(actual))
    check("after 24 hours the event dead-letters",
          c.next_retry_at(first_attempt_at=start, attempt_count=9,
                          now=start + timedelta(hours=24)) is None)
    check("a retry never overshoots the 24-hour deadline",
          c.next_retry_at(first_attempt_at=start, attempt_count=5,
                          now=start + timedelta(hours=23)) <= start + timedelta(hours=24))

    print(f"\n{'=' * 74}\nRESULT: {len(PASS)} passed, {len(FAIL)} failed\n{'=' * 74}")
    for name in FAIL:
        print("  FAILED:", name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    from django.test.runner import DiscoverRunner
    from django.test.utils import setup_test_environment, teardown_test_environment

    setup_test_environment()
    runner = DiscoverRunner(verbosity=0, interactive=False)
    config = runner.setup_databases()
    try:
        code = main()
    finally:
        runner.teardown_databases(config)
        teardown_test_environment()
    sys.exit(code)
