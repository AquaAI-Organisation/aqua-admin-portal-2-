"""Authoritative field sources for the submitted-profile event (spec §7.2).

The backend's signup path writes the applicant's answers into
``profile.metadata["application_data"]`` and does **not** populate the matching
profile columns. On production today that means, for every provider ever
registered:

    consultant.credentials      empty  (19 of 19)
    consultant.services_list    empty  (19 of 19)
    breeder.service_area        null   (17 of 17)
    breeder.specializations     empty  (17 of 17)

and likewise ``x`` / ``tik_tok`` / ``linkedin``, which exist as columns on
ConsultantProfile but are never written, and do not exist at all on
BreederProfile.

So the rule the spec states, and this module implements, is: read
``metadata.application_data`` first and fall back to a profile column only when
that column actually holds something. Treating an empty legacy column as "the
applicant did not supply this" is what made the clean-case score unreachable.
"""
from __future__ import annotations

import logging
from typing import Any

from . import contract as c

logger = logging.getLogger(__name__)


def application_data(profile) -> dict:
    """The applicant's submitted answers, as stored at signup."""
    metadata = getattr(profile, "metadata", None) or {}
    if not isinstance(metadata, dict):
        return {}
    data = metadata.get("application_data")
    return data if isinstance(data, dict) else {}


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_list(value: Any) -> list[str]:
    """Normalise to a list of non-empty strings, whatever shape was stored."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            text = _clean_text(item)
            if text:
                out.append(text)
        return out
    if isinstance(value, dict):
        return _clean_list(list(value.values()))
    text = _clean_text(value)
    return [text] if text else []


def prefer(submitted: Any, column: Any) -> Any:
    """Submitted answer wins; the column is a fallback only when populated."""
    if isinstance(submitted, (list, tuple, set, dict)) or isinstance(column, (list, tuple, set, dict)):
        from_submitted = _clean_list(submitted)
        return from_submitted if from_submitted else _clean_list(column)
    return _clean_text(submitted) or _clean_text(column)


# --- the four score-bearing fields the review engine must not baseline -----

def credentials_for(profile, *, subject_type: str) -> list[str]:
    """Consultant certifications (spec §7.2 -> metadata.application_data.certifications)."""
    if subject_type != "consultant":
        return []
    data = application_data(profile)
    return prefer(data.get("certifications"), getattr(profile, "credentials", None))


def services_for(profile, *, subject_type: str) -> list[str]:
    if subject_type != "consultant":
        return []
    data = application_data(profile)
    submitted = data.get("services_list", data.get("services"))
    return prefer(submitted, getattr(profile, "services_list", None))


def service_area_for(profile, *, subject_type: str) -> str | None:
    if subject_type != "breeder":
        return None
    data = application_data(profile)
    submitted = data.get("service_area", data.get("business_address"))
    return prefer(submitted, getattr(profile, "service_area", None))


def specializations_for(profile, *, subject_type: str) -> list[str]:
    if subject_type != "breeder":
        return []
    data = application_data(profile)
    submitted = data.get("specializations", data.get("species"))
    return prefer(submitted, getattr(profile, "specializations", None))


# --- social profiles -------------------------------------------------------
#: Spec §7.2: instagram and facebook may come from a populated profile column;
#: x, tiktok and linkedin are authoritative ONLY from application_data, because
#: the columns are never written (and do not exist for breeders).
_SOCIAL_FROM_COLUMNS = ("instagram", "facebook")
_SOCIAL_FROM_APPLICATION_ONLY = {"x": "x", "tiktok": "tik_tok", "linkedin": "linkedin"}


def social_profiles_for(profile) -> dict[str, str | None]:
    data = application_data(profile)
    out: dict[str, str | None] = {}
    for key in _SOCIAL_FROM_COLUMNS:
        out[key] = prefer(data.get(key), getattr(profile, key, None))
    for wire_key, application_key in _SOCIAL_FROM_APPLICATION_ONLY.items():
        out[wire_key] = _clean_text(data.get(application_key) or data.get(wire_key))
    return out


# --- breeder licence -------------------------------------------------------

def breeder_licence_for(user) -> dict[str, str] | None:
    """Latest non-rejected ``marketplace_breederverification`` for this user.

    Spec §7.2 names that table, keyed on ``seller = user.id`` — not a column on
    BreederProfile, and not in the marketplace app's seller profile.
    """
    try:
        from ..models import ExternalBreederVerification
    except Exception:                                  # pragma: no cover
        return None
    try:
        row = (ExternalBreederVerification.objects
               .filter(seller_id=getattr(user, "id", None))
               .exclude(status__iexact="rejected")
               .order_by("-created_at", "-id")
               .first())
    except Exception:
        logger.exception("breeder licence lookup failed for %s", getattr(user, "id", "?"))
        return None
    if row is None or not row.licence_number or not row.issuing_authority or not row.expiry_date:
        # The wire schema requires all three together or ``null``; a partial
        # record is sent as absent rather than as a half-filled object.
        return None
    return {
        "licence_number": str(row.licence_number)[:255],
        "issuing_authority": str(row.issuing_authority)[:255],
        "expires_at": row.expiry_date.isoformat(),
    }


# --- postcode --------------------------------------------------------------
#: Deliberately conservative. Spec §7.2: "Include only when safely parsed."
#: A wrong postcode is worse than none, because the CRM may match on it.
import re  # noqa: E402  (kept beside the pattern it serves)

_UK_POSTCODE = re.compile(
    r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", re.I)


def postal_code_for(profile) -> str | None:
    data = application_data(profile)
    explicit = _clean_text(data.get("postal_code") or data.get("postcode"))
    if explicit:
        return explicit[:32]
    address = _clean_text(data.get("business_address")
                          or getattr(profile, "business_address", None))
    if not address:
        return None
    match = _UK_POSTCODE.search(address)
    if not match:
        return None
    return f"{match.group(1).upper()} {match.group(2).upper()}"[:32]


# --- the event -------------------------------------------------------------

def build_submitted_event(*, review, profile, user, environment: str,
                          event_id: str | None = None, occurred_at=None) -> dict:
    """Assemble ``admin.provider.application.submitted`` (spec §9.2)."""
    from django.utils import timezone

    subject_type = review.subject_type
    moment = occurred_at or timezone.now()
    provider_type = "Breeder" if subject_type == "breeder" else "Consultant"

    company_name = _clean_text(getattr(profile, "company_name", None)) or ""
    website = _clean_text(application_data(profile).get("website")
                          or getattr(profile, "website", None))
    contact_name = _clean_text(getattr(user, "name", None)) or _clean_text(
        f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}")

    application: dict[str, Any] = {
        "application_id": str(review.subject_id),
        "aqua_provider_id": str(getattr(user, "id", "")),
        "provider_type": provider_type,
        "submitted_at": c.rfc3339_ms(moment),
    }
    token = _clean_text(application_data(profile).get("crm_campaign_token"))
    if token and 10 <= len(token) <= 128:
        application["crm_campaign_token"] = token

    company: dict[str, Any] = {"declared_name": company_name[:255]}
    if website:
        company["website_declared"] = website[:500]
    postcode = postal_code_for(profile)
    if postcode:
        company["postal_code"] = postcode

    credentials: dict[str, Any] = {
        "consultant_certifications": [
            item[:255] for item in credentials_for(profile, subject_type=subject_type)
        ],
    }
    if subject_type == "breeder":
        credentials["breeder_licence"] = breeder_licence_for(user)

    return {
        "schema_version": c.SCHEMA_VERSION,
        "event_id": event_id or c.new_ulid(),
        "event_type": "admin.provider.application.submitted",
        "occurred_at": c.rfc3339_ms(moment),
        "correlation_id": str(review.subject_id),
        "producer": {"system": "aqua-admin-portal", "environment": environment},
        "data": {
            "application": application,
            "company": company,
            "contact": {
                "name": (contact_name or "")[:255],
                "email": (getattr(user, "email", "") or "")[:320],
            },
            "social_profiles": social_profiles_for(profile),
            "declared_credentials": credentials,
        },
    }
