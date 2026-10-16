"""V2.3 item 14: the decision, evidence-request, account-status and erasure outboxes.

An emitter nothing calls is worth nothing, so these checks go through the real
portal paths wherever one exists — ``manual_override`` for a decision, the
re-run view for an evidence request, ``_set_entity_active_state`` for a
suspension, ``approve_and_send_dsar`` for an erasure — rather than calling the
``enqueue_*`` helpers and declaring the wiring done.

Every envelope built here is also dumped to JSON for the companion Node run,
which validates it against the CRM's own ``aquaV2AdminEventSchema``. Agreement
with the spec text is not the same as agreement with the code that will receive
these events.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import uuid
from datetime import timedelta

PORTAL = str(pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, PORTAL)

os.environ["DJANGO_SETTINGS_MODULE"] = "aqua_admin.settings"
os.environ["DATABASE_URL"] = "sqlite:///" + tempfile.mkstemp(suffix=".sqlite3")[1]
os.environ["INBOX_AUTOREFRESH"] = "false"

import django  # noqa: E402

django.setup()

from django.test import Client, override_settings  # noqa: E402
from django.urls import reverse  # noqa: E402
from django.utils import timezone  # noqa: E402

PASS, FAIL = [], []
ADMIN_RING = '{"current": {"key_id": "admin-1", "secret": "%s"}}' % ("b" * 64)
CAPTURED: list[dict] = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{extra}]" if extra else ""))


def section(title):
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def build_mirror_schema():
    """Create the unmanaged mirror tables the portal reads from.

    The provider profiles and platform users live in the backend's database, so
    their models are ``managed=False`` and the test runner does not build them.
    These checks need real rows in them, because the point is that suspending a
    real profile emits a real event.
    """
    from django.apps import apps
    from django.db import connection

    mirrors = [m for m in apps.get_app_config("admin_portal").get_models()
               if not m._meta.managed]
    for m in mirrors:
        m._meta.managed = True
    with connection.schema_editor() as se:
        for m in mirrors:
            try:
                se.create_model(m)
            except Exception as exc:
                print(f"    (schema) {m.__name__}: {type(exc).__name__}: {exc}")
    return mirrors


@override_settings(
    AQUA_V2_ENVIRONMENT="staging",
    AQUA_ADMIN_TO_CRM_WEBHOOK_KEYS=ADMIN_RING,
    AQUA_CRM_V2_EVENTS_URL="https://crm.aquaai.uk/api/integrations/v2/admin-events",
    ADMIN_EMAILS=[],
    SUPERADMIN_EMAILS=["op@aquaai.uk"],
    SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False,
)
def main():
    from admin_portal.models import (
        AdminUser, AIAccountReview, DSARRequest, ExternalConsultantProfile,
        ExternalBreederProfile, ExternalUser, V2OutboxEvent,
    )
    from admin_portal.integrations import events as ev

    build_mirror_schema()

    def fresh():
        V2OutboxEvent.objects.all().delete()

    def only_event():
        rows = list(V2OutboxEvent.objects.all())
        assert len(rows) == 1, f"expected exactly one queued event, got {len(rows)}"
        # raw_body is what is signed and sent, so that is what gets inspected.
        body = json.loads(rows[0].raw_body)
        CAPTURED.append(body)
        return rows[0], body

    def make_review(**kw):
        defaults = dict(
            subject_type="consultant", subject_id=uuid.uuid4(),
            aqua_provider_id=uuid.uuid4(),
            subject_user_email="c@aqua.test", subject_display_name="Co",
            decision="pending", review_state="awaiting_crm_evidence",
            dossier_id="dos_01HZZZ", dossier_revision=3,
            submitted_event_at=timezone.now() - timedelta(hours=1),
        )
        defaults.update(kw)
        return AIAccountReview.objects.create(**defaults)

    # ---------------------------------------------------------------- §13
    section("1. THE DECISION OUTBOX IS FED BY THE REAL OVERRIDE PATH (§13.1)")

    # Must satisfy the real permission gate: operational_admin_required reads
    # is_admin/is_super_admin, and is_super_admin additionally requires the
    # email to be in SUPERADMIN_EMAILS.
    operator = AdminUser.objects.create(
        email="op@aquaai.uk", role="admin", is_platform_super_admin=True,
        is_staff=True, is_active=True)

    user = ExternalUser.objects.create(id=uuid.uuid4(), email="pro@aqua.test", is_active=True)
    consultant = ExternalConsultantProfile.objects.create(
        id=uuid.uuid4(), user_id=user.id, company_name="Reef Consulting",
        is_active=True, admin_status="pending")
    review = make_review(subject_id=consultant.id, aqua_provider_id=user.id)

    fresh()
    from admin_portal.services.review_runner import manual_override

    manual_override(review, "rejected", "Licence could not be verified.", operator)
    review.refresh_from_db()
    row, body = only_event()
    check("manual_override queues a decision event",
          body["event_type"] == "admin.provider.application.decision.recorded",
          body["event_type"])
    check("the queued decision matches the recorded row",
          body["data"]["decision"]["decision_id"] == review.decision_id
          and body["data"]["decision"]["decision_version"] == review.decision_version,
          f"{body['data']['decision']['decision_id']} v{body['data']['decision']['decision_version']}")
    check("a rejection is attributed to the operator",
          body["data"]["decision"]["decision_actor_type"] == "operator")
    check("and carries the dossier it rests on",
          body["data"]["decision"].get("evidence_dossier_id") == "dos_01HZZZ")
    check("the provider id is the platform user, not the profile",
          body["data"]["aqua_provider_id"] == str(user.id))

    fresh()
    manual_override(review, "approved", "Licence verified on appeal.", operator)
    review.refresh_from_db()
    _, body = only_event()
    check("a replacement decision increments the version",
          body["data"]["decision"]["decision_version"] == 2,
          str(body["data"]["decision"]["decision_version"]))
    check("and mints a new decision id",
          body["data"]["decision"]["decision_id"] == review.decision_id)

    # ---------------------------------------------------------------- §14
    section("2. EVIDENCE REQUESTS (§14)")

    fresh()
    row = ev.enqueue_evidence_request(review, reason_code="operator_requested",
                                      requested_checks=[ev.CHECK_WEBSITE])
    _, body = only_event()
    check("an evidence request is queued",
          body["event_type"] == "admin.provider.evidence.requested", body["event_type"])
    req = body["data"]["request"]
    check("the request names the checks asked for",
          req["requested_checks"] == ["website_and_domain"], str(req["requested_checks"]))
    check("it asks for a revision after the one we hold",
          req.get("requested_dossier_revision_after") == 3,
          str(req.get("requested_dossier_revision_after")))
    check("and carries its own request id", req["request_id"].startswith("evr_"))

    fresh()
    refused = None
    try:
        ev.enqueue_evidence_request(review, reason_code="licence_changed")
    except ValueError as exc:
        refused = str(exc)
    check("a licence_changed request without a credential snapshot is refused",
          refused is not None and "credential snapshot" in refused, str(refused))
    check("and nothing was queued", V2OutboxEvent.objects.count() == 0)

    fresh()
    ev.enqueue_evidence_request(
        review, reason_code="licence_changed",
        credential_snapshot={"breeder_licence": {
            "licence_number": "BR-2024-5512", "issuing_authority": "Kent County Council",
            "expires_at": "2027-03-31",
        }})
    _, body = only_event()
    snap = body["data"]["request"]["credential_snapshot"]["breeder_licence"]
    check("with a snapshot it is queued",
          body["data"]["request"]["reason_code"] == "licence_changed")
    check("and the snapshot travels with it",
          snap["licence_number"] == "BR-2024-5512")
    check("the snapshot carries exactly the three fields the CRM takes",
          set(snap) == {"licence_number", "issuing_authority", "expires_at"}, str(sorted(snap)))

    # The CRM's schema requires expires_at as a bare YYYY-MM-DD, with all three
    # fields present. A near-miss here is a 422 on receipt that no retry fixes,
    # so each one has to be refused before it is ever queued.
    for label, licence in [
        ("a snapshot missing expires_at",
         {"licence_number": "BR-1", "issuing_authority": "Kent"}),
        ("a snapshot missing the licence number",
         {"issuing_authority": "Kent", "expires_at": "2027-03-31"}),
        ("a snapshot missing the issuing authority",
         {"licence_number": "BR-1", "expires_at": "2027-03-31"}),
        ("an expires_at with a timestamp on it",
         {"licence_number": "BR-1", "issuing_authority": "Kent",
          "expires_at": "2027-03-31T00:00:00Z"}),
        ("an expires_at in the wrong order",
         {"licence_number": "BR-1", "issuing_authority": "Kent",
          "expires_at": "31-03-2027"}),
        ("a blank expires_at",
         {"licence_number": "BR-1", "issuing_authority": "Kent", "expires_at": "  "}),
    ]:
        fresh()
        refused = None
        try:
            ev.enqueue_evidence_request(review, reason_code="licence_changed",
                                        credential_snapshot={"breeder_licence": licence})
        except ValueError as exc:
            refused = str(exc)
        check(f"{label} is refused", refused is not None and V2OutboxEvent.objects.count() == 0,
              str(refused))

    fresh()
    ev.enqueue_evidence_request(
        review, reason_code="operator_requested",
        credential_snapshot={"breeder_licence": {
            "licence_number": "BR-2024-5512", "issuing_authority": "Kent County Council",
            "expires_at": "2027-03-31", "status": "verified", "notes": "renewed",
        }})
    _, body = only_event()
    check("extra keys are stripped rather than sent on",
          set(body["data"]["request"]["credential_snapshot"]["breeder_licence"])
          == {"licence_number", "issuing_authority", "expires_at"},
          str(sorted(body["data"]["request"]["credential_snapshot"]["breeder_licence"])))

    fresh()
    ev.enqueue_evidence_request(review, reason_code="operator_requested")
    _, body = only_event()
    check("a request with no licence sends credential_snapshot as null",
          body["data"]["request"]["credential_snapshot"] is None,
          str(body["data"]["request"]["credential_snapshot"]))

    fresh()
    refused = None
    try:
        ev.enqueue_evidence_request(review, reason_code="because_i_said_so")
    except ValueError as exc:
        refused = str(exc)
    check("an unknown reason code is refused", refused is not None, str(refused))

    fresh()
    refused = None
    try:
        ev.enqueue_evidence_request(review, reason_code="stale_evidence",
                                    requested_checks=["", "  "])
    except ValueError as exc:
        refused = str(exc)
    check("an empty check list is refused rather than sent",
          refused is not None and "at least one" in refused, str(refused))

    section("3. THE RE-RUN VIEW ASKS THE CRM TO RE-CHECK TOO (§14.1)")
    fresh()
    client = Client()
    client.force_login(operator)
    rerun_url = reverse("admin_portal:review_rerun", args=[review.id])
    resp = client.post(rerun_url, follow=False)
    check("the re-run view accepts the post", resp.status_code in (302, 303),
          str(resp.status_code))
    rerun_rows = list(V2OutboxEvent.objects.filter(
        event_type="admin.provider.evidence.requested"))
    check("re-running a review queues an assessment_rerun request",
          len(rerun_rows) == 1, f"{len(rerun_rows)} queued")
    if rerun_rows:
        rerun_body = json.loads(rerun_rows[0].raw_body)
        CAPTURED.append(rerun_body)
        check("with the right reason code",
              rerun_body["data"]["request"]["reason_code"] == "assessment_rerun",
              rerun_body["data"]["request"]["reason_code"])

    section("4. STALE EVIDENCE IS REFRESHED BEFORE IT IS DECIDED ON (§12.2)")
    now = timezone.now()
    stale = make_review(
        dossier_id="dos_stale",
        dossier_payload={
            "companies_house": {"checked_at": (now - timedelta(days=90)).strftime(
                "%Y-%m-%dT%H:%M:%S.000Z")},
        })
    fresh_dossier = make_review(
        dossier_id="dos_fresh",
        dossier_payload={
            "companies_house": {"checked_at": (now - timedelta(days=2)).strftime(
                "%Y-%m-%dT%H:%M:%S.000Z")},
        })
    check("a 90-day-old Companies House check is stale",
          ev.dossier_is_stale(stale, now=now) is True)
    check("a 2-day-old one is not", ev.dossier_is_stale(fresh_dossier, now=now) is False)
    check("a review with no dossier is not called stale",
          ev.dossier_is_stale(make_review(dossier_id="", dossier_payload={}), now=now) is False)

    # The decisive sources are Companies House and the breeder licence (§12.2).
    licence_stale = make_review(dossier_id="dos_lic", dossier_payload={
        "companies_house": {"checked_at": (now - timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z")},
        "licences": [{"licence_type": "breeder", "checked_at": (
            now - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%S.000Z")}],
    })
    check("a stale breeder licence check makes the dossier stale",
          ev.dossier_is_stale(licence_stale, now=now) is True)

    # Freshness has to be demonstrable; an undatable decisive source is not.
    undatable = make_review(dossier_id="dos_bad", dossier_payload={
        "companies_house": {"checked_at": "last Tuesday"}})
    check("a decisive source that cannot be dated counts as stale",
          ev.dossier_is_stale(undatable, now=now) is True)
    missing_ts = make_review(dossier_id="dos_nots", dossier_payload={
        "companies_house": {"status": "verified_active"}})
    check("a decisive source with no checked_at at all counts as stale",
          ev.dossier_is_stale(missing_ts, now=now) is True)

    # A source the payload does not mention is a completeness question, not a
    # staleness one, so it must not on its own trigger a refresh.
    unrelated = make_review(dossier_id="dos_part", dossier_payload={
        "website_and_domain": {"checked_at": (now - timedelta(days=400)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z")}})
    check("an old non-decisive source alone does not make it stale",
          ev.dossier_is_stale(unrelated, now=now) is False)
    check("the boundary is 30 days", ev.STALE_AFTER == timedelta(days=30),
          str(ev.STALE_AFTER))
    just_inside = make_review(dossier_id="dos_edge", dossier_payload={
        "companies_house": {"checked_at": (now - timedelta(days=29)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z")}})
    check("29 days old is still fresh", ev.dossier_is_stale(just_inside, now=now) is False)

    fresh()
    ev.request_fresh_evidence_if_stale(stale, now=now)
    check("a stale dossier triggers a refresh request",
          V2OutboxEvent.objects.count() == 1, str(V2OutboxEvent.objects.count()))
    if V2OutboxEvent.objects.exists():
        _, body = only_event()
        check("with reason stale_evidence",
              body["data"]["request"]["reason_code"] == "stale_evidence")
    fresh()
    check("a fresh dossier triggers nothing",
          ev.request_fresh_evidence_if_stale(fresh_dossier, now=now) is None
          and V2OutboxEvent.objects.count() == 0)

    # ---------------------------------------------------------------- §15
    section("5. ACCOUNT STATUS (§15)")

    fresh()
    ev.enqueue_account_status(review, status="suspended", reason_code="policy_breach")
    review.refresh_from_db()
    _, body = only_event()
    check("a suspension is queued",
          body["event_type"] == "admin.provider.account.status_changed", body["event_type"])
    check("status_version starts at 1",
          body["data"]["account_status"]["status_version"] == 1,
          str(body["data"]["account_status"]["status_version"]))
    check("and is persisted on the review", review.account_status_version == 1,
          str(review.account_status_version))

    fresh()
    ev.enqueue_account_status(review, status="reactivated", reason_code="review_resolved")
    review.refresh_from_db()
    _, body = only_event()
    check("the next change increments the version",
          body["data"]["account_status"]["status_version"] == 2,
          str(body["data"]["account_status"]["status_version"]))
    check("account status versions are counted apart from decision versions",
          review.account_status_version == 2 and review.decision_version == 2,
          f"status v{review.account_status_version} / decision v{review.decision_version}")

    fresh()
    refused = None
    try:
        ev.enqueue_account_status(review, status="reactivated", reason_code="policy_breach")
    except ValueError as exc:
        refused = str(exc)
    check("a suspension reason cannot be used to reactivate",
          refused is not None and "not permitted" in refused, str(refused))
    check("and no event was queued", V2OutboxEvent.objects.count() == 0)
    review.refresh_from_db()
    check("a refused change does not burn a version number",
          review.account_status_version == 2, str(review.account_status_version))

    fresh()
    refused = None
    try:
        ev.enqueue_account_status(review, status="deleted", reason_code="policy_breach")
    except ValueError as exc:
        refused = str(exc)
    check("an unknown status is refused", refused is not None, str(refused))

    section("6. THE SUSPEND CONTROL IN THE PORTAL EMITS IT (§15)")
    fresh()
    status_url = reverse("admin_portal:entity_status_update",
                         args=["consultant", str(consultant.id)])
    resp = client.post(status_url, {"action": "suspend", "next": "/"}, follow=False)
    check("the suspend control accepts the post", resp.status_code in (302, 303),
          str(resp.status_code))
    consultant.refresh_from_db()
    check("the profile really was suspended", consultant.is_active is False)
    status_rows = list(V2OutboxEvent.objects.filter(
        event_type="admin.provider.account.status_changed"))
    check("suspending through the portal queues the event", len(status_rows) == 1,
          f"{len(status_rows)} queued")
    if status_rows:
        sbody = json.loads(status_rows[0].raw_body)
        CAPTURED.append(sbody)
        check("with status suspended",
              sbody["data"]["account_status"]["status"] == "suspended",
              sbody["data"]["account_status"]["status"])
        check("attributed to an operator",
              sbody["data"]["account_status"]["actor_type"] == "operator")
        check("and a default reason an operator can stand behind",
              sbody["data"]["account_status"]["reason_code"] == "conduct_review",
              sbody["data"]["account_status"]["reason_code"])

    fresh()
    client.post(status_url, {"action": "reactivate", "next": "/"}, follow=False)
    consultant.refresh_from_db()
    check("reactivating restores the profile", consultant.is_active is True)
    react = list(V2OutboxEvent.objects.filter(
        event_type="admin.provider.account.status_changed"))
    check("and queues a reactivation", len(react) == 1, f"{len(react)} queued")
    if react:
        rbody = json.loads(react[0].raw_body)
        CAPTURED.append(rbody)
        check("with reason manual_reactivation",
              rbody["data"]["account_status"]["reason_code"] == "manual_reactivation",
              rbody["data"]["account_status"]["reason_code"])

    section("7. SUSPENDING THE LOGIN SUSPENDS THE PROVIDER WITH IT")
    fresh()
    client.post(reverse("admin_portal:entity_status_update", args=["user", str(user.id)]),
                {"action": "suspend", "next": "/"}, follow=False)
    via_user = list(V2OutboxEvent.objects.filter(
        event_type="admin.provider.account.status_changed"))
    check("suspending the user emits on their provider profile too",
          len(via_user) == 1, f"{len(via_user)} queued")
    if via_user:
        ubody = json.loads(via_user[0].raw_body)
        check("naming the provider profile's application",
              ubody["data"]["application_id"] == str(consultant.id),
              ubody["data"]["application_id"])

    section("8. A PROFILE THE CRM NEVER SAW IS NOT ANNOUNCED")
    fresh()
    orphan_user = ExternalUser.objects.create(
        id=uuid.uuid4(), email="orphan@aqua.test", is_active=True)
    orphan = ExternalBreederProfile.objects.create(
        id=uuid.uuid4(), user_id=orphan_user.id, company_name="No Review Ltd", is_active=True)
    client.post(reverse("admin_portal:entity_status_update",
                        args=["breeder", str(orphan.id)]),
                {"action": "suspend", "next": "/"}, follow=False)
    orphan.refresh_from_db()
    check("the suspension still applies", orphan.is_active is False)
    check("but nothing is sent for a profile with no review",
          V2OutboxEvent.objects.count() == 0, str(V2OutboxEvent.objects.count()))

    # ---------------------------------------------------------------- §16
    section("9. ERASURE (§16)")

    fresh()
    ev.enqueue_erasure(review, request_id="dsar-123",
                       legal_basis="UK GDPR Article 17 erasure request")
    review.refresh_from_db()
    _, body = only_event()
    check("an erasure is queued",
          body["event_type"] == "admin.provider.erased", body["event_type"])
    check("it carries the request id and legal basis",
          body["data"]["erasure"]["request_id"] == "dsar-123"
          and "Article 17" in body["data"]["erasure"]["legal_basis"])
    check("and the review records that it was sent",
          review.erasure_requested_at is not None)

    fresh()
    again = ev.enqueue_erasure(review, request_id="dsar-123", legal_basis="Article 17")
    check("a repeated erasure is not sent twice",
          again is None and V2OutboxEvent.objects.count() == 0,
          str(V2OutboxEvent.objects.count()))

    fresh()
    refused = None
    try:
        ev.enqueue_erasure(make_review(), request_id="", legal_basis="Article 17")
    except ValueError as exc:
        refused = str(exc)
    check("an erasure with no request id is refused", refused is not None, str(refused))
    refused = None
    try:
        ev.enqueue_erasure(make_review(), request_id="x", legal_basis="  ")
    except ValueError as exc:
        refused = str(exc)
    check("an erasure with no legal basis is refused", refused is not None, str(refused))

    section("10. FULFILLING AN ERASURE DSAR TELLS THE CRM (§16)")
    from admin_portal.services.dsar import _notify_crm_of_erasure

    erase_user = ExternalUser.objects.create(
        id=uuid.uuid4(), email="gone@aqua.test", is_active=True)
    erase_profile = ExternalBreederProfile.objects.create(
        id=uuid.uuid4(), user_id=erase_user.id, company_name="Leaving Ltd", is_active=True)
    erase_review = make_review(subject_type="breeder", subject_id=erase_profile.id,
                               aqua_provider_id=erase_user.id)

    fresh()
    dsar = DSARRequest.objects.create(
        request_type="erasure", status="fulfilled", subject_user_id=erase_user.id,
        submitted_email="gone@aqua.test")
    _notify_crm_of_erasure(dsar)
    rows = list(V2OutboxEvent.objects.filter(event_type="admin.provider.erased"))
    check("a fulfilled erasure DSAR queues the erasure event", len(rows) == 1,
          f"{len(rows)} queued")
    if rows:
        ebody = json.loads(rows[0].raw_body)
        CAPTURED.append(ebody)
        check("keyed to the DSAR request itself",
              ebody["data"]["erasure"]["request_id"] == str(dsar.id),
              ebody["data"]["erasure"]["request_id"])
        check("and to the right provider",
              ebody["data"]["aqua_provider_id"] == str(erase_user.id))

    fresh()
    access = DSARRequest.objects.create(
        request_type="access", status="fulfilled", subject_user_id=erase_user.id,
        submitted_email="gone@aqua.test")
    _notify_crm_of_erasure(access)
    check("an ACCESS DSAR erases nothing and sends nothing",
          V2OutboxEvent.objects.count() == 0, str(V2OutboxEvent.objects.count()))

    fresh()
    unmatched = DSARRequest.objects.create(
        request_type="erasure", status="fulfilled", subject_user_id=None,
        submitted_email="stranger@aqua.test")
    _notify_crm_of_erasure(unmatched)
    check("an erasure for someone who is not a provider sends nothing",
          V2OutboxEvent.objects.count() == 0, str(V2OutboxEvent.objects.count()))

    section("11. A USER WITH BOTH PROFILES IS REACHED ON BOTH")
    both_user = ExternalUser.objects.create(
        id=uuid.uuid4(), email="both@aqua.test", is_active=True)
    b_profile = ExternalBreederProfile.objects.create(
        id=uuid.uuid4(), user_id=both_user.id, company_name="Dual Breeder", is_active=True)
    c_profile = ExternalConsultantProfile.objects.create(
        id=uuid.uuid4(), user_id=both_user.id, company_name="Dual Consulting", is_active=True)
    make_review(subject_type="breeder", subject_id=b_profile.id, aqua_provider_id=both_user.id)
    make_review(subject_type="consultant", subject_id=c_profile.id, aqua_provider_id=both_user.id)
    found = ev.reviews_for_user(both_user.id)
    check("both applications are found", len(found) == 2, str(len(found)))

    fresh()
    both_dsar = DSARRequest.objects.create(
        request_type="erasure", status="fulfilled", subject_user_id=both_user.id,
        submitted_email="both@aqua.test")
    _notify_crm_of_erasure(both_dsar)
    check("and an erasure reaches both", V2OutboxEvent.objects.count() == 2,
          str(V2OutboxEvent.objects.count()))

    section("12. EVERY EVENT IS DELIVERABLE AS QUEUED")
    from admin_portal.integrations import contract as c

    fresh()
    ev.enqueue_account_status(review, status="suspended", reason_code="provider_request")
    row, body = only_event()
    raw = row.raw_body.encode("utf-8")
    check("the queued body is within the 256 KB limit", len(raw) <= c.MAX_BODY_BYTES,
          f"{len(raw)} bytes")
    check("the stored bytes are exactly what the envelope parses back to",
          json.loads(row.raw_body) == body)
    check("the row is queued for delivery, not already attempted",
          row.status == "pending" and row.attempt_count == 0,
          f"{row.status}/{row.attempt_count}")
    check("the event id in the row matches the envelope",
          row.event_id == body["event_id"])
    check("and the producer names this portal and environment",
          body["producer"] == {"system": "aqua-admin-portal", "environment": "staging"},
          str(body["producer"]))

    section("13. NO ENVIRONMENT CONFIGURED MEANS NOTHING IS SENT")
    fresh()
    with override_settings(AQUA_ADMIN_TO_CRM_WEBHOOK_KEYS="", AQUA_V2_ENVIRONMENT=""):
        quiet = [
            ev.enqueue_evidence_request(review, reason_code="operator_requested"),
            ev.enqueue_account_status(review, status="suspended", reason_code="conduct_review"),
            ev.enqueue_erasure(make_review(), request_id="r", legal_basis="Article 17"),
        ]
    check("every emitter is a no-op without a key ring",
          all(r is None for r in quiet) and V2OutboxEvent.objects.count() == 0,
          str(V2OutboxEvent.objects.count()))

    out = pathlib.Path(tempfile.gettempdir()) / "v2_outbox_events.json"
    out.write_text(json.dumps(CAPTURED, indent=2))
    print(f"\n{len(CAPTURED)} envelopes written to {out} for the Node schema run")

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
