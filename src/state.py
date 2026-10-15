"""
This module defines the shared case file each agent reads and writes.

Responsibilities:
- Name the violation labels the models may propose.
- Declare each agent's read keys and its one write key.
- Build a fresh case dict for one run.
"""

from typing import Any, TypedDict

# Labels the intake and enrichment models may propose. Code rejects anything else.
ALLOWED_VIOLATIONS: tuple[str, ...] = (
    "speeding",
    "red_light",
    "dui",
    "reckless",
    "at_fault_accident",
    "suspended_license",
    "other",
)

SEVERE_VIOLATIONS: tuple[str, ...] = ("dui", "reckless", "suspended_license")

_ALLOWED_LOOKUP = {label.lower(): label for label in ALLOWED_VIOLATIONS}


def canonical_violation(label: object) -> str | None:
    """Return the allowed label, ignoring case and surrounding space."""
    if not isinstance(label, str):
        return None
    return _ALLOWED_LOOKUP.get(label.strip().lower())

CONTRACTS: dict[str, dict[str, Any]] = {
    "intake": {"reads": ["raw"], "writes": "applicant"},
    "enrichment": {"reads": ["applicant"], "writes": "enrichment"},
    "risk": {"reads": ["applicant", "enrichment"], "writes": "risk"},
    "recommendation": {
        "reads": ["applicant", "enrichment", "risk"],
        "writes": "decision",
    },
}


class CaseState(TypedDict):
    case_id: str
    raw: Any
    applicant: dict[str, Any] | None
    enrichment: dict[str, Any] | None
    risk: dict[str, Any] | None
    decision: dict[str, Any] | None
    meta: dict[str, Any]


def new_case(case_id: str, raw: Any) -> CaseState:
    return {
        "case_id": case_id,
        "raw": raw,
        "applicant": None,
        "enrichment": None,
        "risk": None,
        "decision": None,
        "meta": {"errors": [], "retries": {}},
    }
