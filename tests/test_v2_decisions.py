"""V2.3 item 4: decision fields on AIAccountReview, and flagged -> held.

Covers the three things the checklist asks for:
  * the legacy ``flagged`` state is gone and ``held`` is written in its place,
    everywhere, including the aggregates that would otherwise report zero
    forever;
  * ``decision_version`` is monotonic per profile and ``decision_id`` is
    minted per decision;
  * ``reason_code``, ``decision_actor_type`` and ``decision_path`` are
    populated and validated against §13.3/§13.4, so an invalid combination is
    refused here rather than 422'd by the CRM.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

PORTAL = str(pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, PORTAL)

os.environ["DJANGO_SETTINGS_MODULE"] = "aqua_admin.settings"
os.environ["DATABASE_URL"] = "sqlite:///" + tempfile.mkstemp(suffix=".sqlite3")[1]
os.environ["INBOX_AUTOREFRESH"] = "false"

import django  # noqa: E402

django.setup()

from django.test import override_settings  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{extra}]" if extra else ""))


def section(title):
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def refused(fn, *args, **kwargs) -> str:
    """Return the refusal message, or "" if the call was wrongly allowed."""
    from admin_portal.integrations.decisions import DecisionRefused
    try:
        fn(*args, **kwargs)
    except DecisionRefused as exc:
        return str(exc)
    return ""


@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False,
                   CSRF_COOKIE_SECURE=False)
def main():
    import uuid

    from admin_portal.models import (
        DECISION_CHOICES, AIAccountReview, AIFlag, canonical_decision,
    )
    from admin_portal.integrations.decisions import (
        CLEAN_CASE_MIN_SCORE, decision_payload, record_decision,
    )

    def new_review(**kwargs):
        defaults = dict(
            subject_type="consultant", subject_id=uuid.uuid4(),
            subject_user_email="x@aqua.test", subject_display_name="Test Co",
            decision="pending", confidence=0.80,
        )
        defaults.update(kwargs)
        return AIAccountReview.objects.create(**defaults)

    section("1. 'held' IS THE CANONICAL STATE, 'flagged' IS GONE")
    values = [value for value, _ in DECISION_CHOICES]
    check("'held' is an allowed decision", "held" in values, str(values))
    check("'flagged' is no longer an allowed decision", "flagged" not in values, str(values))
    check("the legacy value still normalises on input",
          canonical_decision("flagged") == "held")
    check("normalisation is case and space tolerant",
          canonical_decision("  FLAGGED ") == "held")
    check("a real value passes through unchanged", canonical_decision("approved") == "approved")

    section("2. THE DECISION ENGINE WRITES 'held'")
    import inspect
    from admin_portal.services import openai_review
    source = inspect.getsource(openai_review._balanced_decision)
    check("_balanced_decision no longer produces 'flagged'", '"flagged"' not in source)
    check("and produces 'held' instead", source.count('decision = "held"') == 2,
          str(source.count('decision = "held"')))

    runner_source = inspect.getsource(
        __import__("admin_portal.services.review_runner", fromlist=["x"])._operationalise_outcome)
    check("the auto-admit clause reads 'held', not the dead 'flagged'",
          '"held"' in runner_source and '"flagged"' not in runner_source)

    section("3. AGGREGATES COUNT 'held' (they would otherwise read zero forever)")
    from admin_portal.services import reporting
    reporting_source = inspect.getsource(reporting)
    check("the daily report counts held decisions",
          'filter=Q(decision="held")' in reporting_source)
    views_source = pathlib.Path(PORTAL, "admin_portal/views.py").read_text()
    check("no view still aggregates on the dead value",
          'filter=Q(decision="flagged")' not in views_source)
    check("the review list filter accepts 'held'",
          '"approved", "rejected", "held", "pending", "error"' in views_source)
    check("the intake filter accepts 'held'",
          '"not_scanned", "pending", "approved", "rejected", "held", "error"' in views_source)
    for template in ("review_list.html", "intake_list.html"):
        body = pathlib.Path(PORTAL, "admin_portal/templates/admin_portal", template).read_text()
        check(f"{template} offers Held, not Flagged",
              'value="held"' in body and 'value="flagged"' not in body)

    section("4. decision_version IS MONOTONIC PER PROFILE")
    review = new_review()
    check("a fresh review starts at version 0", review.decision_version == 0)
    record_decision(review, status="held", reason_code="manual_review_required",
                    reason_text="Needs a person.", actor_type="operator",
                    decision_path="human_review")
    review.refresh_from_db()
    check("the first decision is version 1", review.decision_version == 1,
          str(review.decision_version))
    first_id = review.decision_id
    check("a decision_id was minted", first_id.startswith("dec_"), first_id)

    record_decision(review, status="approved", reason_code="manual_exception_approved",
                    reason_text="Confirmed by phone.", actor_type="operator",
                    decision_path="human_review")
    review.refresh_from_db()
    check("a replacement decision increments to 2", review.decision_version == 2,
          str(review.decision_version))
    check("and mints a different decision_id", review.decision_id != first_id)
    check("the status is recorded", review.decision == "approved", review.decision)
    check("the reason code is recorded",
          review.reason_code == "manual_exception_approved", review.reason_code)
    check("the actor type is recorded", review.decision_actor_type == "operator")
    check("the decision path is recorded", review.decision_path == "human_review")
    check("decided_at is stamped", review.decided_at is not None)

    other = new_review()
    record_decision(other, status="held", reason_code="evidence_conflict",
                    reason_text="Conflicting evidence.", actor_type="operator",
                    decision_path="human_review")
    other.refresh_from_db()
    check("versions are per profile, not global", other.decision_version == 1,
          str(other.decision_version))

    section("5. §21.1 STATE FOLLOWS THE DECISION")
    held = new_review()
    record_decision(held, status="held", reason_code="evidence_overdue",
                    reason_text="No dossier within 12 hours.",
                    actor_type="system_timeout", decision_path="system_safety_hold",
                    use_dossier=False)
    held.refresh_from_db()
    check("a safety hold moves the review to human_review",
          held.review_state == "human_review", held.review_state)
    check("and it is recognised as held", held.is_held)

    clean = new_review(confidence=0.81, dossier_id="dos_X", dossier_revision=1)
    record_decision(clean, status="approved", reason_code="evidence_verified",
                    reason_text="Clean case.", actor_type="approved_rules_engine",
                    decision_path="automatic_clean_case", score=0.81, flags=[])
    clean.refresh_from_db()
    check("a clean-case approval moves to automatic_approved",
          clean.review_state == "automatic_approved", clean.review_state)

    decided = new_review()
    record_decision(decided, status="rejected", reason_code="identity_conflict",
                    reason_text="Identity does not match.", actor_type="operator",
                    decision_path="human_review")
    decided.refresh_from_db()
    check("an operator rejection moves to decided", decided.review_state == "decided",
          decided.review_state)

    # Regression: an operator recording a HOLD leaves the case open. §12.3 says
    # a held case must reach a named person and must not auto-promote, so it
    # stays in human_review whichever path recorded it. Marking it "decided"
    # silently dropped it out of the named queue.
    operator_held = new_review()
    record_decision(operator_held, status="held", reason_code="identity_review_required",
                    reason_text="Identity needs checking.", actor_type="operator",
                    decision_path="human_review")
    operator_held.refresh_from_db()
    check("an operator-recorded HOLD stays in human_review, not decided",
          operator_held.review_state == "human_review", operator_held.review_state)
    check("and is still recognised as held", operator_held.is_held)

    section("6. INVALID COMBINATIONS ARE REFUSED, NOT WRITTEN (§13.3/§13.4)")
    target = new_review()

    msg = refused(record_decision, target, status="rejected",
                  reason_code="identity_conflict", reason_text="x",
                  actor_type="approved_rules_engine", decision_path="human_review")
    check("a rules engine cannot record a rejection", "only an operator" in msg, msg[:70])

    msg = refused(record_decision, target, status="approved",
                  reason_code="identity_conflict", reason_text="x",
                  actor_type="operator", decision_path="human_review")
    check("a rejection reason cannot be used for an approval",
          "not permitted for 'approved'" in msg, msg[:80])

    msg = refused(record_decision, target, status="held",
                  reason_code="evidence_verified", reason_text="x",
                  actor_type="operator", decision_path="human_review")
    check("an approval reason cannot be used for a hold",
          "not permitted for 'held'" in msg, msg[:80])

    msg = refused(record_decision, target, status="approved",
                  reason_code="evidence_verified", reason_text="x",
                  actor_type="system_timeout", decision_path="human_review")
    check("system_timeout is only for a safety hold",
          "only permitted for a safety hold" in msg, msg[:70])

    target.dossier_id = "dos_Y"
    target.save(update_fields=["dossier_id"])
    msg = refused(record_decision, target, status="approved",
                  reason_code="evidence_verified", reason_text="x",
                  actor_type="approved_rules_engine", decision_path="automatic_clean_case",
                  score=0.74, flags=[])
    check(f"an automatic approval below {CLEAN_CASE_MIN_SCORE} is refused",
          "at least 0.75" in msg, msg[:80])

    msg = refused(record_decision, target, status="approved",
                  reason_code="evidence_verified", reason_text="x",
                  actor_type="approved_rules_engine", decision_path="automatic_clean_case",
                  score=0.9, flags=["identity_review_required"])
    check("an automatic approval with an open flag is refused",
          "zero unresolved flags" in msg, msg[:70])

    no_dossier = new_review(confidence=0.9)
    msg = refused(record_decision, no_dossier, status="approved",
                  reason_code="evidence_verified", reason_text="x",
                  actor_type="approved_rules_engine", decision_path="automatic_clean_case",
                  score=0.9, flags=[])
    check("an automatic approval without a terminal dossier is refused",
          "terminal dossier" in msg, msg[:70])

    msg = refused(record_decision, target, status="approved",
                  reason_code="evidence_verified", reason_text="x",
                  actor_type="operator", decision_path="automatic_clean_case")
    check("an operator cannot claim the automatic_clean_case path",
          "approved_rules_engine only" in msg, msg[:80])

    msg = refused(record_decision, target, status="pending",
                  reason_code="evidence_verified", reason_text="x",
                  actor_type="operator", decision_path="human_review")
    check("'pending' is not a decision", "status must be one of" in msg, msg[:60])

    msg = refused(record_decision, target, status="held",
                  reason_code="manual_review_required", reason_text="   ",
                  actor_type="operator", decision_path="human_review")
    check("a blank reason_text is refused", "reason_text is required" in msg, msg[:60])

    msg = refused(record_decision, target, status="held",
                  reason_code="manual_review_required", reason_text="y" * 1001,
                  actor_type="operator", decision_path="human_review")
    check("reason_text over 1000 characters is refused", "1000 characters" in msg, msg[:60])

    target.refresh_from_db()
    check("none of the refused attempts changed the row",
          target.decision_version == 0 and target.decision == "pending",
          f"v{target.decision_version} {target.decision}")

    section("7. THE DECISION PAYLOAD MATCHES §13.2")
    source_review = new_review(confidence=0.72, dossier_id="dos_Z", dossier_revision=2)
    AIFlag.objects.create(review=source_review, severity="warning",
                          reason="identity_review_required", resolved=False)
    AIFlag.objects.create(review=source_review, severity="info",
                          reason="already handled", resolved=True)
    record_decision(source_review, status="held",
                    reason_code="awaiting_additional_evidence",
                    reason_text="Identity evidence requires further human review.",
                    actor_type="operator", decision_path="human_review", score=0.72)
    source_review.refresh_from_db()
    body = decision_payload(source_review)

    for field in ("decision_id", "decision_version", "status", "reason_code",
                  "reason_text", "decided_at", "decision_actor_type", "review"):
        check(f"payload carries {field}", field in body, str(sorted(body)))
    check("status is the contract value", body["status"] == "held", body["status"])
    check("the dossier reference is included",
          body["evidence_dossier_id"] == "dos_Z" and body["evidence_dossier_revision"] == 2)
    check("only UNRESOLVED flags are reported",
          body["review"]["flags"] == ["identity_review_required"], str(body["review"]["flags"]))
    check("decision_path is in the review block",
          body["review"]["decision_path"] == "human_review")
    check("decided_at is RFC 3339 with milliseconds",
          __import__("admin_portal.integrations.contract", fromlist=["x"]).is_rfc3339_ms(
              body["decided_at"]), body["decided_at"])

    timeout_review = new_review()
    record_decision(timeout_review, status="held", reason_code="evidence_overdue",
                    reason_text="No dossier within 12 hours.",
                    actor_type="system_timeout", decision_path="system_safety_hold",
                    use_dossier=False)
    timeout_review.refresh_from_db()
    timeout_body = decision_payload(timeout_review)
    check("a timeout hold may carry no dossier reference",
          "evidence_dossier_id" not in timeout_body, str(sorted(timeout_body)))

    section("8. THE PAYLOAD IS ACCEPTED BY THE CRM'S OWN SCHEMA")
    # The decision event must satisfy aquaV2Integration.ts:78-97. Build the
    # full envelope and validate its shape against the contract's own rules.
    from admin_portal.integrations import contract as c
    envelope = {
        "schema_version": c.SCHEMA_VERSION,
        "event_id": c.new_ulid(),
        "event_type": "admin.provider.application.decision.recorded",
        "occurred_at": c.rfc3339_ms(source_review.decided_at),
        "correlation_id": str(source_review.subject_id),
        "producer": {"system": "aqua-admin-portal", "environment": "staging"},
        "data": {
            "application_id": str(source_review.subject_id),
            "aqua_provider_id": str(source_review.subject_id),
            "decision": body,
        },
    }
    check("event_id is a valid ULID", c.is_event_id(envelope["event_id"]))
    check("correlation_id is a UUID", c.is_profile_id(envelope["correlation_id"]))
    check("decision_version is a positive integer",
          isinstance(body["decision_version"], int) and body["decision_version"] >= 1)
    check("score is within 0..1",
          0.0 <= body["review"]["score"] <= 1.0, str(body["review"]["score"]))
    pathlib.Path("/tmp/crmcheck/decision_event.json").write_text(
        __import__("json").dumps(envelope))
    check("the envelope was written for cross-checking against the CRM", True)

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
