"""GPT-powered review pipeline with balanced hybrid policy enforcement."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

from .intelligence_adapter import build_signup_intelligence
from .openai_runtime import get_openai_runtime_config
from .supabase_edge import has_signup_review_function, invoke_json, signup_review_url

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are the Aqua AI signup-review intelligence. You decide whether a NEW
breeder or consultant account should be APPROVED, FLAGGED for human review, or REJECTED.

WHO YOU ARE REVIEWING
Most applicants are ordinary people starting out: a hobby or small breeder, a local store,
an independent consultant. They may have NO website, NO reviews, NO transaction history and
FEW or NO uploaded documents. That is completely normal for a newcomer and MUST NEVER count
against them. Your job is to separate PLAUSIBLE newcomers from SCAM / ABUSE profiles — not to
demand a polished business.

GOLDEN RULES
1. Absence is neutral, not guilt. Missing website, empty history, no reviews, brand-new
   account, no documents => treat as UNKNOWN/NEUTRAL. Never lower a score or reject for it.
2. Reject ONLY on concrete, material evidence of fraud, abuse, impersonation, or safety risk.
   When unsure, FLAG — never reject on doubt.
3. Judge coherence, not polish. A believable, internally-consistent profile (a real-sounding
   name + a stated profession/store/experience + a location or specialization) is a GOOD
   signal even with nothing else.
4. Use ONLY the dossier. Never invent facts. If a field is empty, note "not provided" — do
   not assume the worst.
5. Fairness does not lower your guard: still catch the specific scam red flags below.

SCORING RUBRIC — score each dimension 0.0-1.0. Start from the newcomer BASELINE, then move
DOWN only for concrete negative signals, or UP for positive evidence. NEVER output 0 for a
dimension merely because data is missing.

- identity_clarity (weight 0.18): is this a real, coherent identity (not bot/spam/test)?
    0.85-1.0 real name + valid-looking email + coherent profile.
    0.60-0.80 BASELINE: plausible name + email, sparse but sensible (normal new signup).
    0.30-0.50 odd but not clearly fake (very generic, name mismatches store).
    0.00-0.20 clear test/spam/gibberish (test, asdf, fake, random strings), disposable/fake
              email, or "contact" fields that are actually off-platform handles.

- business_legitimacy (weight 0.20): does the stated activity sound like a real operation?
    0.80-1.0 clear description of what they breed/advise on + a store/company name + a
             location OR specialization.
    0.55-0.75 BASELINE: states a profession/store + a short bio. NO WEBSITE NEEDED. A named
             sole trader with a sentence of experience sits here (normal new signup).
    0.30-0.50 extremely vague with nothing else, but not suspicious.
    0.00-0.20 incoherent, contradictory, plagiarized boilerplate, or a "business" that is
             really an ad to take contact off-platform.
    Do NOT require a website, address, or registration; their absence caps this at baseline,
    it does not push below it.

- documentation (weight 0.18): verification docs / credentials — a BONUS, not a gate.
    0.85-1.0 relevant licence/credential/ID provided and plausible.
    0.55-0.65 BASELINE: none provided. This is normal — score neutral, NEVER 0.
    0.10-0.30 a document is provided but looks forged, mismatched, or irrelevant.

- role_fit (weight 0.14): does the profile match the role they signed up as?
    0.80-1.0 content clearly fits (breeder: species/breeding/stock; consultant: advisory/expertise).
    0.55-0.70 BASELINE: role stated, minimal detail, nothing contradictory.
    0.20-0.40 content clearly belongs to the OTHER role or is contradictory.

- trust_risk (weight 0.18) — HIGHER = SAFER. Score how safe this person is.
    0.80-1.0 BASELINE: no incidents, no penalties, not at-risk = SAFE. A clean new account
             belongs near the TOP. An empty risk history is GOOD news, never a risk.
    0.40-0.70 minor/old penalties or a low-but-not-alarming trust score.
    0.10-0.30 at-risk flag, meaningful incident penalties, or (breeder) high mortality/disease.
    0.00 active critical/high incident.

- behavioural_intelligence (weight 0.12) — HIGHER = SAFER. Abuse/scam behaviour signals.
    0.80-1.0 BASELINE: no messages/payments/disputes yet = no bad behaviour = high score.
    0.30-0.60 soft signals (some off-platform/urgency language, a couple of failed bookings).
    0.00-0.20 repeated payment-bypass attempts, >=5 payment failures, fraud markers, or a
             pattern of failed/no-show/refunded dealings.

overall_confidence = weighted mean:
  0.18*identity_clarity + 0.20*business_legitimacy + 0.18*documentation
  + 0.14*role_fit + 0.18*trust_risk + 0.12*behavioural_intelligence
A clean, plausible newcomer with the baselines above should land ~0.70-0.80 — comfortably
approvable — WITHOUT a website, documents, or history.

SCAM / RED-FLAG CATALOG — actively look for these; lower the relevant score and raise a flag:
- Identity fakery: test/spam/gibberish names, disposable-domain or nonsensical email, name
  that does not match the store/company.
- Off-platform funneling: bio/phone/website says "message me on WhatsApp/Telegram", "email me
  directly", social handles instead of a business (classic take-it-offline scam move).
- Payment bypass: any push to bank transfer, PayPal friends & family, Cash App, Zelle, crypto,
  wire, or "pay outside the app".
- Impersonation / plagiarism: copied generic boilerplate bio, claims of being an official/known
  entity without support, stolen-looking branding.
- Internal contradictions: claims deep experience but everything empty; location/service-area
  contradicts the stated address; role mismatch.
- Unrealistic claims on a brand-new account (e.g. "thousands of sales" with zero history) —
  flag the inconsistency.
- Pre-existing risk in the dossier: active incidents, at-risk flag, repeated payment failures,
  high mortality/disease (breeders), or any hard_blocks already detected.

SEVERITY: critical = clear fraud/safety (payment-bypass push, forged docs, impersonation,
active critical incident) -> reject or strong flag. warning = suspicious but unproven (vague +
off-platform hint) -> flag. info = minor completeness gaps -> approve with a note.

DECISION POLICY (fairness-first)
- APPROVE when: no hard red flags, identity plausible, stated activity coherent, role matches,
  no critical/behavioural risk. A sparse-but-honest newcomer with NO website MUST be approved.
- FLAG when: genuinely ambiguous — a soft off-platform hint, an unverifiable big claim, or a
  profile so empty you cannot tell if it is real (thin evidence). Prefer FLAG over reject.
- REJECT only when: a hard block fires (test/spam identity, repeated payment-bypass, >=5 payment
  failures, active critical incident) or there is concrete, material fraud/abuse/impersonation.

Every flag MUST include a recommended_solution that UNBLOCKS a legitimate newcomer (e.g. "Ask
the applicant to confirm their store name and what species they breed"), never a dead end.

For remediations YOU can apply automatically, add recommended_actions objects, e.g.:
  { "action": "set_verification_level", "value": "basic" }
  { "action": "deactivate_pending_docs", "missing": ["business_address","license"] }
  { "action": "send_user_email", "template": "request_clarification", "fields": ["company_name"] }

Return STRICT JSON ONLY, matching this schema exactly:
{
  "decision_hint": "approve" | "flag" | "reject",
  "overall_confidence": <float 0-1>,
  "scores": { "identity_clarity": <float>, "business_legitimacy": <float>,
              "documentation": <float>, "role_fit": <float>, "trust_risk": <float>,
              "behavioural_intelligence": <float> },
  "newcomer_no_history": <true|false>,
  "rationale": "<short paragraph; state plainly when low data is normal, not a risk>",
  "evidence": [ "<fact from dossier>", ... ],
  "flags": [ { "severity": "info|warning|critical", "reason": "<factual>", "recommended_solution": "<newcomer-friendly next step>" }, ... ],
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


def build_breeder_dossier(profile, user) -> dict[str, Any]:
    intelligence = build_signup_intelligence("breeder", profile, user)
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
        "intelligence": intelligence,
    }


def build_consultant_dossier(profile, user) -> dict[str, Any]:
    intelligence = build_signup_intelligence("consultant", profile, user)
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
        "intelligence": intelligence,
    }


def _is_placeholder_key(key: str) -> bool:
    return not key or "REPLACE" in key.upper() or key == "sk-REPLACE-WITH-YOUR-GPT-4-KEY"


def call_gpt4(dossier: dict[str, Any]) -> AIReviewOutcome:
    if has_signup_review_function():
        edge_result = invoke_json(
            signup_review_url(),
            {
                "kind": "signup_review",
                "dossier": dossier,
                "system_prompt": SYSTEM_PROMPT,
            },
        )
        if edge_result.ok:
            raw = edge_result.payload or {}
            confidence = float(raw.get("overall_confidence") or 0.0)
            confidence = max(0.0, min(1.0, confidence))
            hint = str(raw.get("decision_hint", "")).lower()
            scores = _normalise_scores(raw.get("scores", {}))
            decision, decision_basis = _balanced_decision(dossier, raw, scores, confidence, hint)
            return AIReviewOutcome(
                decision=decision,
                confidence=confidence,
                rationale=str(raw.get("rationale", "")),
                evidence={
                    "bullets": raw.get("evidence", []),
                    "scores": scores,
                    "decision_basis": decision_basis,
                    "runtime_source": "supabase_edge_function",
                },
                recommended_actions=list(raw.get("recommended_actions", [])),
                flags=list(raw.get("flags", [])),
                raw=raw,
                model=str(raw.get("model", "supabase-edge-signup-review")),
                error="",
            )
        logger.warning("Supabase signup-review function failed: %s", edge_result.error)

    runtime = get_openai_runtime_config()
    api_key = runtime.key
    model = runtime.model or str(getattr(settings, "OPENAI_MODEL", "gpt-4o")).strip()

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
            error=runtime.error or "OpenAI runtime key is not configured from Supabase edge function or environment.",
        )

    try:
        from openai import OpenAI
    except ImportError:
        return AIReviewOutcome(
            decision="error",
            confidence=0.0,
            rationale="",
            evidence={},
            recommended_actions=[],
            flags=[],
            raw={},
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
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": "Review this signup dossier and return the JSON decision:\n"
                    + json.dumps(dossier, default=str),
                },
            ],
        )
        content = completion.choices[0].message.content or "{}"
        raw = json.loads(content)
    except Exception as exc:
        logger.exception("OpenAI call failed")
        return AIReviewOutcome(
            decision="error",
            confidence=0.0,
            rationale="",
            evidence={},
            recommended_actions=[],
            flags=[],
            raw={},
            model=model,
            error=str(exc),
        )

    confidence = float(raw.get("overall_confidence") or 0.0)
    confidence = max(0.0, min(1.0, confidence))
    hint = str(raw.get("decision_hint", "")).lower()
    scores = _normalise_scores(raw.get("scores", {}))
    decision, decision_basis = _balanced_decision(dossier, raw, scores, confidence, hint)

    return AIReviewOutcome(
        decision=decision,
        confidence=confidence,
        rationale=str(raw.get("rationale", "")),
        evidence={
            "bullets": raw.get("evidence", []),
            "scores": scores,
            "decision_basis": decision_basis,
        },
        recommended_actions=list(raw.get("recommended_actions", [])),
        flags=list(raw.get("flags", [])),
        raw=raw,
        model=model,
        error="",
    )


# Neutral fallback for a dimension the model omitted or returned unparseably. It
# is deliberately NOT 0.0: a missing score means "unknown", and for a newcomer with
# little history that is normal, not a red flag. Zeroing it would silently drag an
# honest new applicant toward rejection. 0.6 is the benign "no negative signal" baseline.
_NEUTRAL_SCORE = 0.6


def _normalise_scores(scores: dict[str, Any]) -> dict[str, float]:
    expected = [
        "identity_clarity",
        "business_legitimacy",
        "documentation",
        "role_fit",
        "trust_risk",
        "behavioural_intelligence",
    ]
    scores = scores or {}
    normalized = {}
    for key in expected:
        raw = scores.get(key, None)
        if raw is None:
            normalized[key] = _NEUTRAL_SCORE
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = _NEUTRAL_SCORE
        normalized[key] = max(0.0, min(1.0, value))
    return normalized


def _balanced_decision(
    dossier: dict[str, Any],
    raw: dict[str, Any],
    scores: dict[str, float],
    confidence: float,
    hint: str,
) -> tuple[str, dict[str, Any]]:
    intelligence = dossier.get("intelligence", {})
    hard_blocks = list(intelligence.get("hard_blocks", []))
    thin_evidence = bool(intelligence.get("thin_evidence"))
    identity = intelligence.get("identity", {})
    role_fit = intelligence.get("role_fit", {})
    approve_t = float(getattr(settings, "AI_APPROVE_THRESHOLD", 0.45))
    reject_t = float(getattr(settings, "AI_REJECT_THRESHOLD", 0.10))

    business_required = [
        bool(dossier.get("profile", {}).get("company_name")),
        bool(dossier.get("profile", {}).get("bio")),
    ]
    docs_present = bool(dossier.get("user", {}).get("verification_documents")) or bool(dossier.get("profile", {}).get("credentials"))
    required_identity_ok = identity.get("missing_required_count", 0) <= 1
    role_required_ok = role_fit.get("missing_required_count", 0) <= 1
    no_critical_flags = not any((flag.get("severity") or "").lower() == "critical" for flag in raw.get("flags", []))
    business_fields_ok = sum(1 for present in business_required if present) >= 1
    clear_reject_signal = (
        any((flag.get("severity") or "").lower() == "critical" for flag in raw.get("flags", []))
        or scores.get("trust_risk", 1.0) < 0.20
        or scores.get("behavioural_intelligence", 1.0) < 0.20
    )

    if hard_blocks:
        decision = "rejected"
        reason = "Hard-block signals were detected."
    elif confidence < reject_t and (
        hint == "reject"
        and clear_reject_signal
        and not thin_evidence
    ):
        decision = "rejected"
        reason = "Composite risk is below the reject threshold with supporting evidence."
    elif (
        required_identity_ok
        and business_fields_ok
        and role_required_ok
        and no_critical_flags
        and not hard_blocks
        and (
            confidence >= approve_t
            or (hint != "reject" and not thin_evidence and not clear_reject_signal)
        )
    ):
        decision = "approved"
        reason = "The profile is plausible, passes the core checks, and does not show strong risk signals."
    elif thin_evidence:
        decision = "flagged"
        reason = "Evidence is too thin for safe automatic approval."
    else:
        decision = "flagged"
        reason = "The account is plausible but needs manual review under the balanced policy."

    return decision, {
        "policy": "balanced",
        "approve_threshold": approve_t,
        "reject_threshold": reject_t,
        "decision_hint": hint or "flag",
        "hard_blocks": hard_blocks,
        "thin_evidence": thin_evidence,
        "required_identity_ok": required_identity_ok,
        "business_fields_ok": business_fields_ok,
        "docs_present": docs_present,
        "role_required_ok": role_required_ok,
        "no_critical_flags": no_critical_flags,
        "clear_reject_signal": clear_reject_signal,
        "reason": reason,
    }
