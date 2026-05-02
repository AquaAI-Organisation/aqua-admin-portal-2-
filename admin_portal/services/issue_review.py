"""OpenAI-backed triage for external incidents and consultant warnings."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

from .json_utils import sanitize_json
from .openai_runtime import get_openai_runtime_config
from .supabase_edge import has_issue_triage_function, invoke_json, issue_triage_url

logger = logging.getLogger(__name__)


ISSUE_SYSTEM_PROMPT = """You are the Aqua AI issue-triage intelligence.

Your job: analyse an existing platform issue involving a breeder or consultant
and produce a safe, evidence-based severity assessment plus recommended actions.

Rules:
- Base the answer only on the dossier provided.
- Never invent facts.
- Prefer conservative actions. If facts are weak or conflicting, escalate rather
  than taking irreversible action.
- Only recommend automatic actions that are safe:
  - "notify_super_admins"
  - "request_manual_review"
  - "deactivate_account_pending_review"
  - "set_warning_status"

Return STRICT JSON ONLY with this shape:
{
  "severity": "info" | "warning" | "critical",
  "summary": "<1-2 sentence summary>",
  "rationale": "<short paragraph>",
  "evidence": ["<bullet>", "<bullet>"],
  "recommended_actions": [
    {"action": "notify_super_admins"},
    {"action": "request_manual_review"},
    {"action": "deactivate_account_pending_review", "reason": "<reason>"},
    {"action": "set_warning_status", "value": "<status>"}
  ]
}
"""


@dataclass
class IssueReviewOutcome:
    severity: str
    summary: str
    rationale: str
    evidence: dict[str, Any]
    recommended_actions: list[dict[str, Any]]
    raw: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    error: str = ""


def _is_placeholder_key(key: str) -> bool:
    return not key or "REPLACE" in key.upper() or key == "sk-REPLACE-WITH-YOUR-GPT-4-KEY"


def build_incident_dossier(incident, user, profile=None) -> dict[str, Any]:
    return sanitize_json({
        "source_type": "incident",
        "source_id": str(incident.id),
        "subject": {
            "user_id": str(user.id),
            "email": user.email,
            "name": user.name or f"{user.first_name} {user.last_name}".strip(),
            "role": user.role,
            "is_verified": user.is_verified,
            "is_at_risk": user.is_at_risk,
            "trust_score": user.current_trust_score,
            "regulatory_tier": user.current_regulatory_tier,
        },
        "profile": _profile_snapshot(profile),
        "incident": {
            "incident_code": incident.incident_code,
            "severity_level": incident.severity_level,
            "penalty_points": incident.penalty_points,
            "description": incident.description,
            "evidence": incident.evidence or {},
            "related_entity_type": incident.related_entity_type,
            "related_entity_id": incident.related_entity_id,
            "occurred_at": incident.occurred_at.isoformat() if incident.occurred_at else None,
            "is_cleared": incident.is_cleared,
            "created_by": incident.created_by,
        },
    })


def build_warning_dossier(warning, user, consultant_profile=None) -> dict[str, Any]:
    return sanitize_json({
        "source_type": "consultant_warning",
        "source_id": str(warning.id),
        "subject": {
            "user_id": str(user.id),
            "email": user.email,
            "name": user.name or f"{user.first_name} {user.last_name}".strip(),
            "role": user.role,
            "is_verified": user.is_verified,
            "is_at_risk": user.is_at_risk,
            "trust_score": user.current_trust_score,
            "regulatory_tier": user.current_regulatory_tier,
        },
        "profile": _profile_snapshot(consultant_profile),
        "warning": {
            "title": warning.title,
            "message": warning.message,
            "severity": warning.severity,
            "status": warning.status,
            "metadata": warning.metadata or {},
            "created_at": warning.created_at.isoformat() if warning.created_at else None,
            "resolved_at": warning.resolved_at.isoformat() if warning.resolved_at else None,
        },
    })


def build_signal_dossier(source_type: str, title: str, payload: dict[str, Any], user, profile=None) -> dict[str, Any]:
    return sanitize_json({
        "source_type": source_type,
        "source_id": str(payload.get("source_id") or payload.get("message_id") or payload.get("booking_id") or payload.get("failure_id") or payload.get("snapshot_id") or ""),
        "subject": {
            "user_id": str(user.id),
            "email": user.email,
            "name": user.name or f"{user.first_name} {user.last_name}".strip(),
            "role": user.role,
            "is_verified": user.is_verified,
            "is_at_risk": user.is_at_risk,
            "trust_score": user.current_trust_score,
            "regulatory_tier": user.current_regulatory_tier,
        },
        "profile": _profile_snapshot(profile),
        "signal": {
            "title": title,
            "payload": payload,
        },
    })


def _profile_snapshot(profile) -> dict[str, Any]:
    if not profile:
        return {}
    data = {
        "company_name": getattr(profile, "company_name", "") or "",
        "is_active": getattr(profile, "is_active", False),
        "is_verified": getattr(profile, "is_verified", False),
        "verification_level": getattr(profile, "verification_level", "") or "",
        "business_address": getattr(profile, "business_address", "") or "",
        "business_phone": getattr(profile, "business_phone", "") or "",
        "rating": getattr(profile, "rating", None),
        "reviews_count": getattr(profile, "reviews_count", None),
        "metadata": getattr(profile, "metadata", {}) or {},
    }
    if hasattr(profile, "admin_status"):
        data["admin_status"] = profile.admin_status
        data["admin_notes"] = profile.admin_notes or ""
        data["completion_rate"] = getattr(profile, "completion_rate", None)
        data["cancellation_rate"] = getattr(profile, "cancellation_rate", None)
        data["complaint_count"] = getattr(profile, "complaint_count", None)
        data["total_bookings"] = getattr(profile, "total_bookings", None)
        data["completed_bookings"] = getattr(profile, "completed_bookings", None)
        data["cancelled_bookings"] = getattr(profile, "cancelled_bookings", None)
        data["average_response_time_hours"] = getattr(profile, "average_response_time_hours", None)
        data["overall_score"] = getattr(profile, "overall_score", None)
    else:
        data["total_sales"] = getattr(profile, "total_sales", None)
        data["successful_sales"] = getattr(profile, "successful_sales", None)
        data["returned_stock_count"] = getattr(profile, "returned_stock_count", None)
        data["species_count"] = getattr(profile, "species_count", None)
        data["healthy_stock_rate"] = getattr(profile, "healthy_stock_rate", None)
        data["stock_mortality_rate"] = getattr(profile, "stock_mortality_rate", None)
        data["disease_reported_rate"] = getattr(profile, "disease_reported_rate", None)
        data["local_trust_score"] = getattr(profile, "local_trust_score", None)
    return data


def call_issue_gpt(dossier: dict[str, Any]) -> IssueReviewOutcome:
    if has_issue_triage_function():
        edge_result = invoke_json(
            issue_triage_url(),
            {
                "kind": "issue_triage",
                "dossier": dossier,
                "system_prompt": ISSUE_SYSTEM_PROMPT,
            },
        )
        if edge_result.ok:
            raw = edge_result.payload or {}
            severity = str(raw.get("severity", "warning")).lower()
            if severity not in {"info", "warning", "critical"}:
                severity = "warning"
            return IssueReviewOutcome(
                severity=severity,
                summary=str(raw.get("summary", "")),
                rationale=str(raw.get("rationale", "")),
                evidence={"bullets": raw.get("evidence", []), "runtime_source": "supabase_edge_function"},
                recommended_actions=list(raw.get("recommended_actions", [])),
                raw=raw,
                model=str(raw.get("model", "supabase-edge-issue-triage")),
                error="",
            )
        logger.warning("Supabase issue-triage function failed: %s", edge_result.error)

    runtime = get_openai_runtime_config()
    api_key = runtime.key
    model = runtime.model or str(getattr(settings, "OPENAI_MODEL", "gpt-4o")).strip()

    if _is_placeholder_key(api_key):
        return IssueReviewOutcome(
            severity="warning",
            summary="OpenAI key missing.",
            rationale="Issue triage could not run because OPENAI_API_KEY is not configured.",
            evidence={"bullets": []},
            recommended_actions=[{"action": "notify_super_admins"}],
            model=model,
            error=runtime.error or "OpenAI runtime key is not configured from Supabase edge function or environment.",
        )

    try:
        from openai import OpenAI
    except ImportError:
        return IssueReviewOutcome(
            severity="warning",
            summary="OpenAI package missing.",
            rationale="Issue triage could not run because the openai package is not installed.",
            evidence={"bullets": []},
            recommended_actions=[{"action": "notify_super_admins"}],
            model=model,
            error="openai package not installed. Run: pip install -r requirements.txt",
        )

    client = OpenAI(api_key=api_key)

    try:
        completion = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            temperature=0.1,
            messages=[
                {"role": "system", "content": ISSUE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": "Triage this Aqua AI issue and return the JSON result:\n"
                    + json.dumps(dossier, default=str),
                },
            ],
        )
        content = completion.choices[0].message.content or "{}"
        raw = json.loads(content)
    except Exception as exc:
        logger.exception("Issue triage OpenAI call failed")
        return IssueReviewOutcome(
            severity="warning",
            summary="AI triage failed.",
            rationale="The issue could not be triaged automatically because the OpenAI request failed.",
            evidence={"bullets": []},
            recommended_actions=[{"action": "notify_super_admins"}],
            model=model,
            error=str(exc),
        )

    severity = str(raw.get("severity", "warning")).lower()
    if severity not in {"info", "warning", "critical"}:
        severity = "warning"

    return IssueReviewOutcome(
        severity=severity,
        summary=str(raw.get("summary", "")),
        rationale=str(raw.get("rationale", "")),
        evidence={"bullets": raw.get("evidence", [])},
        recommended_actions=list(raw.get("recommended_actions", [])),
        raw=raw,
        model=model,
        error="",
    )
