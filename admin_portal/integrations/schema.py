"""Validation for the inbound CRM dossier event.

Mirrors ``aquaV2CrmDossierEventSchema`` in the CRM's
``shared/aquaV2Integration.ts`` (lines 177-264). Written by hand rather than
with a schema library so the rules and their line references stay readable, and
so the failure codes line up with the CRM's own receiver.

Rule of thumb carried over from the Zod definitions: the **envelope** is strict
(an unknown top-level key is a rejection) while the objects inside ``data`` are
passthrough (an unknown key is carried, not rejected). That asymmetry is
deliberate -- it lets the CRM add evidence fields in a 2.x minor without
breaking this receiver.
"""
from __future__ import annotations

from . import contract as c

# Enumerations, from the TypeScript line noted against each.
SOURCE_TYPES = ("companies_house", "website", "social", "directory", "licence_register")   # :177
LICENCE_STATUSES = ("verified_current", "pending", "failed", "not_found", "unavailable")   # :178
MATCH_CONFIDENCES = ("high", "medium", "low", "review_required")                           # :179
MATCH_RESULTS = ("confirmed", "not_found", "unavailable", "declared_unverified")           # :189
CONFLICT_SEVERITIES = ("low", "medium", "high", "critical")                                # :198
VERIFICATION_METHODS = ("public_register", "operator_public_register", "no_public_register")  # :207
DOSSIER_STATUSES = ("complete", "partial", "conflicts_found", "not_found")                 # :216
BUSINESS_STRUCTURES = ("limited_company", "llp", "sole_trader", "partnership", "unknown")  # :223
COMPANIES_HOUSE_STATUSES = (
    "verified_active", "verified_inactive", "not_found", "not_applicable", "unavailable")  # :226
ENVIRONMENTS = ("sandbox", "staging", "production")


class Invalid(Exception):
    """Payload violates the contract. ``path`` is for the operator, not the wire."""

    def __init__(self, path: str, detail: str):
        super().__init__(f"{path}: {detail}")
        self.path = path
        self.detail = detail


def _obj(value, path):
    if not isinstance(value, dict):
        raise Invalid(path, "must be an object")
    return value


def _text(value, path, *, minimum=1, maximum=10_000, required=True, nullable=False):
    if value is None:
        if nullable or not required:
            return None
        raise Invalid(path, "is required")
    if not isinstance(value, str):
        raise Invalid(path, "must be a string")
    if len(value) < minimum:
        raise Invalid(path, f"must be at least {minimum} character(s)")
    if len(value) > maximum:
        raise Invalid(path, f"must be at most {maximum} characters")
    return value


def _enum(value, path, allowed, *, required=True):
    if value is None and not required:
        return None
    if value not in allowed:
        raise Invalid(path, f"must be one of {', '.join(allowed)}")
    return value


def _positive_int(value, path, *, required=True):
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise Invalid(path, "must be a positive integer")
    return value


def _timestamp(value, path):
    if not isinstance(value, str) or not c.is_rfc3339_ms(value):
        raise Invalid(path, "must be RFC 3339 UTC with milliseconds (…THH:MM:SS.mmmZ)")
    return value


def _array(value, path, maximum):
    if not isinstance(value, list):
        raise Invalid(path, "must be an array")
    if len(value) > maximum:
        raise Invalid(path, f"must hold at most {maximum} entries")
    return value


def _validate_source(raw, path):
    item = _obj(raw, path)
    _enum(item.get("source_type"), f"{path}.source_type", SOURCE_TYPES)
    _text(item.get("source_name"), f"{path}.source_name", maximum=255)
    _timestamp(item.get("checked_at"), f"{path}.checked_at")
    _text(item.get("finding"), f"{path}.finding", maximum=2000)
    _enum(item.get("match_result"), f"{path}.match_result", MATCH_RESULTS)


def _validate_conflict(raw, path):
    item = _obj(raw, path)
    _text(item.get("field"), f"{path}.field", maximum=160)
    _timestamp(item.get("checked_at"), f"{path}.checked_at")
    _enum(item.get("severity"), f"{path}.severity", CONFLICT_SEVERITIES)


def _validate_licence(raw, path):
    item = _obj(raw, path)
    if item.get("licence_type") != "breeder":
        raise Invalid(f"{path}.licence_type", "must be 'breeder'")
    _enum(item.get("verification_status"), f"{path}.verification_status", LICENCE_STATUSES)
    _enum(item.get("verification_method"), f"{path}.verification_method", VERIFICATION_METHODS)
    _timestamp(item.get("checked_at"), f"{path}.checked_at")
    _text(item.get("finding"), f"{path}.finding", maximum=2000)


def validate_dossier(raw) -> dict:
    """Validate ``data.dossier`` (TypeScript :213-246)."""
    d = _obj(raw, "data.dossier")
    _text(d.get("dossier_id"), "data.dossier.dossier_id", maximum=160)
    _positive_int(d.get("revision"), "data.dossier.revision")
    _enum(d.get("status"), "data.dossier.status", DOSSIER_STATUSES)
    _timestamp(d.get("assembled_at"), "data.dossier.assembled_at")
    _text(d.get("match_status"), "data.dossier.match_status", maximum=160)
    _enum(d.get("match_confidence"), "data.dossier.match_confidence", MATCH_CONFIDENCES)

    identity = _obj(d.get("company_identity"), "data.dossier.company_identity")
    _text(identity.get("declared_name"), "data.dossier.company_identity.declared_name", maximum=255)
    _enum(identity.get("business_structure"),
          "data.dossier.company_identity.business_structure", BUSINESS_STRUCTURES)

    house = _obj(d.get("companies_house"), "data.dossier.companies_house")
    _enum(house.get("status"), "data.dossier.companies_house.status", COMPANIES_HOUSE_STATUSES)
    _timestamp(house.get("checked_at"), "data.dossier.companies_house.checked_at")

    website = _obj(d.get("website_and_domain"), "data.dossier.website_and_domain")
    _timestamp(website.get("checked_at"), "data.dossier.website_and_domain.checked_at")

    for index, item in enumerate(_array(d.get("licences"), "data.dossier.licences", 20)):
        _validate_licence(item, f"data.dossier.licences[{index}]")
    for index, item in enumerate(_array(d.get("sources"), "data.dossier.sources", 100)):
        _validate_source(item, f"data.dossier.sources[{index}]")
    for index, item in enumerate(_array(d.get("conflicts"), "data.dossier.conflicts", 100)):
        _validate_conflict(item, f"data.dossier.conflicts[{index}]")

    _text(d.get("summary"), "data.dossier.summary", maximum=4000)
    return d


ENVELOPE_KEYS = {
    "schema_version", "event_id", "event_type", "occurred_at", "correlation_id",
    "producer", "data",
}


def validate_dossier_event(payload) -> dict:
    """Validate a whole ``crm.provider.evidence_dossier.ready`` event.

    Raises :class:`Invalid`. Returns the payload on success.
    """
    event = _obj(payload, "payload")

    # The envelope is .strict() (TypeScript :264) — unknown top-level keys are
    # a contract violation, not something to ignore.
    unknown = set(event) - ENVELOPE_KEYS
    if unknown:
        raise Invalid("payload", f"unknown top-level field(s): {', '.join(sorted(unknown))}")
    missing = ENVELOPE_KEYS - set(event)
    if missing:
        raise Invalid("payload", f"missing field(s): {', '.join(sorted(missing))}")

    if event["schema_version"] != c.SCHEMA_VERSION:
        raise Invalid("schema_version", f"must be {c.SCHEMA_VERSION}")
    if event["event_type"] not in c.CRM_EVENT_TYPES:
        raise Invalid("event_type", "is not a CRM event this receiver accepts")
    if not isinstance(event["event_id"], str) or not c.is_event_id(event["event_id"]):
        raise Invalid("event_id", "must be a ULID or UUIDv7")
    _timestamp(event["occurred_at"], "occurred_at")
    if not isinstance(event["correlation_id"], str) or not c.is_profile_id(event["correlation_id"]):
        raise Invalid("correlation_id", "must be a UUID")

    producer = _obj(event["producer"], "producer")
    if set(producer) - {"system", "environment"}:
        raise Invalid("producer", "accepts only system and environment")
    if producer.get("system") != "aqua-provider-crm":
        raise Invalid("producer.system", "must be aqua-provider-crm")
    _enum(producer.get("environment"), "producer.environment", ENVIRONMENTS)

    data = _obj(event["data"], "data")
    for field in ("application_id", "aqua_provider_id"):
        value = data.get(field)
        if not isinstance(value, str) or not c.is_profile_id(value):
            raise Invalid(f"data.{field}", "must be a UUID")
    validate_dossier(data.get("dossier"))
    return event
