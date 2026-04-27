"""AI analysis for support inbox enquiries."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.utils import timezone

from ..models import AIFlaggedIssue, SupportInquiry
from .json_utils import sanitize_json
from .notifier import notify_issue
from .openai_review import _is_placeholder_key

logger = logging.getLogger(__name__)


PROMPT = """You are Aqua AI's support-enquiry triage intelligence.

Analyse the support email and recommend safe next actions.
Only use facts provided in the enquiry context.

Allowed actions:
- reply_acknowledge
- request_more_information
- suspend_matched_account
- reactivate_matched_account
- open_issue
- mark_resolved_without_action

Return strict JSON:
{
  "summary": "<1-2 sentence summary>",
  "rationale": "<short paragraph>",
  "recommended_actions": [
    {"action": "reply_acknowledge", "label": "Send acknowledgement", "draft_reply": "..."},
    {"action": "open_issue", "label": "Open issue for review", "severity": "warning", "reason": "..."}
  ]
}
"""


@dataclass
class InquiryOutcome:
    summary: str
    rationale: str
    recommended_actions: list[dict[str, Any]]
    raw: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    error: str = ""


def analyse_inquiry(inquiry: SupportInquiry) -> InquiryOutcome:
    api_key = getattr(settings, "OPENAI_API_KEY", "")
    model = getattr(settings, "OPENAI_MODEL", "gpt-4o")

    if _is_placeholder_key(api_key):
        return _heuristic_outcome(inquiry, model, "OpenAI key is not configured.")

    try:
        from openai import OpenAI
    except ImportError:
        return _heuristic_outcome(inquiry, model, "openai package is not installed.")

    client = OpenAI(api_key=api_key)
    dossier = {
        "from_email": inquiry.from_email,
        "from_name": inquiry.from_name,
        "subject": inquiry.subject,
        "body_text": inquiry.body_text,
        "matched_entity_type": inquiry.matched_entity_type,
        "matched_entity_id": inquiry.matched_entity_id,
    }
    try:
        completion = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            temperature=0.1,
            messages=[
                {"role": "system", "content": PROMPT},
                {"role": "user", "content": json.dumps(dossier, default=str)},
            ],
        )
        raw = json.loads(completion.choices[0].message.content or "{}")
        actions = sanitize_json(list(raw.get("recommended_actions", [])))
        return InquiryOutcome(
            summary=str(raw.get("summary", "")),
            rationale=str(raw.get("rationale", "")),
            recommended_actions=actions,
            raw=sanitize_json(raw),
            model=model,
            error="",
        )
    except Exception as exc:
        logger.exception("Inquiry analysis failed")
        return _heuristic_outcome(inquiry, model, str(exc))


def persist_inquiry_analysis(inquiry: SupportInquiry) -> SupportInquiry:
    outcome = analyse_inquiry(inquiry)
    inquiry.ai_summary = outcome.summary
    inquiry.ai_rationale = outcome.rationale
    inquiry.ai_recommended_actions = outcome.recommended_actions
    inquiry.ai_raw = outcome.raw
    inquiry.ai_model = outcome.model
    inquiry.ai_error = outcome.error
    inquiry.status = "triaged" if not outcome.error else "error"
    inquiry.save(update_fields=["ai_summary", "ai_rationale", "ai_recommended_actions", "ai_raw", "ai_model", "ai_error", "status", "updated_at"])
    return inquiry


def apply_inquiry_action(inquiry: SupportInquiry, action: dict[str, Any], *, actor, state_handler) -> str:
    name = str(action.get("action", "")).strip()
    if name == "reply_acknowledge":
        inquiry.response_draft = str(action.get("draft_reply", "")).strip()
        inquiry.status = "triaged"
        inquiry.save(update_fields=["response_draft", "status", "updated_at"])
        return "Drafted an acknowledgement reply."
    if name == "request_more_information":
        inquiry.response_draft = str(action.get("draft_reply", "")).strip()
        inquiry.status = "triaged"
        inquiry.save(update_fields=["response_draft", "status", "updated_at"])
        return "Drafted a request for more information."
    if name == "suspend_matched_account":
        if inquiry.matched_entity_type and inquiry.matched_entity_id:
            return state_handler(inquiry.matched_entity_type, inquiry.matched_entity_id, activate=False, actor=actor)
        return "No matched account was available to suspend."
    if name == "reactivate_matched_account":
        if inquiry.matched_entity_type and inquiry.matched_entity_id:
            return state_handler(inquiry.matched_entity_type, inquiry.matched_entity_id, activate=True, actor=actor)
        return "No matched account was available to re-activate."
    if name == "open_issue":
        issue = AIFlaggedIssue.objects.create(
            source_type="support_inquiry",
            source_id=inquiry.message_id,
            subject_type=inquiry.matched_entity_type if inquiry.matched_entity_type in {"breeder", "consultant"} else "",
            subject_user_email=inquiry.from_email,
            subject_display_name=inquiry.from_name or inquiry.from_email,
            title=inquiry.subject[:255] if inquiry.subject else "Support enquiry",
            severity=str(action.get("severity", "warning")).lower(),
            status="open",
            summary=str(action.get("reason", inquiry.ai_summary)),
            rationale=inquiry.ai_rationale,
            evidence={"bullets": [inquiry.body_text[:300]]},
            recommended_actions=[sanitize_json(action)],
            applied_actions=[],
            source_payload={"message_id": inquiry.message_id},
            openai_raw=sanitize_json(inquiry.ai_raw),
            ai_model=inquiry.ai_model,
            triaged_at=timezone.now(),
        )
        delivery = notify_issue(issue)
        issue.notified_emails = delivery.get("recipients", [])
        issue.notified_slack = delivery.get("slack", False)
        issue.save(update_fields=["notified_emails", "notified_slack"])
        inquiry.status = "actioned"
        inquiry.save(update_fields=["status", "updated_at"])
        return "Opened a flagged issue from the support enquiry."
    inquiry.status = "actioned"
    inquiry.save(update_fields=["status", "updated_at"])
    return "Marked the enquiry as actioned."


def _heuristic_outcome(inquiry: SupportInquiry, model: str, error: str) -> InquiryOutcome:
    lowered = (inquiry.body_text or "").lower()
    actions = [
        {
            "action": "reply_acknowledge",
            "label": "Send acknowledgement",
            "draft_reply": "Thanks for contacting Aqua AI support. We have received your enquiry and are reviewing it now.",
        },
        {
            "action": "request_more_information",
            "label": "Request more information",
            "draft_reply": "Thanks for contacting Aqua AI support. Please reply with any extra screenshots, order details, or account context so we can investigate properly.",
        },
    ]
    if inquiry.matched_entity_type and any(token in lowered for token in ["report", "fraud", "scam", "complaint", "abuse"]):
        actions.append(
            {
                "action": "open_issue",
                "label": "Open issue for review",
                "severity": "warning",
                "reason": "The support message appears to report misconduct or a complaint.",
            }
        )
    return InquiryOutcome(
        summary="Support enquiry imported for review.",
        rationale="The enquiry was triaged with a heuristic fallback because live AI analysis was unavailable.",
        recommended_actions=actions,
        raw={"fallback": True},
        model=model,
        error=error,
    )
