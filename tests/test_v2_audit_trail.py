"""V2.3 item 15: every automated transition leaves an audit entry.

The portal already audited what people did: every view that writes calls
``audit.record_write``. Nothing audited what the system did on its own — a
decision minted by the rules engine, a safety hold fired by a timeout, a
dossier arriving from the CRM overnight, an account activated by the scheduler.
Those are precisely the transitions nobody witnessed, so they are the ones that
most need a record.

These checks drive the real paths rather than calling the audit helper: an
audit trail that only proves the logger works is worth nothing. Each case
performs the transition and then asks whether the trail can answer "what
happened to this provider, and who decided it".
"""
from __future__ import annotations

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
CRM_RING = '{"current": {"key_id": "crm-1", "secret": "%s"}}' % ("a" * 64)


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{extra}]" if extra else ""))


def section(title):
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def clean_dossier(dossier_id="dos_01HZZZ", revision=3):
    """A terminal, conflict-free dossier.

    Fixtures need one because §12.3 refuses an approval or rejection without
    terminal CRM evidence, and refuses an AUTOMATIC approval unless every
    clean-case condition holds. A bare dossier_id with no dossier behind it is
    not evidence, and the gate is right to say so.
    """
    import datetime as _dt
    stamp = _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "dossier_id": dossier_id, "revision": revision, "status": "complete",
        "assembled_at": stamp, "match_status": "confirmed",
        "match_confidence": "high",
        "company_identity": {"declared_name": "Co",
                             "business_structure": "limited_company"},
        "companies_house": {"status": "verified_active", "checked_at": stamp,
                            "directors_observed": []},
        "website_and_domain": {"checked_at": stamp},
        "licences": [], "sources": [], "conflicts": [],
        "summary": "Company confirmed active.",
    }


def build_mirror_schema():
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
            except Exception:
                pass


@override_settings(
    AQUA_V2_ENVIRONMENT="staging",
    AQUA_ADMIN_TO_CRM_WEBHOOK_KEYS=ADMIN_RING,
    AQUA_CRM_TO_ADMIN_WEBHOOK_KEYS=CRM_RING,
    AQUA_CRM_V2_EVENTS_URL="https://crm.aquaai.uk/api/integrations/v2/admin-events",
    ADMIN_EMAILS=[], SUPERADMIN_EMAILS=["op@aquaai.uk"],
    SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False,
)
def main():
    from admin_portal.models import (
        AdminAuditLog, AdminUser, AIAccountReview, ExternalBreederProfile,
        ExternalConsultantProfile, ExternalUser, V2OutboxEvent,
    )
    from admin_portal.integrations import contract as c
    from admin_portal.integrations import events as ev
    from admin_portal.integrations.decisions import record_decision
    from admin_portal.services import audit

    build_mirror_schema()
    now = timezone.now()

    def entries(action=None):
        qs = AdminAuditLog.objects.all().order_by("created_at")
        return list(qs.filter(action=action) if action else qs)

    def fresh():
        AdminAuditLog.objects.all().delete()
        V2OutboxEvent.objects.all().delete()

    def make_review(**kw):
        defaults = dict(
            subject_type="consultant", subject_id=uuid.uuid4(),
            aqua_provider_id=uuid.uuid4(),
            subject_user_email="c@aqua.test", subject_display_name="Co",
            decision="pending", review_state="awaiting_crm_evidence",
            dossier_id="dos_01HZZZ", dossier_revision=3,
            dossier_status="complete", dossier_payload=clean_dossier(),
            submitted_event_at=now - timedelta(hours=1),
        )
        defaults.update(kw)
        return AIAccountReview.objects.create(**defaults)

    operator = AdminUser.objects.create(
        email="op@aquaai.uk", role="admin", is_platform_super_admin=True,
        is_staff=True, is_active=True)

    # ------------------------------------------------------------------
    section("1. THE VOCABULARY IS CLOSED")
    refused = None
    try:
        audit.record_system("something.invented", target_type="review", target_id="x")
    except ValueError as exc:
        refused = str(exc)
    check("an unknown action is refused rather than written",
          refused is not None and "SYSTEM_ACTIONS" in refused, str(refused))
    check("and nothing was written", not entries())

    section("2. AN AUTOMATIC DECISION IS ATTRIBUTABLE TO THE ENGINE")
    fresh()
    auto = make_review(confidence=0.91)
    record_decision(
        auto, status="approved", reason_code="evidence_verified",
        reason_text="All checks passed with no unresolved flags.",
        actor_type="approved_rules_engine", decision_path="automatic_clean_case",
        score=0.91, flags=[])
    auto.refresh_from_db()
    rows = entries("v2.decision.recorded")
    check("an automatic approval is audited", len(rows) == 1, f"{len(rows)} rows")
    if rows:
        row = rows[0]
        check("with no actor, because nobody chose it", row.actor_id is None)
        check("naming the engine as the decider",
              row.details.get("actor_type") == "approved_rules_engine",
              str(row.details.get("actor_type")))
        check("and carrying the decision id the CRM was told",
              row.details.get("decision_id") == auto.decision_id,
              str(row.details.get("decision_id")))
        check("the reason is recorded, not just the status",
              row.details.get("reason_code") == "evidence_verified"
              and "All checks passed" in row.details.get("reason_text", ""))
        check("the evidence it rested on is named",
              row.details.get("evidence_dossier_id") == "dos_01HZZZ"
              and row.details.get("evidence_dossier_revision") == 3)
        check("and the score that justified it",
              abs(float(row.details.get("score", 0)) - 0.91) < 0.001,
              str(row.details.get("score")))
        check("the target points at the application",
              row.target_type == "review" and row.target_id == str(auto.subject_id))

    section("3. A SAFETY HOLD IS DISTINGUISHABLE FROM A DECISION")
    fresh()
    held = make_review(dossier_id="")
    record_decision(
        held, status="held", reason_code="evidence_overdue",
        reason_text="No dossier within 12 hours of submission.",
        actor_type="system_timeout", decision_path="system_safety_hold",
        use_dossier=False)
    rows = entries("v2.decision.safety_hold")
    check("a timeout hold gets its own action", len(rows) == 1, f"{len(rows)} rows")
    check("and is not filed as an ordinary decision",
          not entries("v2.decision.recorded"))
    if rows:
        check("attributed to the timeout, not a person",
              rows[0].details.get("actor_type") == "system_timeout"
              and rows[0].actor_id is None)
        check("and recorded as still needing a person",
              rows[0].details.get("review_state") == "human_review",
              str(rows[0].details.get("review_state")))

    section("4. AN OPERATOR DECISION KEEPS THE OPERATOR'S NAME")
    fresh()
    manual = make_review()
    record_decision(
        manual, status="rejected", reason_code="manual_rejection",
        reason_text="Licence could not be verified.",
        actor_type="operator", decision_path="human_review", actor=operator)
    rows = entries("v2.decision.recorded")
    check("an operator decision is audited too", len(rows) == 1, f"{len(rows)} rows")
    if rows:
        check("and carries who made it", rows[0].actor_id == operator.pk,
              str(rows[0].actor_id))
        check("so a rejection can never read as automatic",
              rows[0].details.get("actor_type") == "operator")

    section("5. A REFUSED DECISION LEAVES NO TRAIL, BECAUSE NOTHING HAPPENED")
    fresh()
    from admin_portal.integrations.decisions import DecisionRefused

    bad = make_review()
    refused = None
    try:
        record_decision(
            bad, status="rejected", reason_code="manual_rejection",
            reason_text="Auto-rejected.", actor_type="approved_rules_engine",
            decision_path="automatic_clean_case")
    except DecisionRefused as exc:
        refused = str(exc)
    check("the contract violation is refused", refused is not None, str(refused))
    check("and no audit entry claims a decision was made", not entries(),
          f"{len(entries())} rows")

    section("6. A DOSSIER ARRIVING OVERNIGHT IS RECORDED")
    fresh()
    user = ExternalUser.objects.create(
        id=uuid.uuid4(), email="pro@aqua.test", is_active=True)
    profile = ExternalConsultantProfile.objects.create(
        id=uuid.uuid4(), user_id=user.id, company_name="Reef Consulting",
        is_active=True, admin_status="pending")
    review = make_review(subject_id=profile.id, aqua_provider_id=user.id,
                         dossier_id="", dossier_revision=None,
                         review_state="awaiting_crm_evidence")

    import json

    def crm_post(client, body: dict):
        raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
        ts = str(int(timezone.now().timestamp() * 1000))
        key_ring = c.key_ring_from_env("AQUA_CRM_TO_ADMIN_WEBHOOK_KEYS")
        sig = c.signature_header(key_ring.current.secret, ts, raw)
        return client.post(
            c.CRM_EVENTS_PATH, data=raw, content_type=c.CONTENT_TYPE,
            HTTP_X_AQUA_EVENT=body["event_type"],
            HTTP_X_AQUA_SCHEMA_VERSION=c.SCHEMA_VERSION,
            HTTP_X_AQUA_EVENT_ID=body["event_id"],
            HTTP_X_AQUA_TIMESTAMP_MS=ts,
            HTTP_X_AQUA_KEY_ID=key_ring.current.key_id,
            HTTP_X_AQUA_SIGNATURE=sig,
        )

    def dossier_event(application_id, *, revision=1, dossier_id="dos_NIGHT"):
        stamp = c.rfc3339_ms(timezone.now())
        return {
            "schema_version": c.SCHEMA_VERSION,
            "event_id": c.new_ulid(),
            "event_type": "crm.provider.evidence_dossier.ready",
            "occurred_at": stamp,
            "correlation_id": str(application_id),
            "producer": {"system": "aqua-provider-crm", "environment": "staging"},
            "data": {
                "application_id": str(application_id),
                "aqua_provider_id": str(user.id),
                "dossier": {
                    "dossier_id": dossier_id, "revision": revision,
                    "status": "complete", "assembled_at": stamp,
                    "match_status": "confirmed", "match_confidence": "high",
                    "company_identity": {
                        "declared_name": "Reef Consulting",
                        "business_structure": "limited_company",
                    },
                    "companies_house": {"status": "verified_active",
                                        "checked_at": stamp,
                                        "directors_observed": []},
                    "website_and_domain": {"checked_at": stamp},
                    "licences": [], "sources": [], "conflicts": [],
                    "summary": "Company confirmed active at Companies House.",
                },
            },
        }

    client = Client()
    resp = crm_post(client, dossier_event(profile.id, revision=4))
    check("the receiver accepted the dossier", resp.status_code == 200,
          f"{resp.status_code} {resp.content[:120]}")
    rows = entries("v2.dossier.received")
    check("and recorded that it arrived", len(rows) == 1, f"{len(rows)} rows")
    if rows:
        check("naming the revision now in force",
              rows[0].details.get("dossier_revision") == 4,
              str(rows[0].details.get("dossier_revision")))
        check("and the state the application moved to",
              rows[0].details.get("review_state") == "evidence_received",
              str(rows[0].details.get("review_state")))
        check("with the signing key that was trusted",
              rows[0].details.get("signature_key_id") == "crm-1",
              str(rows[0].details.get("signature_key_id")))

    section("7. A DOSSIER FOR SOMEONE WE DO NOT KNOW IS RECORDED AS REFUSED")
    fresh()
    resp = crm_post(client, dossier_event(uuid.uuid4()))
    check("the receiver refuses it", resp.status_code == 422, str(resp.status_code))
    rows = entries("v2.dossier.refused")
    check("and the refusal is auditable", len(rows) == 1, f"{len(rows)} rows")
    if rows:
        check("with the reason it was refused",
              rows[0].details.get("reason") == "unknown_application",
              str(rows[0].details.get("reason")))
    check("no 'received' entry claims it was attached",
          not entries("v2.dossier.received"))

    section("8. AN ABANDONED OUTBOUND EVENT REACHES A PERSON")
    fresh()
    from admin_portal.integrations import outbox

    target = make_review()
    record_decision(
        target, status="held", reason_code="manual_review_required",
        reason_text="Waiting on the licence register.",
        actor_type="operator", decision_path="human_review", actor=operator)
    ev.enqueue_decision_event(target)
    AdminAuditLog.objects.all().delete()

    class Refuses:
        class Resp:
            status_code = 422
            text = "invalid_payload"

        def post(self, url, data=None, headers=None, timeout=None):
            return self.Resp()

    counts = outbox.dispatch_due(session=Refuses())
    check("the dispatcher dead-letters an unretryable refusal",
          counts["dead_letter"] == 1, str(counts))
    rows = entries("v2.outbox.dead_letter")
    check("and it is audited, not only logged", len(rows) == 1, f"{len(rows)} rows")
    if rows:
        check("naming the event the CRM never received",
              rows[0].details.get("event_type")
              == "admin.provider.application.decision.recorded",
              str(rows[0].details.get("event_type")))
        check("and pointing at the application it concerned",
              rows[0].target_id == str(target.subject_id))

    section("9. AN ERASURE INSTRUCTION IS RECORDED SEPARATELY FROM THE DSAR")
    fresh()
    erase_user = ExternalUser.objects.create(
        id=uuid.uuid4(), email="gone@aqua.test", is_active=True)
    erase_profile = ExternalBreederProfile.objects.create(
        id=uuid.uuid4(), user_id=erase_user.id, company_name="Leaving Ltd",
        is_active=True)
    erase_review = make_review(subject_type="breeder", subject_id=erase_profile.id,
                               aqua_provider_id=erase_user.id)
    ev.enqueue_erasure(erase_review, request_id="dsar-77",
                       legal_basis="UK GDPR Article 17 erasure request")
    rows = entries("v2.erasure.queued")
    check("the erasure is audited", len(rows) == 1, f"{len(rows)} rows")
    if rows:
        check("tied to the request that authorised it",
              rows[0].details.get("request_id") == "dsar-77")
        check("and naming the legal basis",
              "Article 17" in rows[0].details.get("legal_basis", ""))

    section("10. AN ACCOUNT ACTIVATED BY THE SCHEDULER SAYS SO")
    fresh()
    from admin_portal.services.review_runner import _apply_actions

    auto_user = ExternalUser.objects.create(
        id=uuid.uuid4(), email="auto@aqua.test", is_active=False)
    auto_profile = ExternalConsultantProfile.objects.create(
        id=uuid.uuid4(), user_id=auto_user.id, company_name="Auto Co",
        is_active=False, admin_status="pending")
    auto_review = make_review(
        subject_id=auto_profile.id, aqua_provider_id=auto_user.id,
        decision="approved", confidence=0.88, reason_code="evidence_verified",
        decision_actor_type="approved_rules_engine", recommended_actions=[])
    _apply_actions("consultant", auto_profile, auto_user, auto_review)
    rows = entries("review.auto_approved")
    check("an unattended activation is audited", len(rows) == 1, f"{len(rows)} rows")
    if rows:
        check("with no actor", rows[0].actor_id is None)
        check("naming who the account belongs to",
              rows[0].details.get("subject_email") == "auto@aqua.test",
              str(rows[0].details.get("subject_email")))
        check("and the confidence it was activated on",
              abs(float(rows[0].details.get("score", 0)) - 0.88) < 0.001,
              str(rows[0].details.get("score")))
    auto_profile.refresh_from_db()
    check("the account really was activated", auto_profile.is_active is True)

    fresh()
    rej_user = ExternalUser.objects.create(
        id=uuid.uuid4(), email="rej@aqua.test", is_active=True)
    rej_profile = ExternalConsultantProfile.objects.create(
        id=uuid.uuid4(), user_id=rej_user.id, company_name="Rejected Co",
        is_active=True, admin_status="pending")
    rej_review = make_review(
        subject_id=rej_profile.id, aqua_provider_id=rej_user.id,
        decision="rejected", reason_code="manual_rejection",
        recommended_actions=[])
    _apply_actions("consultant", rej_profile, rej_user, rej_review)
    check("an unattended deactivation is audited too",
          len(entries("review.auto_deactivated")) == 1,
          str(len(entries("review.auto_deactivated"))))

    section("11. A HELD REVIEW CHANGES NO ACCOUNT, AND CLAIMS NOTHING")
    fresh()
    hold_user = ExternalUser.objects.create(
        id=uuid.uuid4(), email="hold@aqua.test", is_active=True)
    hold_profile = ExternalConsultantProfile.objects.create(
        id=uuid.uuid4(), user_id=hold_user.id, company_name="Held Co",
        is_active=True, admin_status="pending")
    hold_review = make_review(
        subject_id=hold_profile.id, aqua_provider_id=hold_user.id,
        decision="held", recommended_actions=[])
    _apply_actions("consultant", hold_profile, hold_user, hold_review)
    check("a hold does not record an activation",
          not entries("review.auto_approved"))
    check("nor a deactivation", not entries("review.auto_deactivated"))

    section("12. THE TRAIL SURVIVES ITS OWN FAILURES")
    fresh()
    from unittest.mock import patch

    survivor = make_review()
    with patch("admin_portal.models.AdminAuditLog.objects.create",
               side_effect=RuntimeError("audit table unavailable")):
        record_decision(
            survivor, status="approved", reason_code="evidence_verified",
            reason_text="Checks passed.", actor_type="operator",
            decision_path="human_review", actor=operator)
    survivor.refresh_from_db()
    check("a decision still commits when its audit entry cannot be written",
          survivor.decision == "approved" and bool(survivor.decision_id),
          f"{survivor.decision}/{survivor.decision_id}")

    section("13. AN OPERATOR CAN READ WHAT THE SYSTEM DID ON ITS OWN")
    fresh()
    seen = make_review()
    record_decision(
        seen, status="approved", reason_code="evidence_verified",
        reason_text="Clean case.", actor_type="approved_rules_engine",
        decision_path="automatic_clean_case", score=0.9, flags=[])
    audit.record(operator, "entity.suspend", target_type="consultant",
                 target_id=str(seen.subject_id))

    client.force_login(operator)
    url = reverse("admin_portal:audit_log")
    everything = client.get(url)
    check("the audit page loads", everything.status_code == 200,
          str(everything.status_code))
    system_only = client.get(url, {"source": "system"})
    rows = list(system_only.context["page"].object_list)
    check("filtering to the system shows only unattended transitions",
          len(rows) == 1 and rows[0].action == "v2.decision.recorded",
          str([r.action for r in rows]))
    people_only = client.get(url, {"source": "people"})
    rows = list(people_only.context["page"].object_list)
    check("and filtering to people excludes them",
          len(rows) == 1 and rows[0].action == "entity.suspend",
          str([r.action for r in rows]))
    check("every system action is explained on the page",
          all(audit.SYSTEM_ACTIONS[a] for a in audit.SYSTEM_ACTIONS))

    section("14. THE SYSTEM NEVER NOTIFIES DEVELOPERS PER TRANSITION")
    calls = []
    import admin_portal.services.audit as audit_module

    original = audit_module.notify_developer_action
    audit_module.notify_developer_action = lambda *a, **k: calls.append(a)
    try:
        fresh()
        quiet = make_review()
        record_decision(
            quiet, status="approved", reason_code="evidence_verified",
            reason_text="Clean.", actor_type="approved_rules_engine",
            decision_path="automatic_clean_case", score=0.9, flags=[])
    finally:
        audit_module.notify_developer_action = original
    check("an automated transition raises no developer notification",
          not calls, str(len(calls)))
    check("but it is still recorded", len(entries("v2.decision.recorded")) == 1)

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
