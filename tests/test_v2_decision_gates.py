"""V2.3 items 9, 10 and 11: when a decision may be made, and by whom.

These are one mechanism, not three:

  9  no approval or rejection in production without a terminal CRM dossier,
     "whatever an operational setting says"
 10  the §12.3 clean-case gate — six conditions, every one of which must hold
 11  rejection is always human, and so is any approval the gate did not clear

The defect they close is not in the validator. It was in the review runner,
which wrote the model's verdict straight onto the row — ``decision=outcome
.decision`` in an update_or_create — and then applied it to the account. The
gate could not refuse what never reached it. So the checks that matter here
drive the runner, not the validator: a model that says "approved" must not be
able to approve anyone on its own.

The gate's bias is deliberate and is asserted in both directions: a condition
that cannot be evaluated fails. "We could not tell" is not "it was fine".
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

from django.test import override_settings  # noqa: E402
from django.utils import timezone  # noqa: E402

PASS, FAIL = [], []
ADMIN_RING = '{"current": {"key_id": "admin-1", "secret": "%s"}}' % ("b" * 64)


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{extra}]" if extra else ""))


def section(title):
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


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


def clean_dossier(**overrides):
    """A dossier that satisfies every §12.3 condition."""
    stamp = timezone.now().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    dossier = {
        "dossier_id": "dos_CLEAN", "revision": 1, "status": "complete",
        "assembled_at": stamp, "match_status": "confirmed",
        "match_confidence": "high",
        "company_identity": {"declared_name": "Reef Consulting",
                             "business_structure": "limited_company"},
        "companies_house": {"status": "verified_active", "checked_at": stamp,
                            "directors_observed": []},
        "website_and_domain": {"checked_at": stamp},
        "licences": [], "sources": [], "conflicts": [],
        "summary": "Company confirmed active.",
    }
    dossier.update(overrides)
    return dossier


@override_settings(
    AQUA_V2_ENVIRONMENT="production",
    AQUA_ADMIN_TO_CRM_WEBHOOK_KEYS=ADMIN_RING,
    AQUA_CRM_V2_EVENTS_URL="https://crm.aquaai.uk/api/integrations/v2/admin-events",
    ADMIN_EMAILS=[], SUPERADMIN_EMAILS=["op@aquaai.uk"],
    SECURE_SSL_REDIRECT=False,
)
def main():
    from admin_portal.models import (
        AdminUser, AIAccountReview, AIFlag, ExternalConsultantProfile, ExternalUser,
    )
    from admin_portal.integrations import clean_case
    from admin_portal.integrations import evidence_sla as sla
    from admin_portal.integrations.decisions import DecisionRefused, record_decision

    build_mirror_schema()
    now = timezone.now()
    operator = AdminUser.objects.create(
        email="op@aquaai.uk", role="admin", is_platform_super_admin=True,
        is_staff=True, is_active=True)

    def make_review(*, dossier=None, score=0.9, **kw):
        payload = dossier if dossier is not None else clean_dossier()
        defaults = dict(
            subject_type="consultant", subject_id=uuid.uuid4(),
            aqua_provider_id=uuid.uuid4(),
            subject_user_email="c@aqua.test", subject_display_name="Co",
            decision="pending", review_state="evidence_received",
            confidence=score,
            dossier_id=payload.get("dossier_id", "") if payload else "",
            dossier_revision=payload.get("revision") if payload else None,
            dossier_status=payload.get("status", "") if payload else "",
            dossier_payload=payload or {},
            submitted_event_at=now - timedelta(hours=1),
        )
        defaults.update(kw)
        return AIAccountReview.objects.create(**defaults)

    def refusal(fn):
        try:
            fn()
        except DecisionRefused as exc:
            return str(exc)
        return None

    # ================================================================== 10
    section("1. THE GATE PASSES ONLY A GENUINELY CLEAN CASE (§12.3, item 10)")
    clean = make_review()
    assessment = clean_case.assess(clean)
    check("a complete, conflict-free, high-confidence case passes",
          assessment.passed, assessment.reason())
    check("and every condition is reported, not just the failures",
          len(assessment.conditions) == 7, str(len(assessment.conditions)))

    section("2. EACH CONDITION CAN STOP IT ON ITS OWN")
    cases = [
        ("a partial dossier", dict(dossier=clean_dossier(status="partial")),
         "dossier_status_complete"),
        ("a dossier reporting conflicts",
         dict(dossier=clean_dossier(status="conflicts_found", conflicts=[
             {"field": "company_name", "severity": "high", "checked_at": "x"}])),
         "no_conflicts"),
        ("a medium-confidence match",
         dict(dossier=clean_dossier(match_confidence="medium")), "match_confidence_high"),
        ("an inactive company record",
         dict(dossier=clean_dossier(
             companies_house={"status": "verified_inactive"})), "companies_house"),
        ("a company record not found",
         dict(dossier=clean_dossier(
             companies_house={"status": "not_found"})), "companies_house"),
        ("a score below the floor", dict(score=0.74), "score"),
    ]
    for label, kw, expected in cases:
        review = make_review(**kw)
        result = clean_case.assess(review)
        failed = [c.name for c in result.failures]
        check(f"{label} stops automatic approval",
              not result.passed and expected in failed, str(failed))

    section("3. AN UNRESOLVED FLAG OF ANY SEVERITY STOPS IT")
    flagged = make_review()
    AIFlag.objects.create(review=flagged, severity="info", reason="Minor note")
    result = clean_case.assess(flagged)
    check("even an info-severity flag blocks the gate",
          not result.passed and "no_unresolved_flags" in [c.name for c in result.failures],
          result.reason())

    section("4. THE GATE NEVER FILLS A GAP WITH A DEFAULT")
    # "An approval engine MUST NOT infer, downgrade or waive any condition."
    missing = make_review(dossier={"dossier_id": "dos_THIN", "revision": 1,
                                   "status": "complete"})
    result = clean_case.assess(missing)
    failed = [c.name for c in result.failures]
    check("an absent conflicts array is not read as 'no conflicts'",
          "no_conflicts" in failed, str(failed))
    check("an absent match confidence is not read as high",
          "match_confidence_high" in failed, str(failed))
    check("an absent company record is not waived", "companies_house" in failed, str(failed))

    # The column is NOT NULL, so an unavailable score arrives as something
    # unparseable rather than as an absent row value.
    # None is not in this list: it is the API's "use the review's own score",
    # and the column is NOT NULL so a review always has one.
    unscored = make_review()
    for bad in ("", "n/a", object()):
        failures = [c.name for c in clean_case.assess(unscored, score=bad).failures]
        check(f"a score of {bad!r} is not treated as passing", "score" in failures,
              str(failures))

    section("5. not_applicable IS ACCEPTED ONLY WHERE IT MAKES SENSE")
    sole = make_review(dossier=clean_dossier(
        companies_house={"status": "not_applicable"},
        company_identity={"declared_name": "Dan's Fish",
                          "business_structure": "sole_trader"}))
    check("a sole trader with no company record passes",
          clean_case.assess(sole).passed, clean_case.assess(sole).reason())
    ltd = make_review(dossier=clean_dossier(
        companies_house={"status": "not_applicable"},
        company_identity={"declared_name": "Reef Ltd",
                          "business_structure": "limited_company"}))
    check("but a limited company claiming not_applicable does not",
          not clean_case.assess(ltd).passed)
    check("and the refusal says why",
          "sole trader or partnership" in clean_case.assess(ltd).reason(),
          clean_case.assess(ltd).reason())

    # =================================================================== 9
    section("6. NO APPROVAL OR REJECTION WITHOUT A TERMINAL DOSSIER (item 9)")
    bare = make_review(dossier={}, dossier_id="", dossier_status="")
    msg = refusal(lambda: record_decision(
        bare, status="approved", reason_code="evidence_verified",
        reason_text="Looks fine.", actor_type="operator",
        decision_path="human_review", actor=operator))
    check("an operator cannot approve without one",
          msg is not None and "terminal CRM evidence dossier" in msg, str(msg))

    msg = refusal(lambda: record_decision(
        bare, status="rejected", reason_code="manual_rejection",
        reason_text="No.", actor_type="operator",
        decision_path="human_review", actor=operator))
    check("nor reject without one",
          msg is not None and "terminal CRM evidence dossier" in msg, str(msg))
    bare.refresh_from_db()
    check("and nothing was written", bare.decision == "pending" and not bare.decision_id)

    section("7. BUT A SAFETY HOLD STILL WORKS WITHOUT ONE")
    # A timeout hold exists precisely because no dossier arrived.
    timeout = make_review(dossier={}, dossier_id="", dossier_status="")
    record_decision(
        timeout, status="held", reason_code="evidence_overdue",
        reason_text="No dossier within 12 hours.",
        actor_type="system_timeout", decision_path="system_safety_hold",
        use_dossier=False)
    timeout.refresh_from_db()
    check("the 12-hour safety hold is unaffected by the dossier rule",
          timeout.decision == "held" and timeout.review_state == "human_review")

    section("8. LOWER ENVIRONMENTS MAY DECIDE WITHOUT ONE, PRODUCTION MAY NOT")
    sandbox_review = make_review(dossier={}, dossier_id="", dossier_status="")
    with override_settings(AQUA_V2_ENVIRONMENT="staging"):
        staged = refusal(lambda: record_decision(
            sandbox_review, status="approved", reason_code="evidence_verified",
            reason_text="Fixture.", actor_type="operator",
            decision_path="human_review", actor=operator))
    check("staging can decide without a dossier, for fixtures", staged is None, str(staged))
    prod_review = make_review(dossier={}, dossier_id="", dossier_status="")
    blocked = refusal(lambda: record_decision(
        prod_review, status="approved", reason_code="evidence_verified",
        reason_text="Fixture.", actor_type="operator",
        decision_path="human_review", actor=operator))
    check("production cannot, whatever a setting says", blocked is not None)

    # ================================================================== 11
    section("9. REJECTION IS ALWAYS HUMAN (item 11)")
    for actor_type, path in (("approved_rules_engine", "automatic_clean_case"),
                             ("approved_rules_engine", "human_review"),
                             ("system_timeout", "system_safety_hold")):
        r = make_review()
        msg = refusal(lambda r=r, a=actor_type, p=path: record_decision(
            r, status="rejected", reason_code="manual_rejection",
            reason_text="Auto-reject.", actor_type=a, decision_path=p,
            score=0.95, flags=[]))
        check(f"{actor_type} cannot reject via {path}", msg is not None, str(msg))

    section("10. AN APPROVAL THE GATE DID NOT CLEAR NEEDS A PERSON")
    not_clean = make_review(dossier=clean_dossier(status="partial"))
    msg = refusal(lambda: record_decision(
        not_clean, status="approved", reason_code="evidence_verified",
        reason_text="Close enough.", actor_type="approved_rules_engine",
        decision_path="automatic_clean_case", score=0.95, flags=[]))
    check("the engine cannot approve a case the gate refused",
          msg is not None and "clean-case gate is not satisfied" in msg, str(msg))
    check("and the refusal names the condition that stopped it",
          msg is not None and "dossier_status_complete" in msg, str(msg))

    # The same case IS approvable, by a person taking responsibility for it.
    person = record_decision(
        not_clean, status="approved", reason_code="manual_exception_approved",
        reason_text="Partial dossier reviewed; satisfied on the balance of evidence.",
        actor_type="operator", decision_path="human_review", actor=operator)
    check("but a person may approve it, on the record",
          person.decision == "approved" and person.decision_actor_type == "operator")
    check("and it is attributed to them", person.overridden_by_id == operator.pk)

    section("11. A HELD CASE MUST NOT AUTO-PROMOTE")
    held = make_review()
    record_decision(
        held, status="held", reason_code="manual_review_required",
        reason_text="Needs a look.", actor_type="operator",
        decision_path="human_review", actor=operator)
    held.refresh_from_db()
    msg = refusal(lambda: record_decision(
        held, status="approved", reason_code="evidence_verified",
        reason_text="Resolved automatically.", actor_type="approved_rules_engine",
        decision_path="automatic_clean_case", score=0.95, flags=[]))
    check("the engine cannot lift a hold",
          msg is not None and "must not auto-promote" in msg, str(msg))
    resolved = record_decision(
        held, status="approved", reason_code="manual_exception_approved",
        reason_text="Reviewed and cleared.", actor_type="operator",
        decision_path="human_review", actor=operator)
    check("only a person can", resolved.decision == "approved")
    check("and the version moved on", resolved.decision_version == 2,
          str(resolved.decision_version))

    # ============================================= the defect being closed
    section("12. THE REVIEW RUNNER NO LONGER DECIDES FOR ITSELF")
    from admin_portal.services.review_runner import _persist_review
    from admin_portal.services.openai_review import AIReviewOutcome

    def outcome(decision, confidence=0.95, flags=()):
        return AIReviewOutcome(
            decision=decision, confidence=confidence,
            rationale="Model verdict.", evidence={}, flags=list(flags),
            recommended_actions=[], raw={}, model="test", error="")

    def provider(email, active=False):
        u = ExternalUser.objects.create(
            id=uuid.uuid4(), email=email, is_active=active)
        p = ExternalConsultantProfile.objects.create(
            id=uuid.uuid4(), user_id=u.id, company_name="Runner Co",
            is_active=active, admin_status="pending")
        return u, p

    u1, p1 = provider("norush@aqua.test")
    review = _persist_review("consultant", p1, u1, outcome("approved"))
    review.refresh_from_db()
    p1.refresh_from_db()
    check("a model saying 'approved' with no dossier does not approve anyone",
          review.decision == "pending", review.decision)
    check("the account is not activated", p1.is_active is False)
    check("and the case is parked for a person",
          review.review_state == "human_review", review.review_state)
    check("with the gate's verdict kept for whoever picks it up",
          bool((review.evidence or {}).get("clean_case_gate")))

    section("13. THERE IS NO AUTOMATIC REJECTION PATH AT ALL")
    u2, p2 = provider("norej@aqua.test", active=True)
    rej = _persist_review("consultant", p2, u2, outcome("rejected", confidence=0.1))
    rej.refresh_from_db()
    p2.refresh_from_db()
    check("a model saying 'rejected' records no rejection",
          rej.decision == "pending", rej.decision)
    check("the account is not deactivated", p2.is_active is True)
    check("it goes to a person instead", rej.review_state == "human_review")

    section("14. A CLEAN CASE STILL AUTO-APPROVES")
    u3, p3 = provider("clean@aqua.test")
    AIAccountReview.objects.create(
        subject_type="consultant", subject_id=p3.id, aqua_provider_id=u3.id,
        subject_user_email=u3.email, subject_display_name="Clean Co",
        decision="pending", review_state="evidence_received", confidence=0.0,
        dossier_id="dos_CLEAN", dossier_revision=1, dossier_status="complete",
        dossier_payload=clean_dossier(), submitted_event_at=now - timedelta(hours=1))
    cleaned = _persist_review("consultant", p3, u3, outcome("approved", confidence=0.93))
    cleaned.refresh_from_db()
    p3.refresh_from_db()
    check("a clean case is approved automatically",
          cleaned.decision == "approved", cleaned.decision)
    check("through the clean-case path",
          cleaned.decision_path == "automatic_clean_case", cleaned.decision_path)
    check("attributed to the rules engine",
          cleaned.decision_actor_type == "approved_rules_engine")
    check("it carries a decision id and version",
          bool(cleaned.decision_id) and cleaned.decision_version == 1)
    check("and the account really is activated", p3.is_active is True)

    section("15. A RE-SCORE NEVER REOPENS A CASE A PERSON SETTLED")
    u4, p4 = provider("settled@aqua.test")
    settled = AIAccountReview.objects.create(
        subject_type="consultant", subject_id=p4.id, aqua_provider_id=u4.id,
        subject_user_email=u4.email, subject_display_name="Settled Co",
        decision="rejected", review_state="decided", confidence=0.2,
        manually_overridden=True, decision_id="dec_EXISTING", decision_version=3,
        dossier_id="dos_CLEAN", dossier_status="complete",
        dossier_payload=clean_dossier())
    _persist_review("consultant", p4, u4, outcome("approved", confidence=0.99))
    settled.refresh_from_db()
    check("an operator's rejection survives a later clean re-score",
          settled.decision == "rejected", settled.decision)
    check("and its decision id is untouched", settled.decision_id == "dec_EXISTING")

    section("16. PARKED CASES ARE VISIBLE IN A NAMED QUEUE")
    counts = sla.queue_counts()
    check("the awaiting-decision queue exists",
          sla.QUEUE_AWAITING_DECISION in counts, str(list(counts)))
    parked = list(sla.queue(sla.QUEUE_AWAITING_DECISION))
    check("and the parked applications are in it", len(parked) >= 2, str(len(parked)))
    ids = {r.subject_id for r in parked}
    check("including the one the model wanted to approve", p1.id in ids)
    check("and the one it wanted to reject", p2.id in ids)
    check("but not the clean case it approved", p3.id not in ids)
    check("nor the one a person already settled", p4.id not in ids)

    section("17. NO OPERATIONAL SETTING CAN APPROVE PAST THE GATE")
    # §12.3: "no application may be approved or rejected without a terminal CRM
    # dossier, whatever an operational setting says". The auto-activation
    # toggle produces an outcome of approved/1.0 confidence, and that outcome
    # is advice like any other.
    from admin_portal.models import OperationalSettings
    from admin_portal.services.review_runner import run_review

    config = OperationalSettings.get_solo()
    config.auto_activate_new_accounts = True
    config.auto_activate_stagger_enabled = False
    config.save()

    u5, p5 = provider("toggle@aqua.test")
    toggled = run_review("consultant", p5, u5)
    toggled.refresh_from_db()
    p5.refresh_from_db()
    check("the auto-activate toggle cannot approve without evidence",
          toggled.decision == "pending", toggled.decision)
    check("and the account stays inactive", p5.is_active is False)
    check("the case goes to a person", toggled.review_state == "human_review")

    # Staggered mode is a different entry point and must not be a way round it.
    config.auto_activate_stagger_enabled = True
    config.save()
    u6, p6 = provider("stagger@aqua.test")
    run_review("consultant", p6, u6)
    staggered = AIAccountReview.objects.get(subject_type="consultant", subject_id=p6.id)
    p6.refresh_from_db()
    check("staggered mode is not a way around the gate either",
          staggered.decision == "pending", staggered.decision)
    check("and it activates nobody", p6.is_active is False)

    config.auto_activate_new_accounts = False
    config.auto_activate_stagger_enabled = False
    config.save()

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
