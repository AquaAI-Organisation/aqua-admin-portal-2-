"""GPT-4 powered review pipeline.

Each pending breeder/consultant profile is converted into a compact JSON
"dossier" and handed to GPT-4 with a strict JSON-output system prompt. The
model returns a decision, confidence, rationale, list of evidence items and
recommended actions. We then map confidence → approved / rejected / flagged
using the AI_APPROVE_THRESHOLD / AI_REJECT_THRESHOLD settings.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are the Aqua AI signup-review intelligence.

Your job: decide whether a newly created BREEDER or CONSULTANT account should be
auto-approved, auto-rejected, or flagged for human review on Aqua AI's platform.

You MUST base every decision on verifiable evidence from the provided dossier.
NEVER invent facts. If the dossier is too thin to decide, lean toward "flagged".

Score each account on these dimensions (0-1 each):
  - identity_clarity: real-looking name, complete profile, valid email, no obvious test/spam patterns
  - business_legitimacy: company name, website, business address, plausible bio
  - documentation: presence and plausibility of verification_documents / credentials
  - role_fit: profile content matches the claimed role (breeder vs consultant)
  - risk_signals: trust score, mortality rate, disease rate, is_at_risk flag, suspicious metadata

Compute overall_confidence = weighted mean (identity 0.2, business 0.25, docs 0.25, role 0.15, risk 0.15).
Note that risk_signals contributes INVERSELY for breeders with high mortality/disease.

For each material concern produce a "flag" with:
  - severity: "info" | "warning" | "critical"
  - reason: 1-2 sentence factual statement
  - recommended_solution: concrete next step (e.g. "Request a copy of the breeding licence",
    "Mark account inactive pending document upload", "Email the user requesting clarification on X")

For each remediation YOU can apply automatically to the platform (e.g. set verification_level,
deactivate, request docs), put it in recommended_actions as an object:
  { "action": "set_verification_level", "value": "basic" }
  { "action": "deactivate_pending_docs", "missing": ["business_address","license"] }
  { "action": "send_user_email", "template": "request_documents", "fields": [...] }

Return STRICT JSON ONLY, matching this schema exactly:
{
  "decision_hint": "approve" | "reject" | "flag",
  "overall_confidence": <float 0-1>,
  "scores": { "identity_clarity": <float>, "business_legitimacy": <float>,
              "documentation": <float>, "role_fit": <float>, "risk_signals": <float> },
  "rationale": "<short paragraph>",
  "evidence": [ "<bullet>", "<bullet>", ... ],
  "flags": [ { "severity": "...", "reason": "...", "recommended_solution": "..." }, ... ],
  "recommended_actions": [ { "action": "...", ...extra }, ... ]
}
"""


@dataclass
class AIReviewOutcome:
    decision: str  # approved / rejected / flagged / error
    confidence: float
    rationale: str
    evidence: dict[str, Any]
    recommended_actions: list[dict[str, Any]]
    flags: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# Dossier builders (no PII beyond what's already in the platform)
# ---------------------------------------------------------------------------

def build_breeder_dossier(profile, user) -> dict[str, Any]:
    return {
        "subject_type": "breeder",
        "subject_id": str(profile.id),
        "user": {
            "email": user.email,
            "name": user.name or f"{user.first_name} {user.last_name}".strip(),
            "phone": user.phone or "",
            "is_verified": user.is_verified,
            "date_joined": user.date_joined.isoformat() if user.date_joined else None,
            "verification_documents": user.verification_documents or [],
            "current_trust_score": user.current_trust_score,
            "current_regulatory_tier": user.current_regulatory_tier,
            "is_at_risk": user.is_at_risk,
            "badges_count": user.badges_count,
            "successful_transactions": user.successful_transactions,
            "average_rating": user.average_rating,
            "stock_items_sold": user.stock_items_sold,
            "health_reports_submitted": user.health_reports_submitted,
            "lineage_documented_count": user.lineage_documented_count,
            "last_activity_at": user.last_activity_at.isoformat() if user.last_activity_at else None,
            "overall_score": user.overall_score,
            "responsibility_score": user.responsibility_score,
            "community_score": user.community_score,
            "transaction_score": user.transaction_score,
            "consistency_score": user.consistency_score,
            "data_stewardship_score": user.data_stewardship_score,
            "habitat_stability_score": user.habitat_stability_score,
            "trading_reliability_score": user.trading_reliability_score,
        },
        "profile": {
            "company_name": profile.company_name or "",
            "bio": profile.bio or "",
            "website": profile.website or "",
            "business_phone": profile.business_phone or "",
            "business_address": profile.business_address or "",
            "rating": profile.rating,
            "reviews_count": profile.reviews_count,
            "total_inquiries": profile.total_inquiries,
            "total_responded": profile.total_responded,
            "average_response_hours": profile.average_response_hours,
            "verification_level": profile.verification_level,
            "has_certified_lineage": profile.has_certified_lineage,
            "lineage_documentation_count": profile.lineage_documentation_count,
            "breeding_records_complete": profile.breeding_records_complete,
            "healthy_stock_rate": profile.healthy_stock_rate,
            "stock_mortality_rate": profile.stock_mortality_rate,
            "disease_reported_rate": profile.disease_reported_rate,
            "latitude": profile.latitude,
            "longitude": profile.longitude,
            "total_sales": profile.total_sales,
            "successful_sales": profile.successful_sales,
            "returned_stock_count": profile.returned_stock_count,
            "species_count": profile.species_count,
            "total_stock_sold": profile.total_stock_sold,
            "local_sales_count": profile.local_sales_count,
            "repeat_local_customers": profile.repeat_local_customers,
            "local_trust_score": profile.local_trust_score,
            "specializations": profile.specializations or [],
            "service_area": profile.service_area or "",
            "metadata": profile.metadata or {},
        },
    }


def build_consultant_dossier(profile, user) -> dict[str, Any]:
    return {
        "subject_type": "consultant",
        "subject_id": str(profile.id),
        "user": {
            "email": user.email,
            "name": user.name or f"{user.first_name} {user.last_name}".strip(),
            "phone": user.phone or "",
            "is_verified": user.is_verified,
            "date_joined": user.date_joined.isoformat() if user.date_joined else None,
            "verification_documents": user.verification_documents or [],
            "current_trust_score": user.current_trust_score,
            "current_regulatory_tier": user.current_regulatory_tier,
            "is_at_risk": user.is_at_risk,
            "badges_count": user.badges_count,
            "successful_transactions": user.successful_transactions,
            "average_rating": user.average_rating,
            "consultations_completed": user.consultations_completed,
            "avg_response_time_hours": user.avg_response_time_hours,
            "last_activity_at": user.last_activity_at.isoformat() if user.last_activity_at else None,
            "overall_score": user.overall_score,
            "responsibility_score": user.responsibility_score,
            "community_score": user.community_score,
            "transaction_score": user.transaction_score,
            "consistency_score": user.consistency_score,
            "data_stewardship_score": user.data_stewardship_score,
            "habitat_stability_score": user.habitat_stability_score,
            "trading_reliability_score": user.trading_reliability_score,
        },
        "profile": {
            "company_name": profile.company_name or "",
            "bio": profile.bio or "",
            "website": profile.website or "",
            "business_phone": profile.business_phone or "",
            "business_address": profile.business_address or "",
            "rating": profile.rating,
            "reviews_count": profile.reviews_count,
            "verification_level": profile.verification_level,
            "credentials": profile.credentials or [],
            "latitude": profile.latitude,
            "longitude": profile.longitude,
            "total_bookings": profile.total_bookings,
            "completed_bookings": profile.completed_bookings,
            "cancelled_bookings": profile.cancelled_bookings,
            "no_show_count": profile.no_show_count,
            "completion_rate": profile.completion_rate,
            "cancellation_rate": profile.cancellation_rate,
            "complaint_count": profile.complaint_count,
            "average_response_time_hours": profile.average_response_time_hours,
            "fast_responses_count": profile.fast_responses_count,
            "total_inquiries": profile.total_inquiries,
            "repeated_clients_count": profile.repeated_clients_count,
            "overall_score": profile.overall_score,
            "professionalism_score": profile.professionalism_score,
            "reliability_score": profile.reliability_score,
            "responsiveness_score": profile.responsiveness_score,
            "expertise_score": profile.expertise_score,
            "specializations": profile.specializations or [],
            "services_list": profile.services_list or [],
            "metadata": profile.metadata or {},
        },
    }


# ---------------------------------------------------------------------------
# OpenAI call
# ---------------------------------------------------------------------------

def _is_placeholder_key(key: str) -> bool:
    return not key or "REPLACE" in key.upper() or key == "sk-REPLACE-WITH-YOUR-GPT-4-KEY"


def call_gpt4(dossier: dict[str, Any]) -> AIReviewOutcome:
    api_key = getattr(settings, "OPENAI_API_KEY", "")
    model = getattr(settings, "OPENAI_MODEL", "gpt-4o")

    if _is_placeholder_key(api_key):
        return AIReviewOutcome(
            decision="error",
            confidence=0.0,
            rationale="",
            evidence={},
            recommended_actions=[],
            flags=[],
            raw={},
            model=model,
            error="OPENAI_API_KEY is not set. Paste your real GPT-4 key into .env.",
        )

    try:
        from openai import OpenAI
    except ImportError:
        return AIReviewOutcome(
            decision="error", confidence=0.0, rationale="", evidence={},
            recommended_actions=[], flags=[], raw={}, model=model,
            error="openai package not installed. Run: pip install -r requirements.txt",
        )

    client = OpenAI(api_key=api_key)

    try:
        completion = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            temperature=0.1,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",
                 "content": "Review this signup dossier and return the JSON decision:\n"
                            + json.dumps(dossier, default=str)},
            ],
        )
        content = completion.choices[0].message.content or "{}"
        raw = json.loads(content)
    except Exception as exc:
        logger.exception("OpenAI call failed")
        return AIReviewOutcome(
            decision="error", confidence=0.0, rationale="", evidence={},
            recommended_actions=[], flags=[], raw={}, model=model, error=str(exc),
        )

    confidence = float(raw.get("overall_confidence") or 0.0)
    confidence = max(0.0, min(1.0, confidence))
    hint = (raw.get("decision_hint") or "").lower()
    approve_t = float(getattr(settings, "AI_APPROVE_THRESHOLD", 0.80))
    reject_t = float(getattr(settings, "AI_REJECT_THRESHOLD", 0.30))

    if hint == "reject" or confidence <= reject_t:
        decision = "rejected"
    elif hint == "approve" and confidence >= approve_t:
        decision = "approved"
    elif hint == "flag":
        decision = "flagged"
    else:
        decision = "flagged"

    return AIReviewOutcome(
        decision=decision,
        confidence=confidence,
        rationale=str(raw.get("rationale", "")),
        evidence={"bullets": raw.get("evidence", []), "scores": raw.get("scores", {})},
        recommended_actions=list(raw.get("recommended_actions", [])),
        flags=list(raw.get("flags", [])),
        raw=raw,
        model=model,
        error="",
    )
