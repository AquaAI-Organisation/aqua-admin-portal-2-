"""V2.3 items 5 (reconciliation) and 7 (evidence SLA, safety hold, queues).

The behaviour under test is mostly about time, so every case drives the clock
explicitly rather than sleeping: a review is created with a chosen
``submitted_event_at`` and the sweep is asked to evaluate at a chosen ``now``.

The point of the safety hold is that it is NOT a decision about the applicant.
These checks lean on that: a hold must be recorded, synchronised and queued,
and must never become an approval or a rejection on its own.
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


@override_settings(
    AQUA_V2_ENVIRONMENT="staging",
    AQUA_ADMIN_TO_CRM_WEBHOOK_KEYS=ADMIN_RING,
    AQUA_CRM_V2_EVENTS_URL="https://crm.aquaai.uk/api/integrations/v2/admin-events",
    ADMIN_EMAILS=[],
    SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False,
)
def main():
    from admin_portal.models import AIAccountReview, V2InboxEvent, V2OutboxEvent
    from admin_portal.integrations import evidence_sla as sla
    from admin_portal.integrations import reconciliation as recon
    from admin_portal.integrations.decisions import record_decision

    now = timezone.now()

    def review_at(hours_ago: float, *, state="awaiting_crm_evidence", dossier="", **kw):
        return AIAccountReview.objects.create(
            subject_type="consultant", subject_id=uuid.uuid4(),
            aqua_provider_id=uuid.uuid4(),
            subject_user_email="x@aqua.test", subject_display_name=f"Co {hours_ago}h",
            decision="pending", review_state=state, dossier_id=dossier,
            submitted_event_at=now - timedelta(hours=hours_ago), **kw)

    section("1. THE THREE CLOCKS (spec §12.1)")
    check("target is 2 hours", sla.DOSSIER_TARGET == timedelta(hours=2))
    check("deadline is 12 hours", sla.DOSSIER_DEADLINE == timedelta(hours=12))
    check("applicant promise is 24 hours", sla.APPLICANT_PROMISE == timedelta(hours=24))

    fresh = review_at(0.5)
    state = sla.sla_state(fresh, now=now)
    check("a half-hour-old case is on target", state.state == "on_target", state.state)
    check("its deadline is 12h after submission",
          state.deadline_at == fresh.submitted_event_at + timedelta(hours=12))
    check("its promise is 24h after submission",
          state.promise_at == fresh.submitted_event_at + timedelta(hours=24))

    check("a 3-hour-old case is past target",
          sla.sla_state(review_at(3), now=now).state == "past_target")
    check("a 13-hour-old case is overdue",
          sla.sla_state(review_at(13), now=now).state == "overdue")
    check("a case with a dossier is satisfied, however old",
          sla.sla_state(review_at(40, dossier="dos_1"), now=now).state == "satisfied")
    check("a case never submitted has no clock",
          sla.sla_state(AIAccountReview.objects.create(
              subject_type="breeder", subject_id=uuid.uuid4(), decision="pending"),
              now=now).state == "not_submitted")

    section("2. THE 12-HOUR SAFETY HOLD")
    AIAccountReview.objects.all().delete()
    on_target = review_at(1)
    past_t = review_at(5)
    overdue_a = review_at(13)
    overdue_b = review_at(30)
    satisfied = review_at(20, dossier="dos_9")

    due = list(sla.due_for_safety_hold(now=now))
    check("only overdue cases are due for a hold", len(due) == 2, str(len(due)))
    check("the on-target case is not due", on_target.pk not in [r.pk for r in due])
    check("the past-target case is not due", past_t.pk not in [r.pk for r in due])
    check("a case with a dossier is never due", satisfied.pk not in [r.pk for r in due])

    counts = sla.sweep(now=now, notify=False)
    check("the sweep held both overdue cases", counts["held"] == 2, str(counts))
    check("and reports how many are past target", counts["past_target"] == 1, str(counts))

    overdue_a.refresh_from_db()
    check("the held case is 'held', not rejected", overdue_a.decision == "held",
          overdue_a.decision)
    check("with reason_code evidence_overdue",
          overdue_a.reason_code == "evidence_overdue", overdue_a.reason_code)
    check("recorded by system_timeout",
          overdue_a.decision_actor_type == "system_timeout", overdue_a.decision_actor_type)
    check("on the system_safety_hold path",
          overdue_a.decision_path == "system_safety_hold", overdue_a.decision_path)
    check("it carries no dossier reference (§13.3)", overdue_a.dossier_id == "")
    check("it has a decision_version", overdue_a.decision_version == 1,
          str(overdue_a.decision_version))
    check("and sits in human_review, awaiting a person",
          overdue_a.review_state == "human_review", overdue_a.review_state)

    section("3. THE HOLD IS SYNCHRONISED TO THE CRM (§12.1)")
    queued = V2OutboxEvent.objects.filter(
        event_type="admin.provider.application.decision.recorded")
    check("a decision event was queued per hold", queued.count() == 2, str(queued.count()))
    import json
    body = json.loads(queued.first().raw_body)
    check("the event carries the application id",
          body["data"]["application_id"] in {str(overdue_a.subject_id), str(overdue_b.subject_id)})
    check("and a provider id", bool(body["data"]["aqua_provider_id"]))
    check("the decision status is held", body["data"]["decision"]["status"] == "held")
    check("the reason is evidence_overdue",
          body["data"]["decision"]["reason_code"] == "evidence_overdue")
    check("the actor is system_timeout",
          body["data"]["decision"]["decision_actor_type"] == "system_timeout")
    check("no dossier reference is sent for a timeout hold",
          "evidence_dossier_id" not in body["data"]["decision"])
    pathlib.Path("/tmp/crmcheck/hold_event.json").write_text(queued.first().raw_body)

    section("4. THE SWEEP IS IDEMPOTENT AND NEVER AUTO-PROMOTES (§12.3)")
    before = V2OutboxEvent.objects.count()
    counts = sla.sweep(now=now, notify=False)
    check("a second sweep holds nothing new", counts["held"] == 0, str(counts))
    check("and queues no duplicate event", V2OutboxEvent.objects.count() == before)
    overdue_a.refresh_from_db()
    check("the held case stays held", overdue_a.decision == "held", overdue_a.decision)
    check("its version did not move", overdue_a.decision_version == 1,
          str(overdue_a.decision_version))

    from admin_portal.services.review_runner import _already_reviewed_ids
    check("a held case is excluded from re-review, so it cannot auto-promote",
          overdue_a.subject_id in _already_reviewed_ids("consultant"))

    late = review_at(13, dossier="")
    late.dossier_id = "dos_late"
    late.save(update_fields=["dossier_id"])
    check("evidence arriving before the sweep cancels the hold",
          sla.record_safety_hold(late, now=now) is False)
    late.refresh_from_db()
    check("and that case is untouched", late.decision == "pending", late.decision)

    section("5. NAMED QUEUES (§23.3)")
    counts = sla.queue_counts()
    check("evidence_overdue queue holds both cases",
          counts[sla.QUEUE_EVIDENCE_OVERDUE] == 2, str(counts))

    identity = review_at(2)
    record_decision(identity, status="held", reason_code="identity_review_required",
                    reason_text="Identity needs checking.", actor_type="operator",
                    decision_path="human_review")
    check("identity_review queue picks it up",
          sla.queue_counts()[sla.QUEUE_IDENTITY_REVIEW] == 1,
          str(sla.queue_counts()))

    dead = V2OutboxEvent.objects.first()
    dead.status = "dead_letter"
    dead.save(update_fields=["status"])
    V2InboxEvent.objects.create(
        producer_system="aqua-provider-crm", event_id="01J" + "A" * 23,
        event_type="crm.provider.evidence_dossier.ready", schema_version="2.2",
        body_sha256="f" * 64, processing_state="failed", response_code=422,
        error="unknown_application")
    check("integration_exceptions counts dead letters and failed inbox rows",
          sla.queue_counts()[sla.QUEUE_INTEGRATION_EXCEPTIONS] == 2,
          str(sla.queue_counts()))

    resolved = sla.queue(sla.QUEUE_EVIDENCE_OVERDUE).first()
    record_decision(resolved, status="approved", reason_code="manual_exception_approved",
                    reason_text="Operator confirmed by phone.", actor_type="operator",
                    decision_path="human_review")
    check("a human decision removes the case from the queue",
          sla.queue_counts()[sla.QUEUE_EVIDENCE_OVERDUE] == 1,
          str(sla.queue_counts()))

    section("6. THE QUEUE PAGE RENDERS FOR AN ADMIN")
    from django.contrib.auth import get_user_model
    from django.test import Client
    AdminUser = get_user_model()
    admin = AdminUser.objects.create_user(email="queue@humara.io", password="pw-test-12345")
    admin.is_active = True; admin.must_change_password = False
    admin.role = "super_admin"; admin.is_staff = True; admin.is_superuser = True
    admin.save()
    client = Client()
    check("admin signed in", client.login(email="queue@humara.io", password="pw-test-12345"))
    for name in (sla.QUEUE_EVIDENCE_OVERDUE, sla.QUEUE_IDENTITY_REVIEW,
                 sla.QUEUE_INTEGRATION_EXCEPTIONS):
        response = client.get("/admin-portal/queues/", {"queue": name})
        check(f"the {name} queue renders", response.status_code == 200,
              str(response.status_code))
        check(f"{name} has no unrendered template tags",
              "{{" not in response.content.decode() and "{%" not in response.content.decode())
    anon = Client()
    r = anon.get("/admin-portal/queues/")
    check("the queue page requires sign-in",
          r.status_code == 302 and "login" in r.get("Location", ""), str(r.status_code))

    section("7. RECONCILIATION FINDS WHAT FAILS SILENTLY (item 5)")
    report = recon.build_report(now=now)
    codes = {f.code: f for f in report.findings}
    check("a dead-lettered event is reported", "outbox_dead_letter" in codes, str(sorted(codes)))
    check("as critical", codes["outbox_dead_letter"].severity == "critical")
    check("a failed inbox row is reported", "inbox_failed" in codes, str(sorted(codes)))
    check("the report is not ok when something diverged", report.ok is False)
    check("totals are included", report.totals["reviews"] > 0, str(report.totals)[:90])
    check("queue counts ride along", "queues" in report.totals)

    stuck = V2OutboxEvent.objects.filter(status="pending").first()
    if stuck:
        stuck.first_attempt_at = now - timedelta(hours=5)
        stuck.save(update_fields=["first_attempt_at"])
    never = V2OutboxEvent.objects.filter(status="pending").last()
    if never:
        never.first_attempt_at = None
        never.created_at = now - timedelta(hours=5)
        never.save(update_fields=["first_attempt_at", "created_at"])
    report = recon.build_report(now=now)
    codes = {f.code for f in report.findings}
    check("an outbox row stuck after a first attempt is reported",
          "outbox_stuck" in codes or "outbox_never_attempted" in codes, str(sorted(codes)))

    unheld = review_at(20)
    report = recon.build_report(now=now)
    codes = {f.code: f for f in report.findings}
    check("an overdue case with no hold is reported as critical",
          "evidence_overdue_unheld" in codes
          and codes["evidence_overdue_unheld"].severity == "critical", str(sorted(codes)))

    breached = review_at(30, state="human_review")
    record_decision(breached, status="held", reason_code="evidence_overdue",
                    reason_text="Overdue.", actor_type="system_timeout",
                    decision_path="system_safety_hold", use_dossier=False)
    report = recon.build_report(now=now)
    codes = {f.code for f in report.findings}
    check("a breached 24-hour applicant promise is reported",
          "applicant_promise_breached" in codes, str(sorted(codes)))

    text = recon.render(report)
    check("the report renders as readable text", "reconciliation" in text.lower())
    check("and names the findings", "evidence_overdue_unheld" in text)
    check("worst severity is surfaced", report.worst == "critical", report.worst)

    section("8. A CLEAN LEDGER REPORTS NOTHING AND STAYS QUIET")
    V2OutboxEvent.objects.all().delete()
    V2InboxEvent.objects.all().delete()
    AIAccountReview.objects.all().delete()
    clean = recon.build_report(now=now)
    check("no findings on a clean ledger", clean.findings == [], str(clean.findings))
    check("the report is ok", clean.ok is True)
    check("and no alert is sent", recon.notify(clean) is False)
    check("rendering still produces a report", "No divergence found." in recon.render(clean))

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
