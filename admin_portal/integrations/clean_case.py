"""The §12.3 clean-case approval gate.

The gate is the only path by which an application may be approved without a
person. Six conditions, all of which must hold:

    CRM dossier status        complete
    CRM conflicts             the conflicts array is empty
    CRM business match        match_confidence is "high"
    Companies House outcome   verified_active, or not_applicable for a sole
                              trader or partnership
    Admin review score        at least 0.75
    Admin review flags        no unresolved flags of any severity

The spec is emphatic that an approval engine "MUST NOT infer, downgrade or
waive any condition", so this module reads what is there and never fills a gap
with a default. A condition that cannot be evaluated — a missing dossier, an
absent field, an unrecognised value — fails, because "we could not tell" is not
"it was fine". Each condition reports its own verdict so a refusal can say
which one stopped it rather than leaving an operator to guess.

Nothing here decides anything or writes anything. It answers one question:
would an automatic approval be permitted right now.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: §12.3. The score floor for an automatic approval.
MIN_SCORE = 0.75

#: §11.1. A dossier is terminal in any of these states; only ``complete`` is
#: clean. ``partial``, ``conflicts_found`` and ``not_found`` are terminal
#: answers that still need a person.
TERMINAL_DOSSIER_STATUSES = ("complete", "partial", "conflicts_found", "not_found")

#: §12.3. ``not_applicable`` is for a business with no company record to check,
#: and §12.1 is explicit that it "is not adverse evidence".
ACCEPTABLE_COMPANIES_HOUSE = ("verified_active", "not_applicable")

#: Structures for which no Companies House record is expected.
UNINCORPORATED_STRUCTURES = ("sole_trader", "partnership")


@dataclass
class Condition:
    name: str
    passed: bool
    detail: str


@dataclass
class Assessment:
    """Whether an automatic approval is permitted, and why not when it is not."""
    conditions: list[Condition] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.conditions) and all(c.passed for c in self.conditions)

    @property
    def failures(self) -> list[Condition]:
        return [c for c in self.conditions if not c.passed]

    def reason(self) -> str:
        """One line naming every condition that blocked automatic approval."""
        if self.passed:
            return "every clean-case condition is satisfied"
        return "; ".join(f"{c.name}: {c.detail}" for c in self.failures)

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "conditions": [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in self.conditions
            ],
        }


def _dossier_of(review) -> dict:
    payload = getattr(review, "dossier_payload", None)
    return payload if isinstance(payload, dict) else {}


def assess(review, *, score: float | None = None, flags=None) -> Assessment:
    """Evaluate every §12.3 condition against a review.

    ``score`` and ``flags`` default to the values recorded on the review, and
    are passed explicitly when a caller is deciding on a freshly computed
    outcome that has not been written yet.
    """
    dossier = _dossier_of(review)
    conditions: list[Condition] = []

    # 1. There has to be a dossier at all before anything else can be read.
    dossier_id = (getattr(review, "dossier_id", "") or "").strip()
    status = (getattr(review, "dossier_status", "") or "").strip().lower()
    if not dossier_id:
        conditions.append(Condition(
            "crm_dossier", False,
            "no CRM evidence dossier has been received for this application"))
    elif status not in TERMINAL_DOSSIER_STATUSES:
        conditions.append(Condition(
            "crm_dossier", False,
            f"dossier status {status or 'unknown'!r} is not a terminal state"))
    else:
        conditions.append(Condition(
            "crm_dossier", True, f"dossier {dossier_id} is terminal ({status})"))

    # 2. Only "complete" is clean. The other terminal states are real answers,
    #    but they are answers that need a person.
    conditions.append(Condition(
        "dossier_status_complete", status == "complete",
        "status is complete" if status == "complete"
        else f"status is {status or 'missing'}, which is not complete"))

    # 3. Any conflict at all disqualifies; an absent array is not an empty one.
    conflicts = dossier.get("conflicts")
    if not isinstance(conflicts, list):
        conditions.append(Condition(
            "no_conflicts", False,
            "the dossier does not report a conflicts array"))
    else:
        conditions.append(Condition(
            "no_conflicts", not conflicts,
            "no conflicts found" if not conflicts
            else f"{len(conflicts)} conflict(s) recorded"))

    # 4. Business match must be high confidence.
    confidence = (dossier.get("match_confidence") or "").strip().lower()
    conditions.append(Condition(
        "match_confidence_high", confidence == "high",
        "match confidence is high" if confidence == "high"
        else f"match confidence is {confidence or 'missing'}, not high"))

    # 5. Companies House: verified_active, or not_applicable for a business
    #    with no company record to find. not_applicable is only acceptable
    #    where the declared structure explains it — accepting it for a limited
    #    company would be waiving the condition rather than meeting it.
    house = dossier.get("companies_house")
    house_status = ((house or {}).get("status") or "").strip().lower() \
        if isinstance(house, dict) else ""
    identity = dossier.get("company_identity")
    structure = ((identity or {}).get("business_structure") or "").strip().lower() \
        if isinstance(identity, dict) else ""
    if house_status == "verified_active":
        conditions.append(Condition(
            "companies_house", True, "company record verified active"))
    elif house_status == "not_applicable" and structure in UNINCORPORATED_STRUCTURES:
        conditions.append(Condition(
            "companies_house", True,
            f"not applicable for a {structure.replace('_', ' ')}"))
    elif house_status == "not_applicable":
        conditions.append(Condition(
            "companies_house", False,
            f"not_applicable is only acceptable for a sole trader or "
            f"partnership; this application declares {structure or 'no structure'}"))
    else:
        conditions.append(Condition(
            "companies_house", False,
            f"company record is {house_status or 'missing'}"))

    # 6. Score floor.
    value = score if score is not None else getattr(review, "confidence", None)
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = None
    if numeric is None:
        conditions.append(Condition(
            "score", False, "no Admin review score is available"))
    else:
        conditions.append(Condition(
            "score", numeric >= MIN_SCORE,
            f"score {numeric:.2f} meets the {MIN_SCORE} floor" if numeric >= MIN_SCORE
            else f"score {numeric:.2f} is below the {MIN_SCORE} floor"))

    # 7. Unresolved flags of any severity.
    if flags is None:
        flags = list(review.unresolved_flag_reasons()) if hasattr(
            review, "unresolved_flag_reasons") else []
    flags = list(flags)
    conditions.append(Condition(
        "no_unresolved_flags", not flags,
        "no unresolved flags" if not flags
        else f"{len(flags)} unresolved flag(s): {', '.join(str(f) for f in flags)[:200]}"))

    return Assessment(conditions=conditions)


def is_clean(review, *, score: float | None = None, flags=None) -> bool:
    return assess(review, score=score, flags=flags).passed
